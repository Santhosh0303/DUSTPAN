"""Canonicalise DAX expressions into a comparable fingerprint.

The whole product hangs off this module. Two measures that are textually
different but structurally identical must produce the same fingerprint; two
measures that differ semantically must NEVER produce the same fingerprint.
Those two goals are not symmetric -- a missed duplicate costs the user
nothing, a false one makes them delete a working measure. So every rule
below is one we can argue is *lossless in DAX*, and everything else is left
alone even when it "obviously" looks equivalent.

Rules applied (all provably meaning-preserving in DAX):

1. Comments (``//``, ``--``, ``/* */``) are removed. They carry no meaning.
2. Whitespace is normalised away entirely -- the canonical form is rebuilt
   from the token stream, so layout, indentation and line endings vanish.
3. Identifiers -- function names, keywords, table and column names -- are
   upper-cased (ASCII only). DAX object and function names are
   case-insensitive, so this is lossless. ``ir.Ref.key()`` already treats
   references case-insensitively, which is the same judgement.
4. Redundant quoting on table names is stripped: ``'Sales'[Amount]`` and
   ``Sales[Amount]`` are the same reference. Quotes are kept when the name
   could not survive unquoted (spaces, punctuation, non-ASCII, or a name
   that collides with a DAX keyword).
5. Numeric literals are canonicalised through ``decimal.Decimal`` so
   ``1``, ``1.0``, ``1.00`` and ``1e0`` agree, and ``.5`` becomes ``0.5``.
   The conversion is exact (never via ``float``).

Rules deliberately REJECTED as unsafe (see tests for each):

* No sorting of function arguments, ever -- not even for ``+``, ``*``,
  ``&&`` or ``||``. Argument position is meaningful in DAX
  (``DIVIDE(a, b)``), the boolean operators short-circuit, and
  ``CALCULATE``'s filter order is observable. Order-insensitive comparison
  is still available to detectors through ``Metric.ref_fingerprint()``,
  where it is a *heuristic* and scored below 1.0.
* No unwrapping of function calls: ``CALCULATE(SUM(x), f)`` can never
  reduce to ``SUM(x)``, and ``SUMX(T, T[c])`` can never reduce to
  ``SUM(T[c])``. We only ever rewrite tokens, never remove calls.
* No stripping of redundant parentheses -- that needs a real parser, and a
  mistake there silently changes precedence.
* No alpha-renaming of ``VAR`` names. Correct renaming needs real scope
  analysis (nested VAR blocks, shadowing, names that collide with table
  names); getting it wrong merges two different measures. Cost: two
  identical measures whose variables are named differently are missed.
* ``=`` and ``==`` are kept distinct: in DAX they differ on BLANK.
* String literals are untouched -- case, whitespace and content are
  preserved. DAX compares strings case-insensitively at runtime, but a
  string is often *returned* to the user, and "UK" is not "uk" on screen.
* No rewriting between equivalent-looking functions (``a / b`` vs
  ``DIVIDE(a, b)``, ``&&`` vs ``AND``, ``COUNT`` vs ``COUNTROWS``): they
  differ on blanks, errors or arity.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .. import limits
from ..ir import Metric, Ref
from .lexer import Token, TokenKind, ascii_upper, is_trivia, tokenise

__all__ = [
    "AGGREGATIONS",
    "UNPARSED_PREFIX",
    "NormResult",
    "detect_agg_kind",
    "enrich",
    "extract_refs",
    "normalise",
]


@dataclass
class NormResult:
    """Result of canonicalising one expression. Frozen shape -- see CONTRACT.md."""

    normalised: str
    fingerprint: str  # sha256 hex of `normalised`
    refs: list[Ref]
    agg_kind: str | None
    ok: bool
    error: str | None


#: Marker that prefixes the canonical form of anything we could not parse.
#: It guarantees an unparsed expression can never collide with a parsed one
#: (a canonical DAX expression can never begin with '#'), while two byte-for-byte
#: identical broken expressions still agree with each other.
UNPARSED_PREFIX = "#UNPARSED#"

#: Aggregation functions, used for the best-effort `agg_kind` hint only.
#: This never affects the fingerprint.
AGGREGATIONS = frozenset(
    {
        "SUM",
        "SUMX",
        "AVERAGE",
        "AVERAGEA",
        "AVERAGEX",
        "MIN",
        "MINA",
        "MINX",
        "MAX",
        "MAXA",
        "MAXX",
        "COUNT",
        "COUNTA",
        "COUNTAX",
        "COUNTX",
        "COUNTROWS",
        "COUNTBLANK",
        "DISTINCTCOUNT",
        "DISTINCTCOUNTNOBLANK",
        "APPROXIMATEDISTINCTCOUNT",
        "MEDIAN",
        "MEDIANX",
        "PRODUCT",
        "PRODUCTX",
        "GEOMEAN",
        "GEOMEANX",
        "CONCATENATEX",
        "STDEV.S",
        "STDEV.P",
        "STDEVX.S",
        "STDEVX.P",
        "VAR.S",
        "VAR.P",
        "VARX.S",
        "VARX.P",
        "PERCENTILE.INC",
        "PERCENTILE.EXC",
        "PERCENTILEX.INC",
        "PERCENTILEX.EXC",
    }
)

#: A table name safe to write without quotes. Deliberately ASCII-only and
#: conservative: anything else keeps its quotes (a missed match, never a
#: wrong one).
_BARE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Names that must stay quoted when standing alone, because unquoted they
# would lex as syntax rather than as an object name.
from .lexer import KEYWORDS as _KEYWORDS  # noqa: E402  (kept next to its use)

_NO_SPACE_BEFORE = frozenset({")", ",", "}", ";"})
_NO_SPACE_AFTER = frozenset({"(", "{"})


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_number(text: str) -> str:
    """Canonical form of a numeric literal: ``1.0`` -> ``1``, ``.5`` -> ``0.5``.

    Exact decimal arithmetic only -- going through ``float`` could change the
    value, and a changed value is a changed measure.
    """
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return ascii_upper(text)
    if not value.is_finite():
        return ascii_upper(text)
    if abs(value.adjusted()) > 60:
        # Absurd exponents: keep a compact canonical form rather than
        # expanding to hundreds of digits.
        return ascii_upper(str(value.normalize()))
    out = format(value, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    if out in ("", "-"):
        out += "0"
    return out


def _render_name(name: str) -> str:
    """Canonical rendering of a table name, dropping quotes when redundant."""
    upper = ascii_upper(name)
    if _BARE_NAME_RE.match(name) and upper not in _KEYWORDS:
        return upper
    return "'" + upper.replace("'", "''") + "'"


def _render(token: Token) -> str:
    """Canonical text for one significant token."""
    kind = token.kind
    if kind is TokenKind.STRING:
        # Case and content preserved exactly; only the escaping is normalised.
        return '"' + (token.value or "").replace('"', '""') + '"'
    if kind is TokenKind.NUMBER:
        return canonical_number(token.text)
    if kind is TokenKind.REF:
        column = "[" + ascii_upper(token.column or "").replace("]", "]]") + "]"
        if token.table is None:
            return column
        return _render_name(token.table) + column
    if kind is TokenKind.QUOTED_IDENT:
        return _render_name(token.value or "")
    if kind in (TokenKind.IDENT, TokenKind.KEYWORD):
        return ascii_upper(token.text)
    return token.text


def _join(tokens: list[Token], parts: list[str]) -> str:
    """Glue rendered tokens together.

    A single space separates every pair except around ``( ) , { } ;`` and
    between a function name and its opening bracket. Those characters always
    end a token, so eliding the space can never merge two tokens into a
    different one -- the canonical string re-lexes to exactly this token
    stream, which is what makes the fingerprint injective.
    """
    out: list[str] = []
    for index, part in enumerate(parts):
        if index:
            previous, previous_token = parts[index - 1], tokens[index - 1]
            space = True
            if (
                (previous in _NO_SPACE_AFTER and previous_token.kind is TokenKind.PUNCT)
                or (part in _NO_SPACE_BEFORE and tokens[index].kind is TokenKind.PUNCT)
                or (
                    part == "("
                    and tokens[index].kind is TokenKind.PUNCT
                    and previous_token.kind in (TokenKind.IDENT, TokenKind.KEYWORD)
                )
            ):
                space = False
            if space:
                out.append(" ")
        out.append(part)
    return "".join(out)


def extract_refs(tokens: list[Token]) -> list[Ref]:
    """Every table/column reference, in first-seen order, de-duplicated.

    ``table`` is ``None`` for a bare ``[Measure]`` reference. Names keep
    their original casing (``ir.Ref.key()`` already compares case-insensitively).
    Table-only mentions such as the ``Sales`` in ``COUNTROWS(Sales)`` are NOT
    emitted: ``Ref`` requires a column, and inventing an empty one would
    pollute ``ref_fingerprint()``.
    """
    seen: set[tuple[str | None, str]] = set()
    refs: list[Ref] = []
    for token in tokens:
        if token.kind is not TokenKind.REF or not token.column:
            continue
        key = (
            token.table.casefold() if token.table is not None else None,
            token.column.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        refs.append(Ref(table=token.table, column=token.column))
    return refs


def detect_agg_kind(tokens: list[Token]) -> str | None:
    """Best-effort outermost aggregation, e.g. ``SUM`` for ``CALCULATE(SUM(x), f)``.

    Only the shallowest nesting level that contains an aggregation is
    considered, and only when that level agrees: ``DIVIDE(SUM(a), COUNTROWS(b))``
    is ambiguous and returns ``None``. This is a hint for heuristic grouping
    and never contributes to the fingerprint.
    """
    tokens = [t for t in tokens if not is_trivia(t)]
    best_depth: int | None = None
    names: set[str] = set()
    first: str | None = None
    depth = 0
    for index, token in enumerate(tokens):
        if token.kind is TokenKind.PUNCT:
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            continue
        if token.kind not in (TokenKind.IDENT, TokenKind.KEYWORD):
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is None or following.text != "(":
            continue
        name = ascii_upper(token.text)
        if name not in AGGREGATIONS:
            continue
        if best_depth is None or depth < best_depth:
            best_depth, names, first = depth, {name}, name
        elif depth == best_depth:
            names.add(name)
    if first is None or len(names) != 1:
        return None
    return first


def _first_error(tokens: list[Token]) -> str | None:
    for token in tokens:
        if token.kind is TokenKind.ERROR:
            return f"{token.error} (line {token.line}, column {token.col})"
    return None


def _balance_error(tokens: list[Token]) -> str | None:
    depth = 0
    braces = 0
    for token in tokens:
        if token.kind is not TokenKind.PUNCT:
            continue
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
            if depth < 0:
                return f"unbalanced ')' (line {token.line}, column {token.col})"
        elif token.text == "{":
            braces += 1
        elif token.text == "}":
            braces -= 1
            if braces < 0:
                return f"unbalanced '}}' (line {token.line}, column {token.col})"
    if depth > 0:
        return f"{depth} unclosed '('"
    if braces > 0:
        return f"{braces} unclosed '{{'"
    return None


def _failed(
    expression: str,
    error: str,
    refs: list[Ref] | None = None,
    agg_kind: str | None = None,
) -> NormResult:
    """Canonical form for an expression we could not vouch for.

    Whitespace is collapsed so that the same broken measure copied twice
    still matches itself, but nothing else is rewritten and the
    ``#UNPARSED#`` marker keeps it out of the parsed fingerprint space.
    """
    text = (UNPARSED_PREFIX + " " + " ".join(expression.split())).rstrip()
    return NormResult(text, _sha256(text), refs or [], agg_kind, False, error)


def normalise(expression: str) -> NormResult:
    """Canonicalise a DAX expression. Never raises.

    On success ``ok`` is True and ``fingerprint`` is the sha256 of the
    canonical single-line form. On failure ``ok`` is False, ``error`` says
    why, and the fingerprint covers a whitespace-collapsed copy of the
    original text under the ``#UNPARSED#`` marker.
    """
    size = len(str(expression or "").encode("utf-8", "ignore"))
    if limits.exceeds(size, limits.MAX_EXPRESSION_BYTES):
        return NormResult(
            normalised="",
            fingerprint="",
            refs=[],
            agg_kind=None,
            ok=False,
            error=limits.describe(
                size,
                limits.MAX_EXPRESSION_BYTES,
                "expression",
                "DUSTPAN_MAX_EXPRESSION_BYTES",
            ),
        )
    try:
        raw = (
            expression
            if isinstance(expression, str)
            else ("" if expression is None else str(expression))
        )
        tokens = tokenise(raw)
        refs = extract_refs(tokens)
        agg_kind = detect_agg_kind(tokens)
        meaningful = [t for t in tokens if not is_trivia(t)]

        error = _first_error(meaningful)
        if error is None:
            error = _balance_error(meaningful)
        if error is None and not meaningful:
            error = "empty expression"
        if error is not None:
            return _failed(raw, error, refs, agg_kind)

        parts = [_render(t) for t in meaningful]
        text = _join(meaningful, parts)
        return NormResult(text, _sha256(text), refs, agg_kind, True, None)
    except Exception as exc:  # pragma: no cover - defensive; must never raise
        safe = expression if isinstance(expression, str) else ""
        return _failed(safe, f"internal normaliser error: {exc!r}")


def enrich(metric: Metric) -> None:
    """Run :func:`normalise` on ``metric.expression`` and fill the metric in place.

    Populates ``normalised`` / ``fingerprint`` / ``refs`` / ``agg_kind`` /
    ``lexically_ok`` / ``parse_error``. Never raises: a metric that cannot be
    normalised is marked ``lexically_ok = False`` and left for the ``unparsed``
    finding rather than being silently fingerprinted as if it were understood.
    """
    try:
        expression = getattr(metric, "expression", "") or ""
        if not isinstance(expression, str):
            expression = str(expression)
        dialect = getattr(metric, "dialect", "unknown") or "unknown"
        if isinstance(dialect, str) and dialect.casefold() not in ("dax", "unknown", ""):
            result = _failed(
                expression,
                f"{dialect!r} expressions are not handled by the DAX normaliser",
            )
        else:
            result = normalise(expression)
    except Exception as exc:  # pragma: no cover - defensive
        result = _failed("", f"internal normaliser error: {exc!r}")

    metric.normalised = result.normalised
    metric.fingerprint = result.fingerprint
    metric.refs = list(result.refs)
    metric.agg_kind = result.agg_kind
    metric.lexically_ok = result.ok
    metric.parse_error = result.error

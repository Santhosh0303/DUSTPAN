"""A DAX lexer.

Turns a DAX expression into a flat list of typed :class:`Token` objects.
It is deliberately *lexical only* -- there is no parser here, because a
half-correct parser is worse than none: it would tempt us into rewrites we
cannot prove are safe.

Design rules (see CONTRACT.md -- precision beats recall):

* ``tokenise`` NEVER raises. Anything it cannot understand becomes an
  ``ERROR`` token, which downstream turns into ``lexically_ok = False``.
* Trivia (whitespace and comments) is emitted, never silently dropped, so
  callers decide what to discard.
* ``Table[Column]``, ``'Table Name'[Column]`` and bare ``[Measure]`` all
  collapse into a single ``REF`` token carrying the *decoded* table and
  column names (quoting and ``]]`` / ``''`` escapes already resolved).
  DAX ignores trivia between the table name and the ``[``, so we do too --
  ``Sales /* x */ [Amount]`` is one reference, exactly as the engine reads it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "KEYWORDS",
    "OPERATORS",
    "PUNCTUATION",
    "Token",
    "TokenKind",
    "ascii_upper",
    "is_trivia",
    "significant",
    "tokenise",
]


class TokenKind(StrEnum):
    """Every kind of token the DAX lexer can produce."""

    WHITESPACE = "WHITESPACE"
    LINE_COMMENT = "LINE_COMMENT"  # // ... or -- ...
    BLOCK_COMMENT = "BLOCK_COMMENT"  # /* ... */
    STRING = "STRING"  # "double quoted", "" escapes a quote
    NUMBER = "NUMBER"
    REF = "REF"  # Table[Column] / 'Table'[Column] / [Measure]
    IDENT = "IDENT"  # function or object name
    KEYWORD = "KEYWORD"  # VAR / RETURN / IN / NOT / ...
    QUOTED_IDENT = "QUOTED_IDENT"  # 'Table Name' not followed by [
    OPERATOR = "OPERATOR"
    PUNCT = "PUNCT"  # ( ) , { } ;
    ERROR = "ERROR"


#: Words that are syntax rather than object names. Kept deliberately small:
#: ``MEASURE``/``COLUMN``/``TABLE`` are omitted because they are plausible
#: object names and only act as keywords inside query DEFINE blocks.
KEYWORDS = frozenset(
    {
        "VAR",
        "RETURN",
        "IN",
        "NOT",
        "ORDER",
        "BY",
        "ASC",
        "DESC",
        "START",
        "AT",
        "DEFINE",
        "EVALUATE",
        "TRUE",
        "FALSE",
    }
)

#: Longest first -- the scanner takes the first match.
OPERATORS = (
    "<=",
    ">=",
    "<>",
    "==",
    "&&",
    "||",
    ":=",
    "+",
    "-",
    "*",
    "/",
    "^",
    "&",
    "=",
    "<",
    ">",
)

PUNCTUATION = frozenset("(),{};")

_TRIVIA_KINDS = frozenset(
    {TokenKind.WHITESPACE, TokenKind.LINE_COMMENT, TokenKind.BLOCK_COMMENT}
)

# A bare identifier: a letter or underscore, then word characters. Dotted
# segments are part of the name so that DAX function names such as VAR.S,
# STDEV.P and PERCENTILE.INC lex as one token (and, importantly, so that
# VAR.S is NOT mistaken for the VAR keyword).
_IDENT_RE = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", re.UNICODE)

# 1  1.  1.5  .5  1e3  1.5E-3
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

_ASCII_UPPER_MAP = {c: c - 32 for c in range(ord("a"), ord("z") + 1)}


def ascii_upper(text: str) -> str:
    """Upper-case ASCII letters only.

    DAX object names are case-insensitive, so folding case is lossless --
    but only for ASCII. ``str.upper()`` would map ``ß`` to ``SS`` and could
    therefore merge two genuinely different names, so we never touch
    non-ASCII characters. The cost is a missed duplicate; the alternative is
    a false one.
    """
    return text.translate(_ASCII_UPPER_MAP)


@dataclass(frozen=True)
class Token:
    """One lexical unit.

    ``text`` is the verbatim source slice. ``table`` / ``column`` are set on
    ``REF`` tokens, ``value`` carries the decoded payload of strings and
    quoted identifiers (escapes resolved), and ``error`` explains an
    ``ERROR`` token.
    """

    kind: TokenKind
    text: str
    pos: int = 0
    line: int = 1
    col: int = 1
    table: str | None = None
    column: str | None = None
    value: str | None = None
    error: str | None = None


def is_trivia(token: Token) -> bool:
    """True for whitespace and comments -- tokens with no meaning."""
    return token.kind in _TRIVIA_KINDS


def significant(tokens: list[Token]) -> list[Token]:
    """The token list with whitespace and comments removed."""
    return [t for t in tokens if t.kind not in _TRIVIA_KINDS]


class _Lexer:
    """Single-pass scanner. Every method advances ``self.i`` or emits an error."""

    def __init__(self, text: str) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0
        self.line = 1
        self.col = 1
        self.out: list[Token] = []

    # -- plumbing -------------------------------------------------------
    def _advance_to(self, j: int) -> None:
        chunk = self.s[self.i : j]
        newlines = chunk.count("\n")
        if newlines:
            self.line += newlines
            self.col = len(chunk) - chunk.rfind("\n")
        else:
            self.col += len(chunk)
        self.i = j

    def _emit(self, kind: TokenKind, end: int, **kwargs: object) -> None:
        start, line, col = self.i, self.line, self.col
        text = self.s[start:end]
        self._advance_to(end)
        self.out.append(Token(kind, text, start, line, col, **kwargs))  # type: ignore[arg-type]

    # -- scanners -------------------------------------------------------
    def _scan_delimited(self, delim: str, i: int) -> tuple[int, str | None]:
        """Scan ``delim ... delim`` from ``i``; a doubled delimiter escapes one.

        Returns ``(index_after_closing_delimiter, decoded_value)`` or
        ``(-1, None)`` when the literal is never closed.
        """
        s, n = self.s, self.n
        j = i + 1
        parts: list[str] = []
        while j < n:
            c = s[j]
            if c == delim:
                if j + 1 < n and s[j + 1] == delim:
                    parts.append(delim)
                    j += 2
                    continue
                return j + 1, "".join(parts)
            parts.append(c)
            j += 1
        return -1, None

    def _scan_bracket(self, i: int) -> tuple[int, str | None]:
        """Scan ``[Column]`` from ``i``; ``]]`` escapes a literal ``]``."""
        s, n = self.s, self.n
        j = i + 1
        parts: list[str] = []
        while j < n:
            c = s[j]
            if c == "]":
                if j + 1 < n and s[j + 1] == "]":
                    parts.append("]")
                    j += 2
                    continue
                return j + 1, "".join(parts)
            parts.append(c)
            j += 1
        return -1, None

    def _trivia_end(self, i: int) -> int:
        """Index just past a run of whitespace/comments starting at ``i``.

        An unterminated block comment is not trivia -- scanning stops before
        it so the main loop reports it.
        """
        s, n = self.s, self.n
        while i < n:
            c = s[i]
            if c.isspace():
                i += 1
                continue
            if s.startswith("/*", i):
                k = s.find("*/", i + 2)
                if k < 0:
                    return i
                i = k + 2
                continue
            if s.startswith("//", i) or s.startswith("--", i):
                k = s.find("\n", i)
                i = n if k < 0 else k
                continue
            break
        return i

    def _emit_name_or_ref(self, end: int, name: str, quoted: bool) -> None:
        """Emit a table-qualified REF if a ``[`` follows, else a name token."""
        k = self._trivia_end(end)
        if k < self.n and self.s[k] == "[":
            bracket_end, column = self._scan_bracket(k)
            if bracket_end >= 0 and column:
                self._emit(TokenKind.REF, bracket_end, table=name, column=column)
                return
            # Unterminated or empty [ ... ]: emit the name and let the main
            # loop reach the bracket and report it.
        if quoted:
            self._emit(TokenKind.QUOTED_IDENT, end, value=name)
            return
        kind = TokenKind.KEYWORD if ascii_upper(name) in KEYWORDS else TokenKind.IDENT
        self._emit(kind, end, value=name)

    # -- main loop ------------------------------------------------------
    def run(self) -> None:
        while self.i < self.n:
            before = self.i
            self._step()
            if self.i <= before:  # pragma: no cover - defensive anti-hang guard
                self._emit(TokenKind.ERROR, before + 1, error="lexer made no progress")

    def _step(self) -> None:
        s, n, i = self.s, self.n, self.i
        c = s[i]

        if c.isspace():
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            self._emit(TokenKind.WHITESPACE, j)
            return

        if s.startswith("/*", i):
            k = s.find("*/", i + 2)
            if k < 0:
                self._emit(TokenKind.ERROR, n, error="unterminated block comment")
                return
            self._emit(TokenKind.BLOCK_COMMENT, k + 2)
            return

        # Both // and -- start a line comment in DAX. That means `1--2` is
        # `1` followed by a comment, not a double negation; matching the
        # engine here is the whole point.
        if s.startswith("//", i) or s.startswith("--", i):
            k = s.find("\n", i)
            self._emit(TokenKind.LINE_COMMENT, n if k < 0 else k)
            return

        if c == '"':
            end, value = self._scan_delimited('"', i)
            if end < 0:
                self._emit(TokenKind.ERROR, n, error="unterminated string literal")
                return
            self._emit(TokenKind.STRING, end, value=value)
            return

        if c == "'":
            end, value = self._scan_delimited("'", i)
            if end < 0:
                self._emit(TokenKind.ERROR, n, error="unterminated quoted identifier")
                return
            if not value:
                self._emit(TokenKind.ERROR, end, error="empty quoted identifier")
                return
            self._emit_name_or_ref(end, value, quoted=True)
            return

        if c == "[":
            end, column = self._scan_bracket(i)
            if end < 0:
                self._emit(TokenKind.ERROR, n, error="unterminated [column] reference")
                return
            if not column:
                self._emit(TokenKind.ERROR, end, error="empty [] reference")
                return
            self._emit(TokenKind.REF, end, table=None, column=column)
            return

        if c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = _NUMBER_RE.match(s, i)
            if m is not None:
                self._emit(TokenKind.NUMBER, m.end())
                return

        m = _IDENT_RE.match(s, i)
        if m is not None:
            self._emit_name_or_ref(m.end(), m.group(0), quoted=False)
            return

        for op in OPERATORS:
            if s.startswith(op, i):
                self._emit(TokenKind.OPERATOR, i + len(op))
                return

        if c in PUNCTUATION:
            self._emit(TokenKind.PUNCT, i + 1)
            return

        self._emit(TokenKind.ERROR, i + 1, error=f"unexpected character {c!r}")


def tokenise(expression: str | None) -> list[Token]:
    """Tokenise a DAX expression. Never raises.

    Unrecognised input is reported as ``ERROR`` tokens rather than
    exceptions, so callers can always fingerprint *something* while still
    knowing the expression was not understood.
    """
    if expression is None:
        expression = ""
    elif not isinstance(expression, str):
        expression = str(expression)
    lexer = _Lexer(expression)
    try:
        lexer.run()
    except Exception as exc:  # pragma: no cover - defensive
        lexer.out.append(
            Token(
                TokenKind.ERROR,
                "",
                lexer.i,
                lexer.line,
                lexer.col,
                error=f"internal lexer error: {exc!r}",
            )
        )
    return lexer.out

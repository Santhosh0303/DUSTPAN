"""TMDL (Tabular Model Definition Language) parser.

Reads the `*.SemanticModel/definition/` folder tree that Power BI Desktop
writes for a PBIP project (one `.tmdl` file per table, plus `model.tmdl`,
`relationships.tmdl`, culture files, ...) and extracts every `measure`
object into an `ir.Metric`.

GRAMMAR -- researched, not guessed
-----------------------------------
TMDL is not fully documented as a formal grammar, so this module was built
against Microsoft's own examples (Microsoft Learn, "Tabular Model
Definition Language (TMDL)" overview and object-reference pages, fetched
during development) rather than against `CONTRACT.md`'s fixture alone. The
rules that matter for measure extraction:

* Indentation is **tabs only**, one tab per nesting level. Level 1 is an
  object header (`table Sales`, at column 0). Level 2, one tab in, is
  either a *property* of that object (`formatString: ...`) or a *nested
  object* (`measure 'X' = ...`, `column Y`). properties of THAT nested
  object are one tab deeper again, and so on -- indentation directly
  mirrors object nesting, recursively.
* An object's *default property* can be assigned inline after `=` on the
  header line itself (`measure 'Sales Amount' = SUM(...)`.) When the value
  needs more than one line, the header line ends with a bare `=` and the
  value's lines are indented **one level deeper than where a property of
  that object would sit** -- i.e. two levels deeper than the header, not
  one. This is confirmed by every multi-line example Microsoft publishes:

      measure 'Sales YTD' =                <- header, level N
              var ytd = TOTALYTD(...)      <- level N+2 (content)
              return ytd
          formatString: $ #,##0            <- level N+1 (property)

  This is also exactly what `tests/fixtures/.../Sales.tmdl` does for
  `'Sum of Sales'`, so **the fixture is authentic TMDL and needed no
  correction** -- confirmed against Microsoft Learn during development,
  not assumed.
* Object names need single-quoting only when they contain whitespace, `.`,
  `=`, `:`, `'` or would otherwise collide with a keyword; a doubled `''`
  inside a quoted name escapes a literal `'` (`'Customer''s Total'`).
* Non-expression property values use `:`; a bare keyword with nothing
  after it (`isHidden` alone, no colon) is shorthand for `isHidden: true`.
  A quoted string value follows `"`-escaping (`""` -> `"`), same idea as
  DAX string literals but this is TMDL's OWN quoting, not the DAX lexer's
  -- the two are handled independently even though the escaping rule
  happens to match.
* `/// text` immediately above an object (no blank line in between) is
  its description. A blank line breaks the association.
* `ref table X` references a table defined elsewhere (used by
  `model.tmdl` to declare which tables belong to the model) rather than
  redefining it; in this codebase's fixture it carries no children, and
  the state machine below handles that for free -- it only ever produces
  a measure when it actually finds indented `measure` lines under a
  level-0 header, regardless of whether that header said `table` or
  `ref table`.
* A default-property value of exactly three backticks (`` measure X = ``` ``)
  opens a fenced block: Microsoft Learn's own words are "to enforce a
  different indentation or to preserve trailing blank lines or whitespaces,
  use the three backticks enclosing". Content is then taken verbatim, line
  for line, with no indentation trimming and no early stop on a dedented
  line, until a line that is exactly ` ``` ` closes it. This module handles
  it (rare in Desktop/PBIP output for an ordinary measure, but real), and an
  unterminated fence is reported in `estate.errors` rather than silently
  swallowing the rest of the file.

PRECISION NOTE
--------------
Indentation is trusted **only when it is literal tab characters** --
matching Microsoft's own statement that TMDL indentation is tabs, not
spaces. A file that (incorrectly) uses spaces will simply fail to nest
anything below column 0, so its measures are silently *not found* rather
than mis-attributed to the wrong object. That is the safe failure
direction for this tool: a measure dustpan never saw cannot become a false
duplicate or a false "unused" claim -- it just never entered the estate.
Since real TMDL as written by Power BI Desktop and Tabular Editor is
always tab-indented, this is not expected to bite on real projects.

Two measures are never merged across files or re-parsed twice: each
`measure` header line encountered produces exactly one `Metric`.

Never raises. Malformed input (unreadable file, a measure with no name,
non-tab indentation that leaves it un-nested) degrades gracefully --
either the measure is skipped with a note in `estate.errors`, or, for a
measure whose expression comes out empty or unparsable, it is still
recorded and left for `dax.normalise` / the `unparsed` finding to flag
honestly rather than vanishing without a trace.

Public surface (see CONTRACT.md):
    parse_model(model_dir: str, estate: Estate) -> None
"""

from __future__ import annotations

import os
import re

from dustpan import safeio

from ..ir import Estate, Metric, project_root
from .discover import model_name_from_path

__all__ = ["parse_model"]

# Recognised measure properties -> the Metric field they populate.
_PROPERTY_FIELDS = {
    "formatstring": "format_string",
    "displayfolder": "display_folder",
    "datatype": "data_type",
}

_BARE_NAME_RE = re.compile(r"[^\s=]+")
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def parse_model(model_dir: str, estate: Estate) -> None:
    """Parse every `.tmdl` file under `model_dir` and append the measures found.

    `model_dir` is the `definition/` folder of a `*.SemanticModel` (or the
    legacy `*.Dataset`) PBIP part, as returned by `parsers.discover.discover`
    (kind `"pbip_model"`). The semantic model's name is derived from the
    *folder path* (`model_name_from_path`), never from file contents, so it
    always agrees with the model name a report's `definition.pbir` resolves
    to -- that agreement is what lets `detect/unused.py` match a report's
    measure references back to the right model.

    Never raises: unreadable files and structurally broken measures are
    recorded in `estate.errors` and otherwise skipped.
    """
    try:
        if not os.path.isdir(model_dir):
            estate.errors.append(f"tmdl: {model_dir}: not a directory")
            return

        model = model_name_from_path(model_dir)
        files = _find_tmdl_files(model_dir, estate)
        if not files:
            estate.errors.append(f"tmdl: {model_dir}: no .tmdl files found")
            return

        for path in files:
            _parse_file(path, model, estate)
    except Exception as exc:  # pragma: no cover - defensive; contract: never raise
        estate.errors.append(f"tmdl: {model_dir}: unexpected {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# file discovery
# --------------------------------------------------------------------------


def _find_tmdl_files(model_dir: str, estate: Estate) -> list[str]:
    """Every `.tmdl` file under `model_dir`, recursively, in a stable order.

    Two safety properties, both from an external audit:

    AUD-006 -- `followlinks=False` stops `os.walk` descending into symlinked
    *directories*, but a symlinked regular `.tmdl` is still returned and
    opened. A link planted in a model folder could therefore pull arbitrary
    file content into the estate, and from there into an exported report. Any
    file whose real path escapes `model_dir` is skipped and recorded.

    AUD-016 -- traversal failures used to be silently discarded (a note was
    built and then deleted, and `os.walk` had no `onerror`), so a partly
    unreadable model looked completely scanned. Every failure is now recorded
    on the estate, which also marks the scan degraded.
    """
    out: list[str] = []
    real_dir = os.path.realpath(model_dir)
    prefix = real_dir.rstrip(os.sep) + os.sep

    def _on_error(exc: OSError) -> None:
        estate.errors.append(
            f"tmdl: {model_dir}: could not read '{getattr(exc, 'filename', '?')}' "
            f"while walking: {exc} -- results below are incomplete"
        )

    try:
        walker = os.walk(model_dir, followlinks=False, onerror=_on_error)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if not name.lower().endswith(".tmdl"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    real = os.path.realpath(full)
                except OSError as exc:
                    estate.errors.append(f"tmdl: {full}: could not resolve path: {exc}")
                    continue
                if real != full and not (real == real_dir or real.startswith(prefix)):
                    estate.errors.append(
                        f"tmdl: skipped '{full}' -- it is a link resolving to "
                        f"'{real}', outside the model folder. dustpan does not "
                        "read files from outside the tree it was pointed at."
                    )
                    continue
                out.append(full)
    except OSError as exc:
        estate.errors.append(
            f"tmdl: {model_dir}: error while walking directory: {exc} "
            "-- returning the files found before the failure"
        )
    # Sort so `model.tmdl` (model-level) and `database.tmdl` do not dictate
    # ordering; a stable full-path sort makes output deterministic run to run.
    out.sort()
    return out


# --------------------------------------------------------------------------
# per-file state machine
# --------------------------------------------------------------------------


def _parse_file(path: str, model: str, estate: Estate) -> None:
    try:
        text = safeio.read_text(path, estate, "tmdl")
        if text is None:
            return
    except OSError as exc:
        estate.errors.append(f"tmdl: {path}: could not read file: {exc}")
        return
    except UnicodeDecodeError as exc:
        estate.errors.append(f"tmdl: {path}: not valid UTF-8: {exc}")
        return

    try:
        _parse_text(text, model, path, estate)
    except Exception as exc:  # pragma: no cover - defensive; contract: never raise
        estate.errors.append(f"tmdl: {path}: unexpected {type(exc).__name__}: {exc}")


def _count_tabs(line: str) -> int:
    n = 0
    for ch in line:
        if ch != "\t":
            break
        n += 1
    return n


def _parse_text(text: str, model: str, path: str, estate: Estate) -> None:
    lines = text.splitlines()
    n = len(lines)
    current_table: str | None = None
    pending_description: list[str] = []

    i = 0
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            pending_description = []
            i += 1
            continue

        if stripped.startswith("///"):
            pending_description.append(stripped[3:].strip())
            i += 1
            continue

        indent = _count_tabs(raw)

        if indent == 0:
            current_table = _table_header(stripped)
            pending_description = []
            i += 1
            continue

        if (
            indent == 1
            and current_table is not None
            and _starts_with_word(stripped, "measure")
        ):
            metric, i = _consume_measure(
                lines, i, indent, current_table, model, path, pending_description, estate
            )
            pending_description = []
            if metric is not None:
                estate.metrics.append(metric)
            continue

        # Anything else -- a column/partition/hierarchy/annotation header,
        # a stray deep line outside any measure, or an indent-1 line with no
        # open table context. Not our concern; move on one line at a time.
        pending_description = []
        i += 1


def _starts_with_word(text: str, word: str) -> bool:
    if not text.startswith(word):
        return False
    rest = text[len(word) :]
    return rest == "" or rest[0].isspace() or rest[0] in "'="


def _table_header(text: str) -> str | None:
    """`table Sales` / `ref table Sales` -> `"Sales"`; anything else -> None."""
    if _starts_with_word(text, "table"):
        name, _ = _read_name(text[len("table") :])
        return name or None
    if _starts_with_word(text, "ref"):
        rest = text[len("ref") :].lstrip(" \t")
        if _starts_with_word(rest, "table"):
            name, _ = _read_name(rest[len("table") :])
            return name or None
    return None


def _read_name(text: str) -> tuple[str, str]:
    """Read one TMDL object name (quoted or bare) from the start of `text`.

    Returns `(name, remainder)`. A quoted name uses `'...'` with `''` as an
    escaped literal quote, matching every naming example Microsoft
    publishes for TMDL object names.
    """
    text = text.lstrip(" \t")
    if text.startswith("'"):
        out: list[str] = []
        i = 1
        n = len(text)
        while i < n:
            c = text[i]
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), text[i + 1 :]
            out.append(c)
            i += 1
        # Unterminated quote: return what we scanned rather than nothing --
        # a best-effort name beats silently dropping the whole measure.
        return "".join(out), ""
    m = _BARE_NAME_RE.match(text)
    if not m:
        return "", text
    return m.group(0), text[m.end() :]


def _parse_property_line(text: str) -> tuple[str, str | None]:
    """`"key: value"` -> `("key", "value")`; a bare `"key"` -> `("key", None)`.

    Splits on the first `:` or `=`, whichever comes first (TMDL uses `:` for
    ordinary properties and `=` for annotations/default-property lines) --
    only the first occurrence matters, so a value that itself contains `:`
    (a time format string, say) survives intact.
    """
    idx_colon = text.find(":")
    idx_eq = text.find("=")
    candidates = [x for x in (idx_colon, idx_eq) if x >= 0]
    if not candidates:
        return text.strip(), None
    idx = min(candidates)
    return text[:idx].strip(), text[idx + 1 :].strip()


def _unquote(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('""', '"')
    return value or None


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return True  # bare keyword shorthand, e.g. a lone `isHidden` line
    return value.strip().casefold() not in _FALSE_VALUES


def _consume_measure(
    lines: list[str],
    i: int,
    header_indent: int,
    table_name: str,
    model: str,
    path: str,
    description_lines: list[str],
    estate: Estate,
) -> tuple[Metric | None, int]:
    """Parse the `measure` header at `lines[i]` plus everything indented under it.

    Returns `(metric_or_None, index_of_first_line_after_this_measure)`.
    """
    raw = lines[i]
    content = raw[header_indent:]
    rest = content[len("measure") :].lstrip(" \t")
    name, rest = _read_name(rest)
    rest = rest.strip()

    inline_expr = ""
    fenced = False
    if rest.startswith("="):
        value = rest[1:].strip()
        if value == "```":
            # Triple-backtick fencing: an explicit escape hatch TMDL offers
            # ("to enforce a different indentation or to preserve trailing
            # blank lines or whitespaces, use the three backticks enclosing",
            # per Microsoft Learn) so content between the fences is taken
            # verbatim -- no indentation trimming, no early stop on a line
            # that dedents to or past `header_indent`. Rare in Desktop/PBIP
            # output for a plain measure, but real and worth not choking on.
            fenced = True
        else:
            inline_expr = value
    # else: no '=' on the header line at all -- malformed/incomplete measure.
    # Leave inline_expr empty; whatever (if anything) is indented under it
    # is still collected below, and an all-empty result is caught honestly
    # by dax.normalise as an "empty expression" parse failure rather than
    # invented or silently dropped.

    expr_lines: list[str] = [inline_expr] if inline_expr else []
    format_string: str | None = None
    dynamic_format = False
    #: Which property, if any, owns the lines indented below it right now.
    #: None means the measure's own value expression is still being read.
    owner: str | None = None
    nested: dict[str, list[str]] = {}
    display_folder: str | None = None
    data_type: str | None = None
    lineage_tag: str | None = None
    is_hidden = False

    j = i + 1
    n = len(lines)
    in_fence = fenced
    while j < n:
        raw_j = lines[j]
        if in_fence:
            # Verbatim until the closing fence: blank lines are content
            # (that is the whole point of fencing), and a dedented line is
            # still content, not a signal to stop.
            if raw_j.strip() == "```":
                in_fence = False
                j += 1
                continue
            expr_lines.append(raw_j)
            j += 1
            continue
        if not raw_j.strip():
            j += 1
            continue
        indent_j = _count_tabs(raw_j)
        if indent_j <= header_indent:
            break
        content_j = raw_j[indent_j:]
        if indent_j >= header_indent + 2:
            # AUD-V3-012: a line indented below the measure belongs to the
            # measure's VALUE expression only until the first property line.
            # After that it belongs to whichever property opened last -- a
            # nested `formatStringDefinition` carries its own DAX, and
            # appending it to the value produced the corrupt
            # `SUM(T[X])\nIF([Flag], "0.0", "0%")`. Value DAX and format DAX
            # are separate TMDL objects and are kept separate here.
            if owner is None:
                expr_lines.append(content_j.rstrip())
            else:
                nested.setdefault(owner, []).append(content_j.rstrip())
        else:  # indent_j == header_indent + 1: a property (or annotation) line
            pkey, pvalue = _parse_property_line(content_j.strip())
            key, value = pkey, (pvalue or "")
            owner = key.casefold()
            field = _PROPERTY_FIELDS.get(key.casefold())
            if field == "format_string":
                format_string = _unquote(value)
            elif field == "display_folder":
                display_folder = _unquote(value)
            elif field == "data_type":
                data_type = _unquote(value)
            elif key.casefold() == "ishidden":
                is_hidden = _parse_bool(value)
            elif key.casefold() == "lineagetag":
                lineage_tag = _unquote(value)
            elif key.casefold() == "formatstringdefinition":
                # A dynamic format string is a SEPARATE DAX expression that
                # decides how the measure is displayed. Two measures with an
                # identical value expression can still render differently, so
                # its presence blocks any interchangeability claim. We record
                # the fact, not the expression -- knowing it exists is enough
                # to refuse, and parsing it is not needed for that.
                dynamic_format = True
        j += 1
    if in_fence:
        estate.errors.append(
            f"tmdl: {path}: line {i + 1}: measure '{name or '?'}' opened a ``` "
            "fence that was never closed -- expression may be truncated or run "
            "past its intended end"
        )

    if not name:
        estate.errors.append(
            f"tmdl: {path}: line {i + 1}: 'measure' with no readable name -- skipped"
        )
        return None, j

    expression = "\n".join(expr_lines).strip()
    description = "\n".join(d for d in description_lines if d).strip() or None

    extra: dict[str, object] = {"table": table_name}
    if lineage_tag:
        extra["lineage_tag"] = lineage_tag

    if "formatstringdefinition" in nested:
        dynamic_format = True
    if dynamic_format:
        extra = {**(extra or {}), "dynamic_format": True}
        fmt_lines = nested.get("formatstringdefinition")
        if fmt_lines:
            # Preserved as its own object, never merged into the value DAX.
            extra["format_string_expression"] = "\n".join(fmt_lines).strip()
    metric = Metric(
        id=f"powerbi:{model}:{name}",
        name=name,
        tool="powerbi",
        model=model,
        source_path=path,
        source_root=project_root(path),
        expression=expression,
        dialect="dax",
        description=description,
        display_folder=display_folder,
        format_string=format_string,
        data_type=data_type,
        is_hidden=is_hidden,
        extra=extra,
    )
    return metric, j

"""Power BI report definition parser.

Answers one question: *which metric names does this report actually reference?*
Everything else here exists to make that answer trustworthy, because the
consumer is `detect/unused.py` and a **missed reference becomes a false
"unused measure" finding** -- the single worst failure mode this tool has.
So the bias throughout is deliberate: over-collect names, never under-collect.

Two on-disk formats are handled (see `_detect_format`):

1. **Legacy / PBIR-Legacy** -- one monolithic ``report.json``::

       {"sections": [{"name": ..., "displayName": "Overview",
                      "visualContainers": [{"config": "<JSON *string*>",
                                            "filters": "<JSON *string*>",
                                            "query": "<JSON *string*>",
                                            "dataTransforms": "<JSON *string*>"}]}]}

   The inner ``config`` string re-parses to
   ``{"singleVisual": {"visualType": ..., "projections": {"Values": [{"queryRef": ...}]}}}``.

2. **PBIR (Power BI Enhanced Report Format)** -- one file per object::

       definition/version.json
       definition/report.json                       report-level filters
       definition/reportExtensions.json             report-level measures (DAX)
       definition/pages/pages.json                  page order
       definition/pages/<page>/page.json            page-level filters
       definition/pages/<page>/visuals/<v>/visual.json
       definition/pages/<page>/visuals/<v>/mobile.json
       definition/bookmarks/<b>.bookmark.json

   A PBIR visual carries::

       {"name": ..., "position": {...},
        "visual": {"visualType": ...,
                   "query": {"queryState": {"<role>": {"projections":
                        [{"field": <QueryExpressionContainer>,
                          "queryRef": "Sales.Total Sales",
                          "nativeQueryRef": "Total Sales"}]}}},
                   "objects": {...}},
        "filterConfig": {"filters": [{"field": <QueryExpressionContainer>, ...}]}}

A ``QueryExpressionContainer`` is the shared "semantic query" node used by both
formats and in every position a field can appear (projections, filters, sort
definitions, conditional-formatting fill rules, sparklines, ...). Its variants
include ``SourceRef``/``Column``/``Measure``/``Aggregation``/``HierarchyLevel``/
``NativeMeasure``/``NativeVisualCalculation``. A measure reads::

    {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                 "Property": "Total Sales"}}

Rather than enumerate every legal position, the collector walks the *whole*
JSON tree of each file (transparently re-parsing embedded JSON strings) and
picks up references wherever they occur. Structured knowledge of the schema is
used only to label page / visual / role, never to gate what counts as a
reference.

Standard library only. Never raises: malformed input lands in ``Estate.errors``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from typing import Any

from dustpan import safeio

from ..ir import Asset, Estate, Visual, project_root

__all__ = ["PSEUDO_VISUAL_TYPES", "parse_query_ref", "parse_report"]

# --------------------------------------------------------------------------
# tuning
# --------------------------------------------------------------------------

#: Recursion cap. Real report JSON nests ~15 deep; embedded JSON strings can
#: push that further. The cap only exists so pathological input cannot blow the
#: Python stack -- it must never fire on a real report.
_MAX_DEPTH = 80

#: Cap on how many characters of a string we will try to re-parse as JSON.
_MAX_EMBEDDED_JSON = 8 * 1024 * 1024

#: Keys whose *string* value is a raw DAX expression (visual calculations,
#: report-level measures, native measures).
_DAX_STRING_KEYS = frozenset(
    {"expression", "daxexpression", "measureexpression", "nativeexpression"}
)

#: Keys whose string value is a query reference, e.g. "Sales.Total Sales".
_QUERY_REF_KEYS = frozenset({"queryref", "nativequeryref", "queryname"})

#: QueryExpressionContainer variants that name a field directly.
#: value = (kind, key holding the field name)
_FIELD_VARIANTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Measure": ("measure", ("Property", "property", "name")),
    "Column": ("column", ("Property", "property", "name")),
    "HierarchyLevel": ("hierarchy_level", ("Level", "level", "name")),
    "Hierarchy": ("hierarchy", ("Hierarchy", "hierarchy", "name")),
}

#: Variants whose ``Expression`` is a raw DAX *string* rather than a container.
_NATIVE_DAX_VARIANTS = frozenset(
    {"NativeMeasure", "NativeColumn", "NativeVisualCalculation"}
)

#: Keys naming a real table on a SourceRef / short-form field.
_ENTITY_KEYS = ("Entity", "entity", "Table", "table")
#: Keys naming a *query alias* ("s"), resolvable only via the query's `From`.
_ALIAS_KEYS = ("Source", "source")

#: `visual_type` values for synthetic visuals that hold references belonging to
#: no real visual. Consumers that count or list visuals should skip these.
PSEUDO_VISUAL_TYPES = frozenset(
    {"pageFilters", "reportFilters", "reportExtensions", "bookmarks"}
)

# ``Sum(Sales.Amount)`` / ``CountNonNull(Sales.Id)`` -> ("Sum", "Sales.Amount")
_AGG_WRAPPER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$", re.DOTALL)

# DAX masking / reference extraction
_DAX_COMMENT_RE = re.compile(r"//[^\n\r]*|/\*.*?\*/", re.DOTALL)
_DAX_STRING_RE = re.compile(r'"(?:[^"\\]|\\.|"")*"', re.DOTALL)
# AUD-V3-015: in DAX a `]` inside a bracketed identifier is escaped by
# doubling it, so `[A]]B]` names the measure `A]B`. The old patterns stopped
# at the first `]` and extracted `A`, quietly pointing usage evidence at a
# measure that does not exist. `(?:[^\]]|\]\])+` consumes doubled brackets;
# `_unescape_brackets` turns them back into single characters.
_BRACKET_BODY = r"(?:[^\]]|\]\])+"
_DAX_QUOTED_TABLE_RE = re.compile(r"'((?:[^']|'')+)'\s*\[(" + _BRACKET_BODY + r")\]")
_DAX_BARE_TABLE_RE = re.compile(
    r"(?<![\w'\]])([A-Za-z_][A-Za-z0-9_]*)\s*\[(" + _BRACKET_BODY + r")\]"
)
_DAX_BARE_BRACKET_RE = re.compile(r"\[(" + _BRACKET_BODY + r")\]")


def _unescape_brackets(name: str) -> str:
    """`A]]B` -> `A]B`. The doubled `]` is DAX's escape, not two characters."""
    return name.replace("]]", "]")


# Cheap "does this look like DAX with a reference in it" gate.
_LOOKS_LIKE_DAX_RE = re.compile(r"\[[^\[\]]+\]")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def parse_query_ref(query_ref: str) -> tuple[str | None, str, str | None]:
    """Split a Power BI ``queryRef`` into ``(table, field, aggregation)``.

    ``"Sales.Total Sales"``      -> ``("Sales", "Total Sales", None)``
    ``"Sum(Sales.Amount)"``      -> ``("Sales", "Amount", "Sum")``
    ``"Date.Calendar.Year"``     -> ``("Date", "Calendar.Year", None)``
    ``"Total Sales"``            -> ``(None, "Total Sales", None)``

    The table is whatever precedes the *first* dot, because measure names may
    themselves contain dots but table names in a queryRef never do.
    """
    agg: str | None = None
    text = (query_ref or "").strip()
    # Unwrap at most a couple of nested aggregation wrappers.
    for _ in range(3):
        m = _AGG_WRAPPER_RE.match(text)
        if not m:
            break
        agg = agg or m.group(1)
        text = m.group(2).strip()
    if not text:
        return None, "", agg
    if "." in text:
        table, field = text.split(".", 1)
        table = table.strip()
        field = field.strip()
        if table and field:
            return table, field, agg
    return None, text, agg


def _read_json(path: str, estate: Estate, *, required: bool = True) -> Any | None:
    """Load a JSON file. Any failure is recorded, never raised."""
    try:
        text = safeio.read_text(path, estate, "report", required=required)
        if text is None:
            return None
        return json.loads(text)
    except FileNotFoundError:
        if required:
            estate.errors.append(f"report parser: missing file: {path}")
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        estate.errors.append(f"report parser: unreadable JSON {path}: {exc}")
        return None


def _maybe_embedded_json(value: str) -> Any | None:
    """Re-parse a string that is itself a JSON document, else ``None``.

    The legacy format stores ``config`` / ``filters`` / ``query`` /
    ``dataTransforms`` as JSON *strings*; handling this generically means we
    also follow any other stringified blob a future format version adds.
    """
    stripped = value.strip()
    if len(stripped) < 2 or len(stripped) > _MAX_EMBEDDED_JSON:
        return None
    if stripped[0] not in "{[" or stripped[-1] not in "}]":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _dax_references(expression: str) -> list[tuple[str | None, str]]:
    """Best-effort ``(table, field)`` references inside a DAX string.

    Not a DAX parser (``dax/`` owns that) -- just enough to stop a measure that
    is only reachable through a report-level measure or a visual calculation
    from looking unused. String literals and comments are masked first so
    ``"[not a ref]"`` cannot produce a phantom reference.
    """
    if not expression or not _LOOKS_LIKE_DAX_RE.search(expression):
        return []

    def _blank(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    masked = _DAX_COMMENT_RE.sub(_blank, expression)
    masked = _DAX_STRING_RE.sub(_blank, masked)

    out: list[tuple[str | None, str]] = []
    for regex in (_DAX_QUOTED_TABLE_RE, _DAX_BARE_TABLE_RE):
        for match in regex.finditer(masked):
            table = match.group(1).replace("''", "'").strip()
            column = _unescape_brackets(match.group(2)).strip()
            if column:
                out.append((table or None, column))
        masked = regex.sub(_blank, masked)

    # Whatever brackets survive are unqualified -- i.e. measure references.
    for match in _DAX_BARE_BRACKET_RE.finditer(masked):
        name = _unescape_brackets(match.group(1)).strip()
        if name:
            out.append((None, name))
    return out


# --------------------------------------------------------------------------
# reference collection
# --------------------------------------------------------------------------


class _Collector:
    """Accumulates references found while walking a JSON tree.

    ``names`` is what lands in :attr:`ir.Visual.metric_names`. It intentionally
    includes column and hierarchy names as well as measures: the detector
    matches these against real model measure names, so a name that is not a
    measure simply never matches, whereas a *missing* name produces a false
    "unused" finding. Typed detail is kept in ``refs`` so a caller that wants
    to be stricter (e.g. match on table+name) still can.
    """

    __slots__ = ("_seen_names", "_seen_refs", "depth_exceeded", "names", "refs")

    def __init__(self) -> None:
        self.names: list[str] = []
        self._seen_names: set[str] = set()
        self.refs: list[dict[str, Any]] = []
        self._seen_refs: set[tuple[Any, ...]] = set()
        #: True when the recursion cap stopped the walk before the bottom of
        #: the document, meaning references below it were never seen.
        self.depth_exceeded = False

    def add(
        self,
        table: str | None,
        name: str,
        kind: str,
        source: str,
        *,
        role: str | None = None,
        query_ref: str | None = None,
        aggregation: str | None = None,
    ) -> None:
        name = (name or "").strip()
        if not name:
            return
        table = (table or "").strip() or None

        key = (table, name, kind, source, role)
        if key not in self._seen_refs:
            self._seen_refs.add(key)
            ref: dict[str, Any] = {
                "table": table,
                "name": name,
                "kind": kind,
                "source": source,
            }
            if role:
                ref["role"] = role
            if query_ref:
                ref["query_ref"] = query_ref
            if aggregation:
                ref["aggregation"] = aggregation
            self.refs.append(ref)

        if name not in self._seen_names:
            self._seen_names.add(name)
            self.names.append(name)

    def add_query_ref(self, query_ref: str, source: str, role: str | None = None) -> None:
        table, field, agg = parse_query_ref(query_ref)
        if not field:
            return
        kind = "aggregated_column" if agg else "query_ref"
        self.add(
            table, field, kind, source, role=role, query_ref=query_ref, aggregation=agg
        )
        # A dotted remainder means a hierarchy path such as "Calendar.Year";
        # also offer the leaf, since either half could be the real object name.
        if "." in field:
            leaf = field.rsplit(".", 1)[-1].strip()
            if leaf:
                self.add(
                    table,
                    leaf,
                    kind,
                    source,
                    role=role,
                    query_ref=query_ref,
                    aggregation=agg,
                )

    def add_dax(self, expression: str, source: str, role: str | None = None) -> None:
        for table, name in _dax_references(expression):
            kind = "dax_column" if table else "dax_measure"
            self.add(table, name, kind, source, role=role)


def _collect_source_aliases(
    node: Any, out: dict[str, str], depth: int = 0
) -> dict[str, str]:
    """Map query aliases to table names from every ``From`` clause in a document.

    A filter body reads ``{"From": [{"Name": "s", "Entity": "Sales"}], "Where":
    [... {"SourceRef": {"Source": "s"}} ...]}``, so without this the table for
    such a reference would be the meaningless alias ``"s"``. Aliases are
    gathered document-wide rather than per-scope: the table is advisory
    metadata only (names drive detection), so breadth beats scope precision.
    """
    if depth > _MAX_DEPTH:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() == "from" and isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    alias = item.get("Name") or item.get("name")
                    entity = item.get("Entity") or item.get("entity")
                    if isinstance(alias, str) and isinstance(entity, str):
                        alias, entity = alias.strip(), entity.strip()
                        if alias and entity:
                            out.setdefault(alias, entity)
            if isinstance(value, (dict, list)):
                _collect_source_aliases(value, out, depth + 1)
            elif isinstance(value, str):
                embedded = _maybe_embedded_json(value)
                if embedded is not None:
                    _collect_source_aliases(embedded, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_source_aliases(item, out, depth + 1)
    elif isinstance(node, str):
        embedded = _maybe_embedded_json(node)
        if embedded is not None:
            _collect_source_aliases(embedded, out, depth + 1)
    return out


def _entity_of(node: Any, aliases: dict[str, str], depth: int = 0) -> str | None:
    """Find the table name inside a nested expression container.

    Returns ``None`` rather than a bare query alias: a caller must never be
    handed ``"s"`` and mistake it for a table.
    """
    if depth > 12 or not isinstance(node, dict):
        return None
    for key in ("SourceRef", "sourceRef"):
        ref = node.get(key)
        if isinstance(ref, dict):
            for ek in _ENTITY_KEYS:
                val = ref.get(ek)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for ak in _ALIAS_KEYS:
                val = ref.get(ak)
                if isinstance(val, str) and val.strip():
                    return aliases.get(val.strip())
    # Short form: {"Measure": {"entity": "Sales", "name": "Total Sales"}}
    for ek in _ENTITY_KEYS:
        val = node.get(ek)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("Expression", "expression"):
        inner = node.get(key)
        if isinstance(inner, dict):
            found = _entity_of(inner, aliases, depth + 1)
            if found:
                return found
    return None


def _scan_document(
    node: Any,
    col: _Collector,
    source: str,
    estate: Estate | None = None,
    where: str = "",
) -> None:
    """Entry point for one JSON document: resolve aliases, then walk it.

    AUD-V1-009: when the recursion cap stops the walk, the references below
    it were never seen. That is missing usage evidence, so it is recorded as
    a material note rather than left as a silent omission -- a reference that
    disappears is exactly how a measure that IS used gets called unused.
    """
    aliases = _collect_source_aliases(node, {})
    _scan(node, col, source, aliases)
    if col.depth_exceeded and estate is not None:
        message = (
            f"report: {where or source}: nesting deeper than {_MAX_DEPTH} levels was "
            "not walked, so any measure references below that depth were not "
            "counted and usage evidence is incomplete"
        )
        if message not in estate.errors:
            estate.errors.append(message)


def _scan(
    node: Any,
    col: _Collector,
    source: str,
    aliases: dict[str, str],
    role: str | None = None,
    depth: int = 0,
) -> None:
    """Recursively collect every reference reachable from ``node``.

    Deliberately position-agnostic: projections, filters, sort definitions,
    conditional-formatting fill rules, tooltips, drill fields, slicer targets
    and bookmark state all reduce to the same containers, so one walk covers
    them all -- including shapes Microsoft has not shipped yet.
    """
    if depth > _MAX_DEPTH:
        # AUD-V1-009: a reference below the cap simply disappeared, with no
        # signal. Silently dropping a reference is how a measure that IS used
        # gets reported unused, so the cap has to be visible.
        col.depth_exceeded = True
        return

    if isinstance(node, dict):
        # 1. Field-naming expression containers.
        for variant, (kind, name_keys) in _FIELD_VARIANTS.items():
            payload = node.get(variant)
            if not isinstance(payload, dict):
                continue
            name = None
            for nk in name_keys:
                candidate = payload.get(nk)
                if isinstance(candidate, str) and candidate.strip():
                    name = candidate
                    break
            if name:
                col.add(_entity_of(payload, aliases), name, kind, source, role=role)

        # 2. Variants carrying inline DAX (visual calculations, native measures).
        for variant in _NATIVE_DAX_VARIANTS:
            payload = node.get(variant)
            if isinstance(payload, dict):
                expr = payload.get("Expression") or payload.get("expression")
                if isinstance(expr, str):
                    col.add_dax(expr, source, role)

        # 3. queryRef and its sibling nativeQueryRef, handled as a pair.
        handled: set[str] = set()
        query_ref = node.get("queryRef") or node.get("queryName")
        native_ref = node.get("nativeQueryRef")
        if isinstance(query_ref, str) and query_ref.strip():
            handled.update({"queryRef", "queryName", "nativeQueryRef"})
            _, _, agg = parse_query_ref(query_ref)
            col.add_query_ref(query_ref, source, role)
            # For an aggregated column, nativeQueryRef is a synthesised display
            # name ("Sum of Amount") and naming no real object -- skip it.
            if not agg and isinstance(native_ref, str):
                col.add_query_ref(native_ref, source, role)

        # 4. Scalars and recursion, using keys we recognise to add labels.
        for key, value in node.items():
            lowered = key.lower() if isinstance(key, str) else ""
            if isinstance(value, str):
                if key in handled:
                    continue
                if lowered in _QUERY_REF_KEYS:
                    col.add_query_ref(value, source, role)
                elif lowered in _DAX_STRING_KEYS:
                    col.add_dax(value, source, role)
                else:
                    embedded = _maybe_embedded_json(value)
                    if embedded is not None:
                        _scan(embedded, col, source, aliases, role, depth + 1)
            elif isinstance(value, (dict, list)):
                # queryState / projections are keyed by visual role name.
                if lowered in ("querystate", "projections") and isinstance(value, dict):
                    for role_name, sub in value.items():
                        _scan(sub, col, source, aliases, str(role_name), depth + 1)
                    continue
                _scan(value, col, source, aliases, role, depth + 1)
        return

    if isinstance(node, list):
        for item in node:
            _scan(item, col, source, aliases, role, depth + 1)
        return

    if isinstance(node, str):
        embedded = _maybe_embedded_json(node)
        if embedded is not None:
            _scan(embedded, col, source, aliases, role, depth + 1)


# --------------------------------------------------------------------------
# format detection
# --------------------------------------------------------------------------


def _detect_format(report_dir: str) -> tuple[str, str]:
    """Return ``(format, root_dir)`` where format is ``pbir`` / ``legacy`` / ``unknown``."""
    root = report_dir
    if os.path.isfile(root):
        # Tolerate being handed report.json or definition.pbir directly.
        root = os.path.dirname(os.path.abspath(root)) or "."
    if os.path.isdir(os.path.join(root, "definition", "pages")):
        return "pbir", root
    # Tolerate being handed the `definition` folder itself.
    if os.path.basename(os.path.normpath(root)) == "definition" and os.path.isdir(
        os.path.join(root, "pages")
    ):
        return "pbir", os.path.dirname(os.path.normpath(root)) or "."
    if os.path.isfile(os.path.join(root, "report.json")):
        return "legacy", root
    if os.path.isdir(os.path.join(root, "definition")):
        # PBIR-shaped but page-less (or a partial export).
        return "pbir", root
    return "unknown", root


def _report_name(report_dir: str) -> str:
    base = os.path.basename(os.path.normpath(os.path.abspath(report_dir)))
    if base.lower().endswith(".report"):
        base = base[: -len(".report")]
    return base or "report"


def _model_hint(report_dir: str, estate: Estate) -> tuple[str | None, str | None]:
    """Read ``definition.pbir`` for the semantic model this report binds to."""
    pbir_path = os.path.join(report_dir, "definition.pbir")
    if not os.path.isfile(pbir_path):
        return None, None
    data = _read_json(pbir_path, estate, required=False)
    if not isinstance(data, dict):
        return None, None
    ref = data.get("datasetReference")
    if not isinstance(ref, dict):
        return None, None
    by_path = ref.get("byPath")
    if isinstance(by_path, dict):
        raw = by_path.get("path")
        if isinstance(raw, str) and raw.strip():
            base = os.path.basename(os.path.normpath(raw.strip().replace("\\", "/")))
            if base.lower().endswith(".semanticmodel"):
                base = base[: -len(".semanticmodel")]
            elif base.lower().endswith(".dataset"):
                base = base[: -len(".dataset")]
            return (base or None), raw.strip()
    by_conn = ref.get("byConnection")
    if isinstance(by_conn, dict):
        for key in ("pbiModelDatabaseName", "connectionString"):
            val = by_conn.get(key)
            if isinstance(val, str) and val.strip():
                return None, val.strip()
    return None, None


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}#{n}"
        n += 1
    used.add(candidate)
    return candidate


# --------------------------------------------------------------------------
# legacy report.json
# --------------------------------------------------------------------------


def _parse_legacy(root: str, asset: Asset, estate: Estate) -> None:
    path = os.path.join(root, "report.json")
    data = _read_json(path, estate)
    if data is None:
        return
    if not isinstance(data, dict):
        estate.errors.append(f"report parser: {path}: top level is not a JSON object")
        return

    asset.extra["definition_files"] = [os.path.relpath(path, root)]
    used_ids: set[str] = set()

    sections = data.get("sections")
    if not isinstance(sections, list):
        estate.errors.append(f"report parser: {path}: no 'sections' array")
        sections = []

    for s_index, section in enumerate(sections):
        if not isinstance(section, dict):
            estate.errors.append(
                f"report parser: {path}: section {s_index} is not an object"
            )
            continue
        page = _first_str(section, "displayName", "name") or f"page{s_index}"

        containers = section.get("visualContainers")
        if containers is None:
            containers = []
        if not isinstance(containers, list):
            estate.errors.append(
                f"report parser: {path}: section '{page}' has non-list visualContainers"
            )
            containers = []

        for v_index, container in enumerate(containers):
            if not isinstance(container, dict):
                estate.errors.append(
                    f"report parser: {path}: section '{page}' visual {v_index} is not an object"
                )
                continue
            config = _decode_embedded(
                container.get("config"), f"{path} [{page}/{v_index} config]", estate
            )
            single = config.get("singleVisual") if isinstance(config, dict) else None
            group = config.get("singleVisualGroup") if isinstance(config, dict) else None

            visual_type = "unknown"
            if isinstance(single, dict):
                visual_type = _first_str(single, "visualType") or "unknown"
            elif isinstance(group, dict):
                visual_type = "visualGroup"

            name = None
            if isinstance(config, dict):
                name = _first_str(config, "name")
            vid = _unique_id(name or f"{page}:{v_index}", used_ids)

            check_embedded_fields(container, f"{path} [{page}/{v_index}]", estate)
            col = _Collector()
            # Walk the entire container: config, filters, query and
            # dataTransforms are all JSON strings and all can hold references.
            _scan_document(container, col, "visual", estate, root)
            _append_visual(asset, vid, page, visual_type, col)

        # Page-level filters live on the section, outside any visual.
        check_embedded_fields(section, f"{path} [{page} page]", estate)
        page_col = _Collector()
        for key in ("filters", "config"):
            if key in section:
                _scan_document(section.get(key), page_col, "page_filter", estate, root)
        _append_pseudo_visual(asset, f"{page}::__page__", page, "pageFilters", page_col)

    # Report-level filters / config / modelExtensions (report-level measures).
    check_embedded_fields(data, f"{path} [report]", estate)
    report_col = _Collector()
    for key in ("filters", "config", "modelExtensions", "resourcePackages", "pods"):
        if key in data:
            _scan_document(data.get(key), report_col, "report_filter", estate, root)
    _append_pseudo_visual(asset, "__report__", "(report)", "reportFilters", report_col)


#: Legacy fields Power BI stores as JSON *strings*. A value here that looks
#: like a document but does not parse is missing usage evidence, not an
#: ordinary label (AUD-V3-008).
KNOWN_EMBEDDED_FIELDS = ("config", "query", "filters", "dataTransforms")


def check_embedded_fields(node: Any, label: str, estate: Estate) -> None:
    """Report known embedded JSON-string fields that are present but broken.

    AUD-V3-008: a malformed `query` string was silently dropped, so the
    references it carried disappeared and the scan still looked clean -- and a
    measure those references protected was then offered for retirement. Only
    the fields Power BI actually stores as documents are checked, so an
    ordinary label that happens to start with a brace is not misreported.
    """
    if not isinstance(node, dict):
        return
    for key in KNOWN_EMBEDDED_FIELDS:
        if key not in node:
            continue
        value = node[key]
        if isinstance(value, (dict, list)):
            continue
        if not isinstance(value, str):
            _record(estate, f"report parser: {label}: embedded '{key}' has invalid type")
            continue
        stripped = value.strip()
        if not stripped:
            continue  # empty optional payload carries no document
        if len(stripped) > _MAX_EMBEDDED_JSON:
            _record(
                estate,
                f"report parser: {label}: embedded '{key}' is "
                f"{len(stripped):,} bytes, over the "
                f"{_MAX_EMBEDDED_JSON:,}-byte embedded-document budget -- the "
                "references it carries were not read, so usage evidence is "
                "incomplete",
            )
            continue
        try:
            decoded = json.loads(stripped)
            if not isinstance(decoded, (dict, list)):
                raise ValueError("expected a JSON object or array")
        except ValueError as exc:
            _record(
                estate,
                f"report parser: {label}: embedded '{key}' is a malformed JSON "
                f"document ({exc}) -- any measure references inside it were "
                "not read, so usage evidence is incomplete",
            )


def _record(estate: Estate, message: str) -> None:
    if message not in estate.errors:
        estate.errors.append(message)


def _decode_embedded(value: Any, label: str, estate: Estate) -> Any:
    """Decode a legacy JSON-string field, recording a parse failure."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        parsed = _maybe_embedded_json(stripped)
        if parsed is None:
            estate.errors.append(
                f"report parser: {label}: embedded JSON string did not parse"
            )
            return {}
        return parsed
    if isinstance(value, (dict, list)):
        return value
    return {}


def _first_str(node: Any, *keys: str) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in keys:
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


# --------------------------------------------------------------------------
# PBIR
# --------------------------------------------------------------------------


def _parse_pbir(root: str, asset: Asset, estate: Estate) -> None:
    definition = os.path.join(root, "definition")
    files: list[str] = []
    used_ids: set[str] = set()

    version = _read_json(os.path.join(definition, "version.json"), estate, required=False)
    if isinstance(version, dict):
        ver = version.get("version")
        if isinstance(ver, str):
            asset.extra["pbir_version"] = ver

    pages_dir = os.path.join(definition, "pages")
    page_names = _ordered_page_names(pages_dir, definition, estate, files)

    for page_dir_name in page_names:
        page_dir = os.path.join(pages_dir, page_dir_name)
        page_json_path = os.path.join(page_dir, "page.json")
        page_data = _read_json(page_json_path, estate, required=False)
        if page_data is None and not os.path.isdir(page_dir):
            estate.errors.append(f"report parser: missing page folder: {page_dir}")
            continue
        files.append(os.path.relpath(page_json_path, root))
        page = _first_str(page_data, "displayName", "name") or page_dir_name

        visuals_dir = os.path.join(page_dir, "visuals")
        for visual_name in _sorted_dirs(visuals_dir, estate):
            v_dir = os.path.join(visuals_dir, visual_name)
            v_path = os.path.join(v_dir, "visual.json")
            v_data = _read_json(v_path, estate, required=True)
            if v_data is None:
                continue
            files.append(os.path.relpath(v_path, root))
            if not isinstance(v_data, dict):
                estate.errors.append(
                    f"report parser: {v_path}: top level is not a JSON object"
                )
                continue

            visual_cfg = v_data.get("visual")
            visual_type = _first_str(visual_cfg, "visualType", "type") or _first_str(
                v_data, "visualType"
            )
            if not visual_type:
                visual_type = "visualGroup" if "visualGroup" in v_data else "unknown"
            vid = _unique_id(
                _first_str(v_data, "name") or f"{page}:{visual_name}", used_ids
            )

            col = _Collector()
            _scan_document(v_data, col, "visual", estate, root)

            # mobile.json repeats container formatting, which can embed
            # conditional-formatting rules that name measures.
            mobile_path = os.path.join(v_dir, "mobile.json")
            if os.path.isfile(mobile_path):
                mobile = _read_json(mobile_path, estate, required=False)
                if mobile is not None:
                    files.append(os.path.relpath(mobile_path, root))
                    _scan_document(mobile, col, "visual_mobile", estate, root)

            _append_visual(asset, vid, page, visual_type, col)

        # Page-level filters (page.json filterConfig) belong to no visual.
        page_col = _Collector()
        _scan_document(page_data, page_col, "page_filter", estate, root)
        _append_pseudo_visual(
            asset, f"{page_dir_name}::__page__", page, "pageFilters", page_col
        )

    # Report-level filters.
    report_col = _Collector()
    report_json_path = os.path.join(definition, "report.json")
    report_data = _read_json(report_json_path, estate, required=False)
    if report_data is not None:
        files.append(os.path.relpath(report_json_path, root))
        _scan_document(report_data, report_col, "report_filter", estate, root)
    _append_pseudo_visual(asset, "__report__", "(report)", "reportFilters", report_col)

    # Report-level measures: their DAX can be the only path to a model measure.
    ext_col = _Collector()
    ext_path = os.path.join(definition, "reportExtensions.json")
    ext_data = _read_json(ext_path, estate, required=False)
    if ext_data is not None:
        files.append(os.path.relpath(ext_path, root))
        _scan_document(ext_data, ext_col, "report_extension", estate, root)
        asset.extra["report_level_measures"] = _report_level_measure_names(ext_data)
    _append_pseudo_visual(
        asset, "__reportExtensions__", "(report)", "reportExtensions", ext_col
    )

    # Bookmarks capture filter/selection state, including measure filters.
    bm_col = _Collector()
    bookmarks_dir = os.path.join(definition, "bookmarks")
    if os.path.isdir(bookmarks_dir):
        for entry in sorted(os.listdir(bookmarks_dir)):
            if not entry.lower().endswith(".json"):
                continue
            bm_path = os.path.join(bookmarks_dir, entry)
            bm_data = _read_json(bm_path, estate, required=False)
            if bm_data is not None:
                files.append(os.path.relpath(bm_path, root))
                _scan_document(bm_data, bm_col, "bookmark", estate, root)
    _append_pseudo_visual(asset, "__bookmarks__", "(report)", "bookmarks", bm_col)

    asset.extra["definition_files"] = files


def _report_level_measure_names(ext_data: Any) -> list[str]:
    """Names of measures *defined by the report* (``reportExtensions.json``).

    These are not model measures, so a detector should not report them as
    unused model measures -- and it must not confuse them with model measures
    of the same name.
    """
    out: list[str] = []
    if not isinstance(ext_data, dict):
        return out
    entities = ext_data.get("entities")
    if not isinstance(entities, list):
        return out
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        measures = entity.get("measures")
        if isinstance(measures, list):
            for measure in measures:
                name = _first_str(measure, "name")
                if name:
                    out.append(name)
        # Flat variant: the entity itself is the measure.
        if not isinstance(measures, list):
            name = _first_str(entity, "name")
            if name and _first_str(entity, "expression"):
                out.append(name)
    return out


def _ordered_page_names(
    pages_dir: str, definition: str, estate: Estate, files: list[str]
) -> list[str]:
    """Page folder names, honouring ``pages.json`` order when present."""
    on_disk = _sorted_dirs(pages_dir, estate)
    meta_path = os.path.join(pages_dir, "pages.json")
    meta = _read_json(meta_path, estate, required=False)
    if isinstance(meta, dict):
        order = meta.get("pageOrder")
        if isinstance(order, list):
            # AUD-V3-016: a page repeated in pageOrder used to be walked twice,
            # so the fixture's six real visuals were counted as eight and the
            # keeper ranking shifted with it. Keep the first occurrence and its
            # position; a repeat is metadata noise, not another page.
            ordered: list[str] = []
            for name in order:
                if isinstance(name, str) and name in on_disk and name not in ordered:
                    ordered.append(name)
            ordered += [n for n in on_disk if n not in ordered]
            return ordered
    if not on_disk and not os.path.isdir(pages_dir):
        estate.errors.append(f"report parser: missing pages folder: {pages_dir}")
    return on_disk


def _sorted_dirs(path: str, estate: Estate | None = None) -> list[str]:
    """Sub-directories of `path`, or [] with a recorded note.

    AUD-V1-012: an unreadable directory used to return an empty list that was
    indistinguishable from a genuinely empty one, so a permission-denied
    pages folder reported zero visuals and the scan still looked clean.
    """
    try:
        return sorted(e for e in os.listdir(path) if os.path.isdir(os.path.join(path, e)))
    except OSError as exc:
        if estate is not None:
            message = (
                f"report: could not list '{path}': {exc} -- any pages or visuals "
                "below it were not scanned, so usage evidence is incomplete"
            )
            if message not in estate.errors:
                estate.errors.append(message)
        return []


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def _append_visual(
    asset: Asset, vid: str, page: str, visual_type: str, col: _Collector
) -> None:
    asset.visuals.append(
        Visual(id=vid, page=page, visual_type=visual_type, metric_names=list(col.names))
    )
    _record_refs(asset, vid, page, col)


def _append_pseudo_visual(
    asset: Asset, vid: str, page: str, visual_type: str, col: _Collector
) -> None:
    """Attach non-visual references (page/report filters, bookmarks, report
    measures) as a synthetic Visual.

    :class:`ir.Visual` is the only channel through which an Asset exposes
    references, so a measure used *only* in a report-level filter would
    otherwise read as unused. These entries are emitted only when they
    actually carry a reference, and their ``visual_type`` marks them as
    non-visual so consumers can exclude them from visual counts.
    """
    if not col.names:
        return
    _append_visual(asset, vid, page, visual_type, col)


def _record_refs(asset: Asset, vid: str, page: str, col: _Collector) -> None:
    if not col.refs:
        return
    bucket = asset.extra.setdefault("references", [])
    for ref in col.refs:
        entry = dict(ref)
        entry["visual_id"] = vid
        entry["page"] = page
        bucket.append(entry)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def parse_report(report_dir: str, estate: Estate) -> None:
    """Parse one Power BI report folder and append an :class:`ir.Asset`.

    Handles both the legacy monolithic ``report.json`` and the newer PBIR
    folder layout. Never raises; problems are appended to ``estate.errors``.
    """
    try:
        fmt, root = _detect_format(report_dir)
        name = _report_name(root)
        asset = Asset(
            id=f"powerbi:{name}",
            name=name,
            tool="powerbi",
            source_path=os.path.abspath(root),
            source_root=project_root(root),
            extra={"format": fmt},
        )

        model, model_ref = _model_hint(root, estate)
        if model:
            asset.model = model
        if model_ref:
            asset.extra["dataset_reference"] = model_ref

        if fmt == "legacy":
            _parse_legacy(root, asset, estate)
        elif fmt == "pbir":
            _parse_pbir(root, asset, estate)
        else:
            estate.errors.append(
                f"report parser: {report_dir}: no report.json and no definition/pages -- "
                "not a recognised Power BI report folder"
            )
            return

        asset.extra["pages"] = _distinct(
            v.page for v in asset.visuals if v.visual_type not in PSEUDO_VISUAL_TYPES
        )
        asset.extra["referenced_tables"] = _distinct(
            r.get("table") for r in asset.extra.get("references", []) if r.get("table")
        )
        estate.assets.append(asset)
    except Exception as exc:  # never raise -- contract
        estate.errors.append(
            f"report parser: {report_dir}: unexpected {type(exc).__name__}: {exc}"
        )


def _distinct(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out

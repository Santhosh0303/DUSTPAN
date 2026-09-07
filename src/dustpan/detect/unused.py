"""Usage detection: which metrics nothing points at.

Owner: Agent D.  Codes only against the frozen IR (``dustpan.ir``).

WHY THIS MODULE IS PARANOID
---------------------------
The output of this module is read as "you can delete this".  A false
positive here deletes a working measure.  So every rule below is biased in
one direction: **when in doubt, call it used**.  Under-reporting an unused
measure costs the user a little clutter.  Over-reporting costs them a
broken report and their trust in the tool.

Concretely that bias shows up as:

* A measure referenced by *another measure* is used, transitively.  We build
  the measure->measure dependency graph from ``Metric.refs`` and also from a
  crude ``[Bracketed]`` scan of the raw expression, so a measure stays safe
  even when the DAX normaliser could not parse the thing that references it.
* Name matching is deliberately loose (``Sales.Total Sales``,
  ``Sales[Total Sales]``, ``[Total Sales]``, ``Total Sales`` all resolve).
  Loose matching can only ever mark *more* things used.
* If we cannot see any evidence of usage at all -- no assets, no visuals, no
  metric references, or references that resolve to nothing we know about --
  we report NOTHING as unused and emit a single ``usage_unknown`` finding
  instead.  "We scanned no reports" must never render as "delete everything".

CONFIDENCE
----------
``unused_measure`` is a heuristic and is capped well below 1.0 (the contract
reserves 1.0 for deterministic identity).  dustpan only sees the files it was
pointed at; a measure can still be consumed by an unscanned report, an Excel
pivot / Analyze-in-Excel session, a paginated report, a calculation group, a
composite model in another workspace, or a format-string expression.

    0.6  normal: at least one asset was scanned and its references resolved.
    0.4  degraded: the estate contains unparsed metrics (so the dependency
         graph is provably incomplete) or the scan recorded errors (so we may
         not have read every report).
    0.0  informational findings (``usage_unknown``).  0.0 does not mean
         "probably false" -- it means "this finding asserts nothing about any
         metric being removable".  ``evidence["informational"]`` marks these.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from dustpan.ir import Estate, Finding, Metric

__all__ = [
    "CONFIDENCE_INFORMATIONAL",
    "CONFIDENCE_UNUSED",
    "CONFIDENCE_UNUSED_DEGRADED",
    "AssetIndex",
    "VisualRef",
    "build_asset_index",
    "build_dependency_graph",
    "find_unused",
    "metric_name_key",
    "reference_keys",
]

CONFIDENCE_UNUSED = 0.6
CONFIDENCE_UNUSED_DEGRADED = 0.4
CONFIDENCE_INFORMATIONAL = 0.0

_SAMPLE = 10  # how many examples to put in evidence lists

# ---------------------------------------------------------------------------
# name normalisation
# ---------------------------------------------------------------------------

_QUOTE_CHARS = "'\"`“”"
_WS_RE = re.compile(r"\s+")
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
_BRACKET_FORM_RE = re.compile(r"^(?P<table>.*?)\[(?P<name>[^\]]*)\]$", re.DOTALL)


def _strip_wrappers(text: str) -> str:
    """Drop surrounding quotes/whitespace, e.g. ``'Total Sales'`` -> ``Total Sales``."""
    out = text.strip()
    while len(out) >= 2 and out[0] in _QUOTE_CHARS and out[-1] in _QUOTE_CHARS:
        out = out[1:-1].strip()
    return out


def metric_name_key(name: str) -> str:
    """Canonical lookup key for a metric name.

    Case-insensitive, whitespace-collapsed, quote-stripped.  This is an
    *identity* key -- it does not do any of the fuzzy token work that the
    name-similarity duplicate tier does.
    """
    return _WS_RE.sub(" ", _strip_wrappers(name)).casefold()


def reference_keys(raw: str) -> list[str]:
    """Candidate metric-name keys for one raw reference string from a visual.

    Report formats differ and the report parser hands us the string verbatim,
    so we generate every plausible reading, most specific first::

        "Sales.Total Sales"    -> ["sales.total sales", "total sales"]
        "'Sales'[Total Sales]" -> ["'sales'[total sales]", "total sales"]
        "[Total Sales]"        -> ["[total sales]", "total sales"]
        "Total Sales"          -> ["total sales"]

    The caller takes the first key that matches a known metric name, so a
    metric whose name genuinely contains a ``.`` or ``[`` still wins over the
    stripped reading.
    """
    out: list[str] = []

    def add(value: str) -> None:
        key = metric_name_key(value)
        if key and key not in out:
            out.append(key)

    text = _strip_wrappers(raw)
    if not text:
        return out
    add(text)

    match = _BRACKET_FORM_RE.match(text)
    if match:
        add(match.group("name"))

    if "." in text:
        # A measure name may itself contain dots, so offer both splits.
        add(text.split(".", 1)[1])
        add(text.rsplit(".", 1)[1])

    return out


def _expression_bracket_names(expression: str | None) -> set[str]:
    """Every ``[Bracketed]`` token in a raw expression, as name keys.

    A safety net for metrics the DAX normaliser could not parse: without it a
    measure referenced only by an unparsable measure would look orphaned and
    be offered up for deletion.  It over-matches (it also picks up the column
    part of ``Sales[Amount]``, and text inside string literals or comments),
    which can only ever mark more metrics used.
    """
    if not expression:
        return set()
    return {k for k in (metric_name_key(t) for t in _BRACKET_RE.findall(expression)) if k}


# ---------------------------------------------------------------------------
# asset -> metric index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisualRef:
    """One place a visual points at a metric."""

    asset_id: str
    asset_name: str
    page: str
    visual_id: str
    visual_type: str
    raw_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "page": self.page,
            "visual_id": self.visual_id,
            "visual_type": self.visual_type,
            "raw_name": self.raw_name,
        }


@dataclass
class AssetIndex:
    """Which visuals reference which metrics, plus the counts needed to say
    honestly how much evidence we actually had."""

    by_metric_id: dict[str, list[VisualRef]] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    asset_count: int = 0
    visual_count: int = 0
    reference_count: int = 0

    def visual_uses(self, metric_id: str) -> int:
        """Distinct (asset, visual) pairs that reference this metric."""
        return len(
            {(r.asset_id, r.visual_id) for r in self.by_metric_id.get(metric_id, ())}
        )

    def asset_uses(self, metric_id: str) -> list[str]:
        return sorted({r.asset_id for r in self.by_metric_id.get(metric_id, ())})


def _name_lookup(metrics: Sequence[Metric]) -> dict[str, list[str]]:
    lookup: dict[str, list[str]] = {}
    for metric in metrics:
        key = metric_name_key(metric.name)
        if not key:
            continue
        bucket = lookup.setdefault(key, [])
        if metric.id not in bucket:
            bucket.append(metric.id)
    return lookup


def _resolve(
    candidates: Iterable[str],
    lookup: dict[str, list[str]],
    models: dict[str, str],
    prefer_model: str | None,
    roots: dict[str, str] | None = None,
    prefer_root: str | None = None,
) -> list[str]:
    """First candidate key that matches anything wins; ties are narrowed to the
    preferred model when that narrowing leaves at least one metric.

    Model preference is a precision win (a report bound to model X does not
    use model Y's measure of the same name) that degrades safely: if the model
    names the two parsers produced do not line up, nothing is narrowed and we
    keep every candidate."""
    for key in candidates:
        ids = lookup.get(key)
        if not ids:
            continue
        # AUD-008: narrow to the report's own project first. Model-name
        # preference cannot separate two tenants that both call their model
        # `Sales`, so without this a report in tenant A counts as usage for
        # tenant B's identically-named measure -- and usage picks which
        # duplicate survives. Degrades the same way as model preference: if
        # the roots do not line up, nothing is narrowed.
        if roots and prefer_root:
            same_root = [i for i in ids if roots.get(i) == prefer_root]
            if same_root:
                ids = same_root
        if prefer_model:
            wanted = prefer_model.casefold()
            same_model = [i for i in ids if models.get(i, "").casefold() == wanted]
            if same_model:
                return same_model
        return list(ids)
    return []


def build_asset_index(estate: Estate) -> AssetIndex:
    """Map every metric id to the visuals that reference it."""
    index = AssetIndex()
    lookup = _name_lookup(estate.metrics)
    models = {m.id: (m.model or "") for m in estate.metrics}
    roots = {m.id: (m.source_root or "") for m in estate.metrics}
    seen: dict[str, set[tuple[str, str, str]]] = {}
    unresolved: list[str] = []

    index.asset_count = len(estate.assets)
    for asset in estate.assets:
        index.visual_count += len(asset.visuals)
        for visual in asset.visuals:
            for raw in visual.metric_names:
                if raw is None:
                    continue
                index.reference_count += 1
                ids = _resolve(
                    reference_keys(raw),
                    lookup,
                    models,
                    asset.model,
                    roots,
                    asset.source_root,
                )
                if not ids:
                    if raw not in unresolved:
                        unresolved.append(raw)
                    continue
                for metric_id in ids:
                    dedupe = seen.setdefault(metric_id, set())
                    token = (asset.id, visual.id, raw)
                    if token in dedupe:
                        continue
                    dedupe.add(token)
                    index.by_metric_id.setdefault(metric_id, []).append(
                        VisualRef(
                            # AUD-008: `asset.id` is `powerbi:{basename}`, so
                            # two unrelated reports both called `Sales` share
                            # it. Usage observations are deduplicated on this,
                            # and usage decides which duplicate is kept -- so a
                            # collision does not merely miscount, it can pick
                            # the wrong survivor. Key on the deployment.
                            asset_id=f"{asset.id}@{asset.source_root or ''}",
                            asset_name=asset.name,
                            page=visual.page,
                            visual_id=visual.id,
                            visual_type=visual.visual_type,
                            raw_name=raw,
                        )
                    )

    index.unresolved = sorted(unresolved)
    for refs in index.by_metric_id.values():
        refs.sort(key=lambda r: (r.asset_id, r.page, r.visual_id, r.raw_name))
    return index


# ---------------------------------------------------------------------------
# measure -> measure dependency graph
# ---------------------------------------------------------------------------


def build_dependency_graph(estate: Estate) -> dict[str, set[str]]:
    """``{metric id: set of metric ids it depends on}``.

    Two edge sources, unioned:

    1. ``Metric.refs``.  A bare ``[Measure]`` reference arrives as
       ``Ref(table=None, column="Measure")``, but authors do also write
       ``Table[Measure]``, so we match on the column name regardless of table.
       That can mistake a *column* for a same-named measure; the only effect is
       marking that measure used, which is the safe direction.
    2. A ``[Bracketed]`` scan of the raw expression, which keeps the graph
       usable when the normaliser failed and ``refs`` is empty.

    Within a model we prefer same-model targets (a DAX measure cannot
    reference another model), falling back to every same-named metric.
    """
    lookup = _name_lookup(estate.metrics)
    models = {m.id: (m.model or "") for m in estate.metrics}
    graph: dict[str, set[str]] = {m.id: set() for m in estate.metrics}

    for metric in estate.metrics:
        keys: list[str] = []
        for ref in metric.refs:
            key = metric_name_key(ref.column or "")
            if key and key not in keys:
                keys.append(key)
        for key in sorted(_expression_bracket_names(metric.expression)):
            if key not in keys:
                keys.append(key)

        for key in keys:
            for target in _resolve([key], lookup, models, metric.model):
                if target != metric.id:
                    graph[metric.id].add(target)

    return graph


def _reachable(roots: Iterable[str], graph: dict[str, set[str]]) -> set[str]:
    """Every metric reachable from ``roots`` along depends-on edges.

    Iterative, with a visited set, so a malformed circular definition cannot
    hang the scan."""
    seen: set[str] = set()
    stack = [r for r in roots if r in graph]
    seen.update(stack)
    while stack:
        current = stack.pop()
        for nxt in graph.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _metric_sort_key(metric: Metric) -> tuple[str, str]:
    return (metric_name_key(metric.name), metric.id)


def _usage_unknown(estate: Estate, index: AssetIndex) -> Finding:
    """We have no evidence of usage.  Say so; never call it 'unused'."""
    if index.asset_count == 0:
        reason = "no_assets"
        detail = "no reports or dashboards were scanned"
    elif index.visual_count == 0:
        reason = "no_visuals"
        detail = f"{index.asset_count} asset(s) were scanned but none exposed any visuals"
    elif index.reference_count == 0:
        reason = "no_metric_references"
        detail = f"{index.visual_count} visual(s) were scanned but none referenced a metric by name"
    else:
        reason = "no_references_resolved"
        detail = (
            f"{index.reference_count} visual reference(s) were found but none matched a known "
            "metric name -- the report and model parsers probably disagree about naming"
        )

    return Finding(
        kind="usage_unknown",
        severity="medium",
        confidence=CONFIDENCE_INFORMATIONAL,
        metric_ids=[],
        summary=(
            f"Usage could not be determined for {len(estate.metrics)} metric(s): {detail}. "
            "No metric is being reported as unused."
        ),
        evidence={
            "informational": True,
            "assertion": "usage evidence was unavailable; no metric is claimed to be removable",
            "reason": reason,
            "assets_scanned": index.asset_count,
            "visuals_scanned": index.visual_count,
            "metric_references_seen": index.reference_count,
            "metrics_in_estate": len(estate.metrics),
            "unresolved_reference_sample": index.unresolved[:_SAMPLE],
            "known_metric_name_sample": sorted(m.name for m in estate.metrics)[:_SAMPLE],
            "what_to_do": (
                "point dustpan at the reports as well as the semantic model, then re-run"
            ),
        },
        asset_ids=sorted({a.id for a in estate.assets}),
    )


def _missing_measure_refs(estate: Estate, index: AssetIndex) -> list[str]:
    """Unresolved references that might name a MEASURE we never saw.

    `index.unresolved` mixes two very different things. Most entries are
    ordinary column references (`Amount`, `Region`, `Calendar.Year`) -- a
    report naturally names columns, and no measure was ever meant to match
    them. The rest are references to measures in a model that was not
    scanned. Only the second kind is evidence of missing usage, so treating
    the whole list as a coverage gap would permanently degrade every result
    and make the signal meaningless.

    Columns are recognised from the DAX we already parsed: every
    `Table[Column]` reference across all metrics.
    """
    known_columns = {
        (ref.column or "").casefold() for m in estate.metrics for ref in m.refs
    }
    out: list[str] = []
    for raw in index.unresolved:
        text = " ".join(str(raw).split())
        leaf = text.rsplit(".", 1)[-1].strip("[]")
        if leaf.casefold() in known_columns:
            continue
        out.append(text)
    return out


def _shielded_by_unresolved(estate: Estate, index: AssetIndex) -> set[str]:
    """Metric ids an unresolved reference could plausibly be naming.

    Matching is deliberately loose and one-directional-safe: any containment
    either way between the normalised metric name and the normalised raw
    reference counts. Loose matching can only ever mark MORE measures used,
    never fewer, so its failure mode is a missed finding rather than a
    deletion recommendation for a measure something actually depends on.
    """
    missing = _missing_measure_refs(estate, index)
    if not missing:
        return set()
    raws = [r.casefold() for r in missing]
    shielded: set[str] = set()
    for metric in estate.metrics:
        name = " ".join((metric.name or "").split()).casefold()
        if not name:
            continue
        if any(name in raw or raw in name for raw in raws):
            shielded.add(metric.id)
    return shielded


def find_unused(estate: Estate) -> list[Finding]:
    """Metrics that no scanned visual references, directly or transitively.

    Returns one ``unused_measure`` finding per metric, sorted by name, or a
    single informational ``usage_unknown`` finding when there was no usage
    evidence to reason from.
    """
    if not estate.metrics:
        return []

    index = build_asset_index(estate)
    if not index.by_metric_id:
        # No assets / no visuals / no references / nothing resolved.  Any of
        # these means we have zero evidence, and zero evidence is not proof of
        # disuse.  Reporting the whole model as dead here would be the single
        # most destructive thing dustpan could do.
        return [_usage_unknown(estate, index)]

    graph = build_dependency_graph(estate)
    seeds = set(index.by_metric_id.keys())

    # AUD-004: the all-or-nothing fallback above only fires when NOTHING
    # resolved. One resolved reference used to unlock unused claims for every
    # other measure, even while other references in the same report stayed
    # unresolved -- and an unresolved reference is exactly the evidence that
    # would have marked a measure used. Withhold the claim for any metric an
    # unresolved reference could plausibly be naming, and treat the rest as a
    # degraded, lower-confidence result rather than a clean one.
    shielded = _shielded_by_unresolved(estate, index)
    seeds |= shielded
    unresolved_count = len(_missing_measure_refs(estate, index))
    used = _reachable(seeds, graph)

    unparsed = sorted(
        (m for m in estate.metrics if not m.lexically_ok), key=_metric_sort_key
    )
    degraded_reasons: list[str] = []
    if unparsed:
        degraded_reasons.append(
            f"{len(unparsed)} metric(s) could not be parsed, so the measure-to-measure "
            "dependency graph is incomplete: "
            + ", ".join(repr(m.name) for m in unparsed[:_SAMPLE])
        )
    if estate.errors:
        degraded_reasons.append(
            f"the scan recorded {len(estate.errors)} error(s), so some reports may not have "
            "been read: " + "; ".join(estate.errors[:3])
        )

    confidence = CONFIDENCE_UNUSED_DEGRADED if degraded_reasons else CONFIDENCE_UNUSED

    by_id = {m.id: m for m in estate.metrics}
    unused_metrics = sorted(
        (m for m in estate.metrics if m.id not in used), key=_metric_sort_key
    )
    unused_ids = {m.id for m in unused_metrics}

    reverse: dict[str, list[str]] = {}
    for source, targets in graph.items():
        if source not in unused_ids:
            continue
        for target in targets:
            reverse.setdefault(target, []).append(source)

    asset_names = sorted({a.name for a in estate.assets})
    findings: list[Finding] = []
    for metric in unused_metrics:
        caveats = [
            "dustpan only sees the files it was pointed at: this measure may still be used by "
            "a report outside the scanned path, an Excel/Analyze-in-Excel connection, a "
            "paginated report, a calculation group, or a dynamic format string.",
            *degraded_reasons,
        ]
        if unresolved_count:
            caveats.append(
                f"{unresolved_count} reference(s) in the scanned reports could not "
                "be resolved to a measure, so usage evidence is incomplete: this "
                "is a weaker claim than it would be on a fully resolved estate"
            )
        upstream = sorted(by_id[i].name for i in reverse.get(metric.id, ()) if i in by_id)
        if upstream:
            caveats.append(
                "only referenced by measures that are themselves unused: "
                + ", ".join(repr(n) for n in upstream)
            )

        findings.append(
            Finding(
                kind="unused_measure",
                severity="medium",
                # AUD-004: unresolved references are missing usage evidence,
                # and missing evidence cannot leave a claim as strong as it
                # was. Shielding removes the metrics an unresolved reference
                # might name; this degrades what is left.
                confidence=(
                    round(confidence * 0.75, 3) if unresolved_count else confidence
                ),
                metric_ids=[metric.id],
                summary=(
                    f"{metric.name!r} is not referenced by any of the {index.visual_count} "
                    f"visual(s) in the {index.asset_count} scanned asset(s), and no other "
                    "measure references it"
                ),
                evidence={
                    "metric": {
                        "id": metric.id,
                        "name": metric.name,
                        "model": metric.model,
                        "source_path": metric.source_path,
                        "expression": metric.expression,
                        "display_folder": metric.display_folder,
                        "description": metric.description,
                        "is_hidden": metric.is_hidden,
                        "fingerprint": metric.fingerprint,
                        "lexically_ok": metric.lexically_ok,
                    },
                    "searched_for_names": [metric_name_key(metric.name)],
                    "assets_scanned": index.asset_count,
                    "asset_names": asset_names,
                    "visuals_scanned": index.visual_count,
                    "visual_references_scanned": index.reference_count,
                    "visual_references_to_this_metric": 0,
                    "referenced_by_measures": [],
                    "referenced_by_unused_measures": upstream,
                    "depends_on_measures": sorted(
                        by_id[t].name for t in graph.get(metric.id, ()) if t in by_id
                    ),
                    "method": (
                        "reachability from every visual-referenced measure across the "
                        "measure-to-measure dependency graph"
                    ),
                    "caveats": caveats,
                },
                asset_ids=[],
            )
        )

    return findings

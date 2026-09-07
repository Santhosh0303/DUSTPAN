"""Duplicate and near-duplicate measure detection.

THE GOVERNING RULE (see CONTRACT.md): precision beats recall. A false
positive here makes someone delete or "consolidate" a measure that was not
actually redundant, and that is the one failure mode that kills this
product. So this module reports in three explicitly-labelled tiers, and
never blurs the language between them:

  1. EXACT  (confidence 1.00, kind ``exact_duplicate``)
     Two or more metrics share the same ``fingerprint`` -- the sha256 of
     the canonicalised DAX produced by ``dax.normalise``. That
     canonicalisation never reorders arguments, never unwraps a function
     call, never renames a variable (see ``dax/normalise.py`` for the
     full, deliberately conservative rule list) -- so an identical
     fingerprint means the two expressions are token-for-token the same
     program modulo whitespace, comments, quoting style and numeric
     spelling. This is the only tier that is a *fact*, not a guess.

  2. CANDIDATE / same shape (confidence ~0.6, kind ``near_duplicate``)
     Different fingerprint, but the same ``ref_fingerprint()`` (the same
     set of table/column references, order-independent) **and** the same
     best-effort outermost ``agg_kind``. Worth a human's time, never
     worth an automatic action: ``CALCULATE`` filters, row context
     (``SUMX`` vs ``SUM``), ``VAR``/``RETURN`` logic and time
     intelligence can all touch the same columns with the same outer
     aggregation and still compute a completely different number.

  3. CANDIDATE / possible definition drift (confidence ~0.3, kind
     ``near_duplicate``)
     The metric *names* reduce to the same bag of words (case-insensitive,
     order-insensitive, grammatical glue words dropped) but the
     expressions are not all identical. This is the least certain tier --
     name similarity says nothing about semantics -- and simultaneously,
     per CONTRACT.md, "the highest-value finding in a real estate,
     because it means the same words mean different numbers": two
     measures a business user would call by the same name computing two
     different things is exactly the kind of drift a duplicate-finder
     exists to surface. Low confidence and high value are not in
     conflict here; both are stated honestly.

Deliberately built to avoid two specific false-positive traps:

* Tiers 2 and 3 both *require at least two distinct fingerprints* in the
  candidate group. A group that is internally 100% identical is already
  fully explained by an ``exact_duplicate`` finding; re-reporting it as a
  "candidate" would be pure noise dressed up as a second opinion.
* Tier 2 requires a **non-empty** ``ref_fingerprint()`` and a **non-None**
  ``agg_kind``. Without this, every pair of measures that reference no
  columns at all (``1 + 1``, a pure ``VAR``/``RETURN`` measure, ...) would
  share the trivially-equal signature ``("", None)`` and cluster together
  for no real reason.

WHICH ONE TO KEEP
------------------
Only ``exact_duplicate`` findings carry a keep/retire recommendation
(``near_duplicate`` findings are candidates, not proven equivalent, so
there is no valid notion of "which one is correct" to fall back on). The
rule (``order_duplicate_set``) is usage-first and deterministic:

    1. Prefer the measure referenced by the most scanned visuals (distinct
       (asset, visual) pairs, across every scanned report -- see
       ``detect.unused.build_asset_index``, which is the single
       implementation of "what counts as a reference" for the whole
       codebase).
    2. Then prefer any measure with at least one reference over one with
       none. In a plain descending-count sort this is already implied by
       (1), but it is called out on its own because it is the one
       comparison that must never come out wrong: a measure a live report
       actually uses must never lose to one nothing points at.
    3. Then a measure that is NOT hidden.
    4. Then the alphabetically-first name (case-insensitive), then id, as
       a pure, meaningless-but-deterministic tiebreak.

Usage evidence outranks every other signal on purpose. An earlier version
of this rule looked only at hidden-then-alphabetical and ignored usage
entirely; on this project's own fixture that recommended keeping an
*unused* measure ("Sales Total") and retiring the one a live report visual
actually referenced ("Total Sales") -- exactly backwards, and the fastest
way to turn a zero-effort cleanup into migration work. When NO member of a
set is referenced by anything dustpan scanned, usage evidence has nothing
to say, and the tool says so out loud rather than silently falling back:
``evidence["any_member_referenced"]`` is ``False``, ``evidence["keeper_rule"]``
states plainly that "no member is referenced by any scanned visual --
keeper chosen arbitrarily for determinism", and the same caveat is
repeated on the rendered "Action" line, so the choice is never mistaken
for a claim about which measure matters more.

None of this is a judgement about DAX quality -- by construction every
member of an exact-duplicate group computes identically, so *which* one
survives cannot change any number a report shows. Free-text metadata such
as ``description`` is deliberately never consulted to choose a keeper:
this fixture's own ``'Sum of Sales'`` measure carries the description
"Duplicate created during the 2024 migration", which is exactly the kind
of field a naive "prefer the better-documented measure" heuristic would
misread as *this* being the canonical one to keep, when the text is
plainly saying the opposite. Structural, boolean facts (is it referenced?
is it hidden?) and the name itself are the only inputs, so the rule can
never be misled by what a description happens to say.

Every metric with ``lexically_ok is False`` is excluded from all three tiers
-- an expression dustpan could not understand must never contribute to a
fingerprint match. Their absence is not silent: ``pipeline._flag_unparsed``
raises one ``unparsed`` finding per such metric after every detector has
run, so a coverage gap always shows up somewhere in the report.

CROSS-MODEL EXACT DUPLICATES
-----------------------------
``fingerprint`` grouping is estate-wide, not scoped to one semantic model
-- dustpan is pointed at a *folder of* ``.pbip`` projects, so two unrelated
models both defining ``Total Sales = SUM(Sales[Amount])`` is a real and
fairly common thing to see. The identical-fingerprint fact is reported
either way (it is still true, and still useful as a "these could share a
certified dataset" signal). What changes is the actionable part: a
keep/retire recommendation is emitted (``evidence["keep"]``,
``evidence["retire"]``) only when every member of the set shares one
``model``. Across models those keys are withheld entirely -- "keep A,
retire B" is not just unhelpful but actively dangerous when A and B are
independent deployments, most likely feeding independent reports; deleting
B would not delete whatever *other* model or report still needs a "Total
Sales" measure to exist. ``evidence["same_model"]`` and
``evidence["models"]`` always say which situation a given finding is in,
and ``pipeline.proven_removable_metric_ids`` (which drives the "measures
proven removable" headline stat) only ever counts the same-model case.

Public surface (see CONTRACT.md):
    find_duplicates(estate: Estate) -> list[Finding]
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from dustpan.ir import Estate, Finding, Metric

__all__ = [
    "CONFIDENCE_CANDIDATE_NAME",
    "CONFIDENCE_CANDIDATE_SHAPE",
    "CONFIDENCE_EXACT",
    "find_duplicates",
    "name_token_bag",
    "order_duplicate_set",
]

CONFIDENCE_EXACT = 1.0
CONFIDENCE_CANDIDATE_SHAPE = 0.6
CONFIDENCE_CANDIDATE_NAME = 0.3

_SAMPLE = 12

_WORD_RE = re.compile(r"[A-Za-z0-9]+")

#: Pure grammatical glue, dropped before comparing measure-name word bags.
#: Deliberately tiny and deliberately excludes anything that could carry
#: real meaning (aggregation words like "sum"/"count"/"total" stay IN the
#: bag on purpose -- stripping them would let e.g. "Sum of Sales" and
#: "Count of Sales" collide on the leftover word "Sales" alone, which is
#: exactly the kind of over-eager match this tier must not produce).
_STOPWORDS = frozenset(
    {"of", "the", "a", "an", "and", "or", "for", "to", "by", "in", "on", "with"}
)


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def order_duplicate_set(
    metrics: list[Metric], reference_counts: dict[str, int] | None = None
) -> list[Metric]:
    """Deterministic order for an exact-duplicate set; index 0 is the keeper.

    See the module docstring ("WHICH ONE TO KEEP") for the rule and why it
    intentionally never looks at free-text fields. ``reference_counts`` is
    ``{metric id: distinct (asset, visual) pairs that reference it}``; a
    metric absent from the mapping (or a missing/empty mapping altogether)
    is treated as having zero references, which degrades safely to the old
    hidden-then-alphabetical order.
    """
    counts = reference_counts or {}
    return sorted(
        metrics,
        key=lambda m: (
            -counts.get(m.id, 0),
            1 if m.is_hidden else 0,
            (m.name or "").casefold(),
            m.id,
        ),
    )


def name_token_bag(name: str) -> frozenset[str]:
    """A measure name reduced to a case-insensitive, order-insensitive word set."""
    words = _WORD_RE.findall((name or "").casefold())
    return frozenset(w for w in words if w not in _STOPWORDS)


def _sort_group(metrics: list[Metric]) -> list[Metric]:
    return sorted(metrics, key=lambda m: ((m.name or "").casefold(), m.id))


def _metric_brief(m: Metric, *, ref_count: int | None = None) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "id": m.id,
        "name": m.name,
        "model": m.model,
        "source_path": m.source_path,
        "display_folder": m.display_folder,
        "is_hidden": m.is_hidden,
        "fingerprint": m.fingerprint,
        "agg_kind": m.agg_kind,
    }
    if ref_count is not None:
        brief["asset_references"] = ref_count
    return brief


def _reference_counts(estate: Estate) -> dict[str, int]:
    """``{metric id: distinct (asset, visual) pairs that reference it}``.

    Reuses ``detect.unused``'s asset index rather than re-walking every
    asset's visuals here, so there is exactly one implementation of "what
    counts as a reference" in the whole codebase. Best-effort and never
    raises: a broken or unavailable asset index must not stop duplicate
    detection, it should just leave every count at 0 -- the safe,
    uninformative default that ``order_duplicate_set`` degrades to.
    """
    try:
        from dustpan.detect.unused import build_asset_index

        index = build_asset_index(estate)
    except Exception:
        return {}
    return {mid: index.visual_uses(mid) for mid in index.by_metric_id}


def _quoted_names(metrics: list[Metric]) -> str:
    names = [m.name for m in metrics[:_SAMPLE]]
    text = ", ".join(repr(n) for n in names)
    if len(metrics) > _SAMPLE:
        text += f", and {len(metrics) - _SAMPLE} more"
    return text


# --------------------------------------------------------------------------
# tier 1: exact
# --------------------------------------------------------------------------


def _exact_duplicates(
    parsed: list[Metric], reference_counts: dict[str, int]
) -> list[Finding]:
    groups: dict[str, list[Metric]] = defaultdict(list)
    for m in parsed:
        if m.fingerprint:
            groups[m.fingerprint].append(m)

    findings: list[Finding] = []
    for fingerprint in sorted(groups):
        group = groups[fingerprint]
        if len(group) < 2:
            continue

        ordered = order_duplicate_set(group, reference_counts)
        keeper, retire = ordered[0], ordered[1:]
        models = sorted({m.model for m in ordered if m.model})
        # AUD-003: compare deployments, not folder basenames. Two independent
        # `Sales.SemanticModel` folders are two deployments that happen to
        # share a name; "retire all but one" across them is destructive advice.
        same_model = len({m.deployment for m in ordered}) <= 1
        blocked_by = interchangeability_blocker(ordered)
        any_referenced = any(reference_counts.get(m.id, 0) > 0 for m in ordered)

        summary = (
            f"{len(ordered)} measures normalise to the identical DAX "
            f"expression: {_quoted_names(ordered)}."
        )
        keeper_rule = (
            "usage-first, not a DAX-quality judgement -- every member "
            "computes identically by construction: prefer the measure "
            "referenced by the most scanned visuals, then any measure with "
            "at least one reference over one with none, then a visible "
            "(non-hidden) measure, then the alphabetically-first name as a "
            "pure tiebreak. Free-text fields such as `description` are "
            "never used to choose, because they can be actively misleading."
        )
        if same_model and not any_referenced:
            keeper_rule = (
                "no member is referenced by any scanned visual -- keeper "
                "chosen arbitrarily for determinism (a visible measure, "
                "then the alphabetically-first name). This choice carries "
                "no information about which measure matters more -- confirm "
                "actual usage (other reports, Excel/Analyze-in-Excel, "
                "paginated reports) before acting on it."
            )
        if not same_model:
            # A shared formula across independent semantic models is not
            # redundancy within one model -- each model is a separate
            # deployment, almost certainly feeding separate reports. "Keep
            # the first, retire the rest" is actively dangerous advice here:
            # it would tell someone to delete a measure a *different*
            # model's reports still depend on. The identical-fingerprint
            # fact is still reported (it is still true and still useful --
            # e.g. as a signal to consolidate onto a shared/certified
            # dataset) but with no keep/retire instruction attached.
            summary += (
                f" These live in {len(models)} different semantic models "
                f"({', '.join(models)}), so this is not a single-model "
                "cleanup -- see evidence."
            )
            keeper_rule = (
                "withheld: this set spans more than one semantic model, so "
                "there is no single measure to 'keep' -- each model is an "
                "independent deployment and most likely feeds independent "
                "reports. Retiring one would not retire the others' need "
                "for it. Consider this a signal for a shared/certified "
                "dataset, not a deletion candidate."
            )

        evidence: dict[str, Any] = {
            "tier": "exact",
            "normalised": ordered[0].normalised,
            "fingerprint": fingerprint,
            "measure_count": len(ordered),
            "models": models,
            "same_model": same_model,
            "retirement_blocked_by": blocked_by,
            "any_member_referenced": any_referenced,
            "keeper_rule": keeper_rule,
            "metrics": [
                _metric_brief(m, ref_count=reference_counts.get(m.id, 0)) for m in ordered
            ],
        }
        if same_model:
            # AUD-V3-004: a detector called directly, outside `scan()`,
            # used to hand back keep/retire alongside its own blocker. A
            # detector states comparison FACTS; only the actionability
            # authority may publish an instruction, and only after checking
            # current estate state. The proposal is recorded under a
            # deliberately non-actionable key that `pipeline.
            # apply_actionability` promotes when -- and only when -- the set
            # is cleared.
            evidence["keeper_proposal"] = {
                "keep": keeper.name,
                "keep_id": keeper.id,
                "retire": [m.name for m in retire],
            }

        findings.append(
            Finding(
                kind="exact_duplicate",
                severity="high",
                confidence=CONFIDENCE_EXACT,
                metric_ids=[m.id for m in ordered],
                summary=summary,
                evidence=evidence,
            )
        )
    return findings


# --------------------------------------------------------------------------
# tier 2: same shape (ref_fingerprint + agg_kind), different expression
# --------------------------------------------------------------------------


def _shape_candidates(parsed: list[Metric]) -> list[Finding]:
    groups: dict[tuple[str, str], list[Metric]] = defaultdict(list)
    for m in parsed:
        ref_fp = m.ref_fingerprint()
        if not ref_fp or not m.agg_kind:
            continue  # an empty/absent signature is not a meaningful match
        groups[(ref_fp, m.agg_kind)].append(m)

    findings: list[Finding] = []
    for key in sorted(groups):
        group = groups[key]
        if len(group) < 2:
            continue
        fingerprints = {m.fingerprint for m in group}
        if len(fingerprints) < 2:
            continue  # fully identical already -> exact_duplicate covers it

        ref_fp, agg = key
        ordered = _sort_group(group)

        findings.append(
            Finding(
                kind="near_duplicate",
                severity="medium",
                confidence=CONFIDENCE_CANDIDATE_SHAPE,
                metric_ids=[m.id for m in ordered],
                summary=(
                    f"{len(ordered)} measures all aggregate {ref_fp} with {agg}, but "
                    "their full expressions are not identical -- candidates for "
                    "consolidation, not proven equivalent: "
                    f"{_quoted_names(ordered)}."
                ),
                evidence={
                    "tier": "same_shape",
                    "ref_fingerprint": ref_fp,
                    "agg_kind": agg,
                    "distinct_expression_count": len(fingerprints),
                    "measure_count": len(ordered),
                    "distinct_normalised_forms": sorted(
                        {m.normalised for m in ordered if m.normalised}
                    )[:_SAMPLE],
                    "why_not_proven": (
                        "CALCULATE filters, row context (SUMX vs SUM), VAR logic and "
                        "time intelligence can all change the same columns and outer "
                        "aggregation into a different number."
                    ),
                    "metrics": [_metric_brief(m) for m in ordered],
                },
            )
        )
    return findings


# --------------------------------------------------------------------------
# tier 3: same name, different expression
# --------------------------------------------------------------------------


def _name_drift_candidates(parsed: list[Metric]) -> list[Finding]:
    groups: dict[frozenset[str], list[Metric]] = defaultdict(list)
    for m in parsed:
        bag = name_token_bag(m.name)
        if not bag:
            continue  # nothing but stopwords/punctuation -- no real signal
        groups[bag].append(m)

    findings: list[Finding] = []
    for bag in sorted(groups, key=lambda b: sorted(b)):
        group = groups[bag]
        if len(group) < 2:
            continue
        fingerprints = {m.fingerprint for m in group}
        if len(fingerprints) < 2:
            continue  # every same-named measure already computes the same thing

        ordered = _sort_group(group)
        distinct_names = sorted({m.name for m in ordered})

        findings.append(
            Finding(
                kind="near_duplicate",
                severity="medium",
                confidence=CONFIDENCE_CANDIDATE_NAME,
                metric_ids=[m.id for m in ordered],
                summary=(
                    f"{len(ordered)} measures share the name "
                    f"{', '.join(repr(n) for n in distinct_names)} but compute "
                    "different things -- possible definition drift, worth "
                    "confirming which one the business actually means."
                ),
                evidence={
                    "tier": "name_drift",
                    "name_tokens": sorted(bag),
                    "distinct_expression_count": len(fingerprints),
                    "measure_count": len(ordered),
                    "distinct_normalised_forms": sorted(
                        {m.normalised for m in ordered if m.normalised}
                    )[:_SAMPLE],
                    "why_not_proven": (
                        "Name similarity is not semantic equivalence -- these "
                        "expressions differ and may intentionally compute "
                        "different things."
                    ),
                    "metrics": [_metric_brief(m) for m in ordered],
                },
            )
        )
    return findings


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def find_duplicates(estate: Estate) -> list[Finding]:
    """Exact and candidate duplicate findings across every parsed metric.

    Metrics with ``lexically_ok is False`` never participate in any tier (see
    module docstring). Never raises: this function only reads `estate` and
    builds `Finding` objects from data already present on it.
    """
    parsed = [m for m in estate.metrics if m.lexically_ok and m.fingerprint]
    reference_counts = _reference_counts(estate)

    findings: list[Finding] = []
    findings += _exact_duplicates(parsed, reference_counts)
    findings += _shape_candidates(parsed)
    findings += _name_drift_candidates(parsed)
    return findings


def interchangeability_blocker(members: list[Metric]) -> str | None:
    """Why a formula-identical set is NOT safe to consolidate, or None.

    Identical DAX proves the members compute the same number. It does not
    prove they are the same OBJECT. A measure carries display and type
    metadata that changes what a user sees even when the arithmetic is
    untouched: repointing a visual from a percent-formatted measure onto a
    currency-formatted one silently changes the report while every number
    underneath stays equal. Promoting "same formula" to "safe to retire" was
    the central defect an external audit found in v0.1.0 (AUD-001).

    A dynamic format string is treated as an outright blocker rather than
    compared, because it is a separate DAX expression governing display; not
    comparing it and not refusing would be the same mistake one level down.
    """

    def spread(attr: str) -> set[object]:
        return {getattr(m, attr, None) for m in members}

    # AUD-V3-014: metadata that was PRESENT but unusable is not metadata that
    # matched. A measure whose formatString was the wrong type has unknown
    # display behaviour, and unknown is not compatible.
    unusable = sorted(
        {
            field
            for m in members
            for field in ((getattr(m, "extra", None) or {}).get("invalid_metadata") or ())
        }
    )
    if unusable:
        return (
            "a member has unusable display metadata ("
            + ", ".join(unusable)
            + ") -- present but the wrong type, so compatibility is unknown"
        )
    if any((getattr(m, "extra", None) or {}).get("dynamic_format") for m in members):
        return (
            "a member has a dynamic format string -- a separate DAX expression "
            "controlling display, which this version does not compare"
        )
    if len(spread("format_string")) > 1:
        return "members disagree on formatString, so they display differently"
    if len(spread("data_type")) > 1:
        return "members disagree on dataType"
    return None

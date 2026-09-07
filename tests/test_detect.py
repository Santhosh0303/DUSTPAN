"""Tests for `detect/duplicates.py` and `detect/unused.py`.

Both detectors exist to answer one question honestly: is this claim safe
enough to act on? So these tests check not just *that* a finding appears,
but that its `kind` / `confidence` sit in the right tier, and that the
things which must NEVER be claimed (a CALCULATE-wrapped near-miss as an
exact duplicate; a measure used only via another measure's expression as
unused; "no reports scanned" as "everything is unused") really are absent.
"""

# ADAPTED for AUD-V3-004 (v3 audit, section 8 bullet 1).
#
# ORIGINAL EXPECTATION: `find_duplicates` itself returns evidence["keep"] and
# evidence["retire"].
#
# EVIDENCE IT WAS WRONG: v3 case B-CO-04 showed the public detector handing
# back keep/retire alongside its own metadata blocker, so an API consumer
# received a deletion instruction the pipeline would have refused.
#
# CORRECTED EXPECTATION: a detector publishes the comparison FACT,
# evidence["keeper_proposal"]; only pipeline.apply_actionability promotes it to
# keep/retire after checking current estate state.
#
# WHY THE REQUIREMENT SURVIVES: "a same-model exact duplicate names a keeper
# and the measures to retire" is still asserted here, at the layer that owns
# it; the promoted keys are asserted through the authority elsewhere.

from __future__ import annotations

import pytest

from conftest import (
    ADVERSARIAL_CALCULATE,
    ADVERSARIAL_SUMX,
    DUPLICATE_TRIO,
    ORPHAN,
    REFERENCED_BY_REPORT,
    make_metric,
    metric_by_name,
)
from dustpan.detect.duplicates import (
    CONFIDENCE_CANDIDATE_NAME,
    CONFIDENCE_CANDIDATE_SHAPE,
    CONFIDENCE_EXACT,
    find_duplicates,
    name_token_bag,
    order_duplicate_set,
)
from dustpan.detect.unused import (
    CONFIDENCE_INFORMATIONAL,
    CONFIDENCE_UNUSED,
    CONFIDENCE_UNUSED_DEGRADED,
    build_asset_index,
    build_dependency_graph,
    find_unused,
    metric_name_key,
    reference_keys,
)
from dustpan.ir import Asset, Estate, Metric, Visual

# ---------------------------------------------------------------------------
# find_duplicates: end-to-end against the real fixture
# ---------------------------------------------------------------------------


def test_exactly_one_exact_duplicate_set_for_the_planted_trio(sales_estate):
    findings = find_duplicates(sales_estate)
    exact = [f for f in findings if f.kind == "exact_duplicate"]
    assert len(exact) == 1
    finding = exact[0]
    assert finding.confidence == CONFIDENCE_EXACT == 1.0
    assert finding.severity == "high"
    names = {metric_by_name(sales_estate, n).id for n in DUPLICATE_TRIO}
    assert set(finding.metric_ids) == names


def test_adversarial_measures_never_appear_in_any_duplicate_finding(sales_estate):
    """CONTRACT.md: the CALCULATE-wrapped and SUMX measures must NOT be
    reported as duplicates of the SUM trio -- at any confidence."""
    findings = find_duplicates(sales_estate)
    all_ids = {mid for f in findings for mid in f.metric_ids}
    uk_id = metric_by_name(sales_estate, ADVERSARIAL_CALCULATE).id
    revenue_id = metric_by_name(sales_estate, ADVERSARIAL_SUMX).id
    assert uk_id not in all_ids
    assert revenue_id not in all_ids


def test_exact_duplicate_keeper_rule_prefers_the_referenced_measure(sales_estate):
    """CONTRACT.md: 'Total Sales' is referenced by a SalesDemo.Report visual;
    'Sales Total' and 'Sum of Sales' are not. The keeper must be the measure
    something actually uses -- recommending the opposite (keep the unused
    one, retire the one a live visual points at) is exactly the false
    recommendation this rule exists to prevent."""
    finding = next(
        f for f in find_duplicates(sales_estate) if f.kind == "exact_duplicate"
    )
    ordered_names = [sales_estate.metric_by_id()[mid].name for mid in finding.metric_ids]
    assert ordered_names[0] == "Total Sales"
    # The remaining two are tied on reference count (zero each), so they
    # fall back to alphabetical (case-insensitive) order.
    assert ordered_names[1:] == sorted(ordered_names[1:], key=str.casefold)
    assert finding.evidence["keeper_proposal"]["keep"] == "Total Sales"
    assert (
        finding.evidence["keeper_proposal"]["keep_id"]
        == metric_by_name(sales_estate, "Total Sales").id
    )
    assert finding.evidence["keeper_proposal"]["retire"] == ordered_names[1:]
    assert finding.evidence["any_member_referenced"] is True


def test_evidence_carries_the_actual_expressions_for_a_human_to_verify(sales_estate):
    finding = next(
        f for f in find_duplicates(sales_estate) if f.kind == "exact_duplicate"
    )
    assert finding.evidence.get("normalised")
    metrics_evidence = finding.evidence.get("metrics")
    assert metrics_evidence and len(metrics_evidence) == 3
    assert all("fingerprint" in m for m in metrics_evidence)
    # Reference counts ride along in the same structure so a machine reader
    # (or the console's small aligned table) can show *why* the keeper won.
    assert all("asset_references" in m for m in metrics_evidence)
    by_name = {m["name"]: m["asset_references"] for m in metrics_evidence}
    assert by_name["Total Sales"] >= 1
    assert by_name["Sales Total"] == 0
    assert by_name["Sum of Sales"] == 0


def test_find_duplicates_never_raises_on_an_empty_estate():
    assert find_duplicates(Estate()) == []


# ---------------------------------------------------------------------------
# find_duplicates: targeted tier behaviour with hand-built metrics
# ---------------------------------------------------------------------------


def test_order_duplicate_set_prefers_non_hidden_over_alphabetically_first():
    hidden_but_alphabetically_first = make_metric(
        "AAA Hidden", "SUM(Sales[Amount])", is_hidden=True
    )
    visible = make_metric("ZZZ Visible", "SUM(Sales[Amount])", is_hidden=False)
    ordered = order_duplicate_set([hidden_but_alphabetically_first, visible])
    assert ordered[0].name == "ZZZ Visible"


def test_order_duplicate_set_falls_back_to_alphabetical_case_insensitive():
    a = make_metric("banana", "1", is_hidden=False)
    b = make_metric("Apple", "1", is_hidden=False)
    ordered = order_duplicate_set([a, b])
    assert [m.name for m in ordered] == ["Apple", "banana"]


def test_order_duplicate_set_reference_count_outranks_hidden_status():
    """FIX 1: usage evidence dominates every other signal, including the
    hidden flag -- a hidden-but-referenced measure still wins over a
    visible-but-unreferenced one."""
    hidden_and_referenced = make_metric(
        "AAA Hidden", "SUM(Sales[Amount])", is_hidden=True
    )
    visible_but_unreferenced = make_metric(
        "ZZZ Visible", "SUM(Sales[Amount])", is_hidden=False
    )
    counts = {hidden_and_referenced.id: 3, visible_but_unreferenced.id: 0}
    ordered = order_duplicate_set(
        [visible_but_unreferenced, hidden_and_referenced], counts
    )
    assert ordered[0].name == "AAA Hidden"


def test_order_duplicate_set_any_reference_beats_none_even_with_fewer_refs():
    referenced_once = make_metric("Once", "SUM(Sales[Amount])")
    referenced_never = make_metric("Never", "SUM(Sales[Amount])")
    counts = {referenced_once.id: 1, referenced_never.id: 0}
    ordered = order_duplicate_set([referenced_never, referenced_once], counts)
    assert ordered[0].name == "Once"


def test_order_duplicate_set_degrades_safely_with_no_reference_counts():
    """A missing/empty reference_counts mapping must fall back to the old
    hidden-then-alphabetical order, never crash or silently misorder."""
    a = make_metric("A", "1")
    b = make_metric("B", "1")
    assert order_duplicate_set([b, a]) == order_duplicate_set([b, a], {})
    assert order_duplicate_set([b, a], None)[0].name == "A"


def test_shape_candidate_tier_needs_two_distinct_fingerprints():
    """Same (ref_fingerprint, agg_kind), different actual DAX -> a tier-2
    candidate, never a proven duplicate."""
    a = make_metric("A", "SUM(Sales[Amount])")
    b = make_metric("B", "CALCULATE(SUM(Sales[Amount]), Sales[Amount] > 0)")
    assert a.fingerprint != b.fingerprint
    assert a.ref_fingerprint() == b.ref_fingerprint()
    assert a.agg_kind == b.agg_kind == "SUM"

    findings = find_duplicates(Estate(metrics=[a, b]))
    exact = [f for f in findings if f.kind == "exact_duplicate"]
    near = [f for f in findings if f.kind == "near_duplicate"]
    assert exact == []
    assert len(near) == 1
    assert near[0].confidence == CONFIDENCE_CANDIDATE_SHAPE
    assert set(near[0].metric_ids) == {a.id, b.id}


def test_shape_candidate_tier_requires_nonempty_ref_fingerprint_and_agg_kind():
    """Two different expressions that both touch nothing and aggregate
    nothing must not cluster on the trivially-equal signature ('', None)."""
    a = make_metric("A", "1 + 1")
    b = make_metric("B", "2 + 2")
    assert a.ref_fingerprint() == "" and b.ref_fingerprint() == ""
    assert a.agg_kind is None and b.agg_kind is None
    findings = find_duplicates(Estate(metrics=[a, b]))
    assert findings == []


def test_name_drift_tier_fires_on_same_name_bag_different_computation():
    a = make_metric("Total Sales", "SUM(Sales[Amount])")
    b = make_metric("Sales Total", "COUNTROWS(Sales)")
    assert name_token_bag(a.name) == name_token_bag(b.name)
    assert a.fingerprint != b.fingerprint
    assert a.ref_fingerprint() != b.ref_fingerprint()  # not also a shape candidate

    findings = find_duplicates(Estate(metrics=[a, b]))
    drift = [f for f in findings if f.kind == "near_duplicate"]
    assert len(drift) == 1
    assert drift[0].confidence == CONFIDENCE_CANDIDATE_NAME
    assert set(drift[0].metric_ids) == {a.id, b.id}


def test_name_drift_tier_does_not_fire_when_same_name_bag_is_fully_identical():
    """Two measures with the same name-bag that also compute the exact same
    thing are already fully explained by the exact_duplicate finding --
    re-reporting them as a name-drift 'candidate' would be pure noise."""
    a = make_metric("Total Sales", "SUM(Sales[Amount])")
    b = make_metric("Sales Total", "SUM(Sales[Amount])")
    findings = find_duplicates(Estate(metrics=[a, b]))
    kinds = {f.kind for f in findings}
    assert kinds == {"exact_duplicate"}


def test_unparsed_metrics_are_excluded_from_every_tier():
    a = make_metric("A", "SUM(")  # unterminated -> lexically_ok False
    b = make_metric("B", "SUM(")  # identical broken text -> same #UNPARSED# fingerprint
    assert a.lexically_ok is False and b.lexically_ok is False
    assert a.fingerprint == b.fingerprint  # sanity: would collide if not filtered out
    findings = find_duplicates(Estate(metrics=[a, b]))
    assert findings == []


def test_single_metric_never_produces_a_duplicate_finding():
    a = make_metric("Solo", "SUM(Sales[Amount])")
    assert find_duplicates(Estate(metrics=[a])) == []


def test_same_model_exact_duplicate_gets_a_keep_retire_recommendation():
    a = make_metric("A", "SUM(Sales[Amount])", model="OneModel")
    b = make_metric("B", "SUM(Sales[Amount])", model="OneModel")
    finding = next(
        f for f in find_duplicates(Estate(metrics=[a, b])) if f.kind == "exact_duplicate"
    )
    assert finding.evidence["same_model"] is True
    assert finding.evidence["models"] == ["OneModel"]
    assert finding.evidence["keeper_proposal"]["keep"]
    assert finding.evidence["keeper_proposal"]["retire"]
    # No assets were scanned at all, so neither measure has any usage
    # evidence -- the tool must say the keeper choice carries no information
    # rather than silently presenting an arbitrary pick as a real one.
    assert finding.evidence["any_member_referenced"] is False
    assert (
        "no member is referenced by any scanned visual" in finding.evidence["keeper_rule"]
    )


def test_keeper_rule_prefers_the_measure_with_more_asset_references():
    a = make_metric("Alpha", "SUM(Sales[Amount])", model="M")
    b = make_metric("Beta", "SUM(Sales[Amount])", model="M")
    asset = Asset(
        id="asset1",
        name="R1",
        tool="powerbi",
        source_path="p",
        model="M",
        visuals=[
            Visual(id="v1", page="P1", visual_type="card", metric_names=["Beta"]),
            Visual(id="v2", page="P1", visual_type="card", metric_names=["Beta"]),
        ],
    )
    finding = next(
        f
        for f in find_duplicates(Estate(metrics=[a, b], assets=[asset]))
        if f.kind == "exact_duplicate"
    )
    assert finding.evidence["keeper_proposal"]["keep"] == "Beta"
    assert finding.evidence["any_member_referenced"] is True
    by_name = {m["name"]: m["asset_references"] for m in finding.evidence["metrics"]}
    assert by_name["Beta"] == 2
    assert by_name["Alpha"] == 0


def test_keeper_rule_says_so_explicitly_when_nothing_in_the_set_is_referenced():
    """An asset was scanned, but it references a measure outside this
    duplicate set entirely -- neither member has any usage evidence, and
    the tool must not dress up an arbitrary pick as a real recommendation."""
    a = make_metric("Alpha", "SUM(Sales[Amount])", model="M")
    b = make_metric("Beta", "SUM(Sales[Amount])", model="M")
    other = make_metric("Other", "COUNTROWS(Sales)", model="M")
    asset = Asset(
        id="asset1",
        name="R1",
        tool="powerbi",
        source_path="p",
        model="M",
        visuals=[Visual(id="v1", page="P1", visual_type="card", metric_names=["Other"])],
    )
    estate = Estate(metrics=[a, b, other], assets=[asset])
    finding = next(f for f in find_duplicates(estate) if f.kind == "exact_duplicate")
    assert finding.evidence["any_member_referenced"] is False
    assert (
        "no member is referenced by any scanned visual" in finding.evidence["keeper_rule"]
    )
    assert "keeper chosen arbitrarily for determinism" in finding.evidence["keeper_rule"]
    # Still falls back to a deterministic order -- alphabetical here, since
    # neither is hidden.
    assert finding.evidence["keeper_proposal"]["keep"] == "Alpha"


def test_cross_model_exact_duplicate_withholds_keep_retire_recommendation():
    """Two unrelated semantic models both defining the same formula is real
    and common in a multi-project scan -- but 'keep A, retire B' is
    dangerous advice across that boundary: each model is an independent
    deployment, most likely feeding independent reports. The identical-
    fingerprint FACT must still be reported (it is still true and useful),
    but with no keep/retire instruction attached."""
    a = make_metric("A", "SUM(Sales[Amount])", model="ModelOne", id="powerbi:ModelOne:A")
    b = make_metric("B", "SUM(Sales[Amount])", model="ModelTwo", id="powerbi:ModelTwo:B")
    findings = find_duplicates(Estate(metrics=[a, b]))
    finding = next(f for f in findings if f.kind == "exact_duplicate")
    # The fact itself is still reported, at full confidence.
    assert finding.confidence == CONFIDENCE_EXACT
    assert set(finding.metric_ids) == {a.id, b.id}
    # But the actionable keep/retire recommendation is withheld.
    assert finding.evidence["same_model"] is False
    assert finding.evidence["models"] == ["ModelOne", "ModelTwo"]
    assert "keeper_proposal" not in finding.evidence and "keep" not in finding.evidence
    assert "keep_id" not in finding.evidence
    assert "retire" not in finding.evidence


def test_proven_removable_metric_ids_excludes_cross_model_duplicates():
    from dustpan import pipeline

    a = make_metric("A", "SUM(Sales[Amount])", model="ModelOne", id="powerbi:ModelOne:A")
    b = make_metric("B", "SUM(Sales[Amount])", model="ModelTwo", id="powerbi:ModelTwo:B")
    estate = Estate(metrics=[a, b])
    estate.findings = find_duplicates(estate)
    assert pipeline.proven_removable_metric_ids(estate) == set()


def test_proven_removable_metric_ids_still_counts_same_model_duplicates():
    """ADAPTED for AUD-V3-005.

    ORIGINAL EXPECTATION: two same-model duplicates are removable with no
    asset in the estate at all.

    EVIDENCE IT WAS WRONG: v3 case B-CO-05 showed usage coverage being treated
    as one global fact, so a deployment with no report of its own inherited
    another deployment's coverage. With no asset anywhere, nothing is known
    about who consumes these measures.

    CORRECTED EXPECTATION: the deployment gets a bound report, which is the
    evidence the original test silently assumed it had.

    WHY THE REQUIREMENT SURVIVES: the assertion -- same-model duplicates with
    sufficient evidence yield exactly one removable id -- is unchanged. This
    is also the positive control the handoff requires: the repair must not
    achieve safety by suppressing every result."""
    from dustpan import pipeline
    from dustpan.ir import Asset, Visual

    a = make_metric("A", "SUM(Sales[Amount])", model="OneModel", id="powerbi:OneModel:A")
    b = make_metric("B", "SUM(Sales[Amount])", model="OneModel", id="powerbi:OneModel:B")
    a.source_root = b.source_root = "/proj/One"
    estate = Estate(metrics=[a, b])
    estate.assets.append(
        Asset(
            id="powerbi:OneReport",
            name="OneReport",
            tool="powerbi",
            source_path="/proj/One/One.Report",
            source_root="/proj/One",
            visuals=[Visual(id="v", page="p", visual_type="card", metric_names=["A"])],
        )
    )
    estate.findings = find_duplicates(estate)
    proven = pipeline.proven_removable_metric_ids(estate)
    assert len(proven) == 1
    assert proven <= {a.id, b.id}


def test_review_candidate_metric_ids_never_overlaps_proven_removable(sales_estate):
    """FIX 2: the two headline numbers must never merge -- not just in the
    rendered stat, but as sets. A metric proven removable is never also
    listed as merely 'worth reviewing'."""
    from dustpan import pipeline

    sales_estate.findings = find_duplicates(sales_estate) + find_unused(sales_estate)
    proven = pipeline.proven_removable_metric_ids(sales_estate)
    candidates = pipeline.review_candidate_metric_ids(sales_estate)
    assert proven, "the planted trio should yield same-model proven-removable ids"
    assert candidates, "the orphan measure should yield a review candidate"
    assert proven.isdisjoint(candidates)
    # The orphan measure is unused, not an exact duplicate -- it belongs
    # only in the review-candidates bucket.
    orphan_id = metric_by_name(sales_estate, ORPHAN).id
    assert orphan_id in candidates
    assert orphan_id not in proven


def test_review_candidate_metric_ids_includes_near_duplicate_members():
    a = make_metric("Total Sales", "SUM(Sales[Amount])")
    b = make_metric("Sales Total", "COUNTROWS(Sales)")  # name-drift candidate, not exact
    estate = Estate(metrics=[a, b])
    estate.findings = find_duplicates(estate)
    from dustpan import pipeline

    candidates = pipeline.review_candidate_metric_ids(estate)
    assert candidates == {a.id, b.id}
    assert pipeline.proven_removable_metric_ids(estate) == set()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Total Sales", {"total", "sales"}),
        ("Sum of Sales", {"sum", "sales"}),  # "of" is a dropped stopword
        ("Sales Total", {"sales", "total"}),
        ("", set()),
        ("The Of A", set()),  # pure stopwords -> empty bag, no signal
    ],
)
def test_name_token_bag(name, expected):
    assert name_token_bag(name) == frozenset(expected)


# ---------------------------------------------------------------------------
# find_unused: end-to-end against the real fixture
# ---------------------------------------------------------------------------


def test_orphan_measure_is_reported_unused(sales_estate):
    findings = find_unused(sales_estate)
    unused_names = {
        sales_estate.metric_by_id()[mid].name
        for f in findings
        if f.kind == "unused_measure"
        for mid in f.metric_ids
    }
    assert ORPHAN in unused_names


def test_report_referenced_measures_are_never_reported_unused(sales_estate):
    """CONTRACT.md: Total Sales, Revenue and Order Count are referenced by
    report.json visuals and must never be claimed unused."""
    findings = find_unused(sales_estate)
    unused_names = {
        sales_estate.metric_by_id()[mid].name
        for f in findings
        if f.kind == "unused_measure"
        for mid in f.metric_ids
    }
    assert unused_names.isdisjoint(REFERENCED_BY_REPORT)


def test_unused_findings_are_capped_below_1_0_confidence(sales_estate):
    findings = find_unused(sales_estate)
    unused = [f for f in findings if f.kind == "unused_measure"]
    assert unused
    assert all(f.confidence < 1.0 for f in unused)
    assert all(f.confidence == CONFIDENCE_UNUSED for f in unused)  # nothing degraded here


def test_find_unused_on_empty_estate_returns_nothing():
    assert find_unused(Estate()) == []


# ---------------------------------------------------------------------------
# find_unused: the no-assets case must never claim everything is unused
# ---------------------------------------------------------------------------


def test_no_assets_scanned_yields_one_informational_finding_never_unused_claims(
    sales_estate_no_report,
):
    findings = find_unused(sales_estate_no_report)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "usage_unknown"
    assert finding.confidence == CONFIDENCE_INFORMATIONAL == 0.0
    assert finding.metric_ids == []  # no metric is claimed to be removable
    assert finding.evidence.get("informational") is True
    # The catastrophic failure mode CONTRACT.md warns about: reporting every
    # measure unused because no reports were scanned.
    assert not any(f.kind == "unused_measure" for f in findings)


# ---------------------------------------------------------------------------
# find_unused: measure-referenced-by-measure is used, transitively
# ---------------------------------------------------------------------------


def test_measure_referenced_by_another_measure_is_not_unused():
    base = make_metric("Base", "SUM(Sales[Amount])")
    wrapper = make_metric("Wrapper", "[Base] * 2")
    untouched = make_metric("Untouched", "COUNTROWS(Sales)")
    asset = Asset(
        id="asset1",
        name="R1",
        tool="powerbi",
        source_path="p",
        model="M",
        visuals=[
            Visual(id="v1", page="P1", visual_type="card", metric_names=["Wrapper"])
        ],
    )
    estate = Estate(metrics=[base, wrapper, untouched], assets=[asset])

    findings = find_unused(estate)
    unused_names = {
        estate.metric_by_id()[mid].name
        for f in findings
        if f.kind == "unused_measure"
        for mid in f.metric_ids
    }
    assert unused_names == {"Untouched"}
    assert "Base" not in unused_names  # reached transitively through Wrapper
    assert "Wrapper" not in unused_names  # referenced directly by the visual


def test_transitive_chain_of_three_measures():
    a = make_metric("A", "SUM(Sales[Amount])")
    b = make_metric("B", "[A] + 1")
    c = make_metric("C", "[B] + 1")
    asset = Asset(
        id="asset1",
        name="R1",
        tool="powerbi",
        source_path="p",
        model="M",
        visuals=[Visual(id="v1", page="P1", visual_type="card", metric_names=["C"])],
    )
    estate = Estate(metrics=[a, b, c], assets=[asset])
    findings = find_unused(estate)
    assert [f for f in findings if f.kind == "unused_measure"] == []


def test_raw_bracket_scan_safety_net_catches_refs_the_lexer_could_not():
    """If the DAX normaliser fails so badly that `Metric.refs` ends up empty
    (an unterminated string can swallow the rest of the expression into one
    ERROR token), the measure it referenced must still not look orphaned."""
    base = make_metric("Base2", "SUM(Sales[Amount])")
    broken = make_metric("BrokenRef", '"oops + [Base2]')  # unterminated string
    assert broken.lexically_ok is False
    assert (
        broken.refs == []
    )  # proves the primary refs-based edge can't be what saves Base2

    asset = Asset(
        id="asset1",
        name="R1",
        tool="powerbi",
        source_path="p",
        model="M",
        visuals=[
            Visual(id="v1", page="P1", visual_type="card", metric_names=["BrokenRef"])
        ],
    )
    estate = Estate(metrics=[base, broken], assets=[asset])
    findings = find_unused(estate)
    unused_names = {
        estate.metric_by_id()[mid].name
        for f in findings
        if f.kind == "unused_measure"
        for mid in f.metric_ids
    }
    assert "Base2" not in unused_names


def test_degraded_confidence_when_estate_has_unparsed_metrics():
    used = make_metric("Used", "SUM(Sales[Amount])")
    unused = make_metric("ReallyUnused", "COUNTROWS(Sales)")
    broken = make_metric("Broken", "SUM(")  # lexically_ok False anywhere in the estate
    assert broken.lexically_ok is False
    asset = Asset(
        id="asset1",
        name="R1",
        tool="powerbi",
        source_path="p",
        model="M",
        visuals=[Visual(id="v1", page="P1", visual_type="card", metric_names=["Used"])],
    )
    estate = Estate(metrics=[used, unused, broken], assets=[asset])
    findings = find_unused(estate)
    target = next(
        f
        for f in findings
        if f.kind == "unused_measure"
        and "ReallyUnused" in [estate.metric_by_id()[mid].name for mid in f.metric_ids]
    )
    assert target.confidence == CONFIDENCE_UNUSED_DEGRADED
    assert target.confidence < CONFIDENCE_UNUSED


# ---------------------------------------------------------------------------
# name resolution helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sales.Total Sales", ["sales.total sales", "total sales"]),
        ("'Sales'[Total Sales]", ["'sales'[total sales]", "total sales"]),
        ("[Total Sales]", ["[total sales]", "total sales"]),
        ("Total Sales", ["total sales"]),
    ],
)
def test_reference_keys_matches_its_own_documented_examples(raw, expected):
    assert reference_keys(raw) == expected


def test_metric_name_key_is_case_and_whitespace_insensitive():
    assert metric_name_key("Total Sales") == metric_name_key("  total   sales ")
    assert metric_name_key("'Total Sales'") == metric_name_key("Total Sales")


def test_build_asset_index_resolves_report_referenced_measures(sales_estate):
    index = build_asset_index(sales_estate)
    assert index.asset_count == 1
    assert index.visual_count == 3
    resolved_names = {
        sales_estate.metric_by_id()[mid].name
        for mid in index.by_metric_id
        if mid in sales_estate.metric_by_id()
    }
    assert resolved_names == set(REFERENCED_BY_REPORT)


def test_build_dependency_graph_captures_bare_measure_refs():
    base = make_metric("Base", "SUM(Sales[Amount])")
    wrapper = make_metric("Wrapper", "[Base] * 2")
    graph = build_dependency_graph(Estate(metrics=[base, wrapper]))
    assert graph[wrapper.id] == {base.id}
    assert graph[base.id] == set()


def test_build_dependency_graph_never_creates_a_self_edge():
    # A measure that (incorrectly) references its own name must not create
    # a self-loop that could confuse reachability.
    m = Metric(
        id="powerbi:M:Self",
        name="Self",
        tool="powerbi",
        model="M",
        source_path="p",
        expression="[Self] + 1",
        dialect="dax",
    )
    from dustpan.dax.normalise import enrich

    enrich(m)
    graph = build_dependency_graph(Estate(metrics=[m]))
    assert m.id not in graph[m.id]


def test_formula_identical_but_metadata_different_is_not_proven_removable():
    """AUD-001. Identical DAX proves the numbers match; it does not prove the
    measures are interchangeable. A percent-formatted measure and a
    currency-formatted one show the user different things."""
    from dustpan.detect.duplicates import interchangeability_blocker
    from dustpan.ir import Metric

    def m(name, fmt="#,0.00", dtype="double", extra=None):
        return Metric(
            id=f"powerbi:M:{name}",
            name=name,
            tool="powerbi",
            model="M",
            source_path="x.tmdl",
            expression="SUM(Sales[Amount])",
            dialect="dax",
            format_string=fmt,
            data_type=dtype,
            extra=extra or {},
        )

    assert interchangeability_blocker([m("A"), m("B")]) is None
    assert "formatString" in (
        interchangeability_blocker([m("A"), m("B", fmt="0.0%")]) or ""
    )
    assert "dataType" in (
        interchangeability_blocker([m("A"), m("B", dtype="string")]) or ""
    )
    assert "dynamic format" in (
        interchangeability_blocker([m("A"), m("B", extra={"dynamic_format": True})]) or ""
    )


def test_proven_removable_is_empty_when_usage_is_unknown():
    """AUD-001/CO-01. Claiming something is safe to delete while admitting no
    report was scanned is incoherent, however identical the DAX."""
    from dustpan import pipeline
    from dustpan.dax.normalise import enrich
    from dustpan.detect.duplicates import find_duplicates
    from dustpan.detect.unused import find_unused
    from dustpan.ir import Estate, Metric

    estate = Estate()
    for name in ("A", "B"):
        metric = Metric(
            id=f"powerbi:M:{name}",
            name=name,
            tool="powerbi",
            model="M",
            source_path="x.tmdl",
            expression="SUM(Sales[Amount])",
            dialect="dax",
            format_string="#,0.00",
            data_type="double",
        )
        enrich(metric)
        estate.metrics.append(metric)
    estate.findings = find_duplicates(estate) + find_unused(estate)

    assert any(f.kind == "usage_unknown" for f in estate.findings)
    assert pipeline.proven_removable_metric_ids(estate) == set()

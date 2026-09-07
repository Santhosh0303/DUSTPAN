"""Tests for `parsers/discover.py` and `parsers/report.py`.

The report parser's one job is answering "which measure names does this
report reference?" honestly -- CONTRACT.md is explicit that a missed
reference becomes a false "unused" finding, the worst failure mode this
tool has. So these tests weight heavily towards: filters (page, visual,
report-level, bookmark) must count as usage exactly like a projection does,
across BOTH the legacy `report.json` format and the newer PBIR folder
layout confirmed present in `tests/fixtures/PbirDemo.Report`.
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import FIXTURES_DIR, PBIR_REPORT_DIR, SALES_MODEL_DIR, SALES_REPORT_DIR
from dustpan.ir import Estate
from dustpan.parsers.discover import discover, model_name_from_path
from dustpan.parsers.report import PSEUDO_VISUAL_TYPES, parse_query_ref, parse_report

# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_finds_every_planted_fixture_part():
    found = discover(FIXTURES_DIR)
    kinds = {kind for kind, _ in found}
    assert "pbip_model" in kinds
    assert "pbip_report" in kinds

    model_paths = [p for k, p in found if k == "pbip_model"]
    assert any(
        p.replace("\\", "/").endswith("SalesDemo.SemanticModel/definition")
        for p in model_paths
    )

    report_paths = {os.path.basename(p) for k, p in found if k == "pbip_report"}
    assert report_paths == {"SalesDemo.Report", "PbirDemo.Report"}


def test_discover_returns_each_part_exactly_once():
    found = discover(FIXTURES_DIR)
    assert len(found) == len(set(found))


def test_discover_models_sort_before_reports():
    found = discover(FIXTURES_DIR)
    kinds_in_order = [k for k, _ in found]
    first_report = next(i for i, k in enumerate(kinds_in_order) if k == "pbip_report")
    assert all(k in ("pbip_model", "model_bim") for k in kinds_in_order[:first_report])


def test_discover_on_nonexistent_path_returns_empty_list():
    assert discover("/definitely/does/not/exist/anywhere") == []


def test_discover_on_empty_directory_returns_empty_list(tmp_path):
    assert discover(str(tmp_path)) == []


def test_discover_tolerates_nesting(tmp_path):
    # "Tolerant of nesting" per CONTRACT.md: bury the same parts several
    # levels deep inside unrelated folders and confirm they are still found.
    nested = tmp_path / "workspace" / "exports" / "2026" / "Q1"
    model_dir = nested / "Nested.SemanticModel" / "definition"
    model_dir.mkdir(parents=True)
    (model_dir / "model.tmdl").write_text("model Model\n\tculture: en-US\n")
    (model_dir / "T.tmdl").write_text("table T\n\n\tmeasure 'X' = 1\n")

    report_dir = nested / "Nested.Report"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text('{"sections": []}')

    found = discover(str(tmp_path))
    kinds = {kind for kind, _ in found}
    assert "pbip_model" in kinds
    assert "pbip_report" in kinds


def test_discover_prefers_tmdl_over_model_bim_when_both_present(tmp_path):
    """A single semantic model is stored as TMDL *or* TMSL, never both --
    if a folder somehow has both, TMDL wins so the same measures are not
    parsed (and turned into bogus 'duplicates') twice."""
    model_root = tmp_path / "Both.SemanticModel"
    definition = model_root / "definition"
    definition.mkdir(parents=True)
    (definition / "T.tmdl").write_text("table T\n\n\tmeasure 'X' = 1\n")
    (model_root / "model.bim").write_text(json.dumps({"model": {"tables": []}}))

    found = discover(str(model_root))
    kinds = [kind for kind, _ in found]
    assert "pbip_model" in kinds
    assert "model_bim" not in kinds


def test_discover_never_raises_on_a_file_path():
    # __file__ is a real .py file, not a directory and not named model.bim.
    assert discover(__file__) == []


def test_model_name_from_path_strips_semanticmodel_suffix():
    assert model_name_from_path(SALES_MODEL_DIR) == "SalesDemo"


def test_model_name_from_path_strips_dataset_suffix(tmp_path):
    p = tmp_path / "Legacy.Dataset" / "definition"
    p.mkdir(parents=True)
    assert model_name_from_path(str(p)) == "Legacy"


# ---------------------------------------------------------------------------
# parse_query_ref()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query_ref,table,field,agg",
    [
        ("Sales.Total Sales", "Sales", "Total Sales", None),
        ("Sum(Sales.Amount)", "Sales", "Amount", "Sum"),
        ("CountNonNull(Sales.Id)", "Sales", "Id", "CountNonNull"),
        ("Date.Calendar.Year", "Date", "Calendar.Year", None),
        ("Total Sales", None, "Total Sales", None),
        ("", None, "", None),
    ],
)
def test_parse_query_ref(query_ref, table, field, agg):
    assert parse_query_ref(query_ref) == (table, field, agg)


# ---------------------------------------------------------------------------
# legacy report.json (tests/fixtures/SalesDemo.Report)
# ---------------------------------------------------------------------------


def test_legacy_report_parses_to_one_asset_with_correct_page():
    estate = Estate()
    parse_report(SALES_REPORT_DIR, estate)
    assert estate.errors == []
    assert len(estate.assets) == 1
    asset = estate.assets[0]
    assert asset.tool == "powerbi"
    assert asset.extra.get("format") == "legacy"
    real_visuals = [v for v in asset.visuals if v.visual_type not in PSEUDO_VISUAL_TYPES]
    assert {v.page for v in real_visuals} == {"Overview"}


def test_legacy_report_references_exactly_the_planted_measures():
    """CONTRACT.md: 'Total Sales, Revenue, Order Count are referenced by
    report.json visuals.'"""
    estate = Estate()
    parse_report(SALES_REPORT_DIR, estate)
    names = estate.assets[0].referenced_metric_names()
    assert {"Total Sales", "Revenue", "Order Count"} <= names


def test_legacy_report_visual_containers_map_projections_correctly():
    estate = Estate()
    parse_report(SALES_REPORT_DIR, estate)
    real_visuals = {
        v.id: v
        for v in estate.assets[0].visuals
        if v.visual_type not in PSEUDO_VISUAL_TYPES
    }
    by_names = {frozenset(v.metric_names) for v in real_visuals.values()}
    assert frozenset({"Total Sales"}) in by_names
    assert frozenset({"Revenue", "Region"}) in by_names
    assert frozenset({"Order Count"}) in by_names


# ---------------------------------------------------------------------------
# PBIR (tests/fixtures/PbirDemo.Report) -- filters at every scope must count
# as usage, exactly the scenario CONTRACT.md warns about.
# ---------------------------------------------------------------------------


def test_pbir_format_and_model_detected():
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert estate.errors == []
    asset = estate.assets[0]
    assert asset.extra.get("format") == "pbir"
    assert asset.model == "PbirDemo"  # from definition.pbir's datasetReference.byPath


def test_pbir_page_order_and_display_names_from_pages_json():
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert estate.assets[0].extra.get("pages") == ["Summary", "Order Detail"]


def test_pbir_visual_projection_references():
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    visuals = {v.id: set(v.metric_names) for v in estate.assets[0].visuals}
    # card1: a Values projection naming a Measure.
    assert "Total Sales" in visuals["card1"]
    # bar1: Category (Column) + Y (Measure) + a conditional-formatting
    # FillRule input (Measure) nested under objects.dataPoint.
    assert {"Region", "Revenue", "Profit"} <= visuals["bar1"]


def test_pbir_visual_level_filter_counts_as_usage():
    """bar1's filterConfig references 'Order Count' -- nothing else in the
    fixture uses it. Missing this would be exactly the false-unused bug
    CONTRACT.md calls out."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    visuals = {v.id: set(v.metric_names) for v in estate.assets[0].visuals}
    assert "Order Count" in visuals["bar1"]


def test_pbir_page_level_filter_counts_as_usage():
    """Summary/page.json's filterConfig references 'Cost Total', which
    appears nowhere else in the fixture."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert "Cost Total" in estate.assets[0].referenced_metric_names()


def test_pbir_report_level_filter_counts_as_usage():
    """definition/report.json's filterConfig references 'Gross Margin',
    including once through a query alias (Source: 's') that must resolve
    back to the Sales entity via the filter's own From clause."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert "Gross Margin" in estate.assets[0].referenced_metric_names()


def test_pbir_bookmark_filter_counts_as_usage():
    """bm1.bookmark.json's explorationState references 'Bookmarked
    Measure', which appears nowhere else -- only reachable via bookmarks."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert "Bookmarked Measure" in estate.assets[0].referenced_metric_names()


def test_pbir_report_extension_dax_expression_counts_as_usage():
    """reportExtensions.json defines a report-level measure 'Margin %' whose
    DAX body is `DIVIDE([Gross Margin], [Revenue])` -- both bracketed refs
    must be picked up even though they only ever appear inside a DAX
    *string*, not a structured field container."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    names = estate.assets[0].referenced_metric_names()
    assert {"Gross Margin", "Revenue"} <= names


def test_pbir_report_level_measure_name_itself_is_tracked_separately():
    """'Margin %' is a name the *report* defines, not a model measure
    reference -- it must not be silently conflated with the DAX refs it
    contains, and must be recorded so a consumer can tell the two apart."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert estate.assets[0].extra.get("report_level_measures") == ["Margin %"]


def test_pbir_native_visual_calculation_dax_reference_counts_as_usage():
    """table1's NativeVisualCalculation has Expression
    'RUNNINGSUM ( [Units Sold] )' -- a bare bracket reference inside inline
    DAX, not a structured field container, and 'Units Sold' appears nowhere
    else in the fixture."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    visuals = {v.id: set(v.metric_names) for v in estate.assets[0].visuals}
    assert "Units Sold" in visuals["table1"]


def test_pbir_aggregated_column_reference():
    """table1's Values projection wraps a Column in an Aggregation
    (queryRef 'Sum(Sales.Amount)'); the column name must be picked up, and
    the synthesised nativeQueryRef ('Sum of Amount') must NOT be invented as
    a spurious name."""
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    visuals = {v.id: set(v.metric_names) for v in estate.assets[0].visuals}
    assert "Amount" in visuals["table1"]
    assert "Sum of Amount" not in visuals["table1"]


def test_pbir_hierarchy_level_reference():
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    visuals = {v.id: set(v.metric_names) for v in estate.assets[0].visuals}
    assert "Year" in visuals["table1"]


def test_pbir_mobile_json_is_scanned_without_error():
    # table1/mobile.json exists in the fixture and carries no references of
    # its own; it must be read without raising or adding spurious errors.
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    assert estate.errors == []


def test_pbir_pseudo_visuals_are_excluded_from_page_list_but_carry_names():
    estate = Estate()
    parse_report(PBIR_REPORT_DIR, estate)
    asset = estate.assets[0]
    pseudo = [v for v in asset.visuals if v.visual_type in PSEUDO_VISUAL_TYPES]
    assert pseudo  # at least the report-level filter/extension/bookmark ones
    assert all(v.metric_names for v in pseudo)  # only emitted when non-empty


# ---------------------------------------------------------------------------
# malformed input: never raise, degrade gracefully
# ---------------------------------------------------------------------------


def test_parse_report_on_missing_folder_records_error():
    estate = Estate()
    parse_report("/definitely/does/not/exist.Report", estate)
    assert estate.assets == []
    assert any("not a recognised Power BI report folder" in e for e in estate.errors)


def test_legacy_sections_not_a_list(tmp_path):
    report_dir = tmp_path / "Bad.Report"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps({"sections": "nope"}))
    estate = Estate()
    parse_report(str(report_dir), estate)  # must not raise
    assert len(estate.assets) == 1
    assert any("no 'sections' array" in e for e in estate.errors)


def test_legacy_section_not_an_object(tmp_path):
    report_dir = tmp_path / "Bad.Report"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps({"sections": ["not-an-object"]}))
    estate = Estate()
    parse_report(str(report_dir), estate)
    assert any("is not an object" in e for e in estate.errors)


def test_legacy_visual_container_config_invalid_json_string(tmp_path):
    report_dir = tmp_path / "Bad.Report"
    report_dir.mkdir()
    payload = {
        "sections": [{"name": "S", "visualContainers": [{"config": "{not valid json"}]}]
    }
    (report_dir / "report.json").write_text(json.dumps(payload))
    estate = Estate()
    parse_report(str(report_dir), estate)  # must not raise
    assert any("embedded JSON string did not parse" in e for e in estate.errors)


def test_legacy_top_level_not_an_object(tmp_path):
    report_dir = tmp_path / "Bad.Report"
    report_dir.mkdir()
    (report_dir / "report.json").write_text("[1, 2, 3]")
    estate = Estate()
    parse_report(str(report_dir), estate)
    assert any("top level is not a JSON object" in e for e in estate.errors)


def test_pbir_missing_pages_folder_records_error(tmp_path):
    report_dir = tmp_path / "Bad.Report"
    definition = report_dir / "definition"
    definition.mkdir(parents=True)
    (definition / "report.json").write_text("{}")
    # No `pages/` directory at all, but the folder still looks PBIR-shaped
    # because `definition/` exists -- degrade gracefully, don't raise, and
    # still produce the asset (with zero pages) rather than nothing at all.
    estate = Estate()
    parse_report(str(report_dir), estate)
    assert len(estate.assets) == 1
    assert estate.assets[0].visuals == []
    assert any("missing pages folder" in e for e in estate.errors)


def test_unrecognised_report_folder_shape(tmp_path):
    report_dir = tmp_path / "NotAReport.Report"
    report_dir.mkdir()
    (report_dir / "readme.txt").write_text("nothing useful here")
    estate = Estate()
    parse_report(str(report_dir), estate)
    assert estate.assets == []
    assert any("not a recognised Power BI report folder" in e for e in estate.errors)

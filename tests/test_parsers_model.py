"""Tests for `parsers/tmdl.py` (TMDL) and `parsers/tmsl.py` (model.bim / TMSL).

The TMDL half is exercised against the real, frozen fixture
(`tests/fixtures/SalesDemo.SemanticModel/definition/Sales.tmdl`) wherever
possible, rather than a hand-rolled copy, so a regression here means the
actual fixture stopped parsing the way CONTRACT.md says it must. Synthetic
`.tmdl` text (via `tmp_path`) is used only for behaviour the fixture does not
exercise: malformed input, and the backtick-fenced multi-line form.
"""

from __future__ import annotations

import json

from conftest import SALES_MODEL_DIR, load_estate, metric_by_name
from dustpan.dax.normalise import enrich
from dustpan.ir import Estate
from dustpan.parsers.discover import model_name_from_path
from dustpan.parsers.tmdl import parse_model
from dustpan.parsers.tmsl import parse_model_bim

# ---------------------------------------------------------------------------
# TMDL: the real fixture
# ---------------------------------------------------------------------------


def test_parses_all_seven_planted_measures():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    names = {m.name for m in estate.metrics}
    assert names == {
        "Total Sales",
        "Sales Total",
        "Sum of Sales",
        "Total Sales UK",
        "Revenue",
        "Order Count",
        "Orphan Measure",
    }
    assert estate.errors == []


def test_every_metric_has_correct_identity_and_dialect():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    for m in estate.metrics:
        assert m.tool == "powerbi"
        assert m.model == "SalesDemo"
        assert m.dialect == "dax"
        assert m.id == f"powerbi:SalesDemo:{m.name}"
        assert m.source_path.endswith("Sales.tmdl")
        assert m.extra.get("table") == "Sales"


def test_single_line_measure_fields():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    total_sales = metric_by_name(estate, "Total Sales")
    assert total_sales.expression == "SUM ( Sales[Amount] )"
    assert total_sales.format_string == "#,0.00"
    assert total_sales.display_folder == "Core"
    assert total_sales.is_hidden is False
    assert total_sales.description is None


def test_multiline_measure_expression_and_description():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    sum_of_sales = metric_by_name(estate, "Sum of Sales")
    assert "SUM" in sum_of_sales.expression
    assert "Amount" in sum_of_sales.expression
    assert sum_of_sales.display_folder == "Legacy"
    # The /// comment directly above the measure is its description.
    assert sum_of_sales.description == "Duplicate created during the 2024 migration"


def test_multiline_measure_normalises_identically_to_single_line_siblings():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    for m in estate.metrics:
        enrich(m)
    trio = [
        metric_by_name(estate, n) for n in ("Total Sales", "Sales Total", "Sum of Sales")
    ]
    assert all(m.lexically_ok for m in trio)
    assert len({m.fingerprint for m in trio}) == 1


def test_adversarial_measures_are_present_with_distinct_expressions():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    uk = metric_by_name(estate, "Total Sales UK")
    revenue = metric_by_name(estate, "Revenue")
    assert "CALCULATE" in uk.expression.upper()
    assert "SUMX" in revenue.expression.upper()


def test_orphan_measure_parses_like_any_other_measure():
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    orphan = metric_by_name(estate, "Orphan Measure")
    assert orphan.expression == "DISTINCTCOUNT ( Sales[OrderId] )"
    assert orphan.display_folder == "Unused"


def test_model_name_is_derived_from_folder_not_file_contents():
    assert model_name_from_path(SALES_MODEL_DIR) == "SalesDemo"


def test_model_tmdl_contributes_no_spurious_measures():
    # model.tmdl declares `ref table Sales` with no measures of its own; make
    # sure walking every .tmdl file doesn't double-count or invent anything.
    estate = Estate()
    parse_model(SALES_MODEL_DIR, estate)
    assert len(estate.metrics) == 7


# ---------------------------------------------------------------------------
# TMDL: malformed input, never raises
# ---------------------------------------------------------------------------


def test_parse_model_on_missing_directory_records_error_not_exception():
    estate = Estate()
    parse_model("/definitely/does/not/exist", estate)
    assert estate.metrics == []
    assert any("not a directory" in e for e in estate.errors)


def test_parse_model_on_directory_with_no_tmdl_files(tmp_path):
    empty = tmp_path / "Empty.SemanticModel" / "definition"
    empty.mkdir(parents=True)
    estate = Estate()
    parse_model(str(empty), estate)
    assert estate.metrics == []
    assert any("no .tmdl files" in e for e in estate.errors)


def test_measure_with_no_name_is_skipped_not_crashed(tmp_path):
    model_dir = tmp_path / "Bad.SemanticModel" / "definition"
    model_dir.mkdir(parents=True)
    (model_dir / "Bad.tmdl").write_text(
        "table T\n\n\tmeasure = SUM ( T[Amount] )\n\n\tmeasure 'Good' = SUM ( T[Amount] )\n"
    )
    estate = Estate()
    parse_model(str(model_dir), estate)
    names = {m.name for m in estate.metrics}
    assert names == {"Good"}
    assert any("no readable name" in e for e in estate.errors)


def test_non_utf8_file_records_error_not_exception(tmp_path):
    model_dir = tmp_path / "Bin.SemanticModel" / "definition"
    model_dir.mkdir(parents=True)
    (model_dir / "Bin.tmdl").write_bytes(
        b"\xff\xfe\x00\xff not valid utf-8 or utf-8-sig \xff"
    )
    estate = Estate()
    parse_model(str(model_dir), estate)  # must not raise
    assert estate.metrics == []
    assert any("not valid UTF-8" in e for e in estate.errors)


def test_space_indented_measure_is_not_falsely_attributed(tmp_path):
    """TMDL indentation is tabs; a file that uses spaces must not be
    mis-nested into the wrong object. The safe failure is 'not found', never
    'attributed to the wrong table'."""
    model_dir = tmp_path / "Spaces.SemanticModel" / "definition"
    model_dir.mkdir(parents=True)
    (model_dir / "S.tmdl").write_text("table T\n\n    measure 'X' = SUM ( T[Amount] )\n")
    estate = Estate()
    parse_model(str(model_dir), estate)
    assert estate.metrics == []  # not found is safe; misattributed would not be


# ---------------------------------------------------------------------------
# TMDL: backtick-fenced multi-line measures (a real TMDL feature, confirmed
# against Microsoft Learn: "to enforce a different indentation or to
# preserve trailing blank lines or whitespaces, use the three backticks
# enclosing")
# ---------------------------------------------------------------------------


def test_backtick_fenced_measure_is_parsed_verbatim(tmp_path):
    model_dir = tmp_path / "Fence.SemanticModel" / "definition"
    model_dir.mkdir(parents=True)
    (model_dir / "F.tmdl").write_text(
        "table T\n"
        "\n"
        "\tmeasure 'Fenced' = ```\n"
        "\t\t\tSUMX(\n"
        "\n"
        "T,\n"
        "\t\t\tT[Amount]\n"
        "\t\t\t)\n"
        "\t\t```\n"
        "\t\tformatString: 0\n"
        "\n"
        "\tmeasure 'After' = SUM ( T[Amount] )\n"
    )
    estate = Estate()
    parse_model(str(model_dir), estate)
    assert estate.errors == []
    fenced = metric_by_name(estate, "Fenced")
    after = metric_by_name(estate, "After")
    assert fenced.format_string == "0"
    enrich(fenced)
    enrich(after)
    assert fenced.lexically_ok
    assert fenced.normalised == "SUMX(T, T[AMOUNT])"
    # The measure that follows a fenced one must parse normally -- the fence
    # must not swallow anything past its own closing ```.
    assert after.lexically_ok
    assert after.normalised == "SUM(T[AMOUNT])"


def test_unterminated_fence_reports_error_and_does_not_raise(tmp_path):
    model_dir = tmp_path / "BadFence.SemanticModel" / "definition"
    model_dir.mkdir(parents=True)
    (model_dir / "F.tmdl").write_text(
        "table T\n\n\tmeasure 'Bad' = ```\n\t\t\tSUM(T[Amount])\n"
    )
    estate = Estate()
    parse_model(str(model_dir), estate)
    assert any("fence" in e and "never closed" in e for e in estate.errors)


# ---------------------------------------------------------------------------
# TMSL (model.bim)
# ---------------------------------------------------------------------------


def _write_bim(tmp_path, payload, folder_name="BimDemo.SemanticModel"):
    model_dir = tmp_path / folder_name
    model_dir.mkdir(parents=True)
    path = model_dir / "model.bim"
    path.write_text(json.dumps(payload))
    return str(path)


def test_tmsl_basic_measure_fields(tmp_path):
    path = _write_bim(
        tmp_path,
        {
            "model": {
                "tables": [
                    {
                        "name": "Sales",
                        "measures": [
                            {
                                "name": "Total Sales",
                                "expression": "SUM(Sales[Amount])",
                                "formatString": "#,0.00",
                                "displayFolder": "Core",
                                "description": "the total",
                                "isHidden": False,
                            }
                        ],
                    }
                ]
            }
        },
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert estate.errors == []
    assert len(estate.metrics) == 1
    m = estate.metrics[0]
    assert m.name == "Total Sales"
    assert m.expression == "SUM(Sales[Amount])"
    assert m.format_string == "#,0.00"
    assert m.display_folder == "Core"
    assert m.description == "the total"
    assert m.is_hidden is False
    assert m.dialect == "dax"
    assert m.extra.get("table") == "Sales"


def test_tmsl_expression_as_list_of_lines_joins_with_newlines_and_matches_string_form(
    tmp_path,
):
    path = _write_bim(
        tmp_path,
        {
            "model": {
                "tables": [
                    {
                        "name": "Sales",
                        "measures": [
                            {"name": "OneLine", "expression": "SUM(Sales[Amount])"},
                            {
                                "name": "MultiLine",
                                "expression": ["SUM (", "    Sales[Amount]", ")"],
                            },
                        ],
                    }
                ]
            }
        },
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert estate.errors == []
    by_name = {m.name: m for m in estate.metrics}
    assert by_name["MultiLine"].expression == "SUM (\n    Sales[Amount]\n)"
    for m in estate.metrics:
        enrich(m)
    assert by_name["OneLine"].fingerprint == by_name["MultiLine"].fingerprint


def test_tmsl_ishidden_true(tmp_path):
    path = _write_bim(
        tmp_path,
        {
            "model": {
                "tables": [
                    {
                        "name": "Sales",
                        "measures": [
                            {"name": "Hidden", "expression": "1", "isHidden": True},
                        ],
                    }
                ]
            }
        },
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert estate.metrics[0].is_hidden is True


def test_tmsl_model_name_from_folder_not_json_internal_name(tmp_path):
    path = _write_bim(
        tmp_path,
        {
            "name": "SomeInternalTabularEditorName",
            "model": {
                "tables": [{"name": "T", "measures": [{"name": "X", "expression": "1"}]}]
            },
        },
        folder_name="RealFolderName.SemanticModel",
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert estate.metrics[0].model == "RealFolderName"
    assert estate.metrics[0].id == "powerbi:RealFolderName:X"


def test_tmsl_missing_expression_records_error_but_still_creates_metric(tmp_path):
    """A measure dustpan could not read an expression for is not invented as
    empty-and-silent -- it is recorded so `dax.normalise` honestly flags it
    unparsed, and the read failure itself lands in estate.errors."""
    path = _write_bim(
        tmp_path,
        {"model": {"tables": [{"name": "T", "measures": [{"name": "NoExpr"}]}]}},
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert len(estate.metrics) == 1
    assert estate.metrics[0].expression == ""
    assert any("missing" in e and "expression" in e for e in estate.errors)
    enrich(estate.metrics[0])
    assert estate.metrics[0].lexically_ok is False


def test_tmsl_measure_with_no_name_is_skipped(tmp_path):
    path = _write_bim(
        tmp_path,
        {
            "model": {
                "tables": [
                    {
                        "name": "T",
                        "measures": [
                            {"expression": "1"},
                            {"name": "Good", "expression": "1"},
                        ],
                    }
                ]
            }
        },
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert [m.name for m in estate.metrics] == ["Good"]
    assert any("no valid" in e for e in estate.errors)


def test_tmsl_invalid_json_records_error_not_exception(tmp_path):
    path = tmp_path / "bad.bim"
    path.write_text("{not valid json at all")
    estate = Estate()
    parse_model_bim(str(path), estate)
    assert estate.metrics == []
    assert any("unreadable JSON" in e for e in estate.errors)


def test_tmsl_missing_file_records_error_not_exception(tmp_path):
    estate = Estate()
    parse_model_bim(str(tmp_path / "nope.bim"), estate)
    assert estate.metrics == []
    assert any("not a file" in e for e in estate.errors)


def test_tmsl_top_level_not_an_object(tmp_path):
    path = tmp_path / "list.bim"
    path.write_text("[1, 2, 3]")
    estate = Estate()
    parse_model_bim(str(path), estate)
    assert estate.metrics == []
    assert any("not a JSON object" in e for e in estate.errors)


def test_tmsl_measures_not_a_list_skips_table_not_whole_file(tmp_path):
    path = _write_bim(
        tmp_path,
        {
            "model": {
                "tables": [
                    {"name": "Bad", "measures": "not a list"},
                    {"name": "Good", "measures": [{"name": "X", "expression": "1"}]},
                ]
            }
        },
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert [m.name for m in estate.metrics] == ["X"]
    assert any("not a list" in e for e in estate.errors)


def test_tmsl_expression_wrong_type_is_recorded(tmp_path):
    path = _write_bim(
        tmp_path,
        {
            "model": {
                "tables": [{"name": "T", "measures": [{"name": "X", "expression": 42}]}]
            }
        },
    )
    estate = Estate()
    parse_model_bim(path, estate)
    assert estate.metrics[0].expression == ""
    assert any("unexpected type" in e for e in estate.errors)


def test_tmsl_bare_model_document_without_outer_wrapper(tmp_path):
    """Tolerate a `model.bim` that is the model object itself, with no
    outer name/compatibilityLevel wrapper -- both shapes appear in the wild."""
    path = tmp_path / "Bare.SemanticModel" / "model.bim"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"tables": [{"name": "T", "measures": [{"name": "X", "expression": "1"}]}]}
        )
    )
    estate = Estate()
    parse_model_bim(str(path), estate)
    assert [m.name for m in estate.metrics] == ["X"]


# ---------------------------------------------------------------------------
# discover() picks the right one when both TMDL and TMSL are technically
# reachable, and never double-parses a model
# ---------------------------------------------------------------------------


def test_full_pipeline_style_scan_of_sales_model_yields_seven_unique_metrics():
    estate = load_estate(model=True, report=False)
    assert len(estate.metrics) == 7
    assert len({m.id for m in estate.metrics}) == 7  # every id unique


def test_two_projects_with_the_same_model_name_stay_separate(tmp_path):
    """AUD-003 / AUD-008. `Sales`, `Finance` and `Executive` are common enough
    that two unrelated projects sharing a model name is routine, and folder
    basenames alone cannot tell them apart. Before this fix both produced the
    id `powerbi:SalesDemo:Total Sales`, `metric_by_id()` silently kept one,
    and usage from one tenant's report counted towards the other tenant's
    measure -- which decides which duplicate survives a retirement."""
    import os
    import shutil

    from dustpan import pipeline

    source = os.path.join(os.path.dirname(__file__), "fixtures")
    for tenant in ("tenantA", "tenantB"):
        for part in ("SalesDemo.SemanticModel", "SalesDemo.Report"):
            shutil.copytree(os.path.join(source, part), tmp_path / tenant / part)

    estate = pipeline.scan(str(tmp_path))

    ids = [m.id for m in estate.metrics]
    assert len(set(ids)) == len(ids), "every metric needs its own id"
    assert estate.id_collisions() == {}
    assert len(estate.metric_by_id()) == len(estate.metrics), (
        "metric_by_id() must not silently drop a colliding measure"
    )
    assert len({m.source_root for m in estate.metrics}) == 2

    # The clash is reported, not silently papered over.
    assert any("separate deployments define" in e for e in estate.errors)

    # Usage is scoped: each tenant's report counts only for its own measures.
    from dustpan.detect.unused import build_asset_index

    index = build_asset_index(estate)
    for metric in (m for m in estate.metrics if m.name == "Total Sales"):
        assert index.visual_uses(metric.id) == 1

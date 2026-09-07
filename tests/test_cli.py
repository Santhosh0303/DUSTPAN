"""Tests for `cli.py`, and (via the CLI's own output paths) `output/console.py`
and `output/writers.py`.

Exit codes are part of the contract (see CONTRACT.md and `cli.py`'s
docstring): 0 clean, 1 findings-with-`--fail-on-findings`, 2 bad usage or an
output file that could not be written. These tests pin that contract down
argument by argument.
"""

from __future__ import annotations

import json

import pytest

from conftest import DUPLICATE_TRIO, FIXTURES_DIR, ORPHAN
from dustpan import cli, pipeline
from dustpan.ir import Estate
from dustpan.output import console, writers


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# argument parsing / usage errors
# ---------------------------------------------------------------------------


def test_no_arguments_prints_help_and_exits_usage(capsys):
    code, out, err = run([], capsys)
    assert code == cli.EXIT_USAGE
    assert "usage" in out.lower()


def test_unknown_command_falls_through_to_help(capsys):
    code, out, err = run(["frobnicate"], capsys)
    assert code == cli.EXIT_USAGE


def test_scan_missing_path_exits_usage(capsys):
    code, out, err = run(["scan", "/definitely/does/not/exist"], capsys)
    assert code == cli.EXIT_USAGE
    assert "no such path" in err


def test_scan_path_is_a_file_not_a_folder_exits_usage(capsys, tmp_path):
    f = tmp_path / "not_a_folder.txt"
    f.write_text("hello")
    code, out, err = run(["scan", str(f)], capsys)
    assert code == cli.EXIT_USAGE
    assert "folder" in err.lower()


@pytest.mark.parametrize("bad_value", ["abc", "-0.1", "1.1", "2"])
def test_min_confidence_rejects_out_of_range_or_non_numeric(capsys, bad_value):
    code, out, err = run(["scan", FIXTURES_DIR, "--min-confidence", bad_value], capsys)
    assert code == cli.EXIT_USAGE


@pytest.mark.parametrize("good_value", ["0", "1", "0.5", "1.0", "0.0"])
def test_min_confidence_accepts_valid_range(capsys, good_value):
    code, out, err = run(
        ["scan", FIXTURES_DIR, "--min-confidence", good_value, "--quiet"], capsys
    )
    assert code in (cli.EXIT_OK, cli.EXIT_FINDINGS)


def test_version_command(capsys):
    code, out, err = run(["version"], capsys)
    assert code == cli.EXIT_OK
    assert "dustpan" in out
    assert pipeline.version() in out


# ---------------------------------------------------------------------------
# exit codes for a real scan
# ---------------------------------------------------------------------------


def test_scan_exits_clean_without_fail_on_findings_flag_even_with_findings(capsys):
    """Findings alone must never fail the run -- only --fail-on-findings does."""
    code, out, err = run(["scan", FIXTURES_DIR], capsys)
    assert code == cli.EXIT_OK
    assert "HIGH" in out  # there really are high-severity findings in the fixture


def test_scan_fail_on_findings_exits_1_when_high_severity_present(capsys):
    code, out, err = run(["scan", FIXTURES_DIR, "--fail-on-findings"], capsys)
    assert code == cli.EXIT_FINDINGS
    assert "high-severity" in err


def test_scan_fail_on_findings_exits_0_when_filtered_below_high(capsys, tmp_path):
    """Point at a folder with nothing at all -- no findings, so
    --fail-on-findings has nothing to fail on."""
    empty = tmp_path / "Empty"
    empty.mkdir()
    code, out, err = run(["scan", str(empty), "--fail-on-findings"], capsys)
    assert code == cli.EXIT_OK


def test_quiet_suppresses_all_stdout(capsys):
    code, out, err = run(["scan", FIXTURES_DIR, "--quiet"], capsys)
    assert code == cli.EXIT_OK
    assert out == ""


def test_min_confidence_1_0_hides_the_unused_candidates(capsys):
    code, out, err = run(["scan", FIXTURES_DIR, "--min-confidence", "1.0"], capsys)
    assert "UNUSED MEASURE CANDIDATES" not in out
    assert (
        "hidden" in out
    )  # tells the user findings were filtered, doesn't just drop them silently


# ---------------------------------------------------------------------------
# file outputs
# ---------------------------------------------------------------------------


def test_json_output_is_valid_and_complete(capsys, tmp_path):
    out_path = tmp_path / "estate.json"
    code, out, err = run(
        ["scan", FIXTURES_DIR, "--json", str(out_path), "--quiet"], capsys
    )
    assert code == cli.EXIT_OK
    data = json.loads(out_path.read_text())
    assert len(data["metrics"]) == 7
    assert any(f["kind"] == "exact_duplicate" for f in data["findings"])
    assert any(f["kind"] == "unused_measure" for f in data["findings"])


def test_json_output_summary_splits_proven_removable_from_review_candidates(
    capsys, tmp_path
):
    """FIX 2: the two headline numbers must never merge -- and machine
    readers get the same split the console/markdown reports show, via a
    `summary` block built from `pipeline.summarise`."""
    out_path = tmp_path / "estate.json"
    code, out, err = run(
        ["scan", FIXTURES_DIR, "--json", str(out_path), "--quiet"], capsys
    )
    assert code == cli.EXIT_OK
    summary = json.loads(out_path.read_text())["summary"]
    assert summary["proven_removable"] >= 1
    assert summary["review_candidates"] >= 1
    assert isinstance(summary["proven_removable_ids"], list)
    assert isinstance(summary["review_candidate_ids"], list)
    assert set(summary["proven_removable_ids"]).isdisjoint(
        summary["review_candidate_ids"]
    )
    # The old blended stat must be gone, not just renamed.
    assert "removable" not in summary
    assert "removable_ids" not in summary
    assert "removable_pct" not in summary


def test_markdown_output_mentions_the_planted_duplicate_names(capsys, tmp_path):
    out_path = tmp_path / "report.md"
    code, out, err = run(
        ["scan", FIXTURES_DIR, "--markdown", str(out_path), "--quiet"], capsys
    )
    assert code == cli.EXIT_OK
    text = out_path.read_text()
    assert text.startswith("# dustpan scan report")
    for name in DUPLICATE_TRIO:
        assert name in text
    assert ORPHAN in text


def test_markdown_summary_splits_proven_removable_from_review_candidates(
    capsys, tmp_path
):
    out_path = tmp_path / "report.md"
    code, out, err = run(
        ["scan", FIXTURES_DIR, "--markdown", str(out_path), "--quiet"], capsys
    )
    assert code == cli.EXIT_OK
    text = out_path.read_text()
    assert "Measures proven removable" in text
    assert "Candidates for review" in text
    # No single blended "N measures removable (X% of the estate)" line.
    assert "Measures removable" not in text


def test_stdout_reports_wrote_lines_for_each_output_file(capsys, tmp_path):
    jpath = tmp_path / "e.json"
    mpath = tmp_path / "r.md"
    code, out, err = run(
        ["scan", FIXTURES_DIR, "--json", str(jpath), "--markdown", str(mpath)], capsys
    )
    assert f"wrote {jpath}" in out
    assert f"wrote {mpath}" in out


def test_unwritable_output_path_exits_usage_with_message(capsys, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad_path = blocker / "sub" / "out.json"
    code, out, err = run(["scan", FIXTURES_DIR, "--json", str(bad_path)], capsys)
    assert code == cli.EXIT_USAGE
    assert "could not write" in err


# ---------------------------------------------------------------------------
# colour handling
# ---------------------------------------------------------------------------


def test_no_color_flag_and_env_var_both_suppress_ansi_escapes(capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    code, out, err = run(["scan", FIXTURES_DIR, "--no-color"], capsys)
    assert "\x1b[" not in out

    monkeypatch.setenv("NO_COLOR", "1")
    code, out, err = run(["scan", FIXTURES_DIR], capsys)
    assert "\x1b[" not in out


def test_console_render_can_force_colour_on_directly(sales_estate):
    # capsys/pytest stdout is never a real tty, so exercising forced colour
    # has to go around the CLI and call the renderer directly.
    coloured = console.render(sales_estate, color=True)
    plain = console.render(sales_estate, color=False)
    assert "\x1b[" in coloured
    assert "\x1b[" not in plain
    # Stripping the escapes should leave the same information behind.
    import re

    stripped = re.sub(r"\x1b\[[0-9;]*m", "", coloured)
    assert stripped == plain


# ---------------------------------------------------------------------------
# never crashes on an empty / nonsense estate
# ---------------------------------------------------------------------------


def test_render_on_a_completely_empty_estate_does_not_raise():
    text = console.render(Estate(), color=False)
    assert "No measures found" in text


def test_console_summary_splits_proven_removable_from_review_candidates(capsys):
    """FIX 2: two numbers that never merge into one blended headline stat."""
    code, out, err = run(["scan", FIXTURES_DIR], capsys)
    assert code == cli.EXIT_OK
    assert "proven removable" in out
    assert "for review" in out
    assert "of the estate" not in out  # the old single blended percentage phrase


def test_console_metrics_table_shows_aligned_columns_not_raw_dict_repr(sales_estate):
    """FIX 3: evidence['metrics'] used to be dumped as a raw Python dict
    repr, wrapped mid-token. It must now render as a small aligned table,
    and never leak a dict repr into the console output."""
    from dustpan.detect.duplicates import find_duplicates

    sales_estate.findings = find_duplicates(sales_estate)
    text = console.render(sales_estate, color=False)
    assert "measure" in text
    assert "folder" in text
    assert "hidden" in text
    assert "source" in text
    assert "{'id':" not in text
    assert "'fingerprint':" not in text
    assert "asset_references" not in text  # raw key name never leaks into prose


def test_markdown_evidence_never_dumps_raw_dict_repr(sales_estate):
    from dustpan.detect.duplicates import find_duplicates

    sales_estate.findings = find_duplicates(sales_estate)
    text = writers.render_markdown(sales_estate)
    assert "{'id':" not in text
    assert "'fingerprint':" not in text
    assert "Hidden" in text  # the extended per-metric table column


def test_cross_model_duplicate_is_never_rendered_as_a_delete_instruction():
    """A shared formula across two unrelated semantic models must read as
    an observation, not as 'keep A, retire B' -- retiring B would not
    retire whatever *other* model still needs a measure by that name."""
    from conftest import make_metric
    from dustpan.detect.duplicates import find_duplicates
    from dustpan.output import writers

    a = make_metric("A", "SUM(Sales[Amount])", model="ModelOne", id="powerbi:ModelOne:A")
    b = make_metric("B", "SUM(Sales[Amount])", model="ModelTwo", id="powerbi:ModelTwo:B")
    estate = Estate(metrics=[a, b])
    estate.findings = find_duplicates(estate)

    console_text = console.render(estate, color=False)
    assert "Action: keep" not in console_text
    assert "different semantic models" in console_text

    md_text = writers.render_markdown(estate)
    assert "**Action:**" not in md_text
    assert "different semantic models" in md_text


def test_write_json_and_markdown_on_empty_estate(tmp_path):
    estate = Estate()
    jpath = tmp_path / "e.json"
    mpath = tmp_path / "r.md"
    writers.write_json(estate, str(jpath))
    writers.write_markdown(estate, str(mpath))
    assert json.loads(jpath.read_text())["metrics"] == []
    assert mpath.read_text().startswith("# dustpan scan report")


def test_writers_create_missing_parent_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "out.json"
    writers.write_json(Estate(), str(nested))
    assert nested.exists()


# ---------------------------------------------------------------------------
# build_parser() structure
# ---------------------------------------------------------------------------


def test_build_parser_scan_requires_a_path():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scan"])


def test_build_parser_accepts_all_documented_flags():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "scan",
            "somepath",
            "--json",
            "a.json",
            "--markdown",
            "a.md",
            "--quiet",
            "--min-confidence",
            "0.5",
            "--fail-on-findings",
            "--no-color",
        ]
    )
    assert args.path == "somepath"
    assert args.json_path == "a.json"
    assert args.markdown_path == "a.md"
    assert args.quiet is True
    assert args.min_confidence == 0.5
    assert args.fail_on_findings is True
    assert args.no_color is True

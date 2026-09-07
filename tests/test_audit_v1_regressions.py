"""Regression tests for the findings of the v1 adversarial audit.

Each test names the finding it locks down and states the failure mode in
terms of what a user would lose, not in terms of the code shape -- a test
that only asserts today's implementation stops protecting anything the
moment the implementation is rewritten.
"""

from __future__ import annotations

import json
import os
import re
import shutil

import pytest

from dustpan import pipeline
from dustpan.dax.normalise import enrich
from dustpan.detect.duplicates import find_duplicates
from dustpan.ir import Asset, Estate, Metric, Visual
from dustpan.output import console, writers

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _metric(name, *, mid=None, root="/proj", fmt="#,0.00", dtype="double", extra=None):
    metric = Metric(
        id=mid or f"powerbi:M:{name}",
        name=name,
        tool="powerbi",
        model="M",
        source_path=f"{root}/model.tmdl",
        source_root=root,
        expression="SUM(Sales[Amount])",
        dialect="dax",
        format_string=fmt,
        data_type=dtype,
        extra=extra or {},
    )
    enrich(metric)
    return metric


def _estate(metrics, *, with_usage=True, asset_root="/proj"):
    estate = Estate(metrics=list(metrics))
    if with_usage:
        estate.assets.append(
            Asset(
                id="a",
                name="r",
                tool="powerbi",
                source_path="r.json",
                source_root=asset_root,
                visuals=[
                    Visual(
                        id="v",
                        page="p",
                        visual_type="card",
                        metric_names=[metrics[0].name],
                    )
                ],
            )
        )
    estate.findings = find_duplicates(estate)
    pipeline.apply_actionability(estate)
    return estate


# --------------------------------------------------------------------------
# AUD-V1-001 / 002 / 003 -- one authority for actionability
# --------------------------------------------------------------------------


def test_blocked_duplicate_never_renders_a_retirement_action():
    """AUD-V1-001. The detector computed a blocker and the renderers ignored
    it, printing 'Action: keep A, retire B' for a set it had already decided
    was not interchangeable. Whatever the blocker, no output may instruct."""
    estate = _estate([_metric("A"), _metric("B", fmt="0.0%")])
    finding = next(f for f in estate.findings if f.kind == "exact_duplicate")

    assert finding.evidence["retirement_cleared"] is False
    assert "keep" not in finding.evidence
    assert "retire" not in finding.evidence
    assert "Action: keep" not in console.render(estate, color=False)
    assert "**Action:**" not in writers.render_markdown(estate)
    assert pipeline.proven_removable_metric_ids(estate) == set()


def test_two_deployments_sharing_a_model_name_are_never_removable():
    """AUD-V1-002. Disambiguating ids emptied `id_collisions()`, which had
    been the only guard catching same-basename deployments, while removability
    still compared model BASENAMES. Scope is the deployment, always."""
    estate = _estate(
        [
            _metric("Total", mid="powerbi:M:Total", root="/tenantA"),
            _metric("Total", mid="powerbi:M:Total#2", root="/tenantB"),
        ],
        asset_root="/tenantA",
    )
    assert estate.id_collisions() == {}, "ids are unique -- the old guard cannot fire"
    assert len({m.deployment for m in estate.metrics}) == 2
    assert pipeline.proven_removable_metric_ids(estate) == set()


def test_material_scan_note_withdraws_every_proven_removal():
    """AUD-V1-003. A duplicate stayed 'proven removable' while a report the
    scan could not read sat unmentioned in the same estate. Incomplete usage
    evidence cannot authorise a deletion."""
    estate = _estate([_metric("A"), _metric("B")])
    assert pipeline.proven_removable_metric_ids(estate), "control: clean estate removes"

    estate.errors.append("report: /x/y.json: could not read file: denied")
    pipeline._reconcile_notes(estate)
    pipeline.apply_actionability(estate)

    assert estate.material_notes()
    assert pipeline.proven_removable_metric_ids(estate) == set()


# --------------------------------------------------------------------------
# AUD-V1-004 / 006 -- reads are recorded, and contained
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "SalesDemo.SemanticModel/definition/model.tmdl",
        "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json",
    ],
)
def test_scanned_files_that_produce_no_metric_are_still_unwritable(tmp_path, relative):
    """AUD-V1-004. The overwrite guard was inferred from Metric/Asset
    source_path, so a structural model.tmdl (no measures) and a nested PBIR
    file (not the asset's own path) were both truncated by --force."""
    from dustpan.cli import main

    shutil.copytree(FIXTURES, tmp_path / "fx")
    target = tmp_path / "fx" / relative
    before = target.read_bytes()

    exit_code = main(["scan", str(tmp_path / "fx"), "--json", str(target), "--force"])

    assert exit_code != 0
    assert target.read_bytes() == before


def test_nested_report_symlink_cannot_read_outside_the_scan_root(tmp_path):
    """AUD-V1-006. Containment was checked on discovered top-level targets
    only; PBIR opens a tree of nested files afterwards, and a symlinked
    visual.json pulled outside content into the estate and into exports."""
    shutil.copytree(FIXTURES, tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    leak = outside / "leak.json"
    leak.write_text(
        json.dumps(
            {
                "name": "leak",
                "visual": {
                    "visualType": "card",
                    "query": {
                        "queryState": {
                            "Values": {
                                "projections": [{"queryRef": "Secret.Leaked Outside"}]
                            }
                        }
                    },
                },
            }
        )
    )
    victim = (
        tmp_path
        / "root"
        / "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
    )
    victim.unlink()
    victim.symlink_to(leak)

    estate = pipeline.scan(str(tmp_path / "root"))

    blob = json.dumps([v.metric_names for a in estate.assets for v in a.visuals])
    assert "Leaked Outside" not in blob
    assert any("outside the scan root" in e for e in estate.errors)
    assert pipeline.summarise(estate)["degraded"] >= 1


# --------------------------------------------------------------------------
# AUD-V1-005 -- multi-file output is one transaction
# --------------------------------------------------------------------------


def test_a_failed_second_output_leaves_the_first_untouched(tmp_path, monkeypatch):
    """AUD-V1-005 / AUD-V3-006. Outputs committed one at a time, so a failed
    second write left a fresh first output claiming to describe the same scan.

    ADAPTED (v3 §511): the write path no longer renames the original aside,
    so counting os.replace calls no longer lands on the intended boundary.
    The injector now fires on the SECOND destination's commit -- the same
    stimulus -- and the assertion is unchanged: both pre-existing files keep
    their original bytes and no owned temporary file is left behind."""
    one = tmp_path / "one.txt"
    two = tmp_path / "two.txt"
    one.write_text("ORIGINAL-ONE\n")
    two.write_text("ORIGINAL-TWO\n")

    real_replace = os.replace
    fired = {"count": 0}

    def flaky(src, dst):
        # Commits are the replaces whose destination is a requested output.
        if str(dst) in {str(one), str(two)}:
            fired["count"] += 1
            if fired["count"] == 2:
                raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)

    with pytest.raises(OSError):
        writers.commit_all([(str(one), "NEW-ONE\n"), (str(two), "NEW-TWO\n")])

    assert fired["count"] >= 2, "the injector must actually have fired"
    assert one.read_text() == "ORIGINAL-ONE\n"
    assert two.read_text() == "ORIGINAL-TWO\n"
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".dustpan-")]


def test_a_directory_destination_is_refused_not_renamed(tmp_path):
    """A directory given as an output path used to be moved aside and
    replaced by a file, because os.replace happily renames directories."""
    from dustpan.cli import main

    shutil.copytree(FIXTURES, tmp_path / "fx")
    victim = tmp_path / "adir"
    victim.mkdir()

    exit_code = main(["scan", str(tmp_path / "fx"), "--markdown", str(victim), "--force"])

    assert exit_code != 0
    assert victim.is_dir()


def test_overwriting_preserves_the_existing_permission_bits(tmp_path):
    """AUD-V1-018. mkstemp creates 0600, so an atomic replace silently
    tightened a world-readable report."""
    from dustpan.cli import main

    shutil.copytree(FIXTURES, tmp_path / "fx")
    out = tmp_path / "report.json"
    out.write_text("x\n")
    out.chmod(0o644)
    # Windows chmod exposes the writable/read-only attribute, not POSIX
    # group/other bits. Verify preservation of the actual pre-write mode.
    original_mode = out.stat().st_mode
    if os.name != "nt":
        assert oct(original_mode)[-3:] == "644"

    main(["scan", str(tmp_path / "fx"), "--json", str(out), "--force", "--quiet"])

    assert out.stat().st_mode == original_mode


# --------------------------------------------------------------------------
# AUD-V1-007 / 008 / 009 / 010 / 013
# --------------------------------------------------------------------------


def test_control_characters_are_neutralised_at_the_display_boundary():
    """AUD-V1-007, CORRECTED by AUD-V3-003.

    ORIGINAL EXPECTATION: `Visual`, `ScanNote` and `Finding` scrub control
    characters inside `__post_init__`, i.e. the IR itself holds escaped text.

    EVIDENCE IT WAS WRONG: the v3 audit (B-CE-04) showed that scrubbing in the
    IR makes a DAX expression containing a real ESC byte identical to one
    containing the six literal characters `\\x1b` BEFORE any fingerprint is
    taken. dustpan then reported an exact duplicate and offered to delete one
    of two genuinely different measures. Escaping upstream of identity
    manufactures identity.

    CORRECTED EXPECTATION: the IR keeps exact source bytes; console and
    Markdown render through `display_copy`, which escapes on a copy.

    WHY THE REQUIREMENT SURVIVES: the original requirement was that no raw
    control byte reaches a terminal or a Markdown document. That is asserted
    here directly on the rendered output, which is stronger than asserting an
    implementation detail of the IR."""
    from dustpan.ir import display_copy

    hostile = "x\x1b]0;pwned\x07y"
    estate = Estate(
        metrics=[
            Metric(
                id=f"powerbi:M:{hostile}",
                name=hostile,
                tool="powerbi",
                model="M",
                source_path="/proj/model.tmdl",
                source_root="/proj",
                expression='IF(a[b] = "\x1b", 1, 0)',
                dialect="dax",
            )
        ]
    )
    estate.errors.append(f"report: {hostile}: could not read file")
    pipeline._reconcile_notes(estate)

    # The IR keeps the bytes exactly as the source had them.
    assert estate.metrics[0].name == hostile
    assert "\x1b" in estate.metrics[0].expression

    # Every display surface neutralises them.
    assert "\x1b" not in console.render(estate, color=False)
    assert "\x07" not in console.render(estate, color=False)
    assert "\x1b" not in writers.render_markdown(estate)
    shown = display_copy(estate)
    assert "\x1b" not in (shown.metrics[0].name or "")
    assert shown.notes and "\x1b" not in shown.notes[0].message

    # JSON round-trips the true semantic text through JSON escaping.
    payload = json.loads(writers.render_json(estate))
    assert payload["metrics"][0]["name"] == hostile


def test_markdown_headings_and_summaries_do_not_carry_active_content():
    """AUD-V1-007. Table cells were escaped; headings and summaries were not,
    so a hostile measure name rendered as live HTML in a ticket."""
    estate = _estate(
        [_metric("<img src=x onerror=alert(1)>"), _metric("[go](javascript:alert(1))")]
    )
    import re

    # An escaped `\[go\](javascript:...)` cannot form a link, so the test
    # looks for the ACTIVE constructs only: an unescaped link bracket, and a
    # raw tag open.
    active_link = re.compile(r"(?<!\\)\]\(\s*javascript:", re.I)
    for line in writers.render_markdown(estate).splitlines():
        if line.startswith("#") or line.startswith("2 measures"):
            assert "<img" not in line, line
            assert not active_link.search(line), line


def test_tmsl_dynamic_format_blocks_retirement_like_tmdl_does(tmp_path):
    """AUD-V1-008. The same measure blocked retirement when read from TMDL
    and cleared it when read from model.bim -- safety depended on which file
    format the estate happened to use."""
    model_dir = tmp_path / "Dyn.SemanticModel"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bim").write_text(
        json.dumps(
            {
                "name": "Dyn",
                "model": {
                    "tables": [
                        {
                            "name": "Sales",
                            "measures": [
                                {
                                    "name": "A",
                                    "expression": "SUM(Sales[Amount])",
                                    "formatString": "#,0.00",
                                    "formatStringDefinition": {"expression": '"$" & 0'},
                                },
                                {
                                    "name": "B",
                                    "expression": "SUM(Sales[Amount])",
                                    "formatString": "#,0.00",
                                },
                            ],
                        }
                    ]
                },
            }
        )
    )
    report_dir = tmp_path / "Dyn.Report"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(
        json.dumps(
            {
                "sections": [
                    {
                        "name": "s",
                        "displayName": "P",
                        "visualContainers": [
                            {
                                "config": json.dumps(
                                    {
                                        "name": "v",
                                        "singleVisual": {
                                            "visualType": "card",
                                            "projections": {
                                                "Values": [{"queryRef": "Sales.A"}]
                                            },
                                        },
                                    }
                                )
                            }
                        ],
                    }
                ]
            }
        )
    )

    estate = pipeline.scan(str(tmp_path))
    flags = {m.name: (m.extra or {}).get("dynamic_format") for m in estate.metrics}

    assert flags.get("A") is True
    assert pipeline.proven_removable_metric_ids(estate) == set()


def test_recursion_cap_degrades_the_scan_instead_of_dropping_references(tmp_path):
    """AUD-V1-009. A reference below the depth cap simply vanished, and a
    reference that vanishes is how a used measure gets reported unused."""
    shutil.copytree(FIXTURES, tmp_path / "fx")
    node: object = {"queryRef": "Sales.Buried Measure"}
    for _ in range(100):
        node = {"wrap": node}
    target = (
        tmp_path
        / "fx"
        / "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
    )
    target.write_text(
        json.dumps({"name": "card1", "visual": {"visualType": "card", "query": node}})
    )

    estate = pipeline.scan(str(tmp_path / "fx"))

    assert any("deeper than" in e for e in estate.errors)
    assert pipeline.summarise(estate)["degraded"] >= 1


def test_health_state_survives_every_confidence_threshold(tmp_path):
    """AUD-V1-010. filter_by_confidence rebuilt the Estate without notes, so
    raising --min-confidence turned a degraded scan into a clean-looking one."""
    shutil.copytree(FIXTURES, tmp_path / "fx")
    estate = pipeline.scan(str(tmp_path / "fx"))
    estate.errors.append("report: /gone.json: could not read file: denied")
    pipeline._reconcile_notes(estate)

    baseline = pipeline.summarise(estate)["degraded"]
    assert baseline >= 1

    for threshold in (0.1, 0.5, 0.9, 1.0):
        trimmed, _dropped = pipeline.filter_by_confidence(estate, threshold)
        assert pipeline.summarise(trimmed)["degraded"] == baseline
        assert trimmed.read_paths == estate.read_paths


def test_summary_counts_deployments_not_folder_names(tmp_path):
    """AUD-V1-013. Two tenants each with a `SalesDemo` model reported as one
    model, understating the estate."""
    for tenant in ("a", "b"):
        shutil.copytree(
            os.path.join(FIXTURES, "SalesDemo.SemanticModel"),
            tmp_path / tenant / "SalesDemo.SemanticModel",
        )

    stats = pipeline.summarise(pipeline.scan(str(tmp_path)))

    assert stats["models"] == 2
    assert stats["deployments"] == 2
    assert stats["model_names"] == ["SalesDemo"]


# --------------------------------------------------------------------------
# AUD-V3-010 -- rendered-node oracle, not a regex
# --------------------------------------------------------------------------


def _rendered_html(markdown_text: str) -> str:
    """Render Markdown with a real CommonMark parser (dev-only dependency).

    A regex cannot prove that a payload failed to create an active node --
    only a parser can. `markdown-it-py` is a test dependency; the runtime
    stays standard-library-only.
    """
    markdown_it = pytest.importorskip("markdown_it")
    return markdown_it.MarkdownIt("commonmark").enable("table").render(markdown_text)


HOSTILE_TEXT = [
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
    "[go](javascript:alert(1))",
    "![img](javascript:alert(1))",
    "a` <script>alert(1)</script> `b",
    "back\\\\slash and `tick`",
    "pipe | in | a | cell",
    "line\\nbreak attempt",
    "``` fence terminator",
    "]] bracket [[ soup",
]


@pytest.mark.parametrize("payload", HOSTILE_TEXT)
def test_hostile_model_and_measure_names_create_no_active_markdown_nodes(payload):
    """AUD-V3-010. A hostile MODEL name reached the removal checklist outside
    any code span, and backslash escaping inside a code span is inert under
    CommonMark, so a name containing a backtick escaped the span."""
    metric_a = _metric("A")
    metric_b = _metric("B")
    metric_a.name = payload
    metric_a.model = payload
    metric_b.model = payload
    estate = _estate([metric_a, metric_b])

    html = _rendered_html(writers.render_markdown(estate))

    # Assert on NODES, not substrings: the payload appearing as inert escaped
    # text is the desired outcome, so `"javascript:" not in html` would fail
    # on a correct render. What must not exist is an element the payload
    # created.
    elements = re.findall(r"<\s*([A-Za-z][A-Za-z0-9]*)\b([^>]*)>", html)
    dangerous = {"img", "script", "iframe", "svg", "object", "embed", "form"}
    for tag, attrs in elements:
        assert tag.lower() not in dangerous, (
            f"payload {payload!r} created an active <{tag}> node"
        )
        assert not re.search(r"""(href|src)\s*=\s*["']?\s*javascript:""", attrs, re.I), (
            f"payload {payload!r} created an active javascript: URL"
        )
        assert not re.search(r"\bon[a-z]+\s*=", attrs, re.I), (
            f"payload {payload!r} created an event-handler attribute"
        )
    # The text must still be legible -- safety by deletion is not the fix.
    # It appears escaped (`&lt;script&gt;`), so look for a distinctive
    # alphanumeric run from the payload rather than the raw string.
    token = max(re.findall(r"[A-Za-z]{3,}", payload) or [""], key=len)
    assert not token or token in html

"""Fresh boundary variants; safety assertions fail when a defect is reproduced."""

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dustpan import limits, pipeline, safeio
from dustpan.dax.normalise import enrich
from dustpan.detect.duplicates import find_duplicates
from dustpan.detect.unused import find_unused
from dustpan.ir import Asset, Estate, Metric, Visual
from dustpan.output import console, writers

E = Path(__file__).parent


def _metric(name, fmt="#,0.00"):
    m = Metric(
        id=f"powerbi:M:{name}",
        name=name,
        tool="powerbi",
        model="M",
        source_path="/deployment/M.SemanticModel/T.tmdl",
        source_root="/deployment",
        expression="SUM(Sales[Amount])",
        dialect="dax",
        format_string=fmt,
        data_type="double",
        extra={"table": "Sales"},
    )
    enrich(m)
    return m


def _asset(names):
    return Asset(
        id="report",
        name="R",
        tool="powerbi",
        model="M",
        source_path="/deployment/M.Report",
        source_root="/deployment",
        visuals=[Visual(id="v", page="p", visual_type="card", metric_names=names)],
    )


def _findings(e):
    e.findings = find_duplicates(e) + find_unused(e)


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def minimal(root, model="M", exprs=None):
    exprs = exprs or {"A": "1", "B": "1"}
    put(
        root / f"{model}.SemanticModel/model.bim",
        {
            "model": {
                "tables": [
                    {
                        "name": "T",
                        "measures": [
                            {"name": k, "expression": v} for k, v in exprs.items()
                        ],
                    }
                ]
            }
        },
    )
    put(
        root / f"{model}.Report/report.json",
        {
            "sections": [
                {
                    "name": "p",
                    "visualContainers": [
                        {
                            "config": json.dumps(
                                {
                                    "name": "v",
                                    "singleVisual": {
                                        "visualType": "card",
                                        "projections": {
                                            "Values": [
                                                {"queryRef": f"T.{next(iter(exprs))}"}
                                            ]
                                        },
                                    },
                                }
                            )
                        }
                    ],
                }
            ]
        },
    )
    put(
        root / f"{model}.Report/definition.pbir",
        {"datasetReference": {"byPath": {"path": f"../{model}.SemanticModel"}}},
    )


def cli_run(args):
    import contextlib
    import io

    from dustpan import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main([str(x) for x in args])
    return rc, out.getvalue() + err.getvalue()


rows = []


def run(i, sev, title, fn):
    t = time.perf_counter()
    try:
        detail = fn()
        status = "PASS"
    except AssertionError as e:
        detail = str(e)
        status = "FAIL"
    except Exception:
        detail = traceback.format_exc()
        status = "ERROR"
    rows.append(
        {
            "id": i,
            "severity": sev,
            "title": title,
            "status": status,
            "detail": detail,
            "seconds": time.perf_counter() - t,
        }
    )
    print(i, status, detail, flush=True)


def model_coverage():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        minimal(root, model="One", exprs={"Used": "2"})
        put(
            root / "Two.SemanticModel/model.bim",
            {
                "model": {
                    "tables": [
                        {
                            "name": "T",
                            "measures": [
                                {"name": "A", "expression": "1"},
                                {"name": "B", "expression": "1"},
                            ],
                        }
                    ]
                }
            },
        )
        e = pipeline.scan(str(root))
        two = {m.id for m in e.metrics if m.model == "Two"}
        bad = pipeline.proven_removable_metric_ids(e) & two
        assert not bad, (
            f"Unreported sibling Two falsely covered; removable={sorted(bad)}; "
            f"assets={[(x.model, x.name) for x in e.assets]}"
        )
    return "Unbound sibling model remains non-actionable"


def display_metadata():
    e = Estate(
        metrics=[_metric("A", fmt="x\x1by"), _metric("B", fmt="x\\x1by")],
        assets=[_asset(names=["A"])],
    )
    _findings(e)
    raw = json.loads(writers.render_json(e))["summary"]["proven_removable"]
    md = writers.render_markdown(e)
    txt = console.render(e, color=False)
    assert raw == 0, "Raw control invalid"
    assert "**Action:**" not in md and "Action:" not in txt, (
        f"Raw JSON removable={raw}, Markdown action={'**Action:**' in md}, "
        f"console action={'Action:' in txt}; distinct raw formats collapsed"
    )
    return "Raw/display actionability agree"


def output_alias():
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        (r / "scan").mkdir()
        (r / "real").mkdir()
        (r / "alias").symlink_to(r / "real", target_is_directory=True)
        p = r / "real/out"
        q = r / "alias/out"
        rc, msg = cli_run(["scan", r / "scan", "--json", p, "--markdown", q, "--quiet"])
        assert rc != 0 and not p.exists(), (
            f"Exit={rc}; both outputs same inode={p.exists() and p.samefile(q)}; "
            f"final content={p.read_text()[:50] if p.exists() else None!r}"
        )
        try:
            writers.commit_all([(str(p), "JSON"), (str(q), "MARKDOWN")])
        except OSError:
            pass
        else:
            raise AssertionError("public writer accepted duplicate destinations")
        assert not p.exists(), "public writer changed output before refusing aliases"
    return "Parent-symlink destination alias rejected before commit"


def root_swap():
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        root = r / "scan"
        root.mkdir()
        out = r / "outside"
        out.mkdir()
        (root / "x").write_text("SAFE")
        (out / "x").write_text("OUTSIDE_SECRET")
        real = os.open
        hits = []
        e = Estate(scan_root=str(root))

        def op(path, flags, *args, **kw):
            if str(path) == str(root) and not hits:
                root.rename(r / "original")
                root.symlink_to(out, target_is_directory=True)
                hits.append(True)
            return real(path, flags, *args, **kw)

        with mock.patch.object(safeio.os, "open", op):
            data = safeio.read_bytes(str(root / "x"), e, "probe")
        assert hits, "Root swap hook did not fire"
        assert data != b"OUTSIDE_SECRET", (
            f"Root swap ingested {data!r}; material notes={len(e.material_notes())}"
        )
    return "Root identity pinned before traversal"


def growing_file():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = root / "x"
        p.write_bytes(b"1234")
        e = Estate(scan_root=td)
        real = safeio._read_exactly
        hits = []

        def grow(fd, cap):
            p.write_bytes(b"A" * 200)
            hits.append(cap)
            return real(fd, cap)

        with (
            mock.patch.object(safeio, "_read_exactly", grow),
            mock.patch.object(limits, "MAX_FILE_BYTES", 100),
        ):
            data = safeio.read_bytes(str(p), e, "probe")
        pipeline._reconcile_notes(e)
        assert hits, "Growth hook did not fire"
        assert data is None and e.material_notes(), (
            f"File grew 4->200 with budget100, read cap={hits}; accepted={data!r}, "
            f"material notes={len(e.material_notes())}"
        )
    return "Growth exceeding budget refused explicitly"


def malformed_query():
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        minimal(r)
        p = r / "M.Report/report.json"
        d = json.loads(p.read_text())
        d["sections"][0]["visualContainers"][0]["query"] = "NOT_JSON"
        put(p, d)
        e = pipeline.scan(str(r))
        bad = pipeline.proven_removable_metric_ids(e)
        assert e.material_notes() and not bad, (
            f"Known query string NOT_JSON silently ignored; notes={len(e.material_notes())}, "
            f"removable={sorted(bad)}"
        )
    return "Malformed known embedded query degrades scan"


for args in [
    ("X01", "High", "Coverage must distinguish sibling semantic models", model_coverage),
    ("X02", "High", "Display metadata cannot change actionability", display_metadata),
    ("X03", "High", "Parent symlink aliases cannot collapse two outputs", output_alias),
    ("X04", "High", "Scan-root replacement cannot escape containment", root_swap),
    (
        "X05",
        "Medium",
        "Concurrent file growth cannot be silently truncated",
        growing_file,
    ),
    (
        "X06",
        "High",
        "Known embedded query must fail closed on non-JSON content",
        malformed_query,
    ),
]:
    run(*args)
ap = argparse.ArgumentParser()
ap.add_argument("--json", default="boundary-results.json")
args = ap.parse_args()
Path(args.json).write_text(json.dumps(rows, indent=2))
sys.exit(0 if all(r["status"] == "PASS" for r in rows) else 1)

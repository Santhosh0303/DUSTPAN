#!/usr/bin/env python3
"""The v3 audit's 30 bounded stress workloads, re-executed with assertions.

Each workload runs in a fresh child process with a wall-clock timeout, and
each asserts a CORRECTNESS outcome -- a count, a fingerprint, an expected
refusal -- rather than merely exiting zero. Peak RSS is the child's Linux
high-water mark, which includes interpreter and import baseline; it is a
single observation, not a percentile benchmark, and no production SLA is
implied by any of it.

    python tools/stress.py [--json out.json] [--timeout 60]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

WORKLOADS: list[tuple[str, str, int]] = [
    ("S01", "dax", 1024),
    ("S02", "dax", 32768),
    ("S03", "dax", 131072),
    ("S04", "dax", 262144),
    ("S05", "dax", 262145),
    ("S06", "duplicates", 1000),
    ("S07", "duplicates", 5000),
    ("S08", "duplicates", 10000),
    ("S09", "dependency", 1000),
    ("S10", "dependency", 5000),
    ("S11", "dependency", 20000),
    ("S12", "json_output", 1000),
    ("S13", "json_output", 5000),
    ("S14", "json_output", 10000),
    ("S15", "tmdl", 100),
    ("S16", "tmdl", 1000),
    ("S17", "tmdl", 5000),
    ("S18", "report_bytes", 1048576),
    ("S19", "report_bytes", 8388608),
    ("S20", "report_bytes", 16777216),
    ("S21", "report_bytes", 16777217),
    ("S22", "many_duplicate_groups", 500),
    ("S23", "many_duplicate_groups", 2000),
    ("S24", "many_duplicate_groups", 5000),
    ("S25", "many_source_paths", 500),
    ("S26", "many_source_paths", 2000),
    ("S27", "many_source_paths", 5000),
    ("S28", "many_unresolved_refs", 500),
    ("S29", "many_unresolved_refs", 2000),
    ("S30", "many_unresolved_refs", 5000),
]

WORKER = r"""
import json, os, resource, sys, tempfile
sys.path.insert(0, %(src)r)
kind, size = sys.argv[1], int(sys.argv[2])
from dustpan import pipeline
from dustpan.dax.normalise import normalise, enrich
from dustpan.detect.duplicates import find_duplicates
from dustpan.detect.unused import build_asset_index, build_dependency_graph
from dustpan.ir import Asset, Estate, Metric, Visual
from dustpan.output import writers
from dustpan import safeio

def m(name, expr="SUM(S[X])", root="/proj"):
    x = Metric(id="powerbi:M:%%s" %% name, name=name, tool="powerbi", model="M",
               source_path=root + "/m.tmdl", source_root=root, expression=expr,
               dialect="dax", format_string="#,0", data_type="double")
    enrich(x); return x

out = {}
if kind == "dax":
    unit = " + 1"
    expr = "SUM(S[X])" + unit * max(0, (size - len("SUM(S[X])")) // len(unit))
    expr += "0" * (size - len(expr))
    r = normalise(expr)
    out = {"ok": r.ok, "error": r.error, "normalised_bytes": len(r.normalised or "")}
    assert len(expr) == size, "generator must produce the exact requested byte count"
    if size > 262144:
        assert not r.ok and "budget" in (r.error or ""), "over-budget input must be refused"
    else:
        assert r.ok, "in-budget input must normalise"
elif kind == "duplicates":
    e = Estate(metrics=[m("M%%d" %% i) for i in range(size)])
    f = find_duplicates(e)
    exact = [x for x in f if x.kind == "exact_duplicate"]
    out = {"metrics": size, "findings": len(exact), "members": len(exact[0].metric_ids)}
    assert len(exact) == 1 and len(exact[0].metric_ids) == size
elif kind == "dependency":
    ms = [m("M0")] + [m("M%%d" %% i, "[M%%d] + 1" %% (i - 1)) for i in range(1, size)]
    g = build_dependency_graph(Estate(metrics=ms))
    edges = sum(len(v) for v in g.values())
    out = {"nodes": size, "edges": edges}
    assert edges == size - 1
elif kind == "json_output":
    e = Estate(metrics=[m("M%%d" %% i, "SUM(S[C%%d])" %% i) for i in range(size)])
    text = writers.render_json(e)
    payload = json.loads(text)
    out = {"metrics": size, "output_bytes": len(text)}
    assert len(payload["metrics"]) == size
elif kind == "tmdl":
    from dustpan.parsers.tmdl import parse_model
    d = tempfile.mkdtemp()
    lines = ["table T", ""] + ["\tmeasure 'M%%d' = SUM(T[C%%d])" %% (i, i) for i in range(size)]
    open(os.path.join(d, "T.tmdl"), "w").write("\n".join(lines) + "\n")
    e = Estate(); e.scan_root = d; parse_model(d, e)
    out = {"requested": size, "parsed": len(e.metrics), "errors": len(e.errors)}
    assert len(e.metrics) == size and not e.errors
elif kind == "report_bytes":
    from dustpan.parsers.report import parse_report
    d = tempfile.mkdtemp(); rd = os.path.join(d, "R.Report"); os.makedirs(rd)
    cfg = json.dumps({"name": "v", "singleVisual": {"visualType": "card",
          "projections": {"Values": [{"queryRef": "S.A"}]}}})
    doc = {"sections": [{"name": "s", "displayName": "P", "visualContainers": [{"config": cfg}]}],
           "pad": ""}
    base = len(json.dumps(doc))
    doc["pad"] = "x" * max(0, size - base)
    open(os.path.join(rd, "report.json"), "w").write(json.dumps(doc))
    e = Estate(); e.scan_root = d; parse_report(rd, e)
    out = {"file_bytes": os.path.getsize(os.path.join(rd, "report.json")),
           "assets": len(e.assets), "errors": len(e.errors)}
    if size > 16777216:
        assert e.errors, "an over-budget report must be refused with a note"
    else:
        assert len(e.assets) == 1 and not e.errors
elif kind == "many_duplicate_groups":
    groups = size // 2
    ms = []
    for i in range(groups):
        ms.append(m("A%%d" %% i, "SUM(S[C%%d])" %% i))
        ms.append(m("B%%d" %% i, "SUM(S[C%%d])" %% i))
    e = Estate(metrics=ms)
    e.assets.append(Asset(id="r", name="r", tool="powerbi", source_path="r", source_root="/proj",
        visuals=[Visual(id="v", page="p", visual_type="card",
                        metric_names=["A%%d" %% i for i in range(groups)])]))
    e.findings = find_duplicates(e)
    removable = pipeline.proven_removable_metric_ids(e)
    out = {"groups": groups, "removable": len(removable)}
    assert len(removable) == groups
elif kind == "many_source_paths":
    d = tempfile.mkdtemp(); e = Estate(); e.scan_root = d
    for i in range(size):
        p = os.path.join(d, "f%%d.txt" %% i)
        open(p, "w").write("x")
        safeio.register_protected(p, e)
    out = {"requested": size, "protected": len(set(e.protected_paths))}
    assert len(set(e.protected_paths)) >= size
elif kind == "many_unresolved_refs":
    e = Estate(metrics=[m("Known")])
    e.assets.append(Asset(id="r", name="r", tool="powerbi", source_path="r", source_root="/proj",
        visuals=[Visual(id="v", page="p", visual_type="card",
                        metric_names=["Ghost%%d" %% i for i in range(size)])]))
    idx = build_asset_index(e)
    out = {"requested": size, "unresolved": len(idx.unresolved)}
    assert len(idx.unresolved) == size
else:
    raise SystemExit("unknown workload " + kind)

out["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps(out))
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    worker = WORKER % {"src": SRC}
    script = Path(ROOT, ".stress_worker.py")
    script.write_text(worker, encoding="utf-8")
    rows = []
    try:
        for sid, kind, size in WORKLOADS:
            started = time.perf_counter()
            try:
                proc = subprocess.run(
                    [sys.executable, str(script), kind, str(size)],
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                )
                elapsed = time.perf_counter() - started
                if proc.returncode != 0:
                    status, detail = (
                        "FAIL",
                        proc.stderr.strip().splitlines()[-1:] or ["no output"],
                    )
                    evidence = {"stderr": detail[0][:200]}
                else:
                    status = "PASS"
                    evidence = json.loads(proc.stdout.strip().splitlines()[-1])
            except subprocess.TimeoutExpired:
                elapsed = time.perf_counter() - started
                status, evidence = "FAIL", {"error": f"timed out after {args.timeout}s"}
            rows.append(
                {
                    "id": sid,
                    "workload": kind,
                    "size": size,
                    "result": status,
                    "seconds": round(elapsed, 6),
                    "max_rss_kib": evidence.get("max_rss_kib"),
                    "evidence": evidence,
                }
            )
            print(
                f"{status:4}  {sid}  {kind:22} {size:>9}  {elapsed:7.3f}s  "
                f"rss={evidence.get('max_rss_kib', '?')}  {evidence}"
            )
    finally:
        script.unlink(missing_ok=True)

    passed = sum(1 for r in rows if r["result"] == "PASS")
    print(f"\nStress: {passed}/{len(rows)} workloads passed their correctness assertion")
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

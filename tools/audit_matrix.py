#!/usr/bin/env python3
"""Executable form of the v1 adversarial audit's 40-case Edge/OAT matrix.

The audit was run by hand once and reported 20 PASS / 20 FAIL. A finding that
is only ever checked by hand comes back, so every case is reimplemented here
against its ORIGINAL expectation and can be re-run on demand:

    python tools/audit_matrix.py            # table + non-zero exit on any fail
    python tools/audit_matrix.py --json out.json

Rules this harness holds itself to:

* a case asserts the user-visible property the audit named, not the shape of
  today's implementation -- a case that only checks the current code stops
  protecting anything the moment the code is rewritten;
* nothing is marked PASS because it "did not crash";
* a case that cannot be decided in this environment reports SKIP with the
  reason, never PASS. SKIP is not a pass, and the summary counts it apart.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, SRC)

from dustpan import limits, pipeline  # noqa: E402
from dustpan.dax.normalise import enrich, normalise  # noqa: E402
from dustpan.detect.duplicates import find_duplicates  # noqa: E402
from dustpan.ir import Asset, Estate, Metric, Visual  # noqa: E402
from dustpan.output import console, writers  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _delivered_metadata_leaks() -> list[str]:
    """VCS/private metadata present in this tree AS DELIVERED.

    Snapshotted at import, before any case runs, because several OAT cases
    build the package and a build writes `*.egg-info` and `__pycache__` into
    the source tree. Measuring after they run would flag a correct release
    for artifacts the harness itself created. What the release ARCHIVE
    contains is verified separately, at build time, by release.sh.
    """
    out: list[str] = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [
            d for d in dirs if d not in {"__pycache__", ".mypy_cache", ".ruff_cache"}
        ]
        for name in files:
            full = os.path.join(base, name)
            if (
                ".git" + os.sep in full + os.sep
                or "egg-info" in full
                or name.endswith((".pyc", ".env"))
            ):
                out.append(os.path.relpath(full, ROOT))
    return out


DELIVERED_LEAKS = _delivered_metadata_leaks()


@dataclass
class Case:
    cid: str
    level: str
    kind: str
    title: str
    fn: Callable[[], tuple[str, str]]
    result: str = ""
    detail: str = ""
    seconds: float = 0.0


CASES: list[Case] = []


#: v3 handoff section 5: "Matrix A preserves baseline IDs with an A prefix".
ID_PREFIX = "A-"


def case(cid: str, level: str, kind: str, title: str):
    def wrap(fn):
        CASES.append(Case(ID_PREFIX + cid, level, kind, title, fn))
        return fn

    return wrap


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def metric(name, *, mid=None, root="/proj", fmt="#,0.00", dtype="double", expr=None):
    m = Metric(
        id=mid or f"powerbi:M:{name}",
        name=name,
        tool="powerbi",
        model="M",
        source_path=f"{root}/model.tmdl",
        source_root=root,
        expression=expr or "SUM(Sales[Amount])",
        dialect="dax",
        format_string=fmt,
        data_type=dtype,
    )
    enrich(m)
    return m


def estate_with(metrics, *, usage=True, root="/proj"):
    e = Estate(metrics=list(metrics))
    if usage:
        e.assets.append(
            Asset(
                id="a",
                name="r",
                tool="powerbi",
                source_path="r.json",
                source_root=root,
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
    e.findings = find_duplicates(e)
    pipeline.apply_actionability(e)
    return e


def cli(args: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=SRC, NO_COLOR="1")
    proc = subprocess.run(
        [sys.executable, "-m", "dustpan.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    return proc.returncode, proc.stdout + proc.stderr


def copy_fixtures(dst: str) -> str:
    shutil.copytree(FIXTURES, dst)
    return dst


def dump_json(obj: object, path: str) -> None:
    """`json.dump` with the file handling done properly (argument order kept
    identical so call sites read the same)."""
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def venv_python(venv_dir: str) -> str:
    """Interpreter inside `venv_dir`, on this platform.

    AUD-V3-018: the runner hard-coded `venv/bin/pip` and `venv/bin/dustpan`
    while the CI matrix declares Windows runners, where a virtual
    environment's executables live under `Scripts` with a `.exe` suffix. Every
    tool is invoked as `<python> -m <tool>` so only one path has to be right.

    Reference: https://docs.python.org/3/library/venv.html
    """
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def venv_script(venv_dir: str, name: str) -> str:
    """A console entry point inside `venv_dir`, on this platform."""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", f"{name}.exe")
    return os.path.join(venv_dir, "bin", name)


def isolated_env() -> dict[str, str]:
    """Environment for artifact-isolation subprocesses.

    An inherited PYTHONPATH pointing at the source tree makes an installed
    wheel appear importable when it is not -- the exact contamination the v3
    audit hit and corrected in its own harness (report section 8). Every venv,
    pip, build and installed-CLI invocation runs without it.
    """
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def verdict(ok: bool, good: str, bad: str) -> tuple[str, str]:
    return (PASS, good) if ok else (FAIL, bad)


# ==========================================================================
# CRITICAL -- edge
# ==========================================================================


@case("CE-01", "Critical", "Edge", "Metadata mismatch cannot produce retirement advice")
def ce_01():
    e = estate_with([metric("A"), metric("B", fmt="0.0%")])
    f = next(x for x in e.findings if x.kind == "exact_duplicate")
    leaked = [k for k in ("keep", "keep_id", "retire") if k in f.evidence]
    rendered = "Action: keep" in console.render(e, color=False) or (
        "**Action:**" in writers.render_markdown(e)
    )
    removable = pipeline.proven_removable_metric_ids(e)
    ok = not leaked and not rendered and not removable
    return verdict(
        ok,
        "blocked set carries no keep/retire evidence, no rendered action, nothing removable",
        f"leaked evidence keys={leaked} rendered_action={rendered} removable={sorted(removable)}",
    )


@case("CE-02", "Critical", "Edge", "Same-basename deployments stay isolated")
def ce_02():
    e = estate_with(
        [
            metric("Total", mid="powerbi:M:Total", root="/tenantA"),
            metric("Total", mid="powerbi:M:Total#2", root="/tenantB"),
        ],
        root="/tenantA",
    )
    removable = pipeline.proven_removable_metric_ids(e)
    ok = not removable and len({m.deployment for m in e.metrics}) == 2
    return verdict(
        ok,
        "two deployments recognised; nothing removable across them",
        f"proven_removable={sorted(removable)}",
    )


@case("CE-03", "Critical", "Edge", "Material scan errors block actionability")
def ce_03():
    e = estate_with([metric("A"), metric("B")])
    control = pipeline.proven_removable_metric_ids(e)
    e.errors.append("report: /x/y.json: could not read file: denied")
    pipeline._reconcile_notes(e)
    pipeline.apply_actionability(e)
    after = pipeline.proven_removable_metric_ids(e)
    ok = bool(control) and not after and bool(e.material_notes())
    return verdict(
        ok,
        f"clean estate removed {len(control)}; a material note withdrew all of them",
        f"control={sorted(control)} after_error={sorted(after)}",
    )


@case("CE-04", "Critical", "Edge", "Nested PBIR source cannot be an output")
def ce_04():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        target = os.path.join(
            fx, "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
        )
        before = Path(target).read_bytes()
        code, _out = cli(["scan", fx, "--json", target, "--force", "--quiet"])
        same = Path(target).read_bytes() == before
        ok = code != 0 and same
        return verdict(
            ok,
            f"refused with exit {code}; nested source byte-identical",
            f"exit={code} unchanged={same}",
        )


@case("CE-05", "Critical", "Edge", "TMSL dynamic format blocks retirement")
def ce_05():
    with tempfile.TemporaryDirectory() as tmp:
        md = os.path.join(tmp, "Dyn.SemanticModel")
        os.makedirs(md)
        dump_json(
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
            },
            os.path.join(md, "model.bim"),
        )
        rd = os.path.join(tmp, "Dyn.Report")
        os.makedirs(rd)
        dump_json(
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
            },
            os.path.join(rd, "report.json"),
        )
        e = pipeline.scan(tmp)
        flags = {m.name: (m.extra or {}).get("dynamic_format") for m in e.metrics}
        removable = pipeline.proven_removable_metric_ids(e)
        ok = flags.get("A") is True and not removable
        return verdict(
            ok,
            "TMSL formatStringDefinition retained and blocking",
            f"flags={flags} removable={sorted(removable)}",
        )


# ==========================================================================
# CRITICAL -- OAT
# ==========================================================================


@case("CO-01", "Critical", "OAT", "Dual outputs commit all-or-none")
def co_01():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        out_json = os.path.join(tmp, "out.json")
        Path(out_json).write_text("ORIGINAL\n")
        blocked = os.path.join(tmp, "isdir")
        os.makedirs(blocked)
        code, _ = cli(
            ["scan", fx, "--json", out_json, "--markdown", blocked, "--force", "--quiet"]
        )
        untouched = Path(out_json).read_text() == "ORIGINAL\n"
        strays = [p for p in os.listdir(tmp) if "dustpan" in p]
        ok = code != 0 and untouched and not strays
        return verdict(
            ok,
            f"exit {code}; the first output was not committed; no temp files left",
            f"exit={code} json_untouched={untouched} strays={strays}",
        )


@case("CO-02", "Critical", "OAT", "Structural TMDL source cannot be overwritten")
def co_02():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        target = os.path.join(fx, "SalesDemo.SemanticModel/definition/model.tmdl")
        before = Path(target).read_bytes()
        code, _ = cli(["scan", fx, "--json", target, "--force", "--quiet"])
        same = Path(target).read_bytes() == before
        ok = code != 0 and same
        return verdict(
            ok,
            f"refused with exit {code}; structural TMDL byte-identical",
            f"exit={code} unchanged={same}",
        )


@case("CO-03", "Critical", "OAT", "Nested PBIR symlink is contained")
def co_03():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "root"))
        outside = os.path.join(tmp, "outside")
        os.makedirs(outside)
        leak = os.path.join(outside, "leak.json")
        dump_json(
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
            },
            leak,
        )
        victim = os.path.join(
            fx, "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
        )
        os.unlink(victim)
        os.symlink(leak, victim)
        e = pipeline.scan(fx)
        blob = json.dumps([v.metric_names for a in e.assets for v in a.visuals])
        contained = "Leaked Outside" not in blob
        noted = any("outside the scan root" in x for x in e.errors)
        degraded = pipeline.summarise(e)["degraded"] >= 1
        ok = contained and noted and degraded
        return verdict(
            ok,
            "outside content not ingested; refusal recorded; scan marked degraded",
            f"contained={contained} noted={noted} degraded={degraded}",
        )


@case("CO-04", "Critical", "OAT", "Console neutralises terminal controls")
def co_04():
    hostile = "Evil\x1b]0;pwned\x07\x1b[2JName"
    e = estate_with([metric(hostile), metric("B")])
    e.errors.append("report: hostile\x1b]0;pwn\x07 path")
    pipeline._reconcile_notes(e)
    text = console.render(e, color=False)
    bad = [c for c in ("\x1b", "\x07", "\x9b") if c in text]
    return verdict(
        not bad,
        "no raw C0/C1 control bytes in console output with colour disabled",
        f"raw controls present: {[hex(ord(c)) for c in bad]}",
    )


@case("CO-05", "Critical", "OAT", "Markdown neutralises active content")
def co_05():
    """Baseline stimulus retained; oracle upgraded per v3 handoff section 6.

    The original assertion was a regex over the Markdown source, which cannot
    distinguish a payload rendered as inert text from one that created an
    active node -- and flagged correct output whose payload sits inside a
    code span. The stimulus is unchanged; the check now parses the rendered
    document and asserts on ELEMENTS. `markdown-it-py` is a declared dev
    dependency; without it the case reports SKIP, never PASS.
    """
    import re as _re

    try:
        import markdown_it
    except ImportError:
        return SKIP, "markdown-it-py not installed (declared in the dev extra)"

    e = estate_with(
        [metric("<img src=x onerror=alert(1)>"), metric("[go](javascript:alert(1))")]
    )
    e.metrics[0].model = "<script>alert(1)</script>"
    md = writers.render_markdown(e)
    html = markdown_it.MarkdownIt("commonmark").enable("table").render(md)

    dangerous = {"img", "script", "iframe", "svg", "object", "embed", "form"}
    offences: list[str] = []
    for tag, attrs in _re.findall(r"<\s*([A-Za-z][A-Za-z0-9]*)\b([^>]*)>", html):
        if tag.lower() in dangerous:
            offences.append(f"<{tag}>")
        if _re.search(r"""(href|src)\s*=\s*["']?\s*javascript:""", attrs, _re.I):
            offences.append(f"<{tag} javascript: url>")
        if _re.search(r"\bon[a-z]+\s*=", attrs, _re.I):
            offences.append(f"<{tag} event handler>")
    return verdict(
        not offences,
        "hostile measure and model text creates no active node in the parsed render",
        f"active nodes created: {sorted(set(offences))}",
    )


# ==========================================================================
# HIGH -- edge
# ==========================================================================


@case("HE-01", "High", "Edge", "Deep JSON reference cannot disappear silently")
def he_01():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        node: object = {"queryRef": "Sales.Buried Measure"}
        for _ in range(100):
            node = {"wrap": node}
        target = os.path.join(
            fx, "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
        )
        dump_json(
            {"name": "card1", "visual": {"visualType": "card", "query": node}},
            target,
        )
        e = pipeline.scan(fx)
        noted = any("deeper than" in x for x in e.errors)
        degraded = pipeline.summarise(e)["degraded"] >= 1
        return verdict(
            noted and degraded,
            "depth cap recorded as a material note; scan marked degraded",
            f"noted={noted} degraded={degraded}",
        )


@case("HE-02", "High", "Edge", "Confidence filtering preserves health notes")
def he_02():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        e = pipeline.scan(fx)
        e.errors.append("report: /gone.json: could not read file: denied")
        pipeline._reconcile_notes(e)
        base = pipeline.summarise(e)["degraded"]
        seen = []
        for threshold in (0.1, 0.5, 0.9, 1.0):
            trimmed, _ = pipeline.filter_by_confidence(e, threshold)
            seen.append(pipeline.summarise(trimmed)["degraded"])
        ok = base >= 1 and all(v == base for v in seen)
        return verdict(
            ok,
            f"degraded={base} preserved at every threshold {seen}",
            f"base={base} thresholds={seen}",
        )


@case("HE-03", "High", "Edge", "Summary counts deployment identities")
def he_03():
    with tempfile.TemporaryDirectory() as tmp:
        for tenant in ("a", "b"):
            shutil.copytree(
                os.path.join(FIXTURES, "SalesDemo.SemanticModel"),
                os.path.join(tmp, tenant, "SalesDemo.SemanticModel"),
            )
        stats = pipeline.summarise(pipeline.scan(tmp))
        ok = stats["models"] == 2 and stats.get("deployments") == 2
        return verdict(
            ok,
            f"models={stats['models']} deployments={stats.get('deployments')}",
            f"models={stats['models']} deployments={stats.get('deployments')}",
        )


@case("HE-04", "High", "Edge", "Source-file budget is enforced")
def he_04():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        big = os.path.join(fx, "SalesDemo.SemanticModel/definition/huge.tmdl")
        with open(big, "w") as fh:
            fh.write("table Big\n\n\tmeasure 'X' = SUM(A[b])\n" + "\t// pad\n" * 200000)
        os.environ["DUSTPAN_MAX_FILE_BYTES"] = "1048576"
        try:
            import importlib

            importlib.reload(limits)
            e = pipeline.scan(fx)
        finally:
            os.environ.pop("DUSTPAN_MAX_FILE_BYTES", None)
            import importlib

            importlib.reload(limits)
        noted = any("budget" in x for x in e.errors)
        survived = len(e.metrics) >= 7
        return verdict(
            noted and survived,
            "oversize file skipped with a note; the rest of the model still parsed",
            f"noted={noted} metrics={len(e.metrics)}",
        )


@case("HE-05", "High", "Edge", "Expression budget is enforced")
def he_05():
    r = normalise("SUM(a[b])" + " + 1" * 80000)
    ok = (not r.ok) and "budget" in (r.error or "")
    return verdict(
        ok, "oversize expression refused with a budget error", f"ok={r.ok} err={r.error}"
    )


# ==========================================================================
# HIGH -- OAT
# ==========================================================================


@case("HO-01", "High", "OAT", "Native test suite passes")
def ho_01():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], capture_output=True, text=True, cwd=ROOT
    )
    tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    return verdict(p.returncode == 0, tail, tail or p.stderr[-200:])


@case("HO-02", "High", "OAT", "Repository Ruff CI gate passes")
def ho_02():
    if shutil.which("ruff") is None:
        return SKIP, "ruff not installed in this environment"
    checks = []
    for args in (["check", "src", "tests"], ["format", "--check", "src", "tests"]):
        p = subprocess.run(["ruff", *args], capture_output=True, text=True, cwd=ROOT)
        checks.append((args[0], p.returncode, p.stdout.strip().splitlines()[-1:]))
    ok = all(rc == 0 for _n, rc, _o in checks)
    ver = subprocess.run(["ruff", "--version"], capture_output=True, text=True).stdout
    return verdict(ok, f"ruff check and format clean ({ver.strip()})", f"{checks}")


@case("HO-03", "High", "OAT", "Strict mypy CI gate passes")
def ho_03():
    if shutil.which("mypy") is None:
        return SKIP, "mypy not installed in this environment"
    p = subprocess.run(
        ["mypy", "--strict", "--namespace-packages", "-p", "dustpan"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=dict(os.environ, MYPYPATH=SRC),
    )
    return verdict(
        p.returncode == 0, p.stdout.strip().splitlines()[-1], p.stdout.strip()[-300:]
    )


@case("HO-04", "High", "OAT", "Wheel installs and runs offline")
def ho_04():
    with tempfile.TemporaryDirectory() as tmp:
        b = subprocess.run(
            [sys.executable, "-m", "build", "-o", tmp],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=isolated_env(),
        )
        if b.returncode != 0:
            return FAIL, f"build failed: {b.stderr[-200:]}"
        wheels = [f for f in os.listdir(tmp) if f.endswith(".whl")]
        if not wheels:
            return FAIL, "no wheel produced"
        venv = os.path.join(tmp, "venv")
        subprocess.run(
            [sys.executable, "-m", "venv", venv], check=True, env=isolated_env()
        )
        py = venv_python(venv)
        i = subprocess.run(
            [py, "-m", "pip", "install", "--no-index", os.path.join(tmp, wheels[0])],
            capture_output=True,
            text=True,
            env=isolated_env(),
        )
        if i.returncode != 0:
            return FAIL, f"offline install failed: {i.stderr[-200:]}"
        r = subprocess.run(
            [venv_script(venv, "dustpan"), "version"],
            capture_output=True,
            text=True,
            env=isolated_env(),
        )
        return verdict(
            r.returncode == 0 and "dustpan" in r.stdout,
            f"offline install ok; {r.stdout.strip()}",
            f"cli exit={r.returncode} out={r.stdout!r}",
        )


@case("HO-05", "High", "OAT", "Sdist runs bundled tests")
def ho_05():
    with tempfile.TemporaryDirectory() as tmp:
        b = subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "-o", tmp],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=isolated_env(),
        )
        if b.returncode != 0:
            return FAIL, f"sdist build failed: {b.stderr[-200:]}"
        tar = next(f for f in os.listdir(tmp) if f.endswith(".tar.gz"))
        ext = os.path.join(tmp, "x")
        os.makedirs(ext)
        subprocess.run(
            ["tar", "xzf", os.path.join(tmp, tar), "-C", ext, "--strip-components=1"],
            check=True,
        )
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            capture_output=True,
            text=True,
            cwd=ext,
        )
        tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        return verdict(p.returncode == 0, f"sdist self-test: {tail}", tail)


# ==========================================================================
# MEDIUM -- edge
# ==========================================================================


@case("ME-01", "Medium", "Edge", "Discovery walk errors are visible")
def me_01():
    from dustpan.parsers import discover as D

    e = Estate()
    real_walk = os.walk

    def fake_walk(top, **kw):
        cb = kw.get("onerror")
        if cb:
            exc = PermissionError(13, "Permission denied")
            exc.filename = os.path.join(top, "locked")
            cb(exc)
        return real_walk(top, **kw)

    os.walk = fake_walk  # type: ignore[assignment]
    try:
        D.discover(FIXTURES, e)
    finally:
        os.walk = real_walk  # type: ignore[assignment]
    noted = any("could not read" in x for x in e.errors)
    return verdict(noted, "walk denial recorded on the estate", f"errors={e.errors[:1]}")


@case("ME-02", "Medium", "Edge", "Report listing errors are visible")
def me_02():
    from dustpan.parsers import report as R

    e = Estate()
    real = os.listdir

    def fake(p):
        if "visuals" in str(p):
            raise PermissionError(13, "Permission denied")
        return real(p)

    os.listdir = fake  # type: ignore[assignment]
    try:
        R._sorted_dirs("/some/visuals", e)
    finally:
        os.listdir = real  # type: ignore[assignment]
    noted = any("could not list" in x for x in e.errors)
    return verdict(noted, "listing denial recorded", f"errors={e.errors[:1]}")


@case("ME-03", "Medium", "Edge", "Malformed visual is isolated")
def me_03():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        bad = os.path.join(
            fx, "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
        )
        Path(bad).write_text("{ not json")
        e = pipeline.scan(fx)
        noted = bool(e.errors)
        survived = any(v.visual_type == "barChart" for a in e.assets for v in a.visuals)
        return verdict(
            noted and survived,
            "malformed visual reported; sibling visuals still parsed",
            f"noted={noted} siblings={survived}",
        )


@case("ME-04", "Medium", "Edge", "Dependency cycle terminates safely")
def me_04():
    from dustpan.detect.unused import find_unused

    a = metric("A", expr="[B] + 1")
    b = metric("B", expr="[A] + 1")
    e = Estate(metrics=[a, b])
    e.assets.append(
        Asset(
            id="a",
            name="r",
            tool="powerbi",
            source_path="r",
            source_root="/proj",
            visuals=[Visual(id="v", page="p", visual_type="card", metric_names=["A"])],
        )
    )
    findings = find_unused(e)
    unused = [n for f in findings if f.kind == "unused_measure" for n in f.metric_ids]
    return verdict(
        not unused, "cycle terminated; both members reachable", f"unused={unused}"
    )


@case("ME-05", "Medium", "Edge", "Unicode path and names are handled")
def me_05():
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "租户-α", "Ventes-é.SemanticModel", "definition")  # noqa: RUF001
        os.makedirs(d)
        Path(d, "Ventes.tmdl").write_text(
            "table Ventes\n\n\tmeasure 'Čistý zisk' = SUM ( Ventes[Montant] )\n",
            encoding="utf-8",
        )
        e = pipeline.scan(tmp)
        names = [m.name for m in e.metrics]
        return verdict(
            "Čistý zisk" in names and not e.errors,
            f"unicode path and measure parsed: {names}",
            f"names={names} errors={e.errors[:1]}",
        )


# ==========================================================================
# MEDIUM -- OAT
# ==========================================================================


@case("MO-01", "Medium", "OAT", "Documented test count is current")
def mo_01():
    import re

    p = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    m = re.search(r"(\d+)\s+tests? collected", p.stdout)
    collected = int(m.group(1)) if m else -1
    readme = read_text(os.path.join(ROOT, "README.md"))
    claims = {int(x) for x in re.findall(r"\b(\d{3})\s+tests\b", readme)}
    ok = collected > 0 and (not claims or claims == {collected})
    return verdict(
        ok,
        f"README states {sorted(claims) or 'no count'}; collection finds {collected}",
        f"claims={sorted(claims)} collected={collected}",
    )


@case("MO-02", "Medium", "OAT", "IR contract and constant agree")
def mo_02():
    import re

    from dustpan.ir import IR_VERSION

    text = read_text(os.path.join(SRC, "dustpan", "ir.py"))
    stated = {int(x) for x in re.findall(r"[Cc]ontract version:\s*(\d+)", text)}
    ok = not stated or stated == {IR_VERSION}
    return verdict(
        ok,
        f"docstring and IR_VERSION both say {IR_VERSION}",
        f"docstring={sorted(stated)} IR_VERSION={IR_VERSION}",
    )


@case("MO-03", "Medium", "OAT", "Source bundle excludes VCS/private metadata")
def mo_03():
    import zipfile

    # The property belongs to the ARTIFACT, so check the tree this harness is
    # actually running in first. Running only the release script would pass in
    # a development checkout while saying nothing about what a user received --
    # and a user's extracted copy has no repository to build from at all.
    here = DELIVERED_LEAKS
    in_repo = os.path.isdir(os.path.join(ROOT, ".git"))
    if not in_repo:
        return verdict(
            not here,
            f"distributed tree carries no VCS or generated metadata ({len(here)} found)",
            f"leaked in the distributed tree: {here[:5]}",
        )

    # A development checkout legitimately has .git; what must be clean is what
    # `release.sh` produces from it.
    script = os.path.join(ROOT, "release.sh")
    if not os.path.exists(script):
        return FAIL, "no release.sh -- releases would be built from the working tree"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rel.zip")
        p = subprocess.run(
            ["bash", script, "HEAD", out], capture_output=True, text=True, cwd=ROOT
        )
        if p.returncode != 0 or not os.path.exists(out):
            return FAIL, f"release build failed: {(p.stdout + p.stderr)[-200:]}"
        names = zipfile.ZipFile(out).namelist()
        leaks = [
            n
            for n in names
            if "/.git/" in n or "egg-info" in n or n.endswith((".pyc", ".env"))
        ]
        return verdict(
            not leaks,
            f"release archive: {len(names)} entries, no VCS or generated metadata",
            f"leaked entries: {leaks[:5]}",
        )


@case("MO-04", "Medium", "OAT", "Rendered outputs are deterministic")
def mo_04():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        a, b = pipeline.scan(fx), pipeline.scan(fx)
        ok = writers.render_json(a) == writers.render_json(b) and writers.render_markdown(
            a
        ) == writers.render_markdown(b)
        return verdict(
            ok, "JSON and Markdown byte-identical across runs", "outputs differ"
        )


@case("MO-05", "Medium", "OAT", "License is final for publishing")
def mo_05():
    text = read_text(os.path.join(ROOT, "LICENSE")).lower()
    markers = [
        w for w in ("placeholder", "replace it", "before publishing", "todo") if w in text
    ]
    return verdict(
        not markers,
        "LICENSE states a final position with no placeholder markers",
        f"placeholder markers present: {markers}",
    )


# ==========================================================================
# LOW -- edge
# ==========================================================================


@case("LE-01", "Low", "Edge", "Empty estate renders cleanly")
def le_01():
    e = Estate()
    j, m, c = (
        writers.render_json(e),
        writers.render_markdown(e),
        console.render(e, color=False),
    )
    ok = bool(json.loads(j)) and bool(m.strip()) and bool(c.strip())
    return verdict(
        ok, "empty estate renders valid JSON, Markdown and console", "render failed"
    )


@case("LE-02", "Low", "Edge", "Limit environment parsing is bounded")
def le_02():
    import importlib

    results = {}
    for raw in (None, "", "not-a-number", "-5", "0", "123"):
        if raw is None:
            os.environ.pop("DUSTPAN_MAX_FILE_BYTES", None)
        else:
            os.environ["DUSTPAN_MAX_FILE_BYTES"] = raw
        importlib.reload(limits)
        results[repr(raw)] = limits.MAX_FILE_BYTES
    os.environ.pop("DUSTPAN_MAX_FILE_BYTES", None)
    importlib.reload(limits)
    ok = (
        all(v >= 0 for v in results.values())
        and results["'123'"] == 123
        and results["'0'"] == 0  # 0 is the documented way to disable a budget
        and results["'-5'"] == limits._budget("__missing__", 16 * 1024 * 1024)
    )
    return verdict(ok, f"bounded for every input: {results}", f"{results}")


@case("LE-03", "Low", "Edge", "Extreme numeric exponents stay compact")
def le_03():
    start = time.perf_counter()
    r = normalise("1e100000 + a[b]")
    elapsed = time.perf_counter() - start
    ok = elapsed < 1.0 and len(r.normalised or "") < 500
    return verdict(
        ok,
        f"normalised in {elapsed:.4f}s, {len(r.normalised or '')} chars",
        f"elapsed={elapsed:.3f}s len={len(r.normalised or '')}",
    )


@case("LE-04", "Low", "Edge", "Duplicate visual IDs are stable")
def le_04():
    with tempfile.TemporaryDirectory() as tmp:
        rd = os.path.join(tmp, "Dup.Report")
        os.makedirs(rd)
        vis = {
            "name": "same",
            "singleVisual": {
                "visualType": "card",
                "projections": {"Values": [{"queryRef": "S.A"}]},
            },
        }
        dump_json(
            {
                "sections": [
                    {
                        "name": "s",
                        "displayName": "P",
                        "visualContainers": [
                            {"config": json.dumps(vis)},
                            {"config": json.dumps(vis)},
                        ],
                    }
                ]
            },
            os.path.join(rd, "report.json"),
        )
        e = pipeline.scan(tmp)
        ids = [v.id for a in e.assets for v in a.visuals]
        ok = len(ids) == len(set(ids))
        return verdict(ok, f"visual ids unique: {ids}", f"ids={ids}")


@case("LE-05", "Low", "Edge", "Depth boundary is inclusive")
def le_05():
    from dustpan.parsers.report import _MAX_DEPTH, _Collector, _scan_document

    node: object = {"queryRef": "S.Deep"}
    for _ in range(_MAX_DEPTH - 2):
        node = {"wrap": node}
    col = _Collector()
    _scan_document(node, col, "visual")
    ok = "Deep" in col.names and not col.depth_exceeded
    return verdict(
        ok,
        f"reference at depth {_MAX_DEPTH - 2} found without tripping the cap",
        f"names={col.names} exceeded={col.depth_exceeded}",
    )


# ==========================================================================
# LOW -- OAT
# ==========================================================================


@case("LO-01", "Low", "OAT", "Installed wheel passes pip check")
def lo_01():
    with tempfile.TemporaryDirectory() as tmp:
        b = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "-o", tmp],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=isolated_env(),
        )
        if b.returncode != 0:
            return FAIL, f"build failed: {b.stderr[-200:]}"
        wheel = next(f for f in os.listdir(tmp) if f.endswith(".whl"))
        venv = os.path.join(tmp, "v")
        subprocess.run(
            [sys.executable, "-m", "venv", venv], check=True, env=isolated_env()
        )
        py = venv_python(venv)
        subprocess.run(
            [py, "-m", "pip", "install", "--no-index", os.path.join(tmp, wheel)],
            capture_output=True,
            check=True,
            env=isolated_env(),
        )
        p = subprocess.run(
            [py, "-m", "pip", "check"], capture_output=True, text=True, env=isolated_env()
        )
        return verdict(p.returncode == 0, p.stdout.strip(), p.stdout.strip())


@case("LO-02", "Low", "OAT", "All JSON fixtures are valid")
def lo_02():
    bad = []
    count = 0
    for base, _dirs, files in os.walk(FIXTURES):
        for name in files:
            if name.endswith((".json", ".pbism", ".pbir")):
                count += 1
                path = os.path.join(base, name)
                try:
                    json.loads(read_text(path))
                except Exception as exc:
                    bad.append(f"{name}: {exc}")
    return verdict(not bad, f"all {count} JSON fixtures parse", f"invalid: {bad}")


@case("LO-03", "Low", "OAT", "Runtime dependency claim is true")
def lo_03():
    import ast

    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for base, _d, files in os.walk(os.path.join(SRC, "dustpan")):
        for name in files:
            if not name.endswith(".py"):
                continue
            tree = ast.parse(read_text(os.path.join(base, name)))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    if mod not in stdlib and mod != "dustpan":
                        offenders.append(f"{name}: {mod}")
    return verdict(
        not offenders,
        "runtime imports are stdlib + dustpan only",
        f"third-party: {offenders}",
    )


@case("LO-04", "Low", "OAT", "Output encoding/newlines are portable")
def lo_04():
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy_fixtures(os.path.join(tmp, "fx"))
        j = os.path.join(tmp, "o.json")
        m = os.path.join(tmp, "o.md")
        code, _ = cli(["scan", fx, "--json", j, "--markdown", m, "--quiet"])
        problems = []
        for path in (j, m):
            raw = Path(path).read_bytes()
            raw.decode("utf-8")
            if b"\r\n" in raw:
                problems.append(f"{os.path.basename(path)}: CRLF")
            if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
                problems.append(f"{os.path.basename(path)}: final newline")
        return verdict(
            code == 0 and not problems,
            "UTF-8, LF, exactly one final newline",
            f"{problems}",
        )


@case("LO-05", "Low", "OAT", "No embedded VCS repository, or a valid hook-free one")
def lo_05():
    import zipfile

    # The original case checked that an EMBEDDED repository was structurally
    # valid and had no active hooks. Shipping no repository at all satisfies
    # that intent strictly more completely, so absence passes.
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return PASS, "distributed tree embeds no repository at all"

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rel.zip")
        p = subprocess.run(
            ["bash", os.path.join(ROOT, "release.sh"), "HEAD", out],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if p.returncode != 0:
            return FAIL, f"release build failed: {(p.stdout + p.stderr)[-200:]}"
        git_entries = [n for n in zipfile.ZipFile(out).namelist() if "/.git/" in n]
        return verdict(
            not git_entries,
            "release archive embeds no repository at all",
            f"{len(git_entries)} .git entries present",
        )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

LEVELS = ["Critical", "High", "Medium", "Low"]


def run(selected: str | None = None) -> int:
    for c in CASES:
        if selected and c.cid != selected and c.cid != ID_PREFIX + selected:
            continue
        start = time.perf_counter()
        try:
            c.result, c.detail = c.fn()
        except Exception as exc:  # a harness failure is never a pass
            c.result, c.detail = FAIL, f"harness error: {type(exc).__name__}: {exc}"
        c.seconds = time.perf_counter() - start
        print(f"{c.result:4}  {c.cid}  {c.title}\n        {c.detail}")

    print("\nMatrix A pass/fail summary")
    print(
        f"{'Severity':<10}{'Tests':>7}{'Passed':>8}{'Failed':>8}{'Skipped':>9}{'Pass rate':>11}"
    )
    total = passed = failed = skipped = 0
    for level in LEVELS:
        group = [c for c in CASES if c.level == level and c.result]
        p = sum(1 for c in group if c.result == PASS)
        f = sum(1 for c in group if c.result == FAIL)
        s = sum(1 for c in group if c.result == SKIP)
        total, passed, failed, skipped = (
            total + len(group),
            passed + p,
            failed + f,
            skipped + s,
        )
        rate = f"{(p / len(group) * 100):.0f}%" if group else "-"
        print(f"{level:<10}{len(group):>7}{p:>8}{f:>8}{s:>9}{rate:>11}")
    rate = f"{(passed / total * 100):.0f}%" if total else "-"
    print(f"{'Total':<10}{total:>7}{passed:>8}{failed:>8}{skipped:>9}{rate:>11}")
    return 1 if (failed or skipped) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--case")
    args = ap.parse_args()
    code = run(args.case)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "id": c.cid,
                        "level": c.level,
                        "kind": c.kind,
                        "title": c.title,
                        "result": c.result,
                        "detail": c.detail,
                        "seconds": round(c.seconds, 6),
                    }
                    for c in CASES
                ],
                fh,
                indent=2,
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())

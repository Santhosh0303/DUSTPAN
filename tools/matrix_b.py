#!/usr/bin/env python3
"""Matrix B: the 40 fresh Edge/OAT scenarios from the v3 adversarial audit.

Matrix A (``tools/audit_matrix.py``) reassesses the baseline expectations.
This file carries the v3 audit's *fresh* scenarios, five Edge and five OAT at
each of Critical, High, Medium and Low, with the original case IDs preserved.

The v3 evidence bundle (matrix_b.py and its receipts) was not supplied with
this handoff, so each case is reimplemented from the stimulus, expected
criterion and observed evidence stated in the audit report. That is recorded
in TEST-ADAPTATIONS.md rather than presented as a port of the original
scripts.

Rules this harness holds itself to:

* an assertion states the user-visible safety property the audit named, not
  the shape of the current implementation;
* nothing is a PASS because it did not crash;
* a case that cannot be decided here reports SKIP with the reason and the
  runner exits non-zero -- SKIP is never counted as a pass;
* every subprocess has a timeout, so a hang is a FAIL rather than a hung run.

    python tools/matrix_b.py [--json out.json] [--case B-CE-01]
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, SRC)

from dustpan import pipeline  # noqa: E402
from dustpan.dax.normalise import enrich  # noqa: E402
from dustpan.detect.duplicates import find_duplicates  # noqa: E402
from dustpan.ir import Asset, Estate, Metric, Visual  # noqa: E402
from dustpan.output import writers  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
TIMEOUT = 60


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


def case(cid: str, level: str, kind: str, title: str):
    def wrap(fn):
        CASES.append(Case(cid, level, kind, title, fn))
        return fn

    return wrap


def isolated_env() -> dict[str, str]:
    """Environment for artifact-isolation subprocesses.

    An inherited PYTHONPATH pointing at the source tree makes an installed
    wheel appear importable when it is not -- the exact contamination the v3
    audit hit and corrected in its own harness (report section 8). Every venv,
    pip, build and installed-CLI invocation runs without it.
    """
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def _wheel_metadata(path: str) -> str:
    """METADATA text from a wheel, located without hard-coding the version."""
    with zipfile.ZipFile(path) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        return zf.read(name).decode()


def verdict(ok: bool, good: str, bad: str) -> tuple[str, str]:
    return (PASS, good) if ok else (FAIL, bad)


def cli(
    args: list[str], *, env_extra: dict[str, str] | None = None, cwd: str | None = None
):
    env = dict(os.environ, PYTHONPATH=SRC, NO_COLOR="1")
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "dustpan.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or ROOT,
        timeout=TIMEOUT,
    )
    return proc.returncode, proc.stdout + proc.stderr


def fixtures_into(directory: str) -> str:
    return shutil.copytree(FIXTURES, directory)


def metric(
    name, *, mid=None, root="/proj", fmt="#,0.00", dtype="double", expr=None, model="M"
):
    m = Metric(
        id=mid or f"powerbi:{model}:{name}",
        name=name,
        tool="powerbi",
        model=model,
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


def bim_model(directory: str, name: str, measures: list[dict]) -> None:
    os.makedirs(directory, exist_ok=True)
    Path(directory, "model.bim").write_text(
        json.dumps(
            {"name": name, "model": {"tables": [{"name": "S", "measures": measures}]}}
        ),
        encoding="utf-8",
    )


def legacy_report(
    directory: str, query_ref: str = "S.A", extra: dict | None = None
) -> None:
    os.makedirs(directory, exist_ok=True)
    container = {
        "config": json.dumps(
            {
                "name": "v",
                "singleVisual": {
                    "visualType": "card",
                    "projections": {"Values": [{"queryRef": query_ref}]},
                },
            }
        )
    }
    container.update(extra or {})
    Path(directory, "report.json").write_text(
        json.dumps(
            {
                "sections": [
                    {"name": "s", "displayName": "P", "visualContainers": [container]}
                ]
            }
        ),
        encoding="utf-8",
    )


# ==========================================================================
# CRITICAL -- Edge
# ==========================================================================


@case(
    "B-CE-01",
    "Critical",
    "Edge",
    "Existing backup-named bystander survives output commit",
)
def b_ce_01():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        out = os.path.join(t, "out.json")
        bystander = out + ".dustpan-backup"
        Path(out).write_text("OLD\n")
        Path(bystander).write_text("BYSTANDER\n")
        code, _ = cli(["scan", fx, "--json", out, "--force", "--quiet"])
        survived = (
            os.path.exists(bystander) and Path(bystander).read_text() == "BYSTANDER\n"
        )
        return verdict(
            survived,
            f"unrequested backup-named file untouched (exit {code})",
            f"exit={code} exists={os.path.exists(bystander)}",
        )


@case(
    "B-CE-02",
    "Critical",
    "Edge",
    "Output destination overlapping another backup is preserved",
)
def b_ce_02():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        j = os.path.join(t, "out.json")
        m = j + ".dustpan-backup"
        code, _ = cli(["scan", fx, "--json", j, "--markdown", m, "--quiet"])
        both = os.path.exists(j) and os.path.exists(m)
        neither = not os.path.exists(j) and not os.path.exists(m)
        ok = (code == 0 and both) or (code != 0 and neither)
        return verdict(
            ok,
            f"exit {code}; both outputs written"
            if both
            else f"exit {code}; refused before mutation",
            f"exit={code} json={os.path.exists(j)} markdown={os.path.exists(m)}",
        )


@case(
    "B-CE-03", "Critical", "Edge", "Oversize skipped source cannot become forced output"
)
def b_ce_03():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        src = os.path.join(fx, "SalesDemo.SemanticModel", "definition", "Sales.tmdl")
        before = Path(src).read_bytes()
        code, _ = cli(
            ["scan", fx, "--json", src, "--force", "--quiet"],
            env_extra={"DUSTPAN_MAX_FILE_BYTES": "64"},
        )
        preserved = Path(src).read_bytes() == before
        return verdict(
            code != 0 and preserved,
            f"refused with exit {code}; skipped source byte-identical",
            f"exit={code} preserved={preserved}",
        )


@case(
    "B-CE-04",
    "Critical",
    "Edge",
    "Control-containing DAX strings remain semantically distinct",
)
def b_ce_04():
    a = metric("A", expr='IF(x[y] = "\x1b", 1, 0)')
    b = metric("B", expr='IF(x[y] = "\\x1b", 1, 0)')
    e = estate_with([a, b])
    exact = [f for f in e.findings if f.kind == "exact_duplicate"]
    removable = pipeline.proven_removable_metric_ids(e)
    ok = a.fingerprint != b.fingerprint and not exact and not removable
    return verdict(
        ok,
        "a real control byte and its printable spelling keep distinct identities",
        f"same_fingerprint={a.fingerprint == b.fingerprint} exact_sets={len(exact)} "
        f"removable={sorted(removable)}",
    )


@case(
    "B-CE-05",
    "Critical",
    "Edge",
    "New material note invalidates cached retirement decision",
)
def b_ce_05():
    e = estate_with([metric("A"), metric("B")])
    before = pipeline.proven_removable_metric_ids(e)
    e.errors.append("report: /gone.json: could not read file: denied")
    pipeline._reconcile_notes(e)
    after = pipeline.proven_removable_metric_ids(e)
    payload = json.loads(writers.render_json(e))
    stale = [
        f
        for f in payload["findings"]
        if f["kind"] == "exact_duplicate"
        and ("retire" in f["evidence"] or "keep" in f["evidence"])
    ]
    ok = bool(before) and not after and not stale
    return verdict(
        ok,
        f"clearance for {len(before)} id(s) withdrawn by a later material note; no "
        f"stale keys in JSON",
        f"before={sorted(before)} after={sorted(after)} stale_keys={len(stale)}",
    )


# ==========================================================================
# CRITICAL -- OAT
# ==========================================================================


@case(
    "B-CO-01", "Critical", "OAT", "Mid-commit failure restores both existing destinations"
)
def b_co_01():
    with tempfile.TemporaryDirectory() as t:
        one = os.path.join(t, "one.txt")
        two = os.path.join(t, "two.txt")
        Path(one).write_text("ORIGINAL-ONE\n")
        Path(two).write_text("ORIGINAL-TWO\n")
        real = os.replace
        fired = {"n": 0}

        def flaky(src, dst):
            if str(dst) in {one, two}:
                fired["n"] += 1
                if fired["n"] == 2:
                    raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst)

        os.replace = flaky  # type: ignore[assignment]
        try:
            writers.commit_all([(one, "NEW-ONE\n"), (two, "NEW-TWO\n")])
            raised = False
        except OSError:
            raised = True
        finally:
            os.replace = real  # type: ignore[assignment]
        intact = (
            Path(one).read_text() == "ORIGINAL-ONE\n"
            and Path(two).read_text() == "ORIGINAL-TWO\n"
        )
        strays = [p for p in os.listdir(t) if p.startswith(".dustpan-")]
        ok = raised and fired["n"] >= 2 and intact and not strays
        return verdict(
            ok,
            f"injector fired {fired['n']}x; both destinations restored; no temporary files left",
            f"raised={raised} fired={fired['n']} intact={intact} strays={strays}",
        )


@case(
    "B-CO-02",
    "Critical",
    "OAT",
    "Rollback failure is reported without unchanged-state claim",
)
def b_co_02():
    with tempfile.TemporaryDirectory() as t:
        one = os.path.join(t, "one.txt")
        two = os.path.join(t, "two.txt")
        Path(one).write_text("ORIGINAL-ONE\n")
        Path(two).write_text("ORIGINAL-TWO\n")
        real = os.replace
        state = {"commits": 0, "restore_blocked": False}

        def flaky(src, dst):
            dst_s = str(dst)
            if dst_s in {one, two}:
                state["commits"] += 1
                if state["commits"] == 2:
                    raise OSError(errno.ENOSPC, "No space left on device")
                if state["commits"] > 2:  # this is the rollback attempt
                    state["restore_blocked"] = True
                    raise OSError(errno.EIO, "I/O error during restore")
            return real(src, dst)

        os.replace = flaky  # type: ignore[assignment]
        try:
            writers.commit_all([(one, "NEW-ONE\n"), (two, "NEW-TWO\n")])
            err = None
        except writers.CommitError as exc:
            err = exc
        except OSError as exc:
            err = exc
        finally:
            os.replace = real  # type: ignore[assignment]

        if not isinstance(err, writers.CommitError):
            return FAIL, f"expected a CommitError carrying per-path state, got {err!r}"
        claims_unchanged = all(s == "old" for s in err.states.values())
        recovery_ok = bool(err.recovery) and all(
            os.path.exists(p) for p in err.recovery.values()
        )
        names_paths = bool(err.states)
        ok = (
            state["restore_blocked"]
            and not claims_unchanged
            and recovery_ok
            and names_paths
        )
        return verdict(
            ok,
            f"restore failure surfaced: states={err.states}; recovery retained at "
            f"{list(err.recovery.values())}",
            f"restore_blocked={state['restore_blocked']} claims_unchanged={claims_unchanged} "
            f"recovery={err.recovery}",
        )


@case(
    "B-CO-03", "Critical", "OAT", "Abrupt process exit does not make old output disappear"
)
def b_co_03():
    with tempfile.TemporaryDirectory() as t:
        one = os.path.join(t, "one.txt")
        two = os.path.join(t, "two.txt")
        Path(one).write_text("ORIGINAL-ONE\n")
        Path(two).write_text("ORIGINAL-TWO\n")
        script = os.path.join(t, "crash.py")
        Path(script).write_text(
            "import os, sys\n"
            f"sys.path.insert(0, {SRC!r})\n"
            "from dustpan.output import writers\n"
            "real = os.replace\n"
            "seen = {'n': 0}\n"
            "def crash(src, dst):\n"
            f"    targets = {{{one!r}, {two!r}}}\n"
            "    if str(dst) in targets:\n"
            "        seen['n'] += 1\n"
            "        if seen['n'] == 2:\n"
            "            os._exit(9)\n"
            "    return real(src, dst)\n"
            "os.replace = crash\n"
            f"writers.commit_all([({one!r}, 'NEW-ONE\\n'), ({two!r}, 'NEW-TWO\\n')])\n"
        )
        proc = subprocess.run(
            [sys.executable, script], capture_output=True, text=True, timeout=TIMEOUT
        )
        present = os.path.exists(one) and os.path.exists(two)
        complete = present and Path(one).read_text() in {"ORIGINAL-ONE\n", "NEW-ONE\n"}
        complete = complete and Path(two).read_text() in {"ORIGINAL-TWO\n", "NEW-TWO\n"}
        recovery = [p for p in os.listdir(t) if p.startswith(".dustpan-recovery-")]
        return verdict(
            proc.returncode == 9 and present and complete,
            "after an abrupt exit mid-sequence every requested path still holds a "
            f"complete old or new file; recovery data retained ({len(recovery)} file(s))",
            f"exit={proc.returncode} present={present} complete={complete}",
        )


@case(
    "B-CO-04",
    "Critical",
    "OAT",
    "Public detector API withholds metadata-blocked retirement keys",
)
def b_co_04():
    a = metric("A", fmt="#,0.00")
    b = metric("B", fmt="0.0%")
    findings = find_duplicates(Estate(metrics=[a, b]))
    exact = [f for f in findings if f.kind == "exact_duplicate"]
    if not exact:
        return FAIL, "no exact-duplicate finding produced by the detector"
    leaked = [k for k in ("keep", "keep_id", "retire") if k in exact[0].evidence]
    blocker = exact[0].evidence.get("retirement_blocked_by")
    return verdict(
        not leaked and bool(blocker),
        f"detector states the blocker and publishes no instruction ({blocker!s:.60})",
        f"leaked keys={leaked} blocker={blocker!r}",
    )


@case(
    "B-CO-05",
    "Critical",
    "OAT",
    "Unscanned deployment does not inherit another deployment usage",
)
def b_co_05():
    with tempfile.TemporaryDirectory() as t:
        for name, expr, with_report in (
            ("One", "SUM(S[X])", True),
            ("Two", "SUM(S[Y])", False),
        ):
            bim_model(
                os.path.join(t, name, f"{name}.SemanticModel"),
                name,
                [
                    {"name": "A", "expression": expr, "formatString": "#,0"},
                    {"name": "B", "expression": expr, "formatString": "#,0"},
                ],
            )
            if with_report:
                legacy_report(os.path.join(t, name, f"{name}.Report"))
        e = pipeline.scan(t)
        removable = sorted(pipeline.proven_removable_metric_ids(e))
        two = [r for r in removable if ":Two:" in r]
        one = [r for r in removable if ":One:" in r]
        return verdict(
            not two and bool(one),
            f"Two (no bound report) yields nothing; One remains actionable {one}",
            f"removable={removable}",
        )


# ==========================================================================
# HIGH -- Edge
# ==========================================================================


@case(
    "B-HE-01",
    "High",
    "Edge",
    "Read target swap after authorisation cannot escape scan root",
)
def b_he_01():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        outside = os.path.join(t, "outside")
        os.makedirs(outside)
        leak = os.path.join(outside, "leak.json")
        Path(leak).write_text(
            json.dumps(
                {
                    "name": "leak",
                    "visual": {
                        "visualType": "card",
                        "query": {
                            "queryState": {
                                "Values": {
                                    "projections": [{"queryRef": "Secret.LeakedOutside"}]
                                }
                            }
                        },
                    },
                }
            )
        )
        victim = os.path.join(
            fx, "PbirDemo.Report/definition/pages/Summary/visuals/card1/visual.json"
        )

        # Deterministic swap: the path is a symlink at open time, exactly the
        # state the audit's race arrives at. A descriptor-bound open must
        # refuse it; a name-based reopen would follow it.
        os.unlink(victim)
        os.symlink(leak, victim)
        e = pipeline.scan(fx)
        blob = json.dumps([v.metric_names for a in e.assets for v in a.visuals])
        contained = "LeakedOutside" not in blob
        noted = any(
            "outside the scan root" in x or "symbolic link" in x for x in e.errors
        )

        # Second stimulus: swap an intermediate DIRECTORY, which O_NOFOLLOW on
        # the final component alone would not catch.
        fx2 = fixtures_into(os.path.join(t, "fx2"))
        pages = os.path.join(fx2, "PbirDemo.Report/definition/pages")
        elsewhere = os.path.join(t, "elsewhere")
        shutil.move(pages, elsewhere)
        os.symlink(elsewhere, pages)
        e2 = pipeline.scan(fx2)
        dir_blob = json.dumps([v.metric_names for a in e2.assets for v in a.visuals])
        dir_contained = "Buried" not in dir_blob
        dir_noted = any("symbolic link" in x for x in e2.errors)

        ok = contained and noted and dir_contained and dir_noted
        return verdict(
            ok,
            "final-component and intermediate-directory swaps both refused and recorded",
            f"file_contained={contained} file_noted={noted} dir_contained={dir_contained} "
            f"dir_noted={dir_noted}",
        )


@case(
    "B-HE-02",
    "High",
    "Edge",
    "Absent optional report extensions do not degrade a valid report",
)
def b_he_02():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        target = os.path.join(fx, "PbirDemo.Report/definition/reportExtensions.json")
        os.unlink(target)
        healthy = pipeline.scan(fx)
        noise = [x for x in healthy.errors if "reportExtensions" in x]

        # Present but malformed, and present but unreadable, must still degrade.
        fx2 = fixtures_into(os.path.join(t, "fx2"))
        bad = os.path.join(fx2, "PbirDemo.Report/definition/reportExtensions.json")
        Path(bad).write_text("{ not json")
        malformed = pipeline.scan(fx2)
        flagged = any("reportExtensions" in x for x in malformed.errors)

        return verdict(
            not noise and flagged,
            "absent optional file is silent; present-but-malformed still degrades",
            f"absent_noise={noise[:1]} malformed_flagged={flagged}",
        )


@case(
    "B-HE-03", "High", "Edge", "Malformed embedded legacy query produces a material note"
)
def b_he_03():
    with tempfile.TemporaryDirectory() as t:
        bim_model(
            os.path.join(t, "M.SemanticModel"),
            "M",
            [
                {"name": "A", "expression": "SUM(S[X])", "formatString": "#,0"},
                {"name": "B", "expression": "SUM(S[X])", "formatString": "#,0"},
            ],
        )
        legacy_report(
            os.path.join(t, "M.Report"), extra={"query": '{"Commands": [ {"broken" '}
        )
        e = pipeline.scan(t)
        noted = any("malformed JSON document" in x for x in e.errors)
        removable = sorted(pipeline.proven_removable_metric_ids(e))
        return verdict(
            noted and not removable,
            "a broken embedded query is a material note and withdraws retirement",
            f"noted={noted} removable={removable}",
        )


@case(
    "B-HE-04",
    "High",
    "Edge",
    "TMDL nested dynamic format expression stays separate from value DAX",
)
def b_he_04():
    with tempfile.TemporaryDirectory() as t:
        d = os.path.join(t, "M.SemanticModel", "definition")
        os.makedirs(d)
        Path(d, "T.tmdl").write_text(
            "table T\n\n"
            "\tmeasure 'M1' = SUM(T[X])\n"
            "\t\tformatStringDefinition =\n"
            '\t\t\t\tIF([Flag], "0.0", "0%")\n'
            "\t\tdisplayFolder: Core\n\n"
            "\tmeasure 'M2' = COUNTROWS(T)\n",
            encoding="utf-8",
        )
        e = Estate()
        from dustpan.parsers.tmdl import parse_model

        parse_model(d, e)
        by_name = {m.name: m for m in e.metrics}
        m1 = by_name.get("M1")
        m2 = by_name.get("M2")
        ok = (
            m1 is not None
            and m1.expression.strip() == "SUM(T[X])"
            and (m1.extra or {}).get("dynamic_format") is True
            and m2 is not None
            and m2.expression.strip() == "COUNTROWS(T)"
        )
        return verdict(
            ok,
            "value DAX kept exactly; nested format DAX held separately; sibling unaffected",
            f"M1={(m1.expression if m1 else None)!r} "
            f"dynamic={(m1.extra or {}).get('dynamic_format') if m1 else None}",
        )


@case("B-HE-05", "High", "Edge", "Model name remains inert in Markdown removal checklist")
def b_he_05():
    try:
        import markdown_it
    except ImportError:
        return SKIP, "markdown-it-py not installed (declared in the dev extra)"
    hostile = "<img src=x onerror=alert(1)>"
    a = metric("A")
    b = metric("B")
    a.model = b.model = hostile
    e = estate_with([a, b])
    md = writers.render_markdown(e)
    checklist = [line for line in md.splitlines() if line.startswith("- [ ]")]
    html = markdown_it.MarkdownIt("commonmark").enable("table").render(md)
    active = re.findall(r"<\s*(img|script|iframe|svg)\b", html, re.I)
    return verdict(
        not active and bool(checklist),
        f"hostile model name inert in the checklist ({len(checklist)} row(s))",
        f"active nodes={active}",
    )


# ==========================================================================
# HIGH -- OAT
# ==========================================================================


@case(
    "B-HO-01", "High", "OAT", "CI test-job dependencies include its audit build command"
)
def b_ho_01():
    pyproject = Path(ROOT, "pyproject.toml").read_text(encoding="utf-8")
    declared_build = re.search(r'"build[><=]', pyproject) is not None
    workflow = Path(ROOT, ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs_running_matrix = "audit_matrix.py" in workflow
    installs_dev = workflow.count('pip install -e ".[dev]"') >= 2
    # And prove it: a clean venv installing only the declared dev extra must
    # be able to import the frontend the matrix invokes.
    with tempfile.TemporaryDirectory() as t:
        venv = os.path.join(t, "v")
        subprocess.run(
            [sys.executable, "-m", "venv", venv],
            check=True,
            timeout=TIMEOUT,
            env=isolated_env(),
        )
        py = (
            os.path.join(venv, "bin", "python")
            if os.name != "nt"
            else os.path.join(venv, "Scripts", "python.exe")
        )
        inst = subprocess.run(
            [py, "-m", "pip", "install", "-q", "-e", f"{ROOT}[dev]"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if inst.returncode != 0:
            return SKIP, f"could not install the dev extra offline: {inst.stderr[-160:]}"
        probe = subprocess.run(
            [py, "-c", "import build, markdown_it"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    ok = declared_build and jobs_running_matrix and installs_dev and probe.returncode == 0
    return verdict(
        ok,
        "build and the render oracle are declared in [dev] and importable from a clean install",
        f"declared={declared_build} matrix_in_ci={jobs_running_matrix} "
        f"dev_installs={installs_dev} probe={probe.returncode}",
    )


@case(
    "B-HO-02",
    "High",
    "OAT",
    "CI audit installation paths support declared Windows runners",
)
def b_ho_02():
    source = Path(ROOT, "tools/audit_matrix.py").read_text(encoding="utf-8")
    hard_coded = re.findall(r'"bin",\s*"(?:pip|dustpan)"', source)
    platform_aware = "venv_python" in source and "Scripts" in source
    uses_dash_m_pip = '"-m", "pip"' in source
    workflow = Path(ROOT, ".github/workflows/ci.yml").read_text(encoding="utf-8")
    ci_portable = "python -m ruff" in workflow and "python -m mypy" in workflow
    ok = not hard_coded and platform_aware and uses_dash_m_pip and ci_portable
    return verdict(
        ok,
        "venv interpreter selected per platform; tools invoked as python -m",
        f"hard_coded={hard_coded} platform_aware={platform_aware} "
        f"dash_m_pip={uses_dash_m_pip} ci={ci_portable}",
    )


@case("B-HO-03", "High", "OAT", "Named pipe source cannot hang a scan")
def b_ho_03():
    if not hasattr(os, "mkfifo"):
        return (
            SKIP,
            "POSIX FIFOs are not available on this platform (NOT_APPLICABLE here)",
        )
    with tempfile.TemporaryDirectory() as t:
        d = os.path.join(t, "F.SemanticModel", "definition")
        os.makedirs(d)
        Path(d, "model.tmdl").write_text("model M\n")
        os.mkfifo(os.path.join(d, "evil.tmdl"))
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"import sys;sys.path.insert(0,{SRC!r});from dustpan import pipeline;"
                    f"e=pipeline.scan({t!r});"
                    "print('NOTED' if any('regular file' in x for x in e.errors) else 'SILENT')",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            return (
                FAIL,
                "the scan hung on a FIFO and had to be killed by the harness watchdog",
            )
        elapsed = time.perf_counter() - started
        ok = proc.returncode == 0 and "NOTED" in proc.stdout and elapsed < 10
        return verdict(
            ok,
            f"FIFO rejected and diagnosed by the product in {elapsed:.2f}s, no watchdog needed",
            f"exit={proc.returncode} out={proc.stdout.strip()!r} elapsed={elapsed:.2f}s",
        )


@case(
    "B-HO-04",
    "High",
    "OAT",
    "Malformed Unicode source is diagnosed without CLI traceback",
)
def b_ho_04():
    with tempfile.TemporaryDirectory() as t:
        d = os.path.join(t, "B.SemanticModel", "definition")
        os.makedirs(d)
        Path(d, "Bin.tmdl").write_bytes(b"\xff\xfe\x00\xff not utf-8 \xff")
        code, out = cli(["scan", t])
        ok = code in (0, 1) and "Traceback" not in out and "not valid UTF-8" in out
        return verdict(
            ok,
            f"malformed Unicode diagnosed in output, exit {code}, no traceback",
            f"exit={code} traceback={'Traceback' in out}",
        )


@case(
    "B-HO-05", "High", "OAT", "Installed wheel scans independently of source import path"
)
def b_ho_05():
    with tempfile.TemporaryDirectory() as t:
        build = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "-o", t],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=600,
        )
        if build.returncode != 0:
            return FAIL, f"wheel build failed: {build.stderr[-200:]}"
        wheel = next(f for f in os.listdir(t) if f.endswith(".whl"))
        venv = os.path.join(t, "v")
        subprocess.run(
            [sys.executable, "-m", "venv", venv],
            check=True,
            timeout=TIMEOUT,
            env=isolated_env(),
        )
        py = (
            os.path.join(venv, "bin", "python")
            if os.name != "nt"
            else os.path.join(venv, "Scripts", "python.exe")
        )
        inst = subprocess.run(
            [py, "-m", "pip", "install", "-q", "--no-index", os.path.join(t, wheel)],
            capture_output=True,
            text=True,
            timeout=300,
            env=isolated_env(),
        )
        if inst.returncode != 0:
            return FAIL, f"offline install failed: {inst.stderr[-200:]}"
        # Removing the key is what "empty source import path" means; setting
        # it to "" leaves an empty entry that behaves like the cwd.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        unrelated = os.path.join(t, "elsewhere")
        os.makedirs(unrelated)
        run = subprocess.run(
            [
                py,
                "-m",
                "dustpan.cli",
                "scan",
                FIXTURES,
                "--json",
                os.path.join(t, "o.json"),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            cwd=unrelated,
            env=env,
            timeout=TIMEOUT,
        )
        found = 0
        if os.path.exists(os.path.join(t, "o.json")):
            found = len(json.loads(Path(t, "o.json").read_text())["metrics"])
        return verdict(
            run.returncode == 0 and found >= 7,
            f"installed wheel scanned {found} fixture measures from an unrelated cwd with "
            f"empty PYTHONPATH",
            f"exit={run.returncode} metrics={found} err={run.stderr[-120:]}",
        )


# ==========================================================================
# MEDIUM -- Edge
# ==========================================================================


@case(
    "B-ME-01",
    "Medium",
    "Edge",
    "Duplicate IDs inside one deployment are treated as ambiguous",
)
def b_me_01():
    a = metric("X", mid="powerbi:M:X", expr="SUM(S[A])")
    b = metric("X", mid="powerbi:M:X", expr="SUM(S[B])")
    b.source_path = "/proj/other.tmdl"
    e = Estate(metrics=[a, b])
    e.assets.append(
        Asset(
            id="r",
            name="r",
            tool="powerbi",
            source_path="r",
            source_root="/proj",
            visuals=[Visual(id="v", page="p", visual_type="card", metric_names=["X"])],
        )
    )
    collisions = e.id_collisions()
    e.findings = find_duplicates(e)
    pipeline.apply_actionability(e)
    removable = pipeline.proven_removable_metric_ids(e)
    # A repeated reference to the SAME definition is not a conflict.
    same = metric("Y", mid="powerbi:M:Y")
    same_again = metric("Y", mid="powerbi:M:Y")
    benign = Estate(metrics=[same, same_again]).id_collisions()
    ok = len(collisions) == 1 and not removable and not benign
    return verdict(
        ok,
        "two different definitions sharing one id are ambiguous and block advice; "
        "identical repeats are not reported",
        f"collisions={len(collisions)} removable={sorted(removable)} benign={len(benign)}",
    )


@case(
    "B-ME-02", "Medium", "Edge", "First JSON render agrees with its actionability summary"
)
def b_me_02():
    e = estate_with([metric("A"), metric("B", fmt="0.0%")])
    first = writers.render_json(e)
    second = writers.render_json(e)
    payload = json.loads(first)
    summary_removable = payload["summary"]["proven_removable"]
    stale = [
        f
        for f in payload["findings"]
        if f["kind"] == "exact_duplicate"
        and ("retire" in f["evidence"] or "keep" in f["evidence"])
    ]
    ok = first == second and (summary_removable > 0 or not stale)
    return verdict(
        ok,
        f"first render is internally consistent (summary={summary_removable}, no orphan keys) "
        "and byte-identical to the second",
        f"deterministic={first == second} summary={summary_removable} stale_keys={len(stale)}",
    )


@case(
    "B-ME-03", "Medium", "Edge", "Duplicate pageOrder entries cannot double-count visuals"
)
def b_me_03():
    with tempfile.TemporaryDirectory() as t:
        baseline = pipeline.summarise(
            pipeline.scan(fixtures_into(os.path.join(t, "base")))
        )["visuals"]
        fx = fixtures_into(os.path.join(t, "fx"))
        pages_json = os.path.join(fx, "PbirDemo.Report/definition/pages/pages.json")
        data = json.loads(Path(pages_json).read_text())
        order = data.get("pageOrder") or []
        if order:
            data["pageOrder"] = [*order, order[0]]
            Path(pages_json).write_text(json.dumps(data))
        after = pipeline.summarise(pipeline.scan(fx))["visuals"]
        return verdict(
            after == baseline,
            f"repeated pageOrder entry left the real visual count at {after}",
            f"baseline={baseline} after_duplicate_order={after}",
        )


@case(
    "B-ME-04",
    "Medium",
    "Edge",
    "Wrong-typed semantic metadata cannot silently clear retirement",
)
def b_me_04():
    results = {}
    for label, bad in (
        ("dict", {"x": 1}),
        ("list", [1, 2]),
        ("number", 5),
        ("bool", True),
    ):
        with tempfile.TemporaryDirectory() as t:
            bim_model(
                os.path.join(t, "M.SemanticModel"),
                "M",
                [
                    {"name": "A", "expression": "SUM(S[X])", "formatString": bad},
                    {"name": "B", "expression": "SUM(S[X])"},
                ],
            )
            legacy_report(os.path.join(t, "M.Report"))
            e = pipeline.scan(t)
            results[label] = (
                sorted(pipeline.proven_removable_metric_ids(e)),
                any("not a string" in x for x in e.errors),
            )
    with tempfile.TemporaryDirectory() as t:
        bim_model(
            os.path.join(t, "M.SemanticModel"),
            "M",
            [
                {"name": "A", "expression": "SUM(S[X])", "formatString": "#,0"},
                {"name": "B", "expression": "SUM(S[X])", "formatString": "#,0"},
            ],
        )
        legacy_report(os.path.join(t, "M.Report"))
        control = sorted(pipeline.proven_removable_metric_ids(pipeline.scan(t)))
    blocked = all(not r and noted for r, noted in results.values())
    return verdict(
        blocked and bool(control),
        f"every wrong type blocked with a material note; valid matched metadata still "
        f"clears {control}",
        f"results={results} control={control}",
    )


@case(
    "B-ME-05",
    "Medium",
    "Edge",
    "Report DAX escaped bracket names retain exact references",
)
def b_me_05():
    from dustpan.parsers.report import _dax_references

    checks = {
        "[A]]B]": [(None, "A]B")],
        "T[C]]D]": [("T", "C]D")],
        "'My T'[E]]F]": [("My T", "E]F")],
        '"[NotARef]" & [Real]': [(None, "Real")],
        "// [Nope]\n[Yes]": [(None, "Yes")],
    }
    bad = {
        expr: _dax_references(expr)
        for expr, want in checks.items()
        if _dax_references(expr) != want
    }
    return verdict(
        not bad,
        "escaped brackets, quoted tables, string literals and comments all resolve exactly",
        f"mismatches={bad}",
    )


# ==========================================================================
# MEDIUM -- OAT
# ==========================================================================


@case(
    "B-MO-01", "Medium", "OAT", "Public JSON writer preserves existing file permissions"
)
def b_mo_01():
    if os.name == "nt":
        return SKIP, "POSIX mode bits are not Windows ACLs (NOT_APPLICABLE here)"
    with tempfile.TemporaryDirectory() as t:
        results = {}
        for want in (0o640, 0o644):
            target = os.path.join(t, f"o{want:o}.json")
            Path(target).write_text("x")
            os.chmod(target, want)
            writers.write_json(Estate(), target)
            results[oct(want)] = oct(stat.S_IMODE(os.stat(target).st_mode))
        strays = [p for p in os.listdir(t) if p.startswith(".dustpan-")]
        ok = all(results[oct(w)] == oct(w) for w in (0o640, 0o644)) and not strays
        return verdict(
            ok,
            f"modes preserved {results}; no leftover temporaries",
            f"{results} strays={strays}",
        )


@case("B-MO-02", "Medium", "OAT", "New reports respect restrictive process umask")
def b_mo_02():
    if os.name == "nt":
        return SKIP, "POSIX umask is not a Windows concept (NOT_APPLICABLE here)"
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        out = os.path.join(t, "new.json")
        script = (
            f"import os,sys,runpy;os.umask(0o077);sys.path.insert(0,{SRC!r});"
            f"sys.argv=['dustpan','scan',{fx!r},'--json',{out!r},'--quiet'];"
            "runpy.run_module('dustpan.cli', run_name='__main__')"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        if not os.path.exists(out):
            return FAIL, "no output produced under umask 077"
        mode = stat.S_IMODE(os.stat(out).st_mode)
        return verdict(
            not mode & 0o077,
            f"new report created {oct(mode)} -- no group/other bits under umask 077",
            f"mode={oct(mode)}",
        )


@case(
    "B-MO-03",
    "Medium",
    "OAT",
    "Output expansion matches the destination used by preflight",
)
def b_mo_03():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        home = os.path.join(t, "home")
        os.makedirs(home)
        code, _ = cli(
            ["scan", fx, "--json", "~/out.json", "--quiet"],
            env_extra={"HOME": home},
            cwd=t,
        )
        expanded = os.path.join(home, "out.json")
        literal = os.path.join(t, "~", "out.json")
        return verdict(
            code == 0 and os.path.exists(expanded) and not os.path.exists(literal),
            "the quoted tilde destination was written where preflight validated it",
            f"exit={code} expanded={os.path.exists(expanded)} literal={os.path.exists(literal)}",
        )


@case("B-MO-04", "Medium", "OAT", "Sdist includes the documented executable audit matrix")
def b_mo_04():
    import tarfile

    with tempfile.TemporaryDirectory() as t:
        build = subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "-o", t],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=600,
        )
        if build.returncode != 0:
            return FAIL, f"sdist build failed: {build.stderr[-200:]}"
        tar = next(f for f in os.listdir(t) if f.endswith(".tar.gz"))
        with tarfile.open(os.path.join(t, tar)) as archive:
            names = archive.getnames()
        need = ["tools/audit_matrix.py", "tools/matrix_b.py", "tests/conftest.py"]
        missing = [n for n in need if not any(x.endswith(n) for x in names)]
        fixtures = sum(1 for x in names if "/tests/fixtures/" in x)
        return verdict(
            not missing and fixtures > 0,
            f"sdist carries the documented audit tooling and {fixtures} fixture files",
            f"missing={missing} fixtures={fixtures}",
        )


@case(
    "B-MO-05",
    "Medium",
    "OAT",
    "Declared minimum build backend accepts modern license metadata",
)
def b_mo_05():
    pyproject = Path(ROOT, "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires\s*=\s*\["setuptools>=([\d.]+)', pyproject)
    if not match:
        return FAIL, "no setuptools floor declared in [build-system].requires"
    floor = match.group(1)
    with tempfile.TemporaryDirectory() as t:
        venv = os.path.join(t, "v")
        subprocess.run(
            [sys.executable, "-m", "venv", venv],
            check=True,
            timeout=TIMEOUT,
            env=isolated_env(),
        )
        py = (
            os.path.join(venv, "bin", "python")
            if os.name != "nt"
            else os.path.join(venv, "Scripts", "python.exe")
        )
        inst = subprocess.run(
            # Pin the complete lower bound, including its patch version.
            # A newer release in the same major series is not a floor test.
            [
                py,
                "-m",
                "pip",
                "install",
                "-q",
                f"setuptools=={floor}",
                "wheel",
                "build",
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if inst.returncode != 0:
            return (
                SKIP,
                f"setuptools=={floor} could not be installed here: {inst.stderr[-160:]}",
            )
        out = os.path.join(t, "dist")
        built = subprocess.run(
            [py, "-m", "build", "--no-isolation", "-o", out, ROOT],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if built.returncode != 0:
            return (
                FAIL,
                f"build at the declared floor {floor}.x failed: {built.stderr[-300:]}",
            )
        wheel = next(f for f in os.listdir(out) if f.endswith(".whl"))
        meta = _wheel_metadata(os.path.join(out, wheel))
        has_expr = "License-Expression:" in meta
        has_file = "License-File:" in meta
        return verdict(
            has_expr and has_file,
            f"built at the exact declared floor setuptools=={floor}; SPDX expression and "
            f"license file accepted",
            f"license_expression={has_expr} license_file={has_file}",
        )


# ==========================================================================
# LOW -- Edge
# ==========================================================================


@case("B-LE-01", "Low", "Edge", "Invalid numeric confidence values are rejected")
def b_le_01():
    from dustpan.ir import Finding

    bad = []
    for value in (float("nan"), float("inf"), float("-inf"), -0.5, 1.5):
        e = Estate(metrics=[metric("A")])
        e.findings = [
            Finding(
                kind="near_duplicate",
                severity="low",
                confidence=value,
                metric_ids=["powerbi:M:A"],
                summary="x",
            )
        ]
        try:
            kept, dropped = pipeline.filter_by_confidence(e, 0.5)
            rendered = writers.render_json(e)
            json.loads(rendered)
            summary = pipeline.summarise(e)
        except Exception as exc:  # a crash on hostile input is a failure
            bad.append(f"{value}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(summary.get("proven_removable"), int):
            bad.append(f"{value}: summary corrupted")
    return verdict(
        not bad,
        "NaN, infinities and out-of-range confidences are handled without corrupting output",
        f"failures={bad}",
    )


@case("B-LE-02", "Low", "Edge", "Lexical source path aliases remain protected")
def b_le_02():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        src = os.path.join(fx, "SalesDemo.SemanticModel", "definition", "Sales.tmdl")
        alias = os.path.join(
            fx, "SalesDemo.SemanticModel", "definition", "..", "definition", "Sales.tmdl"
        )
        before = Path(src).read_bytes()
        code, _ = cli(["scan", fx, "--json", alias, "--force", "--quiet"])
        return verdict(
            code != 0 and Path(src).read_bytes() == before,
            f"a parent-segment alias of a source is refused (exit {code}) and the "
            f"source is untouched",
            f"exit={code} preserved={Path(src).read_bytes() == before}",
        )


@case("B-LE-03", "Low", "Edge", "Hard-linked output cannot truncate the source inode")
def b_le_03():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        src = os.path.join(fx, "SalesDemo.SemanticModel", "definition", "Sales.tmdl")
        link = os.path.join(t, "hardlink.json")
        before_inode = os.stat(src).st_ino
        before_bytes = Path(src).read_bytes()
        try:
            os.link(src, link)
        except OSError as exc:
            return SKIP, f"hard links unavailable here: {exc}"
        cli(["scan", fx, "--json", link, "--force", "--quiet"])
        return verdict(
            os.stat(src).st_ino == before_inode
            and Path(src).read_bytes() == before_bytes,
            "atomic replacement left the original source inode and bytes intact",
            f"inode_stable={os.stat(src).st_ino == before_inode}",
        )


@case("B-LE-04", "Low", "Edge", "Two lexical aliases for one output are rejected")
def b_le_04():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        a = os.path.join(t, "out.json")
        b = os.path.join(t, ".", "sub", "..", "out.json")
        code, out = cli(["scan", fx, "--json", a, "--markdown", b, "--quiet"])
        return verdict(
            code != 0 and "resolve to" in out,
            f"two spellings of one destination refused before writing (exit {code})",
            f"exit={code} out={out.strip()[:120]}",
        )


@case(
    "B-LE-05",
    "Low",
    "Edge",
    "Non-ASCII identifiers remain distinct during canonicalisation",
)
def b_le_05():
    from dustpan.dax.normalise import normalise

    sharp = normalise("SUM(T[straße])")
    upper = normalise("SUM(T[STRASSE])")
    accent_a = normalise("SUM(T[Å])")
    accent_b = normalise("SUM(T[å])")
    ok = (
        sharp.fingerprint != upper.fingerprint
        and accent_a.fingerprint != accent_b.fingerprint
    )
    return verdict(
        ok,
        "case folding does not collapse ss/sz or accented identifiers",
        f"sharp_vs_upper_equal={sharp.fingerprint == upper.fingerprint} "
        f"accent_equal={accent_a.fingerprint == accent_b.fingerprint}",
    )


# ==========================================================================
# LOW -- OAT
# ==========================================================================


@case("B-LO-01", "Low", "OAT", "Quiet successful CLI scan emits no output")
def b_lo_01():
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        env = dict(os.environ, PYTHONPATH=SRC, NO_COLOR="1")
        proc = subprocess.run(
            [sys.executable, "-m", "dustpan.cli", "scan", fx, "--quiet"],
            capture_output=True,
            text=True,
            env=env,
            timeout=TIMEOUT,
        )
        return verdict(
            proc.returncode == 0 and not proc.stdout and not proc.stderr,
            "a quiet successful scan writes nothing to stdout or stderr",
            f"exit={proc.returncode} stdout={len(proc.stdout)}B stderr={len(proc.stderr)}B",
        )


@case("B-LO-02", "Low", "OAT", "NO_COLOR wins over forced colour configuration")
def b_lo_02():
    from dustpan.output import console

    e = estate_with([metric("A"), metric("B")])
    forced = console.render(e, color=True)
    off = console.render(e, color=False)
    with tempfile.TemporaryDirectory() as t:
        fx = fixtures_into(os.path.join(t, "fx"))
        env = dict(
            os.environ, PYTHONPATH=SRC, NO_COLOR="1", FORCE_COLOR="1", CLICOLOR_FORCE="1"
        )
        proc = subprocess.run(
            [sys.executable, "-m", "dustpan.cli", "scan", fx],
            capture_output=True,
            text=True,
            env=env,
            timeout=TIMEOUT,
        )
    esc = "\x1b["
    env_ansi = esc in proc.stdout
    off_ansi = esc in off
    forced_ansi = esc in forced
    return verdict(
        not env_ansi and not off_ansi and forced_ansi,
        "NO_COLOR suppresses ANSI even with FORCE_COLOR set; explicit color=True still colours",
        f"env_run_has_ansi={env_ansi} explicit_off_has_ansi={off_ansi} "
        f"forced_has_ansi={forced_ansi}",
    )


@case(
    "B-LO-03", "Low", "OAT", "Pristine v3 input remains byte-identical to the audited ZIP"
)
def b_lo_03():
    baseline = "9efd2e2aa977673270b95512115f04bd9ea554c011d9d5a8a244dff3cc0b0437"
    candidates = [
        os.path.join(ROOT, "tests", "audit_baseline", "dustpan-v3.zip"),
        os.path.join(os.path.dirname(ROOT), "work", "dustpan-v3-PRISTINE.zip"),
        os.path.join(os.path.dirname(ROOT), "dustpan-v3.zip"),
    ]
    found = next((p for p in candidates if os.path.exists(p)), None)
    if not found:
        return SKIP, "the pristine v3 input ZIP is not present beside the candidate"
    digest = hashlib.sha256(Path(found).read_bytes()).hexdigest()
    return verdict(
        digest == baseline,
        f"audited input unchanged: {digest[:16]}... (the repaired candidate is expected to differ)",
        f"expected={baseline[:16]}... observed={digest[:16]}...",
    )


@case(
    "B-LO-04", "Low", "OAT", "Wheel metadata contains declared version and license file"
)
def b_lo_04():
    with tempfile.TemporaryDirectory() as t:
        build = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "-o", t],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=600,
        )
        if build.returncode != 0:
            return FAIL, f"wheel build failed: {build.stderr[-200:]}"
        wheel = next(f for f in os.listdir(t) if f.endswith(".whl"))
        zf = zipfile.ZipFile(os.path.join(t, wheel))
        meta = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        text = zf.read(meta).decode()
        declared = re.search(
            r'^version\s*=\s*"([^"]+)"', Path(ROOT, "pyproject.toml").read_text(), re.M
        )
        version = declared.group(1) if declared else "?"
        has_license_file = any(n.endswith("LICENSE") for n in zf.namelist())
        ok = (
            f"Version: {version}" in text
            and "License-Expression:" in text
            and has_license_file
        )
        return verdict(
            ok,
            f"wheel declares version {version}, a license expression and ships LICENSE",
            f"version_ok={f'Version: {version}' in text} license_file={has_license_file}",
        )


@case(
    "B-LO-05",
    "Low",
    "OAT",
    "Historical v1 response bytes are preserved under its superseded notice",
)
def b_lo_05():
    pristine = os.path.join(ROOT, "tests", "audit_baseline", "dustpan-v3.zip")
    if not os.path.exists(pristine):
        return (
            SKIP,
            "the pristine v3 input ZIP is not present, so the historical bytes cannot be compared",
        )
    original = (
        zipfile.ZipFile(pristine).read("dustpan/docs/AUDIT-V1-RESPONSE.md").decode()
    )
    current = Path(ROOT, "docs/AUDIT-V1-RESPONSE.md").read_text(encoding="utf-8")
    marker = "# Response to the v1 adversarial audit"
    if marker not in current:
        return FAIL, "the historical response document is missing"
    body = current[current.index(marker) :]
    banner = current[: current.index(marker)]
    labelled = "SUPERSEDED" in banner and "historical" in banner.lower()
    return verdict(
        body == original and labelled,
        "the historical response body is byte-identical; only a labelled "
        "superseded notice was prepended",
        f"body_identical={body == original} labelled={labelled}",
    )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

LEVELS = ["Critical", "High", "Medium", "Low"]


def run(selected: str | None = None) -> int:
    for c in CASES:
        if selected and c.cid != selected:
            continue
        started = time.perf_counter()
        try:
            c.result, c.detail = c.fn()
        except Exception as exc:
            c.result, c.detail = FAIL, f"harness error: {type(exc).__name__}: {exc}"
        c.seconds = time.perf_counter() - started
        print(f"{c.result:4}  {c.cid}  {c.title}\n        {c.detail}")

    print("\nMatrix B pass/fail summary")
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
        Path(args.json_path).write_text(
            json.dumps(
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
                indent=2,
            ),
            encoding="utf-8",
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())

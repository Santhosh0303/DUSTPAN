"""File outputs: the machine-readable dump and the human write-up.

Both are deterministic -- no timestamps, sorted keys -- so two scans of an
unchanged estate produce byte-identical files. That makes the JSON safe to
commit and diff in CI, which is most of the point of having it.

Public surface (see CONTRACT.md):
    write_json(estate: Estate, path: str) -> None
    write_markdown(estate: Estate, path: str) -> None
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import stat
import tempfile
import textwrap
from typing import Any

from dustpan import pipeline
from dustpan.ir import IR_VERSION, Estate, Finding, Metric, display_copy, scrub

__all__ = ["render_markdown", "write_json", "write_markdown"]

_KIND_TITLES: dict[str, str] = {
    "exact_duplicate": "Exact duplicates",
    "near_duplicate": "Near-duplicate candidates",
    "unused_measure": "Unused measure candidates",
    "unparsed": "Unparsed expressions",
}

_KIND_NOTES: dict[str, str] = {
    "exact_duplicate": (
        "These expressions are identical once normalised. The claim is "
        "deterministic, not a guess."
    ),
    "near_duplicate": (
        "These resemble each other but are **not** proven equivalent. Read both "
        "expressions before changing anything."
    ),
    "unused_measure": (
        "No visual in the scanned reports references these. dustpan only sees what "
        "is under the scanned path -- confirm against other reports, apps and "
        "direct queries before removing."
    ),
    "unparsed": (
        "dustpan could not normalise these, so they took no part in the duplicate "
        "comparison. Their absence from the findings above proves nothing."
    ),
}


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def write_json(estate: Estate, path: str) -> None:
    """Dump the whole Estate as JSON: every metric, asset, finding and error,
    plus the same `summary` block the console and markdown reports are built
    from (see `pipeline.summarise`) -- so a machine reader gets the
    proven-removable / review-candidate split too, not just raw findings.
    """
    _write(path, render_json(estate))


def render_json(estate: Estate) -> str:
    """Render the JSON payload to a string without touching the filesystem.

    Kept separate from `write_json` so the CLI can render every requested
    output in memory and only then commit them -- a render that raises must
    never leave a half-written file behind.
    """
    # AUD-V3-004: decide over CURRENT state before snapshotting. The first
    # render used to serialise whatever the detector had left in evidence,
    # so a manually assembled estate produced a JSON payload carrying retire
    # keys beside a summary of zero -- and a second render disagreed with the
    # first.
    pipeline.apply_actionability(estate)
    payload = dataclasses.asdict(estate)
    payload["summary"] = pipeline.summarise(estate)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    return text + "\n"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def write_markdown(estate: Estate, path: str) -> None:
    """Write a report a user can paste straight into a ticket."""
    _write(path, render_markdown(estate))


def render_markdown(estate: Estate) -> str:
    # AUD-V3-003: Markdown is a display surface; JSON is not.
    stats = pipeline.summarise(estate)
    proven = pipeline.proven_removable_metric_ids(estate)
    candidates = pipeline.review_candidate_metric_ids(estate)
    estate = display_copy(estate)
    by_id = estate.metric_by_id()
    out: list[str] = ["# dustpan scan report", ""]

    root = _common_root(estate)
    if root:
        out += [f"Scanned {_code(root)}.", ""]

    out += _summary_table(stats)
    out += _confidence_note()
    out += _checklist(estate, by_id, proven, candidates)
    out += _findings(estate, by_id, stats)
    out += _notes(estate)
    out += _footer(stats)
    return "\n".join(out).rstrip() + "\n"


def _summary_table(stats: dict[str, Any]) -> list[str]:
    coverage = (
        f"{stats['parsed']}/{stats['metrics']} ({_pct(stats['coverage'])})"
        if stats["metrics"]
        else "n/a"
    )
    proven = str(stats["proven_removable"])
    if stats["metrics"]:
        proven += f" ({_pct(stats['proven_removable_pct'])} of the estate)"
    candidates = str(stats["review_candidates"])
    if stats["metrics"]:
        candidates += f" ({_pct(stats['review_candidates_pct'])} of the estate)"

    rows = [
        ("Measures scanned", str(stats["metrics"])),
        ("Semantic models", str(stats["models"])),
        ("Reports", str(stats["assets"])),
        ("Visuals inspected", str(stats["visuals"])),
        ("Exact duplicate sets", str(stats["exact_duplicate_sets"])),
        ("Near-duplicate candidates", str(stats["near_duplicate_sets"])),
        ("Unused candidates", str(stats["unused"])),
        ("Measures proven removable", proven),
        ("Candidates for review (not proven)", candidates),
        ("DAX coverage", coverage),
    ]
    out = ["## Summary", "", "| | |", "|---|---|"]
    out += [f"| {label} | {value} |" for label, value in rows]
    out.append("")
    if stats["degraded"]:
        out += [
            f"> **Degraded scan.** {stats['degraded']} analysis stage(s) did not run "
            "(see Scan notes). Every count above is a floor, not a total.",
            "",
        ]
    return out


def _confidence_note() -> list[str]:
    return [
        "> **How to read confidence.** `1.00` means the normalised expressions are "
        "identical -- a deterministic fact, not a judgement. Anything below `1.00` is "
        "a *candidate*: something for a human to read, never an instruction to delete. "
        "**Measures proven removable** and **candidates for review** above are never "
        "added together into one number: the first is a deterministic fact, the second "
        "is a reading list, and blending them would overstate how much is actually "
        "proven.",
        "",
    ]


def _checklist(
    estate: Estate, by_id: dict[str, Metric], proven: set[str], candidates: set[str]
) -> list[str]:
    if not proven and not candidates:
        return []

    reasons: dict[str, str] = {}
    for finding in pipeline.sort_findings(estate.findings):
        ids = list(finding.metric_ids)
        if finding.kind == "exact_duplicate" and float(finding.confidence or 0) >= 1.0:
            keeper = by_id.get(ids[0])
            keeper_name = keeper.name if keeper else ids[0]
            for mid in ids[1:]:
                reasons.setdefault(mid, f"identical to {_code(keeper_name)}")
        elif finding.kind == "unused_measure":
            for mid in ids:
                reasons.setdefault(mid, "no visual reference found -- confirm first")
        elif finding.kind == "near_duplicate":
            for mid in ids:
                reasons.setdefault(
                    mid, "resembles other measure(s) -- not proven equivalent"
                )

    def _rows(ids: set[str]) -> list[str]:
        rows: list[str] = []
        for mid in sorted(
            ids, key=lambda i: (by_id[i].model, by_id[i].name) if i in by_id else ("", i)
        ):
            metric = by_id.get(mid)
            name = metric.name if metric else mid
            # AUD-V3-010: the model name is prose here, outside the code span.
            model = f" ({_inline(metric.model)})" if metric and metric.model else ""
            reason = reasons.get(mid, "flagged by a detector")
            rows.append(f"- [ ] {_code(name)}{model} -- {reason}")
        return rows

    out = ["## Removal checklist", ""]
    if proven:
        out += [
            "### Proven removable",
            "",
            "Identical to another measure, byte-for-byte once normalised. Tick one "
            "off only once you have read its expression above.",
            "",
        ]
        out += _rows(proven)
        out.append("")
    if candidates:
        out += [
            "### Candidates for review",
            "",
            "Not proven -- either a near-duplicate resemblance or an absence of "
            "visual references, both of which could be wrong. Confirm before "
            "acting on any of these.",
            "",
        ]
        out += _rows(candidates)
        out.append("")
    return out


def _findings(
    estate: Estate, by_id: dict[str, Metric], stats: dict[str, Any]
) -> list[str]:
    if not estate.findings:
        note = (
            "No duplicate or unused measures were detected."
            if not stats["degraded"]
            else "No findings -- but stages of this scan did not run, so this is not a "
            "clean bill of health."
        )
        return ["## Findings", "", note, ""]

    grouped: dict[str, list[Finding]] = {}
    for finding in pipeline.sort_findings(estate.findings):
        grouped.setdefault(finding.kind, []).append(finding)

    kinds = [k for k in pipeline.KIND_ORDER if k in grouped]
    kinds += sorted(k for k in grouped if k not in pipeline.KIND_ORDER)

    out: list[str] = []
    for kind in kinds:
        findings = grouped[kind]
        title = _KIND_TITLES.get(kind, kind.replace("_", " ").capitalize())
        out += [f"## {title} ({len(findings)})", ""]
        kind_note = _KIND_NOTES.get(kind)
        if kind_note:
            out += [kind_note, ""]
        for index, finding in enumerate(findings, start=1):
            out += _finding(finding, index, kind, by_id)
    return out


def _finding(
    finding: Finding, index: int, kind: str, by_id: dict[str, Metric]
) -> list[str]:
    confidence = float(finding.confidence or 0.0)
    proven = confidence >= 1.0
    metrics = [by_id[mid] for mid in finding.metric_ids if mid in by_id]
    names = [m.name for m in metrics] or list(finding.metric_ids)

    # AUD-V1-007: the heading is an inline Markdown context built entirely
    # from scanned names, so it needs the same escaping as a table cell.
    out = [f"### {index}. {', '.join(_inline(n) for n in names)}", ""]

    label = (
        "proven identical"
        if proven and kind == "exact_duplicate"
        else ("observed" if proven else "candidate -- not proven")
    )
    out += [
        f"- **Severity:** {finding.severity}",
        f"- **Confidence:** {confidence:.2f} ({label})",
    ]

    shared = _shared(finding, metrics, "normalised", "normalised")
    if shared:
        out.append(f"- **Normalised form:** {_code(shared)}")
    fingerprint = _shared(finding, metrics, "fingerprint", "fingerprint")
    if fingerprint:
        out.append(f"- **Fingerprint:** {_code(fingerprint)}")
    out.append("")

    if finding.summary:
        out += [_summary_line(finding.summary, proven), ""]

    if len(metrics) > 1:
        out += [
            "| | Measure | Model | Display folder | Hidden | Source |",
            "|---|---|---|---|---|---|",
        ]
        for position, metric in enumerate(metrics):
            out.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    _tag(position, len(metrics)),
                    _cell(metric.name),
                    _cell(metric.model or "-"),
                    _cell(metric.display_folder or "-"),
                    "yes" if metric.is_hidden else "no",
                    _cell(os.path.basename(metric.source_path or "") or "-"),
                )
            )
        out.append("")
    elif metrics:
        metric = metrics[0]
        location = " / ".join(p for p in (metric.model, metric.display_folder) if p)
        out += [f"- **Defined in:** {_code(metric.source_path or '?')}"]
        if location:
            out.append(f"- **Location:** {_cell(location)}")
        out.append("")

    for position, metric in enumerate(metrics):
        # AUD-V1-007: table cells were escaped but headings and summaries
        # were not, so a measure named `<img src=x onerror=...>` rendered as
        # live HTML in a ticket that allows it.
        heading = (
            f"{_tag(position, len(metrics))} -- {_inline(metric.name)}"
            if len(metrics) > 1
            else _inline(metric.name)
        )
        out += [f"**{heading}**", "", "```dax", _expression(metric.expression), "```", ""]

    extra = _extra_evidence(finding)
    if extra:
        out += ["<details><summary>Detector evidence</summary>", ""]
        out += [f"- {_code(k)}: {_cell(_evidence_value(v))}" for k, v in extra]
        out += ["", "</details>", ""]

    if kind == "exact_duplicate" and proven and len(metrics) > 1:
        models = sorted({m.model for m in metrics if m.model})
        if len(models) > 1:
            # Independent semantic models -- "retire" does not apply across
            # this boundary. See detect/duplicates.py's cross-model note.
            out += [
                f"**Note:** this identical formula appears in {len(models)} different "
                f"semantic models ({', '.join(_cell(mo) for mo in models)}), each "
                "presumably feeding its own reports. Not a deletion candidate -- a "
                "signal to consider a shared/certified dataset instead.",
                "",
            ]
        elif not finding.evidence.get("retirement_cleared", False):
            reasons = finding.evidence.get("retirement_blockers") or []
            out += [
                "**Not cleared for retirement:** "
                + ("; ".join(_inline(r) for r in reasons) if reasons else "blocked")
                + ". The expressions are identical -- that is the finding. "
                "Consolidating them is a decision this scan cannot make for you.",
                "",
            ]
        else:
            # A measure name containing a backtick would otherwise terminate
            # the code span and inject whatever follows as live Markdown.
            keep = _code(metrics[0].name)
            drop = ", ".join(_code(m.name) for m in metrics[1:])
            out += [
                f"**Action:** keep {keep}. Repoint anything reading {drop}, then retire "
                f"them -- {len(metrics) - 1} measure(s) removable.",
                "",
            ]
            if finding.evidence.get("any_member_referenced") is False:
                out += [
                    "**Note:** no member of this set is referenced by any scanned "
                    f"visual -- {keep} was chosen arbitrarily for determinism, not "
                    "because of usage evidence. Confirm actual usage before acting.",
                    "",
                ]
    return out


def _summary_line(summary: str, proven: bool) -> str:
    summary = _inline(summary)
    text = " ".join(str(summary).split())
    return text if proven else f"*Candidate:* {text}"


def _tag(position: int, total: int) -> str:
    if total < 2:
        return ""
    letters = ""
    index = position + 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _expression(expression: str) -> str:
    text = textwrap.dedent((expression or "").replace("\t", "    ")).strip("\n")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    if "```" in text:  # never let an expression break out of the fence
        text = text.replace("```", "'''")
    return text or "(empty expression)"


# "metrics"/"metric" are per-metric structures (a list of dicts / a single
# dict) already rendered as the table above or the per-metric heading/code
# block below -- dumping them again here would print each as a raw Python
# dict repr rather than useful evidence.
_EVIDENCE_SKIP = {
    "normalised",
    "normalized",
    "shared_normalised",
    "expression",
    "fingerprint",
    "metrics",
    "metric",
}


def _extra_evidence(finding: Finding) -> list[tuple[str, Any]]:
    return [
        (k, v)
        for k, v in sorted((finding.evidence or {}).items())
        if k not in _EVIDENCE_SKIP and v not in (None, "", [], {})
    ]


def _evidence_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        joined = ", ".join(_inline(v) for v in items[:12])
        return joined + (" ..." if len(items) > 12 else "")
    return _inline(value)


def _shared(finding: Finding, metrics: list[Metric], key: str, attr: str) -> str | None:
    value = (finding.evidence or {}).get(key)
    if isinstance(value, str) and value.strip():
        return value
    values = {getattr(m, attr) for m in metrics if getattr(m, attr, None)}
    return values.pop() if len(values) == 1 else None


def _notes(estate: Estate) -> list[str]:
    if not estate.errors:
        return []
    out = [
        f"## Scan notes ({len(estate.errors)})",
        "",
        "Things dustpan could not do. Your estate may contain more than this report shows.",
        "",
    ]
    out += [f"- {_inline(error)}" for error in estate.errors]
    out.append("")
    return out


def _footer(stats: dict[str, Any]) -> list[str]:
    return [
        "---",
        "",
        f"Generated by dustpan {pipeline.version()} (IR v{IR_VERSION}). "
        "Re-run with `--json` for the full machine-readable dump.",
        "",
    ]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


# Characters that turn scanned text into active Markdown or HTML. Control
# bytes are already neutralised on the way into the IR (see `ir.scrub`); this
# is the second half of AUD-005 -- a measure named `<img src=x onerror=...>`
# or `[go](javascript:...)` must render as those characters, not run as them
# when the report is pasted into a ticket that allows inline HTML.
_MD_ESCAPE = str.maketrans(
    {
        "<": "&lt;",
        ">": "&gt;",
        "&": "&amp;",
        "[": "\\[",
        "]": "\\]",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
    }
)


def _code(value: Any) -> str:
    """Wrap arbitrary text in a Markdown code span that cannot be escaped.

    AUD-V3-010. CommonMark does not process backslash escapes inside a code
    span, so `_inline`'s `\\``-style escaping is inert there: a measure named
    ``a` <script>`` still closed the span and let the rest render as live
    Markdown. The spec's own mechanism is used instead -- a fence longer than
    the longest backtick run in the content, plus the single space of padding
    the spec strips again when the content starts or ends with a backtick.

    Newlines are folded to spaces because a code span cannot contain a blank
    line and a raw newline would break out of the construct.

    Reference: https://spec.commonmark.org/0.31.2/#code-spans
    """
    text = " ".join((scrub(str(value)) or "").split())
    if not text:
        return "``"
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _inline(value: Any) -> str:
    return " ".join((scrub(str(value)) or "").split()).translate(_MD_ESCAPE)


def _cell(value: Any) -> str:
    """Make a value safe to drop into a markdown table cell."""
    return _inline(value).replace("|", "\\|")


def _common_root(estate: Estate) -> str | None:
    paths = [m.source_path for m in estate.metrics if m.source_path]
    paths += [a.source_path for a in estate.assets if a.source_path]
    if not paths:
        return None
    try:
        root = os.path.commonpath([os.path.abspath(p) for p in paths])
    except ValueError:  # e.g. mixed drives on Windows
        return None
    return root if os.path.isdir(root) else os.path.dirname(root)


class CommitError(OSError):
    """A multi-output commit that did not fully succeed.

    Carries what is actually known about every destination rather than a
    blanket claim. AUD-V3-006: the CLI used to print "every destination is
    unchanged" after a failure that had in fact left the first destination
    absent.
    """

    def __init__(
        self,
        message: str,
        *,
        states: dict[str, str],
        recovery: dict[str, str],
        mapping: str | None = None,
    ) -> None:
        super().__init__(message)
        #: destination -> "old" | "new" | "absent" | "uncertain"
        self.states = states
        #: destination -> retained recovery snapshot holding its previous bytes
        self.recovery = recovery
        #: path of the record naming this invocation's owned files, if retained
        self.mapping = mapping


def commit_all(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Write several (path, text) outputs as one operation.

    Returns destination -> "old"/"new" for a full success. Raises
    `CommitError` otherwise, carrying per-destination state and the recovery
    paths that still hold the previous bytes.

    The design follows the audited fault model rather than claiming more:

    * **Snapshots are copies, never renames** (AUD-V3-006). The original stays
      at its requested path until `os.replace` swaps a complete new file over
      it, so an abrupt exit at any instant leaves a complete old *or* new
      file. Moving the original aside first is what made a requested path
      vanish when a process died between the two renames.
    * **Recovery files are unpredictable and exclusively created**
      (AUD-V3-001). `destination + ".dustpan-backup"` is a name a user's own
      file can already occupy; `mkstemp` cannot collide, and nothing this
      invocation did not create is ever deleted.
    * **Two renames are not a transaction.** Across an abrupt exit each file
      is individually old or new; they are not guaranteed to be all-old or
      all-new. That boundary is stated in CONTRACT.md rather than papered over.
    """
    resolved = [
        (os.path.normcase(os.path.realpath(os.path.expanduser(p))), t) for p, t in pairs
    ]
    if len({path for path, _ in resolved}) != len(resolved):
        raise OSError("multiple outputs resolve to the same destination")
    existed = {path: os.path.exists(path) for path, _ in resolved}

    owned_temps: list[str] = []
    staged: list[tuple[str, str]] = []
    recovery: dict[str, str] = {}
    states: dict[str, str] = {
        path: ("old" if existed[path] else "absent") for path, _ in resolved
    }
    mapping_path: str | None = None
    committed: list[str] = []

    try:
        for path, text in resolved:
            tmp = _stage(path, text)
            owned_temps.append(tmp)
            staged.append((path, tmp))

        for path, _tmp in staged:
            if existed[path]:
                snapshot = _snapshot(path)
                owned_temps.append(snapshot)
                recovery[path] = snapshot

        if recovery:
            mapping_path = _write_mapping(recovery)
            owned_temps.append(mapping_path)

        for path, tmp in staged:
            os.replace(tmp, path)
            owned_temps.remove(tmp)
            committed.append(path)
            states[path] = "new"
            _fsync_dir(os.path.dirname(path))
    except BaseException as exc:
        failures = _rollback(committed, existed, recovery, states)
        if failures:
            raise CommitError(
                f"{exc}; and restoring {len(failures)} destination(s) also failed",
                states=states,
                recovery={p: r for p, r in recovery.items() if os.path.exists(r)},
                mapping=mapping_path
                if mapping_path and os.path.exists(mapping_path)
                else None,
            ) from exc
        _cleanup(owned_temps)
        if isinstance(exc, OSError):
            raise CommitError(str(exc), states=states, recovery={}, mapping=None) from exc
        raise
    else:
        _cleanup(owned_temps)
        return states


def _rollback(
    committed: list[str],
    existed: dict[str, bool],
    recovery: dict[str, str],
    states: dict[str, str],
) -> list[str]:
    """Put every destination back. Returns the paths that could not be restored."""
    failures: list[str] = []
    for path in reversed(committed):
        try:
            if existed[path]:
                snapshot = recovery.get(path)
                if snapshot and os.path.exists(snapshot):
                    # Copy back rather than move, so the snapshot survives a
                    # second failure and stays available to the operator.
                    restored = _stage(path, None, source=snapshot)
                    os.replace(restored, path)
                    states[path] = "old"
                else:
                    states[path] = "uncertain"
                    failures.append(path)
            else:
                # Only remove an output this invocation created.
                os.unlink(path)
                states[path] = "absent"
        except OSError:
            states[path] = "uncertain"
            failures.append(path)
    return failures


def _cleanup(paths: list[str]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _stage(path: str, text: str | None, *, source: str | None = None) -> str:
    """Write content to an exclusively created temporary sibling of `path`."""
    parent = os.path.dirname(os.path.abspath(path)) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dustpan-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            if source is not None:
                with open(source, "rb") as src:
                    shutil.copyfileobj(src, handle)
            else:
                handle.write((text or "").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _apply_mode(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _apply_mode(tmp: str, destination: str) -> None:
    """Preserve an existing destination's mode; otherwise stay private.

    AUD-V3-019: a new report was chmodded to 0644 unconditionally, adding
    group/other read bits under `umask 077`. AUD-V3-020: the public writers
    did the opposite and tightened an existing 0640 file to 0600. New files
    keep `mkstemp`'s private 0600; existing files keep exactly the bits they
    had. Neither case consults or mutates the process-global umask.
    """
    try:
        existing = stat.S_IMODE(os.stat(destination).st_mode)
    except OSError:
        return  # new file: mkstemp's 0600 stands
    with contextlib.suppress(OSError):
        os.chmod(tmp, existing)


def _snapshot(path: str) -> str:
    """Copy an existing destination to an exclusive recovery file.

    A copy, not a rename: the original must never leave its requested path
    (AUD-V3-006), and the recovery name must be one no bystander can already
    hold (AUD-V3-001).
    """
    parent = os.path.dirname(os.path.abspath(path)) or os.curdir
    fd, snap = tempfile.mkstemp(prefix=".dustpan-recovery-", suffix=".bak", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle, open(path, "rb") as src:
            shutil.copyfileobj(src, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(snap)
        raise
    return snap


def _write_mapping(recovery: dict[str, str]) -> str:
    """Record which recovery file belongs to which destination.

    §4.6 of the repair contract: unpredictable recovery names cannot identify
    their target from the filename alone, so a minimal private record names
    only this invocation's own files. It is data for a human, not a replay
    engine -- dustpan never restores anything on startup.
    """
    parent = os.path.dirname(next(iter(recovery))) or os.curdir
    fd, path = tempfile.mkstemp(prefix=".dustpan-recovery-", suffix=".json", dir=parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "tool": "dustpan",
                "pid": os.getpid(),
                "note": "recovery snapshots of files this invocation replaced",
                "destinations": recovery,
            },
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _fsync_dir(path: str) -> None:
    """Best-effort directory fsync so a rename survives a crash."""
    if not path:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write(path: str, text: str) -> None:
    """Single-destination write, on exactly the same primitive as the CLI.

    AUD-V3-020: `write_json` and `write_markdown` had their own subtly
    different implementation, which is how one gained mode preservation and
    directory fsync while the other did not.
    """
    commit_all([(path, text)])

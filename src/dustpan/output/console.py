"""Terminal report.

Plain ASCII and ANSI SGR codes only -- no third-party libraries, nothing
that assumes a modern terminal. Colour turns itself off when the stream is
not a TTY, when NO_COLOR is set, when TERM=dumb, and on Windows consoles
that will not accept virtual-terminal sequences.

The one rule this renderer will not bend: a finding below confidence 1.0 is
never presented in language that sounds settled. Sub-1.0 findings are
labelled CANDIDATE and carry an explicit "not proven" caveat, because a
false positive that makes someone delete a working measure is the failure
mode that kills this tool.

Public surface (see CONTRACT.md):
    render(estate: Estate) -> str
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from typing import Any, TextIO

from dustpan import pipeline
from dustpan.ir import Estate, Finding, Metric, display_copy, scrub

__all__ = ["colour_enabled", "render"]

MIN_WIDTH = 60
MAX_WIDTH = 100

# Presentation per finding kind: heading, one-line subtitle.
# Why a sub-1.0 finding of this kind is not a delete order. Kind-specific,
# because "not proven equivalent" is nonsense for an unused-measure claim.
_CAVEATS: dict[str, str] = {
    "exact_duplicate": (
        "Not proven identical -- dustpan is flagging this for a human to read, "
        "not recommending a deletion."
    ),
    "near_duplicate": (
        "Not proven equivalent. These expressions merely resemble each other; "
        "read both in full before touching either."
    ),
    "unused_measure": (
        "Possible, not certain: dustpan only sees the reports under the path you "
        "scanned. A measure used by another report, an app, a paginated report or "
        "a direct query will look unused here."
    ),
}
_CAVEAT_DEFAULT = (
    "Not proven -- flagged for a human to review, not a recommendation to delete."
)

_SECTIONS: dict[str, tuple[str, str]] = {
    "exact_duplicate": (
        "EXACT DUPLICATES",
        "byte-for-byte identical once normalised -- consolidation candidates. "
        "Identical DAX proves the numbers match, not that the measures are "
        "interchangeable; see each finding for whether retirement is cleared.",
    ),
    "near_duplicate": (
        "NEAR-DUPLICATE CANDIDATES",
        "similar shape, NOT proven equivalent -- read both before acting",
    ),
    "unused_measure": (
        "UNUSED MEASURE CANDIDATES",
        "no visual in the scanned reports references these",
    ),
    "unparsed": (
        "UNPARSED EXPRESSIONS",
        "dustpan could not normalise these, so they sat out the comparison",
    ),
}


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------


class _Style:
    """ANSI SGR wrappers that collapse to identity when colour is off."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def blue(self, t: str) -> str:
        return self._wrap("36", t)

    def heading(self, t: str) -> str:
        return self._wrap("1;37", t)

    def severity(self, sev: str, t: str) -> str:
        return {"high": self.red, "medium": self.yellow}.get(sev, self.blue)(t)


def _enable_windows_vt() -> bool:
    """Ask a Windows console to interpret ANSI. True if it will."""
    if sys.platform != "win32":  # pragma: no cover - platform specific
        return True
    try:  # pragma: no cover - platform specific
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # pragma: no cover - platform specific
        return False


def colour_enabled(stream: TextIO | None = None) -> bool:
    """Whether ANSI colour is appropriate for `stream` right now."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return _enable_windows_vt()


def _terminal_width(explicit: int | None) -> int:
    if explicit:
        return max(MIN_WIDTH, min(MAX_WIDTH, explicit))
    try:
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        cols = 80
    return max(MIN_WIDTH, min(MAX_WIDTH, cols))


# --------------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------------


def _bar(confidence: float, cells: int = 10) -> str:
    filled = max(0, min(cells, round(float(confidence) * cells)))
    return "[" + "#" * filled + "-" * (cells - filled) + "]"


def _label(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def _expression_lines(expression: str, max_lines: int = 20) -> list[str]:
    text = textwrap.dedent((expression or "").replace("\t", "    ")).strip("\n")
    lines = [ln.rstrip() for ln in text.splitlines()] or ["(empty expression)"]
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = [*lines[:max_lines], f"... ({_plural(hidden, 'more line')})"]
    return lines


def _wrap(text: str, width: int, indent: str) -> list[str]:
    body = " ".join(str(text).split())
    if not body:
        return []
    return textwrap.wrap(
        body,
        width=max(20, width - len(indent)),
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _short(value: Any, limit: int = 68) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _metric_location(metric: Metric) -> str:
    path = os.path.basename(metric.source_path or "") or metric.source_path or "?"
    bits = [p for p in (metric.model, metric.display_folder) if p]
    return f"{path}" + (f"  ({' / '.join(bits)})" if bits else "")


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def render(estate: Estate, *, color: bool | None = None, width: int | None = None) -> str:
    """Render a scan as a terminal report.

    `color` and `width` are optional overrides; with neither, colour is
    auto-detected from stdout and width from the terminal.
    """
    # AUD-V3-003: escape at the display boundary, on a copy, so the analysed
    # estate keeps its exact source bytes.
    stats = pipeline.summarise(estate)
    estate = display_copy(estate)
    st = _Style(colour_enabled() if color is None else bool(color))
    w = _terminal_width(width)

    out: list[str] = []
    out += _header(st, w, stats)
    out += _summary(st, w, stats)

    if not estate.metrics and not estate.errors:
        out += _nothing_found(st, w)
        return "\n".join(out) + "\n"

    out += _findings(estate, st, w, stats)
    out += _notes(estate, st, w)
    out += _footer(st, w, stats)
    return "\n".join(_squeeze(out)) + "\n"


def _squeeze(lines: list[str]) -> list[str]:
    """Collapse runs of blank lines to a single blank."""
    out: list[str] = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return out


def _header(st: _Style, w: int, stats: dict[str, Any]) -> list[str]:
    title = f"dustpan {pipeline.version()}  --  scan report"
    return ["", st.bold(title), st.dim("=" * w), ""]


def _summary(st: _Style, w: int, stats: dict[str, Any]) -> list[str]:
    line1 = "  " + "  |  ".join(
        [
            st.bold(_plural(stats["metrics"], "measure")),
            _plural(stats["models"], "model"),
            _plural(stats["assets"], "report"),
            _plural(stats["visuals"], "visual"),
        ]
    )

    dup = stats["exact_duplicate_sets"]
    near = stats["near_duplicate_sets"]
    unused = stats["unused"]
    parts: list[str] = []
    parts.append((st.red if dup else st.green)(_plural(dup, "exact duplicate set")))
    if near:
        parts.append(st.yellow(_plural(near, "near-duplicate candidate")))
    if unused:
        parts.append(st.yellow(_plural(unused, "unused candidate")))
    line2 = "  " + "  |  ".join(parts)

    # Two numbers that never merge: one is a deterministic fact, the other
    # is a reading list. Adding them into one "N removable" headline is
    # exactly the false-precision bug this split exists to prevent.
    proven = stats["proven_removable"]
    candidates = stats["review_candidates"]
    coverage = (
        f"DAX coverage {stats['parsed']}/{stats['metrics']} ({_pct(stats['coverage'])})"
        if stats["metrics"]
        else "DAX coverage n/a"
    )
    proven_text = _plural(proven, "measure proven removable", "measures proven removable")
    if stats["metrics"]:
        proven_text += f" ({_pct(stats['proven_removable_pct'])})"
    candidates_text = _plural(candidates, "candidate for review", "candidates for review")
    if stats["metrics"]:
        candidates_text += f" ({_pct(stats['review_candidates_pct'])})"
    line3 = "  " + "  |  ".join(
        [
            (st.bold if proven else st.dim)(proven_text),
            (st.yellow if candidates else st.dim)(candidates_text),
        ]
    )
    line4 = "  " + st.dim(coverage)

    lines = [line1, line2, line3, line4, ""]

    if stats["degraded"]:
        warning = _wrap(
            f"! degraded scan: {_plural(stats['degraded'], 'analysis stage')} did not "
            "run (see SCAN NOTES below). The counts above are a floor, not a total.",
            w,
            "  ",
        )
        lines += [st.yellow(line) for line in warning] + [""]
    return lines


def _nothing_found(st: _Style, w: int) -> list[str]:
    return [
        st.dim("  No measures found under that path."),
        "",
        st.dim("  dustpan looks for:"),
        st.dim("    *.SemanticModel/  or  *.Dataset/   (PBIP, TMDL definitions)"),
        st.dim("    model.bim                          (TMSL)"),
        st.dim("    *.Report/report.json               (to see what is used)"),
        "",
        st.dim("  Point it at the folder containing your .pbip file."),
        "",
    ]


def _findings(estate: Estate, st: _Style, w: int, stats: dict[str, Any]) -> list[str]:
    if not estate.findings:
        if stats["degraded"]:
            return [
                *_wrap(
                    "No findings -- but stages of this scan did not run, so this is not "
                    "a clean bill of health. Fix the SCAN NOTES below and re-run.",
                    w,
                    "  ",
                ),
                "",
            ]
        return [
            st.green("  Nothing to remove. No duplicate or unused measures detected."),
            "",
        ]

    by_id = estate.metric_by_id()
    grouped: dict[str, list[Finding]] = {}
    for finding in pipeline.sort_findings(estate.findings):
        grouped.setdefault(finding.kind, []).append(finding)

    ordered_kinds = [k for k in pipeline.KIND_ORDER if k in grouped]
    ordered_kinds += sorted(k for k in grouped if k not in pipeline.KIND_ORDER)

    out: list[str] = []
    for kind in ordered_kinds:
        findings = grouped[kind]
        heading, subtitle = _SECTIONS.get(
            kind, (kind.replace("_", " ").upper(), "reported by a detector")
        )
        out.append(st.heading(f"{heading}  ({len(findings)})"))
        out.append(st.dim(f"  {subtitle}"))
        out.append(st.dim("-" * w))
        out.append("")
        for index, finding in enumerate(findings, start=1):
            out += _finding(finding, index, kind, by_id, st, w)
        out.append("")
    return out


def _finding(
    finding: Finding,
    index: int,
    kind: str,
    by_id: dict[str, Metric],
    st: _Style,
    w: int,
) -> list[str]:
    confidence = float(finding.confidence or 0.0)
    proven = confidence >= 1.0
    sev = str(finding.severity)

    if not proven:
        verdict = st.yellow("CANDIDATE")
    elif kind == "unparsed":
        verdict = st.blue("OBSERVED")
    else:
        verdict = st.green("PROVEN")
    conf = f"confidence {confidence:.2f} {_bar(confidence)}"
    head = f"  [{index}] {st.severity(sev, sev.upper())}  {verdict}  " + (
        st.green(conf) if proven else st.yellow(conf)
    )
    out = [head]

    out += _wrap(finding.summary, w, "      ")
    if not proven:
        out += _wrap(_CAVEATS.get(kind, _CAVEAT_DEFAULT), w, "      ")
    out.append("")

    metrics = [by_id[mid] for mid in finding.metric_ids if mid in by_id]
    missing = [mid for mid in finding.metric_ids if mid not in by_id]

    if kind in ("exact_duplicate", "near_duplicate") and len(metrics) > 1:
        out += _comparison(finding, metrics, st, w, proven)
    elif metrics:
        out += _single(metrics, st, w)

    for mid in missing:
        out.append(st.dim(f"      (metric id not in this estate: {mid})"))

    out += _evidence(finding, st, w)

    if kind == "exact_duplicate" and proven and len(metrics) > 1:
        models = sorted({m.model for m in metrics if m.model})
        if len(models) > 1:
            # Independent semantic models -- "retire" does not apply across
            # this boundary. See detect/duplicates.py's cross-model note.
            out += _wrap(
                f"Note: this identical formula appears in {len(models)} different "
                f"semantic models ({', '.join(models)}), each presumably feeding its "
                "own reports. Not a deletion candidate -- a signal to consider a "
                "shared/certified dataset instead.",
                w,
                "      ",
            )
        elif not finding.evidence.get("retirement_cleared", False):
            # AUD-V1-001: the renderer does not decide this. `pipeline.
            # apply_actionability` is the single authority; printing an action
            # here whenever confidence was 1.0 is exactly how a set the
            # detector had already blocked still told the user to delete it.
            reasons = finding.evidence.get("retirement_blockers") or []
            out += _wrap(
                "Not cleared for retirement: "
                + ("; ".join(str(r) for r in reasons) if reasons else "blocked")
                + ". The expressions are identical -- that is the finding. "
                "Consolidating them is a decision this scan cannot make for you.",
                w,
                "      ",
            )
        else:
            keep = f"{_label(0)} ({metrics[0].name})"
            drop = ", ".join(_label(i) for i in range(1, len(metrics)))
            out += _wrap(
                f"Action: keep {keep}. Repoint consumers of {drop}, then retire them "
                f"-- {_plural(len(metrics) - 1, 'measure')} removable.",
                w,
                "      ",
            )
            if finding.evidence.get("any_member_referenced") is False:
                out += _wrap(
                    "No member of this set is referenced by any scanned visual -- "
                    f"{_label(0)} was chosen arbitrarily for determinism, not because "
                    "of usage evidence. Confirm actual usage before acting.",
                    w,
                    "      ",
                )
        out.append("")
    return out


_NAME_CAP = 26
_FOLDER_CAP = 18
_SOURCE_CAP = 22


def _clip(text: str, cap: int) -> str:
    text = text or "-"
    return text if len(text) <= cap else text[: max(1, cap - 3)] + "..."


def _metrics_table(metrics: list[Metric], st: _Style, w: int) -> list[str]:
    """A small aligned table: measure / folder / hidden / source file.

    This is the fact a human actually checks a duplicate/candidate claim
    against, so it replaces dumping ``evidence["metrics"]``/``["metric"]``
    as a raw Python dict repr -- which used to wrap mid-token across lines
    and defeated the entire point of printing evidence: letting a human
    verify the claim by eye. The full structure is still in the JSON
    output (see output/writers.py); this is only the console rendering.
    """
    rows = [
        (
            _label(position),
            _clip(m.name or "?", _NAME_CAP),
            _clip(m.display_folder or "-", _FOLDER_CAP),
            "yes" if m.is_hidden else "no",
            _clip(
                os.path.basename(m.source_path or "") or (m.source_path or "?"),
                _SOURCE_CAP,
            ),
        )
        for position, m in enumerate(metrics)
    ]
    headers = ("", "measure", "folder", "hidden", "source")
    widths = [max(len(c) for c in col) for col in zip(headers, *rows, strict=False)]

    def _row(cells: tuple[str, ...]) -> str:
        return "  ".join(
            cell.ljust(width) for cell, width in zip(cells, widths, strict=False)
        ).rstrip()

    out = [st.dim("      " + _row(headers))]
    out += ["      " + _row(row) for row in rows]
    out.append("")
    return out


def _comparison(
    finding: Finding, metrics: list[Metric], st: _Style, w: int, proven: bool
) -> list[str]:
    """The expressions themselves, laid out for eyeball comparison."""
    out: list[str] = []

    shared = _shared_normalised(finding, metrics)
    if shared:
        out.append(st.dim("      normalised   ") + _short(shared, w - 20))
    fingerprint = _fingerprint(finding, metrics)
    if fingerprint:
        out.append(st.dim("      fingerprint  ") + st.dim(fingerprint[:16] + "..."))
    if shared or fingerprint:
        out.append("")

    shown = metrics[:10]
    out += _metrics_table(shown, st, w)

    for position, metric in enumerate(shown):
        tag = st.bold(_label(position))
        name = metric.name
        out.append(f"      {tag}  {st.bold(name)}")
        out.append(st.dim(f"         {_metric_location(metric)}"))
        for line in _expression_lines(metric.expression):
            out.append(st.dim("         | ") + line)
        out.append("")

    if len(metrics) > len(shown):
        out.append(
            st.dim(
                f"      ... and {len(metrics) - len(shown)} more in this set "
                "(full list in the --json output)"
            )
        )
        out.append("")
    return out


def _single(metrics: list[Metric], st: _Style, w: int) -> list[str]:
    out: list[str] = []
    for metric in metrics[:10]:
        out.append(f"      {st.bold(metric.name)}")
        out.append(st.dim(f"         {_metric_location(metric)}"))
        for line in _expression_lines(metric.expression, max_lines=8):
            out.append(st.dim("         | ") + line)
        out.append("")
    if len(metrics) > 10:
        out.append(st.dim(f"      ... and {len(metrics) - 10} more"))
        out.append("")
    return out


def _shared_normalised(finding: Finding, metrics: list[Metric]) -> str | None:
    for key in ("normalised", "normalized", "shared_normalised", "expression"):
        value = finding.evidence.get(key)
        if isinstance(value, str) and value.strip():
            return value
    forms = {m.normalised for m in metrics if m.normalised}
    return forms.pop() if len(forms) == 1 else None


def _fingerprint(finding: Finding, metrics: list[Metric]) -> str | None:
    value = finding.evidence.get("fingerprint")
    if isinstance(value, str) and value:
        return value
    prints = {m.fingerprint for m in metrics if m.fingerprint}
    return prints.pop() if len(prints) == 1 else None


# Evidence keys already shown elsewhere in the block. "metrics"/"metric" are
# per-metric structures (a list of dicts / a single dict) already rendered
# as the small aligned table (_metrics_table) or the per-metric detail
# block above -- dumping them here too would print a raw Python dict repr,
# wrapped mid-token across lines, which is illegible and was the FIX 3 bug.
_EVIDENCE_SKIP = {
    "normalised",
    "normalized",
    "shared_normalised",
    "expression",
    "fingerprint",
    "metric_ids",
    "names",
    "source_path",
    "metrics",
    "metric",
}


def _evidence(finding: Finding, st: _Style, w: int) -> list[str]:
    extra = {
        k: v
        for k, v in (finding.evidence or {}).items()
        if k not in _EVIDENCE_SKIP and v not in (None, "", [], {})
    }
    if not extra:
        return []
    out = [st.dim("      evidence")]
    for key in sorted(extra):
        value = extra[key]
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            value = ", ".join(str(v) for v in items[:8]) + (
                " ..." if len(items) > 8 else ""
            )
        out += _kv(f"        {key}: ", value, st, w)
    out.append("")
    return out


def _kv(label: str, value: Any, st: _Style, w: int) -> list[str]:
    """`label: value`, wrapping long values under a hanging indent."""
    body = " ".join(str(value).split())
    avail = max(24, w - len(label))
    if len(body) <= avail:
        return [st.dim(label) + body]
    chunks = textwrap.wrap(body, width=avail) or [body]
    pad = " " * len(label)
    return [st.dim(label) + chunks[0]] + [pad + c for c in chunks[1:]]


def _notes(estate: Estate, st: _Style, w: int) -> list[str]:
    if not estate.errors:
        return []
    out = [
        st.heading(f"SCAN NOTES  ({len(estate.errors)})"),
        st.dim(
            "  things dustpan could not do -- your estate may hold more than the above"
        ),
        st.dim("-" * w),
        "",
    ]
    for error in (scrub(e) or "" for e in estate.errors[:40]):
        marker = (
            st.yellow("  ! ") if error.startswith(pipeline.DEGRADED_PREFIX) else "  - "
        )
        wrapped = _wrap(error, w, "      ")
        first = wrapped[0].strip() if wrapped else error
        out.append(marker + first)
        out += wrapped[1:]
    if len(estate.errors) > 40:
        out.append(st.dim(f"  ... and {len(estate.errors) - 40} more"))
    out.append("")
    return out


def _footer(st: _Style, w: int, stats: dict[str, Any]) -> list[str]:
    out = [st.dim("-" * w)]
    if stats["proven_removable"] or stats["review_candidates"]:
        out += _wrap(
            "Verify before deleting: the expressions printed above are the whole "
            "argument for each claim.",
            w,
            "  ",
        )
    if stats["unparsed"]:
        out += [
            st.dim(line)
            for line in _wrap(
                f"{_plural(stats['unparsed'], 'measure')} could not be parsed and took "
                "no part in the comparison.",
                w,
                "  ",
            )
        ]
    out += [
        st.dim(line)
        for line in _wrap(
            "--markdown FILE for a ticket-ready write-up  |  --json FILE for the "
            "full machine-readable dump",
            w,
            "  ",
        )
    ]
    out.append("")
    return out

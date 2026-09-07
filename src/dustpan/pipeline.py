"""Scan orchestration: discover -> parse -> normalise -> detect.

This module is deliberately defensive. Every collaborating module
(`parsers.*`, `dax.normalise`, `detect.*`) is imported lazily, inside
`scan()`, and every call into one is guarded. A module that is missing,
half-written, or that raises on import degrades the scan -- it never kills
it. What was skipped is recorded verbatim in `Estate.errors` so the user
sees exactly which part of the analysis did not run.

Public surface (see CONTRACT.md):
    scan(root: str) -> Estate
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterable
from typing import Any

from dustpan.ir import Estate, Finding, Metric, ScanNote

__all__ = [
    "HEALTH_KINDS",
    "SEVERITY_ORDER",
    "apply_actionability",
    "degraded_notes",
    "estate_blockers",
    "filter_by_confidence",
    "finding_blockers",
    "scan",
    "sort_findings",
    "summarise",
    "version",
]

# Errors that mean "a whole stage of the analysis did not run" start with
# this prefix so the renderers can call the scan degraded rather than clean.
DEGRADED_PREFIX = "pipeline: "

SEVERITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# Findings that describe the health/coverage of the scan itself rather than a
# property of a measure. These survive every confidence filter (AUD-007) and
# their presence must make a clean bill of health impossible.
HEALTH_KINDS: frozenset[str] = frozenset({"usage_unknown", "unparsed"})

# Kinds in the order a human wants to read them: proven first, then
# candidates, then hygiene.
KIND_ORDER: list[str] = [
    "exact_duplicate",
    "near_duplicate",
    "unused_measure",
    "unparsed",
]

_FALLBACK_VERSION = "0.1.2"


def version() -> str:
    """Installed distribution version, or the source fallback."""
    try:
        from importlib import metadata

        return metadata.version("dustpan")
    except Exception:
        return _FALLBACK_VERSION


# --------------------------------------------------------------------------
# lazy, forgiving module loading
# --------------------------------------------------------------------------


class _Loader:
    """Imports collaborator functions on demand, once, never raising."""

    def __init__(self, estate: Estate) -> None:
        self._estate = estate
        self._cache: dict[tuple[str, str], Callable[..., Any] | None] = {}

    def get(self, module: str, attr: str, purpose: str) -> Callable[..., Any] | None:
        key = (module, attr)
        if key in self._cache:
            return self._cache[key]

        fn: Callable[..., Any] | None = None
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # ImportError, SyntaxError, anything at all
            self._note(
                f"module '{module}' is unavailable "
                f"({type(exc).__name__}: {exc}) -- {purpose} skipped"
            )
        else:
            candidate = getattr(mod, attr, None)
            if candidate is None:
                self._note(
                    f"module '{module}' does not define '{attr}()' yet -- {purpose} skipped"
                )
            elif not callable(candidate):
                self._note(
                    f"module '{module}' has a non-callable '{attr}' -- {purpose} skipped"
                )
            else:
                fn = candidate

        self._cache[key] = fn
        return fn

    def _note(self, message: str) -> None:
        text = DEGRADED_PREFIX + message
        if text not in self._estate.errors:
            self._estate.errors.append(text)


_NOTE_KINDS = (
    ("could not read", "unreadable"),
    ("could not resolve", "unreadable"),
    ("while walking", "unreadable"),
    ("outside the scan root", "contained"),
    ("outside the model folder", "contained"),
    ("unavailable", "stage_skipped"),
    ("skipped", "stage_skipped"),
    ("raised on", "malformed"),
)


def classify(message: str) -> ScanNote:
    """Turn a free-text error into a structured note (AUD-011).

    Parsers append plain strings to `Estate.errors`; this is where that text
    becomes health state the renderers can trust. Anything unrecognised is
    treated as MATERIAL rather than informational -- an unclassified failure
    is exactly the kind that should not quietly become a clean result.
    """
    stage = message.split(":", 1)[0].strip() if ":" in message else "scan"
    lowered = message.casefold()
    kind = next((k for token, k in _NOTE_KINDS if token in lowered), "malformed")
    return ScanNote(stage=stage, kind=kind, message=message, material=True)


def _record(estate: Estate, message: str) -> None:
    if message not in estate.errors:
        estate.errors.append(message)
        estate.notes.append(classify(message))


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def scan(root: str) -> Estate:
    """Walk `root`, parse everything understood, and detect findings.

    Never raises. A missing collaborator module, an unreadable file or a
    detector that blows up all become entries in `Estate.errors`; the scan
    returns whatever it did manage to learn.
    """
    estate = Estate()
    loader = _Loader(estate)

    root = os.path.abspath(os.path.expanduser(str(root)))
    estate.scan_root = root
    if not os.path.isdir(root):
        _record(estate, f"pipeline: '{root}' is not a directory -- nothing scanned")
        return estate

    targets = _discover(root, loader, estate)
    targets = _contained(targets, root, estate)
    _parse(targets, loader, estate)
    _enrich(estate, loader)
    _detect(estate, loader)
    _flag_unparsed(estate)

    _reconcile_notes(estate)
    apply_actionability(estate)
    estate.findings = sort_findings(estate.findings)
    return estate


def _reconcile_notes(estate: Estate) -> None:
    """Give every error a structured note, including ones parsers appended
    directly to `Estate.errors` without going through `_record`."""
    known = {n.message for n in estate.notes}
    for message in estate.errors:
        if message not in known:
            estate.notes.append(classify(message))
            known.add(message)


def _contained(
    targets: list[tuple[str, str]], root: str, estate: Estate
) -> list[tuple[str, str]]:
    """Drop anything that resolves outside the requested scan root.

    AUD-006: `os.walk(followlinks=False)` refuses to descend into symlinked
    directories but happily hands back a symlinked regular file, so a `.tmdl`
    link inside a model folder could pull in content from anywhere on the
    filesystem -- and an exported report would then disclose it. Containment
    is checked on the resolved real path, and every skip is recorded as a
    security-relevant note rather than silently dropped.
    """
    real_root = os.path.realpath(root)
    prefix = real_root.rstrip(os.sep) + os.sep
    kept: list[tuple[str, str]] = []
    for kind, path in targets:
        try:
            real = os.path.realpath(path)
        except OSError:
            continue
        if real == real_root or real.startswith(prefix):
            kept.append((kind, path))
        else:
            _record(
                estate,
                f"discover: skipped '{path}' -- it resolves to '{real}', "
                "outside the scan root. Symlinks are not followed out of the "
                "folder you asked dustpan to scan.",
            )
    return kept


def _discover(root: str, loader: _Loader, estate: Estate) -> list[tuple[str, str]]:
    discover = loader.get("dustpan.parsers.discover", "discover", "discovery")
    if discover is None:
        return []
    try:
        try:
            found = discover(root, estate)
        except TypeError:  # older signature -- still works, just quieter
            found = discover(root)
    except Exception as exc:
        _record(estate, f"discover: failed on '{root}' ({type(exc).__name__}: {exc})")
        return []

    targets: list[tuple[str, str]] = []
    for item in found or []:
        try:
            kind, path = item
            targets.append((str(kind), str(path)))
        except Exception:
            _record(estate, f"discover: ignored malformed result {item!r}")
    return targets


# kind -> (module, function, human name)
_PARSERS: dict[str, tuple[str, str, str]] = {
    "pbip_model": ("dustpan.parsers.tmdl", "parse_model", "TMDL model parsing"),
    "model_bim": ("dustpan.parsers.tmsl", "parse_model_bim", "TMSL model parsing"),
    "pbip_report": ("dustpan.parsers.report", "parse_report", "report parsing"),
}

# Models before reports: unused-measure detection needs metrics to exist
# before it can ask which of them a visual mentions.
_KIND_PRIORITY = {"pbip_model": 0, "model_bim": 0, "pbip_report": 1}


def _parse(targets: Iterable[tuple[str, str]], loader: _Loader, estate: Estate) -> None:
    ordered = sorted(targets, key=lambda t: (_KIND_PRIORITY.get(t[0], 2), t[1]))
    models = [t for t in ordered if _KIND_PRIORITY.get(t[0], 2) == 0]
    rest = [t for t in ordered if _KIND_PRIORITY.get(t[0], 2) != 0]

    _parse_targets(models, loader, estate)
    # Metric ids must be unique BEFORE reports are parsed, because report
    # parsing resolves visual references onto those ids (AUD-003).
    _disambiguate(estate, estate.metrics, "measure")
    _parse_targets(rest, loader, estate)
    _disambiguate(estate, estate.assets, "report")


def _disambiguate(estate: Estate, items: list[Any], noun: str) -> None:
    """Make ids unique across deployments, keeping them readable.

    `powerbi:Sales:Total` is composed from a folder BASENAME, so two
    unrelated projects that both call a model `Sales` produce one id for two
    different objects -- `metric_by_id()` keeps one and loses the other, and
    a keep/retire instruction could then name the wrong deployment entirely
    (AUD-003, AUD-008).

    Rather than hashing every id and making all of them unreadable, only the
    ones that actually clash are suffixed `#2`, `#3` -- the same convention
    the report parser already uses for duplicate visual names. Ordering is by
    `source_root`, so the suffix a given object gets is stable across runs.
    Each clash is recorded as a material note: two same-named deployments in
    one scan is something the user should know about, not something to paper
    over silently.
    """
    groups: dict[str, list[Any]] = {}
    for item in items:
        groups.setdefault(item.id, []).append(item)

    for base_id, group in sorted(groups.items()):
        roots = sorted({(getattr(item, "source_root", None) or "") for item in group})
        if len(roots) < 2:
            continue
        for item in group:
            position = roots.index(getattr(item, "source_root", None) or "")
            if position:
                item.id = f"{base_id}#{position + 1}"
        _record(
            estate,
            f"pipeline: {len(roots)} separate deployments define the {noun} "
            f"'{base_id}' ({', '.join(roots)}). Their ids were made unique with "
            "'#n' suffixes; nothing is compared or retired across that boundary.",
        )


def _parse_targets(
    targets: Iterable[tuple[str, str]], loader: _Loader, estate: Estate
) -> None:
    for kind, path in targets:
        spec = _PARSERS.get(kind)
        if spec is None:
            _record(
                estate, f"pipeline: no parser for discovered kind '{kind}' at '{path}'"
            )
            continue
        module, attr, purpose = spec
        parse = loader.get(module, attr, purpose)
        if parse is None:
            continue
        try:
            parse(path, estate)
        except Exception as exc:
            # Parsers are contracted never to raise; if one does, the scan
            # still finishes and the user is told which file did it.
            _record(
                estate,
                f"{module.rsplit('.', 1)[-1]}: raised on '{path}' "
                f"({type(exc).__name__}: {exc})",
            )


def _enrich(estate: Estate, loader: _Loader) -> None:
    if not estate.metrics:
        return
    enrich = loader.get("dustpan.dax.normalise", "enrich", "DAX normalisation")
    if enrich is None:
        return
    failures = 0
    for metric in estate.metrics:
        try:
            enrich(metric)
        except Exception as exc:
            failures += 1
            metric.lexically_ok = False
            metric.parse_error = f"{type(exc).__name__}: {exc}"
    if failures:
        _record(
            estate,
            f"normalise: enrich() raised on {failures} metric(s); "
            "those measures are excluded from duplicate analysis",
        )


_DETECTORS: list[tuple[str, str, str]] = [
    ("dustpan.detect.duplicates", "find_duplicates", "duplicate detection"),
    ("dustpan.detect.unused", "find_unused", "unused-measure detection"),
]


def _detect(estate: Estate, loader: _Loader) -> None:
    for module, attr, purpose in _DETECTORS:
        detect = loader.get(module, attr, purpose)
        if detect is None:
            continue
        try:
            findings = detect(estate)
        except Exception as exc:
            _record(
                estate,
                f"{attr}: raised during {purpose} ({type(exc).__name__}: {exc}) "
                "-- results are incomplete",
            )
            continue
        for finding in findings or []:
            if isinstance(finding, Finding):
                estate.findings.append(finding)
            else:
                _record(
                    estate, f"{attr}: ignored non-Finding result {type(finding).__name__}"
                )


def _flag_unparsed(estate: Estate) -> None:
    """Surface measures whose expression could not be normalised.

    These are not a judgement about the measure -- they are a statement
    about dustpan's own coverage, and the reason a duplicate set might be
    incomplete. Skipped for any metric a detector already flagged.
    """
    already = {
        mid for f in estate.findings if f.kind == "unparsed" for mid in f.metric_ids
    }
    for metric in estate.metrics:
        if metric.lexically_ok or metric.id in already:
            continue
        estate.findings.append(
            Finding(
                kind="unparsed",
                severity="low",
                confidence=1.0,  # deterministic fact: the parse did fail
                metric_ids=[metric.id],
                summary=(
                    f"'{metric.name}' could not be normalised, so it was excluded "
                    "from duplicate analysis."
                ),
                evidence={
                    "parse_error": metric.parse_error or "no normalised form produced",
                    "source_path": metric.source_path,
                },
            )
        )


# --------------------------------------------------------------------------
# helpers shared by the renderers, so every surface reports the same numbers
# --------------------------------------------------------------------------


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Severity, then confidence (high first), then a stable tiebreak."""

    def key(f: Finding) -> tuple[Any, ...]:
        return (
            SEVERITY_ORDER.get(str(f.severity), 3),
            -float(f.confidence or 0.0),
            KIND_ORDER.index(f.kind) if f.kind in KIND_ORDER else len(KIND_ORDER),
            str(f.kind),
            tuple(f.metric_ids),
        )

    return sorted(findings, key=key)


def filter_by_confidence(estate: Estate, minimum: float) -> tuple[Estate, int]:
    """Return (estate with low-confidence findings dropped, number dropped).

    The returned Estate shares metric/asset objects with the original; only
    the findings list is new.

    AUD-007: health records in `HEALTH_KINDS` are never dropped, whatever the
    threshold. `usage_unknown` carries confidence 0.0 -- deliberately, because
    it asserts nothing about a metric -- so any `--min-confidence` above zero
    used to delete the one line explaining that usage was never assessed, and
    the console then printed "Nothing to remove". A coverage gap is not a weak
    finding to be filtered; it is the context that makes every other number
    interpretable.
    """
    if minimum <= 0.0:
        return estate, 0
    kept = [
        f
        for f in estate.findings
        if f.kind in HEALTH_KINDS or float(f.confidence or 0.0) >= minimum
    ]
    dropped = len(estate.findings) - len(kept)
    if not dropped:
        return estate, 0
    trimmed = Estate(
        metrics=estate.metrics,
        assets=estate.assets,
        usage=estate.usage,
        findings=kept,
        errors=estate.errors,
        # AUD-V1-010: notes, provenance and scan root were dropped whenever a
        # single finding was filtered, and `degraded_notes()` prefers notes
        # when present -- so raising --min-confidence silently converted a
        # degraded scan into a clean-looking one.
        notes=estate.notes,
        read_paths=estate.read_paths,
        scan_root=estate.scan_root,
        ir_version=estate.ir_version,
    )
    return trimmed, dropped


def degraded_notes(estate: Estate) -> list[str]:
    """Every message meaning the results below are an incomplete picture.

    AUD-011: this used to return only errors carrying the ``"pipeline: "``
    prefix, so a report parser that could not read a file left the summary
    saying ``degraded=0`` while the scan had genuinely missed content. Health
    is now read from structured notes, and anything unclassified counts as
    material -- a scan is clean only when nothing said otherwise.
    """
    if estate.notes:
        return [n.message for n in estate.material_notes()]
    return [e for e in estate.errors if e.startswith(DEGRADED_PREFIX)]


ACTIONABLE_KIND = "exact_duplicate"


def estate_blockers(estate: Estate) -> list[str]:
    """Reasons NO retirement instruction may be issued anywhere in this scan.

    These are properties of the scan, not of any one finding. Each says the
    same thing in a different way: we do not know enough about this estate to
    tell someone to delete something from it.
    """
    blockers: list[str] = []
    if any(f.kind == "usage_unknown" for f in estate.findings):
        blockers.append(
            "usage coverage is unknown -- no report was scanned, so no consumer "
            "of these measures has been checked"
        )
    if estate.id_collisions():
        blockers.append(
            "two distinct measures answer to one id, so a retirement instruction "
            "cannot say which object it would remove"
        )
    material = estate.material_notes()
    if material:
        blockers.append(
            f"{len(material)} material scan note(s): part of the estate could not "
            "be read or parsed, so usage evidence is incomplete "
            f"({material[0].message[:90]})"
        )
    return blockers


def covered_deployments(estate: Estate) -> set[tuple[str, str]]:
    """Deployments for which a bound report was actually scanned.

    AUD-V3-005: coverage used to be a single global fact -- if ANY report was
    scanned anywhere under the root, every deployment counted as covered. A
    root holding One (with a report) and Two (without one) therefore offered
    to retire a measure in Two on the strength of One's report. A report
    covers the project it belongs to and nothing else.

    Binding requires a model within the report's project root. An omitted
    model hint is resolvable only when that root contains one deployment.
    Merely sharing a parent folder never covers a second sibling model.
    """
    covered: set[tuple[str, str]] = set()
    for asset in estate.assets:
        root = asset.source_root or ""
        if not root:
            continue
        try:
            real = asset.real_visuals()
        except Exception:
            real = list(asset.visuals)
        if not real:
            continue
        candidates = {
            metric.deployment
            for metric in estate.metrics
            if (metric.source_root or "") == root
            and (not asset.model or metric.model == asset.model)
        }
        if len(candidates) == 1:
            covered.update(candidates)
    return covered


def finding_blockers(estate: Estate, finding: Finding) -> list[str]:
    """Reasons THIS duplicate set may not carry a retirement instruction."""
    if finding.kind != ACTIONABLE_KIND or float(finding.confidence or 0.0) < 1.0:
        return ["not a proven exact duplicate"]

    by_id = estate.metric_by_id()
    members = [by_id[mid] for mid in finding.metric_ids if mid in by_id]
    if len(members) < 2:
        return ["members could not be resolved back to metrics"]

    blockers: list[str] = []
    # AUD-V1-002: scope by DEPLOYMENT, never by model basename. Two tenants
    # can both call a model `Sales`; once ids are disambiguated the collision
    # check no longer catches them, so basename comparison here would hand
    # out cross-tenant deletion advice with nothing left to stop it.
    deployments = {m.deployment for m in members}
    if len(deployments) > 1:
        blockers.append(
            "members live in different deployments, each feeding its own reports"
        )
    else:
        covered = covered_deployments(estate)
        uncovered = sorted(d for d in deployments if d not in covered)
        if uncovered:
            names = ", ".join(f"'{model or '?'}'" for model, _root in uncovered)
            blockers.append(
                f"no report bound to deployment {names} was scanned, so nothing "
                "is known about who consumes these measures -- another "
                "deployment's report is not evidence about this one"
            )
    try:
        from dustpan.detect.duplicates import interchangeability_blocker
    except Exception:
        return ["duplicate detector unavailable -- nothing can be proven"]
    reason = interchangeability_blocker(members)
    if reason:
        blockers.append(reason)
    return blockers


def apply_actionability(estate: Estate) -> None:
    """Decide, in ONE place, which findings may carry a retirement action.

    AUD-V1-001/002/003: this used to be recomputed three times -- the
    detector attached keep/retire whenever deployments matched, the pipeline
    re-derived removability from model basenames, and both renderers printed
    an action whenever confidence was 1.0. Each knew a different subset of
    the rules, so a set the detector had already marked blocked was still
    rendered as "Action: keep A, retire B".

    Every consumer now reads `evidence["retirement_cleared"]`. Nothing
    downstream re-derives actionability from confidence.
    """
    shared = estate_blockers(estate)
    for finding in estate.findings:
        if finding.kind != ACTIONABLE_KIND:
            continue
        blockers = shared + finding_blockers(estate, finding)
        finding.evidence["retirement_cleared"] = not blockers
        finding.evidence["retirement_blockers"] = blockers
        proposal = finding.evidence.get("keeper_proposal")
        if blockers:
            # The FACT (identical expressions) stands and is still reported.
            # The INSTRUCTION does not.
            finding.evidence.pop("keep", None)
            finding.evidence.pop("keep_id", None)
            finding.evidence.pop("retire", None)
        elif isinstance(proposal, dict):
            # AUD-V3-004: the authority must restore advice when conditions
            # become safe again, not only remove it when they stop being safe.
            finding.evidence["keep"] = proposal.get("keep")
            finding.evidence["keep_id"] = proposal.get("keep_id")
            finding.evidence["retire"] = list(proposal.get("retire") or ())


def proven_removable_metric_ids(estate: Estate) -> set[str]:
    """Metrics that are provably retireable, full stop -- no review needed.

    Reads the single actionability decision made by `apply_actionability`
    rather than re-deriving it, so this can never disagree with what the
    console and the Markdown report told the user.
    """
    # AUD-V3-004 / B-CE-05: ALWAYS decide over current state. Recomputing only
    # when the flag was missing meant a decision taken before a material note
    # was appended stayed cached and kept authorising a deletion. The estate is
    # mutable, so a cached clearance is a stale clearance; the decision is
    # cheap and is simply retaken.
    apply_actionability(estate)

    known = set(estate.metric_by_id())
    out: set[str] = set()
    for f in estate.findings:
        if f.kind != ACTIONABLE_KIND:
            continue
        if not f.evidence.get("retirement_cleared"):
            continue
        out.update(list(f.metric_ids)[1:])
    return {mid for mid in out if mid in known} if known else out


def review_candidate_metric_ids(estate: Estate) -> set[str]:
    """Metrics worth a human's review, but NOT proven removable.

    Every metric_id attached to any finding below confidence 1.0: both
    near-duplicate tiers (same-shape and name-drift candidates) and every
    ``unused_measure`` finding (0.6 normal / 0.4 degraded -- heuristic
    evidence of disuse, never a certainty). Deliberately disjoint from
    `proven_removable_metric_ids`: a metric already proven removable is
    never also listed as merely "worth reviewing", and this set's size is
    never added to that one to make a third, blended number -- that blend
    is exactly the false-precision bug this split exists to prevent. See
    `pipeline.summarise`.
    """
    by_id = estate.metric_by_id()
    proven = proven_removable_metric_ids(estate)
    out: set[str] = set()
    for f in estate.findings:
        if float(f.confidence or 0.0) >= 1.0:
            continue
        out.update(f.metric_ids)
    known = set(by_id)
    out = {mid for mid in out if mid in known} if known else out
    return out - proven


def summarise(estate: Estate) -> dict[str, Any]:
    """The one place scan statistics are computed."""
    apply_actionability(estate)
    metrics: list[Metric] = list(estate.metrics)
    total = len(metrics)
    parsed = sum(1 for m in metrics if m.lexically_ok)
    # AUD-V1-013: two tenants that both call their model `Sales` are two
    # deployments, not one model. The headline counts deployments; the names
    # are still exposed separately for display.
    models = sorted({m.model for m in metrics if m.model})
    deployments = {m.deployment for m in metrics}
    try:
        visuals = sum(len(a.real_visuals()) for a in estate.assets)
    except Exception:  # parser module unavailable -> fall back to raw count
        visuals = sum(len(a.visuals) for a in estate.assets)
    reference_carriers = sum(len(a.visuals) for a in estate.assets) - visuals

    by_kind: dict[str, int] = {}
    for f in estate.findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1

    proven = proven_removable_metric_ids(estate)
    candidates = review_candidate_metric_ids(estate)
    duplicated = {
        mid
        for f in estate.findings
        if f.kind == "exact_duplicate"
        for mid in f.metric_ids
    }

    return {
        "metrics": total,
        "parsed": parsed,
        "coverage": (parsed / total) if total else 1.0,
        "models": len(deployments),
        "model_names": models,
        "deployments": len(deployments),
        "assets": len(estate.assets),
        "visuals": visuals,
        "reference_carriers": reference_carriers,
        "findings": len(estate.findings),
        "by_kind": by_kind,
        "exact_duplicate_sets": by_kind.get("exact_duplicate", 0),
        "near_duplicate_sets": by_kind.get("near_duplicate", 0),
        "unused": by_kind.get("unused_measure", 0),
        "unparsed": total - parsed,
        "high": sum(1 for f in estate.findings if f.severity == "high"),
        "medium": sum(1 for f in estate.findings if f.severity == "medium"),
        "low": sum(1 for f in estate.findings if f.severity == "low"),
        "duplicated_metrics": len(duplicated),
        # Two numbers that must never be added together into one blended
        # headline stat -- one is a fact, the other is a reading list. See
        # `proven_removable_metric_ids` / `review_candidate_metric_ids`.
        "proven_removable": len(proven),
        "proven_removable_ids": sorted(proven),
        "proven_removable_pct": (len(proven) / total) if total else 0.0,
        "review_candidates": len(candidates),
        "review_candidate_ids": sorted(candidates),
        "review_candidates_pct": (len(candidates) / total) if total else 0.0,
        "errors": len(estate.errors),
        "degraded": len(degraded_notes(estate)),
    }

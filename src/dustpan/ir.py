"""Frozen tool-agnostic intermediate representation for dustpan.

EVERY module codes against this. Parsers produce it; detectors consume it.
Do not add tool-specific fields here -- if it only makes sense for Power BI,
it belongs in `extra`.

Contract version: 2 (see IR_VERSION below -- keep the two in step).

Fields are additive-only: nothing here is renamed or removed once shipped,
because a consumer reading last week's JSON must still be able to read it.
New fields carry defaults so an Estate built by an older caller stays valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

IR_VERSION = 2

# Characters that must never reach a terminal or a Markdown document from a
# scanned file. C0/C1 controls include ESC, which starts the OSC/CSI sequences
# a hostile measure name could use to clear the screen, retitle the window,
# drive the clipboard or forge output (AUD-005). Neutralising them HERE, as
# text enters the IR, fixes every consumer at once -- console, Markdown and
# JSON -- instead of relying on each renderer to remember.
_CONTROLS = {c: f"\\x{c:02x}" for c in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]}
_CONTROLS_KEEPING_LAYOUT = {
    c: e for c, e in _CONTROLS.items() if c not in (0x09, 0x0A, 0x0D)
}


_PROJECT_MARKERS = (".semanticmodel", ".report", ".pbip", ".dataset")


def project_root(path: str) -> str:
    """The folder that owns a whole Power BI project, not one part of it.

    A `.pbip` project is several sibling folders -- `X.SemanticModel`,
    `X.Report` -- so "which deployment is this?" is answered by their PARENT,
    not by the folder a given file sits in. Using the file's own directory
    would give a model and the report that consumes it different deployment
    identities, and usage would never line up (AUD-003/008).

    Falls back to the containing directory when no marker is found, which is
    the safest answer: it can only split deployments apart, never merge two.
    """
    import os as _os

    current = _os.path.realpath(path)
    if _os.path.isfile(current):
        current = _os.path.dirname(current)
    walked = current
    while True:
        parent, leaf = _os.path.split(walked)
        if not parent or parent == walked:
            return current
        if leaf.casefold().endswith(_PROJECT_MARKERS):
            return parent
        walked = parent


def scrub(value: str | None, keep_layout: bool = False) -> str | None:
    """Replace control characters with a visible escape, leaving text intact.

    DISPLAY ONLY. AUD-V3-003: this used to run inside `Metric.__post_init__`,
    which meant a DAX expression containing a real ESC byte and one
    containing the six literal characters ``\\x1b`` became the same string
    before any fingerprint was taken -- so the analyser manufactured formula
    identity and offered to delete one of two genuinely different measures.
    Escaping is a property of a terminal, not of the model, and belongs at
    the boundary where text is shown.

    `keep_layout` preserves tab/newline/carriage-return, for fields such as a
    DAX expression where they are real content. Everything else -- names,
    folders, paths, error text -- is single-line by nature, so a newline in
    one is either corruption or an attempt to forge output.
    """
    if not value:
        return value
    table = _CONTROLS_KEEPING_LAYOUT if keep_layout else _CONTROLS
    return value.translate(table)


Tool = Literal["powerbi", "tableau", "streamlit"]
Dialect = Literal["dax", "tableau-calc", "python", "unknown"]


@dataclass(frozen=True)
class Ref:
    """A table/column reference discovered inside an expression."""

    table: str | None
    column: str

    def key(self) -> str:
        return f"{(self.table or '').casefold()}[{self.column.casefold()}]"


@dataclass
class Metric:
    """One measure / calculated field / metric definition, from any tool."""

    # -- identity (parser fills) --
    id: str  # stable & unique: f"{tool}:{model}:{name}"
    name: str
    tool: Tool
    model: str  # semantic model / workbook / module name
    source_path: str

    # -- definition (parser fills) --
    expression: str  # verbatim native expression
    dialect: Dialect = "unknown"
    description: str | None = None
    display_folder: str | None = None
    format_string: str | None = None
    data_type: str | None = None
    is_hidden: bool = False

    # -- normalisation (dax module fills) --
    normalised: str | None = None
    fingerprint: str | None = None  # sha256 hex of `normalised`
    refs: list[Ref] = field(default_factory=list)
    agg_kind: str | None = None  # best-effort: SUM / COUNT / AVERAGE / ...
    # AUD-010: this was called `parse_ok`, which claimed more than it checked.
    # The normaliser validates TOKENISATION and delimiter balance, not DAX
    # grammar -- `normalise("1 +")` canonicalises happily. The honest name
    # says what was actually established: the expression could be reduced to
    # a canonical form. Two measures that are both `1 +` really are
    # identical, so treating them as duplicates is not wrong; claiming they
    # "parsed" was.
    lexically_ok: bool = False
    parse_error: str | None = None

    # Absolute real path of the semantic-model folder this measure came from.
    # `model` alone is a folder BASENAME, so two unrelated deployments that
    # both call their model `Sales` are indistinguishable by it -- and a
    # keep/retire instruction issued across that boundary would tell someone
    # to delete a measure another tenant's reports depend on (AUD-003).
    # Deployment identity is (model, source_root), never `model` alone.
    source_root: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def deployment(self) -> tuple[str, str]:
        """The unit inside which 'retire all but one' is a coherent claim."""
        return (self.model or "", self.source_root or "")

    @property
    def parse_ok(self) -> bool:
        """Deprecated alias for `lexically_ok`, kept for external readers."""
        return self.lexically_ok

    def ref_fingerprint(self) -> str:
        """Order-independent signature of what this metric touches."""
        return "|".join(sorted({r.key() for r in self.refs}))


@dataclass
class Visual:
    """A single visual on a report page and the metrics it references."""

    id: str
    page: str
    visual_type: str
    metric_names: list[str] = field(default_factory=list)


@dataclass
class Asset:
    """A report / dashboard / workbook that consumes metrics."""

    id: str
    name: str
    tool: Tool
    source_path: str
    model: str | None = None
    # Same deployment-identity role as `Metric.source_root` (AUD-003/008):
    # two unrelated projects can both contain a `Sales.Report` folder, and
    # `powerbi:Sales` alone would merge their usage observations -- which
    # feeds the keeper choice, so it is not merely a counting error.
    source_root: str | None = None
    visuals: list[Visual] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def deployment(self) -> tuple[str, str]:
        return (self.name or "", self.source_root or "")

    def real_visuals(self) -> list[Visual]:
        """Visuals a user would actually see on a page.

        The report parser models page filters, report filters, extensions and
        bookmarks as synthetic visuals so their metric references still count
        towards usage. They are not visuals, and counting them inflates every
        headline (AUD-015): the supplied fixture reported 10 where 6 are real.
        Reachability keeps using every entry; only counts and presentation
        use this.
        """
        from dustpan.parsers.report import PSEUDO_VISUAL_TYPES

        return [v for v in self.visuals if v.visual_type not in PSEUDO_VISUAL_TYPES]

    def referenced_metric_names(self) -> set[str]:
        out: set[str] = set()
        for v in self.visuals:
            out.update(v.metric_names)
        return out


@dataclass
class Usage:
    """Activity-log derived usage. Empty in V0; reserved for V2."""

    asset_id: str
    opens: int = 0
    unique_users: int = 0
    exports: int = 0
    last_accessed: str | None = None


Severity = Literal["high", "medium", "low"]


@dataclass
class Finding:
    """One thing the user could remove, merge, or investigate.

    `confidence` is NOT a vibe: 1.0 is reserved for deterministic proof
    (identical fingerprints). Anything heuristic must be < 1.0.
    """

    kind: str  # exact_duplicate | near_duplicate | unused_measure | unparsed
    severity: Severity
    confidence: float
    metric_ids: list[str]
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    asset_ids: list[str] = field(default_factory=list)


def _conflicting(group: list[Metric]) -> bool:
    """True when a group of same-id metrics holds genuinely different objects.

    Compared on what identity actually depends on -- expression, deployment
    and display metadata -- rather than on object identity, so re-parsing the
    same file twice is not mistaken for a conflict.
    """
    if len(group) < 2:
        return False
    signatures = {
        (
            m.expression,
            m.deployment,
            m.format_string,
            m.data_type,
            m.source_path,
        )
        for m in group
    }
    return len(signatures) > 1


def display_copy(estate: Estate) -> Estate:
    """A render-safe shallow projection of `estate` (AUD-V3-003).

    Every string a terminal or Markdown document will show is scrubbed HERE,
    on copies, so the analysed objects keep their exact source bytes. JSON
    deliberately does not use this: `json.dumps` escapes control characters
    natively, so the machine-readable output round-trips the true semantic
    text.
    """
    import copy as _copy

    out = _copy.copy(estate)
    out.metrics = [_display_metric(m) for m in estate.metrics]
    out.assets = [_display_asset(a) for a in estate.assets]
    out.findings = [_display_finding(f) for f in estate.findings]
    out.errors = [scrub(e) or "" for e in estate.errors]
    out.notes = [_display_note(n) for n in estate.notes]
    return out


def _display_metric(metric: Metric) -> Metric:
    import copy as _copy

    out = _copy.copy(metric)
    for field_name in (
        "name",
        "model",
        "source_path",
        "description",
        "display_folder",
        "format_string",
        "data_type",
        "parse_error",
    ):
        setattr(out, field_name, scrub(getattr(out, field_name, None)))
    out.expression = scrub(out.expression, keep_layout=True) or ""
    out.normalised = scrub(out.normalised, keep_layout=True)
    return out


def _display_asset(asset: Asset) -> Asset:
    import copy as _copy

    out = _copy.copy(asset)
    out.id = scrub(out.id) or ""
    out.name = scrub(out.name) or ""
    out.source_path = scrub(out.source_path) or ""
    out.model = scrub(out.model)
    out.visuals = [_display_visual(v) for v in asset.visuals]
    return out


def _display_visual(visual: Visual) -> Visual:
    import copy as _copy

    out = _copy.copy(visual)
    out.id = scrub(out.id) or ""
    out.page = scrub(out.page) or ""
    out.visual_type = scrub(out.visual_type) or ""
    out.metric_names = [scrub(n) or "" for n in visual.metric_names]
    return out


def _display_finding(finding: Finding) -> Finding:
    import copy as _copy

    out = _copy.copy(finding)
    out.summary = scrub(out.summary) or ""
    # Identity keys remain raw for lookups; escape only their visible labels.
    out.metric_ids = list(finding.metric_ids)
    out.asset_ids = list(finding.asset_ids)
    out.evidence = {k: _display_value(v) for k, v in finding.evidence.items()}
    return out


def _display_note(note: ScanNote) -> ScanNote:
    import copy as _copy

    out = _copy.copy(note)
    out.message = scrub(out.message) or ""
    out.path = scrub(out.path)
    return out


def _display_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_display_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _display_value(v) for k, v in value.items()}
    return value


@dataclass
class ScanNote:
    """One structured statement about the health of a scan.

    Health used to be inferred by testing whether an error string started
    with ``"pipeline: "`` (AUD-011), so a parser that failed to read a file
    left the summary claiming ``degraded=0``. Classification belongs in the
    data, not in a prefix a future message could forget to carry.

    `stage`  which part of the scan produced it (discover/tmdl/report/...).
    `kind`   stage_skipped | unreadable | malformed | contained | info.
    `material` True when it means the results below are incomplete. A
             material note must prevent any clean bill of health.
    """

    stage: str
    kind: str
    message: str
    material: bool = True
    path: str | None = None


@dataclass
class Estate:
    """Everything discovered in one scan."""

    metrics: list[Metric] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[ScanNote] = field(default_factory=list)
    #: Real paths of every file this scan actually opened. Recorded rather
    #: than inferred from `source_path`, because structural files and nested
    #: PBIR files produce no Metric or Asset of their own but must still
    #: never be overwritten by an output (AUD-V1-004).
    read_paths: list[str] = field(default_factory=list)
    #: Every path DISCOVERED as a scan source, recorded before any read is
    #: attempted and regardless of whether it succeeded (AUD-V3-002). A source
    #: refused for being over budget, unreadable or malformed is still the
    #: user's source and must never become an output destination.
    protected_paths: list[str] = field(default_factory=list)
    #: Absolute root the user asked to scan. Every read is contained to it.
    scan_root: str | None = None
    ir_version: int = IR_VERSION

    def material_notes(self) -> list[ScanNote]:
        """Notes that mean the findings below are an incomplete picture."""
        return [n for n in self.notes if n.material]

    def metric_by_id(self) -> dict[str, Metric]:
        return {m.id: m for m in self.metrics}

    def id_collisions(self) -> dict[str, list[Metric]]:
        """Metric ids claimed by more than one distinct measure (AUD-003).

        `metric_by_id()` keeps the last writer and silently loses the rest, so
        a collision is not merely a reporting nuisance: it can make two
        different measures look like one object. Callers that are about to
        recommend a deletion must treat a non-empty result as a hard stop.

        AUD-V3-013: this used to require the colliding measures to be in
        different DEPLOYMENTS, so two conflicting definitions inside one model
        -- a malformed or duplicated model file -- returned no collision at
        all while `metric_by_id()` quietly kept only the last one. What makes
        an id ambiguous is two DIFFERENT definitions claiming it, wherever
        they live. Repeated references to the same definition are not a
        conflict, so identical definitions are not reported.
        """
        seen: dict[str, list[Metric]] = {}
        for metric in self.metrics:
            seen.setdefault(metric.id, []).append(metric)
        return {mid: group for mid, group in seen.items() if _conflicting(group)}


class Parser:
    """Protocol every parser follows.

    `detect` is cheap (path sniffing only). `parse` returns whatever it found;
    it must never raise on malformed input -- append to Estate.errors instead.
    """

    tool: Tool

    def detect(self, path: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, path: str, estate: Estate) -> None:  # pragma: no cover
        raise NotImplementedError

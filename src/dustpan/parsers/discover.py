"""Filesystem discovery of Power BI project parts.

`discover()` walks a tree and reports the interesting bits of a Power BI
Project (PBIP) layout:

    MyModel.SemanticModel/
        definition.pbism            <- marks the folder as a semantic model
        definition/                 -> ("pbip_model", <this folder>)
            database.tmdl
            model.tmdl
            tables/Sales.tmdl
        model.bim                   -> ("model_bim", <this file>)   [TMSL form]
    MyReport.Report/                -> ("pbip_report", <this folder>)
        definition.pbir
        report.json                 [PBIR-Legacy]
        definition/report.json      [PBIR]

Design notes
------------
* A semantic model is stored as *either* TMDL (``definition/``) or TMSL
  (``model.bim``), never both.  If both are present the TMDL folder wins --
  emitting both would parse the same measures twice and manufacture bogus
  "duplicate" findings.
* Partial matches are ignored rather than reported: a ``*.SemanticModel``
  folder with neither a populated ``definition/`` nor a ``model.bim`` yields
  nothing.
* Discovery never raises.  Unreadable directories are skipped.
"""

from __future__ import annotations

import os

from dustpan.ir import Estate

__all__ = ["discover", "model_name_from_path"]

KIND_MODEL = "pbip_model"
KIND_MODEL_BIM = "model_bim"
KIND_REPORT = "pbip_report"

# Folder-name suffixes that mark a PBIP part.  ``.Dataset`` is the pre-2024
# spelling of ``.SemanticModel`` and still shows up in older repos.
_MODEL_SUFFIXES = (".semanticmodel", ".dataset")
_REPORT_SUFFIXES = (".report",)

# Marker files that identify a part even when the folder is not suffixed
# (people rename folders; git exports sometimes flatten them).
_MODEL_MARKER = "definition.pbism"
_REPORT_MARKER = "definition.pbir"

# Never walked into.  ``.pbi`` holds Desktop's local cache, ``TMDLScripts``
# and ``DAXQueries`` hold user scratch files that are *not* part of the model.
_SKIP_DIRS = {
    "__pycache__",
    "node_modules",
    "venv",
    "tmdlscripts",
    "daxqueries",
    "customvisuals",
    "staticresources",
}

# Emit models before reports so a consuming pipeline can resolve report
# references against already-parsed measures.
_KIND_ORDER = {KIND_MODEL: 0, KIND_MODEL_BIM: 1, KIND_REPORT: 2}


def discover(root: str, estate: Estate | None = None) -> list[tuple[str, str]]:
    """Return ``[(kind, path)]`` for every Power BI project part under `root`.

    `kind` is one of ``"pbip_model"`` (a ``definition/`` folder of TMDL files),
    ``"model_bim"`` (a TMSL ``model.bim`` file) or ``"pbip_report"`` (a
    ``*.Report`` folder).  Never raises.

    Pass `estate` to have traversal failures recorded as material scan notes
    (AUD-V1-012). The optional argument keeps the CONTRACT.md signature valid
    for callers that do not care.
    """
    found: set[tuple[str, str]] = set()

    try:
        if not os.path.isdir(root):
            # Tolerate being handed a single file directly.
            if os.path.isfile(root) and os.path.basename(root).lower() == "model.bim":
                return [(KIND_MODEL_BIM, os.path.normpath(root))]
            return []
    except OSError:
        return []

    # Directories whose model definition we have already claimed, so the
    # loose-match fallbacks below don't report the same thing twice.
    claimed: set[str] = set()
    errors: list[str] = []

    # AUD-V1-012: `onerror=None` makes os.walk swallow a permission denial
    # and simply yield nothing for that subtree, so an unreadable folder was
    # indistinguishable from an empty one and the scan still looked complete.
    def _on_error(exc: OSError) -> None:
        errors.append(
            f"discover: could not read '{getattr(exc, 'filename', root)}': {exc} "
            "-- anything below it was not discovered"
        )

    try:
        walker = os.walk(root, topdown=True, onerror=_on_error, followlinks=False)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
            try:
                _classify(dirpath, dirnames, filenames, found, claimed)
            except OSError as exc:
                errors.append(f"discover: could not classify '{dirpath}': {exc}")
                continue
    except OSError as exc:
        errors.append(f"discover: walk of '{root}' failed: {exc}")

    if estate is not None:
        for message in errors:
            if message not in estate.errors:
                estate.errors.append(message)
    return sorted(found, key=lambda kp: (_KIND_ORDER.get(kp[0], 99), kp[1]))


def _skip_dir(name: str) -> bool:
    low = name.lower()
    return low.startswith(".") or low in _SKIP_DIRS


def _classify(
    dirpath: str,
    dirnames: list[str],
    filenames: list[str],
    found: set[tuple[str, str]],
    claimed: set[str],
) -> None:
    base = os.path.basename(os.path.normpath(dirpath))
    low = base.lower()
    files = {f.lower(): f for f in filenames}
    subdirs = {d.lower(): d for d in dirnames}
    here = os.path.normpath(dirpath)

    definition = subdirs.get("definition")
    def_path = os.path.join(here, definition) if definition else None

    # --- semantic model folder -------------------------------------------
    is_model_folder = low.endswith(_MODEL_SUFFIXES) or _MODEL_MARKER in files
    if is_model_folder:
        if def_path and _contains_tmdl(def_path):
            found.add((KIND_MODEL, def_path))
            claimed.add(here)
            claimed.add(os.path.normpath(def_path))
        elif "model.bim" in files:
            found.add((KIND_MODEL_BIM, os.path.join(here, files["model.bim"])))
            claimed.add(here)
        # else: partially-matching folder -- nothing usable, stay quiet.

    # --- report folder ----------------------------------------------------
    is_report_folder = low.endswith(_REPORT_SUFFIXES) or _REPORT_MARKER in files
    if is_report_folder and _looks_like_report(here, files, def_path):
        found.add((KIND_REPORT, here))

    # --- loose matches ----------------------------------------------------
    # A `definition/` folder of TMDL whose parent was not recognisable as a
    # semantic model (renamed folder, extracted zip, git subtree...).
    if (
        low == "definition"
        and here not in claimed
        and ("model.tmdl" in files or "database.tmdl" in files)
        and _contains_tmdl(here)
    ):
        parent = os.path.dirname(here)
        if parent not in claimed:
            found.add((KIND_MODEL, here))
            claimed.add(here)

    # A stray model.bim not inside a recognised semantic model folder.
    if "model.bim" in files and here not in claimed:
        found.add((KIND_MODEL_BIM, os.path.join(here, files["model.bim"])))


def _looks_like_report(here: str, files: dict[str, str], def_path: str | None) -> bool:
    if "report.json" in files:  # PBIR-Legacy
        return True
    if not def_path:
        return False
    try:  # PBIR (enhanced report format)
        entries = {e.lower() for e in os.listdir(def_path)}
    except OSError:
        return False
    return bool(entries & {"report.json", "pages", "version.json"})


def _contains_tmdl(path: str, _depth: int = 0) -> bool:
    """True if `path` holds at least one .tmdl file (recursively)."""
    if _depth > 4:
        return False
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return False
    subdirs = []
    for entry in entries:
        try:
            if entry.is_file() and entry.name.lower().endswith(".tmdl"):
                return True
            if entry.is_dir() and not _skip_dir(entry.name):
                subdirs.append(entry.path)
        except OSError:
            continue
    return any(_contains_tmdl(d, _depth + 1) for d in subdirs)


def model_name_from_path(path: str) -> str:
    """Best-effort semantic model name for a file or folder inside a PBIP part.

    ``.../SalesDemo.SemanticModel/definition/tables/Sales.tmdl`` -> ``SalesDemo``.
    Falls back to the containing folder's name so the result is always a
    non-empty, deterministic string.
    """
    try:
        target = os.path.normpath(os.path.abspath(path))
        if os.path.isfile(target):
            target = os.path.dirname(target)
    except OSError:
        target = os.path.normpath(path)

    cur = target
    for _ in range(8):
        base = os.path.basename(cur)
        low = base.lower()
        for suffix in _MODEL_SUFFIXES:
            if low.endswith(suffix) and len(base) > len(suffix):
                return base[: -len(suffix)]
        try:
            if base and os.path.isfile(os.path.join(cur, _MODEL_MARKER)):
                return base
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent

    return os.path.basename(target) or "model"

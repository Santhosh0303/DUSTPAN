"""One guarded boundary for every file the scan opens.

Five audited defects share a single cause: the scan decided a path was
acceptable and then re-opened it *by name*. Between the decision and the open
the name can mean something else.

* **AUD-V3-007** — replacing the authorised file with a symlink after
  `realpath`/`stat` and before `open` read a file outside the scan root. A
  check on a path is not a check on the bytes you end up reading.
* **AUD-V3-009** — a FIFO named `*.tmdl` passed the size check and then
  blocked the scan forever on `open`. A stat before a blocking open cannot
  help; the open itself has to be non-blocking until the descriptor is known
  to be a regular file.
* **AUD-V3-002** — protection was inferred from *successful reads*, so a
  source refused for being over budget was never recorded and could then be
  selected as an output destination and overwritten.
* **AUD-V3-011** — `authorise` returned before the caller's `required=False`
  handling could apply, so a legitimately absent optional file became a
  material error and degraded a healthy report.
* **AUD-V3-003 (partly)** — bytes are returned unchanged; nothing here
  rewrites content for display.

The fix is to traverse and open with descriptors rather than names.
`_open_contained` walks from the scan root one component at a time using
`dir_fd`, with `O_NOFOLLOW` on every component, so neither the final file nor
an intermediate directory can be swapped for a link mid-traversal. The
descriptor is then `fstat`ed: type, size and identity are established on the
object actually opened, not on whatever the name pointed at a moment ago.
"""

from __future__ import annotations

import errno
import os
import stat as stat_mod

from dustpan import limits
from dustpan.ir import Estate

__all__ = [
    "STRICT_CONTAINMENT",
    "protected_paths",
    "read_bytes",
    "read_text",
    "register_protected",
    "was_read",
]

#: True when this platform can enforce descriptor-bound containment. POSIX
#: gives us O_NOFOLLOW and dir_fd; Windows does not, and pretending otherwise
#: would be the silent fallback the audit warned against.
STRICT_CONTAINMENT = hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd

_DEGRADED_PLATFORM_NOTE = (
    "safeio: this platform cannot enforce descriptor-bound containment "
    "(O_NOFOLLOW/dir_fd unavailable), so a file swapped for a symlink between "
    "validation and read cannot be detected. Reads continue with path-based "
    "checks only; treat the scanned tree as trusted and immutable on this "
    "platform."
)


def register_protected(path: str, estate: Estate) -> None:
    """Record a path as a scan source BEFORE any read is attempted.

    AUD-V3-002: protection must not depend on the read succeeding. A file that
    is discovered and then refused -- over budget, unreadable, malformed, a
    FIFO -- is still the user's source and must never become an output
    destination. Both the lexical absolute path and the resolved real path are
    recorded, because either spelling can be handed to `--json`.
    """
    for candidate in _identities(path):
        if candidate not in estate.protected_paths:
            estate.protected_paths.append(candidate)


def protected_paths(estate: Estate) -> set[str]:
    """Every path this scan treats as a source. The do-not-overwrite set."""
    return set(estate.protected_paths) | set(estate.read_paths)


def was_read(estate: Estate) -> set[str]:
    """Paths whose bytes were actually consumed."""
    return set(estate.read_paths)


def read_bytes(
    path: str,
    estate: Estate,
    stage: str,
    *,
    required: bool = True,
) -> bytes | None:
    """Return the bytes of `path`, or None if this read is refused.

    Refusals are recorded as material scan notes -- a file dustpan declined to
    read is missing evidence, and missing evidence must degrade the scan
    rather than quietly shrink it. The single exception is a genuinely absent
    file the caller marked optional (`required=False`, AUD-V3-011); anything
    else about an optional file, including being unreadable or outside
    containment, is still reported.
    """
    register_protected(path, estate)

    if not STRICT_CONTAINMENT:
        _note_once(estate, _DEGRADED_PLATFORM_NOTE)

    root = getattr(estate, "scan_root", None)
    try:
        fd = _open_contained(path, root)
    except FileNotFoundError:
        if not required:
            return None
        _note(estate, f"{stage}: {path}: file not found")
        return None
    except _Refused as refusal:
        _note(estate, f"{stage}: {refusal}")
        return None
    except OSError as exc:
        if exc.errno == errno.ENOENT and not required:
            return None
        _note(estate, f"{stage}: {path}: could not open file: {exc}")
        return None

    try:
        info = os.fstat(fd)
        # AUD-V3-009: decided on the DESCRIPTOR, so a regular file swapped for
        # a FIFO between check and open cannot reach the read below.
        if not stat_mod.S_ISREG(info.st_mode):
            _note(
                estate,
                f"{stage}: refused '{path}' -- it is a "
                f"{_describe(info.st_mode)}, not a regular file. dustpan reads "
                "regular files only; devices, FIFOs and sockets can block a "
                "scan indefinitely.",
            )
            return None

        budget = limits.MAX_FILE_BYTES
        if limits.exceeds(info.st_size, budget):
            _note(
                estate,
                f"{stage}: {path}: "
                + limits.describe(info.st_size, budget, "file", "DUSTPAN_MAX_FILE_BYTES"),
            )
            return None

        # Bounded read: a file that grows after fstat cannot exceed the budget
        # by racing us. One extra byte is requested so growth is detectable.
        cap = info.st_size + 1
        data = _read_exactly(fd, cap)
        if limits.exceeds(len(data), budget):
            _note(
                estate,
                f"{stage}: {path}: grew past the {budget:,}-byte budget while "
                "being read -- skipped.",
            )
            return None
        after = os.fstat(fd)
        if (
            len(data) != info.st_size
            or after.st_size != info.st_size
            or after.st_mtime_ns != info.st_mtime_ns
            or after.st_ctime_ns != info.st_ctime_ns
        ):
            _note(estate, f"{stage}: {path}: file changed while being read -- skipped.")
            return None
    finally:
        os.close(fd)

    real = _identities(path)[-1]
    if real not in estate.read_paths:
        estate.read_paths.append(real)
    return data


def read_text(
    path: str,
    estate: Estate,
    stage: str,
    *,
    required: bool = True,
    encoding: str = "utf-8-sig",
) -> str | None:
    """`read_bytes` decoded. A decoding failure is a material note, not a raise."""
    raw = read_bytes(path, estate, stage, required=required)
    if raw is None:
        return None
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        _note(
            estate,
            f"{stage}: {path}: content is not valid UTF-8 ({encoding}): {exc}",
        )
        return None


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


class _Refused(Exception):
    """A containment or traversal refusal carrying a user-facing reason."""


def _identities(path: str) -> list[str]:
    """Lexical absolute path and resolved real path, in that order, deduped."""
    absolute = os.path.abspath(os.path.expanduser(path))
    try:
        real = os.path.realpath(absolute)
    except OSError:
        real = absolute
    return [absolute] if absolute == real else [absolute, real]


def _open_contained(path: str, root: str | None) -> int:
    """Open `path` for reading without ever following a symlink.

    Walks from `root` one component at a time with `dir_fd`, so an attacker
    replacing an intermediate directory mid-traversal is caught too --
    `O_NOFOLLOW` on the final component alone would not be enough. Opens
    non-blocking so a FIFO cannot stall before its type has been checked.
    """
    absolute = os.path.abspath(os.path.expanduser(path))
    # Windows CRT text mode translates CRLF and treats Ctrl-Z as EOF. The
    # descriptor must return physical bytes for fstat size checks to be valid.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)

    if not STRICT_CONTAINMENT:
        # Documented degraded path: the note above has already been recorded.
        real = os.path.realpath(absolute)
        if root and not _within(real, root):
            raise _Refused(
                f"refused '{path}' -- it resolves to '{real}', outside the scan root"
            )
        return os.open(real, flags)

    if not root:
        return os.open(absolute, flags | os.O_NOFOLLOW)

    real_root = os.path.realpath(root)
    relative = os.path.relpath(absolute, real_root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise _Refused(f"refused '{path}' -- it lies outside the scan root '{real_root}'")

    parts = [p for p in relative.split(os.sep) if p not in ("", os.curdir)]
    expected_root = os.stat(real_root, follow_symlinks=False)
    dir_fd = os.open(real_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened_root = os.fstat(dir_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (
            expected_root.st_dev,
            expected_root.st_ino,
        ):
            raise _Refused(f"refused '{path}' -- scan root changed before opening")
        for part in parts[:-1]:
            try:
                nxt = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                # O_NOFOLLOW|O_DIRECTORY reports a symlinked directory as
                # ENOTDIR on Linux and ELOOP elsewhere. Both mean the same
                # thing here and must say so plainly rather than surfacing a
                # bare "Not a directory" the user cannot act on.
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise _Refused(
                        f"refused '{path}' -- the path component '{part}' is a "
                        "symbolic link, which can resolve outside the scan "
                        "root; dustpan does not follow links out of the "
                        "scanned tree, so this file was not read"
                    ) from exc
                raise
            os.close(dir_fd)
            dir_fd = nxt
        try:
            return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise _Refused(
                    f"refused '{path}' -- it is a symbolic link, which can "
                    "resolve outside the scan root. dustpan reads only files "
                    "that genuinely live inside the scanned tree."
                ) from exc
            raise
    finally:
        os.close(dir_fd)


def _within(candidate: str, root: str) -> bool:
    real_root = os.path.realpath(root)
    return candidate == real_root or candidate.startswith(
        real_root.rstrip(os.sep) + os.sep
    )


def _read_exactly(fd: int, cap: int) -> bytes:
    chunks: list[bytes] = []
    remaining = cap
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _describe(mode: int) -> str:
    if stat_mod.S_ISFIFO(mode):
        return "named pipe (FIFO)"
    if stat_mod.S_ISSOCK(mode):
        return "socket"
    if stat_mod.S_ISBLK(mode):
        return "block device"
    if stat_mod.S_ISCHR(mode):
        return "character device"
    if stat_mod.S_ISDIR(mode):
        return "directory"
    return "non-regular file"


def _note(estate: Estate, message: str) -> None:
    if message not in estate.errors:
        estate.errors.append(message)


_note_once = _note

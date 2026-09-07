"""dustpan command line.

    dustpan scan PATH [--json F] [--markdown F] [--quiet]
                      [--min-confidence F] [--fail-on-findings] [--no-color]
    dustpan version

Exit codes
    0   scan completed, nothing to fail on
    1   high-severity findings present and --fail-on-findings was given
    2   bad usage, unusable path, or an output file could not be written

Public surface (see CONTRACT.md):
    main(argv: list[str] | None = None) -> int
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence

from dustpan import pipeline
from dustpan.ir import IR_VERSION, Estate

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

_EPILOG = """\
examples:
  dustpan scan ./MyProject                     read the report in the terminal
  dustpan scan ./MyProject --markdown out.md   write a report to paste in a ticket
  dustpan scan ./MyProject --json estate.json  full machine-readable dump
  dustpan scan . --quiet --fail-on-findings    CI gate: exit 1 on high-severity findings

Point PATH at the folder holding your .pbip file, or at any folder containing
*.SemanticModel / *.Dataset directories or a model.bim.

Confidence 1.00 means the normalised expressions are identical -- deterministic,
not a guess. Anything lower is a candidate for a human to read, never a delete
order. --min-confidence hides findings below a threshold.
"""


def _confidence(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"'{raw}' is not a number") from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0.0 and 1.0 (got {value:g})")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dustpan",
        description=(
            "Subtractive analysis for BI estates: finds duplicate, unused and "
            "drifting metric definitions."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    scan = sub.add_parser(
        "scan",
        help="scan a folder of Power BI projects",
        description="Scan PATH and report duplicate, unused and unparsed measures.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan.add_argument("path", metavar="PATH", help="folder to scan")
    scan.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing output files (never a scanned source)",
    )
    scan.add_argument(
        "--json",
        metavar="FILE",
        dest="json_path",
        help="write the full estate as JSON",
    )
    scan.add_argument(
        "--markdown",
        metavar="FILE",
        dest="markdown_path",
        help="write a ticket-ready markdown report",
    )
    scan.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing to stdout (errors still go to stderr)",
    )
    scan.add_argument(
        "--min-confidence",
        metavar="FLOAT",
        type=_confidence,
        default=0.0,
        help="hide findings below this confidence (0.0-1.0, default 0.0)",
    )
    scan.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="exit 1 if any high-severity finding survives filtering (for CI)",
    )
    scan.add_argument(
        "--no-color",
        action="store_true",
        help="never emit ANSI colour (NO_COLOR in the environment does the same)",
    )

    sub.add_parser("version", help="print the dustpan version and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not args_list:
        parser.print_help()
        return EXIT_USAGE

    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:  # --help (0) and argparse usage errors (2)
        return int(exc.code or 0)

    try:
        if args.command == "version":
            return _cmd_version()
        if args.command == "scan":
            return _cmd_scan(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130

    parser.print_help()
    return EXIT_USAGE


def _cmd_version() -> int:
    print(f"dustpan {pipeline.version()} (IR v{IR_VERSION})")
    return EXIT_OK


def _cmd_scan(args: argparse.Namespace) -> int:
    root = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.exists(root):
        print(f"dustpan: no such path: {args.path}", file=sys.stderr)
        return EXIT_USAGE
    if not os.path.isdir(root):
        print(
            f"dustpan: {args.path} is a file, not a folder.\n"
            "         Point dustpan at the folder that contains it.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    estate = pipeline.scan(root)
    reported, hidden = pipeline.filter_by_confidence(estate, args.min_confidence)

    written, write_failed = _write_outputs(args, reported, root)

    if not args.quiet:
        from dustpan.output import console

        colour = False if args.no_color else None
        sys.stdout.write(console.render(reported, color=colour))
        if hidden:
            print(
                f"  ({hidden} finding(s) below --min-confidence "
                f"{args.min_confidence:g} hidden)\n"
            )
        for path in written:
            print(f"  wrote {path}")
        if written:
            print()

    if write_failed:
        return EXIT_USAGE

    high = sum(1 for f in reported.findings if f.severity == "high")
    if args.fail_on_findings and high:
        if not args.quiet:
            print(
                f"dustpan: {high} high-severity finding(s) -- failing as requested.",
                file=sys.stderr,
            )
        return EXIT_FINDINGS
    return EXIT_OK


def _requested(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Requested (path, label) destinations, in a stable order."""
    return [
        (p, label)
        for p, label in ((args.json_path, "JSON"), (args.markdown_path, "markdown"))
        if p
    ]


def _scanned_sources(estate: Estate) -> set[str]:
    """Every path this scan treats as a source, by both spellings.

    AUD-V3-002: this used to be derived from files whose bytes were
    successfully READ, so a source refused for being over budget -- or
    unreadable, or malformed, or a FIFO -- was never protected and could be
    handed to `--json` and overwritten. Protection is now recorded at
    discovery, before any read outcome is known. `Metric`/`Asset`
    `source_path` values are still unioned in as a fallback for an Estate
    assembled without going through `scan()`.
    """
    from dustpan import safeio

    paths: set[str] = set(safeio.protected_paths(estate))
    for item in list(estate.metrics) + list(estate.assets):
        source = getattr(item, "source_path", None)
        if source:
            with contextlib.suppress(OSError, ValueError):
                paths.add(os.path.abspath(source))
                paths.add(os.path.realpath(source))
    return paths


def _protected_roots(estate: Estate) -> list[str]:
    """Semantic-model and report directories whose full file set is unknown.

    AUD-V3-002 adjacent case: discovery cannot promise it enumerated every
    file inside a `.SemanticModel` or `.Report` folder -- a file it never
    reached is still part of the user's project. Writing anywhere inside one
    of those namespaces is refused. Everywhere else, including an ordinary
    existing report the user explicitly names, `--force` still works.
    """
    roots: set[str] = set()
    for path in _scanned_sources(estate):
        walked = path
        while True:
            parent, leaf = os.path.split(walked)
            if not parent or parent == walked:
                break
            if leaf.casefold().endswith((".semanticmodel", ".report", ".dataset")):
                roots.add(walked)
                break
            walked = parent
    return sorted(roots)


def _resolve_destination(raw: str) -> str:
    """Normalise a requested output path ONCE (AUD-V3-021).

    Preflight used to check `os.path.expanduser(...)` while the write used the
    raw argument, so a quoted `~/out.json` was validated in the home directory
    and then written to a literal `./~/out.json`. Every later stage --
    protection check, staging, commit, and what is reported to the user --
    uses the single value this returns.
    """
    absolute = os.path.abspath(os.path.expanduser(raw))
    return os.path.realpath(absolute)


def _write_outputs(
    args: argparse.Namespace, estate: Estate, root: str
) -> tuple[list[str], bool]:
    """Preflight, render, then commit every requested output.

    Returns (paths actually written, failed). Every stage -- protection check,
    staging, commit and the destination reported back to the user -- operates
    on the single normalised path produced by `_resolve_destination`
    (AUD-V3-021).
    """
    requested = [
        (_resolve_destination(raw), raw, label)
        for raw, label in ((args.json_path, "JSON"), (args.markdown_path, "markdown"))
        if raw
    ]
    if not requested:
        return [], False

    from dustpan.output import writers

    sources = {os.path.normcase(p) for p in _scanned_sources(estate)}
    protected_roots = _protected_roots(estate)
    seen: dict[str, str] = {}

    for target, raw, label in requested:
        identity = os.path.normcase(target)
        if identity in sources or os.path.normcase(os.path.realpath(target)) in sources:
            print(
                f"dustpan: refusing to write {label} to {raw}\n"
                "         -- that file is a source this scan discovered. dustpan\n"
                "         only ever reads and reports; it will not overwrite your\n"
                "         model, whether or not it managed to read it.",
                file=sys.stderr,
            )
            return [], True
        inside = next((r for r in protected_roots if _under(target, r)), None)
        if inside is not None:
            print(
                f"dustpan: refusing to write {label} inside {inside}\n"
                "         -- that is a scanned semantic model or report. dustpan\n"
                "         cannot prove it enumerated every file in there, so it\n"
                "         will not write anywhere inside it. Choose a path\n"
                "         outside the project.",
                file=sys.stderr,
            )
            return [], True
        if os.path.isdir(target):
            print(
                f"dustpan: {label} destination {raw} is a directory, not a file.",
                file=sys.stderr,
            )
            return [], True
        if identity in seen:
            print(
                f"dustpan: {label} and {seen[identity]} both resolve to {target}\n"
                "         -- refusing to let one output silently replace the other.",
                file=sys.stderr,
            )
            return [], True
        if os.path.exists(target) and not args.force:
            print(
                f"dustpan: {target} already exists -- pass --force to overwrite.",
                file=sys.stderr,
            )
            return [], True
        seen[identity] = label

    rendered: list[tuple[str, str]] = []
    for target, _raw, label in requested:
        renderer = writers.render_json if label == "JSON" else writers.render_markdown
        try:
            rendered.append((target, renderer(estate)))
        except Exception as exc:  # deliberate: never crash on a render bug
            print(f"dustpan: could not render {label}: {exc}", file=sys.stderr)
            return [], True

    try:
        writers.commit_all(rendered)
    except writers.CommitError as exc:
        # AUD-V3-006: report what is actually known, per path, and name the
        # recovery files that still hold the previous bytes. Never claim
        # everything is unchanged unless every state says so.
        print(f"dustpan: could not write outputs: {exc}", file=sys.stderr)
        for path, state in sorted(exc.states.items()):
            print(f"         {path}: {_STATE_WORDS.get(state, state)}", file=sys.stderr)
        for path, snapshot in sorted(exc.recovery.items()):
            print(
                f"         previous bytes of {path} kept at {snapshot}", file=sys.stderr
            )
        if exc.mapping:
            print(f"         recovery index: {exc.mapping}", file=sys.stderr)
        if all(state == "old" for state in exc.states.values()):
            print("         Every destination is unchanged.", file=sys.stderr)
        return [], True
    except OSError as exc:
        print(f"dustpan: could not write outputs: {exc}", file=sys.stderr)
        return [], True
    return [target for target, _raw, _label in requested], False


_STATE_WORDS = {
    "old": "unchanged (previous content)",
    "new": "replaced with this scan's output",
    "absent": "not present -- this run did not create it",
    "uncertain": "UNCERTAIN -- could not be restored; check the recovery file",
}


def _under(target: str, root: str) -> bool:
    target = os.path.normcase(os.path.abspath(target))
    root = os.path.normcase(os.path.abspath(root))
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

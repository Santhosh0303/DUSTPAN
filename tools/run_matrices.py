#!/usr/bin/env python3
"""Run both audit matrices and write their receipts.

    python tools/run_matrices.py --out-dir evidence/

Exits non-zero if any case in either matrix is FAIL, SKIP or ERROR. The two
matrices are separate scenario sets: Matrix A reassesses the baseline
expectations, Matrix B carries the v3 audit's fresh scenarios. Their 80 cases
are unique; running them repeatedly does not create additional cases.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    failures = 0
    for script, name in (
        ("audit_matrix.py", "MATRIX-A.json"),
        ("matrix_b.py", "MATRIX-B.json"),
    ):
        target = os.path.join(args.out_dir, name)
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", script), "--json", target],
            cwd=ROOT,
        )
        print(f"--- {script}: exit {proc.returncode}; receipt {target}")
        failures += proc.returncode != 0
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

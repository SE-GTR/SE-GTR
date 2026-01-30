#!/usr/bin/env python3
"""Wrapper to compute PIT deltas for all projects (before vs after)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch PIT before/after diff wrapper.")
    ap.add_argument(
        "--before-root",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/pit/before"),
        help="Root dir containing before/<project>/mutations.xml",
    )
    ap.add_argument(
        "--after-root",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/pit/after"),
        help="Root dir containing after/<project>/mutations.xml",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/pit/pit_coverage_delta.csv"),
        help="Output CSV path",
    )
    ap.add_argument("--python", type=str, default="python3", help="Python executable")
    args = ap.parse_args()

    diff_script = Path(__file__).resolve().parent / "pit_coverage_diff.py"
    if not diff_script.exists():
        raise SystemExit(f"pit_coverage_diff.py not found: {diff_script}")

    cmd = [
        args.python,
        str(diff_script),
        "--before-root",
        str(args.before_root),
        "--after-root",
        str(args.after_root),
        "--out",
        str(args.out),
    ]
    p = subprocess.run(cmd, check=False)
    if p.returncode != 0:
        raise SystemExit(p.returncode)
    print(f"[OK] PIT delta CSV written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

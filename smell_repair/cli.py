from __future__ import annotations

import argparse
from pathlib import Path

from smell_repair.pipeline import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(prog="smell_repair")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run LLM smell repair pipeline")
    p_run.add_argument("--config", type=Path, required=True)
    p_run.add_argument("--projects-root", type=Path, required=True)
    p_run.add_argument("--smelly-json", type=Path, required=True)
    p_run.add_argument("--out-root", type=Path, required=True)
    p_run.add_argument("--smells-dir", type=Path, default=Path(__file__).resolve().parents[1] / "smells")

    args = parser.parse_args()

    if args.cmd == "run":
        run_dir = run_pipeline(
            config_path=args.config,
            projects_root=args.projects_root,
            smelly_json_path=args.smelly_json,
            out_root=args.out_root,
            smells_dir=args.smells_dir,
        )
        print(str(run_dir))


if __name__ == "__main__":
    main()

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List


def run_ant(project_root: Path, targets: List[str], ant_cmd: str = "ant", timeout_sec: int = 1800) -> str:
    cmd = [ant_cmd] + targets
    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Ant failed (targets={targets})\n{proc.stdout}")
    return proc.stdout

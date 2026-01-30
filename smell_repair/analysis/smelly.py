from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SmellInstance:
    test_method: str
    evidence: Optional[Dict[str, Any]] = None


SmellyJson = Dict[str, Dict[str, List[Any]]]


def load_smelly_json(path: Path) -> SmellyJson:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_smelly_json(raw: SmellyJson) -> Dict[str, Dict[str, List[SmellInstance]]]:
    out: Dict[str, Dict[str, List[SmellInstance]]] = {}
    for test_key, smells in raw.items():
        out[test_key] = {}
        for smell_name, items in smells.items():
            norm: List[SmellInstance] = []
            for it in items:
                if isinstance(it, str):
                    norm.append(SmellInstance(test_method=it))
                elif isinstance(it, dict):
                    tm = it.get("test_method") or it.get("method") or it.get("name") or str(it)

                    # Extended evidence format (preferred):
                    #   {"test_method": "test00", "evidence": {...}}
                    # Fallback: any other keys are treated as evidence.
                    ev: Optional[Dict[str, Any]] = None
                    raw_ev = it.get("evidence")
                    if isinstance(raw_ev, dict):
                        ev = raw_ev
                    else:
                        ev = {k: v for k, v in it.items() if k not in {"test_method", "method", "name"}}
                        if not ev:
                            ev = None

                    norm.append(SmellInstance(test_method=tm, evidence=ev))
                else:
                    norm.append(SmellInstance(test_method=str(it)))
            out[test_key][smell_name] = norm
    return out


def run_smelly(
    *,
    smelly_jar: Path,
    evosuite_runtime_jar: Path,
    junit_jar: Path,
    source_path: Path,
    test_path: Path,
    output_dir: Path,
    output_name: str,
    detectors: int = 0,
    mode: int = 0,
    sufix: str = " ",
    resume_analisis: bool = False,
    java_cmd: str = "java",
    timeout_sec: int = 1800,
) -> Path:
    """Run Smelly and return path to output JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(output_name).name
    if safe_name.endswith(".json"):
        safe_name = safe_name[:-5]
    out_path = output_dir / f"{safe_name}.json"

    cmd = [
        java_cmd,
        "-jar",
        str(smelly_jar),
        "--detectors",
        str(detectors),
        "--evosuitePath",
        str(evosuite_runtime_jar),
        "--junitPath",
        str(junit_jar),
        "--mode",
        str(mode),
        "--outputFilePath",
        str(output_dir),
        "--outputFileName",
        safe_name,
        "--sourcePath",
        str(source_path),
        "--testPath",
        str(test_path),
        "-s",
        sufix,
        "--resumeAnalisis",
        str(resume_analisis).lower(),
    ]

    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout_sec, text=True)
    if not out_path.exists():
        raise FileNotFoundError(f"Smelly did not produce output: {out_path}")
    return out_path

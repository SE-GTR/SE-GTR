from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass
class JsonlLogger:
    path: Path
    verbose: bool = True

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **kwargs: Any) -> None:
        rec: Dict[str, Any] = {"ts": time.time(), "event": event, **kwargs}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.verbose:
            terse = {k: v for k, v in rec.items() if k not in {"prompt", "completion", "diff", "java_source"}}
            print(json.dumps(terse, ensure_ascii=False))

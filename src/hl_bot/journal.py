"""JSONL trade journal."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TradeJournal:
    def __init__(self, path: str | Path = "logs/trades.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": time.time(),
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

"""JSONL trade journal."""

from __future__ import annotations

import json
import logging
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
        bad = 0
        with self.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # One torn line (a crash mid-write, a disk hiccup) must
                    # not crash every later read and the bot with it.
                    bad += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        if bad:
            logging.getLogger(__name__).warning(
                "JOURNAL %s skipped %d unreadable line(s)", self.path, bad
            )
        return rows

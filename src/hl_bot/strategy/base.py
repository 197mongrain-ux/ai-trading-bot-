"""Strategy protocol and signal types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Side = Literal["long", "short", "flat"]


@dataclass(frozen=True)
class Signal:
    side: Side  # long / short / flat
    entry: float
    stop: float
    take_profit: float
    vwap: float
    reason: str = ""


class Strategy(Protocol):
    def on_bar(
        self,
        mark: float,
        bars: list[dict[str, float]],
        *,
        has_position: bool = False,
    ) -> Signal:
        """Produce a signal from mark/mid and session bars."""
        ...

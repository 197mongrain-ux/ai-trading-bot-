"""Hunt universe by New York clock.

NY 09:00–16:00 ET (16:00 exclusive): BTC, ETH, NEAR, PUMP, SOL, LIT, AAVE,
ONDO, WLD, TAO.

Every other time, including the overnight session: BTC, ETH, SOL only.
The cut is the clock, not the weekday — crypto still trades on weekends.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

NY_COINS: tuple[str, ...] = (
    "BTC",
    "ETH",
    "NEAR",
    "PUMP",
    "SOL",
    "LIT",
    "AAVE",
    "ONDO",
    "WLD",
    "TAO",
)
AFTER_HOURS_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL")

# 09:00 inclusive, 16:00 exclusive.
_OPEN_MIN = 9 * 60
_CLOSE_MIN = 16 * 60


def as_ny(now: datetime | float) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=NY)
        return now.astimezone(NY)
    return datetime.fromtimestamp(float(now), tz=NY)


def in_ny_session(now: datetime | float) -> bool:
    ny = as_ny(now)
    minutes = ny.hour * 60 + ny.minute
    return _OPEN_MIN <= minutes < _CLOSE_MIN


def session_coins(now: datetime | float) -> tuple[str, ...]:
    if in_ny_session(now):
        return NY_COINS
    return AFTER_HOURS_COINS

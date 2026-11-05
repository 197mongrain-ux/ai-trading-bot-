"""Build PDH/PDL/WKH/WKL from 1-minute bars on the New York calendar.

Previous day and previous week (Monday–Sunday, America/New_York).
A pool is taken once a later bar, or the last trade, prints through it.
Missing history omits that pool rather than inventing a level.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hl_bot.strategy.model_b.swings import bar_open_sec
from hl_bot.strategy.model_b.types import Pool
from hl_bot.strategy.model_b.universe import NY


def _ny_date(ts: float):
    return datetime.fromtimestamp(bar_open_sec(ts) if ts > 1e11 else ts, tz=NY).date()


def pools_from_bars(
    bars: list[dict],
    now: float,
    last_price: float | None = None,
) -> list[Pool]:
    today = datetime.fromtimestamp(float(now), tz=NY).date()
    yesterday = today - timedelta(days=1)
    this_monday = today - timedelta(days=today.weekday())
    prev_start = this_monday - timedelta(days=7)

    prev_day: list[dict] = []
    prev_week: list[dict] = []
    today_bars: list[dict] = []
    this_week: list[dict] = []
    for bar in bars:
        if "t" not in bar:
            continue
        day = _ny_date(bar_open_sec(float(bar["t"])))
        if day == yesterday:
            prev_day.append(bar)
        elif day == today:
            today_bars.append(bar)
        if prev_start <= day < this_monday:
            prev_week.append(bar)
        elif day >= this_monday:
            this_week.append(bar)

    pools: list[Pool] = []

    def _taken_high(level: float, later: list[dict]) -> bool:
        if last_price is not None and last_price >= level:
            return True
        return any(float(b.get("h", 0) or 0) >= level for b in later)

    def _taken_low(level: float, later: list[dict]) -> bool:
        if last_price is not None and last_price <= level:
            return True
        return any(float(b.get("l", 0) or 0) <= level for b in later)

    if prev_day:
        pdh = max(float(b.get("h", 0) or 0) for b in prev_day)
        pdl = min(float(b.get("l", 0) or 0) for b in prev_day)
        if pdh > 0:
            pools.append(Pool("PDH", pdh, taken=_taken_high(pdh, today_bars)))
        if pdl > 0:
            pools.append(Pool("PDL", pdl, taken=_taken_low(pdl, today_bars)))
    if prev_week:
        wkh = max(float(b.get("h", 0) or 0) for b in prev_week)
        wkl = min(float(b.get("l", 0) or 0) for b in prev_week)
        if wkh > 0:
            pools.append(Pool("WKH", wkh, taken=_taken_high(wkh, this_week)))
        if wkl > 0:
            pools.append(Pool("WKL", wkl, taken=_taken_low(wkl, this_week)))
    return pools

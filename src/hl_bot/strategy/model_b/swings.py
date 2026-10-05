"""Confirmed 1-minute swing fractals.

A swing is the middle bar of three closed 1-minute bars, strictly beyond
both neighbors. The forming minute is not eligible. Model B arms on the
latest confirmed swing that is at least 3 ticks from the last trade
(long → swing low, short → swing high). Closer swings are ignored.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.types import Swing

# Epoch milliseconds are ~1e12. Values below this are treated as seconds
# so tests can pass small bar opens without colliding with live candles.
_MS_CUTOFF = 1e11


def bar_open_sec(t: float) -> float:
    t = float(t)
    if t >= _MS_CUTOFF:
        return t / 1000.0
    return t


def confirmed_swings(
    bars: list[dict],
    *,
    kind: str,
    now: float,
) -> list[Swing]:
    """Return confirmed fractals, oldest first. ``kind`` is ``low`` or ``high``."""
    closed: list[tuple[float, dict]] = []
    for bar in bars:
        if "t" not in bar:
            continue
        open_sec = bar_open_sec(float(bar["t"]))
        if open_sec + 60.0 <= float(now) + 1e-9:
            closed.append((open_sec, bar))
    closed.sort(key=lambda item: item[0])

    found: list[Swing] = []
    for i in range(1, len(closed) - 1):
        prev_b = closed[i - 1][1]
        cur_b = closed[i][1]
        next_b = closed[i + 1][1]
        if kind == "low":
            price = float(cur_b.get("l", 0) or 0)
            if price <= 0:
                continue
            if price < float(prev_b.get("l", 0) or 0) and price < float(
                next_b.get("l", 0) or 0
            ):
                found.append(Swing("low", price, closed[i][0]))
        elif kind == "high":
            price = float(cur_b.get("h", 0) or 0)
            if price <= 0:
                continue
            if price > float(prev_b.get("h", 0) or 0) and price > float(
                next_b.get("h", 0) or 0
            ):
                found.append(Swing("high", price, closed[i][0]))
    return found


def select_swing(
    swings: list[Swing],
    last_price: float,
    tick: float,
    *,
    min_ticks: float = 3.0,
) -> Swing | None:
    """Latest confirmed swing at least ``min_ticks`` from the last trade.

    A swing closer than 3 ticks is ignored. The search walks backward so
    the most recent qualifying swing wins. Exactly 3 ticks is kept.
    """
    if tick <= 0 or last_price <= 0:
        return None
    for swing in reversed(swings):
        dist_ticks = abs(last_price - swing.price) / tick
        if dist_ticks + 1e-9 < min_ticks:
            continue
        return swing
    return None


def swing_id(coin: str, swing: Swing) -> str:
    return f"{coin.upper()}:{swing.kind}:{swing.price:.8f}:{int(swing.ts)}"

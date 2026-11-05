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


def _closed_bars(bars: list[dict], now: float) -> list[tuple[float, dict]]:
    """Closed 1m bars, oldest first. The forming minute is left out."""
    closed: list[tuple[float, dict]] = []
    for bar in bars:
        if "t" not in bar:
            continue
        open_sec = bar_open_sec(float(bar["t"]))
        if open_sec + 60.0 <= float(now) + 1e-9:
            closed.append((open_sec, bar))
    closed.sort(key=lambda item: item[0])
    return closed


def confirmed_swings(
    bars: list[dict],
    *,
    kind: str,
    now: float,
) -> list[Swing]:
    """Return confirmed fractals, oldest first. ``kind`` is ``low`` or ``high``."""
    closed = _closed_bars(bars, now)

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


def closed_prices(
    bars: list[dict],
    *,
    field: str,
    now: float,
) -> list[float]:
    """Positive ``field`` prices of every closed bar, oldest first.

    ``field`` is ``h`` or ``l``. The forming minute is left out. The stop
    uses the whole list so an older high is visible when the last three
    bars never traded through the fill.
    """
    prices: list[float] = []
    for _ts, bar in _closed_bars(bars, now):
        px = float(bar.get(field) or 0)
        if px > 0:
            prices.append(px)
    return prices


def local_bar_extreme(
    bars: list[dict],
    *,
    side: str,
    now: float,
    n: int = 3,
) -> float | None:
    """Lowest low (long) or highest high (short) of the last ``n`` closed bars.

    This is the local liquidity pocket next to the sweep. A wick here that
    runs past the sweep print is the level the stop has to clear. ``None``
    when no closed bar has a positive price.
    """
    closed = _closed_bars(bars, now)
    if not closed or n < 1:
        return None
    recent = [bar for _ts, bar in closed[-n:]]
    if side == "long":
        lows = [float(bar.get("l") or 0) for bar in recent]
        lows = [px for px in lows if px > 0]
        return min(lows) if lows else None
    if side == "short":
        highs = [float(bar.get("h") or 0) for bar in recent]
        highs = [px for px in highs if px > 0]
        return max(highs) if highs else None
    return None


def atr14(bars: list[dict], now: float) -> float | None:
    """Mean of the last 14 true ranges, or ``None`` without 15 closed bars.

    Short test tapes (four bars) must not invent a huge ATR and widen the
    stop. Live candles have the history. A wide live ATR still arms; size
    shrinks with the distance.
    """
    closed = _closed_bars(bars, now)
    if len(closed) < 15:
        return None
    window = [bar for _ts, bar in closed[-15:]]
    ranges: list[float] = []
    for i in range(1, len(window)):
        cur = window[i]
        prev_close = float(window[i - 1].get("c") or 0)
        high = float(cur.get("h") or 0)
        low = float(cur.get("l") or 0)
        if high <= 0 or low <= 0 or high < low:
            return None
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(ranges) < 14:
        return None
    return sum(ranges) / float(len(ranges))

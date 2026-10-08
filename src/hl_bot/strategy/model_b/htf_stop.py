"""Is the liquidity stop beyond the nearest higher-timeframe swing?

Oct 8 2026, xyz:SKHX short: the stop sat at 1198.8, one tick under the
13:45 ET 15m swing high at 1198.9. A tag of that high fills the stop.
The liquidity stop is still taken from the 1m sweep / swing / local
extreme. This check asks whether that price is already beyond the
nearest closed 15m swing on the stop side, plus the same buffer
``place_stop`` uses (``max(3 ticks, 2 bps, 0.5×ATR14)``).

``MODEL_B_HTF_STOP`` default ``shadow`` logs the miss and does not move
the stop, so size, TP, and the 2% / 20x caps stay as they are. ``1``
places the stop beyond that swing plus the buffer and sizes from the
wider distance (still 2% and 20x). ``0`` does not look.

"Nearest" is the closest confirmed 15m fractal on the stop side of the
entry (lowest high above a short, highest low below a long). A swing
the fill has already traded through is not an opposing level.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_bot.strategy.model_b.risk import stop_buffer
from hl_bot.strategy.model_b.structure import resample
from hl_bot.strategy.model_b.trend import PIVOT, _pivots

_TF_SEC = {"15m": 900, "1h": 3600}


@dataclass(frozen=True)
class HtfStopCheck:
    """One coin, one stop, one timeframe."""

    inside: bool
    under_swing: bool
    swing: float | None
    swing_ts: float | None
    beyond: float | None
    buffer: float
    tf: str


def opposing_swings(
    bars: list[dict],
    side: str,
    now: float,
    *,
    tf: str = "15m",
) -> list[tuple[float, float]]:
    """Confirmed stop-side fractals ``(price, bar_open_sec)``, oldest first."""
    tf_sec = _TF_SEC.get(tf)
    if tf_sec is None or side not in ("long", "short"):
        return []
    candles = resample(bars, tf_sec, now)
    field = "h" if side == "short" else "l"
    out: list[tuple[float, float]] = []
    for index, price in _pivots(candles, field, PIVOT):
        if price <= 0 or index >= len(candles):
            continue
        out.append((float(price), float(candles[index]["t"])))
    return out


def nearest_opposing_swing(
    bars: list[dict],
    side: str,
    entry: float,
    now: float,
    *,
    tf: str = "15m",
) -> tuple[float, float] | None:
    """Closest stop-side swing that is still strictly beyond ``entry``."""
    if entry <= 0:
        return None
    best: tuple[float, float] | None = None
    for price, ts in opposing_swings(bars, side, now, tf=tf):
        if side == "short":
            if price <= entry:
                continue
            if best is None or price < best[0]:
                best = (price, ts)
        else:
            if price >= entry:
                continue
            if best is None or price > best[0]:
                best = (price, ts)
    return best


def _snap_away(side: str, price: float, tick: float) -> float:
    if tick <= 0:
        return float(price)
    if side == "long":
        return math.floor(price / tick + 1e-9) * tick
    return math.ceil(price / tick - 1e-9) * tick


def check_htf_stop(
    side: str,
    entry: float,
    stop: float,
    bars: list[dict],
    now: float,
    tick: float,
    atr: float | None = None,
    *,
    tf: str = "15m",
) -> HtfStopCheck:
    """Whether ``stop`` is short of the nearest HTF swing plus buffer.

    ``inside`` is true when the stop is not beyond ``swing + buffer``.
    ``under_swing`` is the tighter miss: the stop is still on the entry
    side of the swing print itself (SKHX: 1198.8 under 1198.9).
    ``beyond`` is the price ``MODEL_B_HTF_STOP=1`` would use. It is
    ``None`` when there is no opposing swing.
    """
    buf = stop_buffer(entry, tick, atr) if entry > 0 and tick > 0 else 0.0
    found = nearest_opposing_swing(bars, side, entry, now, tf=tf)
    if found is None or buf <= 0:
        swing = None if found is None else found[0]
        ts = None if found is None else found[1]
        return HtfStopCheck(False, False, swing, ts, None, buf, tf)
    swing, ts = found
    if side == "short":
        raw = swing + buf
        beyond = _snap_away(side, raw, tick)
        under = float(stop) < float(swing) - abs(swing) * 1e-12
        inside = float(stop) < float(beyond) - abs(beyond) * 1e-12
    else:
        raw = swing - buf
        beyond = _snap_away(side, raw, tick)
        under = float(stop) > float(swing) + abs(swing) * 1e-12
        inside = float(stop) > float(beyond) + abs(beyond) * 1e-12
    return HtfStopCheck(inside, under, swing, ts, beyond, buf, tf)

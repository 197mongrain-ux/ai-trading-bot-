"""Trend read for the TP "too far" decision (Chris, Oct 8 10:48 ET).

The STRUCTURE classifier (structure.py) compares only the last two pivots
and counts the live price past the last swing as a fresh extreme. A
sweep-and-reclaim long is by definition trading under the last swing low,
so it reads ``bear`` on almost every arm (all 12 overnight arms were
``would_block``, every winner included). That shadow log is unchanged;
this module is a separate read used only by the TP decision.

Per timeframe (15m and 1h, resampled from the engine's 1m bars, closed
candles only):

- confirmed fractal swings: a high (low) with ``PIVOT`` candles on each
  side that do not exceed it;
- ``up``: the last ``SWINGS`` swing highs are each higher AND the last
  ``SWINGS`` swing lows are each higher (HH + HL);
- ``down``: lower highs AND lower lows (LH + LL);
- ``range``: anything else;
- break of structure: an ``up`` read with any CLOSED candle after the last
  swing low that closed below it (or ``down`` with a close above the last
  swing high) is downgraded to ``range``. A wick through a swing (the sweep
  itself) is not a break; the live price is not used;
- ``unknown``: not enough candles / swings.

The 1h EMA-50 slope is computed and logged for review only.

"With trend" for a side: at least one of 15m / 1h agrees with the side
(long = up, short = down) and neither timeframe points the other way.
``range`` / ``unknown`` on both = neutral (not with trend).
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.strategy.model_b.structure import resample

TREND_TIMEFRAMES: tuple[str, ...] = ("15m", "1h")
_TF_SEC = {"15m": 900, "1h": 3600}
PIVOT = 2
SWINGS = 2
# Candles read per timeframe: 24h of 15m, 3 days of 1h.
LOOKBACK = {"15m": 96, "1h": 72}
EMA_LEN = 50


@dataclass(frozen=True)
class TfTrend:
    tf: str
    state: str  # up | down | range | unknown
    highs: tuple[float, ...] = ()
    lows: tuple[float, ...] = ()
    broke: str | None = None

    def label(self) -> str:
        return f"{self.tf}={self.state}"


@dataclass(frozen=True)
class TrendRead:
    tfs: tuple[TfTrend, ...]
    ema_slope_1h: str = "unknown"  # up | down | flat | unknown (log only)

    def state(self, tf: str) -> str:
        for item in self.tfs:
            if item.tf == tf:
                return item.state
        return "unknown"

    def with_side(self) -> str:
        """``long`` / ``short`` when one side is with the trend, else ``none``."""
        states = [item.state for item in self.tfs]
        if "up" in states and "down" not in states:
            return "long"
        if "down" in states and "up" not in states:
            return "short"
        return "none"

    def is_with(self, side: str) -> bool:
        return self.with_side() == side

    def label(self) -> str:
        parts = " ".join(item.label() for item in self.tfs)
        return f"{parts} ema50_1h={self.ema_slope_1h} with_side={self.with_side()}"


def _pivots(candles: list[dict], field: str, width: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for i in range(width, len(candles) - width):
        px = candles[i][field]
        left = candles[i - width : i]
        right = candles[i + 1 : i + 1 + width]
        if field == "h":
            if all(px > c["h"] for c in left) and all(px >= c["h"] for c in right):
                out.append((i, px))
        elif all(px < c["l"] for c in left) and all(px <= c["l"] for c in right):
            out.append((i, px))
    return out


def _monotone(xs: list[float], rising: bool) -> bool:
    if len(xs) < 2:
        return False
    return all((b > a) if rising else (b < a) for a, b in zip(xs, xs[1:]))


def tf_trend(
    bars: list[dict],
    tf: str,
    now: float,
    *,
    pivot: int = PIVOT,
    swings: int = SWINGS,
    lookback: int | None = None,
) -> TfTrend:
    tf_sec = _TF_SEC.get(tf)
    if tf_sec is None:
        return TfTrend(tf, "unknown")
    n = int(lookback or LOOKBACK.get(tf, 96))
    candles = resample(bars, tf_sec, now)[-n:]
    if len(candles) < 2 * pivot + 3:
        return TfTrend(tf, "unknown")
    hi_piv = _pivots(candles, "h", pivot)
    lo_piv = _pivots(candles, "l", pivot)
    highs = [px for _i, px in hi_piv]
    lows = [px for _i, px in lo_piv]
    k = max(2, int(swings))
    if len(highs) < 2 or len(lows) < 2:
        return TfTrend(tf, "unknown", tuple(highs[-k:]), tuple(lows[-k:]))
    hs, ls = highs[-k:], lows[-k:]
    if _monotone(hs, True) and _monotone(ls, True):
        state = "up"
    elif _monotone(hs, False) and _monotone(ls, False):
        state = "down"
    else:
        state = "range"
    # Break of structure: any closed candle after the last swing low (high)
    # that CLOSED beyond it. Wicks (the sweep) do not count.
    broke = None
    if state == "up":
        i_low = lo_piv[-1][0]
        if any(float(c["c"]) < ls[-1] for c in candles[i_low + 1 :]):
            state, broke = "range", "closed_below_last_low"
    elif state == "down":
        i_high = hi_piv[-1][0]
        if any(float(c["c"]) > hs[-1] for c in candles[i_high + 1 :]):
            state, broke = "range", "closed_above_last_high"
    return TfTrend(tf, state, tuple(hs), tuple(ls), broke)


def ema_slope(bars: list[dict], now: float, *, tf_sec: int = 3600, length: int = EMA_LEN) -> str:
    candles = resample(bars, tf_sec, now)
    if len(candles) < length + 5:
        return "unknown"
    alpha = 2.0 / (length + 1.0)
    ema = None
    series: list[float] = []
    for c in candles:
        px = float(c["c"])
        ema = px if ema is None else ema + alpha * (px - ema)
        series.append(ema)
    a, b = series[-6], series[-1]
    if a <= 0:
        return "unknown"
    move = (b - a) / a
    if move > 0.0005:
        return "up"
    if move < -0.0005:
        return "down"
    return "flat"


def read_trend(
    bars: list[dict],
    now: float,
    timeframes: tuple[str, ...] | list[str] = TREND_TIMEFRAMES,
) -> TrendRead:
    return TrendRead(
        tuple(tf_trend(bars, tf, now) for tf in timeframes),
        ema_slope(bars, now),
    )

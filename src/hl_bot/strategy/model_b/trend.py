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
_TF_SEC = {"15m": 900, "1h": 3600, "4h": 14400}
PIVOT = 2
SWINGS = 2
# Candles read per timeframe: 24h of 15m, 3 days of 1h.
LOOKBACK = {"15m": 96, "1h": 72, "4h": 90}
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


# --- Macro side filter (Chris, Oct 8 11:05 / 11:07 ET: "trend identification
# 1h and 4h. trade only on the side of macro" / "average directional index to
# use for trend id"). ADX (Wilder, 14) with +DI / -DI on 1h and 4h candles
# resampled from whatever bars are passed (the loop passes ~40 days of 1h
# candles; 1m bars also work with less history -> "unknown" when too short).
ADX_PERIOD = 14
DEFAULT_ADX_MIN = 20.0
MACRO_MODES = ("4h_lead", "both", "4h_only", "4h_lead_1h_fill")
DEFAULT_MACRO_MODE = "4h_lead"
_MACRO_TF_SEC = {"1h": 3600, "4h": 14400}


@dataclass(frozen=True)
class AdxTrend:
    tf: str
    state: str  # up | down | range | unknown
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None

    def label(self) -> str:
        if self.adx is None:
            return f"{self.tf}={self.state}"
        return (
            f"{self.tf}={self.state}(adx={self.adx:.1f} +di={self.plus_di:.1f} "
            f"-di={self.minus_di:.1f})"
        )


def adx_series(candles: list[dict], period: int = ADX_PERIOD) -> list[tuple[float, float, float] | None]:
    """Wilder ADX / +DI / -DI per candle (None until there is enough history)."""
    n = len(candles)
    out: list[tuple[float, float, float] | None] = [None] * n
    if n < 2 * period + 1:
        return out
    tr_s = pdm_s = mdm_s = 0.0
    dx_hist: list[float] = []
    adx: float | None = None
    for i in range(1, n):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        ph, pl = candles[i - 1]["h"], candles[i - 1]["l"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        if i <= period:
            tr_s += tr
            pdm_s += pdm
            mdm_s += mdm
            if i < period:
                continue
        else:
            tr_s = tr_s - tr_s / period + tr
            pdm_s = pdm_s - pdm_s / period + pdm
            mdm_s = mdm_s - mdm_s / period + mdm
        if tr_s <= 0:
            continue
        pdi = 100.0 * pdm_s / tr_s
        mdi = 100.0 * mdm_s / tr_s
        dx = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
        if adx is None:
            dx_hist.append(dx)
            if len(dx_hist) == period:
                adx = sum(dx_hist) / period
        else:
            adx = (adx * (period - 1) + dx) / period
        if adx is not None:
            out[i] = (adx, pdi, mdi)
    return out


def adx_state(adx: float, pdi: float, mdi: float, adx_min: float) -> str:
    if adx < adx_min:
        return "range"
    if pdi > mdi:
        return "up"
    if mdi > pdi:
        return "down"
    return "range"


def tf_adx(
    bars: list[dict],
    tf: str,
    now: float,
    *,
    adx_min: float = DEFAULT_ADX_MIN,
    period: int = ADX_PERIOD,
) -> AdxTrend:
    candles = resample(bars, _MACRO_TF_SEC[tf], now)
    series = adx_series(candles, period)
    last = series[-1] if series else None
    if last is None:
        return AdxTrend(tf, "unknown")
    adx, pdi, mdi = last
    return AdxTrend(tf, adx_state(adx, pdi, mdi, adx_min), adx, pdi, mdi)


def combine_macro(h1: str, h4: str, mode: str = DEFAULT_MACRO_MODE) -> str:
    """1h + 4h -> up | down | range.

    4h_lead (default): 4h trending that way and 1h not trending against it.
    both: both trending the same way. 4h_only: the 4h read alone.
    4h_lead_1h_fill: 4h_lead, and a 4h range takes a trending 1h direction.
    """
    if mode == "both":
        return h1 if h1 == h4 and h1 in ("up", "down") else "range"
    if mode == "4h_only":
        return h4 if h4 in ("up", "down") else "range"
    opposite = {"up": "down", "down": "up"}
    if h4 in ("up", "down"):
        return h4 if h1 != opposite[h4] else "range"
    if mode == "4h_lead_1h_fill" and h1 in ("up", "down"):
        return h1
    return "range"


@dataclass(frozen=True)
class MacroRead:
    h1: AdxTrend
    h4: AdxTrend
    macro: str  # up | down | range
    mode: str = DEFAULT_MACRO_MODE

    def allowed(self, range_policy: str = "both") -> tuple[str, ...]:
        if self.macro == "up":
            return ("long",)
        if self.macro == "down":
            return ("short",)
        return ("long", "short") if range_policy == "both" else ()

    def allows(self, side: str, range_policy: str = "both") -> bool:
        return side in self.allowed(range_policy)

    def label(self, range_policy: str = "both") -> str:
        allowed = ",".join(self.allowed(range_policy)) or "none"
        return f"{self.h1.label()} {self.h4.label()} macro={self.macro} allowed={allowed}"


def read_macro(
    bars: list[dict],
    now: float,
    *,
    adx_min: float = DEFAULT_ADX_MIN,
    mode: str = DEFAULT_MACRO_MODE,
) -> MacroRead:
    h1 = tf_adx(bars, "1h", now, adx_min=adx_min)
    h4 = tf_adx(bars, "4h", now, adx_min=adx_min)
    return MacroRead(h1, h4, combine_macro(h1.state, h4.state, mode), mode)

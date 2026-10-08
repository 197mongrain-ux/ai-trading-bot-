"""Higher-timeframe market structure filter (log + gate).

Model B's bias comes from the nearest draw pool. That alone armed a BTC
long at 22:56 ET on Oct 7 while 15m and 1h were printing lower highs and
lower lows. This module resamples the 1m bars the engine already has into
15m and 1h candles and labels each timeframe:

- ``bear``: the last two confirmed swing highs are falling AND the last
  two confirmed swing lows are falling (LH + LL).
- ``bull``: rising highs AND rising lows (HH + HL).
- ``range``: anything else.
- ``unknown``: not enough closed candles / swings to tell.

A price already beyond the last confirmed swing (below the last swing low,
or above the last swing high) counts as a fresh lower low / higher high.

Longs are allowed only when no checked timeframe is ``bear``; shorts only
when none is ``bull``. ``unknown`` does not block (fail-open on missing
history, logged). The stop, target, and size are not read or moved here.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.strategy.model_b.swings import bar_open_sec

STRUCTURE = "STRUCTURE"
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("15m", "1h")
_TF_SEC = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
# Fractal half-width on the resampled candles (2 = 5-bar pivot).
PIVOT = 2
# Candles kept per timeframe (12h of 15m, 2 days of 1h).
LOOKBACK_BARS = 48


@dataclass(frozen=True)
class TfStructure:
    tf: str
    state: str  # bull | bear | range | unknown
    highs: tuple[float, ...] = ()
    lows: tuple[float, ...] = ()

    def label(self) -> str:
        return f"{self.tf}:{self.state}"


def resample(bars: list[dict], tf_sec: int, now: float) -> list[dict]:
    """Closed ``tf_sec`` candles from 1m bars, oldest first. Forming bucket dropped."""
    buckets: dict[int, dict] = {}
    for bar in bars:
        if "t" not in bar:
            continue
        open_sec = bar_open_sec(float(bar["t"]))
        if open_sec + 60.0 > float(now) + 1e-9:
            continue
        try:
            hi = float(bar.get("h", 0) or 0)
            lo = float(bar.get("l", 0) or 0)
            cl = float(bar.get("c", 0) or 0)
        except (TypeError, ValueError):
            continue
        if hi <= 0 or lo <= 0:
            continue
        key = int(open_sec // tf_sec) * tf_sec
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = {"t": key, "h": hi, "l": lo, "c": cl, "_last": open_sec}
        else:
            cur["h"] = max(cur["h"], hi)
            cur["l"] = min(cur["l"], lo)
            if open_sec >= cur["_last"]:
                cur["c"] = cl
                cur["_last"] = open_sec
    out = [
        b for k, b in sorted(buckets.items()) if k + tf_sec <= float(now) + 1e-9
    ]
    return out


def _pivots(candles: list[dict], field: str, width: int) -> list[float]:
    found: list[float] = []
    for i in range(width, len(candles) - width):
        px = candles[i][field]
        left = candles[i - width : i]
        right = candles[i + 1 : i + 1 + width]
        if field == "h":
            if all(px > c["h"] for c in left) and all(px >= c["h"] for c in right):
                found.append(px)
        else:
            if all(px < c["l"] for c in left) and all(px <= c["l"] for c in right):
                found.append(px)
    return found


def classify(
    bars: list[dict],
    tf: str,
    now: float,
    last_px: float | None = None,
    *,
    pivot: int = PIVOT,
    lookback: int = LOOKBACK_BARS,
) -> TfStructure:
    tf_sec = _TF_SEC.get(tf)
    if tf_sec is None:
        return TfStructure(tf, "unknown")
    candles = resample(bars, tf_sec, now)[-int(lookback):]
    if len(candles) < 2 * pivot + 3:
        return TfStructure(tf, "unknown")
    highs = _pivots(candles, "h", pivot)
    lows = _pivots(candles, "l", pivot)
    if last_px is not None and last_px > 0:
        if lows and last_px < lows[-1]:
            lows = lows + [float(last_px)]
        if highs and last_px > highs[-1]:
            highs = highs + [float(last_px)]
    if len(highs) < 2 or len(lows) < 2:
        return TfStructure(tf, "unknown", tuple(highs[-2:]), tuple(lows[-2:]))
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    if h2 < h1 and l2 < l1:
        state = "bear"
    elif h2 > h1 and l2 > l1:
        state = "bull"
    else:
        state = "range"
    return TfStructure(tf, state, (h1, h2), (l1, l2))


def structure_states(
    bars: list[dict],
    now: float,
    last_px: float | None = None,
    timeframes: tuple[str, ...] | list[str] = DEFAULT_TIMEFRAMES,
) -> list[TfStructure]:
    return [classify(bars, tf, now, last_px) for tf in timeframes]


def structure_blocks(side: str, states: list[TfStructure]) -> bool:
    """True when the side fights a checked timeframe's structure."""
    against = "bear" if side == "long" else "bull"
    return any(item.state == against for item in states)


def structure_label(states: list[TfStructure]) -> str:
    return ",".join(item.label() for item in states) or "-"

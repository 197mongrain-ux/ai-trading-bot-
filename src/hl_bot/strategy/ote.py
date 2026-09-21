"""OTE (Optimal Trade Entry) pullback add-on — ICT-lite Fibonacci zone.

Optimal Trade Entry ≈ the 62%–79% Fibonacci retracement of the most recent
impulse swing, taken **with** VWAP bias (never counter-trend).

Pure functions + thin helpers so unit tests can inject synthetic swings offline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImpulseSwing:
    """Bullish impulse = swing_low → swing_high; bearish = swing_high → swing_low."""

    swing_low: float
    swing_high: float
    low_index: int
    high_index: int
    direction: str  # "bull" | "bear"


@dataclass(frozen=True)
class OteZone:
    """Fibonacci OTE band between fib_shallow (0.62) and fib_deep (0.79)."""

    zone_low: float
    zone_high: float
    swing_low: float
    swing_high: float
    direction: str  # "long" | "short"


def find_bullish_impulse(
    bars: list[dict[str, float]],
    *,
    lookback: int = 45,
) -> ImpulseSwing | None:
    """Most recent bullish impulse in the lookback window.

    Swing low = lowest low in the last ``lookback`` bars (excluding the
    unfinished/current bar when present). Swing high = highest high that
    occurs **after** that low. Requires at least one bar after the low.
    """
    if lookback < 3 or len(bars) < 3:
        return None
    # Prefer completed bars: drop latest if we have enough history
    completed = bars[:-1] if len(bars) > lookback else bars
    window = completed[-lookback:]
    if len(window) < 3:
        return None

    lows = [float(b.get("l", b.get("c", 0))) for b in window]
    highs = [float(b.get("h", b.get("c", 0))) for b in window]
    low_i = min(range(len(lows)), key=lambda i: lows[i])
    if low_i >= len(window) - 1:
        return None  # no room for a subsequent high
    after_highs = highs[low_i + 1 :]
    if not after_highs:
        return None
    rel = max(range(len(after_highs)), key=lambda i: after_highs[i])
    high_i = low_i + 1 + rel
    swing_low = lows[low_i]
    swing_high = highs[high_i]
    if swing_high <= swing_low:
        return None
    return ImpulseSwing(
        swing_low=swing_low,
        swing_high=swing_high,
        low_index=low_i,
        high_index=high_i,
        direction="bull",
    )


def find_bearish_impulse(
    bars: list[dict[str, float]],
    *,
    lookback: int = 45,
) -> ImpulseSwing | None:
    """Most recent bearish impulse: swing high → subsequent swing low."""
    if lookback < 3 or len(bars) < 3:
        return None
    completed = bars[:-1] if len(bars) > lookback else bars
    window = completed[-lookback:]
    if len(window) < 3:
        return None

    lows = [float(b.get("l", b.get("c", 0))) for b in window]
    highs = [float(b.get("h", b.get("c", 0))) for b in window]
    high_i = max(range(len(highs)), key=lambda i: highs[i])
    if high_i >= len(window) - 1:
        return None
    after_lows = lows[high_i + 1 :]
    if not after_lows:
        return None
    rel = min(range(len(after_lows)), key=lambda i: after_lows[i])
    low_i = high_i + 1 + rel
    swing_high = highs[high_i]
    swing_low = lows[low_i]
    if swing_high <= swing_low:
        return None
    return ImpulseSwing(
        swing_low=swing_low,
        swing_high=swing_high,
        low_index=low_i,
        high_index=high_i,
        direction="bear",
    )


def ote_zone_long(
    swing_low: float,
    swing_high: float,
    *,
    fib_shallow: float = 0.62,
    fib_deep: float = 0.79,
) -> OteZone | None:
    """Long OTE: retracement down from impulse high into 62–79% band.

    zone_high = high - 0.62*(high-low)  (shallower)
    zone_low  = high - 0.79*(high-low)  (deeper)
    """
    if swing_high <= swing_low or fib_shallow <= 0 or fib_deep <= fib_shallow:
        return None
    rng = swing_high - swing_low
    zone_high = swing_high - fib_shallow * rng
    zone_low = swing_high - fib_deep * rng
    return OteZone(
        zone_low=zone_low,
        zone_high=zone_high,
        swing_low=swing_low,
        swing_high=swing_high,
        direction="long",
    )


def ote_zone_short(
    swing_low: float,
    swing_high: float,
    *,
    fib_shallow: float = 0.62,
    fib_deep: float = 0.79,
) -> OteZone | None:
    """Short OTE: retracement up from impulse low into 62–79% band.

    zone_low  = low + 0.62*(high-low)
    zone_high = low + 0.79*(high-low)
    """
    if swing_high <= swing_low or fib_shallow <= 0 or fib_deep <= fib_shallow:
        return None
    rng = swing_high - swing_low
    zone_low = swing_low + fib_shallow * rng
    zone_high = swing_low + fib_deep * rng
    return OteZone(
        zone_low=zone_low,
        zone_high=zone_high,
        swing_low=swing_low,
        swing_high=swing_high,
        direction="short",
    )


def mark_in_zone(mark: float, zone: OteZone) -> bool:
    return zone.zone_low <= mark <= zone.zone_high


def ote_stop_long(
    entry: float,
    zone_low: float,
    *,
    stop_pct: float,
    stop_buffer: float = 0.0,
) -> float:
    """Long stop: prefer zone_low − buffer, but clamp max risk to STOP_PCT.

    Risk distance = min(entry * STOP_PCT, entry − (zone_low − buffer)).
    Equivalently stop price = max(entry*(1−STOP_PCT), zone_low − buffer)
    when the zone stop is still below entry; otherwise fall back to STOP_PCT.

    Documented rule: if the swing/zone stop is **wider** than STOP_PCT from
    entry, use STOP_PCT so R stays scalp-sized. If the zone stop is tighter,
    use the zone stop.
    """
    if entry <= 0 or stop_pct <= 0:
        return 0.0
    pct_stop = entry * (1.0 - stop_pct)
    swing_stop = zone_low - stop_buffer
    if swing_stop >= entry:
        # zone above entry — unusable; use pct stop
        return pct_stop
    # Higher stop price = tighter for longs → pick max of the two floors
    return max(pct_stop, swing_stop)


def ote_stop_short(
    entry: float,
    zone_high: float,
    *,
    stop_pct: float,
    stop_buffer: float = 0.0,
) -> float:
    """Short stop: prefer zone_high + buffer, clamp max risk to STOP_PCT."""
    if entry <= 0 or stop_pct <= 0:
        return 0.0
    pct_stop = entry * (1.0 + stop_pct)
    swing_stop = zone_high + stop_buffer
    if swing_stop <= entry:
        return pct_stop
    # Lower stop price = tighter for shorts → pick min of the two ceilings
    return min(pct_stop, swing_stop)


def last_bar_bullish(bars: list[dict[str, float]]) -> bool:
    if not bars:
        return False
    b = bars[-1]
    return float(b.get("c", 0)) > float(b.get("o", 0))


def last_bar_bearish(bars: list[dict[str, float]]) -> bool:
    if not bars:
        return False
    b = bars[-1]
    return float(b.get("c", 0)) < float(b.get("o", 0))


def evaluate_ote_long(
    mark: float,
    bars: list[dict[str, float]],
    *,
    lookback: int = 45,
    fib_shallow: float = 0.62,
    fib_deep: float = 0.79,
    stop_pct: float = 0.0015,
    stop_buffer_bps: float = 0.0,
    require_close: bool = False,
    swing_bars: list[dict[str, float]] | None = None,
) -> tuple[str, float, float, OteZone | None]:
    """Try a long OTE entry.

    Returns ``(reason, stop, tp_unused_placeholder, zone)``.
    On success reason is ``ote_long`` and stop > 0.
    On failure: ``ote_no_swing`` / ``ote_outside_zone`` / ``ote_no_close`` /
    ``bad_risk`` with stop=0.
    """
    src = swing_bars if swing_bars is not None else bars
    impulse = find_bullish_impulse(src, lookback=lookback)
    if impulse is None:
        return "ote_no_swing", 0.0, 0.0, None
    zone = ote_zone_long(
        impulse.swing_low,
        impulse.swing_high,
        fib_shallow=fib_shallow,
        fib_deep=fib_deep,
    )
    if zone is None:
        return "ote_no_swing", 0.0, 0.0, None
    if not mark_in_zone(mark, zone):
        return "ote_outside_zone", 0.0, 0.0, zone
    if require_close and not last_bar_bullish(bars):
        return "ote_no_close", 0.0, 0.0, zone
    buf = mark * (stop_buffer_bps / 10_000.0)
    stop = ote_stop_long(mark, zone.zone_low, stop_pct=stop_pct, stop_buffer=buf)
    if stop <= 0 or stop >= mark:
        return "bad_risk", 0.0, 0.0, zone
    return "ote_long", stop, 0.0, zone


def evaluate_ote_short(
    mark: float,
    bars: list[dict[str, float]],
    *,
    lookback: int = 45,
    fib_shallow: float = 0.62,
    fib_deep: float = 0.79,
    stop_pct: float = 0.0015,
    stop_buffer_bps: float = 0.0,
    require_close: bool = False,
    swing_bars: list[dict[str, float]] | None = None,
) -> tuple[str, float, float, OteZone | None]:
    """Try a short OTE entry (mirror of long)."""
    src = swing_bars if swing_bars is not None else bars
    impulse = find_bearish_impulse(src, lookback=lookback)
    if impulse is None:
        return "ote_no_swing", 0.0, 0.0, None
    zone = ote_zone_short(
        impulse.swing_low,
        impulse.swing_high,
        fib_shallow=fib_shallow,
        fib_deep=fib_deep,
    )
    if zone is None:
        return "ote_no_swing", 0.0, 0.0, None
    if not mark_in_zone(mark, zone):
        return "ote_outside_zone", 0.0, 0.0, zone
    if require_close and not last_bar_bearish(bars):
        return "ote_no_close", 0.0, 0.0, zone
    buf = mark * (stop_buffer_bps / 10_000.0)
    stop = ote_stop_short(mark, zone.zone_high, stop_pct=stop_pct, stop_buffer=buf)
    if stop <= 0 or stop <= mark:
        return "bad_risk", 0.0, 0.0, zone
    return "ote_short", stop, 0.0, zone

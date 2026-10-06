"""Log-only volume-profile tags for Model B.

The arm path must not read this. ``vp_as_filter``, ``vp_entries``, and
``vp_enabled`` stay off in ``volume_profile``. A profile miss or a raised
error becomes ``vp_tag=none`` or ``vp_tag=vp_error`` with null numbers.
It does not change the order.

Catalyst heuristic (fail soft to false):

Look at the 15 minutes that end 15 minutes before the local 1m swing,
i.e. bars and prints with time in ``[swing_ts - 30m, swing_ts - 15m)``.
The flag is true only when both of these hold for the side being logged:

1. Range break. Split that window at its midpoint. A long breaks when the
   later half's high is strictly above the earlier half's high. A short
   breaks when the later half's low is strictly below the earlier half's
   low. Each half needs at least 3 bars.
2. One-sided aggressor delta. Prints in the same window that have a side
   satisfy ``(buy - sell) / (buy + sell) >= 0.60`` for a long, or
   ``<= -0.60`` for a short.

Missing swing time, too few bars, no sided prints, or any exception → false.
"""

from __future__ import annotations

from hl_bot.strategy.volume_profile import (
    VolumeProfile,
    compute_profile,
    distance_bps,
    nearest_lvn_on_side,
    vp_touch_level,
)

VP_LOG_KEYS = (
    "vp_poc",
    "vp_vah",
    "vp_val",
    "nearest_lvn_on_side",
    "sweep_to_val_bps",
    "sweep_to_lvn_bps",
    "vp_tag",
    "catalyst_flag",
)

# 15 minutes, in seconds. The catalyst window is the quarter-hour before
# the quarter-hour that precedes the swing.
_CATALYST_INNER_SEC = 15.0 * 60.0
_CATALYST_OUTER_SEC = 30.0 * 60.0
_CATALYST_DELTA = 0.60
_CATALYST_MIN_HALF = 3


def vp_blank_fields(*, tag: str = "none") -> dict:
    return {
        "vp_poc": None,
        "vp_vah": None,
        "vp_val": None,
        "nearest_lvn_on_side": None,
        "sweep_to_val_bps": None,
        "sweep_to_lvn_bps": None,
        "vp_tag": tag,
        "catalyst_flag": False,
    }


def vp_error_fields() -> dict:
    return vp_blank_fields(tag="vp_error")


def vp_log_fields(
    bars: list[dict] | None,
    *,
    now: float,
    prints: list,
    side: str | None,
    sweep: float | None,
    swing_ts: float | None,
    ref_price: float | None,
) -> dict:
    """Journal fields for one Model B evaluation. Never raises."""
    try:
        profile = compute_profile(bars, now=now)
    except Exception:
        return vp_error_fields()
    if profile.error:
        return vp_error_fields()
    if not profile.ok or profile.poc is None or profile.val is None or profile.vah is None:
        return vp_blank_fields(tag="none")
    try:
        return _fields_from_profile(
            profile,
            bars=bars or [],
            prints=prints or [],
            side=side,
            sweep=sweep,
            swing_ts=swing_ts,
            ref_price=ref_price,
        )
    except Exception:
        return vp_error_fields()


def _fields_from_profile(
    profile: VolumeProfile,
    *,
    bars: list[dict],
    prints: list,
    side: str | None,
    sweep: float | None,
    swing_ts: float | None,
    ref_price: float | None,
) -> dict:
    touch_px = sweep if sweep is not None and sweep > 0 else ref_price
    lvn_ref = touch_px if touch_px is not None and touch_px > 0 else None
    lvn = nearest_lvn_on_side(profile.lvns, profile.poc, side, lvn_ref)
    on_val = bool(touch_px and profile.val and vp_touch_level(touch_px, profile.val))
    on_lvn = bool(touch_px and lvn and vp_touch_level(touch_px, lvn))
    if on_val and on_lvn:
        tag = "val+lvn"
    elif on_val:
        tag = "val"
    elif on_lvn:
        tag = "lvn"
    else:
        tag = "none"
    return {
        "vp_poc": profile.poc,
        "vp_vah": profile.vah,
        "vp_val": profile.val,
        "nearest_lvn_on_side": lvn,
        "sweep_to_val_bps": distance_bps(sweep, profile.val),
        "sweep_to_lvn_bps": distance_bps(sweep, lvn),
        "vp_tag": tag,
        "catalyst_flag": catalyst_flag(
            bars,
            prints,
            side=side,
            swing_ts=swing_ts,
        ),
    }


def catalyst_flag(
    bars: list[dict],
    prints: list,
    *,
    side: str | None,
    swing_ts: float | None,
) -> bool:
    """True when the pre-swing window broke its range on a one-sided tape.

    See the module docstring for the exact window and thresholds. Any gap
    in the inputs returns false.
    """
    try:
        if side not in ("long", "short") or swing_ts is None:
            return False
        start = float(swing_ts) - _CATALYST_OUTER_SEC
        end = float(swing_ts) - _CATALYST_INNER_SEC
        mid = (start + end) / 2.0
        earlier: list[dict] = []
        later: list[dict] = []
        for bar in bars:
            if "t" not in bar:
                continue
            t = float(bar["t"])
            if t >= 1e11:
                t = t / 1000.0
            if t < start or t >= end:
                continue
            (earlier if t < mid else later).append(bar)
        if len(earlier) < _CATALYST_MIN_HALF or len(later) < _CATALYST_MIN_HALF:
            return False
        if side == "long":
            broke = _max_high(later) > _max_high(earlier)
        else:
            broke = _min_low(later) < _min_low(earlier)
        if not broke:
            return False
        buy = 0.0
        sell = 0.0
        for print_ in prints:
            ts = float(getattr(print_, "ts", 0) or 0)
            if ts < start or ts >= end:
                continue
            px_side = getattr(print_, "side", None)
            size = float(getattr(print_, "size", 0) or 0)
            if size <= 0 or px_side not in ("buy", "sell"):
                continue
            if px_side == "buy":
                buy += size
            else:
                sell += size
        total = buy + sell
        if total <= 0:
            return False
        delta = (buy - sell) / total
        if side == "long":
            return delta + 1e-12 >= _CATALYST_DELTA
        return delta - 1e-12 <= -_CATALYST_DELTA
    except Exception:
        return False


def _max_high(bars: list[dict]) -> float:
    highs = [float(bar.get("h") or 0) for bar in bars]
    highs = [px for px in highs if px > 0]
    if not highs:
        raise ValueError("no high")
    return max(highs)


def _min_low(bars: list[dict]) -> float:
    lows = [float(bar.get("l") or 0) for bar in bars]
    lows = [px for px in lows if px > 0]
    if not lows:
        raise ValueError("no low")
    return min(lows)

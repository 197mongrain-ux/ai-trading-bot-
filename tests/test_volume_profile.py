"""Session volume profile and Model B log-only VAL/LVN tags."""

from __future__ import annotations

import pytest

from hl_bot.strategy.model_b.types import TradePrint
from hl_bot.strategy.model_b.vp_log import (
    VP_LOG_KEYS,
    catalyst_flag,
    vp_log_fields,
)
from hl_bot.strategy.volume_profile import (
    VP_AS_FILTER,
    VP_ENABLED,
    VP_ENTRIES,
    VP_TOUCH_BPS,
    compute_profile,
    distance_bps,
    nearest_lvn_on_side,
    vp_touch_level,
)

# 2026-10-05 10:00 ET, matching the Model B fixtures.
_NOW = 1_759_672_800.0


def _flat(now: float, n: int, price: float, vol: float, start_offset: float) -> list[dict]:
    bars = []
    for i in range(n):
        bars.append(
            {
                "t": now + start_offset + i * 60.0,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
                "v": vol,
            }
        )
    return bars


def _profile_bars(now: float) -> list[dict]:
    """Heavy node at 100, thin nodes at 98 and 102, small outer nodes."""
    return (
        _flat(now, 8, 96.0, 50.0, -50 * 60)
        + _flat(now, 6, 98.0, 5.0, -40 * 60)
        + _flat(now, 16, 100.0, 800.0, -30 * 60)
        + _flat(now, 6, 102.0, 5.0, -12 * 60)
        + _flat(now, 8, 104.0, 50.0, -6 * 60)
    )


def test_touch_is_twelve_bps_and_switches_stay_off():
    assert VP_TOUCH_BPS == 12.0
    assert VP_AS_FILTER is False
    assert VP_ENTRIES is False
    assert VP_ENABLED is False
    assert vp_touch_level(100.0, 100.12) is True
    assert vp_touch_level(100.0, 99.88) is True
    assert vp_touch_level(100.0, 100.13) is False
    assert vp_touch_level(0.0, 100.0) is False
    assert distance_bps(100.0, 100.12) == pytest.approx(12.0)
    assert distance_bps(100.0, 99.0) < 0


def test_profile_finds_poc_value_area_and_lvns():
    profile = compute_profile(_profile_bars(_NOW), now=_NOW)
    assert profile.ok is True
    assert profile.error is None
    assert profile.poc == pytest.approx(100.0, abs=0.5)
    assert profile.val is not None and profile.vah is not None
    assert profile.val <= profile.poc <= profile.vah
    assert any(abs(px - 98.0) < 1.0 for px in profile.lvns)
    assert any(abs(px - 102.0) < 1.0 for px in profile.lvns)
    long_lvn = nearest_lvn_on_side(profile.lvns, profile.poc, "long", 99.0)
    short_lvn = nearest_lvn_on_side(profile.lvns, profile.poc, "short", 101.0)
    assert long_lvn is not None and long_lvn <= profile.poc
    assert short_lvn is not None and short_lvn >= profile.poc
    assert abs(long_lvn - 98.0) < 1.0
    assert abs(short_lvn - 102.0) < 1.0


def test_too_few_bars_and_bad_rows_do_not_raise():
    thin = compute_profile(_profile_bars(_NOW)[:4], now=_NOW)
    assert thin.ok is False
    assert thin.error is None
    assert thin.poc is None

    fields = vp_log_fields(
        _profile_bars(_NOW)[:4],
        now=_NOW,
        prints=[],
        side="long",
        sweep=99.0,
        swing_ts=_NOW,
        ref_price=104.0,
    )
    assert fields["vp_tag"] == "none"
    assert fields["catalyst_flag"] is False
    for key in (
        "vp_poc",
        "vp_vah",
        "vp_val",
        "nearest_lvn_on_side",
        "sweep_to_val_bps",
        "sweep_to_lvn_bps",
    ):
        assert fields[key] is None

    broken = vp_log_fields(
        [{"t": _NOW - 60, "h": "nope", "l": 1, "v": 1}] * 40,
        now=_NOW,
        prints=[],
        side="long",
        sweep=99.0,
        swing_ts=None,
        ref_price=99.0,
    )
    assert broken["vp_tag"] == "vp_error"
    assert broken["vp_poc"] is None
    assert broken["catalyst_flag"] is False


def test_tag_is_val_lvn_or_both_from_the_sweep():
    bars = _profile_bars(_NOW)
    profile = compute_profile(bars, now=_NOW)
    assert profile.ok and profile.val is not None and profile.poc is not None
    on_val = vp_log_fields(
        bars,
        now=_NOW,
        prints=[],
        side="long",
        sweep=profile.val,
        swing_ts=None,
        ref_price=104.0,
    )
    assert on_val["vp_tag"] in ("val", "val+lvn")
    assert on_val["vp_poc"] == profile.poc
    assert on_val["vp_val"] == profile.val
    assert on_val["vp_vah"] == profile.vah
    assert on_val["sweep_to_val_bps"] == pytest.approx(0.0, abs=1e-6)
    assert on_val["nearest_lvn_on_side"] is not None
    assert on_val["nearest_lvn_on_side"] <= profile.poc

    lvn = on_val["nearest_lvn_on_side"]
    on_lvn = vp_log_fields(
        bars,
        now=_NOW,
        prints=[],
        side="long",
        sweep=lvn,
        swing_ts=None,
        ref_price=104.0,
    )
    assert "lvn" in on_lvn["vp_tag"]
    assert on_lvn["sweep_to_lvn_bps"] == pytest.approx(0.0, abs=1e-6)

    far = vp_log_fields(
        bars,
        now=_NOW,
        prints=[],
        side="long",
        sweep=80.0,
        swing_ts=None,
        ref_price=80.0,
    )
    assert far["vp_tag"] == "none"
    assert far["sweep_to_val_bps"] is not None
    assert far["sweep_to_val_bps"] > 0  # VAL is above a sweep at 80


def test_catalyst_needs_a_range_break_and_one_sided_delta():
    swing = _NOW
    start = swing - 30 * 60
    mid = swing - 22.5 * 60
    end = swing - 15 * 60
    bars = []
    for i in range(5):
        bars.append({"t": start + i * 60, "h": 100.0, "l": 99.0, "c": 99.5, "v": 1})
    for i in range(5):
        bars.append({"t": mid + i * 60, "h": 101.5, "l": 100.0, "c": 101.0, "v": 1})
    assert bars[-1]["t"] < end
    buys = [
        TradePrint(ts=start + 60, coin="BTC", price=101.0, size=10.0, side="buy", seq=i)
        for i in range(4)
    ]
    sell = [TradePrint(ts=start + 120, coin="BTC", price=101.0, size=1.0, side="sell", seq=9)]
    assert catalyst_flag(bars, buys + sell, side="long", swing_ts=swing) is True
    # Same break, balanced tape: not a catalyst.
    flat = buys + [
        TradePrint(ts=start + 180, coin="BTC", price=101.0, size=40.0, side="sell", seq=10)
    ]
    assert catalyst_flag(bars, flat, side="long", swing_ts=swing) is False
    # Break the other way does not count as a long catalyst.
    down = []
    for i in range(5):
        down.append({"t": start + i * 60, "h": 100.0, "l": 99.0, "c": 99.5, "v": 1})
    for i in range(5):
        down.append({"t": mid + i * 60, "h": 99.5, "l": 98.0, "c": 98.2, "v": 1})
    assert catalyst_flag(down, buys + sell, side="long", swing_ts=swing) is False
    sells = [
        TradePrint(ts=start + 60, coin="BTC", price=98.0, size=10.0, side="sell", seq=i)
        for i in range(4)
    ]
    assert catalyst_flag(down, sells, side="short", swing_ts=swing) is True
    assert catalyst_flag(bars, buys, side="long", swing_ts=None) is False
    assert catalyst_flag([], buys, side="long", swing_ts=swing) is False
    assert catalyst_flag(bars, [], side="long", swing_ts=swing) is False
    # A bad bar in the window fails soft.
    assert catalyst_flag([{"t": object()}], buys, side="long", swing_ts=swing) is False


def test_log_keys_match_the_journal_schema():
    fields = vp_log_fields([], now=_NOW, prints=[], side=None, sweep=None, swing_ts=None, ref_price=None)
    assert tuple(fields) == VP_LOG_KEYS

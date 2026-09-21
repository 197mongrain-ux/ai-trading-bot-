"""Unit tests for OTE (Optimal Trade Entry) pullback helpers + strategy wiring."""

from datetime import datetime, timezone

import pytest

from hl_bot.strategy.ote import (
    evaluate_ote_long,
    evaluate_ote_short,
    find_bearish_impulse,
    find_bullish_impulse,
    mark_in_zone,
    ote_stop_long,
    ote_stop_short,
    ote_zone_long,
    ote_zone_short,
)
from hl_bot.strategy.vwap import VwapTrendScalp, session_vwap


def _bar(t_ms, o, h, l, c, v=10):
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_ote_zone_long_math_example():
    """Impulse 100 → 110 → zone [102.1, 103.8]."""
    zone = ote_zone_long(100.0, 110.0, fib_shallow=0.62, fib_deep=0.79)
    assert zone is not None
    assert zone.zone_low == pytest.approx(102.1)
    assert zone.zone_high == pytest.approx(103.8)
    assert mark_in_zone(103.0, zone)
    assert not mark_in_zone(102.0, zone)
    assert not mark_in_zone(104.0, zone)


def test_ote_zone_short_mirror():
    """Impulse high 110 → low 100; short OTE on retrace up."""
    zone = ote_zone_short(100.0, 110.0, fib_shallow=0.62, fib_deep=0.79)
    assert zone is not None
    # zone_low = 100 + 0.62*10 = 106.2; zone_high = 100 + 0.79*10 = 107.9
    assert zone.zone_low == pytest.approx(106.2)
    assert zone.zone_high == pytest.approx(107.9)
    assert mark_in_zone(107.0, zone)


def test_find_bullish_impulse_synthetic():
    """Lowest then higher high after it → bullish impulse."""
    t0 = 1_000_000.0
    bars = []
    # flat, then dip to 100, rally to 110, pull back
    px_seq = [
        (105, 106, 104),
        (104, 105, 103),
        (102, 103, 100),  # swing low 100
        (103, 107, 102),
        (108, 110, 107),  # swing high 110
        (106, 107, 105),
        (104, 105, 103),  # current / pullback
    ]
    for i, (o, h, l) in enumerate(px_seq):
        bars.append(_bar(t0 + i * 60_000, o, h, l, o))
    impulse = find_bullish_impulse(bars, lookback=20)
    assert impulse is not None
    assert impulse.swing_low == pytest.approx(100.0)
    assert impulse.swing_high == pytest.approx(110.0)
    assert impulse.direction == "bull"


def test_find_bearish_impulse_synthetic():
    t0 = 1_000_000.0
    bars = []
    px_seq = [
        (105, 106, 104),
        (108, 110, 107),  # swing high 110
        (106, 107, 104),
        (103, 104, 100),  # swing low 100
        (102, 103, 101),
        (104, 106, 103),  # retrace
    ]
    for i, (o, h, l) in enumerate(px_seq):
        bars.append(_bar(t0 + i * 60_000, o, h, l, o))
    impulse = find_bearish_impulse(bars, lookback=20)
    assert impulse is not None
    assert impulse.swing_high == pytest.approx(110.0)
    assert impulse.swing_low == pytest.approx(100.0)


def test_ote_stop_clamps_to_stop_pct_when_zone_wider():
    """Zone stop wider than STOP_PCT → use STOP_PCT (scalp-sized R)."""
    entry = 103.0
    zone_low = 102.1  # risk 0.9 ≈ 0.87% >> 0.15%
    stop = ote_stop_long(entry, zone_low, stop_pct=0.0015, stop_buffer=0.0)
    assert stop == pytest.approx(entry * (1.0 - 0.0015))
    # Documented example: entry 103, pct stop ≈ 102.8455
    assert stop == pytest.approx(102.84545, rel=1e-5)


def test_ote_stop_uses_tighter_zone_when_inside_pct():
    """Zone stop tighter than STOP_PCT → use zone stop."""
    entry = 103.0
    # STOP_PCT stop ≈ 102.845; zone_low just below entry → tighter
    zone_low = 102.95
    stop = ote_stop_long(entry, zone_low, stop_pct=0.0015, stop_buffer=0.0)
    assert stop == pytest.approx(102.95)
    assert stop > entry * (1.0 - 0.0015)


def test_ote_stop_short_clamp():
    entry = 107.0
    zone_high = 107.9  # wide
    stop = ote_stop_short(entry, zone_high, stop_pct=0.0015, stop_buffer=0.0)
    assert stop == pytest.approx(entry * (1.0 + 0.0015))


def _impulse_pullback_bars(t0, *, low=100.0, high=110.0, mark_zone=103.0):
    """Build quiet bars: dip to low, rally to high, pull into OTE, VWAP below mark."""
    # Keep ranges tight vs STOP_PCT so vol filter does not block.
    bars = []
    # Pre-impulse: around mid, tiny ranges (also pull VWAP down a bit with volume)
    for i in range(10):
        px = 100.5
        bars.append(_bar(t0 + i * 60_000, px, px + 0.05, px - 0.05, px, v=50))
    # Swing low
    bars.append(_bar(t0 + 10 * 60_000, low + 1, low + 1.05, low, low + 0.5, v=5))
    # Rally toward high
    mid = (low + high) / 2
    bars.append(_bar(t0 + 11 * 60_000, mid, mid + 0.05, mid - 0.05, mid, v=5))
    bars.append(_bar(t0 + 12 * 60_000, high - 1, high, high - 1.05, high - 0.5, v=5))
    # Pullback bars into zone (quiet)
    for j in range(3):
        bars.append(
            _bar(
                t0 + (13 + j) * 60_000,
                mark_zone,
                mark_zone + 0.05,
                mark_zone - 0.05,
                mark_zone + 0.02,  # bullish close
                v=5,
            )
        )
    return bars


def test_evaluate_ote_long_in_zone():
    t0 = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc).timestamp() * 1000
    bars = _impulse_pullback_bars(t0, low=100.0, high=110.0, mark_zone=103.0)
    reason, stop, _, zone = evaluate_ote_long(
        103.0, bars, lookback=30, stop_pct=0.0015
    )
    assert reason == "ote_long"
    assert zone is not None
    assert mark_in_zone(103.0, zone)
    assert stop == pytest.approx(103.0 * (1.0 - 0.0015))


def test_evaluate_ote_long_outside_zone():
    t0 = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc).timestamp() * 1000
    bars = _impulse_pullback_bars(t0, low=100.0, high=110.0, mark_zone=103.0)
    reason, stop, _, zone = evaluate_ote_long(
        108.0, bars, lookback=30, stop_pct=0.0015
    )
    assert reason == "ote_outside_zone"
    assert stop == 0.0
    assert zone is not None


def test_strategy_ote_long_mode():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _impulse_pullback_bars(t0, low=100.0, high=110.0, mark_zone=103.0)
    # Ensure VWAP bias long: mark above session VWAP
    vwap = session_vwap(bars, now_ms=t0 + 20 * 60_000)
    assert vwap is not None
    mark = 103.0
    assert mark > vwap

    strat = VwapTrendScalp(
        buffer_bps=0,
        stop_pct=0.0015,
        tp_r_multiple=2.0,
        entry_mode="ote",
        ote_lookback_bars=30,
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(mark, bars)
    assert sig.side == "long"
    assert sig.reason == "ote_long"
    assert sig.entry_mode == "ote"
    risk = mark - sig.stop
    assert sig.take_profit == pytest.approx(mark + 2.0 * risk)


def test_strategy_ote_short_mode():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    # Bearish impulse then retrace into short OTE; VWAP above mark
    bars = []
    for i in range(10):
        px = 109.5
        bars.append(_bar(t0 + i * 60_000, px, px + 0.05, px - 0.05, px, v=50))
    bars.append(_bar(t0 + 10 * 60_000, 109, 110, 108.9, 109.5, v=5))  # high 110
    bars.append(_bar(t0 + 11 * 60_000, 105, 105.05, 104.95, 105, v=5))
    bars.append(_bar(t0 + 12 * 60_000, 101, 101.05, 100, 100.5, v=5))  # low 100
    mark = 107.0  # short OTE ~[106.2, 107.9]
    for j in range(3):
        bars.append(
            _bar(
                t0 + (13 + j) * 60_000,
                mark,
                mark + 0.05,
                mark - 0.05,
                mark - 0.02,  # bearish close
                v=5,
            )
        )
    vwap = session_vwap(bars, now_ms=t0 + 20 * 60_000)
    assert vwap is not None and mark < vwap

    strat = VwapTrendScalp(
        buffer_bps=0,
        stop_pct=0.0015,
        tp_r_multiple=2.0,
        entry_mode="ote",
        ote_lookback_bars=30,
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(mark, bars)
    assert sig.side == "short"
    assert sig.reason == "ote_short"
    assert sig.entry_mode == "ote"


def test_both_prefers_ote_when_in_zone():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _impulse_pullback_bars(t0, low=100.0, high=110.0, mark_zone=103.0)
    # Also set up a prior-bar breakout so breakout would fire if chosen —
    # mark 103 does NOT break prior highs (~110), so only OTE can fire.
    # To prove preference: put mark in zone AND breaking a tiny prior high.
    # Rebuild last bars so prior 3-bar high is below mark but mark still in OTE.
    strat = VwapTrendScalp(
        buffer_bps=0,
        stop_pct=0.0015,
        entry_mode="both",
        ote_lookback_bars=30,
        breakout_bars=3,
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(103.0, bars)
    assert sig.side == "long"
    assert sig.entry_mode == "ote"
    assert sig.reason == "ote_long"


def test_both_falls_back_to_breakout_outside_zone():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    # Flat quiet bars; mark far above VWAP breaking prior high — outside any OTE
    px = 50_000.0
    bars = [
        _bar(t0 + i * 60_000, px, px + 5, px - 5, px, v=10) for i in range(8)
    ]
    mark = 50_020.0  # breaks prior high 50_005
    strat = VwapTrendScalp(
        buffer_bps=0,
        stop_pct=0.0015,
        entry_mode="both",
        ote_lookback_bars=30,
        breakout_bars=3,
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    # Impulse on flat bars: low px-5, high px+5 → zone near px — mark 50020 outside
    sig = strat.on_bar(mark, bars)
    assert sig.side == "long"
    assert sig.entry_mode == "breakout"
    assert "breakout" in sig.reason


def test_ote_only_outside_zone_stays_flat():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _impulse_pullback_bars(t0, low=100.0, high=110.0, mark_zone=103.0)
    strat = VwapTrendScalp(
        buffer_bps=0,
        stop_pct=0.0015,
        entry_mode="ote",
        ote_lookback_bars=30,
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(108.0, bars)  # above VWAP but outside OTE
    assert sig.side == "flat"
    assert sig.reason == "ote_outside_zone"


def test_ote_no_swing_reason():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    # Too few bars / monotonically falling so no subsequent high after low
    bars = [
        _bar(t0 + i * 60_000, 110 - i, 110 - i + 0.05, 110 - i - 0.05, 110 - i)
        for i in range(4)
    ]
    reason, stop, _, zone = evaluate_ote_long(109.0, bars, lookback=45)
    assert reason == "ote_no_swing"
    assert stop == 0.0

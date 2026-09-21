"""Unit tests for session VWAP and micro-breakout scalp signals (offline)."""

from datetime import datetime, timezone

import pytest

from hl_bot.strategy.vwap import VwapTrendScalp, prior_n_bar_high_low, session_vwap


def _bar(t_ms, o, h, l, c, v):
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_session_vwap_volume_weighted():
    # Two bars same session: typical 100 vol 1, typical 200 vol 3 → VWAP = 175
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [
        _bar(t0, 100, 100, 100, 100, 1),
        _bar(t0 + 60_000, 200, 200, 200, 200, 3),
    ]
    v = session_vwap(bars, reset_utc_hour=0, now_ms=t0 + 120_000)
    assert v == pytest.approx(175.0)


def test_session_vwap_equal_weight_without_volume():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [
        _bar(t0, 100, 100, 100, 100, 0),
        _bar(t0 + 60_000, 200, 200, 200, 200, 0),
    ]
    v = session_vwap(bars, reset_utc_hour=0, now_ms=t0 + 120_000)
    assert v == pytest.approx(150.0)


def _flat_session_bars(t0, n=6, px=50_000.0, high=None, low=None):
    """Build N bars around px; last bar is 'current' (excluded from prior window)."""
    h = high if high is not None else px + 10
    l = low if low is not None else px - 10
    return [
        _bar(t0 + i * 60_000, px, h, l, px, 10) for i in range(n)
    ]


def test_long_breakout_above_vwap_fixed_pct_stop():
    """Far above VWAP + break of prior high → stop ~0.15% below entry, NOT near VWAP."""
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    # Session VWAP near 50k; prior 3 bars high = 50_010; current mark breaks it
    bars = _flat_session_bars(t0, n=6, px=50_000.0, high=50_010, low=49_990)
    # Push mark far above VWAP (e.g. +2%) so VWAP-stop would be very wide
    mark = 51_000.0
    # Ensure prior high is broken: prior highs are 50_010
    prior_h, _ = prior_n_bar_high_low(bars, 3)
    assert prior_h == pytest.approx(50_010.0)
    assert mark > prior_h

    strat = VwapTrendScalp(
        buffer_bps=0,
        stop_pct=0.0015,
        tp_r_multiple=2.0,
        breakout_bars=3,
    )
    vwap = session_vwap(bars, reset_utc_hour=0, now_ms=t0 + 400_000)
    assert vwap is not None
    assert mark > vwap  # bias long

    sig = strat.on_bar(mark, bars)
    assert sig.side == "long"
    # Stop is fixed % from entry — NOT near VWAP
    expected_stop = mark * (1.0 - 0.0015)
    assert sig.stop == pytest.approx(expected_stop)
    assert abs(sig.stop - vwap) > abs(sig.stop - expected_stop) * 0.5  # clearly not VWAP stop
    # Specifically: stop is ~0.15% below entry, far from VWAP (~50k)
    assert sig.stop == pytest.approx(51_000.0 - 51_000.0 * 0.0015)
    assert sig.stop > 50_800  # nowhere near VWAP ~50k
    risk = mark - sig.stop
    assert sig.take_profit == pytest.approx(mark + 2.0 * risk)


def test_short_breakout_below_vwap_fixed_pct_stop():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _flat_session_bars(t0, n=6, px=50_000.0, high=50_010, low=49_990)
    mark = 49_000.0  # far below VWAP + breaks prior low 49_990
    prior_h, prior_l = prior_n_bar_high_low(bars, 3)
    assert prior_l == pytest.approx(49_990.0)
    assert mark < prior_l

    strat = VwapTrendScalp(buffer_bps=0, stop_pct=0.0015, tp_r_multiple=2.0, breakout_bars=3)
    sig = strat.on_bar(mark, bars)
    assert sig.side == "short"
    expected_stop = mark * (1.0 + 0.0015)
    assert sig.stop == pytest.approx(expected_stop)
    assert sig.stop < 49_200  # tight, not near VWAP ~50k
    risk = sig.stop - mark
    assert sig.take_profit == pytest.approx(mark - 2.0 * risk)


def test_no_long_without_breakout():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _flat_session_bars(t0, n=6, px=50_000.0, high=50_010, low=49_990)
    # Above VWAP but does NOT break prior high 50_010
    mark = 50_005.0
    strat = VwapTrendScalp(buffer_bps=0, stop_pct=0.0015, breakout_bars=3)
    sig = strat.on_bar(mark, bars)
    assert sig.side == "flat"
    assert "breakout" in sig.reason


def test_flat_inside_buffer():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [_bar(t0, 50_000, 50_000, 50_000, 50_000, 1)]
    # Need enough bars for breakout window — with only 1 bar, no breakout either
    strat = VwapTrendScalp(buffer_bps=10, stop_pct=0.0015, breakout_bars=3)
    vwap = session_vwap(bars, now_ms=t0 + 1000)
    sig = strat.on_bar(vwap, bars)  # exactly at VWAP
    assert sig.side == "flat"


def test_no_signal_when_already_in():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _flat_session_bars(t0, n=6, px=50_000.0)
    strat = VwapTrendScalp(buffer_bps=0, stop_pct=0.0015, breakout_bars=3)
    sig = strat.on_bar(51_000, bars, has_position=True)
    assert sig.side == "flat"
    assert sig.reason == "already_in"


def test_btc_86000_sl_tp_math():
    """Example SL/TP at BTC 86000 with default 0.15% stop and 2R TP."""
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    # Quiet bars (tight range) so vol filter does not block; VWAP below entry
    bars = [
        _bar(t0 + i * 60_000, 85_000, 85_020, 84_980, 85_000, 10) for i in range(5)
    ] + [_bar(t0 + 5 * 60_000, 86_000, 86_010, 85_990, 86_000, 10)]
    mark = 86_000.0
    strat = VwapTrendScalp(buffer_bps=0, stop_pct=0.0015, tp_r_multiple=2.0, breakout_bars=3)
    sig = strat.on_bar(mark, bars)
    assert sig.side == "long"
    stop = 86_000.0 * (1.0 - 0.0015)  # 85_871
    tp = 86_000.0 + (86_000.0 - stop) * 2.0  # 86_258
    assert sig.stop == pytest.approx(stop)
    assert sig.take_profit == pytest.approx(tp)
    assert sig.stop == pytest.approx(85_871.0)
    assert sig.take_profit == pytest.approx(86_258.0)


def test_skip_when_stop_exceeds_max():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _flat_session_bars(t0, n=6, px=50_000.0, high=50_010, low=49_990)
    mark = 51_000.0
    strat = VwapTrendScalp(
        buffer_bps=0, stop_pct=0.0015, breakout_bars=3, max_stop_pct=0.0010
    )
    sig = strat.on_bar(mark, bars)
    assert sig.side == "flat"
    assert "max" in sig.reason


def test_prior_n_bar_high_low():
    bars = [
        {"t": 1, "h": 10, "l": 1, "c": 5, "v": 1},
        {"t": 2, "h": 12, "l": 2, "c": 5, "v": 1},
        {"t": 3, "h": 11, "l": 3, "c": 5, "v": 1},
        {"t": 4, "h": 20, "l": 0, "c": 5, "v": 1},  # current — excluded
    ]
    h, l = prior_n_bar_high_low(bars, 3)
    assert h == 12
    assert l == 1

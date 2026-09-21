"""Unit tests for session VWAP and trend scalp signals (offline)."""

from datetime import datetime, timezone

import pytest

from hl_bot.strategy.vwap import VwapTrendScalp, session_vwap


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


def test_long_signal_above_vwap():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [_bar(t0 + i * 60_000, 50_000, 50_010, 49_990, 50_000, 10) for i in range(5)]
    strat = VwapTrendScalp(buffer_bps=5, stop_buffer_bps=2, tp_r_multiple=2.0)
    vwap = session_vwap(bars, reset_utc_hour=0, now_ms=t0 + 400_000)
    assert vwap is not None
    mark = vwap * 1.002  # clearly above buffer
    sig = strat.on_bar(mark, bars)
    assert sig.side == "long"
    assert sig.stop < vwap
    assert sig.take_profit > mark
    risk = mark - sig.stop
    assert sig.take_profit == pytest.approx(mark + 2.0 * risk)


def test_short_signal_below_vwap():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [_bar(t0 + i * 60_000, 50_000, 50_010, 49_990, 50_000, 10) for i in range(5)]
    strat = VwapTrendScalp(buffer_bps=5, stop_buffer_bps=2, tp_r_multiple=2.0)
    vwap = session_vwap(bars, reset_utc_hour=0, now_ms=t0 + 400_000)
    mark = vwap * 0.998
    sig = strat.on_bar(mark, bars)
    assert sig.side == "short"
    assert sig.stop > vwap
    risk = sig.stop - mark
    assert sig.take_profit == pytest.approx(mark - 2.0 * risk)


def test_flat_inside_buffer():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [_bar(t0, 50_000, 50_000, 50_000, 50_000, 1)]
    strat = VwapTrendScalp(buffer_bps=10)
    vwap = session_vwap(bars, now_ms=t0 + 1000)
    sig = strat.on_bar(vwap, bars)  # exactly at VWAP
    assert sig.side == "flat"


def test_no_signal_when_already_in():
    day = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = [_bar(t0, 50_000, 50_000, 50_000, 50_000, 1)]
    strat = VwapTrendScalp(buffer_bps=1)
    sig = strat.on_bar(51_000, bars, has_position=True)
    assert sig.side == "flat"

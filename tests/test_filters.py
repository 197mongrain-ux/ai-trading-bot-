"""Offline tests for accuracy filters (vol / session / HTF / cooldown)."""

from datetime import datetime, timezone

import pytest

from hl_bot.strategy.filters import (
    aggregate_bars,
    avg_range_pct,
    bar_range_pct,
    cooldown_active,
    htf_vwap_blocks,
    in_trade_hours,
    parse_interval_minutes,
    parse_trade_hours,
    volatility_block,
)
from hl_bot.strategy.vwap import VwapTrendScalp, session_vwap


def _bar(t_ms, o, h, l, c, v=10):
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v}


def _quiet_breakout_long(t0, n=10, px=50_000.0):
    """Quiet 1m bars + setup for long breakout above VWAP."""
    bars = [_bar(t0 + i * 60_000, px, px + 5, px - 5, px) for i in range(n)]
    return bars


# --- parse / session ---


def test_parse_trade_hours_default_window():
    assert parse_trade_hours("12-23") == (12, 23)


def test_parse_trade_hours_disabled():
    assert parse_trade_hours("") is None
    assert parse_trade_hours("0-24") is None
    assert parse_trade_hours(None) is None


def test_in_trade_hours_inclusive_start_exclusive_end():
    noon = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    almost_23 = datetime(2026, 9, 20, 22, 59, tzinfo=timezone.utc)
    at_23 = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
    morning = datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc)
    hours = (12, 23)
    assert in_trade_hours(noon, hours=hours)
    assert in_trade_hours(almost_23, hours=hours)
    assert not in_trade_hours(at_23, hours=hours)
    assert not in_trade_hours(morning, hours=hours)


def test_in_trade_hours_wrap_midnight():
    hours = (22, 6)
    assert in_trade_hours(datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc), hours=hours)
    assert in_trade_hours(datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc), hours=hours)
    assert not in_trade_hours(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc), hours=hours)


def test_session_filter_blocks_outside_window():
    day = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)  # 08:00 UTC
    t0 = day.timestamp() * 1000
    bars = _quiet_breakout_long(t0)
    mark = 51_000.0
    strat = VwapTrendScalp(
        trade_hours_utc="12-23",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(mark, bars, now=day)
    assert sig.side == "flat"
    assert sig.reason == "outside_session"


def test_session_filter_allows_inside_window():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _quiet_breakout_long(t0)
    mark = 51_000.0
    strat = VwapTrendScalp(
        trade_hours_utc="12-23",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(mark, bars, now=day)
    assert sig.side == "long"


# --- volatility ---


def test_bar_range_and_avg():
    bars = [
        _bar(1, 100, 101, 99, 100),   # 2%
        _bar(2, 100, 100.5, 99.5, 100),  # 1%
    ]
    assert bar_range_pct(bars[0]) == pytest.approx(0.02)
    assert avg_range_pct(bars, 2) == pytest.approx(0.015)


def test_vol_too_high_vs_stop():
    # stop 0.15%; avg range 0.2% >= 1.0 * 0.15%
    bars = [_bar(i, 100, 100.1, 99.9, 100) for i in range(5)]  # 0.2% each
    reason = volatility_block(
        bars, stop_pct=0.0015, max_range_vs_stop=1.0, lookback=5, max_bar_range_pct=None
    )
    assert reason == "vol_too_high"


def test_bar_too_wide():
    bars = [_bar(i, 100, 100.05, 99.95, 100) for i in range(4)]
    bars.append(_bar(5, 100, 100.5, 99.5, 100))  # 1% last bar
    reason = volatility_block(
        bars, stop_pct=0.0015, max_range_vs_stop=10.0, lookback=5, max_bar_range_pct=0.003
    )
    assert reason == "bar_too_wide"


def test_strategy_vol_too_high_reason():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    px = 50_000.0
    # Wide bars: 0.4% range >> 0.15% stop
    bars = [
        _bar(t0 + i * 60_000, px, px * 1.002, px * 0.998, px) for i in range(6)
    ]
    strat = VwapTrendScalp(
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_range_vs_stop=1.0,
        max_bar_range_pct=None,
        stop_pct=0.0015,
    )
    sig = strat.on_bar(51_000.0, bars, now=day)
    assert sig.side == "flat"
    assert sig.reason == "vol_too_high"


def test_strategy_bar_too_wide_reason():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    px = 50_000.0
    bars = [_bar(t0 + i * 60_000, px, px + 5, px - 5, px) for i in range(5)]
    # Last bar 0.5% wide
    bars.append(_bar(t0 + 5 * 60_000, px, px * 1.003, px * 0.997, px))
    strat = VwapTrendScalp(
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_range_vs_stop=99.0,
        max_bar_range_pct=0.003,
        stop_pct=0.0015,
    )
    sig = strat.on_bar(51_000.0, bars, now=day)
    assert sig.side == "flat"
    assert sig.reason == "bar_too_wide"


# --- HTF ---


def test_aggregate_bars_5m():
    t0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
    bars = [_bar(t0 + i * 60_000, 100 + i, 101 + i, 99 + i, 100 + i, v=1) for i in range(10)]
    htf = aggregate_bars(bars, 5)
    assert len(htf) == 2
    assert htf[0]["o"] == pytest.approx(100)
    assert htf[0]["c"] == pytest.approx(104)
    assert htf[0]["v"] == pytest.approx(5)
    assert parse_interval_minutes("5m") == 5
    assert parse_interval_minutes("1h") == 60


def test_htf_vwap_blocks_long_below():
    t0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
    # HTF session around 50k
    htf = [_bar(t0 + i * 300_000, 50_000, 50_010, 49_990, 50_000) for i in range(4)]
    assert htf_vwap_blocks("long", 49_000.0, htf, reset_utc_hour=0, now_ms=t0 + 1_200_000)
    assert not htf_vwap_blocks("long", 51_000.0, htf, reset_utc_hour=0, now_ms=t0 + 1_200_000)
    assert htf_vwap_blocks("short", 51_000.0, htf, reset_utc_hour=0, now_ms=t0 + 1_200_000)


def test_strategy_htf_vwap_block():
    """1m breakout long but mark below HTF VWAP → blocked."""
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    # 1m: quiet climb so local VWAP is low and mark breaks high
    bars_1m = [_bar(t0 + i * 60_000, 49_000, 49_005, 48_995, 49_000) for i in range(8)]
    mark = 49_100.0  # above 1m VWAP ~49k and breaks prior high 49_005
    # HTF bars centered much higher so HTF VWAP ~55k > mark
    htf = [_bar(t0 + i * 300_000, 55_000, 55_010, 54_990, 55_000) for i in range(6)]
    assert session_vwap(bars_1m, now_ms=t0 + 500_000) < mark
    assert session_vwap(htf, now_ms=t0 + 500_000) > mark

    strat = VwapTrendScalp(
        trade_hours_utc="0-24",
        htf_confirm=True,
        htf_interval="5m",
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
        breakout_bars=3,
    )
    sig = strat.on_bar(mark, bars_1m, now=day, htf_bars=htf)
    assert sig.side == "flat"
    assert sig.reason == "htf_vwap_block"


def test_strategy_htf_confirm_allows_aligned():
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    bars = _quiet_breakout_long(t0)
    mark = 51_000.0
    # HTF same neighborhood → VWAP ~50k, mark above → allow
    htf = aggregate_bars(bars, 5)
    strat = VwapTrendScalp(
        trade_hours_utc="0-24",
        htf_confirm=True,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
    )
    sig = strat.on_bar(mark, bars, now=day, htf_bars=htf)
    assert sig.side == "long"
    assert "breakout" in sig.reason


# --- cooldown ---


def test_cooldown_active():
    assert cooldown_active(1000.0, now_ts=1050.0, cooldown_sec=120)
    assert not cooldown_active(1000.0, now_ts=1200.0, cooldown_sec=120)
    assert not cooldown_active(None, now_ts=1000.0, cooldown_sec=120)
    assert not cooldown_active(1000.0, now_ts=1050.0, cooldown_sec=0)

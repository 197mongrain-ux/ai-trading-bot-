"""Unit tests for risk sizing and hard limits (offline)."""

from datetime import datetime, timezone

import pytest

from hl_bot.risk.manager import RiskManager


def test_size_from_dollar_risk_not_leverage():
    rm = RiskManager(starting_equity=5000.0, risk_per_trade=0.005, leverage=20)
    # $25 risk, $100 stop distance → 0.25 BTC
    d = rm.size_position(5000.0, entry_price=50_000.0, stop_price=49_900.0)
    assert d.allowed
    assert d.size == pytest.approx(0.25)
    assert d.dollar_risk == pytest.approx(25.0)


def test_tight_scalp_stop_sizing_with_20x():
    """0.15% stop at BTC 86k: size from $ risk, 20x only caps notional."""
    rm = RiskManager(starting_equity=5000.0, risk_per_trade=0.005, leverage=20)
    entry = 86_000.0
    stop = entry * (1.0 - 0.0015)  # 85_871
    d = rm.size_position(5000.0, entry, stop)
    assert d.allowed
    # dollar risk 25 / stop_dist 129 → size ≈ 0.1938 BTC
    stop_dist = entry - stop
    assert d.size == pytest.approx(25.0 / stop_dist)
    # notional ≈ 16_666 < equity*20=100_000 → no leverage cap trim
    assert d.size * entry < 5000.0 * 20


def test_leverage_caps_notional_not_risk_driver():
    rm = RiskManager(starting_equity=5000.0, risk_per_trade=0.005, leverage=1)
    # Without cap: size = 25/100 = 0.25 → notional 12_500 > equity*1=5000
    d = rm.size_position(5000.0, entry_price=50_000.0, stop_price=49_900.0)
    assert d.allowed
    assert d.size == pytest.approx(5000.0 / 50_000.0)
    assert d.size < 0.25


def test_reject_zero_stop_distance():
    rm = RiskManager(starting_equity=5000.0)
    d = rm.size_position(5000.0, 50_000.0, 50_000.0)
    assert not d.allowed


def test_max_daily_loss_halts():
    rm = RiskManager(starting_equity=5000.0, max_daily_loss_pct=0.03)
    # Lose 3%+
    equity = 5000.0 * 0.96  # -4%
    rm.update_equity(equity)
    assert rm.halted_daily_loss
    d = rm.allow_entry(equity, 50_000.0, 49_900.0)
    assert not d.allowed
    assert "daily loss" in d.reason.lower()


def test_max_drawdown_kills():
    rm = RiskManager(starting_equity=5000.0, max_drawdown_pct=0.08)
    rm.high_water_mark = 5000.0
    equity = 5000.0 * 0.91  # -9% from HWM
    flatten, reason = rm.should_flatten(equity)
    assert flatten
    assert rm.killed
    assert "kill" in reason.lower() or rm.killed


def test_max_trades_per_day_when_capped():
    rm = RiskManager(starting_equity=5000.0, max_trades_per_day=2)
    rm.trades_today = 2
    d = rm.allow_entry(5000.0, 50_000.0, 49_900.0)
    assert not d.allowed
    assert "trades/day" in d.reason.lower()


def test_max_trades_per_day_zero_is_unlimited():
    """MAX_TRADES_PER_DAY=0 disables the count cap."""
    rm = RiskManager(starting_equity=5000.0, max_trades_per_day=0)
    rm.trades_today = 500
    d = rm.allow_entry(5000.0, 50_000.0, 49_900.0)
    assert d.allowed


def test_consecutive_losses_pause():
    rm = RiskManager(starting_equity=5000.0, max_consecutive_losses=3)
    rm.record_trade_close(-10)
    rm.record_trade_close(-10)
    rm.record_trade_close(-10)
    assert rm.pause_entries
    d = rm.allow_entry(5000.0, 50_000.0, 49_900.0)
    assert not d.allowed
    assert "consecutive" in d.reason.lower()


def test_stacking_allowed_under_per_symbol_cap():
    """Existing opens on the same symbol are OK until max_positions_per_symbol."""
    rm = RiskManager(
        starting_equity=5000.0,
        max_open_positions=0,
        max_positions_per_symbol=3,
    )
    d = rm.allow_entry(
        5000.0, 50_000.0, 49_900.0, positions_for_symbol=1, open_position_count=1
    )
    assert d.allowed
    d2 = rm.allow_entry(
        5000.0, 50_000.0, 49_900.0, positions_for_symbol=2, open_position_count=2
    )
    assert d2.allowed
    d3 = rm.allow_entry(
        5000.0, 50_000.0, 49_900.0, positions_for_symbol=3, open_position_count=3
    )
    assert not d3.allowed
    assert "symbol" in d3.reason.lower()


def test_legacy_has_open_position_maps_to_one():
    """has_open_position=True with default max_positions_per_symbol=3 still allows."""
    rm = RiskManager(starting_equity=5000.0, max_positions_per_symbol=3)
    d = rm.allow_entry(5000.0, 50_000.0, 49_900.0, has_open_position=True)
    assert d.allowed
    # With max 1 per symbol, legacy flag blocks
    rm1 = RiskManager(starting_equity=5000.0, max_positions_per_symbol=1)
    d1 = rm1.allow_entry(5000.0, 50_000.0, 49_900.0, has_open_position=True)
    assert not d1.allowed


def test_max_open_positions_global_cap():
    rm = RiskManager(
        starting_equity=5000.0, max_open_positions=3, max_positions_per_symbol=5
    )
    # Simulate two opens already
    rm.record_trade_open()
    rm.record_trade_open()
    assert rm.open_positions == 2
    d = rm.allow_entry(
        5000.0, 50_000.0, 49_900.0, positions_for_symbol=0, open_position_count=2
    )
    assert d.allowed
    rm.record_trade_open()
    assert rm.open_positions == 3
    d2 = rm.allow_entry(
        5000.0, 100.0, 99.0, positions_for_symbol=0, open_position_count=3
    )
    assert not d2.allowed
    assert "max open positions" in d2.reason.lower()


def test_max_open_positions_zero_unlimited():
    rm = RiskManager(
        starting_equity=5000.0, max_open_positions=0, max_positions_per_symbol=0
    )
    d = rm.allow_entry(
        5000.0, 50_000.0, 49_900.0, positions_for_symbol=50, open_position_count=100
    )
    assert d.allowed


def test_record_open_close_counts():
    rm = RiskManager(starting_equity=5000.0, max_open_positions=3)
    rm.record_trade_open()
    rm.record_trade_open()
    assert rm.open_positions == 2
    rm.record_trade_close(10.0)
    assert rm.open_positions == 1
    assert rm.consecutive_losses == 0
    rm.record_trade_close(-5.0)
    assert rm.open_positions == 0
    assert rm.consecutive_losses == 1


def test_kill_switch_file_flag():
    rm = RiskManager(starting_equity=5000.0)
    d = rm.allow_entry(5000.0, 50_000.0, 49_900.0, kill_file_active=True)
    assert not d.allowed
    assert rm.killed


def test_stop_required():
    rm = RiskManager(starting_equity=5000.0)
    d = rm.allow_entry(5000.0, 50_000.0, 0.0)
    assert not d.allowed
    assert "stop" in d.reason.lower()


def test_day_roll_resets_halt(monkeypatch):
    rm = RiskManager(starting_equity=5000.0, max_daily_loss_pct=0.03)
    rm.halted_daily_loss = True
    rm.trades_today = 10
    rm._day_utc = "2020-01-01"
    rm.maybe_roll_day(4800.0, now=datetime(2020, 1, 2, tzinfo=timezone.utc))
    assert not rm.halted_daily_loss
    assert rm.trades_today == 0
    assert rm.day_start_equity == 4800.0

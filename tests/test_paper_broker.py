"""Unit tests for paper broker fills (offline)."""

import pytest

from hl_bot.exchange.paper_broker import PaperBroker


def test_open_and_close_long_pnl():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    fill = b.open_position("long", size=0.1, stop_price=49_000.0, take_profit=52_000.0)
    assert fill.action == "open"
    assert b.has_position
    b.set_mark(51_000.0)
    assert b.unrealized_pnl() == pytest.approx(100.0)
    close = b.close_position(reason="manual", price=51_000.0)
    assert close is not None
    assert close.pnl == pytest.approx(100.0)
    assert not b.has_position
    assert b.equity == pytest.approx(5100.0)


def test_short_stop_triggers():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    b.open_position("short", size=0.1, stop_price=50_500.0, take_profit=49_000.0)
    fills = b.check_stops(50_600.0)
    assert len(fills) == 1
    assert fills[0].reason == "stop"
    assert fills[0].pnl < 0


def test_long_take_profit():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    b.open_position("long", size=0.1, stop_price=49_500.0, take_profit=51_000.0)
    fills = b.check_stops(51_000.0)
    assert len(fills) == 1
    assert fills[0].reason == "take_profit"
    assert fills[0].pnl == pytest.approx(100.0)


def test_requires_stop():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    with pytest.raises(ValueError):
        b.open_position("long", size=0.1, stop_price=0, take_profit=52_000.0)


def test_stack_same_symbol_allowed():
    """Multiple positions on the same ticker when under max_positions_per_symbol."""
    b = PaperBroker(starting_equity=5000.0, max_positions_per_symbol=3)
    b.set_mark(50_000.0)
    f1 = b.open_position("long", size=0.1, stop_price=49_000.0, take_profit=52_000.0)
    f2 = b.open_position("long", size=0.05, stop_price=49_500.0, take_profit=51_000.0)
    assert f1.trade_id != f2.trade_id
    assert b.open_position_count == 2
    assert b.position_count_for("BTC") == 2
    assert b.has_position_for("BTC")


def test_rejects_when_at_per_symbol_cap():
    b = PaperBroker(starting_equity=5000.0, max_positions_per_symbol=1)
    b.set_mark(50_000.0)
    b.open_position("long", size=0.1, stop_price=49_000.0, take_profit=52_000.0)
    with pytest.raises(RuntimeError, match="Max 1"):
        b.open_position("short", size=0.1, stop_price=51_000.0, take_profit=48_000.0)


def test_two_btc_longs_independent_stops():
    """Open 2 BTC longs with different stops; one stop closes only one."""
    b = PaperBroker(starting_equity=5000.0, symbols=("BTC",), max_positions_per_symbol=3)
    b.set_mark(50_000.0)
    a = b.open_position(
        "long", size=0.1, stop_price=49_000.0, take_profit=52_000.0, symbol="BTC"
    )
    b.set_mark(50_100.0)
    c = b.open_position(
        "long", size=0.2, stop_price=49_800.0, take_profit=51_000.0, symbol="BTC"
    )
    assert a.trade_id != c.trade_id
    assert b.open_position_count == 2

    # Mark hits the tighter stop (49800) but not the wider (49000)
    fills = b.check_stops(49_700.0, symbol="BTC")
    assert len(fills) == 1
    assert fills[0].trade_id == c.trade_id
    assert fills[0].reason == "stop"
    assert b.open_position_count == 1
    assert b.get_position_by_id(a.trade_id) is not None
    assert b.get_position_by_id(c.trade_id) is None

    # Wider stop still open; hit it
    fills2 = b.check_stops(48_900.0, symbol="BTC")
    assert len(fills2) == 1
    assert fills2[0].trade_id == a.trade_id
    assert not b.has_position


def test_multi_symbol_independent_positions():
    b = PaperBroker(
        starting_equity=5000.0, symbols=("BTC", "SOL", "XRP"), max_positions_per_symbol=3
    )
    b.set_mark(50_000.0, symbol="BTC")
    b.set_mark(100.0, symbol="SOL")
    b.set_mark(0.50, symbol="XRP")

    b.open_position(
        "long", size=0.1, stop_price=49_000.0, take_profit=52_000.0, symbol="BTC"
    )
    b.open_position(
        "short", size=10.0, stop_price=105.0, take_profit=90.0, symbol="SOL"
    )
    assert b.open_position_count == 2
    assert b.has_position_for("BTC")
    assert b.has_position_for("SOL")
    assert not b.has_position_for("XRP")

    # Same-symbol stack OK under cap
    b.open_position(
        "long", size=0.05, stop_price=49_000.0, take_profit=52_000.0, symbol="BTC"
    )
    assert b.position_count_for("BTC") == 2
    assert b.open_position_count == 3

    # Equity = cash + sum MTM (two BTC longs 0.1+0.05 at +1000 → +150; SOL short +20)
    b.set_mark(51_000.0, symbol="BTC")
    b.set_mark(98.0, symbol="SOL")
    assert b.unrealized_pnl() == pytest.approx(170.0)
    assert b.equity_mark_to_market() == pytest.approx(5170.0)

    close_btc = b.close_position(symbol="BTC", price=51_000.0)
    assert close_btc is not None
    assert close_btc.pnl == pytest.approx(100.0)  # oldest BTC 0.1
    assert b.position_count_for("BTC") == 1
    assert b.has_position_for("SOL")


def test_multi_symbol_check_stops_per_symbol():
    b = PaperBroker(starting_equity=5000.0, symbols=("BTC", "SOL"))
    b.set_mark(50_000.0, symbol="BTC")
    b.set_mark(100.0, symbol="SOL")
    b.open_position(
        "long", size=0.1, stop_price=49_500.0, take_profit=51_000.0, symbol="BTC"
    )
    b.open_position(
        "long", size=5.0, stop_price=95.0, take_profit=110.0, symbol="SOL"
    )

    # SOL stop not hit
    assert b.check_stops(100.0, symbol="SOL") == []
    # BTC take profit
    fills = b.check_stops(51_000.0, symbol="BTC")
    assert len(fills) == 1
    assert fills[0].symbol == "BTC"
    assert fills[0].reason == "take_profit"
    assert b.has_position_for("SOL")
    assert not b.has_position_for("BTC")


def test_close_all():
    b = PaperBroker(starting_equity=5000.0, symbols=("BTC", "SOL"))
    b.set_mark(50_000.0, symbol="BTC")
    b.set_mark(100.0, symbol="SOL")
    b.open_position(
        "long", size=0.1, stop_price=49_000.0, take_profit=52_000.0, symbol="BTC"
    )
    b.open_position(
        "long", size=0.05, stop_price=49_000.0, take_profit=52_000.0, symbol="BTC"
    )
    b.open_position(
        "long", size=5.0, stop_price=90.0, take_profit=120.0, symbol="SOL"
    )
    fills = b.close_all(reason="kill", marks={"BTC": 50_000.0, "SOL": 100.0})
    assert len(fills) == 3
    assert not b.has_position


def test_close_by_trade_id():
    b = PaperBroker(starting_equity=5000.0, max_positions_per_symbol=5)
    b.set_mark(100.0)
    f1 = b.open_position("long", size=1.0, stop_price=90.0, take_profit=120.0)
    f2 = b.open_position("long", size=2.0, stop_price=95.0, take_profit=110.0)
    closed = b.close_position(trade_id=f2.trade_id, price=105.0, reason="manual")
    assert closed is not None
    assert closed.trade_id == f2.trade_id
    assert closed.pnl == pytest.approx(10.0)  # (105-100)*2
    assert b.get_position_by_id(f1.trade_id) is not None
    assert b.open_position_count == 1


def test_long_scale_out_at_1r_moves_stop_to_be_then_tp():
    """Long scales 50% at 1R, stop → BE, remainder hits original TP."""
    b = PaperBroker(
        starting_equity=5000.0,
        scale_out_enabled=True,
        scale_out_r=1.0,
        scale_out_pct=0.5,
        be_buffer_bps=2.0,
    )
    entry = 50_000.0
    stop = 49_500.0  # 500 risk → 1R = 50_500
    tp = 51_000.0  # 2R
    b.set_mark(entry)
    fill = b.open_position("long", size=0.2, stop_price=stop, take_profit=tp)
    tid = fill.trade_id

    # Below 1R — no scale
    assert b.check_scale_outs(50_400.0) == []
    pos = b.get_position_by_id(tid)
    assert pos is not None and not pos.scaled and pos.size == pytest.approx(0.2)

    # Hit 1R
    scaled = b.check_scale_outs(50_500.0)
    assert len(scaled) == 1
    assert scaled[0].action == "scale_out"
    assert scaled[0].reason == "sell_into_strength"
    assert scaled[0].size == pytest.approx(0.1)
    assert scaled[0].pnl == pytest.approx(50.0)  # (50500-50000)*0.1
    assert scaled[0].remaining_size == pytest.approx(0.1)

    pos = b.get_position_by_id(tid)
    assert pos is not None
    assert pos.scaled
    assert pos.size == pytest.approx(0.1)
    # BE with 2 bps below entry
    assert pos.stop_price == pytest.approx(entry * (1 - 0.0002))
    assert pos.take_profit == pytest.approx(tp)

    # Second scale should not fire
    assert b.check_scale_outs(50_600.0) == []

    # Remainder hits TP
    closes = b.check_stops(51_000.0)
    assert len(closes) == 1
    assert closes[0].reason == "take_profit"
    assert closes[0].size == pytest.approx(0.1)
    assert closes[0].pnl == pytest.approx(100.0)  # (51000-50000)*0.1
    assert not b.has_position
    assert b.realized_pnl == pytest.approx(150.0)


def test_short_scale_out_mirror():
    """Short covers into weakness (same scale logic)."""
    b = PaperBroker(
        starting_equity=5000.0,
        scale_out_enabled=True,
        scale_out_r=1.0,
        scale_out_pct=0.5,
        be_buffer_bps=2.0,
    )
    entry = 50_000.0
    stop = 50_500.0  # 500 risk → 1R = 49_500
    tp = 49_000.0
    b.set_mark(entry)
    fill = b.open_position("short", size=0.2, stop_price=stop, take_profit=tp)
    tid = fill.trade_id

    scaled = b.check_scale_outs(49_500.0)
    assert len(scaled) == 1
    assert scaled[0].action == "scale_out"
    assert scaled[0].size == pytest.approx(0.1)
    assert scaled[0].pnl == pytest.approx(50.0)

    pos = b.get_position_by_id(tid)
    assert pos is not None and pos.scaled
    assert pos.stop_price == pytest.approx(entry * (1 + 0.0002))
    assert pos.size == pytest.approx(0.1)

    closes = b.check_stops(49_000.0)
    assert len(closes) == 1
    assert closes[0].reason == "take_profit"
    assert closes[0].pnl == pytest.approx(100.0)


def test_scaled_runner_breakeven_stop():
    """After scale-out, pullback to BE stop labels breakeven_stop."""
    b = PaperBroker(
        starting_equity=5000.0,
        scale_out_r=1.0,
        scale_out_pct=0.5,
        be_buffer_bps=0.0,  # exact entry
    )
    b.set_mark(100.0)
    fill = b.open_position("long", size=2.0, stop_price=99.0, take_profit=102.0)
    tid = fill.trade_id
    # 1R = 101
    assert b.check_scale_outs(101.0)
    pos = b.get_position_by_id(tid)
    assert pos is not None
    assert pos.stop_price == pytest.approx(100.0)

    closes = b.check_stops(100.0)
    assert len(closes) == 1
    assert closes[0].reason == "breakeven_stop"
    assert closes[0].pnl == pytest.approx(0.0)


def test_scale_out_disabled():
    b = PaperBroker(starting_equity=5000.0, scale_out_enabled=False)
    b.set_mark(100.0)
    b.open_position("long", size=1.0, stop_price=99.0, take_profit=102.0)
    assert b.check_scale_outs(101.0) == []
    assert not b.position.scaled


def test_runner_tp_r_retargets_remainder():
    b = PaperBroker(
        starting_equity=5000.0,
        scale_out_r=1.0,
        scale_out_pct=0.5,
        be_buffer_bps=0.0,
        runner_tp_r=3.0,
    )
    b.set_mark(100.0)
    fill = b.open_position("long", size=1.0, stop_price=99.0, take_profit=102.0)
    b.check_scale_outs(101.0)
    pos = b.get_position_by_id(fill.trade_id)
    assert pos is not None
    assert pos.take_profit == pytest.approx(103.0)  # entry + 3R

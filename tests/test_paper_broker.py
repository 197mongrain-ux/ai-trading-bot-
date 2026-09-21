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

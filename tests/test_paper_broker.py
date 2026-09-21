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
    fill = b.check_stops(50_600.0)
    assert fill is not None
    assert fill.reason == "stop"
    assert fill.pnl < 0


def test_long_take_profit():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    b.open_position("long", size=0.1, stop_price=49_500.0, take_profit=51_000.0)
    fill = b.check_stops(51_000.0)
    assert fill is not None
    assert fill.reason == "take_profit"
    assert fill.pnl == pytest.approx(100.0)


def test_rejects_second_position_same_symbol():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    b.open_position("long", size=0.1, stop_price=49_000.0, take_profit=52_000.0)
    with pytest.raises(RuntimeError):
        b.open_position("short", size=0.1, stop_price=51_000.0, take_profit=48_000.0)


def test_requires_stop():
    b = PaperBroker(starting_equity=5000.0)
    b.set_mark(50_000.0)
    with pytest.raises(ValueError):
        b.open_position("long", size=0.1, stop_price=0, take_profit=52_000.0)


def test_multi_symbol_independent_positions():
    b = PaperBroker(starting_equity=5000.0, symbols=("BTC", "SOL", "XRP"))
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

    # Same-symbol reject
    with pytest.raises(RuntimeError):
        b.open_position(
            "long", size=0.05, stop_price=49_000.0, take_profit=52_000.0, symbol="BTC"
        )

    # Equity = cash + sum MTM
    b.set_mark(51_000.0, symbol="BTC")  # +100 on BTC long 0.1
    b.set_mark(98.0, symbol="SOL")  # +20 on SOL short 10*(100-98)
    assert b.unrealized_pnl() == pytest.approx(120.0)
    assert b.equity_mark_to_market() == pytest.approx(5120.0)

    close_btc = b.close_position(symbol="BTC", price=51_000.0)
    assert close_btc is not None
    assert close_btc.pnl == pytest.approx(100.0)
    assert b.open_position_count == 1
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
    assert b.check_stops(100.0, symbol="SOL") is None
    # BTC take profit
    fill = b.check_stops(51_000.0, symbol="BTC")
    assert fill is not None
    assert fill.symbol == "BTC"
    assert fill.reason == "take_profit"
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
        "long", size=5.0, stop_price=90.0, take_profit=120.0, symbol="SOL"
    )
    fills = b.close_all(reason="kill", marks={"BTC": 50_000.0, "SOL": 100.0})
    assert len(fills) == 2
    assert not b.has_position

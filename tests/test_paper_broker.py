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


def test_rejects_second_position():
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

"""Config / mode gate tests."""

import pytest

from hl_bot.config import Settings, _parse_symbols, load_settings


def test_paper_by_default():
    s = Settings()
    assert s.is_paper
    assert not s.is_live


def test_live_requires_gate():
    s = Settings(trading_mode="live", i_understand_live_trading=False)
    assert not s.is_live
    with pytest.raises(ValueError, match="I_UNDERSTAND_LIVE_TRADING"):
        s.validate()


def test_live_with_gate():
    s = Settings(
        trading_mode="live",
        i_understand_live_trading=True,
        private_key="0x" + "ab" * 32,
    )
    assert s.is_live
    s.validate()


def test_risk_per_trade_range():
    with pytest.raises(ValueError):
        Settings(risk_per_trade=0.01).validate()


def test_default_symbols():
    s = Settings()
    assert s.symbols == ("BTC", "SOL", "XRP")
    assert s.symbol == "BTC"
    assert s.max_open_positions == 3


def test_parse_symbols_from_env(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "btc, sol ,xrp")
    monkeypatch.delenv("SYMBOL", raising=False)
    assert _parse_symbols() == ("BTC", "SOL", "XRP")


def test_parse_symbol_fallback(monkeypatch):
    monkeypatch.delenv("SYMBOLS", raising=False)
    monkeypatch.setenv("SYMBOL", "eth")
    assert _parse_symbols() == ("ETH",)


def test_parse_symbols_default_when_unset(monkeypatch):
    monkeypatch.delenv("SYMBOLS", raising=False)
    monkeypatch.delenv("SYMBOL", raising=False)
    assert _parse_symbols() == ("BTC", "SOL", "XRP")


def test_load_settings_symbols(monkeypatch, tmp_path):
    monkeypatch.setenv("SYMBOLS", "BTC,SOL")
    monkeypatch.delenv("SYMBOL", raising=False)
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    # Avoid leftover env noise for risk range
    monkeypatch.setenv("RISK_PER_TRADE", "0.005")
    s = load_settings()
    assert s.symbols == ("BTC", "SOL")
    assert s.symbol == "BTC"
    assert s.max_open_positions == 2  # defaults to len(symbols)

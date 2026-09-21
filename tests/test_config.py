"""Config / mode gate tests."""

import pytest

from hl_bot.config import Settings


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

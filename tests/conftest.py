"""Keep the laptop ``.env`` from changing test defaults.

``load_dotenv()`` used to run on import. A live ``.env`` with
``RISK_PER_TRADE=0.02`` and a short ``SYMBOLS`` list then failed the
scalp risk check and shrank the hunt. Tests opt out of that file and
start from a clean config environment. A test that needs a value sets
it itself.
"""

from __future__ import annotations

import os

os.environ["HL_BOT_SKIP_DOTENV"] = "1"

import pytest

# Every key ``load_settings`` reads. Clearing them is what makes a
# sourced ``.env`` in the shell as harmless as an absent one.
_CONFIG_ENV = (
    "TRADING_MODE",
    "I_UNDERSTAND_LIVE_TRADING",
    "KILL_SWITCH",
    "HL_NETWORK",
    "HL_API_URL",
    "HL_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS",
    "SYMBOLS",
    "SYMBOL",
    "MAX_OPEN_POSITIONS",
    "MAX_POSITIONS_PER_SYMBOL",
    "STARTING_EQUITY",
    "RISK_PER_TRADE",
    "MAX_DAILY_LOSS_PCT",
    "MAX_DRAWDOWN_PCT",
    "MAX_TRADES_PER_DAY",
    "MAX_CONSECUTIVE_LOSSES",
    "LEVERAGE",
    "TP_R_MULTIPLE",
    "VWAP_BUFFER_BPS",
    "VWAP_RESET_UTC_HOUR",
    "LOOP_INTERVAL_SEC",
    "STOP_PCT",
    "BREAKOUT_BARS",
    "MIN_STOP_PCT",
    "MAX_STOP_PCT",
    "STOP_BUFFER_BPS",
    "MAX_RANGE_VS_STOP",
    "VOL_LOOKBACK_BARS",
    "MAX_BAR_RANGE_PCT",
    "TRADE_HOURS_UTC",
    "HTF_CONFIRM",
    "HTF_INTERVAL",
    "ENTRY_COOLDOWN_SEC",
    "RESET_DAILY_RISK",
    "ENTRY_MODE",
    "MODEL_B_TP_R",
    "MODEL_B_MIN_PRINTS",
    "MODEL_B_ALO_TIMEOUT_SEC",
    "MODEL_B_DELTA_FLAT_USDC",
    "MODEL_B_DELTA_FLAT_EPS",
    "MODEL_B_CLOSER_SCORE_GUARD",
    "MODEL_B_CLOSE_MARGIN_RESERVE",
    "OTE_LOOKBACK_BARS",
    "OTE_FIB_SHALLOW",
    "OTE_FIB_DEEP",
    "OTE_STOP_BUFFER_BPS",
    "OTE_REQUIRE_CLOSE",
    "OTE_USE_HTF_SWINGS",
    "SCALE_OUT_ENABLED",
    "SCALE_OUT_R",
    "SCALE_OUT_PCT",
    "BE_BUFFER_BPS",
    "RUNNER_TP_R",
    "JOURNAL_PATH",
    "KILLSWITCH_FILE",
)


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HL_BOT_SKIP_DOTENV", "1")
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)

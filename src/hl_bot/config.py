"""Environment-based configuration for the trading bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None and raw.strip() else default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None and raw.strip() else default


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings loaded from environment."""

    trading_mode: str = "paper"
    i_understand_live_trading: bool = False
    kill_switch: bool = False

    network: str = "mainnet"
    api_url: str = "https://api.hyperliquid.xyz"
    private_key: str = ""
    account_address: str = ""

    symbol: str = "BTC"
    starting_equity: float = 5000.0
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.08
    max_trades_per_day: int = 20
    max_consecutive_losses: int = 3
    leverage: int = 5
    tp_r_multiple: float = 2.0
    vwap_buffer_bps: float = 5.0
    vwap_reset_utc_hour: int = 0
    loop_interval_sec: float = 5.0
    stop_buffer_bps: float = 2.0

    journal_path: str = "logs/trades.jsonl"
    killswitch_file: str = ".killswitch"

    @property
    def is_live(self) -> bool:
        return (
            self.trading_mode.strip().lower() == "live"
            and self.i_understand_live_trading
        )

    @property
    def is_paper(self) -> bool:
        return not self.is_live

    def validate(self) -> None:
        if self.trading_mode.strip().lower() == "live" and not self.i_understand_live_trading:
            raise ValueError(
                "TRADING_MODE=live requires I_UNDERSTAND_LIVE_TRADING=true. "
                "Refusing to start LIVE without the explicit gate."
            )
        if not (0.0025 <= self.risk_per_trade <= 0.005):
            raise ValueError(
                f"RISK_PER_TRADE={self.risk_per_trade} outside documented range "
                "0.0025–0.005 (0.25%–0.5%)."
            )
        if not (1.0 <= self.tp_r_multiple <= 3.0):
            raise ValueError(f"TP_R_MULTIPLE={self.tp_r_multiple} must be in [1, 3].")
        if self.leverage < 1 or self.leverage > 20:
            raise ValueError(f"LEVERAGE={self.leverage} must be in [1, 20].")
        if self.is_live and not self.private_key:
            raise ValueError("LIVE mode requires HL_PRIVATE_KEY.")


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings from environment (optionally from a specific .env path)."""
    if env_file is not None:
        load_dotenv(env_file, override=True)

    network = os.getenv("HL_NETWORK", "mainnet").strip().lower()
    default_url = (
        "https://api.hyperliquid-testnet.xyz"
        if network == "testnet"
        else "https://api.hyperliquid.xyz"
    )
    api_url = os.getenv("HL_API_URL", default_url).strip() or default_url

    settings = Settings(
        trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
        i_understand_live_trading=_bool("I_UNDERSTAND_LIVE_TRADING", False),
        kill_switch=_bool("KILL_SWITCH", False),
        network=network,
        api_url=api_url,
        private_key=os.getenv("HL_PRIVATE_KEY", "").strip(),
        account_address=os.getenv("HL_ACCOUNT_ADDRESS", "").strip(),
        symbol=os.getenv("SYMBOL", "BTC").strip().upper(),
        starting_equity=_float("STARTING_EQUITY", 5000.0),
        risk_per_trade=_float("RISK_PER_TRADE", 0.005),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 0.03),
        max_drawdown_pct=_float("MAX_DRAWDOWN_PCT", 0.08),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 20),
        max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 3),
        leverage=_int("LEVERAGE", 5),
        tp_r_multiple=_float("TP_R_MULTIPLE", 2.0),
        vwap_buffer_bps=_float("VWAP_BUFFER_BPS", 5.0),
        vwap_reset_utc_hour=_int("VWAP_RESET_UTC_HOUR", 0),
        loop_interval_sec=_float("LOOP_INTERVAL_SEC", 5.0),
        stop_buffer_bps=_float("STOP_BUFFER_BPS", 2.0),
        journal_path=os.getenv("JOURNAL_PATH", "logs/trades.jsonl"),
        killswitch_file=os.getenv("KILLSWITCH_FILE", ".killswitch"),
    )
    settings.validate()
    return settings

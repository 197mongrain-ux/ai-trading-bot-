"""Environment-based configuration for the trading bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from hl_bot.strategy.filters import parse_trade_hours

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


def _optional_float(name: str, default: float | None) -> float | None:
    """Parse float; empty string → None (disabled). Missing → default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw.strip():
        return None
    return float(raw)


def _parse_symbols() -> tuple[str, ...]:
    """Parse SYMBOLS (comma-separated) with SYMBOL fallback; default BTC,SOL,XRP."""
    symbols_raw = os.getenv("SYMBOLS")
    if symbols_raw is not None and symbols_raw.strip():
        parsed = tuple(s.strip().upper() for s in symbols_raw.split(",") if s.strip())
        if parsed:
            return parsed
    symbol_raw = os.getenv("SYMBOL")
    if symbol_raw is not None and symbol_raw.strip():
        return (symbol_raw.strip().upper(),)
    return ("BTC", "SOL", "XRP")


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

    # Multi-symbol: Hyperliquid perp coin names (bare: BTC, SOL, XRP)
    symbols: tuple[str, ...] = ("BTC", "SOL", "XRP")
    symbol: str = "BTC"  # first of symbols; single-symbol backward compat
    # 0 = unlimited global open positions (still bound by per-symbol + risk)
    max_open_positions: int = 0
    # Max stacked opens on the same ticker; 0 = unlimited per symbol
    max_positions_per_symbol: int = 3

    starting_equity: float = 5000.0
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.08
    # 0 = unlimited daily trade count (strategy + other risk rules still apply)
    max_trades_per_day: int = 0
    max_consecutive_losses: int = 3
    leverage: int = 20
    tp_r_multiple: float = 2.0
    # Bias-only: distance above/below VWAP required for long/short direction
    vwap_buffer_bps: float = 0.0
    vwap_reset_utc_hour: int = 0
    loop_interval_sec: float = 5.0
    # Fixed percent stop from entry (NOT at VWAP). 0.0015 = 0.15%
    stop_pct: float = 0.0015
    breakout_bars: int = 3
    min_stop_pct: float = 0.0
    max_stop_pct: float | None = None
    # Deprecated: previously used for stop-at-VWAP; ignored by strategy
    stop_buffer_bps: float = 0.0

    # --- Accuracy filters ---
    # Skip if avg (high-low)/close over lookback >= this × STOP_PCT
    max_range_vs_stop: float = 1.0
    vol_lookback_bars: int = 5
    # Skip if last bar range > this (None / empty env = disabled). Default 0.3%.
    max_bar_range_pct: float | None = 0.003
    # UTC hours START-END (start inclusive, end exclusive). Empty or 0-24 = off.
    trade_hours_utc: str = "12-23"
    htf_confirm: bool = True
    htf_interval: str = "5m"
    # Seconds to block new entries on a symbol after a stop-out
    entry_cooldown_sec: float = 120.0
    # Journal risk_reset on start and document that restart clears daily halt
    reset_daily_risk: bool = False

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
        if not self.symbols:
            raise ValueError("At least one symbol required (SYMBOLS or SYMBOL).")
        if self.max_open_positions < 0:
            raise ValueError(
                f"MAX_OPEN_POSITIONS={self.max_open_positions} must be >= 0 "
                "(0 = unlimited)."
            )
        if self.max_positions_per_symbol < 0:
            raise ValueError(
                f"MAX_POSITIONS_PER_SYMBOL={self.max_positions_per_symbol} must be >= 0 "
                "(0 = unlimited)."
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
        if self.stop_pct <= 0:
            raise ValueError(f"STOP_PCT={self.stop_pct} must be > 0.")
        if self.breakout_bars < 1:
            raise ValueError(f"BREAKOUT_BARS={self.breakout_bars} must be >= 1.")
        if self.max_trades_per_day < 0:
            raise ValueError(
                f"MAX_TRADES_PER_DAY={self.max_trades_per_day} must be >= 0 "
                "(0 = unlimited)."
            )
        if self.max_range_vs_stop < 0:
            raise ValueError(
                f"MAX_RANGE_VS_STOP={self.max_range_vs_stop} must be >= 0."
            )
        if self.vol_lookback_bars < 1:
            raise ValueError(
                f"VOL_LOOKBACK_BARS={self.vol_lookback_bars} must be >= 1."
            )
        if self.max_bar_range_pct is not None and self.max_bar_range_pct < 0:
            raise ValueError(
                f"MAX_BAR_RANGE_PCT={self.max_bar_range_pct} must be >= 0 "
                "(empty env disables)."
            )
        # Validate trade hours parse (raises on bad format)
        parse_trade_hours(self.trade_hours_utc)
        if self.entry_cooldown_sec < 0:
            raise ValueError(
                f"ENTRY_COOLDOWN_SEC={self.entry_cooldown_sec} must be >= 0."
            )
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

    symbols = _parse_symbols()
    # MAX_OPEN_POSITIONS: 0 = unlimited global (default). Positive = hard cap.
    max_open = _int("MAX_OPEN_POSITIONS", 0)
    # MAX_POSITIONS_PER_SYMBOL: default 3; 0 = unlimited stacking per ticker
    max_per_sym = _int("MAX_POSITIONS_PER_SYMBOL", 3)

    max_stop_raw = os.getenv("MAX_STOP_PCT")
    max_stop: float | None
    if max_stop_raw is not None and max_stop_raw.strip():
        max_stop = float(max_stop_raw)
    else:
        max_stop = None

    # TRADE_HOURS_UTC: default liquid crypto hours; empty / 0-24 disables
    trade_hours_raw = os.getenv("TRADE_HOURS_UTC")
    if trade_hours_raw is None:
        trade_hours = "12-23"
    else:
        trade_hours = trade_hours_raw.strip()

    settings = Settings(
        trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
        i_understand_live_trading=_bool("I_UNDERSTAND_LIVE_TRADING", False),
        kill_switch=_bool("KILL_SWITCH", False),
        network=network,
        api_url=api_url,
        private_key=os.getenv("HL_PRIVATE_KEY", "").strip(),
        account_address=os.getenv("HL_ACCOUNT_ADDRESS", "").strip(),
        symbols=symbols,
        symbol=symbols[0],
        max_open_positions=max_open,
        max_positions_per_symbol=max_per_sym,
        starting_equity=_float("STARTING_EQUITY", 5000.0),
        risk_per_trade=_float("RISK_PER_TRADE", 0.005),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 0.03),
        max_drawdown_pct=_float("MAX_DRAWDOWN_PCT", 0.08),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 0),
        max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 3),
        leverage=_int("LEVERAGE", 20),
        tp_r_multiple=_float("TP_R_MULTIPLE", 2.0),
        vwap_buffer_bps=_float("VWAP_BUFFER_BPS", 0.0),
        vwap_reset_utc_hour=_int("VWAP_RESET_UTC_HOUR", 0),
        loop_interval_sec=_float("LOOP_INTERVAL_SEC", 5.0),
        stop_pct=_float("STOP_PCT", 0.0015),
        breakout_bars=_int("BREAKOUT_BARS", 3),
        min_stop_pct=_float("MIN_STOP_PCT", 0.0),
        max_stop_pct=max_stop,
        stop_buffer_bps=_float("STOP_BUFFER_BPS", 0.0),
        max_range_vs_stop=_float("MAX_RANGE_VS_STOP", 1.0),
        vol_lookback_bars=_int("VOL_LOOKBACK_BARS", 5),
        max_bar_range_pct=_optional_float("MAX_BAR_RANGE_PCT", 0.003),
        trade_hours_utc=trade_hours,
        htf_confirm=_bool("HTF_CONFIRM", True),
        htf_interval=os.getenv("HTF_INTERVAL", "5m").strip() or "5m",
        entry_cooldown_sec=_float("ENTRY_COOLDOWN_SEC", 120.0),
        reset_daily_risk=_bool("RESET_DAILY_RISK", False),
        journal_path=os.getenv("JOURNAL_PATH", "logs/trades.jsonl"),
        killswitch_file=os.getenv("KILLSWITCH_FILE", ".killswitch"),
    )
    settings.validate()
    return settings

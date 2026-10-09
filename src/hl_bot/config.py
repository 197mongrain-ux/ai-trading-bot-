"""Environment-based configuration for the trading bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from hl_bot.strategy.filters import parse_trade_hours
from hl_bot.strategy.model_b.risk import CLOSE_MARGIN_RESERVE
from hl_bot.strategy.model_b.tape import (
    DELTA_FLAT_EPS,
    DELTA_FLAT_USDC,
    MIN_PRINTS,
    density_min_prints,
)
from hl_bot.strategy.model_b.tp_select import parse_tri_mode
from hl_bot.strategy.model_b.universe import canon_coin


def _dotenv_skipped() -> bool:
    """Tests set this so a laptop ``.env`` cannot change defaults."""
    return os.getenv("HL_BOT_SKIP_DOTENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _macro_side_mode() -> str:
    """MODEL_B_MACRO_SIDE_ONLY: 1/on (only the macro side), shadow (log), 0/off."""
    raw = (os.getenv("MODEL_B_MACRO_SIDE_ONLY") or "shadow").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return "on"
    if raw in ("0", "false", "no", "off", ""):
        return "off"
    return raw  # "shadow" or invalid (validated below)


def _tp_mode(name: str, default: str) -> str:
    """1/on, 0/off, or shadow. Empty keeps ``default``."""
    return parse_tri_mode(os.getenv(name), default)


def _structure_mode() -> str:
    """MODEL_B_STRUCTURE_FILTER: 1/on (gate), shadow (log only), 0/off."""
    raw = (os.getenv("MODEL_B_STRUCTURE_FILTER") or "").strip().lower()
    if not raw:
        return "on"
    if raw in {"shadow", "log", "log_only", "logonly"}:
        return "shadow"
    return "on" if raw in {"1", "true", "yes", "on"} else "off"


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


def _optional_int_unset(name: str) -> int | None:
    """Empty or ``0`` is unset. A present integer is returned as-is."""
    raw = os.getenv(name)
    if raw is None or not raw.strip() or raw.strip() == "0":
        return None
    return int(raw)


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
        parsed = tuple(canon_coin(s) for s in symbols_raw.split(",") if canon_coin(s))
        if parsed:
            return parsed
    symbol_raw = os.getenv("SYMBOL")
    if symbol_raw is not None and symbol_raw.strip():
        return (canon_coin(symbol_raw),)
    return ("BTC", "SOL", "XRP")


def check_network_url(network: str, api_url: str) -> None:
    """``HL_NETWORK`` (websocket prints / fills) and ``HL_API_URL`` (orders,
    balance, guard) must name the same chain.

    A mismatch sizes and prices mainnet orders off the testnet tape (or the
    reverse) and puts stops on the wrong side of the real market.
    """
    net = (network or "").strip().lower()
    url = (api_url or "").strip().lower()
    url_testnet = "testnet" in url
    if net == "testnet" and not url_testnet:
        raise ValueError(
            f"HL_NETWORK=testnet but HL_API_URL={api_url} is not a testnet URL. "
            "Fix both together (testnet: https://api.hyperliquid-testnet.xyz)."
        )
    if net != "testnet" and url_testnet:
        raise ValueError(
            f"HL_NETWORK={network or 'mainnet'} but HL_API_URL={api_url} is a testnet URL. "
            "Fix both together (mainnet: https://api.hyperliquid.xyz)."
        )


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

    # --- Entry mode / OTE add-on ---
    # breakout | ote | both | model_b
    # model_b is a separate hunt (sweep/reclaim Alo). It does not wrap
    # breakout/OTE and it does not use the score as a gate.
    entry_mode: str = "both"
    # Model B TP multiple when no liquidity sits in front of the entry.
    # Default 1.5, must stay in [1, 2]. A swing or pool is the target instead.
    model_b_tp_r: float = 1.5
    # 90s print floor. Mainnet stays 30. Testnet auto-scales by tape density
    # unless MODEL_B_MIN_PRINTS is set. See tape.density_min_prints.
    model_b_min_prints: int = 30
    # 0 = rest the maker Alo until the thesis is stale. No default 20s cancel.
    model_b_alo_timeout_sec: float = 0.0
    # USDC notional. Coin size is max(this / mid, the coin floor below).
    # 0 drops this term.
    model_b_delta_flat_usdc: float = DELTA_FLAT_USDC
    # Coin-size floor (buy sz − sell sz). The larger of this and the USDC
    # term is the band. 0 drops this term. Both at 0 is the strict sign check.
    model_b_delta_flat_eps: float = DELTA_FLAT_EPS
    # Closer-ticker cancel compares arm scores. On (default): do not cancel
    # a resting Alo whose score is strictly higher than the closer coin.
    # Equal scores still use the closer-bps swap. Off restores the old cancel.
    # Not an entry gate and not a size change.
    model_b_closer_score_guard: bool = True
    # Fraction of free-margin capacity held for a resting preferred Alo
    # (highest score, then closer limit in bps). An unarmed close setup
    # does not hold it. 0 turns the reserve off. Not hard-coded to BTC.
    model_b_close_margin_reserve: float = CLOSE_MARGIN_RESERVE
    # Size brake: per-ticket notional never exceeds this many times the
    # spot USDC sizing balance (the old 20x brake). The exchange leverage
    # set before the Alo is still the coin max (PR #8 margin). It does not
    # move the stop or the target. Unset / empty / 0 keeps 20.
    model_b_max_leverage: int = 20
    # Sizing floor on stop distance, in bps of the entry. A liquidity stop
    # closer than this stays at its price; only the size is computed from
    # this distance, so a 0.05% stop cannot become a 40x ticket.
    model_b_min_stop_bps: float = 15.0
    # Entry filters (Oct 7 22:56 BTC review). STRUCTURE: no long into
    # 15m/1h LH+LL, no short into HH+HL. COUNTER_FLOW: most adverse rolling
    # 90s delta over the last N seconds, band = max(USDC / mid, coin eps).
    model_b_structure_filter: bool = True
    # on = gate, shadow = log MODEL_B SHADOW would_block=1 only, off = skip.
    model_b_structure_mode: str = "on"
    model_b_counter_flow: bool = True
    model_b_counter_flow_sec: float = 300.0
    model_b_counter_flow_usdc: float = 1_000_000.0
    model_b_counter_flow_eps: float = 0.0
    model_b_counter_flow_flip: float = 1.0
    model_b_counter_flow_hold_sec: float = 30.0
    # SHALLOW_SWEEP: bps past the swing (number or ``BTC:5,default:0.3``),
    # and optionally require the swept level to be a 15m swing / pool.
    model_b_min_sweep_bps: str = "0.3"
    model_b_sweep_require_htf: bool = False
    # Two-sided hunt: every cycle evaluates the long AND the short setup on
    # each coin (same gates / SL / TP / sizing); the nearest draw pool is
    # only that side's TP target, not the bias. 0 = old pool-only bias.
    model_b_two_sided: bool = True
    # TP: when the nearest liquidity target is under this R (same fee/R
    # floor as the 1R pick), walk out to the next real pool at >= it. No
    # such pool: keep the nearest and log MODEL_B TP_UNDER_1_5R. 0 = off.
    model_b_tp_min_pool_r: float = 1.5
    # A walked-to pool past this R is "too far" (no existing pool cap: the
    # liquidity target was always kept past 2R). Too far, or no pool at the
    # min R, and not with the 15m/1h trend -> skip (when the knob below is 1).
    model_b_tp_max_pool_r: float = 3.0
    model_b_tp_far_skip_countertrend: bool = True
    # Spent 1m swings are not TP targets. shadow logs the untaken pick and
    # keeps today's. 0 restores the PR #14 level list.
    model_b_tp_untaken_only: str = "on"
    # At the fill, if the planned TP was traded through while the Alo
    # rested, re-pick. The stop and the size stay. 0 keeps the arm TP.
    model_b_tp_refresh_on_fill: str = "on"
    # Partial TP. shadow logs TP1/TP2/trail and changes no order. 1 places
    # both reduce-only TPs and trails after TP1. 0 is one full-size TP.
    model_b_tp_runner: str = "shadow"
    model_b_tp_runner_frac: float = 0.5
    model_b_tp_runner_max_r: float = 5.0
    # Macro side filter: ADX(14) +DI/-DI on 1h and 4h. macro up -> longs only,
    # down -> shorts only, range -> MODEL_B_MACRO_RANGE_POLICY (both | none).
    # on | shadow (log "would_block", never block) | off. Default shadow.
    model_b_macro_side_only: str = "shadow"
    model_b_macro_range_policy: str = "both"
    model_b_macro_adx_min: float = 20.0
    model_b_macro_mode: str = "4h_lead"
    # Protection guard: cadence (s), loss kill at N x planned risk (2% of
    # spot when the plan is unknown), oversize cut above N x ticket size.
    model_b_guard_sec: float = 3.0
    model_b_loss_kill_r: float = 1.0
    # Hard per-position loss cap (fraction of account). Max 0.02.
    model_b_max_loss_pct: float = 0.02
    # The 2% cap counts the maker entry + taker exit fee (size only; the
    # stop and TP prices are unchanged). 0 = price-only 2% (old sizing).
    model_b_cap_includes_fees: bool = True
    # Rank armed candidates by setup quality before margin. The closer
    # guard and the 60% reserve use the same number. shadow logs it and
    # keeps today's order. 0 is the density score and SYMBOLS order.
    model_b_quality_rank: str = "on"
    # Per-coin maker/taker in the 2% size and the TP fee floor. 0 keeps
    # the published 1.5/4.5 bps tier for every coin.
    model_b_coin_fees: bool = True
    # 1m candles: full backfill once (staggered), then a short tail merged
    # into the cache. 0 refetches the whole window every TTL.
    model_b_candle_incremental: bool = True
    # Candle fetches wait when the last minute is already at this weight.
    # Account and guard reads are not deferred. Under the 1,200/min cap.
    model_b_weight_budget: float = 1000.0
    # Total open risk (positions + resting tickets) and one name per
    # correlated group. shadow logs would_block and still posts. 1 refuses
    # the new ticket. 0 does not look.
    model_b_risk_cap: str = "shadow"
    model_b_max_total_risk_pct: float = 6.0
    # Isolated-only assets (xyz:SMSN) set isolated margin instead of
    # failing the cross update. 0 always asks for cross.
    model_b_isolated_margin: bool = True
    # Stop-market slip in the 2% size. shadow logs would_size and posts
    # today's size. 1 shrinks size so trigger loss + slip + fees <= 2%.
    # 0 does not look. The stop price is not moved. 20x cap unchanged.
    model_b_stop_slip: str = "shadow"
    # Empty: xyz 20 bps, everything else 5 bps. A number applies to every
    # coin. A map is ``xyz:SKHX=15,xyz=20,default=5``. The live spread
    # raises the allowance when it is wider than that floor.
    model_b_stop_slip_bps: str = ""
    # Liquidity stop vs the nearest 15m swing. shadow logs when the stop
    # is not beyond that swing plus the usual buffer and does not move it.
    # 1 places the stop beyond the swing and sizes from the wider distance.
    # 0 does not look.
    model_b_htf_stop: str = "shadow"
    model_b_oversize_ratio: float = 1.1
    # scalp (default) is today's Model B. swing is the higher-timeframe book.
    # Unset MODEL_B_PAPER with swing forces paper so a style flip cannot send
    # real orders. MODEL_B_PAPER=0 is the later live opt-in.
    model_b_style: str = "scalp"
    model_b_paper: bool = False
    model_b_swing_confirm: str = "15m"
    model_b_swing_macro: str = "4h,1d"
    model_b_swing_macro_mode: str = "both"
    model_b_swing_atr_tf: str = "1h"
    model_b_swing_atr_frac: float = 0.5
    model_b_swing_min_r: float = 1.0
    model_b_swing_max_r: float = 5.0
    model_b_swing_top_n: int = 2
    model_b_swing_min_score: float = 3.0
    model_b_swing_cluster_bps: float = 20.0
    model_b_swing_pivot: int = 2
    model_b_swing_hold_days: float = 3.0
    model_b_swing_runner: bool = False
    model_b_swing_runner_frac: float = 0.5
    model_b_swing_slip_main_bps: float = 25.0
    model_b_swing_slip_xyz_bps: float = 30.0
    model_b_swing_min_sweep_bps: float = 5.0
    model_b_swing_flow: str = "tape"
    model_b_swing_fill_hours: float = 24.0
    ote_lookback_bars: int = 45
    ote_fib_shallow: float = 0.62
    ote_fib_deep: float = 0.79
    ote_stop_buffer_bps: float = 0.0
    ote_require_close: bool = False
    ote_use_htf_swings: bool = False

    # --- Scale-out / sell into strength ---
    # At SCALE_OUT_R unrealized R, close SCALE_OUT_PCT of size, move stop to
    # breakeven ± BE_BUFFER_BPS, leave runner to original TP (or RUNNER_TP_R).
    scale_out_enabled: bool = True
    scale_out_r: float = 1.0
    scale_out_pct: float = 0.5
    be_buffer_bps: float = 2.0
    # None = keep existing take_profit on remainder
    runner_tp_r: float | None = None

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
        mode_now = (self.entry_mode or "").strip().lower()
        style_now = (self.model_b_style or "scalp").strip().lower()
        if style_now not in ("scalp", "swing"):
            raise ValueError(
                f"MODEL_B_STYLE={self.model_b_style!r} must be scalp|swing."
            )
        if mode_now == "model_b" and style_now == "swing":
            if not (0.005 - 1e-12 <= self.risk_per_trade <= 0.02 + 1e-12):
                raise ValueError(
                    f"RISK_PER_TRADE={self.risk_per_trade} — swing Model B is "
                    "0.5% to 2% of spot USDC (0.5% and 1% are the tested sizes; "
                    "2% remains the hard account cap)."
                )
        elif mode_now == "model_b":
            if abs(self.risk_per_trade - 0.02) > 1e-12:
                raise ValueError(
                    f"RISK_PER_TRADE={self.risk_per_trade} — Model B sizes at "
                    "RISK_PER_TRADE=0.02 (max 2% of spot USDC, not perp account value)."
                )
        elif not (0.0025 <= self.risk_per_trade <= 0.005):
            raise ValueError(
                f"RISK_PER_TRADE={self.risk_per_trade} outside documented range "
                "0.0025–0.005 (0.25%–0.5%)."
            )
        if not (1.0 <= self.tp_r_multiple <= 3.0):
            raise ValueError(f"TP_R_MULTIPLE={self.tp_r_multiple} must be in [1, 3].")
        if (self.entry_mode or "").strip().lower() == "model_b" and self.leverage != 20:
            raise ValueError(
                f"LEVERAGE={self.leverage} — Model B is 20x only (40x off)."
            )
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
        mode = (self.entry_mode or "").strip().lower()
        if mode not in {"breakout", "ote", "both", "model_b"}:
            raise ValueError(
                f"ENTRY_MODE={self.entry_mode!r} must be breakout|ote|both|model_b."
            )
        if not (1.0 <= self.model_b_tp_r <= 2.0):
            raise ValueError(
                f"MODEL_B_TP_R={self.model_b_tp_r} must be in [1, 2]."
            )
        if self.model_b_min_prints < 1:
            raise ValueError(
                f"MODEL_B_MIN_PRINTS={self.model_b_min_prints} must be >= 1."
            )
        if not (0.0 <= self.model_b_tp_min_pool_r <= 10.0):
            raise ValueError(
                f"MODEL_B_TP_MIN_POOL_R={self.model_b_tp_min_pool_r} must be in [0, 10] (0 = off)."
            )
        if self.model_b_macro_side_only not in ("on", "off", "shadow"):
            raise ValueError(
                f"MODEL_B_MACRO_SIDE_ONLY={self.model_b_macro_side_only!r} must be 1|0|shadow."
            )
        if self.model_b_macro_range_policy not in ("both", "none"):
            raise ValueError(
                f"MODEL_B_MACRO_RANGE_POLICY={self.model_b_macro_range_policy!r} must be both|none."
            )
        if not (5.0 <= self.model_b_macro_adx_min <= 60.0):
            raise ValueError(f"MODEL_B_MACRO_ADX_MIN={self.model_b_macro_adx_min} must be in [5, 60].")
        if self.model_b_macro_mode not in ("4h_lead", "both", "4h_only", "4h_lead_1h_fill"):
            raise ValueError(
                f"MODEL_B_MACRO_MODE={self.model_b_macro_mode!r} must be 4h_lead|both|4h_only|4h_lead_1h_fill."
            )
        if not (0.0 <= self.model_b_tp_max_pool_r <= 20.0):
            raise ValueError(
                f"MODEL_B_TP_MAX_POOL_R={self.model_b_tp_max_pool_r} must be in [0, 20] (0 = no limit)."
            )
        for label, mode in (
            ("MODEL_B_TP_UNTAKEN_ONLY", self.model_b_tp_untaken_only),
            ("MODEL_B_TP_REFRESH_ON_FILL", self.model_b_tp_refresh_on_fill),
            ("MODEL_B_TP_RUNNER", self.model_b_tp_runner),
        ):
            if mode not in ("on", "off", "shadow"):
                raise ValueError(f"{label}={mode!r} must be 1|0|shadow.")
        if not (0.0 < self.model_b_tp_runner_frac < 1.0):
            raise ValueError(
                f"MODEL_B_TP_RUNNER_FRAC={self.model_b_tp_runner_frac} must be in (0, 1)."
            )
        if not (0.0 < self.model_b_tp_runner_max_r <= 20.0):
            raise ValueError(
                f"MODEL_B_TP_RUNNER_MAX_R={self.model_b_tp_runner_max_r} must be in (0, 20]."
            )
        if self.model_b_alo_timeout_sec < 0:
            raise ValueError(
                f"MODEL_B_ALO_TIMEOUT_SEC={self.model_b_alo_timeout_sec} must be >= 0."
            )
        if self.model_b_delta_flat_usdc < 0:
            raise ValueError(
                f"MODEL_B_DELTA_FLAT_USDC={self.model_b_delta_flat_usdc} must be >= 0."
            )
        if self.model_b_delta_flat_eps < 0:
            raise ValueError(
                f"MODEL_B_DELTA_FLAT_EPS={self.model_b_delta_flat_eps} must be >= 0."
            )
        if not (0.0 <= self.model_b_close_margin_reserve <= 1.0):
            raise ValueError(
                f"MODEL_B_CLOSE_MARGIN_RESERVE={self.model_b_close_margin_reserve} "
                "must be in [0, 1] (0 = off)."
            )
        if not (1 <= int(self.model_b_max_leverage) <= 50):
            raise ValueError(
                f"MODEL_B_MAX_LEVERAGE={self.model_b_max_leverage} must be "
                "in [1, 50] (unset / 0 keeps the 20x notional brake)."
            )
        if self.model_b_loss_kill_r <= 0:
            raise ValueError(f"MODEL_B_LOSS_KILL_R={self.model_b_loss_kill_r} must be > 0.")
        if not (0 < self.model_b_max_loss_pct <= 0.02):
            raise ValueError(
                f"MODEL_B_MAX_LOSS_PCT={self.model_b_max_loss_pct} must be in (0, 0.02]: "
                "no position may lose more than 2% of the account."
            )
        if self.model_b_oversize_ratio < 1.0:
            raise ValueError(
                f"MODEL_B_OVERSIZE_RATIO={self.model_b_oversize_ratio} must be >= 1."
            )
        if self.model_b_guard_sec < 0:
            raise ValueError(f"MODEL_B_GUARD_SEC={self.model_b_guard_sec} must be >= 0.")
        if self.model_b_min_stop_bps < 0:
            raise ValueError(
                f"MODEL_B_MIN_STOP_BPS={self.model_b_min_stop_bps} must be >= 0."
            )
        if self.model_b_quality_rank not in ("on", "off", "shadow"):
            raise ValueError(
                f"MODEL_B_QUALITY_RANK={self.model_b_quality_rank!r} must be 1|shadow|0."
            )
        if self.model_b_risk_cap not in ("on", "off", "shadow"):
            raise ValueError(
                f"MODEL_B_RISK_CAP={self.model_b_risk_cap!r} must be 1|shadow|0."
            )
        if self.model_b_max_total_risk_pct < 0:
            raise ValueError(
                f"MODEL_B_MAX_TOTAL_RISK_PCT={self.model_b_max_total_risk_pct} must be >= 0."
            )
        if self.model_b_weight_budget <= 0:
            raise ValueError(
                f"MODEL_B_WEIGHT_BUDGET={self.model_b_weight_budget} must be > 0."
            )
        if self.model_b_stop_slip not in ("on", "off", "shadow"):
            raise ValueError(
                f"MODEL_B_STOP_SLIP={self.model_b_stop_slip!r} must be 1|shadow|0."
            )
        if self.model_b_htf_stop not in ("on", "off", "shadow"):
            raise ValueError(
                f"MODEL_B_HTF_STOP={self.model_b_htf_stop!r} must be 1|shadow|0."
            )
        if style_now == "swing":
            if self.model_b_swing_confirm not in ("5m", "15m", "1h"):
                raise ValueError(
                    f"MODEL_B_SWING_CONFIRM={self.model_b_swing_confirm!r} must be 5m|15m|1h."
                )
            if self.model_b_swing_atr_tf not in ("15m", "1h"):
                raise ValueError(
                    f"MODEL_B_SWING_ATR_TF={self.model_b_swing_atr_tf!r} must be 15m|1h."
                )
            if self.model_b_swing_macro_mode not in ("both", "4h_lead", "4h_only", "4h_lead_1h_fill"):
                raise ValueError(
                    f"MODEL_B_SWING_MACRO_MODE={self.model_b_swing_macro_mode!r} "
                    "must be both|4h_lead|4h_only|4h_lead_1h_fill."
                )
            tfs = tuple(part.strip() for part in self.model_b_swing_macro.split(",") if part.strip())
            if len(tfs) != 2 or any(tf not in ("1h", "4h", "1d") for tf in tfs):
                raise ValueError(
                    f"MODEL_B_SWING_MACRO={self.model_b_swing_macro!r} must be two of 1h,4h,1d."
                )
            if not (0.05 <= self.model_b_swing_atr_frac <= 3.0):
                raise ValueError(
                    f"MODEL_B_SWING_ATR_FRAC={self.model_b_swing_atr_frac} must be in [0.05, 3]."
                )
            if not (1.0 <= self.model_b_swing_min_r <= self.model_b_swing_max_r <= 8.0):
                raise ValueError(
                    "MODEL_B_SWING_MIN_R / MAX_R must satisfy 1 <= min <= max <= 8."
                )
            if self.model_b_swing_top_n < 1:
                raise ValueError("MODEL_B_SWING_TOP_N must be >= 1.")
            if not (0.25 <= self.model_b_swing_hold_days <= 10.0):
                raise ValueError("MODEL_B_SWING_HOLD_DAYS must be in [0.25, 10].")
            if not (0.0 < self.model_b_swing_runner_frac < 1.0):
                raise ValueError("MODEL_B_SWING_RUNNER_FRAC must be in (0, 1).")
            if self.model_b_swing_flow not in ("tape", "off"):
                raise ValueError("MODEL_B_SWING_FLOW must be tape|off.")
            if self.model_b_swing_slip_main_bps < 0 or self.model_b_swing_slip_xyz_bps < 0:
                raise ValueError("Swing slip bps must be >= 0.")
            if self.model_b_swing_fill_hours <= 0:
                raise ValueError("MODEL_B_SWING_FILL_HOURS must be > 0.")
        if self.model_b_stop_slip_bps.strip():
            from hl_bot.strategy.model_b.stop_slip import _parse_spec

            try:
                _parse_spec("BTC", self.model_b_stop_slip_bps)
            except ValueError as exc:
                raise ValueError(
                    f"MODEL_B_STOP_SLIP_BPS={self.model_b_stop_slip_bps!r} is invalid: {exc}"
                ) from exc
        if self.ote_lookback_bars < 3:
            raise ValueError(
                f"OTE_LOOKBACK_BARS={self.ote_lookback_bars} must be >= 3."
            )
        if not (0 < self.ote_fib_shallow < self.ote_fib_deep <= 1.0):
            raise ValueError(
                f"OTE fibs invalid: shallow={self.ote_fib_shallow} "
                f"deep={self.ote_fib_deep} (need 0 < shallow < deep <= 1)."
            )
        if self.ote_stop_buffer_bps < 0:
            raise ValueError(
                f"OTE_STOP_BUFFER_BPS={self.ote_stop_buffer_bps} must be >= 0."
            )
        if self.scale_out_r <= 0:
            raise ValueError(f"SCALE_OUT_R={self.scale_out_r} must be > 0.")
        if not (0.0 < self.scale_out_pct < 1.0):
            raise ValueError(
                f"SCALE_OUT_PCT={self.scale_out_pct} must be in (0, 1)."
            )
        if self.be_buffer_bps < 0:
            raise ValueError(
                f"BE_BUFFER_BPS={self.be_buffer_bps} must be >= 0."
            )
        if self.runner_tp_r is not None and self.runner_tp_r <= 0:
            raise ValueError(
                f"RUNNER_TP_R={self.runner_tp_r} must be > 0 when set."
            )
        if self.is_live and not self.private_key:
            raise ValueError("LIVE mode requires HL_PRIVATE_KEY.")


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings from environment (optionally from a specific .env path)."""
    if env_file is not None:
        load_dotenv(env_file, override=True)
    elif not _dotenv_skipped():
        load_dotenv()

    network = os.getenv("HL_NETWORK", "mainnet").strip().lower()
    default_url = (
        "https://api.hyperliquid-testnet.xyz"
        if network == "testnet"
        else "https://api.hyperliquid.xyz"
    )
    api_url = os.getenv("HL_API_URL", default_url).strip() or default_url
    check_network_url(network, api_url)

    symbols = _parse_symbols()
    entry_mode = os.getenv("ENTRY_MODE", "both").strip().lower() or "both"
    model_b_style = (os.getenv("MODEL_B_STYLE") or "scalp").strip().lower() or "scalp"
    paper_raw = os.getenv("MODEL_B_PAPER")
    if paper_raw is None or not paper_raw.strip():
        # Swing stays on the simulator until someone sets MODEL_B_PAPER=0.
        model_b_paper = model_b_style == "swing"
    else:
        model_b_paper = paper_raw.strip().lower() in {"1", "true", "yes", "on"}
    # Mainnet tape floor stays 30. Testnet uses the measured density ratio
    # unless MODEL_B_MIN_PRINTS is set explicitly.
    if os.getenv("MODEL_B_MIN_PRINTS", "").strip():
        model_b_min_prints = _int("MODEL_B_MIN_PRINTS", MIN_PRINTS)
    elif network == "testnet":
        model_b_min_prints = density_min_prints()
    else:
        model_b_min_prints = MIN_PRINTS
    # Model B scalp is max 2% of spot USDC. Swing defaults to 1% when the
    # var is unset (0.5% and 1% are both valid). Scalp stays at 0.5% otherwise.
    if model_b_style == "swing" and entry_mode == "model_b" and not (os.getenv("RISK_PER_TRADE") or "").strip():
        risk_default = 0.01
    elif entry_mode == "model_b":
        risk_default = 0.02
    else:
        risk_default = 0.005
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
        risk_per_trade=_float("RISK_PER_TRADE", risk_default),
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
        entry_mode=entry_mode,
        model_b_tp_r=_float("MODEL_B_TP_R", 1.5),
        model_b_min_prints=model_b_min_prints,
        model_b_alo_timeout_sec=_float("MODEL_B_ALO_TIMEOUT_SEC", 0.0),
        model_b_delta_flat_usdc=_float("MODEL_B_DELTA_FLAT_USDC", DELTA_FLAT_USDC),
        model_b_delta_flat_eps=_float("MODEL_B_DELTA_FLAT_EPS", DELTA_FLAT_EPS),
        model_b_closer_score_guard=_bool("MODEL_B_CLOSER_SCORE_GUARD", True),
        model_b_close_margin_reserve=_float(
            "MODEL_B_CLOSE_MARGIN_RESERVE", CLOSE_MARGIN_RESERVE
        ),
        model_b_max_leverage=_optional_int_unset("MODEL_B_MAX_LEVERAGE") or 20,
        model_b_min_stop_bps=_float("MODEL_B_MIN_STOP_BPS", 15.0),
        model_b_structure_filter=_structure_mode() == "on",
        model_b_structure_mode=_structure_mode(),
        model_b_counter_flow=_bool("MODEL_B_COUNTER_FLOW", True),
        model_b_counter_flow_sec=_float("MODEL_B_COUNTER_FLOW_SEC", 300.0),
        model_b_counter_flow_usdc=_float("MODEL_B_COUNTER_FLOW_USDC", 1_000_000.0),
        model_b_counter_flow_eps=_float("MODEL_B_COUNTER_FLOW_EPS", 0.0),
        model_b_counter_flow_flip=_float("MODEL_B_COUNTER_FLOW_FLIP", 1.0),
        model_b_counter_flow_hold_sec=_float("MODEL_B_COUNTER_FLOW_HOLD_SEC", 30.0),
        model_b_min_sweep_bps=(os.getenv("MODEL_B_MIN_SWEEP_BPS") or "0.3").strip(),
        model_b_sweep_require_htf=_bool("MODEL_B_SWEEP_REQUIRE_HTF", False),
        model_b_two_sided=_bool("MODEL_B_TWO_SIDED", True),
        model_b_tp_min_pool_r=_float("MODEL_B_TP_MIN_POOL_R", 1.5),
        model_b_tp_max_pool_r=_float("MODEL_B_TP_MAX_POOL_R", 3.0),
        model_b_tp_far_skip_countertrend=_bool("MODEL_B_TP_FAR_SKIP_COUNTERTREND", True),
        model_b_tp_untaken_only=_tp_mode("MODEL_B_TP_UNTAKEN_ONLY", "on"),
        model_b_tp_refresh_on_fill=_tp_mode("MODEL_B_TP_REFRESH_ON_FILL", "on"),
        model_b_tp_runner=_tp_mode("MODEL_B_TP_RUNNER", "shadow"),
        model_b_tp_runner_frac=_float("MODEL_B_TP_RUNNER_FRAC", 0.5),
        model_b_tp_runner_max_r=_float("MODEL_B_TP_RUNNER_MAX_R", 5.0),
        model_b_macro_side_only=_macro_side_mode(),
        model_b_macro_range_policy=(os.getenv("MODEL_B_MACRO_RANGE_POLICY") or "both").strip().lower(),
        model_b_macro_adx_min=_float("MODEL_B_MACRO_ADX_MIN", 20.0),
        model_b_macro_mode=(os.getenv("MODEL_B_MACRO_MODE") or "4h_lead").strip().lower(),
        model_b_guard_sec=_float("MODEL_B_GUARD_SEC", 3.0),
        model_b_loss_kill_r=_float("MODEL_B_LOSS_KILL_R", 1.0),
        model_b_max_loss_pct=_float("MODEL_B_MAX_LOSS_PCT", 0.02),
        model_b_cap_includes_fees=_bool("MODEL_B_CAP_INCLUDES_FEES", True),
        model_b_quality_rank=_tp_mode("MODEL_B_QUALITY_RANK", "on"),
        model_b_coin_fees=_bool("MODEL_B_COIN_FEES", True),
        model_b_candle_incremental=_bool("MODEL_B_CANDLE_INCREMENTAL", True),
        model_b_weight_budget=_float("MODEL_B_WEIGHT_BUDGET", 1000.0),
        model_b_risk_cap=_tp_mode("MODEL_B_RISK_CAP", "shadow"),
        model_b_max_total_risk_pct=_float("MODEL_B_MAX_TOTAL_RISK_PCT", 6.0),
        model_b_isolated_margin=_bool("MODEL_B_ISOLATED_MARGIN", True),
        model_b_stop_slip=_tp_mode("MODEL_B_STOP_SLIP", "shadow"),
        model_b_stop_slip_bps=(os.getenv("MODEL_B_STOP_SLIP_BPS") or "").strip(),
        model_b_htf_stop=_tp_mode("MODEL_B_HTF_STOP", "shadow"),
        model_b_oversize_ratio=_float("MODEL_B_OVERSIZE_RATIO", 1.1),
        model_b_style=model_b_style,
        model_b_paper=model_b_paper,
        model_b_swing_confirm=(os.getenv("MODEL_B_SWING_CONFIRM") or "15m").strip() or "15m",
        model_b_swing_macro=(os.getenv("MODEL_B_SWING_MACRO") or "4h,1d").strip() or "4h,1d",
        model_b_swing_macro_mode=(os.getenv("MODEL_B_SWING_MACRO_MODE") or "both").strip().lower() or "both",
        model_b_swing_atr_tf=(os.getenv("MODEL_B_SWING_ATR_TF") or "1h").strip() or "1h",
        model_b_swing_atr_frac=_float("MODEL_B_SWING_ATR_FRAC", 0.5),
        model_b_swing_min_r=_float("MODEL_B_SWING_MIN_R", 1.0),
        model_b_swing_max_r=_float("MODEL_B_SWING_MAX_R", 5.0),
        model_b_swing_top_n=_int("MODEL_B_SWING_TOP_N", 2),
        model_b_swing_min_score=_float("MODEL_B_SWING_MIN_SCORE", 3.0),
        model_b_swing_cluster_bps=_float("MODEL_B_SWING_CLUSTER_BPS", 20.0),
        model_b_swing_pivot=_int("MODEL_B_SWING_PIVOT", 2),
        model_b_swing_hold_days=_float("MODEL_B_SWING_HOLD_DAYS", 3.0),
        model_b_swing_runner=_bool("MODEL_B_SWING_RUNNER", False),
        model_b_swing_runner_frac=_float("MODEL_B_SWING_RUNNER_FRAC", 0.5),
        model_b_swing_slip_main_bps=_float("MODEL_B_SWING_SLIP_MAIN_BPS", 25.0),
        model_b_swing_slip_xyz_bps=_float("MODEL_B_SWING_SLIP_XYZ_BPS", 30.0),
        model_b_swing_min_sweep_bps=_float("MODEL_B_SWING_MIN_SWEEP_BPS", 5.0),
        model_b_swing_flow=(os.getenv("MODEL_B_SWING_FLOW") or "tape").strip().lower() or "tape",
        model_b_swing_fill_hours=_float("MODEL_B_SWING_FILL_HOURS", 24.0),
        ote_lookback_bars=_int("OTE_LOOKBACK_BARS", 45),
        ote_fib_shallow=_float("OTE_FIB_SHALLOW", 0.62),
        ote_fib_deep=_float("OTE_FIB_DEEP", 0.79),
        ote_stop_buffer_bps=_float("OTE_STOP_BUFFER_BPS", 0.0),
        ote_require_close=_bool("OTE_REQUIRE_CLOSE", False),
        ote_use_htf_swings=_bool("OTE_USE_HTF_SWINGS", False),
        scale_out_enabled=_bool("SCALE_OUT_ENABLED", True),
        scale_out_r=_float("SCALE_OUT_R", 1.0),
        scale_out_pct=_float("SCALE_OUT_PCT", 0.5),
        be_buffer_bps=_float("BE_BUFFER_BPS", 2.0),
        runner_tp_r=_optional_float("RUNNER_TP_R", None),
        journal_path=(
            (os.getenv("MODEL_B_JOURNAL_PATH") or "").strip()
            or (
                "logs/model_b_swing_paper.jsonl"
                if model_b_paper
                else "logs/model_b_swing.jsonl"
            )
            if model_b_style == "swing"
            else os.getenv("JOURNAL_PATH", "logs/trades.jsonl")
        ),
        killswitch_file=os.getenv("KILLSWITCH_FILE", ".killswitch"),
    )
    settings.validate()
    return settings

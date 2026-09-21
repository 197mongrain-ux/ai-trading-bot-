"""Main trading loop — paper by default; LIVE only when gated."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from hl_bot.config import Settings
from hl_bot.exchange.info_client import InfoClient
from hl_bot.exchange.paper_broker import PaperBroker
from hl_bot.journal import TradeJournal
from hl_bot.risk.manager import RiskManager
from hl_bot.strategy.filters import cooldown_active
from hl_bot.strategy.vwap import VwapTrendScalp

logger = logging.getLogger(__name__)


def killswitch_active(settings: Settings) -> bool:
    if settings.kill_switch or os.getenv("KILL_SWITCH", "0").strip() in {"1", "true", "True"}:
        return True
    return Path(settings.killswitch_file).exists()


def _build_info(settings: Settings) -> InfoClient:
    return InfoClient(base_url=settings.api_url)


def _fetch_mark(info: InfoClient, symbol: str, fallback: float | None = None) -> float:
    try:
        return info.get_mark_price(symbol)
    except Exception as exc:
        logger.warning("mark fetch failed for %s: %s", symbol, exc)
        if fallback is not None:
            return fallback
        raise


def run_bot(
    settings: Settings,
    *,
    max_iterations: int | None = None,
    info: InfoClient | None = None,
    broker: PaperBroker | None = None,
    sleep_fn=time.sleep,
) -> dict:
    """Run the main loop.

    In PAPER mode (default) never instantiates LiveExchange / never calls order.
    Each iteration processes every configured symbol. Multiple independent
    positions per symbol are allowed up to ``MAX_POSITIONS_PER_SYMBOL``
    (stacking). Returns a summary dict useful for tests.

    LIVE caveat: Hyperliquid may net same-side size into one position; paper
    keeps independent stops/trade_ids. Live path does best-effort
    ``market_open`` of additional size and per-fill stop/close by size.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mode = "LIVE" if settings.is_live else "PAPER"
    symbols = tuple(settings.symbols)
    logger.info(
        "Starting hl_bot in %s mode | symbols=%s | max_per_symbol=%s | "
        "max_open=%s | stop_pct=%.4f | leverage=%sx | breakout_bars=%d | "
        "entry_mode=%s",
        mode,
        ",".join(symbols),
        settings.max_positions_per_symbol or "unlimited",
        settings.max_open_positions or "unlimited",
        settings.stop_pct,
        settings.leverage,
        settings.breakout_bars,
        settings.entry_mode,
    )
    if settings.is_live:
        logger.warning(
            "LIVE stacking is best-effort: exchange one-way netting may merge "
            "same-side size; independent paper stops/trade_ids are primary."
        )

    if settings.is_live:
        from hl_bot.exchange.live_exchange import LiveExchange

        live = LiveExchange(
            private_key=settings.private_key,
            account_address=settings.account_address or None,
            base_url=settings.api_url,
        )
    else:
        live = None

    info = info or _build_info(settings)
    broker = broker or PaperBroker(
        starting_equity=settings.starting_equity,
        symbols=symbols,
        max_positions_per_symbol=settings.max_positions_per_symbol,
    )
    risk = RiskManager(
        starting_equity=settings.starting_equity,
        risk_per_trade=settings.risk_per_trade,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_drawdown_pct=settings.max_drawdown_pct,
        max_trades_per_day=settings.max_trades_per_day,
        max_consecutive_losses=settings.max_consecutive_losses,
        leverage=settings.leverage,
        kill_switch=settings.kill_switch,
        max_open_positions=settings.max_open_positions,
        max_positions_per_symbol=settings.max_positions_per_symbol,
    )
    strategy = VwapTrendScalp(
        buffer_bps=settings.vwap_buffer_bps,
        stop_pct=settings.stop_pct,
        tp_r_multiple=settings.tp_r_multiple,
        reset_utc_hour=settings.vwap_reset_utc_hour,
        breakout_bars=settings.breakout_bars,
        min_stop_pct=settings.min_stop_pct,
        max_stop_pct=settings.max_stop_pct,
        max_range_vs_stop=settings.max_range_vs_stop,
        vol_lookback_bars=settings.vol_lookback_bars,
        max_bar_range_pct=settings.max_bar_range_pct,
        trade_hours_utc=settings.trade_hours_utc,
        htf_confirm=settings.htf_confirm,
        htf_interval=settings.htf_interval,
        entry_mode=settings.entry_mode,
        ote_lookback_bars=settings.ote_lookback_bars,
        ote_fib_shallow=settings.ote_fib_shallow,
        ote_fib_deep=settings.ote_fib_deep,
        ote_stop_buffer_bps=settings.ote_stop_buffer_bps,
        ote_require_close=settings.ote_require_close,
        ote_use_htf_swings=settings.ote_use_htf_swings,
    )
    journal = TradeJournal(settings.journal_path)
    journal.log(
        "start",
        mode=mode,
        equity=settings.starting_equity,
        symbols=list(symbols),
        stop_pct=settings.stop_pct,
        leverage=settings.leverage,
        breakout_bars=settings.breakout_bars,
        max_positions_per_symbol=settings.max_positions_per_symbol,
        max_open_positions=settings.max_open_positions,
        trade_hours_utc=settings.trade_hours_utc,
        htf_confirm=settings.htf_confirm,
        entry_cooldown_sec=settings.entry_cooldown_sec,
        entry_mode=settings.entry_mode,
        ote_lookback_bars=settings.ote_lookback_bars,
    )
    # Daily loss / consecutive-loss halt is in-memory: restarting `hl_bot run`
    # always clears it (fresh RiskManager). Optional journal marker:
    if settings.reset_daily_risk:
        journal.log(
            "risk_reset",
            reason="RESET_DAILY_RISK=1 on process start",
            day_start_equity=settings.starting_equity,
        )
        logger.info(
            "RESET_DAILY_RISK=1 — journaled risk_reset; in-memory daily halt "
            "is clear on every process start"
        )

    # Per-symbol last stop-out epoch (ENTRY_COOLDOWN_SEC)
    last_stop_ts: dict[str, float] = {}

    iterations = 0
    summary: dict = {"mode": mode, "opens": 0, "closes": 0, "halted": False, "symbols": list(symbols)}

    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break

        ks = killswitch_active(settings)

        # Fetch marks for all symbols
        marks: dict[str, float] = {}
        for symbol in symbols:
            try:
                fallback = broker.get_mark(symbol) or None
                marks[symbol] = _fetch_mark(info, symbol, fallback=fallback)
                broker.set_mark(marks[symbol], symbol=symbol)
            except Exception:
                logger.exception("cannot get mark for %s; skipping symbol this tick", symbol)

        if not marks:
            logger.warning("no marks available; sleeping")
            sleep_fn(settings.loop_interval_sec)
            continue

        equity = broker.equity_mark_to_market(marks=marks)

        # Risk flatten / kill — close ALL open positions (every trade_id)
        flatten, reason = risk.should_flatten(equity, kill_file_active=ks, env_kill=settings.kill_switch)
        if flatten and broker.has_position:
            for tid in list(broker.positions.keys()):
                pos = broker.get_position_by_id(tid)
                if pos is None:
                    continue
                px = marks.get(pos.symbol, broker.get_mark(pos.symbol))
                fill = broker.close_position(reason=reason, price=px, trade_id=tid)
                if fill:
                    risk.record_trade_close(fill.pnl)
                    journal.log(
                        "close",
                        **{k: getattr(fill, k) for k in fill.__dataclass_fields__},
                    )
                    summary["closes"] += 1
                    if live is not None:
                        try:
                            live.market_close(fill.symbol, size=fill.size)
                        except Exception:
                            logger.exception("LIVE close failed for %s", fill.symbol)
            summary["halted"] = True
            if risk.killed:
                logger.error("Kill switch / drawdown — exiting loop")
                break

        # Manage stops/TP for EVERY open position (per symbol mark)
        for symbol in symbols:
            if not broker.has_position_for(symbol):
                continue
            mark = marks.get(symbol)
            if mark is None:
                continue
            for fill in broker.check_stops(mark, symbol=symbol):
                risk.record_trade_close(fill.pnl)
                journal.log(
                    "close",
                    **{k: getattr(fill, k) for k in fill.__dataclass_fields__},
                )
                summary["closes"] += 1
                if fill.reason == "stop":
                    last_stop_ts[symbol] = time.time()
                    logger.info(
                        "[%s] stop-out — entry cooldown %ss",
                        symbol,
                        settings.entry_cooldown_sec,
                    )
                if live is not None:
                    try:
                        # Best-effort: reduce by this fill's size (netting caveat)
                        live.market_close(symbol, size=fill.size)
                    except Exception:
                        logger.exception("LIVE close failed for %s", symbol)

        # Refresh equity after any closes
        equity = broker.equity_mark_to_market(marks=marks)

        # New entries: do NOT skip a symbol merely because it already has a
        # position — only skip when at MAX_POSITIONS_PER_SYMBOL for that ticker.
        if not risk.killed and not risk.halted_daily_loss:
            for symbol in symbols:
                per_sym = broker.position_count_for(symbol)
                if (
                    settings.max_positions_per_symbol > 0
                    and per_sym >= settings.max_positions_per_symbol
                ):
                    continue
                mark = marks.get(symbol)
                if mark is None:
                    continue

                if cooldown_active(
                    last_stop_ts.get(symbol),
                    cooldown_sec=settings.entry_cooldown_sec,
                ):
                    logger.debug(
                        "[%s] entry blocked: cooldown after stop-out", symbol
                    )
                    continue

                bars = info.get_candles(symbol, interval="1m")
                htf_bars = None
                if settings.htf_confirm:
                    try:
                        htf_bars = info.get_candles(
                            symbol, interval=settings.htf_interval
                        )
                    except Exception:
                        logger.debug(
                            "[%s] HTF candle fetch failed; strategy will "
                            "aggregate from 1m",
                            symbol,
                            exc_info=True,
                        )
                        htf_bars = None
                # Pass has_position=False so strategy can signal again while stacked
                signal = strategy.on_bar(
                    mark, bars, has_position=False, htf_bars=htf_bars or None
                )
                if signal.side not in ("long", "short") or signal.stop <= 0:
                    if signal.reason:
                        logger.debug(
                            "[%s] no entry: %s", symbol, signal.reason
                        )
                    continue

                decision = risk.allow_entry(
                    equity,
                    signal.entry,
                    signal.stop,
                    positions_for_symbol=per_sym,
                    open_position_count=broker.open_position_count,
                    kill_file_active=ks,
                    env_kill=settings.kill_switch,
                )
                if not (decision.allowed and decision.size > 0):
                    logger.debug("[%s] entry blocked: %s", symbol, decision.reason)
                    continue

                fill = broker.open_position(
                    side=signal.side,  # type: ignore[arg-type]
                    size=decision.size,
                    stop_price=signal.stop,
                    take_profit=signal.take_profit,
                    price=mark,
                    symbol=symbol,
                )
                risk.record_trade_open()
                journal.log(
                    "open",
                    symbol=symbol,
                    side=signal.side,
                    size=decision.size,
                    price=mark,
                    stop=signal.stop,
                    tp=signal.take_profit,
                    vwap=signal.vwap,
                    stop_pct=settings.stop_pct,
                    leverage=settings.leverage,
                    dollar_risk=decision.dollar_risk,
                    trade_id=fill.trade_id,
                    entry_mode=signal.entry_mode or settings.entry_mode,
                    reason=signal.reason,
                )
                summary["opens"] += 1
                logger.info(
                    "OPEN %s %s size=%.6f @ %.4f stop=%.4f (%.2f bps) tp=%.4f vwap=%.4f "
                    "lev=%sx trade_id=%s stack=%d",
                    symbol,
                    signal.side,
                    decision.size,
                    mark,
                    signal.stop,
                    settings.stop_pct * 10_000.0,
                    signal.take_profit,
                    signal.vwap,
                    settings.leverage,
                    fill.trade_id,
                    per_sym + 1,
                )
                if live is not None:
                    try:
                        is_buy = signal.side == "long"
                        # Best-effort add size; exchange may net same-side
                        live.market_open(
                            symbol,
                            is_buy,
                            decision.size,
                            leverage=settings.leverage,
                        )
                        live.set_stop_loss(
                            symbol,
                            is_buy=not is_buy,
                            size=decision.size,
                            trigger_px=signal.stop,
                        )
                    except Exception:
                        logger.exception("LIVE open/stop failed for %s", symbol)

                # Update equity after open for subsequent symbols' sizing
                equity = broker.equity_mark_to_market(marks=marks)

        sleep_fn(settings.loop_interval_sec)

    journal.log("stop", equity=broker.equity_mark_to_market(), summary=summary)
    summary["equity"] = broker.equity_mark_to_market()
    summary["iterations"] = iterations
    return summary

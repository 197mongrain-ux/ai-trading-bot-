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
        logger.warning("mark fetch failed: %s", exc)
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
    Returns a summary dict useful for tests.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mode = "LIVE" if settings.is_live else "PAPER"
    logger.info("Starting hl_bot in %s mode | symbol=%s", mode, settings.symbol)

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
        starting_equity=settings.starting_equity, symbol=settings.symbol
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
    )
    strategy = VwapTrendScalp(
        buffer_bps=settings.vwap_buffer_bps,
        stop_buffer_bps=settings.stop_buffer_bps,
        tp_r_multiple=settings.tp_r_multiple,
        reset_utc_hour=settings.vwap_reset_utc_hour,
    )
    journal = TradeJournal(settings.journal_path)
    journal.log("start", mode=mode, equity=settings.starting_equity)

    iterations = 0
    summary = {"mode": mode, "opens": 0, "closes": 0, "halted": False}

    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break

        ks = killswitch_active(settings)
        try:
            mark = _fetch_mark(info, settings.symbol, fallback=broker.mark or None)
        except Exception:
            logger.exception("cannot get mark; sleeping")
            sleep_fn(settings.loop_interval_sec)
            continue

        broker.set_mark(mark)
        equity = broker.equity_mark_to_market(mark)

        # Risk flatten / kill
        flatten, reason = risk.should_flatten(equity, kill_file_active=ks, env_kill=settings.kill_switch)
        if flatten and broker.has_position:
            fill = broker.close_position(reason=reason, price=mark)
            if fill:
                risk.record_trade_close(fill.pnl)
                journal.log("close", **{k: getattr(fill, k) for k in fill.__dataclass_fields__})
                summary["closes"] += 1
                if live is not None:
                    try:
                        live.market_close(settings.symbol)
                    except Exception:
                        logger.exception("LIVE close failed")
            summary["halted"] = True
            if risk.killed:
                logger.error("Kill switch / drawdown — exiting loop")
                break

        # Manage open position stops
        if broker.has_position:
            fill = broker.check_stops(mark)
            if fill:
                risk.record_trade_close(fill.pnl)
                journal.log("close", **{k: getattr(fill, k) for k in fill.__dataclass_fields__})
                summary["closes"] += 1
                if live is not None:
                    try:
                        live.market_close(settings.symbol)
                    except Exception:
                        logger.exception("LIVE close failed")

        # New entries
        if not broker.has_position and not risk.killed and not risk.halted_daily_loss:
            bars = info.get_candles(settings.symbol, interval="1m")
            signal = strategy.on_bar(mark, bars, has_position=False)
            if signal.side in ("long", "short") and signal.stop > 0:
                decision = risk.allow_entry(
                    equity,
                    signal.entry,
                    signal.stop,
                    has_open_position=False,
                    kill_file_active=ks,
                    env_kill=settings.kill_switch,
                )
                if decision.allowed and decision.size > 0:
                    fill = broker.open_position(
                        side=signal.side,  # type: ignore[arg-type]
                        size=decision.size,
                        stop_price=signal.stop,
                        take_profit=signal.take_profit,
                        price=mark,
                    )
                    risk.record_trade_open()
                    journal.log(
                        "open",
                        side=signal.side,
                        size=decision.size,
                        price=mark,
                        stop=signal.stop,
                        tp=signal.take_profit,
                        vwap=signal.vwap,
                        dollar_risk=decision.dollar_risk,
                        trade_id=fill.trade_id,
                    )
                    summary["opens"] += 1
                    logger.info(
                        "OPEN %s size=%.6f @ %.2f stop=%.2f tp=%.2f vwap=%.2f",
                        signal.side,
                        decision.size,
                        mark,
                        signal.stop,
                        signal.take_profit,
                        signal.vwap,
                    )
                    if live is not None:
                        try:
                            is_buy = signal.side == "long"
                            live.market_open(
                                settings.symbol,
                                is_buy,
                                decision.size,
                                leverage=settings.leverage,
                            )
                            # Protective stop on opposite side
                            live.set_stop_loss(
                                settings.symbol,
                                is_buy=not is_buy,
                                size=decision.size,
                                trigger_px=signal.stop,
                            )
                        except Exception:
                            logger.exception("LIVE open/stop failed")
                else:
                    logger.debug("entry blocked: %s", decision.reason)

        sleep_fn(settings.loop_interval_sec)

    journal.log("stop", equity=broker.equity_mark_to_market(), summary=summary)
    summary["equity"] = broker.equity_mark_to_market()
    summary["iterations"] = iterations
    return summary

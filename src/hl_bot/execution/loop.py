"""Main trading loop — paper by default; LIVE only when gated."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from decimal import Decimal

from hl_bot.config import Settings
from hl_bot.exchange.info_client import InfoClient
from hl_bot.exchange.paper_broker import PaperBroker
from hl_bot.execution.brackets import BracketTicket, LiveBracketGuard
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


def _resize_model3_brackets(live, req) -> bool:
    """Cancel+replace reduce-only TP/SL so size matches the live position."""
    try:
        live.resize_model3_brackets(
            req.coin,
            req.side,
            req.size,
            req.stop_px,
            req.take_profit_px,
            req.sz_decimals,
            cancel_oids=req.cancel_oids,
            existing_legs=getattr(req, "existing_legs", ()),
        )
        return True
    except Exception:
        logger.exception("bracket resize failed for %s", getattr(req, "coin", "?"))
        return False


def run_bot(
    settings: Settings,
    *,
    max_iterations: int | None = None,
    info: InfoClient | None = None,
    broker: PaperBroker | None = None,
    live=None,
    sleep_fn=time.sleep,
) -> dict:
    """Run the main loop.

    In PAPER mode (default) never instantiates LiveExchange / never calls order.
    Each iteration processes every configured symbol. Multiple independent
    positions per symbol are allowed up to ``MAX_POSITIONS_PER_SYMBOL``
    (stacking). Returns a summary dict useful for tests.

    LIVE caveat: Hyperliquid may net same-side size into one position; paper
    keeps independent stops/trade_ids. Every live fill that opens or grows
    the position journals ``open`` and cancel-replaces reduce-only TP+SL at
    the exact position size before a kill or drawdown halt may exit.
    Scale-out (sell into strength) is paper-first; LIVE uses best-effort
    reduce-only ``market_close`` of the scaled size and resizes brackets.
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
        "entry_mode=%s | scale_out=%s@%.2fR/%.0f%%",
        mode,
        ",".join(symbols),
        settings.max_positions_per_symbol or "unlimited",
        settings.max_open_positions or "unlimited",
        settings.stop_pct,
        settings.leverage,
        settings.breakout_bars,
        settings.entry_mode,
        "on" if settings.scale_out_enabled else "off",
        settings.scale_out_r,
        settings.scale_out_pct * 100.0,
    )
    if settings.is_live:
        logger.warning(
            "LIVE stacking is best-effort: exchange one-way netting may merge "
            "same-side size; independent paper stops/trade_ids are primary."
        )

    if live is None and settings.is_live:
        from hl_bot.exchange.live_exchange import LiveExchange

        live = LiveExchange(
            private_key=settings.private_key,
            account_address=settings.account_address or None,
            base_url=settings.api_url,
        )

    info = info or _build_info(settings)
    broker = broker or PaperBroker(
        starting_equity=settings.starting_equity,
        symbols=symbols,
        max_positions_per_symbol=settings.max_positions_per_symbol,
        scale_out_enabled=settings.scale_out_enabled,
        scale_out_r=settings.scale_out_r,
        scale_out_pct=settings.scale_out_pct,
        be_buffer_bps=settings.be_buffer_bps,
        runner_tp_r=settings.runner_tp_r,
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
        scale_out_enabled=settings.scale_out_enabled,
        scale_out_r=settings.scale_out_r,
        scale_out_pct=settings.scale_out_pct,
        be_buffer_bps=settings.be_buffer_bps,
        runner_tp_r=settings.runner_tp_r,
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
    summary: dict = {
        "mode": mode,
        "opens": 0,
        "closes": 0,
        "scale_outs": 0,
        "halted": False,
        "symbols": list(symbols),
    }
    guard = (
        LiveBracketGuard(settings.stop_pct, settings.tp_r_multiple) if live is not None else None
    )
    if guard is not None:
        guard.seed_from_journal(journal.read_all())
    marks: dict[str, float] = {}
    entries_blocked = False
    halt_deferred_logged = False

    def _journal_exchange_flat(fields: dict) -> None:
        assert guard is not None
        sym = str(fields.get("symbol") or "")
        poses = list(broker.positions_for(sym)) if sym else []
        if not poses:
            journal.log("close", **fields)
            summary["closes"] += 1
            return
        for pos in poses:
            px = marks.get(sym, broker.get_mark(sym) or pos.entry_price)
            fill = broker.close_position(
                reason=str(fields.get("reason") or "exchange_flat"),
                price=px,
                trade_id=pos.trade_id,
            )
            if fill is None:
                continue
            risk.record_trade_close(fill.pnl)
            journal.log(
                "close",
                **{k: getattr(fill, k) for k in fill.__dataclass_fields__},
            )
            summary["closes"] += 1
        guard.note_local_flat(sym)

    def _apply_plan(plan) -> None:
        assert guard is not None
        for ev in plan.journal_events:
            event = str(ev.get("event") or "")
            fields = {k: v for k, v in ev.items() if k != "event"}
            if event == "close":
                _journal_exchange_flat(fields)
                continue
            journal.log(event, **fields)
            if event == "open":
                summary["opens"] += 1
        guard.commit(plan)

    def _sync_live(halt_requested: bool):
        """Journal fills, attach full-size TP/SL, and say if the process may stop."""
        assert guard is not None and live is not None

        class _Result:
            def __init__(self, may_stop: bool, block_entries: bool, naked_coins: list[str]):
                self.may_stop = may_stop
                self.block_entries = block_entries
                self.naked_coins = naked_coins

        try:
            snap = live.fetch_account()
        except Exception:
            logger.exception("live account snapshot failed")
            return _Result(False, True, ["snapshot"])
        mark_dec = {k: Decimal(str(v)) for k, v in marks.items()} if marks else None
        plan = guard.plan(snap, halt_requested=halt_requested, marks=mark_dec)
        _apply_plan(plan)
        failed = False
        for req in plan.resizes:
            if not _resize_model3_brackets(live, req):
                failed = True
        if halt_requested and plan.cancel_entry_orders:
            try:
                live.cancel_orders(list(plan.cancel_entry_orders))
            except Exception:
                logger.exception("failed to cancel resting entry orders before halt")
                failed = True
        if failed:
            naked = list(plan.naked_coins) or ["resize"]
            return _Result(False, True, naked)
        if plan.resizes or (halt_requested and plan.cancel_entry_orders):
            try:
                snap2 = live.fetch_account()
            except Exception:
                logger.exception("post-resize snapshot failed")
                return _Result(False, True, list(plan.naked_coins) or ["snapshot"])
            plan2 = guard.plan(snap2, halt_requested=halt_requested, marks=mark_dec)
            _apply_plan(plan2)
            if plan2.resizes:
                for req in plan2.resizes:
                    if not _resize_model3_brackets(live, req):
                        return _Result(False, True, list(plan2.naked_coins) or ["resize"])
            # A successful resize clears the pre-place naked flag. A position
            # we still cannot size (no valid ticket) stays naked.
            if plan2.naked_coins and not plan2.resizes:
                return _Result(False, True, list(plan2.naked_coins))
            if halt_requested and plan2.cancel_entry_orders:
                return _Result(False, True, ["pending_entry"])
            return _Result(True, False, [])
        return _Result(plan.allow_process_stop, plan.block_new_entries, list(plan.naked_coins))

    def _defer_halt(naked_coins: list[str]) -> None:
        nonlocal halt_deferred_logged
        summary["halted"] = True
        logger.error(
            "refusing halt until reduce-only TP/SL match live size (%s)",
            ",".join(naked_coins) or "unknown",
        )
        if not halt_deferred_logged:
            journal.log(
                "halt_deferred",
                reason="open position without full-size reduce-only TP/SL",
                naked_coins=list(naked_coins),
            )
            halt_deferred_logged = True


    def _one_iteration() -> bool:
        """One loop pass. Return True to leave the loop."""
        nonlocal entries_blocked, marks
        ks = killswitch_active(settings)

        marks = {}
        for symbol in symbols:
            try:
                fallback = broker.get_mark(symbol) or None
                marks[symbol] = _fetch_mark(info, symbol, fallback=fallback)
                broker.set_mark(marks[symbol], symbol=symbol)
            except Exception:
                logger.exception("cannot get mark for %s; skipping symbol this tick", symbol)

        # Fills and brackets before any halt. A resting Alo can fill between
        # passes; this is what journals ``open`` and sizes TP/SL to szi.
        if live is not None:
            synced = _sync_live(halt_requested=False)
            entries_blocked = synced.block_entries

        if not marks:
            logger.warning("no marks available; sleeping")
            sleep_fn(settings.loop_interval_sec)
            return False

        equity = broker.equity_mark_to_market(marks=marks)

        # Risk flatten / kill — close ALL open positions (every trade_id)
        flatten, reason = risk.should_flatten(
            equity, kill_file_active=ks, env_kill=settings.kill_switch
        )
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
                        closed_ok = True
                        try:
                            live.market_close(fill.symbol, size=fill.size)
                        except Exception:
                            closed_ok = False
                            logger.exception("LIVE close failed for %s", fill.symbol)
                        if (
                            closed_ok
                            and guard is not None
                            and not broker.has_position_for(fill.symbol)
                        ):
                            guard.note_local_flat(fill.symbol)
            summary["halted"] = True
            if risk.killed:
                if live is None:
                    logger.error("Kill switch / drawdown — exiting loop")
                    return True
                result = _sync_live(halt_requested=True)
                if result.may_stop:
                    logger.error("Kill switch / drawdown — exiting loop")
                    return True
                _defer_halt(result.naked_coins)
                sleep_fn(settings.loop_interval_sec)
                return False

        if risk.killed and live is not None:
            # No paper position, but an exchange fill may have landed.
            result = _sync_live(halt_requested=True)
            summary["halted"] = True
            if result.may_stop:
                logger.error("Kill switch / drawdown — exiting loop")
                return True
            _defer_halt(result.naked_coins)
            sleep_fn(settings.loop_interval_sec)
            return False

        # Scale-out (sell into strength) then stops/TP for EVERY open leg
        for symbol in symbols:
            if not broker.has_position_for(symbol):
                continue
            mark = marks.get(symbol)
            if mark is None:
                continue

            # 1) Scale out at SCALE_OUT_R before evaluating full stop/TP
            for fill in broker.check_scale_outs(
                mark,
                symbol=symbol,
                scale_out_r=settings.scale_out_r,
                scale_out_pct=settings.scale_out_pct,
                be_buffer_bps=settings.be_buffer_bps,
                runner_tp_r=settings.runner_tp_r,
                enabled=settings.scale_out_enabled,
            ):
                # Partial close — do NOT decrement open_positions / consecutive-loss
                fields = {
                    k: getattr(fill, k)
                    for k in fill.__dataclass_fields__
                    if getattr(fill, k) is not None
                }
                fields["reason"] = fill.reason or "sell_into_strength"
                journal.log("scale_out", **fields)
                summary["scale_outs"] += 1
                logger.info(
                    "SCALE_OUT %s %s size=%.6f @ %.4f pnl=%.4f rem=%.6f "
                    "stop→BE=%.4f tp=%.4f trade_id=%s",
                    fill.symbol,
                    fill.side,
                    fill.size,
                    fill.price,
                    fill.pnl,
                    fill.remaining_size or 0.0,
                    fill.stop_price or 0.0,
                    fill.take_profit or 0.0,
                    fill.trade_id,
                )
                if live is not None and guard is not None:
                    try:
                        live.market_close(symbol, size=fill.size)
                    except Exception:
                        logger.exception("LIVE scale-out failed for %s", symbol)
                    rem = broker.get_position_by_id(fill.trade_id)
                    if rem is not None:
                        guard.update_levels(
                            symbol,
                            side=rem.side,
                            stop=rem.stop_price,
                            take_profit=rem.take_profit,
                            entry=rem.entry_price,
                            trade_id=rem.trade_id,
                        )
                    _sync_live(halt_requested=False)

            # 2) Full stop / TP on remaining (and unscaled) size
            for fill in broker.check_stops(mark, symbol=symbol):
                risk.record_trade_close(fill.pnl)
                journal.log(
                    "close",
                    **{k: getattr(fill, k) for k in fill.__dataclass_fields__},
                )
                summary["closes"] += 1
                if fill.reason in ("stop", "breakeven_stop"):
                    last_stop_ts[symbol] = time.time()
                    logger.info(
                        "[%s] %s — entry cooldown %ss",
                        symbol,
                        fill.reason,
                        settings.entry_cooldown_sec,
                    )
                if live is not None:
                    closed_ok = True
                    try:
                        live.market_close(symbol, size=fill.size)
                    except Exception:
                        closed_ok = False
                        logger.exception("LIVE close failed for %s", symbol)
                    if (
                        closed_ok
                        and guard is not None
                        and not broker.has_position_for(symbol)
                    ):
                        guard.note_local_flat(symbol)

        # Refresh equity after any closes
        equity = broker.equity_mark_to_market(marks=marks)

        # New entries: do NOT skip a symbol merely because it already has a
        # position — only skip when at MAX_POSITIONS_PER_SYMBOL for that ticker.
        # Naked live positions block new hunts until TP/SL match szi.
        if not risk.killed and not risk.halted_daily_loss and not entries_blocked:
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
                if guard is not None:
                    guard.note_local_open(
                        BracketTicket(
                            coin=symbol,
                            side=signal.side,
                            stop=Decimal(str(signal.stop)),
                            take_profit=Decimal(str(signal.take_profit)),
                            entry=Decimal(str(mark)),
                            trade_id=fill.trade_id,
                        ),
                        decision.size,
                    )
                if live is not None:
                    try:
                        is_buy = signal.side == "long"
                        live.market_open(
                            symbol,
                            is_buy,
                            decision.size,
                            leverage=settings.leverage,
                        )
                    except Exception:
                        logger.exception("LIVE open failed for %s", symbol)
                    # Partial or full: brackets follow the exchange size, not
                    # the intended size, and this runs even if market_open raised
                    # after the order was accepted.
                    _sync_live(halt_requested=False)

                equity = broker.equity_mark_to_market(marks=marks)

        sleep_fn(settings.loop_interval_sec)
        return False

    try:
        while True:
            iterations += 1
            if max_iterations is not None and iterations > max_iterations:
                break
            try:
                if _one_iteration():
                    # Re-read the exchange before exiting. A fill can land
                    # after the halt decision; do not leave while it is naked.
                    if live is None:
                        break
                    confirmed = _sync_live(halt_requested=True)
                    if confirmed.may_stop:
                        break
                    _defer_halt(confirmed.naked_coins)
                    sleep_fn(settings.loop_interval_sec)
            except Exception:
                logger.exception("iteration failed; healing brackets before continuing")
                if live is not None:
                    try:
                        healed = _sync_live(halt_requested=False)
                        entries_blocked = healed.block_entries
                    except Exception:
                        logger.exception("bracket heal after iteration error failed")
                sleep_fn(settings.loop_interval_sec)
    except KeyboardInterrupt:
        logger.warning("interrupt — attaching brackets before exit")

    if live is not None:
        result = _sync_live(halt_requested=True)
        summary["equity"] = broker.equity_mark_to_market()
        summary["iterations"] = iterations
        if not result.may_stop:
            summary["naked"] = True
            summary["halted"] = True
            logger.error(
                "refusing process stop; naked positions: %s",
                ",".join(result.naked_coins),
            )
            journal.log_process_stop(
                naked_coins=result.naked_coins,
                equity=summary["equity"],
                summary=summary,
            )
            return summary

    journal.log("stop", equity=broker.equity_mark_to_market(), summary=summary)
    summary["equity"] = broker.equity_mark_to_market()
    summary["iterations"] = iterations
    return summary

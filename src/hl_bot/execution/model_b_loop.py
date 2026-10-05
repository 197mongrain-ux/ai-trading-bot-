"""Model B hunt loop.

Selected with ``ENTRY_MODE=model_b``. Breakout / OTE stay on their own
path. This loop never market-opens. Unfilled Alos are cancelled at 20s
and that swing thesis is done. Exits are stop and TP only.

Paper fills a resting Alo from a later aggressor print. Live posts
``tif=Alo`` and accepts a user fill only when ``crossed`` is false.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from hl_bot.config import Settings
from hl_bot.exchange.hl_trades import HyperliquidTradeFeed, MemoryFeed
from hl_bot.exchange.info_client import InfoClient
from hl_bot.execution.loop import killswitch_active
from hl_bot.journal import TradeJournal
from hl_bot.risk.manager import RiskManager
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import assert_leverage
from hl_bot.strategy.model_b.tape import MIN_PRINTS
from hl_bot.strategy.model_b.thesis import CloseEvent, OpenPosition, ThesisBook
from hl_bot.strategy.model_b.universe import NY_COINS, session_coins

logger = logging.getLogger(__name__)


def format_model_b_fail(decision) -> str:
    """One desk line. THIN_TAPE adds ``prints=N/30`` so a quiet tape is visible."""
    text = (
        f"MODEL_B FAIL {decision.coin} bias={decision.bias} pool={decision.pool} "
        f"swing={decision.swing} sweep={decision.sweep_price} "
        f"absorb={decision.absorb} dW={decision.window_delta} d15={decision.last_15s_delta} "
        f"score={decision.score} vol={decision.volume_tag} reason={decision.fail_reason}"
    )
    if decision.fail_reason == "THIN_TAPE":
        text += f" prints={decision.print_count}/{MIN_PRINTS}"
    return text


def _extract_oid(resp: object) -> object | None:
    if isinstance(resp, dict):
        if "oid" in resp:
            return resp["oid"]
        try:
            statuses = resp["response"]["data"]["statuses"]  # type: ignore[index]
            resting = statuses[0].get("resting") or {}
            return resting.get("oid")
        except Exception:
            return None
    return None


def run_model_b(
    settings: Settings,
    *,
    max_iterations: int | None = None,
    info: InfoClient | None = None,
    feed: MemoryFeed | HyperliquidTradeFeed | None = None,
    exchange=None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] | None = None,
    connect_feed: bool | None = None,
    coins: tuple[str, ...] | list[str] | None = None,
    pools_for: Callable | None = None,
    bbo_for: Callable | None = None,
    tick_for: Callable[[str], float] | None = None,
) -> dict:
    """Hunt the Model B universe. Returns a summary dict for tests.

    A real ``python -m hl_bot run`` connects the trade websocket. Smoke
    runs that pass ``max_iterations`` do not, unless ``feed`` is injected
    or ``connect_feed`` is set. No prints → ``THIN_TAPE``, never a guessed side.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    assert_leverage(settings.leverage)
    settings.validate()
    mode = "LIVE" if settings.is_live else "PAPER"
    if mode == "PAPER":
        logger.warning(
            "Model B paper run is for unit tests. Desk start is "
            "TRADING_MODE=live HL_NETWORK=testnet ENTRY_MODE=model_b LEVERAGE=20"
        )
    clock = now_fn or time.time
    info = info or InfoClient(base_url=settings.api_url)

    own_feed = feed is None
    if feed is None:
        feed = HyperliquidTradeFeed(
            network=settings.network,
            coins=NY_COINS,
            user=settings.account_address or None,
        )
    if connect_feed is None:
        connect_feed = own_feed and max_iterations is None
    if connect_feed and hasattr(feed, "connect"):
        feed.connect()

    live = None
    if settings.is_live:
        if exchange is not None:
            live = exchange
        else:
            from hl_bot.exchange.live_exchange import LiveExchange

            live = LiveExchange(
                private_key=settings.private_key,
                account_address=settings.account_address or None,
                base_url=settings.api_url,
            )

    book = ThesisBook()
    engine = ModelBEngine(
        thesis=book,
        tp_r=settings.model_b_tp_r,
        risk_pct=settings.risk_per_trade,
    )
    # Account rails only. Position size is equity × RISK_PER_TRADE / stop.
    risk = RiskManager(
        starting_equity=settings.starting_equity,
        risk_per_trade=settings.risk_per_trade,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_drawdown_pct=settings.max_drawdown_pct,
        max_trades_per_day=0,
        max_consecutive_losses=0,
        leverage=20,
        kill_switch=settings.kill_switch,
        max_open_positions=0,
        max_positions_per_symbol=1,
    )
    journal = TradeJournal(settings.journal_path)
    journal.log(
        "start",
        mode=mode,
        entry_mode="model_b",
        network=settings.network,
        leverage=20,
        risk_per_trade=settings.risk_per_trade,
        tp_r=settings.model_b_tp_r,
        soft_prop=False,
        strategy_kill=False,
        flow_exit=False,
        market_fallback=False,
    )

    equity = float(settings.starting_equity)
    summary: dict = {
        "mode": mode,
        "entry_mode": "model_b",
        "arms": 0,
        "fails": 0,
        "cancels": 0,
        "opens": 0,
        "closes": 0,
        "halted": False,
    }
    iterations = 0
    allow = {c.upper() for c in coins} if coins is not None else None

    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break

        now = float(clock())
        ks = killswitch_active(settings)
        flatten, reason = risk.should_flatten(
            equity, kill_file_active=ks, env_kill=settings.kill_switch
        )
        if flatten:
            for event in book.flatten({}):
                equity += event.pnl
                journal.log(
                    "close",
                    symbol=event.coin,
                    side=event.side,
                    reason=reason or event.reason,
                    pnl=event.pnl,
                    entry_mode="model_b",
                )
                summary["closes"] += 1
                if live is not None:
                    try:
                        live.market_close(event.coin, size=event.size)
                    except Exception:
                        logger.exception("LIVE flatten failed for %s", event.coin)
            summary["halted"] = True
            if risk.killed:
                logger.error("Kill switch / drawdown — exiting Model B loop")
                break

        for order in book.expire(now):
            summary["cancels"] += 1
            journal.log(
                "model_b_cancel",
                coin=order.coin,
                swing_id=order.swing_id,
                limit_px=order.limit_px,
                reason="unfilled_20s",
                entry_mode="model_b",
            )
            logger.info(
                "MODEL_B CANCEL %s swing=%s px=%s after 20s — thesis done",
                order.coin,
                order.swing_id,
                order.limit_px,
            )
            if live is not None and order.oid is not None:
                try:
                    live.cancel_order(order.coin, order.oid)
                except Exception:
                    logger.exception("LIVE cancel failed for %s", order.coin)

        if settings.is_live:
            for fill in feed.take_user_fills():
                applied = book.apply_user_fill(
                    coin=fill.coin,
                    oid=fill.oid,
                    price=fill.price,
                    ts=fill.ts,
                    crossed=fill.crossed,
                )
                if isinstance(applied, OpenPosition):
                    summary["opens"] += 1
                    journal.log(
                        "open",
                        symbol=applied.coin,
                        side=applied.side,
                        size=applied.size,
                        price=applied.entry,
                        stop=applied.stop,
                        tp=applied.take_profit,
                        entry_mode="model_b",
                        reason="alo_fill",
                    )
                    if live is not None:
                        _place_brackets(live, applied)
                elif isinstance(applied, CloseEvent):
                    equity += applied.pnl
                    risk.record_trade_close(applied.pnl)
                    summary["closes"] += 1
                    journal.log(
                        "close",
                        symbol=applied.coin,
                        side=applied.side,
                        size=applied.size,
                        price=applied.exit,
                        pnl=applied.pnl,
                        reason=applied.reason,
                        entry_mode="model_b",
                    )

        active = session_coins(now)
        if allow is not None:
            active = tuple(c for c in active if c in allow)

        for coin in active:
            prints = feed.prints(coin)
            if not settings.is_live:
                pos = book.try_fill_from_prints(prints)
                if pos is not None:
                    summary["opens"] += 1
                    journal.log(
                        "open",
                        symbol=pos.coin,
                        side=pos.side,
                        size=pos.size,
                        price=pos.entry,
                        stop=pos.stop,
                        tp=pos.take_profit,
                        entry_mode="model_b",
                        reason="alo_fill",
                    )
                    logger.info(
                        "MODEL_B FILL %s %s size=%s @ %s stop=%s tp=%s",
                        pos.coin,
                        pos.side,
                        pos.size,
                        pos.entry,
                        pos.stop,
                        pos.take_profit,
                    )

            last = prints[-1].price if prints else None
            # Paper exits on price. Live exits come from user fills (stop/TP
            # brackets). Public delta never closes a position.
            if last is not None and not settings.is_live:
                closed = book.try_exit(coin, last)
                if closed is not None:
                    equity += closed.pnl
                    risk.record_trade_close(closed.pnl)
                    summary["closes"] += 1
                    journal.log(
                        "close",
                        symbol=closed.coin,
                        side=closed.side,
                        size=closed.size,
                        price=closed.exit,
                        pnl=closed.pnl,
                        reason=closed.reason,
                        entry_mode="model_b",
                    )
                    logger.info(
                        "MODEL_B %s %s @ %s pnl=%s",
                        closed.reason.upper(),
                        closed.coin,
                        closed.exit,
                        closed.pnl,
                    )

            if book.working(coin) is not None or book.position(coin) is not None:
                continue
            if risk.halted_daily_loss or risk.killed:
                continue

            try:
                end_ms = int(now * 1000)
                start_ms = end_ms - 14 * 24 * 3600 * 1000
                # Cached per coin. Do not retry here — a 429 must not spin.
                bars = info.get_candles(coin, interval="1m", start_ms=start_ms, end_ms=end_ms)
            except Exception:
                logger.exception("candles failed for %s", coin)
                bars = []

            if pools_for is not None:
                pools = pools_for(coin, now, bars, last)
            else:
                pools = pools_from_bars(bars, now, last_price=last)
            if bbo_for is not None:
                bid, ask = bbo_for(coin)
            else:
                bid, ask = feed.bbo(coin)
            tick = tick_for(coin) if tick_for is not None else _default_tick(coin, last)

            decision = engine.evaluate(
                coin,
                now=now,
                prints=prints,
                bars=bars,
                pools=pools,
                best_bid=bid,
                best_ask=ask,
                equity=equity,
                tick=tick,
            )
            if not decision.armed or decision.intent is None:
                summary["fails"] += 1
                journal.log(
                    "model_b_fail", entry_mode="model_b", **decision.to_log()
                )
                logger.info("%s", format_model_b_fail(decision))
                continue

            intent = decision.intent
            oid = None
            if live is not None:
                try:
                    resp = live.place_alo(
                        intent.coin,
                        intent.side == "long",
                        intent.size,
                        intent.limit_px,
                        leverage=20,
                    )
                    oid = _extract_oid(resp)
                except Exception:
                    logger.exception("LIVE Alo failed for %s", coin)
                    summary["fails"] += 1
                    journal.log(
                        "model_b_fail",
                        entry_mode="model_b",
                        **{**decision.to_log(), "fail_reason": "SEND_FAIL", "armed": False},
                    )
                    continue
            book.post(intent, now, oid=oid)
            summary["arms"] += 1
            journal.log("model_b_arm", entry_mode="model_b", **decision.to_log())
            logger.info(
                "MODEL_B ARM %s %s alo=%s stop=%s tp=%s size=%s "
                "bias=%s pool=%s swing=%s sweep=%s absorb=%s "
                "dW=%s d15=%s score=%s vol=%s",
                intent.coin,
                intent.side,
                intent.limit_px,
                intent.stop,
                intent.take_profit,
                intent.size,
                decision.bias,
                decision.pool,
                decision.swing,
                decision.sweep_price,
                decision.absorb,
                decision.window_delta,
                decision.last_15s_delta,
                decision.score,
                decision.volume_tag,
            )

        equity_mark = equity
        risk.update_equity(equity_mark)
        sleep_fn(settings.loop_interval_sec)

    journal.log("stop", equity=equity, summary=summary, entry_mode="model_b")
    summary["equity"] = equity
    summary["iterations"] = iterations
    return summary


def _default_tick(coin: str, last: float | None) -> float:
    """Hyperliquid 5-significant-figure price increment, with a 1.0 floor for BTC-scale.

    Tests should pass ``tick_for``. This is only the live fallback.
    """
    import math

    price = last if last and last > 0 else 1.0
    magnitude = math.floor(math.log10(abs(price)))
    tick = 10 ** (magnitude - 4)
    return max(tick, 1e-6)


def _place_brackets(live, pos) -> None:
    is_close_buy = pos.side == "short"
    try:
        live.set_stop_loss(pos.coin, is_buy=is_close_buy, size=pos.size, trigger_px=pos.stop)
    except Exception:
        logger.exception("LIVE stop failed for %s", pos.coin)
    if hasattr(live, "set_take_profit"):
        try:
            live.set_take_profit(
                pos.coin, is_buy=is_close_buy, size=pos.size, trigger_px=pos.take_profit
            )
        except Exception:
            logger.exception("LIVE tp failed for %s", pos.coin)

"""Model B hunt loop.

Selected with ``ENTRY_MODE=model_b``. Breakout / OTE stay on their own
path. This loop never market-opens. A resting Alo stays until the thesis
is stale (a print through the sweep extreme that does not fill it).
``MODEL_B_ALO_TIMEOUT_SEC=0`` (the default) disables the clock cancel.
Exits are stop and TP only. One thesis per coin. Another coin may rest
at the same time when the sizing balance still covers that ticket's
initial margin (notional / 20) at the full 2% size. When it does not,
a new coin takes the slot only by cancelling an unfilled Alo whose
limit is strictly closer to the market, in bps. When BTC is the only
close major, 60% of free-margin capacity stays available for BTC.

Paper fills a resting Alo from a later aggressor print. Live posts
``tif=Alo`` and accepts a user fill only when ``crossed`` is false.
This process owns the stop and the TP after the fill. A drip resizes
those brackets and does not move a liquidity target back to 2R. An
amend that fails is logged; the hunt keeps running.
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
from hl_bot.strategy.model_b.alo import distance_to_fill_bps, is_closer_to_fill, market_ref
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import (
    BTC_FREE_MARGIN_RESERVE,
    assert_leverage,
    initial_margin,
    leaves_btc_headroom,
    non_btc_margin_cap,
    reserve_headroom,
    ticket_fits,
)
from hl_bot.strategy.model_b.thesis import CloseEvent, OpenPosition, ThesisBook
from hl_bot.strategy.model_b.universe import NY_COINS, session_coins

logger = logging.getLogger(__name__)


def format_model_b_fail(decision) -> str:
    """One desk line. THIN_TAPE adds ``prints=N/M`` so a quiet tape is visible."""
    text = (
        f"MODEL_B FAIL {decision.coin} bias={decision.bias} pool={decision.pool} "
        f"swing={decision.swing} sweep={decision.sweep_price} "
        f"absorb={decision.absorb} dW={decision.window_delta} d15={decision.last_15s_delta} "
        f"score={decision.score} vol={decision.volume_tag} reason={decision.fail_reason} "
        f"vp={decision.vp_tag}"
    )
    if decision.fail_reason == "THIN_TAPE":
        text += f" prints={decision.print_count}/{decision.min_prints}"
    if decision.fail_reason == "BAD_TP":
        text += (
            f" r={decision.r_distance} pool_dist={decision.pool_distance} "
            f"pool_r={decision.pool_r} why={decision.bad_tp_why}"
        )
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

    book = ThesisBook(work_sec=settings.model_b_alo_timeout_sec)
    engine = ModelBEngine(
        thesis=book,
        tp_r=settings.model_b_tp_r,
        risk_pct=settings.risk_per_trade,
        min_prints=settings.model_b_min_prints,
        alo_timeout_sec=settings.model_b_alo_timeout_sec,
        delta_flat_eps=settings.model_b_delta_flat_eps,
        delta_flat_usdc=settings.model_b_delta_flat_usdc,
    )
    # Account rails only. Position size is spot USDC × RISK_PER_TRADE / stop.
    # Paper tests pass the running equity in place of that balance. Live
    # reads spotClearinghouseState and does not use perp account value.
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
        min_prints=settings.model_b_min_prints,
        alo_timeout_sec=settings.model_b_alo_timeout_sec,
        delta_flat_usdc=settings.model_b_delta_flat_usdc,
        delta_flat_eps=settings.model_b_delta_flat_eps,
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
    # True after a pass that held the BTC free-margin reserve.
    btc_reserve_was = False
    allow = {c.upper() for c in coins} if coins is not None else None
    logger.info(
        "MODEL_B min_prints=%s network=%s",
        settings.model_b_min_prints,
        settings.network,
    )

    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break

        now = float(clock())

        def _cancel_working(order, reason: str, **extra) -> None:
            summary["cancels"] += 1
            kept = book.position(order.coin) is not None
            is_remainder = kept or bool(extra.get("remainder"))
            payload = dict(extra)
            if is_remainder:
                payload["remainder"] = "cancelled"
                payload["position_kept"] = kept
            journal.log(
                "model_b_cancel",
                coin=order.coin,
                swing_id=order.swing_id,
                limit_px=order.limit_px,
                size=order.size,
                reason=reason,
                entry_mode="model_b",
                **payload,
            )
            if is_remainder:
                tail = "position kept" if kept else "position closed"
                logger.info(
                    "MODEL_B PARTIAL %s remainder cancelled reason=%s size=%s — %s",
                    order.coin,
                    reason,
                    order.size,
                    tail,
                )
            else:
                logger.info(
                    "MODEL_B CANCEL %s swing=%s px=%s reason=%s winner=%s — thesis done",
                    order.coin,
                    order.swing_id,
                    order.limit_px,
                    reason,
                    extra.get("winner"),
                )
            if live is not None and order.oid is not None:
                try:
                    live.cancel_order(order.coin, order.oid)
                except Exception:
                    logger.exception("LIVE cancel failed for %s", order.coin)

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
                if event.remainder is not None:
                    _cancel_working(event.remainder, reason or event.reason, remainder=True)
                if live is not None:
                    try:
                        live.market_close(event.coin, size=event.size)
                    except Exception:
                        logger.exception("LIVE flatten failed for %s", event.coin)
            for order in book.flatten_cancels:
                _cancel_working(order, reason or "kill_switch")
            book.flatten_cancels = []
            summary["halted"] = True
            if risk.killed:
                logger.error("Kill switch / drawdown — exiting Model B loop")
                break

        for order in book.expire(now):
            _cancel_working(order, "unfilled_timeout")

        if settings.is_live:
            for fill in feed.take_user_fills():
                applied = book.apply_user_fill(
                    coin=fill.coin,
                    oid=fill.oid,
                    price=fill.price,
                    ts=fill.ts,
                    crossed=fill.crossed,
                    size=fill.size,
                )
                if isinstance(applied, OpenPosition):
                    if applied.just_opened:
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
                    if applied.remainder_kept:
                        journal.log(
                            "model_b_partial",
                            symbol=applied.coin,
                            side=applied.side,
                            size=applied.size,
                            added=applied.fill_added,
                            remainder_size=applied.remainder_size,
                            remainder="kept",
                            entry_mode="model_b",
                        )
                        logger.info(
                            "MODEL_B PARTIAL %s added=%s position=%s remainder=%s kept",
                            applied.coin,
                            applied.fill_added,
                            applied.size,
                            applied.remainder_size,
                        )
                    if live is not None:
                        # Brackets stay in this process. An amend that fails
                        # is logged. It does not end the hunt.
                        try:
                            if applied.just_opened:
                                _place_brackets(live, applied)
                            else:
                                _resize_brackets(live, applied)
                        except Exception:
                            logger.exception(
                                "LIVE brackets failed for %s tp=%s; hunt continues",
                                applied.coin,
                                applied.take_profit,
                            )
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
                    if applied.remainder is not None:
                        _cancel_working(
                            applied.remainder, applied.reason, remainder=True
                        )

        active = session_coins(now)
        if allow is not None:
            active = tuple(c for c in active if c in allow)
        hunts: list[dict] = []

        # Live dollar risk is 2% of spot USDC. A missing read skips new
        # arms. It is not replaced with perp account value or STARTING_EQUITY.
        risk_base: float | None
        if settings.is_live:
            user = settings.account_address or getattr(live, "account_address", "") or ""
            spot = info.spot_usdc_balance(user)
            if spot is None or spot <= 0:
                risk_base = None
                logger.warning(
                    "MODEL_B no spot USDC balance; skipping new arms "
                    "(not sizing off perp account value)"
                )
            else:
                risk_base = float(spot)
                if iterations == 1:
                    logger.info(
                        "MODEL_B sizing off spot USDC %.4f (not perp account value)",
                        risk_base,
                    )
        else:
            risk_base = equity

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

            for order in book.cancel_if_stale(coin, prints):
                _cancel_working(order, "thesis_stale")

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
                    if closed.remainder is not None and live is not None:
                        _cancel_working(closed.remainder, closed.reason, remainder=True)

            if risk.halted_daily_loss or risk.killed:
                continue
            occupied = book.working(coin) is not None or book.position(coin) is not None
            if risk_base is None:
                if not occupied:
                    summary["fails"] += 1
                    journal.log(
                        "model_b_fail",
                        entry_mode="model_b",
                        coin=coin,
                        fail_reason="NO_SPOT_USDC",
                        armed=False,
                    )
                    logger.info(
                        "MODEL_B FAIL %s reason=NO_SPOT_USDC "
                        "(no spot USDC; not using perp account value)",
                        coin,
                    )
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
                equity=risk_base,
                tick=tick,
            )
            hunts.append(
                {
                    "coin": coin,
                    "decision": decision,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "post": not occupied,
                }
            )

        decisions = [item["decision"] for item in hunts]
        reserve = btc_is_sole_close(decisions) if risk_base is not None else False
        if reserve and not btc_reserve_was:
            capacity = _btc_capacity(float(risk_base), book)
            headroom = reserve_headroom(capacity)
            logger.info(
                "MODEL_B MARGIN BTC reserve engage fraction=%.2f capacity=%.4f reserve=%.4f",
                BTC_FREE_MARGIN_RESERVE,
                capacity,
                headroom,
            )
            journal.log(
                "model_b_margin",
                entry_mode="model_b",
                coin="BTC",
                action="btc_reserve_engage",
                fraction=BTC_FREE_MARGIN_RESERVE,
                capacity=capacity,
                reserve=headroom,
            )
        elif btc_reserve_was and not reserve:
            logger.info("MODEL_B MARGIN BTC reserve release")
            journal.log(
                "model_b_margin",
                entry_mode="model_b",
                coin="BTC",
                action="btc_reserve_release",
            )
        btc_reserve_was = reserve
        if reserve and risk_base is not None:
            capacity = _btc_capacity(float(risk_base), book)
            cap = non_btc_margin_cap(capacity)
            crowded = [
                order
                for order in book.resting_orders()
                if order.coin.upper() != "BTC"
            ]
            crowded.sort(
                key=lambda order: initial_margin(order.size, order.limit_px),
                reverse=True,
            )
            kept_margin = sum(
                initial_margin(order.size, order.limit_px) for order in crowded
            )
            for order in crowded:
                if kept_margin <= cap + 1e-6:
                    break
                released = book.release_for_closer(order.coin)
                if released is None:
                    continue
                kept_margin -= initial_margin(released.size, released.limit_px)
                _cancel_working(
                    released,
                    "btc_margin_reserve",
                    reserve=reserve_headroom(capacity),
                    capacity=capacity,
                    fraction=BTC_FREE_MARGIN_RESERVE,
                )

        for hunt in hunts:
            if not hunt["post"]:
                continue
            coin = hunt["coin"]
            decision = hunt["decision"]
            bid = hunt["bid"]
            ask = hunt["ask"]
            last = hunt["last"]
            if not decision.armed or decision.intent is None:
                summary["fails"] += 1
                journal.log(
                    "model_b_fail", entry_mode="model_b", **decision.to_log()
                )
                logger.info("%s", format_model_b_fail(decision))
                continue

            if decision.delta_flat:
                logger.info(
                    "MODEL_B DELTA_FLAT %s saved=%s dW=%s d15=%s chosen=%s usdc_eps=%s coin_eps=%s usdc=%s px=%s",
                    decision.coin,
                    decision.delta_flat,
                    decision.window_delta,
                    decision.last_15s_delta,
                    decision.delta_flat_eps,
                    decision.delta_flat_usdc_eps,
                    decision.delta_flat_coin_eps,
                    engine.delta_flat_usdc,
                    decision.delta_flat_px,
                )

            intent = decision.intent
            # One thesis per coin is already enforced above. Other coins
            # may both rest when the sizing balance (spot USDC, or paper
            # equity) still covers this ticket's initial margin at the
            # size already chosen — 2% of that full balance, not a cut-down
            # size. Margin already in use is notional / 20 on resting Alos
            # and open positions. When the remainder cannot fund this
            # ticket, fall back to cancelling a strictly farther unfilled
            # Alo. A filled position is never cancelled.
            if reserve and coin != "BTC":
                capacity = _btc_capacity(float(risk_base), book)
                reserve_need = initial_margin(intent.size, intent.limit_px)
                committed = _non_btc_resting_margin(book)
                headroom = reserve_headroom(capacity)
                free_for_btc = capacity - committed
                if not leaves_btc_headroom(capacity, committed, reserve_need):
                    summary["fails"] += 1
                    journal.log(
                        "model_b_fail",
                        entry_mode="model_b",
                        **{
                            **decision.to_log(),
                            "fail_reason": "BTC_MARGIN_RESERVE",
                            "armed": False,
                            "free_margin": free_for_btc,
                            "margin_need": reserve_need,
                            "reserve": headroom,
                            "capacity": capacity,
                        },
                    )
                    journal.log(
                        "model_b_margin",
                        entry_mode="model_b",
                        coin=coin,
                        action="btc_reserve_hold",
                        free_margin=free_for_btc,
                        margin_need=reserve_need,
                        reserve=headroom,
                        capacity=capacity,
                        fraction=BTC_FREE_MARGIN_RESERVE,
                    )
                    logger.info(
                        "MODEL_B MARGIN %s btc_reserve_hold free=%.4f need=%.4f "
                        "reserve=%.4f capacity=%.4f",
                        coin,
                        free_for_btc,
                        reserve_need,
                        headroom,
                        capacity,
                    )
                    continue

            resting = [order for order in book.resting_orders() if order.coin != intent.coin]
            if resting:
                used = _margin_in_use(book)
                free = float(risk_base) - used
                need = initial_margin(intent.size, intent.limit_px)
                held = ",".join(order.coin for order in resting)
                if ticket_fits(float(risk_base), used, intent.size, intent.limit_px):
                    logger.info(
                        "MODEL_B MARGIN %s dual_rest free=%.4f need=%.4f "
                        "equity=%.4f held=%s",
                        coin,
                        free,
                        need,
                        float(risk_base),
                        held,
                    )
                    journal.log(
                        "model_b_margin",
                        entry_mode="model_b",
                        coin=coin,
                        action="dual_rest",
                        free_margin=free,
                        margin_need=need,
                        equity=float(risk_base),
                        held_by=held,
                    )
                else:
                    logger.info(
                        "MODEL_B MARGIN %s insufficient free=%.4f need=%.4f "
                        "equity=%.4f held=%s",
                        coin,
                        free,
                        need,
                        float(risk_base),
                        held,
                    )
                    challenger_ref = market_ref(bid, ask, last)
                    challenger_bps = (
                        distance_to_fill_bps(intent.side, intent.limit_px, challenger_ref)
                        if challenger_ref is not None
                        else None
                    )
                    held_by = None
                    held_bps = None
                    if challenger_bps is None:
                        held_by = resting[0]
                    else:
                        for order in resting:
                            held_prints = feed.prints(order.coin)
                            held_last = held_prints[-1].price if held_prints else None
                            if bbo_for is not None:
                                held_bid, held_ask = bbo_for(order.coin)
                            else:
                                held_bid, held_ask = feed.bbo(order.coin)
                            held_ref = market_ref(held_bid, held_ask, held_last)
                            order_bps = (
                                distance_to_fill_bps(order.side, order.limit_px, held_ref)
                                if held_ref is not None
                                else None
                            )
                            if order_bps is None or not is_closer_to_fill(
                                challenger_bps, order_bps
                            ):
                                held_by = order
                                held_bps = order_bps
                                break
                    if held_by is not None:
                        summary["fails"] += 1
                        journal.log(
                            "model_b_fail",
                            entry_mode="model_b",
                            **{
                                **decision.to_log(),
                                "fail_reason": "NOT_CLOSER",
                                "armed": False,
                                "held_by": held_by.coin,
                                "challenger_bps": challenger_bps,
                                "held_bps": held_bps,
                                "free_margin": free,
                                "margin_need": need,
                            },
                        )
                        logger.info(
                            "MODEL_B FAIL %s reason=NOT_CLOSER held_by=%s "
                            "challenger_bps=%s held_bps=%s free=%.4f need=%.4f",
                            coin,
                            held_by.coin,
                            challenger_bps,
                            held_bps,
                            free,
                            need,
                        )
                        continue
                    logger.info(
                        "MODEL_B MARGIN %s closer_cancel free=%.4f need=%.4f held=%s",
                        coin,
                        free,
                        need,
                        held,
                    )
                    for order in resting:
                        released = book.release_for_closer(order.coin)
                        if released is None:
                            continue
                        _cancel_working(
                            released,
                            "closer_ticker",
                            winner=intent.coin,
                            margin_for_better=True,
                            challenger_bps=challenger_bps,
                            free_margin=free,
                            margin_need=need,
                        )

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
                "dW=%s d15=%s score=%s vol=%s vp=%s size_adjust=%s delta_flat=%s",
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
                decision.vp_tag,
                decision.size_adjust or "-",
                decision.delta_flat or "-",
            )

        equity_mark = equity
        risk.update_equity(equity_mark)
        sleep_fn(settings.loop_interval_sec)

    journal.log("stop", equity=equity, summary=summary, entry_mode="model_b")
    summary["equity"] = equity
    summary["iterations"] = iterations
    return summary


# A confirmed swing the hunt is still waiting on. A fresh arm is a ticket,
# not a waiting setup. Thin tape and no swing never get here.
_CLOSE_FAILS = frozenset(
    {
        "NO_SWEEP",
        "NO_RECLAIM",
        "ABSORB",
        "DELTA",
        "LAST_15s",
        "NO_ALO",
        "BAD_STOP",
        "BAD_TP",
    }
)


def is_close_setup(decision) -> bool:
    """True when this pass has a live swing and did not post.

    Close is the sweep/reclaim path: waiting on the sweep (``NO_SWEEP``)
    or reclaim (``NO_RECLAIM``), or a later tape/geometry fail on that
    swing. ``THIN_TAPE``, ``NO_SWING``, ``NO_SIDE``, and ``OUT_OF_SESSION``
    have no swing, so they are not close. ``THESIS_DONE``, ``SECOND_ALO``,
    and ``AVERAGE_DOWN`` are not close. A coin that arms this pass is a
    ticket, not a waiting setup.
    """
    if decision.swing is None or decision.armed:
        return False
    return decision.fail_reason in _CLOSE_FAILS


def btc_is_sole_close(decisions) -> bool:
    """True when BTC is the only close major and some other major was judged.

    A BTC-only pass has nobody else to take the margin, so the reserve
    stays off.
    """
    close = {d.coin for d in decisions if is_close_setup(d)}
    others = {d.coin for d in decisions if d.coin != "BTC"}
    return close == {"BTC"} and bool(others)


def _btc_capacity(equity: float, book: ThesisBook) -> float:
    """Sizing balance minus margin this rule will not cancel.

    Open positions stay. A resting BTC Alo stays. Non-BTC Alos are not
    included; they are the orders the reserve may cancel.
    """
    locked = 0.0
    for pos in book.open_positions():
        locked += initial_margin(pos.size, pos.entry)
    for order in book.working_orders():
        if order.coin.upper() == "BTC":
            locked += initial_margin(order.size, order.limit_px)
    return float(equity) - locked


def _non_btc_resting_margin(book: ThesisBook) -> float:
    used = 0.0
    for order in book.resting_orders():
        if order.coin.upper() == "BTC":
            continue
        used += initial_margin(order.size, order.limit_px)
    return used


def _margin_in_use(book: ThesisBook) -> float:
    """Initial margin already reserved, in the same units as the sizing balance.

    A resting Alo uses its limit. An open position uses its fill. A partial
    counts both the filled size and the resting remainder. Spot USDC (or
    paper equity) is not re-read here.
    """
    used = 0.0
    for order in book.working_orders():
        used += initial_margin(order.size, order.limit_px)
    for pos in book.open_positions():
        used += initial_margin(pos.size, pos.entry)
    return used


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
    """Place reduce-only stop and TP at the position's prices.

    The trigger is the liquidity target already stored on the position.
    This does not recompute 2R. A failure is logged and does not raise.
    """
    try:
        is_close_buy = pos.side == "short"
        logger.info(
            "MODEL_B BRACKET %s stop=%s tp=%s size=%s",
            pos.coin,
            pos.stop,
            pos.take_profit,
            pos.size,
        )
        try:
            resp = live.set_stop_loss(
                pos.coin, is_buy=is_close_buy, size=pos.size, trigger_px=pos.stop
            )
            pos.stop_oid = _extract_oid(resp)
        except Exception:
            logger.exception("LIVE stop failed for %s", pos.coin)
        if hasattr(live, "set_take_profit"):
            try:
                resp = live.set_take_profit(
                    pos.coin,
                    is_buy=is_close_buy,
                    size=pos.size,
                    trigger_px=pos.take_profit,
                )
                pos.tp_oid = _extract_oid(resp)
            except Exception:
                logger.exception("LIVE tp failed for %s", pos.coin)
    except Exception:
        logger.exception(
            "LIVE brackets failed for %s tp=%s; hunt continues",
            getattr(pos, "coin", "?"),
            getattr(pos, "take_profit", None),
        )


def _resize_brackets(live, pos) -> None:
    """Cancel the previous reduce-only triggers and place them at the new size.

    The trigger prices stay. Only the size changes. The resting entry Alo
    is not one of these oids, so a drip does not cancel it. A failure is
    logged and does not raise.
    """
    try:
        for oid in (pos.stop_oid, pos.tp_oid):
            if oid is None:
                continue
            try:
                live.cancel_order(pos.coin, oid)
            except Exception:
                logger.exception(
                    "LIVE bracket resize cancel failed for %s oid=%s", pos.coin, oid
                )
        pos.stop_oid = None
        pos.tp_oid = None
        _place_brackets(live, pos)
    except Exception:
        logger.exception(
            "LIVE bracket resize failed for %s tp=%s; hunt continues",
            getattr(pos, "coin", "?"),
            getattr(pos, "take_profit", None),
        )

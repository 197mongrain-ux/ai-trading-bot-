"""Model B hunt loop.

Selected with ``ENTRY_MODE=model_b``. Breakout / OTE stay on their own
path. This loop never market-opens. A resting Alo stays until the thesis
is stale (a print through the sweep extreme that does not fill it).
``MODEL_B_ALO_TIMEOUT_SEC=0`` (the default) disables the clock cancel.
Exits are stop and TP only. One thesis per coin. Another coin may rest
at the same time when the sizing balance still covers that ticket's
initial margin (notional / that coin's max leverage, 20× when meta is
missing) at the full 2% size. When it does not,
a new coin takes the slot only by cancelling an unfilled Alo whose
limit is strictly closer to the market, in bps, and whose arm score
is not strictly higher. ``MODEL_B_CLOSER_SCORE_GUARD`` defaults on:
resting score 9 is not cancelled for a closer score 4
(``CLOSER_SKIP_LOWER_SCORE``). Equal scores still swap on closer bps.
``MODEL_B_CLOSE_MARGIN_RESERVE`` (default 0.60, ``0`` off) keeps that
fraction of free-margin capacity for a resting unfilled Alo: highest
arm score, then the closer limit in bps. A close setup that has not
armed does not hold it, and an equal or lower score does not steal it
when only the distance twitches. A strictly higher score takes it
immediately. It is not hard-coded to BTC. Spot USDC ``total`` is still
the 2% sizing base. Free margin subtracts margin already held by open
positions and resting entries, including builder-dex positions.
A reconcile close is only for a position this process is holding.
One exchange fill (tid, else hash) can close one journal open, and
that id is stored on the close so a later loop or restart cannot
spend it on an older open. Opens from another network, or opens
the book never adopted, are logged once and left alone. The
daily-loss tally sees each of those fills once.

Paper fills a resting Alo from a later aggressor print. Live posts
``tif=Alo`` and accepts a user fill only when ``crossed`` is false.
This process owns the stop and the TP after the fill. A drip resizes
those brackets and does not move a liquidity target back to 2R. An
amend that fails is logged; the hunt keeps running.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from hl_bot.config import Settings
from hl_bot.exchange.account import (
    AccountSnapshot,
    BookView,
    PlannedClose,
    consumed_fill_ids,
    dexs_for_coins,
    fill_id,
    plan_reconcile_closes,
)
from hl_bot.exchange.hl_trades import HyperliquidTradeFeed, MemoryFeed, UserFill
from hl_bot.exchange.info_client import InfoClient
from hl_bot.execution.loop import killswitch_active
from hl_bot.journal import TradeJournal
from hl_bot.risk.manager import RiskManager
from hl_bot.strategy.model_b.alo import (
    distance_to_fill_bps,
    is_closer_to_fill,
    market_ref,
    resting_score_blocks_closer_cancel,
)
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import (
    LEVERAGE,
    assert_leverage,
    initial_margin,
    leaves_reserve_headroom,
    other_margin_cap,
    reserve_headroom,
    ticket_fits,
    usable_leverage,
)
from hl_bot.strategy.model_b.thesis import CloseEvent, OpenPosition, ThesisBook
from hl_bot.strategy.model_b.universe import (
    DEFAULT_HUNT_COINS,
    canon_coin,
    perp_dexs_for,
    resolve_hunt_coins,
    session_coins,
)


def _env_symbols_set() -> bool:
    raw = os.getenv("SYMBOLS")
    if raw is not None and raw.strip():
        return True
    raw_one = os.getenv("SYMBOL")
    return raw_one is not None and bool(raw_one.strip())


def model_b_hunt_coins(
    settings: Settings,
    coins: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """Coins this process hunts, majors first unless ``coins`` says otherwise.

    An explicit ``coins`` argument wins (tests and callers). When
    ``ENTRY_MODE=model_b`` and ``SYMBOLS`` or ``SYMBOL`` is set, that env
    list is the hunt, so a later edit does not need a code change. Otherwise
    the default mainnet universe is used. The breakout default
    ``BTC, SOL, XRP`` is not a Model B list.
    """
    if coins is not None:
        return resolve_hunt_coins(coins)
    if (settings.entry_mode or "").strip().lower() == "model_b" and _env_symbols_set():
        return resolve_hunt_coins(settings.symbols)
    return DEFAULT_HUNT_COINS


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
    if not isinstance(resp, dict):
        return None
    if resp.get("oid") is not None:
        return resp["oid"]
    try:
        status = resp["response"]["data"]["statuses"][0]  # type: ignore[index]
    except Exception:
        return None
    if not isinstance(status, dict):
        return None
    for key in ("resting", "filled"):
        block = status.get(key) or {}
        if isinstance(block, dict) and block.get("oid") is not None:
            return block["oid"]
    return None


def _order_error(resp: object) -> str | None:
    """Exchange text when the Alo never rested. ``None`` when it did.

    Hyperliquid reports a margin reject as ``status=ok`` with
    ``statuses[0].error``, or as ``status=err``. A resting or filled oid
    is a real order. An empty body with no oid is not.
    """
    if not isinstance(resp, dict):
        return "empty response"
    if str(resp.get("status") or "").lower() == "err":
        body = resp.get("response")
        return str(body) if body else "err"
    try:
        status = resp["response"]["data"]["statuses"][0]  # type: ignore[index]
    except Exception:
        return None if _extract_oid(resp) is not None else "no resting order"
    if isinstance(status, dict) and status.get("error"):
        return str(status["error"])
    return None


def _reject_reason(detail: str) -> str:
    if "margin" in detail.lower():
        return "MARGIN_REJECT"
    return "SEND_FAIL"


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

    hunt_coins = model_b_hunt_coins(settings, coins)
    own_feed = feed is None
    if feed is None:
        feed = HyperliquidTradeFeed(
            network=settings.network,
            coins=hunt_coins,
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
                perp_dexs=perp_dexs_for(hunt_coins),
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
        coins=hunt_coins,
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
        close_margin_reserve=settings.model_b_close_margin_reserve,
        account=settings.account_address or "",
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
    # Coin that owned the close-margin reserve on the previous pass.
    reserve_coin: str | None = None
    logger.info(
        "MODEL_B min_prints=%s network=%s coins=%s closer_score_guard=%s "
        "close_margin_reserve=%.2f resting_only=1",
        settings.model_b_min_prints,
        settings.network,
        ",".join(hunt_coins),
        "on" if settings.model_b_closer_score_guard else "off",
        settings.model_b_close_margin_reserve,
    )
    announced_adopts: set[str] = set()
    announced_stale: set[str] = set()
    used_fill_ids: set[str] = set(consumed_fill_ids(journal.read_all()))
    risked_fill_ids: set[str] = set()
    session_started_at: float | None = None
    last_margin_sig: tuple | None = None

    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break

        now = float(clock())

        def _release_margin(coin: str, reason: str, **extra) -> None:
            """Journal that this coin's reserved margin is free now."""
            logger.info("MODEL_B MARGIN %s release reason=%s", coin, reason)
            journal.log(
                "model_b_margin",
                entry_mode="model_b",
                coin=coin,
                action="release",
                reason=reason,
                **extra,
            )

        def _apply_close_risk(pnl: float, ids: tuple[str, ...] | list[str]) -> None:
            """Count a close in the daily-loss tally once per fill id.

            A reconcile that runs every loop must not add the same loss
            again. Ids already charged (this process, including a
            websocket close of the same fill) are skipped.
            """
            nonlocal equity
            fresh = [item for item in ids if item not in risked_fill_ids]
            if ids and not fresh:
                return
            equity += pnl
            risk.record_trade_close(pnl)
            risked_fill_ids.update(ids)

        def _journal_reconciled_close(plan: PlannedClose) -> None:
            """Write one close for a position this process is still holding.

            A flat book is not closed from a journal open. That path
            replayed stale opens against one fill. The fill ids are
            stored on the row so a restart cannot spend them again.
            """
            ids = tuple(plan.fill_ids)
            if ids and all(item in used_fill_ids for item in ids):
                return
            existing = book.position(plan.coin)
            if existing is None:
                return
            closed = book.force_flat(plan.coin, plan.exit, reason=plan.reason)
            if closed is None:
                return
            closed.pnl = plan.pnl
            if plan.size > 0:
                closed.size = plan.size
            summary["closes"] += 1
            journal.log(
                "close",
                symbol=closed.coin,
                side=closed.side,
                size=closed.size,
                price=plan.exit,
                pnl=plan.pnl,
                reason=plan.reason,
                entry_mode="model_b",
                source=plan.source,
                entry=plan.entry,
                fill_ids=list(ids),
                network=settings.network,
                account=settings.account_address or "",
            )
            used_fill_ids.update(ids)
            _apply_close_risk(plan.pnl, ids)
            logger.info(
                "MODEL_B RECONCILE close %s @ %s pnl=%s reason=%s source=%s",
                closed.coin,
                plan.exit,
                plan.pnl,
                plan.reason,
                plan.source,
            )
            _release_margin(closed.coin, plan.reason)
            if closed.remainder is not None:
                _cancel_working(closed.remainder, plan.reason, remainder=True)

        def _block_same_coin(coin: str, reason: str, decision) -> None:
            """Do not arm or send when this coin already has a position or entry."""
            pos = book.position(coin)
            summary["fails"] += 1
            extra = {
                **decision.to_log(),
                "fail_reason": reason,
                "armed": False,
            }
            if pos is not None:
                extra["position_size"] = pos.size
                extra["position_side"] = pos.side
                margin = _position_margin(pos)
                extra["margin"] = margin
            else:
                margin = None
            journal.log("model_b_fail", entry_mode="model_b", **extra)
            logger.info(
                "MODEL_B BLOCK %s reason=%s side=%s size=%s margin=%s",
                canon_coin(coin),
                reason,
                getattr(pos, "side", None) or "-",
                getattr(pos, "size", None) or "-",
                f"{margin:.4f}" if margin is not None else "-",
            )

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
                _release_margin(event.coin, reason or event.reason)
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

        snapshot: AccountSnapshot | None = None
        ws_fills: list[UserFill] = []
        if settings.is_live and (
            getattr(info, "has_injected_account", False)
            or (settings.account_address or getattr(live, "account_address", "") or "").strip()
        ):
            user = settings.account_address or getattr(live, "account_address", "") or ""
            try:
                snapshot = info.load_account_snapshot(user, dexs_for_coins(hunt_coins))
            except Exception:
                logger.exception("MODEL_B account snapshot failed")
                snapshot = None
            if snapshot is not None and snapshot.ok:
                for kind, item in sync_exchange_book(book, snapshot, now):
                    key = f"{kind}:{canon_coin(item.coin)}"
                    if key in announced_adopts:
                        continue
                    announced_adopts.add(key)
                    if kind == "position":
                        logger.info(
                            "MODEL_B ADOPT %s %s size=%s entry=%s margin=%.4f",
                            item.coin,
                            item.side,
                            item.size,
                            item.entry,
                            item.held_margin(),
                        )
                        journal.log(
                            "model_b_margin",
                            entry_mode="model_b",
                            coin=item.coin,
                            action="adopt_position",
                            side=item.side,
                            size=item.size,
                            entry=item.entry,
                            margin=item.held_margin(),
                        )
                    else:
                        logger.info(
                            "MODEL_B ADOPT %s resting entry side=%s size=%s px=%s oid=%s",
                            item.coin,
                            item.side,
                            item.size,
                            item.limit_px,
                            item.oid,
                        )
                        journal.log(
                            "model_b_margin",
                            entry_mode="model_b",
                            coin=item.coin,
                            action="adopt_entry",
                            side=item.side,
                            size=item.size,
                            limit_px=item.limit_px,
                            oid=item.oid,
                        )

        if settings.is_live:
            ws_fills = list(feed.take_user_fills())
            for fill in ws_fills:
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
                            network=settings.network,
                            account=settings.account_address or "",
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
                    if live is not None and not applied.adopted:
                        # Brackets stay in this process. An amend that fails
                        # is logged. It does not end the hunt. An adopted
                        # position already has exchange brackets; a stop of 0
                        # must not be sent.
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
                    fid = fill_id(fill)
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
                        source="user_fill",
                        fill_ids=[fid],
                        network=settings.network,
                        account=settings.account_address or "",
                    )
                    used_fill_ids.add(fid)
                    _apply_close_risk(applied.pnl, (fid,))
                    _release_margin(applied.coin, applied.reason)
                    if applied.remainder is not None:
                        _cancel_working(
                            applied.remainder, applied.reason, remainder=True
                        )

        if session_started_at is None:
            session_started_at = now

        if snapshot is not None and snapshot.ok:
            merged = _merge_fills(snapshot.fills, ws_fills)
            reconcile_snapshot = AccountSnapshot(
                ok=True,
                positions=snapshot.positions,
                entry_orders=snapshot.entry_orders,
                fills=tuple(merged),
                reported_margin=snapshot.reported_margin,
                dexs=snapshot.dexs,
            )
            # Journal rows written above (a websocket close) are already
            # in the file, so their fill ids cannot be spent again here.
            used_fill_ids.update(consumed_fill_ids(journal.read_all()))
            planned = plan_reconcile_closes(
                journal.read_all(),
                reconcile_snapshot,
                _book_views(book),
                network=settings.network,
                account=settings.account_address or "",
                session_started_at=session_started_at,
            )
            for coin, count, reason in planned.ignored:
                if coin in announced_stale:
                    continue
                announced_stale.add(coin)
                logger.info(
                    "MODEL_B RECONCILE ignore stale coin=%s opens=%s reason=%s",
                    coin,
                    count,
                    reason,
                )
            for plan in planned.closes:
                _journal_reconciled_close(plan)

        active = session_coins(now, symbols=hunt_coins)
        hunts: list[dict] = []
        lev_map = _coin_max_leverages(info, hunt_coins)
        lev_cap = getattr(settings, "model_b_max_leverage", None)

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

        if risk_base is not None:
            held_now = _margin_in_use(book)
            free_now = float(risk_base) - held_now
            held_names = tuple(
                sorted(pos.coin for pos in book.open_positions() if pos.size > 0)
            )
            order_names = tuple(sorted(order.coin for order in book.working_orders()))
            margin_sig = (round(held_now, 4), held_names, order_names)
            if held_now > 1e-9 and margin_sig != last_margin_sig:
                logger.info(
                    "MODEL_B MARGIN free spot=%.4f held=%.4f free=%.4f positions=%s orders=%s",
                    float(risk_base),
                    held_now,
                    free_now,
                    ",".join(held_names) or "-",
                    ",".join(order_names) or "-",
                )
                journal.log(
                    "model_b_margin",
                    entry_mode="model_b",
                    action="free_margin",
                    spot=float(risk_base),
                    held=held_now,
                    free_margin=free_now,
                    positions=",".join(held_names),
                    orders=",".join(order_names),
                )
            last_margin_sig = margin_sig

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
                        network=settings.network,
                        account=settings.account_address or "",
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
                    _release_margin(closed.coin, closed.reason)
                    if closed.remainder is not None and live is not None:
                        _cancel_working(closed.remainder, closed.reason, remainder=True)

            if risk.halted_daily_loss or risk.killed:
                continue
            exposure = _exposure_reason(book, coin, snapshot)
            occupied = exposure is not None
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

            meta_lev = lev_map.get(canon_coin(coin))
            coin_lev = usable_leverage(meta_lev, lev_cap)
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
                leverage=coin_lev,
            )
            hunts.append(
                {
                    "coin": coin,
                    "decision": decision,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "post": not occupied,
                    "exposure": exposure,
                    "lev_meta": meta_lev,
                    "lev_cap": lev_cap,
                }
            )

        fraction = float(settings.model_b_close_margin_reserve)
        # Only a resting unfilled Alo owns the reserve. An unswept close
        # setup does not, so a coin that cleared every gate is not held
        # back for it. Equal scores do not flip the owner on bps alone.
        preference = (
            stick_preferred(resting_reserve_candidates(book, hunts), reserve_coin)
            if risk_base is not None and fraction > 0
            else None
        )
        preference_coin = preference.coin if preference is not None else None
        if preference_coin != reserve_coin:
            if reserve_coin:
                logger.info(
                    "MODEL_B MARGIN CLOSE_MARGIN_RESERVE release coin=%s",
                    reserve_coin,
                )
                journal.log(
                    "model_b_margin",
                    entry_mode="model_b",
                    coin=reserve_coin,
                    action="close_reserve_release",
                )
            if preference is not None and risk_base is not None:
                capacity = _reserve_capacity(float(risk_base), book, preference.coin)
                headroom = reserve_headroom(capacity, fraction)
                bps_txt = (
                    "na"
                    if preference.distance_bps is None
                    else f"{preference.distance_bps:.2f}"
                )
                logger.info(
                    "MODEL_B MARGIN CLOSE_MARGIN_RESERVE engage coin=%s "
                    "fraction=%.2f capacity=%.4f reserve=%.4f score=%s bps=%s "
                    "protected=resting",
                    preference.coin,
                    fraction,
                    capacity,
                    headroom,
                    preference.score,
                    bps_txt,
                )
                journal.log(
                    "model_b_margin",
                    entry_mode="model_b",
                    coin=preference.coin,
                    action="close_reserve_engage",
                    fraction=fraction,
                    capacity=capacity,
                    reserve=headroom,
                    score=preference.score,
                    distance_bps=preference.distance_bps,
                )
        reserve_coin = preference_coin
        if preference is not None and risk_base is not None:
            capacity = _reserve_capacity(float(risk_base), book, preference.coin)
            cap = other_margin_cap(capacity, fraction)
            crowded = [
                order
                for order in book.resting_orders()
                if canon_coin(order.coin) != preference.coin
            ]
            crowded.sort(
                key=_order_margin,
                reverse=True,
            )
            kept_margin = sum(_order_margin(order) for order in crowded)
            for order in crowded:
                if kept_margin <= cap + 1e-6:
                    break
                if settings.model_b_closer_score_guard and resting_score_blocks_closer_cancel(
                    order.score, preference.score
                ):
                    # PR #5: a strictly higher resting score is not cancelled
                    # to fund the reserve. It stays, and it no longer forces
                    # the smaller tickets out.
                    kept_margin -= _order_margin(order)
                    continue
                released = book.release_for_closer(order.coin)
                if released is None:
                    continue
                kept_margin -= _order_margin(released)
                _cancel_working(
                    released,
                    "close_margin_reserve",
                    reserve=reserve_headroom(capacity, fraction),
                    capacity=capacity,
                    fraction=fraction,
                    preferred=preference.coin,
                )

        for hunt in hunts:
            if not hunt["post"]:
                reason = hunt.get("exposure")
                decision = hunt["decision"]
                if reason and (
                    decision.fail_reason in ("AVERAGE_DOWN", "SECOND_ALO") or decision.armed
                ):
                    _block_same_coin(hunt["coin"], reason, decision)
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
            # size. Margin already in use is notional / the leverage set on
            # that order (20× when it was adopted or meta was missing).
            # When the remainder cannot fund this
            # ticket, fall back to cancelling a strictly farther unfilled
            # Alo. A filled position is never cancelled.
            if (
                preference is not None
                and canon_coin(coin) != preference.coin
                and not _score_beats_reserve(decision.score, preference.score)
            ):
                capacity = _reserve_capacity(float(risk_base), book, preference.coin)
                reserve_need = _order_margin(intent)
                committed = _other_resting_margin(book, preference.coin)
                headroom = reserve_headroom(capacity, fraction)
                free_for_preferred = capacity - committed
                if not leaves_reserve_headroom(
                    capacity, committed, reserve_need, fraction
                ):
                    summary["fails"] += 1
                    journal.log(
                        "model_b_fail",
                        entry_mode="model_b",
                        **{
                            **decision.to_log(),
                            "fail_reason": "CLOSE_MARGIN_RESERVE",
                            "armed": False,
                            "free_margin": free_for_preferred,
                            "margin_need": reserve_need,
                            "reserve": headroom,
                            "capacity": capacity,
                            "preferred": preference.coin,
                        },
                    )
                    journal.log(
                        "model_b_margin",
                        entry_mode="model_b",
                        coin=coin,
                        action="close_reserve_hold",
                        free_margin=free_for_preferred,
                        margin_need=reserve_need,
                        reserve=headroom,
                        capacity=capacity,
                        fraction=fraction,
                        preferred=preference.coin,
                    )
                    logger.info(
                        "MODEL_B MARGIN %s CLOSE_MARGIN_RESERVE hold free=%.4f "
                        "need=%.4f reserve=%.4f capacity=%.4f preferred=%s "
                        "leverage=%s",
                        coin,
                        free_for_preferred,
                        reserve_need,
                        headroom,
                        capacity,
                        preference.coin,
                        intent.leverage,
                    )
                    continue

            resting = [order for order in book.resting_orders() if order.coin != intent.coin]
            used_now = _margin_in_use(book)
            need_now = _order_margin(intent)
            free_now = float(risk_base) - used_now
            if (
                used_now > 1e-9
                and not resting
                and not ticket_fits(
                    float(risk_base),
                    used_now,
                    intent.size,
                    intent.limit_px,
                    intent.leverage,
                )
            ):
                held_by = ",".join(pos.coin for pos in book.open_positions()) or "-"
                summary["fails"] += 1
                journal.log(
                    "model_b_fail",
                    entry_mode="model_b",
                    **{
                        **decision.to_log(),
                        "fail_reason": "INSUFFICIENT_MARGIN",
                        "armed": False,
                        "free_margin": free_now,
                        "margin_need": need_now,
                        "held": used_now,
                        "spot": float(risk_base),
                        "held_by": held_by,
                    },
                )
                logger.info(
                    "MODEL_B MARGIN %s insufficient free=%.4f need=%.4f "
                    "spot=%.4f held=%.4f held_by=%s leverage=%s",
                    coin,
                    free_now,
                    need_now,
                    float(risk_base),
                    used_now,
                    held_by,
                    intent.leverage,
                )
                continue
            if used_now > 1e-9 and not resting and ticket_fits(
                float(risk_base),
                used_now,
                intent.size,
                intent.limit_px,
                intent.leverage,
            ):
                held_by = ",".join(pos.coin for pos in book.open_positions()) or "-"
                logger.info(
                    "MODEL_B MARGIN %s position_free free=%.4f need=%.4f "
                    "spot=%.4f held=%.4f held_by=%s",
                    coin,
                    free_now,
                    need_now,
                    float(risk_base),
                    used_now,
                    held_by,
                )
                journal.log(
                    "model_b_margin",
                    entry_mode="model_b",
                    coin=coin,
                    action="position_free",
                    free_margin=free_now,
                    margin_need=need_now,
                    spot=float(risk_base),
                    held=used_now,
                    held_by=held_by,
                )
            if resting:
                used = _margin_in_use(book)
                free = float(risk_base) - used
                need = _order_margin(intent)
                held = ",".join(order.coin for order in resting)
                if ticket_fits(
                    float(risk_base),
                    used,
                    intent.size,
                    intent.limit_px,
                    intent.leverage,
                ):
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
                        "equity=%.4f held=%s leverage=%s",
                        coin,
                        free,
                        need,
                        float(risk_base),
                        held,
                        intent.leverage,
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
                    # Bps already says the candidate is closer. A strictly
                    # higher resting score keeps that ticket. Equal scores
                    # fall through and still swap. One higher-score Alo
                    # blocks the whole cancel: margin cannot dual-rest.
                    if settings.model_b_closer_score_guard:
                        blocker = None
                        for order in resting:
                            if resting_score_blocks_closer_cancel(
                                order.score, decision.score
                            ):
                                if blocker is None or int(order.score) > int(
                                    blocker.score
                                ):
                                    blocker = order
                        if blocker is not None:
                            summary["fails"] += 1
                            journal.log(
                                "model_b_fail",
                                entry_mode="model_b",
                                **{
                                    **decision.to_log(),
                                    "fail_reason": "CLOSER_SKIP_LOWER_SCORE",
                                    "armed": False,
                                    "held_by": blocker.coin,
                                    "resting_score": blocker.score,
                                    "candidate_score": decision.score,
                                    "challenger_bps": challenger_bps,
                                    "free_margin": free,
                                    "margin_need": need,
                                },
                            )
                            logger.info(
                                "MODEL_B FAIL %s reason=CLOSER_SKIP_LOWER_SCORE "
                                "held_by=%s resting_score=%s candidate_score=%s "
                                "challenger_bps=%s free=%.4f need=%.4f",
                                coin,
                                blocker.coin,
                                blocker.score,
                                decision.score,
                                challenger_bps,
                                free,
                                need,
                            )
                            continue
                    logger.info(
                        "MODEL_B MARGIN %s closer_cancel free=%.4f need=%.4f held=%s "
                        "candidate_score=%s",
                        coin,
                        free,
                        need,
                        held,
                        decision.score,
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
                            resting_score=released.score,
                            candidate_score=decision.score,
                        )

            exposure_now = _exposure_reason(book, intent.coin, snapshot)
            if exposure_now:
                _block_same_coin(intent.coin, exposure_now, decision)
                continue

            meta_txt = hunt.get("lev_meta") if hunt.get("lev_meta") else "-"
            cap_txt = hunt.get("lev_cap") if hunt.get("lev_cap") else "coin"
            logger.info(
                "MODEL_B LEVERAGE %s max=%s used=%s cap=%s margin=%.4f risk=%.4f",
                intent.coin,
                meta_txt,
                intent.leverage,
                cap_txt,
                _order_margin(intent),
                intent.size * abs(intent.limit_px - intent.stop),
            )
            oid = None
            if live is not None:
                rejected: str | None = None
                detail = ""
                try:
                    resp = live.place_alo(
                        intent.coin,
                        intent.side == "long",
                        intent.size,
                        intent.limit_px,
                        leverage=intent.leverage,
                    )
                except Exception as exc:
                    logger.exception("LIVE Alo failed for %s", coin)
                    detail = str(exc)
                    rejected = _reject_reason(detail)
                else:
                    detail = _order_error(resp) or ""
                    oid = _extract_oid(resp)
                    if detail or oid is None:
                        detail = detail or "no resting order"
                        rejected = _reject_reason(detail)
                        logger.info(
                            "MODEL_B %s %s detail=%s — ticket dropped",
                            rejected,
                            coin,
                            detail,
                        )
                if rejected is not None:
                    # The order never rested. Do not post it into the book:
                    # a phantom ticket would reserve margin until the thesis
                    # went stale and block the next coin.
                    summary["fails"] += 1
                    journal.log(
                        "model_b_fail",
                        entry_mode="model_b",
                        **{
                            **decision.to_log(),
                            "fail_reason": rejected,
                            "armed": False,
                            "detail": detail,
                        },
                    )
                    logger.info(
                        "MODEL_B MARGIN %s drop reason=%s detail=%s",
                        coin,
                        rejected,
                        detail,
                    )
                    journal.log(
                        "model_b_margin",
                        entry_mode="model_b",
                        coin=coin,
                        action="drop",
                        reason=rejected,
                        detail=detail,
                    )
                    continue
            book.post(intent, now, oid=oid, score=decision.score)
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


@dataclass(frozen=True)
class ClosePreference:
    """Close setup that owns the free-margin reserve for this pass."""

    coin: str
    score: int
    distance_bps: float | None


def close_distance_bps(decision, ref_px: float | None) -> float | None:
    """Bps from the market to this close setup's swing. Smaller is closer.

    Long uses ``ref - swing`` and short uses ``swing - ref``, the same
    gap as ``distance_to_fill_bps``. Bias ``NONE`` has no side, so the
    gap is the absolute distance. Already through the swing is 0.
    """
    swing = getattr(decision, "swing", None)
    if swing is None or ref_px is None or float(ref_px) <= 0 or float(swing) <= 0:
        return None
    side = getattr(decision, "bias", None)
    if side in ("long", "short"):
        return distance_to_fill_bps(side, float(swing), float(ref_px))
    gap = abs(float(ref_px) - float(swing))
    return gap / float(ref_px) * 10_000.0


def _bps_is_closer(left: float | None, right: float | None) -> bool:
    """True when ``left`` is strictly closer than ``right``.

    A known distance beats a missing one. Two missing distances tie.
    """
    if left is None or right is None:
        return left is not None and right is None
    return float(left) < float(right) - 1e-6


def preferred_close(hunts) -> ClosePreference | None:
    """The close coin that keeps the reserve, or None.

    Close is a confirmed swing that did not post (see ``is_close_setup``).
    Tie-break, in order:

    1. Highest ``score`` among close setups.
    2. Closer swing in bps (``close_distance_bps``: mid, else last).
       A known distance beats a missing one.
    3. Earlier coin in this pass's hunt order.

    None when nothing is close, or when that coin is the only one judged.
    A one-coin pass has nobody else who can spend the margin.
    """
    if len(hunts) < 2:
        return None
    best: ClosePreference | None = None
    # Walk hunt order. A later coin replaces the leader only when its
    # score is higher, or the scores tie and its swing is strictly closer.
    # Equal score and equal (or both missing) distance keep the earlier coin.
    for hunt in hunts:
        decision = hunt["decision"]
        if not is_close_setup(decision):
            continue
        ref = market_ref(hunt.get("bid"), hunt.get("ask"), hunt.get("last"))
        bps = close_distance_bps(decision, ref)
        score = int(getattr(decision, "score", 0) or 0)
        candidate = ClosePreference(
            coin=canon_coin(decision.coin),
            score=score,
            distance_bps=bps,
        )
        if best is None or score > best.score or (
            score == best.score and _bps_is_closer(bps, best.distance_bps)
        ):
            best = candidate
    return best


def stick_preferred(candidates: list[ClosePreference], incumbent: str | None) -> ClosePreference | None:
    """Keep the current reserve coin unless a strictly higher score shows up.

    Equal scores do not flip on a closer bps print. That is what made
    BTC/ETH/SOL trade the reserve every few seconds while none of them
    had armed. The incumbent is dropped when it is no longer resting.
    """
    best: ClosePreference | None = None
    for cand in candidates:
        if best is None or cand.score > best.score or (
            cand.score == best.score and _bps_is_closer(cand.distance_bps, best.distance_bps)
        ):
            best = cand
    if best is None:
        return None
    if not incumbent:
        return best
    current = next((cand for cand in candidates if cand.coin == canon_coin(incumbent)), None)
    if current is None:
        return best
    if best.score > current.score:
        return best
    return current


def _score_beats_reserve(candidate: int | None, resting: int | None) -> bool:
    """A strictly higher score is not reserve-blocked. The closer guard still applies."""
    if candidate is None or resting is None:
        return False
    return int(candidate) > int(resting)


def resting_reserve_candidates(book: ThesisBook, hunts) -> list[ClosePreference]:
    """Resting unfilled Alos, in hunt order. Unarmed close setups are not included."""
    hunt_by = {canon_coin(hunt["decision"].coin): hunt for hunt in hunts}
    coins = [canon_coin(hunt["decision"].coin) for hunt in hunts]
    for order in book.resting_orders():
        if order.coin not in coins:
            coins.append(order.coin)
    candidates: list[ClosePreference] = []
    for coin in coins:
        order = next((item for item in book.resting_orders() if item.coin == coin), None)
        if order is None:
            continue
        hunt = hunt_by.get(coin)
        ref = None
        if hunt is not None:
            ref = market_ref(hunt.get("bid"), hunt.get("ask"), hunt.get("last"))
        bps = None
        if ref is not None and float(ref) > 0:
            bps = distance_to_fill_bps(order.side, order.limit_px, float(ref))
        score = int(order.score) if order.score is not None else 0
        candidates.append(ClosePreference(coin=coin, score=score, distance_bps=bps))
    return candidates


def _exposure_reason(book: ThesisBook, coin: str, snapshot: AccountSnapshot | None) -> str | None:
    """Why this coin cannot take a new entry. ``None`` when it is flat."""
    name = canon_coin(coin)
    if book.position(name) is not None:
        return "OPEN_POSITION"
    if snapshot is not None and snapshot.ok and snapshot.position(name) is not None:
        return "OPEN_POSITION"
    if book.working(name) is not None:
        return "RESTING_ENTRY"
    if snapshot is not None and snapshot.ok and snapshot.entry_order(name) is not None:
        return "RESTING_ENTRY"
    return None


def sync_exchange_book(book: ThesisBook, snapshot: AccountSnapshot, now: float) -> list[tuple]:
    """Adopt exchange positions and resting entries. Drop stubs the exchange released.

    Returns ``("position"|"entry", item)`` for each newly adopted row.
    Managed orders this process posted are left alone.
    """
    if not snapshot.ok:
        return []
    events: list[tuple] = []
    open_coins = {pos.coin for pos in snapshot.positions if pos.size > 0}
    entry_coins = {order.coin for order in snapshot.entry_orders}
    entry_oids = {order.oid for order in snapshot.entry_orders if order.oid is not None}
    for order in list(book.working_orders()):
        if not getattr(order, "external", False):
            continue
        if order.coin in open_coins and order.coin not in entry_coins:
            book.drop_external(order.coin)
            continue
        still_resting = (order.oid is not None and order.oid in entry_oids) or (
            order.coin in entry_coins
        )
        if not still_resting:
            book.drop_external(order.coin)
    known_oids = {order.oid for order in book.working_orders() if order.oid is not None}
    for pos in snapshot.positions:
        if pos.size <= 0:
            continue
        existed = book.position(pos.coin) is not None
        adopted = book.adopt_position(
            coin=pos.coin,
            side=pos.side,
            size=pos.size,
            entry=pos.entry,
            now=now,
            margin_used=pos.held_margin(),
        )
        if adopted is not None and not existed:
            events.append(("position", pos))
    for order in snapshot.entry_orders:
        if order.oid is not None and order.oid in known_oids:
            continue
        if book.working(order.coin) is not None:
            continue
        created = book.adopt_entry(
            coin=order.coin,
            side=order.side,
            limit_px=order.limit_px,
            size=order.size,
            now=now,
            oid=order.oid,
        )
        if created is not None:
            events.append(("entry", order))
    book.margin_floor = max(0.0, float(snapshot.reported_margin))
    return events


def _book_views(book: ThesisBook) -> list[BookView]:
    return [
        BookView(
            coin=pos.coin,
            side=pos.side,
            size=pos.size,
            entry=pos.entry,
            stop=pos.stop,
            take_profit=pos.take_profit,
            opened_at=pos.opened_at,
        )
        for pos in book.open_positions()
    ]


def _merge_fills(snapshot_fills, ws_fills: list[UserFill]) -> list[UserFill]:
    """Snapshot fills plus this pass's websocket fills, one row per tid."""
    merged: list[UserFill] = []
    seen: set[object] = set()
    for fill in list(snapshot_fills) + list(ws_fills):
        tid = getattr(fill, "tid", None)
        if tid is not None and tid != "":
            if tid in seen:
                continue
            seen.add(tid)
        merged.append(fill)
    return merged


def _ticket_leverage(ticket) -> int:
    """Leverage stored on an order or position. Missing stays 20."""
    raw = getattr(ticket, "leverage", LEVERAGE)
    try:
        lev = int(raw)
    except (TypeError, ValueError):
        return LEVERAGE
    return lev if lev >= 1 else LEVERAGE


def _order_margin(ticket) -> float:
    """Initial margin of a resting Alo or an intent at the leverage it was set to."""
    return initial_margin(ticket.size, ticket.limit_px, _ticket_leverage(ticket))


def _coin_max_leverages(info, coins) -> dict[str, int]:
    """Exchange max leverage by coin. A miss or a failure is an empty map.

    Callers then use 20×. An injected map does not touch the network.
    """
    loader = getattr(info, "max_leverages", None)
    if loader is None:
        return {}
    try:
        loaded = loader(dexs_for_coins(coins))
    except Exception:
        logger.exception("maxLeverage meta failed; margin falls back to 20x")
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _position_margin(pos) -> float:
    """Exchange ``marginUsed`` when the position has one, else notional / leverage.

    Adopted positions carry the exchange figure. A position this process
    opened uses the leverage set before the entry. Unknown leverage is 20×.
    """
    held = getattr(pos, "margin_used", None)
    if held is not None and float(held) > 0:
        return float(held)
    return initial_margin(pos.size, pos.entry, _ticket_leverage(pos))


def _reserve_capacity(equity: float, book: ThesisBook, coin: str) -> float:
    """Sizing balance minus margin this rule will not cancel.

    Open positions stay. A resting Alo on ``coin`` stays. Every other
    Alo is left out; those are the orders the reserve may cancel.
    """
    preferred = canon_coin(coin)
    locked = 0.0
    for pos in book.open_positions():
        locked += _position_margin(pos)
    for order in book.working_orders():
        if canon_coin(order.coin) == preferred:
            locked += _order_margin(order)
    return float(equity) - locked


def _other_resting_margin(book: ThesisBook, coin: str) -> float:
    preferred = canon_coin(coin)
    used = 0.0
    for order in book.resting_orders():
        if canon_coin(order.coin) == preferred:
            continue
        used += _order_margin(order)
    return used


def _margin_in_use(book: ThesisBook) -> float:
    """Initial margin already reserved, in the same units as the sizing balance.

    A resting Alo uses its limit. An open position uses exchange
    ``marginUsed`` when that was adopted, otherwise its fill. A partial
    counts both the filled size and the resting remainder. ``margin_floor``
    is the clearinghouse ``totalMarginUsed`` when that figure is larger,
    so a builder-dex position is not dropped just because the local book
    was empty at startup. Spot USDC (or paper equity) is not re-read here.
    """
    used = 0.0
    for order in book.working_orders():
        used += _order_margin(order)
    for pos in book.open_positions():
        used += _position_margin(pos)
    floor = float(getattr(book, "margin_floor", 0.0) or 0.0)
    return max(used, floor)


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

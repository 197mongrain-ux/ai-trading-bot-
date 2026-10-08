"""Model B protection guard: no open position without a stop, ever.

Runs every main-loop pass (right after fills are read, before any cancel
or thesis-stale cleanup) and, in a live run, on its own fast thread every
``MODEL_B_GUARD_SEC`` seconds (default 3). It reads the exchange — every
position on every dex the hunt trades (default + ``xyz``) — and does not
depend on the hunt still tracking a ticket.

Per open position, in this order:

1. LOSS_KILL: unrealized loss above ``loss_kill_r`` x the planned trade
   risk (size x |entry - stop| at the arm), or above ``risk_pct`` of the
   account when the plan is unknown (adopted / manual), closes the whole
   position with a reduce-only IOC. Independent of any stop.
2. OVERSIZE_CUT: notional above ``max_leverage`` x account, or size above
   ``oversize_ratio`` x the ticket size, is cut back with a reduce-only IOC.
3. Stop: a reduce-only stop must cover the full size. Missing or short ->
   place / resize it at the plan's stop (adopted with no plan: the price
   that loses 2% of the account, fees included). A naked position is not
   closed just for being naked:
   - price already through the planned stop but not through the 2% level
     -> NAKED_STOP_PLACED: a full-size stop at the 2% level instead;
   - no plan (adopted / manual) -> NAKED_STOP_PLACED at the 2% level;
   - price already through the 2% level -> NAKED_CLOSE (market close);
   - the stop is rejected on this pass (two tries) -> the coin stays
     unprotected (no new entries) and the next pass (~3s) tries again;
     rejected again -> NAKED_CLOSE.
   After a NAKED_STOP_PLACED the coin stays unprotected (UNPROTECTED_BOOK
   blocks new entries) until a later snapshot shows the stop resting.
4. TP: the plan's TP is placed / resized the same way (a TP failure is
   logged, not a close).

Every NAKED_STOP_PLACED / NAKED_CLOSE / LOSS_KILL / OVERSIZE_CUT writes a ``MODEL_B ALERT``
line and a ``model_b_alert`` journal row for the desk ping. A kill also
cancels that coin's resting entry so it cannot refill into a new naked
position.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from hl_bot.exchange.account import AccountSnapshot, PerpPosition, ProtectiveOrder
from hl_bot.strategy.model_b.risk import HARD_MAX_LOSS_PCT, MAKER_FEE_RATE, TAKER_FEE_RATE
from hl_bot.strategy.model_b.universe import canon_coin

logger = logging.getLogger("hl_bot.execution.model_b_loop")

_SIZE_TOL = 1e-6


@dataclass(frozen=True)
class Plan:
    """What the hunt intended for a coin: from a resting ticket or a filled one."""

    coin: str
    side: str
    stop: float
    take_profit: float
    intended_size: float
    planned_risk: float
    source: str = "ticket"
    # Set only while both TPs are still working. After TP1 the plan's
    # take_profit is the runner and these stay 0, so the guard covers the
    # remainder at that one price and does not cancel the runner order.
    tp1_size: float = 0.0
    runner_tp: float = 0.0
    runner_size: float = 0.0


def _resp_error(resp: object) -> str | None:
    """Exchange error text, or ``None`` when the order was accepted."""
    if resp is None:
        return "empty response"
    if not isinstance(resp, dict):
        return None
    if str(resp.get("status") or "").lower() == "err":
        return str(resp.get("response") or "err")
    try:
        status = resp["response"]["data"]["statuses"][0]  # type: ignore[index]
    except Exception:
        return None
    if isinstance(status, dict) and status.get("error"):
        return str(status["error"])
    return None


def _resp_oid(resp: object) -> object | None:
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


class LockedJournal:
    """TradeJournal shared by the hunt and the guard thread (one writer at a time)."""

    def __init__(self, journal):
        self._journal = journal
        self._lock = threading.Lock()

    def log(self, event: str, **fields):
        with self._lock:
            return self._journal.log(event, **fields)

    def read_all(self):
        with self._lock:
            return self._journal.read_all()

    def __getattr__(self, name):
        return getattr(self._journal, name)


class PositionGuard:
    def __init__(
        self,
        live,
        info,
        *,
        user: str,
        dexs: tuple[str, ...],
        journal=None,
        risk_pct: float = 0.02,
        max_leverage: int = 20,
        loss_kill_r: float = 1.0,
        oversize_ratio: float = 1.1,
        max_loss_pct: float = HARD_MAX_LOSS_PCT,
        retries: int = 1,
        price_for: Callable[[str], float | None] | None = None,
        clock: Callable[[], float] = time.time,
        alert_cooldown_sec: float = 30.0,
        include_fees: bool = True,
    ):
        self.live = live
        self.info = info
        self.user = user
        self.dexs = tuple(dexs)
        self.journal = journal
        self.risk_pct = float(risk_pct)
        self.max_leverage = int(max_leverage)
        self.loss_kill_r = float(loss_kill_r)
        # Hard cap: no position may lose more than this share of the account
        # value seen when it opened. Never above 2%.
        self.max_loss_pct = min(float(max_loss_pct), HARD_MAX_LOSS_PCT)
        if self.max_loss_pct <= 0:
            raise ValueError("max_loss_pct must be > 0")
        self.risk_pct = min(self.risk_pct, self.max_loss_pct)
        self._open_account: dict[tuple, float] = {}
        self._last_account: float | None = None
        self.oversize_ratio = float(oversize_ratio)
        self.retries = max(0, int(retries))
        self.price_for = price_for
        self.clock = clock
        self.alert_cooldown_sec = float(alert_cooldown_sec)
        self._plans: dict[str, Plan] = {}
        self._own: dict[str, set] = {}
        self._fallback: dict[tuple, float] = {}
        self._last_alert: dict[tuple, float] = {}
        # (coin, side) -> clock time of a pass whose stop placement failed.
        # The next pass retries once; a second failure market-closes.
        self._stop_failed: dict[tuple, float] = {}
        # (coin, side) -> trigger of a NAKED_STOP_PLACED stop not yet seen
        # resting in a snapshot. The coin stays unprotected until it is.
        self._naked_pending: dict[tuple, float] = {}
        self._plan_lock = threading.Lock()
        self._pass_lock = threading.Lock()
        self.unprotected: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # The 2% cap counts the maker entry and the taker exit fee.
        self.include_fees = bool(include_fees)
        # Clock time of the last pass that read the account and checked
        # every position. The hunt sends nothing new while it is stale.
        self.last_ok_at: float | None = None

    # ---- plans (set by the hunt) -------------------------------------
    def set_plans(self, plans: dict[str, Plan]) -> None:
        with self._plan_lock:
            self._plans = {canon_coin(k): v for k, v in plans.items()}

    def plan(self, coin: str) -> Plan | None:
        with self._plan_lock:
            return self._plans.get(canon_coin(coin))

    def own_oids(self, coin: str) -> set:
        return set(self._own.get(canon_coin(coin), set()))

    @property
    def threaded(self) -> bool:
        return self._thread is not None

    def stale(self, now: float, max_age_sec: float) -> bool:
        """True when no verified pass happened within ``max_age_sec`` of ``now``."""
        if self.last_ok_at is None:
            return True
        return float(now) - float(self.last_ok_at) > float(max_age_sec)

    def kick(self) -> None:
        """Wake the guard thread now (a fill or a new ticket just happened)."""
        self._wake.set()

    # ---- thread -------------------------------------------------------
    def start(self, every_sec: float) -> None:
        if every_sec <= 0 or self._thread is not None:
            return

        def _run():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    logger.exception("MODEL_B GUARD pass failed")
                self._wake.wait(every_sec)
                self._wake.clear()

        self._thread = threading.Thread(target=_run, name="model-b-guard", daemon=True)
        self._thread.start()
        logger.info("MODEL_B GUARD started every=%.1fs", every_sec)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---- one pass -----------------------------------------------------
    def run_once(
        self,
        snapshot: AccountSnapshot | None = None,
        spot: float | None = None,
    ) -> list[dict]:
        with self._pass_lock:
            return self._run(snapshot, spot)

    def _run(self, snapshot, spot) -> list[dict]:
        events: list[dict] = []
        if snapshot is None:
            try:
                snapshot = self.info.load_account_snapshot(
                    self.user,
                    self.dexs,
                    include_fills=False,
                    orders_when_positions_only=True,
                )
            except TypeError:
                snapshot = self.info.load_account_snapshot(self.user, self.dexs)
            except Exception:
                logger.exception("MODEL_B GUARD snapshot failed")
                return events
        if snapshot is None or not snapshot.ok:
            logger.warning("MODEL_B GUARD snapshot not ok; cannot verify stops this pass")
            return events
        live_positions = [pos for pos in snapshot.positions if pos.size > 0]
        open_coins = {pos.coin for pos in live_positions}
        # Our own stop / TP triggers on a coin that is now flat are pulled so
        # an old trigger can never act on a later position.
        open_keys = {(pos.coin, pos.side) for pos in live_positions}
        for key in list(self._open_account):
            if key not in open_keys:
                self._open_account.pop(key, None)
        for book in (self._stop_failed, self._naked_pending):
            for key in list(book):
                if key not in open_keys:
                    book.pop(key, None)
        for coin in list(self._own):
            if coin in open_coins:
                continue
            for oid in list(self._own.get(coin, ())):
                try:
                    self.live.cancel_order(coin, oid)
                except Exception:
                    pass
            self._own.pop(coin, None)
        if not live_positions:
            self.unprotected.clear()
            self.last_ok_at = float(self.clock())
            return events
        if spot is None:
            try:
                spot = self.info.spot_usdc_balance(self.user)
            except Exception:
                spot = None
        account = float(spot) if spot is not None and float(spot) > 0 else None
        if account is not None:
            self._last_account = account
        else:
            account = self._last_account
        still_naked: set[str] = set()
        for pos in live_positions:
            try:
                ok = self._protect(pos, snapshot, account, events)
            except Exception:
                logger.exception("MODEL_B GUARD failed on %s", pos.coin)
                ok = False
            if not ok:
                still_naked.add(pos.coin)
        self.unprotected = still_naked
        self.last_ok_at = float(self.clock())
        return events

    # ---- helpers ------------------------------------------------------
    def _mark(self, pos: PerpPosition) -> float:
        if pos.mark and pos.mark > 0:
            return float(pos.mark)
        if self.price_for is not None:
            try:
                px = self.price_for(pos.coin)
            except Exception:
                px = None
            if px is not None and px > 0:
                return float(px)
        return float(pos.entry)

    def _alert(self, kind: str, coin: str, events: list[dict], **fields) -> None:
        now = float(self.clock())
        text = " ".join(f"{k}={v}" for k, v in fields.items())
        logger.warning("MODEL_B %s %s %s", kind, coin, text)
        key = (kind, coin)
        if now - self._last_alert.get(key, -1e18) >= self.alert_cooldown_sec:
            self._last_alert[key] = now
            logger.warning("MODEL_B ALERT kind=%s coin=%s %s", kind, coin, text)
        row = {"kind": kind, "coin": coin, **fields}
        events.append(row)
        if self.journal is not None:
            try:
                self.journal.log("model_b_alert", entry_mode="model_b", **row)
            except Exception:
                logger.exception("MODEL_B GUARD journal failed")

    def _reduce(self, coin: str, side: str, size: float, mark: float) -> tuple[bool, str]:
        """Reduce-only IOC on the closing side. Returns (ok, detail)."""
        is_buy = side == "short"
        try:
            if hasattr(self.live, "reduce_only_ioc"):
                resp = self.live.reduce_only_ioc(coin, is_buy, size, mark)
            else:
                resp = self.live.market_close(coin, size=size)
        except Exception as exc:
            logger.exception("MODEL_B GUARD reduce-only failed for %s", coin)
            return False, str(exc)
        err = _resp_error(resp)
        return err is None, err or "ok"

    def _cancel_entries(self, coin: str, snapshot: AccountSnapshot) -> None:
        for order in snapshot.entry_orders:
            if order.coin != coin or order.oid is None:
                continue
            try:
                self.live.cancel_order(coin, order.oid)
                logger.warning("MODEL_B GUARD cancel entry %s oid=%s (position killed)", coin, order.oid)
            except Exception:
                logger.exception("MODEL_B GUARD entry cancel failed for %s", coin)

    def _close(self, kind, pos, mark, snapshot, events, **fields) -> bool:
        ok, detail = self._reduce(pos.coin, pos.side, pos.size, mark)
        self._alert(
            kind,
            pos.coin,
            events,
            side=pos.side,
            size=pos.size,
            mark=mark,
            result="closed" if ok else "CLOSE_FAILED",
            detail=detail,
            **fields,
        )
        self._cancel_entries(pos.coin, snapshot)
        return ok

    def loss_cap(self, pos: PerpPosition, account: float | None) -> float | None:
        """Dollar cap for this position: max_loss_pct x account at open."""
        key = (pos.coin, pos.side)
        if key not in self._open_account and account is not None and account > 0:
            self._open_account[key] = float(account)
        base = self._open_account.get(key)
        if base is None:
            return None
        return self.max_loss_pct * base

    def _fees(self, pos: PerpPosition, exit_px: float) -> float:
        """Maker entry + taker exit fee for the whole position (0 when off)."""
        if not self.include_fees or pos.size <= 0:
            return 0.0
        return pos.size * (abs(pos.entry) * MAKER_FEE_RATE + abs(float(exit_px)) * TAKER_FEE_RATE)

    def _cap_stop(self, pos: PerpPosition, cap: float) -> float:
        """Trigger where the position has lost exactly ``cap``, rounded tighter.

        With fees on, the entry and exit fee come out of ``cap`` first, so
        the realized loss at the trigger (fees included) is still <= cap.
        """
        per = cap / pos.size
        if self.include_fees:
            # Solve size x (dist + entry x maker + exit x taker) = cap with
            # exit = entry -/+ dist: the same fee model the engine sizes with.
            fixed = abs(pos.entry) * (MAKER_FEE_RATE + TAKER_FEE_RATE)
            slope = 1.0 - TAKER_FEE_RATE if pos.side == "long" else 1.0 + TAKER_FEE_RATE
            dist = max(0.0, per - fixed) / slope
        else:
            dist = per
        px = pos.entry - dist if pos.side == "long" else pos.entry + dist
        px = max(px, pos.entry * 1e-6)
        rnd = getattr(self.live, "round_price_toward", None)
        if callable(rnd):
            try:
                px = float(rnd(pos.coin, px, up=pos.side == "long"))
            except Exception:
                pass
        return px

    def _loss_at(self, pos: PerpPosition, stop: float) -> float:
        """Realized loss in $ if the full position stops out at ``stop``."""
        sign = 1.0 if pos.side == "long" else -1.0
        return max(0.0, (pos.entry - stop) * sign * pos.size) + self._fees(pos, stop)

    def _fallback_stop(self, pos: PerpPosition, account: float | None) -> float:
        key = (pos.coin, pos.side, round(pos.entry, 10), round(pos.size, 10))
        if key in self._fallback:
            return self._fallback[key]
        base = self._open_account.get((pos.coin, pos.side), account)
        if base is not None and pos.size > 0:
            dist = self.risk_pct * base / pos.size
        else:
            # Balance unknown: a placeholder only. It is not cached, the coin
            # stays "unprotected" (no new entries), and the 2% cap stop
            # replaces it on the first pass that can read the balance.
            dist = pos.entry * 0.01
        stop = pos.entry - dist if pos.side == "long" else pos.entry + dist
        stop = max(stop, pos.entry * 1e-6)
        if base is not None:
            self._fallback[key] = stop
        return stop

    def _place(self, kind: str, coin: str, side: str, size: float, trigger: float) -> object | None | bool:
        """Place a reduce-only trigger with retries. Returns oid (or True) / False."""
        is_buy = side == "short"
        fn = getattr(self.live, "set_stop_loss" if kind == "sl" else "set_take_profit", None)
        if fn is None:
            return False
        for attempt in range(self.retries + 1):
            try:
                resp = fn(coin, is_buy=is_buy, size=size, trigger_px=trigger)
            except Exception as exc:
                logger.warning(
                    "MODEL_B GUARD %s place failed %s try=%s: %s", kind, coin, attempt + 1, exc
                )
                continue
            err = _resp_error(resp)
            if err is None:
                oid = _resp_oid(resp)
                if oid is not None:
                    self._own.setdefault(coin, set()).add(oid)
                return oid if oid is not None else True
            logger.warning(
                "MODEL_B GUARD %s rejected %s try=%s: %s", kind, coin, attempt + 1, err
            )
        return False

    def _cover(
        self,
        kind: str,
        pos: PerpPosition,
        size: float,
        trigger: float,
        orders: list[ProtectiveOrder],
        *,
        match_trigger: bool = False,
    ) -> tuple[bool, bool]:
        """Make ``kind`` cover ``size``. Returns (covered, placed_now).

        ``match_trigger`` counts and cancels only orders at this price, so
        topping up TP1 does not treat the runner as cover and does not
        cancel it.
        """
        mine = [o for o in orders if o.kind == kind and o.protects(pos.side)]
        if match_trigger:
            tol = max(abs(float(trigger)) * 1e-6, 1e-9)
            mine = [o for o in mine if abs(float(o.trigger_px) - float(trigger)) <= tol]
        if any(o.full_position for o in mine):
            return True, False
        covered = sum(o.size for o in mine)
        if covered + max(_SIZE_TOL, size * 1e-4) >= size:
            return True, False
        own = self._own.get(pos.coin, set())
        others = sum(o.size for o in mine if o.oid not in own)
        need = max(0.0, size - others)
        logger.info(
            "MODEL_B GUARD %s %s %s covered=%.8g size=%.8g placing=%.8g @ %s",
            "STOP" if kind == "sl" else "TP",
            pos.coin,
            pos.side,
            covered,
            size,
            need,
            trigger,
        )
        placed = self._place(kind, pos.coin, pos.side, need, trigger)
        if placed is False:
            return False, False
        # Our older, now-undersized triggers go once the new one rests.
        for o in mine:
            if o.oid in own and o.oid != placed:
                try:
                    self.live.cancel_order(pos.coin, o.oid)
                    own.discard(o.oid)
                except Exception:
                    logger.exception("MODEL_B GUARD old %s cancel failed %s", kind, pos.coin)
        return True, True

    def _protect(self, pos: PerpPosition, snapshot, account, events) -> bool:
        coin = pos.coin
        plan = self.plan(coin)
        if plan is not None and plan.side != pos.side:
            plan = None
        mark = self._mark(pos)
        sign = 1.0 if pos.side == "long" else -1.0
        upnl = (
            float(pos.unrealized_pnl)
            if pos.unrealized_pnl is not None
            else (mark - pos.entry) * pos.size * sign
        )

        # 1. Loss kill at min(loss_kill_r x planned risk, hard cap).
        hard_cap = self.loss_cap(pos, account)
        planned = plan.planned_risk if plan is not None and plan.planned_risk > 0 else None
        limits = []
        if planned is not None:
            limits.append((self.loss_kill_r * planned, "planned_risk"))
        if hard_cap is not None:
            # Price loss allowed once both fees are paid: the realized loss
            # (net of fees) is what Chris's 2% rule and the board measure.
            limits.append(
                (
                    max(0.0, hard_cap - self._fees(pos, mark)),
                    "hard_cap_%.2fpct%s"
                    % (self.max_loss_pct * 100, "_net_fees" if self.include_fees else ""),
                )
            )
        if limits:
            limit, basis = min(limits, key=lambda t: t[0])
            if upnl < 0 and -upnl >= limit - 1e-12:
                return self._close(
                    "LOSS_KILL",
                    pos,
                    mark,
                    snapshot,
                    events,
                    upnl=round(upnl, 6),
                    limit=round(limit, 6),
                    basis=basis,
                ) and False

        # 2. Oversize cut.
        size = float(pos.size)
        target: float | None = None
        why = []
        # Leverage against the balance seen at open: a loss elsewhere that
        # shrinks spot must not turn a valid ticket into an "oversize" one.
        lev_base = self._open_account.get((pos.coin, pos.side), account)
        if lev_base is not None and lev_base > 0 and mark > 0 and self.max_leverage > 0:
            lev_size = self.max_leverage * lev_base / mark
            if size > lev_size * (1 + 1e-6):
                target = lev_size
                why.append(f"lev={size * mark / lev_base:.1f}x>{self.max_leverage}x")
        if plan is not None and plan.intended_size > 0 and size > self.oversize_ratio * plan.intended_size:
            target = plan.intended_size if target is None else min(target, plan.intended_size)
            why.append(f"size>{self.oversize_ratio}x_ticket")
        if target is not None and size - target > _SIZE_TOL:
            cut = size - target
            ok, detail = self._reduce(coin, pos.side, cut, mark)
            self._alert(
                "OVERSIZE_CUT",
                coin,
                events,
                side=pos.side,
                size=size,
                cut=round(cut, 8),
                target=round(target, 8),
                why=",".join(why),
                result="cut" if ok else "CUT_FAILED",
                detail=detail,
            )
            if ok:
                size = target

        # 3. Stop.
        orders = snapshot.protection(coin)
        planned_stop = plan.stop if plan is not None and plan.stop > 0 else None
        stop = planned_stop if planned_stop is not None else self._fallback_stop(pos, account)
        cap_px: float | None = None
        if hard_cap is not None and pos.size > 0:
            cap_px = self._cap_stop(pos, hard_cap)
            # A ticket sized exactly to the cap ties here; float noise must
            # not nudge the planned stop a tick tighter.
            eps = abs(stop) * 1e-9
            if (pos.side == "long" and cap_px > stop + eps) or (
                pos.side == "short" and cap_px < stop - eps
            ):
                stop = cap_px
            # A resting stop past the cap price does not count as protection.
            orders = [
                o
                for o in orders
                if o.kind != "sl"
                or not o.protects(pos.side)
                or (pos.side == "long" and o.trigger_px >= cap_px * (1 - 1e-9))
                or (pos.side == "short" and o.trigger_px <= cap_px * (1 + 1e-9))
            ]
        def _past(px: float) -> bool:
            return (pos.side == "long" and mark <= px) or (pos.side == "short" and mark >= px)

        naked_why: str | None = None
        if planned_stop is not None and _past(stop) and cap_px is not None and not _past(cap_px):
            # Price is past the planned stop but the 2% level is still ahead:
            # protect at the 2% level instead of closing.
            stop = cap_px
            naked_why = "planned_stop_passed"
        # Our own stop looser than the target (a fallback placed before the
        # plan reached the guard, or an older plan) does not count either;
        # it is replaced at the target and then pulled.
        own = self._own.get(coin, set())
        tol = abs(stop) * 1e-4
        loose_own = [
            o
            for o in orders
            if o.kind == "sl"
            and o.protects(pos.side)
            and o.oid in own
            and (
                (pos.side == "long" and o.trigger_px < stop - tol)
                or (pos.side == "short" and o.trigger_px > stop + tol)
            )
        ]
        if loose_own:
            loose_ids = {o.oid for o in loose_own}
            orders = [o for o in orders if o.oid not in loose_ids]
        stops_now = [o for o in orders if o.kind == "sl" and o.protects(pos.side)]
        covered_already = any(o.full_position for o in stops_now) or (
            sum(o.size for o in stops_now) + max(_SIZE_TOL, size * 1e-4) >= size
        )
        key = (coin, pos.side)
        if covered_already:
            # A stop rests for the full size: anything NAKED_STOP_PLACED put
            # there is now confirmed, and a past failure no longer counts.
            self._naked_pending.pop(key, None)
            self._stop_failed.pop(key, None)

        naked = not stops_now
        if _past(stop) and (planned_stop is not None or not covered_already):
            # Past the 2% level (or the balance is unknown): the 2% absolute
            # rule wins.
            self._stop_failed.pop(key, None)
            self._naked_pending.pop(key, None)
            return self._close(
                "NAKED_CLOSE",
                pos,
                mark,
                snapshot,
                events,
                reason="past_stop",
                stop=stop,
                cap_stop=cap_px,
            ) and False
        if naked and naked_why is None and planned_stop is None:
            naked_why = "no_plan"
        ok, placed = self._cover("sl", pos, size, stop, orders)
        if not ok:
            if key in self._stop_failed:
                # Second pass in a row that could not rest a stop: close.
                self._stop_failed.pop(key, None)
                self._naked_pending.pop(key, None)
                self._close(
                    "NAKED_CLOSE", pos, mark, snapshot, events, reason="stop_reject", stop=stop
                )
                return False
            self._stop_failed[key] = float(self.clock())
            logger.warning(
                "MODEL_B GUARD STOP_RETRY %s %s size=%s stop=%s: stop not accepted; "
                "entries blocked, retrying next pass, market close if it fails again",
                coin,
                pos.side,
                size,
                stop,
            )
            return False
        if placed:
            self._stop_failed.pop(key, None)
        if placed and naked and naked_why is not None and cap_px is not None:
            loss = self._loss_at(pos, stop)
            base = self._open_account.get(key, account)
            pct = (loss / base * 100.0) if base else float("nan")
            self._alert(
                "NAKED_STOP_PLACED",
                coin,
                events,
                side=pos.side,
                size=size,
                stop=stop,
                loss_at_stop="%.2f%%" % pct,
                why=naked_why,
            )
            logger.warning(
                "MODEL_B ALERT NAKED_STOP_PLACED coin=%s size=%s stop=%s loss_at_stop=%.2f%%",
                coin,
                size,
                stop,
                pct,
            )
            self._naked_pending[key] = stop
        for o in loose_own:
            try:
                self.live.cancel_order(coin, o.oid)
                own.discard(o.oid)
                logger.info(
                    "MODEL_B GUARD STOP %s replaced loose oid=%s trigger=%s -> %s",
                    coin,
                    o.oid,
                    o.trigger_px,
                    stop,
                )
            except Exception:
                logger.exception("MODEL_B GUARD loose stop cancel failed %s", coin)

        # 4. Take profit (plan only; a failure is not a close).
        # A runner has two triggers. Each is covered on its own so a top-up
        # at TP1 cannot cancel the runner or re-cover the full size there.
        if plan is not None and plan.take_profit > 0:
            split = plan.tp1_size > 0 and plan.runner_size > 0 and plan.runner_tp > 0
            if split:
                ok1, _ = self._cover(
                    "tp", pos, plan.tp1_size, plan.take_profit, orders, match_trigger=True
                )
                ok2, _ = self._cover(
                    "tp", pos, plan.runner_size, plan.runner_tp, orders, match_trigger=True
                )
                tp_ok = ok1 and ok2
            else:
                tp_ok, _ = self._cover("tp", pos, size, plan.take_profit, orders)
            if not tp_ok:
                logger.warning("MODEL_B GUARD TP could not be placed for %s", coin)
        if plan is None and hard_cap is None:
            # No plan and no balance: the stop above is a placeholder, so this
            # coin does not count as protected until the 2% cap is known.
            logger.warning("MODEL_B GUARD %s stop is a placeholder (balance unknown)", coin)
            return False
        if key in self._naked_pending:
            # Placed by NAKED_STOP_PLACED but not yet seen resting: entries
            # stay blocked (UNPROTECTED_BOOK) until a snapshot confirms it.
            return False
        return True


def plans_from_book(book) -> dict[str, Plan]:
    """Plans for the guard: filled tickets first, then resting managed tickets."""
    plans: dict[str, Plan] = {}
    for order in book.working_orders():
        if getattr(order, "external", False) or order.stop <= 0:
            continue
        plans[canon_coin(order.coin)] = Plan(
            coin=canon_coin(order.coin),
            side=order.side,
            stop=float(order.stop),
            take_profit=float(order.take_profit),
            intended_size=float(getattr(order, "orig_size", 0.0) or order.size),
            planned_risk=float(getattr(order, "planned_risk", 0.0) or 0.0),
            source="ticket",
        )
    for pos in book.open_positions():
        if pos.adopted or pos.stop <= 0:
            continue
        intended = float(getattr(pos, "intended_size", 0.0) or 0.0)
        planned = float(getattr(pos, "planned_risk", 0.0) or 0.0)
        if planned <= 0 and intended > 0:
            # Arm risk, not the live stop: a breakeven trail must not
            # shrink the loss-kill budget to zero.
            basis = float(getattr(pos, "planned_stop", 0.0) or 0.0) or float(pos.stop)
            planned = intended * abs(pos.entry - basis)
        tp1 = float(getattr(pos, "tp1_size", 0.0) or 0.0)
        run_sz = float(getattr(pos, "runner_size", 0.0) or 0.0)
        run_px = float(getattr(pos, "runner_px", 0.0) or 0.0)
        split = (
            bool(getattr(pos, "runner_on", False))
            and not bool(getattr(pos, "tp1_filled", False))
            and tp1 > 0
            and run_sz > 0
            and run_px > 0
        )
        plans[canon_coin(pos.coin)] = Plan(
            coin=canon_coin(pos.coin),
            side=pos.side,
            stop=float(pos.stop),
            take_profit=float(pos.take_profit),
            intended_size=intended,
            planned_risk=planned,
            source="position",
            tp1_size=tp1 if split else 0.0,
            runner_tp=run_px if split else 0.0,
            runner_size=run_sz if split else 0.0,
        )
    return plans

"""One thesis per coin.

No average-down, no second Alo, no market fallback, no re-entry on the
same swing after a stale cancel or a stop. A new swing id may start a new
thesis once the coin is flat and no order is working. The maker rests
until that thesis is outdated. A clock timeout is off unless ``work_sec``
is set above zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hl_bot.strategy.model_b.risk import (
    collides_with_fill,
    heal_stop,
    locked_take_profit,
    place_stop,
    stop_is_valid,
    widen_stop_for_fill,
)
from hl_bot.strategy.model_b.types import AloIntent, TradePrint
from hl_bot.strategy.model_b.universe import canon_coin

# 0 = no maker timer. Cancel only when the thesis is stale.
WORK_SEC = 0.0

SECOND_ALO = "SECOND_ALO"
AVERAGE_DOWN = "AVERAGE_DOWN"
THESIS_DONE = "THESIS_DONE"


def _print_stales_order(order: WorkingOrder, print_: TradePrint) -> bool:
    """True when price traded through the sweep and did not fill the Alo."""
    if order.sweep_px is None or print_.side not in ("buy", "sell"):
        return False
    if order.side == "long":
        through = print_.price < order.sweep_px - 1e-9
        fills = print_.side == "sell" and print_.price <= order.limit_px + 1e-9
    else:
        through = print_.price > order.sweep_px + 1e-9
        fills = print_.side == "buy" and print_.price >= order.limit_px - 1e-9
    return through and not fills


@dataclass
class WorkingOrder:
    coin: str
    side: str
    limit_px: float
    size: float
    stop: float
    take_profit: float
    swing_id: str
    posted_at: float
    oid: object | None = None
    tif: str = "Alo"
    sweep_px: float | None = None
    tick: float = 1.0
    pool_px: float | None = None
    tp_r: float = 1.5
    # Arm score, kept so a closer-ticker cancel can compare it. Not an entry gate.
    score: int | None = None
    # Setup quality when MODEL_B_QUALITY_RANK is on. None keeps the integer
    # score comparison (flag 0, and orders posted before the field existed).
    quality: float | None = None
    # Leverage set on the exchange before this Alo. Adopted entries stay
    # at 20 so an old order is not counted as the coin's current max.
    leverage: int = 20
    # Adopted from the exchange after a restart. No sweep, so the thesis
    # timer and stale-print path must not invent a cancel. A user fill is
    # recorded as an adopted position instead of a stop at 0.
    external: bool = False
    # Size at post. A position grown from this order's drips is never
    # tracked above it (a snapshot plus a websocket copy of the same fill
    # double counted 0.08887 BTC on Oct 7).
    orig_size: float = 0.0
    # Dollar risk at the arm: size x |limit - stop|. The guard's loss kill
    # uses it.
    planned_risk: float = 0.0
    # Second target when MODEL_B_TP_RUNNER is on or shadow. ``off`` places
    # one full-size TP. The fill path turns ``on`` into a real split.
    runner_px: float | None = None
    runner_mode: str = "off"


@dataclass
class OpenPosition:
    coin: str
    side: str
    size: float
    entry: float
    stop: float
    take_profit: float
    swing_id: str
    opened_at: float
    stop_oid: object | None = None
    tp_oid: object | None = None
    # Set on each maker fill so the loop can resize brackets and log the remainder.
    remainder_kept: bool = False
    remainder_size: float = 0.0
    fill_added: float = 0.0
    just_opened: bool = False
    # Exchange position this process did not open. Stop and TP are unknown,
    # so a public price must not stop it out. Closes come from fills.
    adopted: bool = False
    # Exchange ``marginUsed`` when we have it. None uses notional / leverage.
    margin_used: float | None = None
    # Leverage this process set before the entry. Adopted positions keep
    # the exchange margin figure and do not assume the coin max.
    leverage: int = 20
    # Ticket this position came from: size posted and dollar risk at the
    # arm. 0 for an adopted position (unknown plan).
    intended_size: float = 0.0
    planned_risk: float = 0.0
    # Taker reduces (a stop drip, a guard cut, a manual trim) already done.
    # ``size`` is what is still open; the final close reports the sum.
    closed_size: float = 0.0
    closed_pnl: float = 0.0
    # Stop quantity. Updated on every fill and every partial reduce so a
    # position is never protected for a size it no longer has.
    stop_size: float = 0.0
    needs_stop_resize: bool = False
    # Runner (MODEL_B_TP_RUNNER=1). Shadow/off leave these empty and the
    # position keeps one full-size TP. ``planned_stop`` is the arm stop;
    # ``stop`` moves to breakeven and then the 1m trail after TP1.
    runner_px: float | None = None
    runner_on: bool = False
    runner_size: float = 0.0
    tp1_size: float = 0.0
    tp2_oid: object | None = None
    tp1_filled: bool = False
    planned_stop: float = 0.0
    armed_at: float = 0.0
    runner_event: str = ""
    # Price increment from the arm, so a fill-time re-pick and the trail
    # use the same tick the stop was built with.
    tick: float = 0.0
    # Shadow runner: log the trail, do not move the stop or split the TP.
    runner_shadow: bool = False


@dataclass
class CloseEvent:
    coin: str
    side: str
    size: float
    entry: float
    exit: float
    pnl: float
    reason: str
    swing_id: str
    # Resting Alo detached because the position itself closed.
    remainder: WorkingOrder | None = None


@dataclass
class _CoinState:
    working: WorkingOrder | None = None
    position: OpenPosition | None = None
    consumed: set[str] = field(default_factory=set)


class ThesisBook:
    def __init__(self, work_sec: float = WORK_SEC):
        self.work_sec = float(work_sec)
        self._coins: dict[str, _CoinState] = {}
        # Working orders cleared by flatten that had no position to hang on.
        self.flatten_cancels: list[WorkingOrder] = []
        # Exchange ``totalMarginUsed`` when it is larger than the sum of the
        # positions and entry orders this book is tracking.
        self.margin_floor: float = 0.0

    def _state(self, coin: str) -> _CoinState:
        coin = canon_coin(coin)
        st = self._coins.get(coin)
        if st is None:
            st = _CoinState()
            self._coins[coin] = st
        return st

    def block_reason(self, coin: str, swing_id: str) -> str | None:
        st = self._coins.get(canon_coin(coin))
        if st is None:
            return None
        if st.position is not None:
            return AVERAGE_DOWN
        if st.working is not None:
            return SECOND_ALO
        if swing_id in st.consumed:
            return THESIS_DONE
        return None

    def post(
        self,
        intent: AloIntent,
        now: float,
        oid: object | None = None,
        score: int | None = None,
        quality: float | None = None,
    ) -> WorkingOrder:
        if intent.tif != "Alo" or intent.market_fallback:
            raise ValueError("Model B is post-only Alo; market fallback is off")
        try:
            lev = int(intent.leverage)
        except (TypeError, ValueError):
            lev = 0
        if lev < 1:
            raise ValueError(f"leverage must be >= 1; got {intent.leverage}")
        reason = self.block_reason(intent.coin, intent.swing_id)
        if reason:
            raise ValueError(reason)
        order = WorkingOrder(
            coin=canon_coin(intent.coin),
            side=intent.side,
            limit_px=intent.limit_px,
            size=intent.size,
            stop=intent.stop,
            take_profit=intent.take_profit,
            swing_id=intent.swing_id,
            posted_at=float(now),
            oid=oid,
            tif="Alo",
            sweep_px=intent.sweep_px,
            tick=intent.tick if intent.tick > 0 else max(abs(intent.limit_px - intent.stop), 1e-12),
            pool_px=intent.pool_px,
            tp_r=intent.tp_r,
            score=None if score is None else int(score),
            quality=None if quality is None else float(quality),
            leverage=lev,
            orig_size=float(intent.size),
            planned_risk=float(intent.size) * abs(float(intent.limit_px) - float(intent.stop)),
            runner_px=getattr(intent, "runner_px", None),
            runner_mode=str(getattr(intent, "runner_mode", "off") or "off"),
        )
        self._state(intent.coin).working = order
        return order

    def expire(self, now: float) -> list[WorkingOrder]:
        """Cancel on the optional timer. ``work_sec <= 0`` never fires.

        The default is no timer. The swing is consumed when a timer is set
        and elapses, same as a stale cancel: that thesis is done.
        """
        if self.work_sec <= 0:
            return []
        cancelled: list[WorkingOrder] = []
        for st in self._coins.values():
            order = st.working
            if order is None or order.external:
                continue
            # A partial fill may already have a position. The timer still
            # drops only the resting remainder; the position stays.
            # An adopted exchange entry is not this process's thesis timer.
            if float(now) + 1e-9 >= order.posted_at + self.work_sec:
                st.consumed.add(order.swing_id)
                st.working = None
                cancelled.append(order)
        return cancelled

    def cancel_if_stale(self, coin: str, prints: list[TradePrint]) -> list[WorkingOrder]:
        """Cancel a resting Alo whose sweep extreme has been printed through.

        A long is stale when a later print trades strictly below the swept
        low and does not fill the bid. A short is stale when a later print
        trades strictly above the swept high and does not fill the ask.
        A print that fills the resting order is not a cancel. The swing is
        consumed either way once cancelled.
        """
        st = self._coins.get(canon_coin(coin))
        if st is None or st.working is None:
            return []
        # A partial fill keeps the position. Stale still cancels the
        # unfilled remainder and does not flatten the filled size.
        order = st.working
        if order.sweep_px is None:
            return []
        for print_ in sorted(prints, key=lambda p: (p.ts, p.seq)):
            if canon_coin(print_.coin) != order.coin or print_.ts + 1e-9 < order.posted_at:
                continue
            if _print_stales_order(order, print_):
                st.consumed.add(order.swing_id)
                st.working = None
                return [order]
        return []

    def adopt_position(
        self,
        *,
        coin: str,
        side: str,
        size: float,
        entry: float,
        now: float,
        margin_used: float | None = None,
    ) -> OpenPosition | None:
        """Record a position the exchange already has.

        A position this process opened keeps its stop and TP. An adopted
        one is refreshed from the exchange (size, entry, margin) so a
        restart does not size the next ticket off the full spot balance.
        """
        if side not in ("long", "short") or size <= 0 or entry <= 0:
            return None
        st = self._state(coin)
        if st.position is not None and not st.position.adopted:
            # The exchange size is the truth. Drips counted twice (snapshot
            # and websocket) are corrected here every pass. Stop / TP stay.
            pos = st.position
            if pos.side == side:
                pos.size = float(size)
                pos.entry = float(entry)
                if margin_used is not None and float(margin_used) > 0:
                    pos.margin_used = float(margin_used)
            return pos
        if st.position is not None:
            pos = st.position
            pos.size = float(size)
            pos.entry = float(entry)
            pos.side = side
            if margin_used is not None and float(margin_used) > 0:
                pos.margin_used = float(margin_used)
            return pos
        pos = OpenPosition(
            coin=canon_coin(coin),
            side=side,
            size=float(size),
            entry=float(entry),
            stop=0.0,
            take_profit=0.0,
            swing_id=f"adopted:{canon_coin(coin)}",
            opened_at=float(now),
            adopted=True,
            margin_used=None if margin_used is None or float(margin_used) <= 0 else float(margin_used),
        )
        st.position = pos
        return pos

    def claim_fill(
        self,
        *,
        coin: str,
        side: str,
        size: float,
        entry: float,
        now: float,
        margin_used: float | None = None,
    ) -> OpenPosition | None:
        """An exchange position that is a fill of this process's own Alo.

        Oct 7 23:02: the snapshot saw the BTC fill before the websocket
        delivered it, so the position was "adopted" with stop 0 / TP 0 and
        brackets were skipped. Here the resting ticket's stop and TP are
        kept (same path as a websocket fill) and the size is the exchange
        size. Returns ``None`` when there is no matching managed ticket.
        """
        st = self._coins.get(canon_coin(coin))
        if st is None or st.position is not None or st.working is None:
            return None
        order = st.working
        if order.external or order.side != side or size <= 0 or entry <= 0:
            return None
        pos = self._fill(order, float(entry), float(now), size=float(size))
        pos.size = float(size)
        pos.entry = float(entry)
        if margin_used is not None and float(margin_used) > 0:
            pos.margin_used = float(margin_used)
        return pos

    def drop_filled_entry(self, coin: str) -> WorkingOrder | None:
        """Forget a managed resting entry the exchange no longer has while a
        position is open (it filled out, or was cancelled). No exchange call."""
        st = self._coins.get(canon_coin(coin))
        if st is None or st.working is None or st.position is None:
            return None
        order = st.working
        st.working = None
        return order

    def adopt_entry(
        self,
        *,
        coin: str,
        side: str,
        limit_px: float,
        size: float,
        now: float,
        oid: object | None = None,
    ) -> WorkingOrder | None:
        """Block a second entry on a resting order this process did not post.

        ``sweep_px`` stays empty so a public print cannot stale-cancel it.
        The oid is the exchange id, so a later closer-cancel can pull it.
        """
        if side not in ("long", "short") or limit_px <= 0 or size <= 0:
            return None
        st = self._state(coin)
        if st.working is not None:
            return None
        order = WorkingOrder(
            coin=canon_coin(coin),
            side=side,
            limit_px=float(limit_px),
            size=float(size),
            stop=float(limit_px),
            take_profit=float(limit_px),
            swing_id=f"external:{canon_coin(coin)}",
            posted_at=float(now),
            oid=oid,
            sweep_px=None,
            external=True,
        )
        st.working = order
        return order

    def drop_external(self, coin: str) -> WorkingOrder | None:
        """Drop an adopted entry that is gone from the exchange. No thesis consume."""
        st = self._coins.get(canon_coin(coin))
        if st is None or st.working is None or not st.working.external:
            return None
        order = st.working
        st.working = None
        return order

    def working(self, coin: str) -> WorkingOrder | None:
        st = self._coins.get(canon_coin(coin))
        return None if st is None else st.working

    def resting_orders(self) -> list[WorkingOrder]:
        """Fully unfilled Alos. A coin with any fill is left out.

        The remainder after a drip stays working until the thesis is stale,
        but it is not offered to a closer coin. Cancelling it would wipe
        the brackets on the open size.
        """
        resting: list[WorkingOrder] = []
        for st in self._coins.values():
            if st.working is None or st.position is not None:
                continue
            resting.append(st.working)
        return resting

    def working_orders(self) -> list[WorkingOrder]:
        """Every resting Alo, including a remainder beside an open position."""
        orders: list[WorkingOrder] = []
        for st in self._coins.values():
            if st.working is not None:
                orders.append(st.working)
        return orders

    def open_positions(self) -> list[OpenPosition]:
        positions: list[OpenPosition] = []
        for st in self._coins.values():
            if st.position is not None:
                positions.append(st.position)
        return positions

    def release_for_closer(self, coin: str) -> WorkingOrder | None:
        """Cancel one unfilled Alo so a closer coin can use the margin.

        An open position is not released: its reduce-only brackets stay.
        The swing is consumed so this thesis does not immediately repost
        and take the margin back.
        """
        st = self._coins.get(canon_coin(coin))
        if st is None or st.working is None or st.position is not None:
            return None
        order = st.working
        st.consumed.add(order.swing_id)
        st.working = None
        return order

    def position(self, coin: str) -> OpenPosition | None:
        st = self._coins.get(canon_coin(coin))
        return None if st is None else st.position

    def _fill(
        self,
        order: WorkingOrder,
        price: float,
        ts: float,
        size: float | None = None,
    ) -> OpenPosition:
        st = self._state(order.coin)
        # Each user fill ``sz`` is that drip, not the cumulative position.
        # A missing size is a full fill (paper prints). A drip smaller than
        # the resting size keeps the maker working. Nothing here cancels it.
        if size is None or float(size) <= 0:
            added = float(order.size)
        else:
            added = min(float(size), float(order.size))
        dust = max(1e-12, abs(order.size) * 1e-8)
        remainder_kept = added < float(order.size) - dust
        if remainder_kept:
            order.size = float(order.size) - added
            remainder = float(order.size)
        else:
            added = float(order.size) if added <= 0 else added
            remainder = 0.0
            st.working = None
            order.size = 0.0
        just_opened = st.position is None
        if just_opened:
            stop = widen_stop_for_fill(
                order.side, price, order.stop, order.limit_px, order.tick, tp_r=order.tp_r
            )
            # Keep the arm stop. A fill-only recompute must not pull it back
            # to the fill. Replace only a stop that is not past the fill.
            if not stop_is_valid(order.side, price, stop) or collides_with_fill(
                price, stop, order.tick
            ):
                pushed = place_stop(
                    order.side,
                    price,
                    price,
                    order.tick,
                    tp_r=order.tp_r,
                    clear_wick_room=False,
                )
                if (
                    pushed is not None
                    and stop_is_valid(order.side, price, pushed)
                    and not collides_with_fill(price, pushed, order.tick)
                ):
                    stop = pushed
            # The arm's liquidity level wins over a 2R price stored on the
            # order. A tight stop must not pull this back to 2R.
            tp = locked_take_profit(
                order.side,
                price,
                order.take_profit,
                order.pool_px,
                stop,
                tp_r=order.tp_r,
            )
            pos = OpenPosition(
                coin=order.coin,
                side=order.side,
                size=added,
                entry=price,
                stop=stop,
                take_profit=tp,
                swing_id=order.swing_id,
                opened_at=ts,
                leverage=int(getattr(order, "leverage", 20) or 20),
                intended_size=float(order.orig_size or 0.0),
                planned_risk=float(order.planned_risk or 0.0),
                runner_px=getattr(order, "runner_px", None),
                runner_on=str(getattr(order, "runner_mode", "off") or "off") == "on",
                planned_stop=float(stop),
                armed_at=float(order.posted_at),
                tick=float(order.tick or 0.0),
            )
            st.position = pos
        else:
            pos = st.position
            assert pos is not None
            grown = pos.size + added
            cap = float(getattr(order, "orig_size", 0.0) or 0.0)
            if cap > 0 and pos.swing_id == order.swing_id and grown > cap:
                # Never track more than the ticket. The snapshot sync sets
                # the exchange size every pass anyway.
                added = max(0.0, cap - pos.size)
                grown = pos.size + added
            if grown > 0:
                pos.entry = (pos.entry * pos.size + price * added) / grown
            pos.size = grown
            # A drip changes size only. Recomputing R here is what put a
            # liquidity TP back at 2R on every partial fill.
        pos.remainder_kept = remainder_kept
        pos.remainder_size = remainder
        pos.fill_added = added
        pos.just_opened = just_opened
        pos.stop_size = float(pos.size)
        if not just_opened:
            pos.needs_stop_resize = True
        return pos

    def _fill_external(
        self,
        order: WorkingOrder,
        price: float,
        ts: float,
        size: float | None = None,
    ) -> OpenPosition:
        """A fill on an order adopted from the exchange.

        Stop and TP are not invented. The position stays ``adopted`` so a
        later public price cannot stop it out between the real brackets.
        """
        st = self._state(order.coin)
        if size is None or float(size) <= 0:
            added = float(order.size)
        else:
            added = min(float(size), float(order.size))
        dust = max(1e-12, abs(order.size) * 1e-8)
        remainder_kept = added < float(order.size) - dust
        if remainder_kept:
            order.size = float(order.size) - added
            remainder = float(order.size)
        else:
            remainder = 0.0
            st.working = None
        just_opened = st.position is None
        if just_opened:
            pos = OpenPosition(
                coin=order.coin,
                side=order.side,
                size=added,
                entry=price,
                stop=0.0,
                take_profit=0.0,
                swing_id=order.swing_id,
                opened_at=ts,
                adopted=True,
                leverage=int(getattr(order, "leverage", 20) or 20),
            )
            st.position = pos
        else:
            pos = st.position
            assert pos is not None
            pos.entry = (pos.entry * pos.size + price * added) / (pos.size + added)
            pos.size = pos.size + added
        pos.remainder_kept = remainder_kept
        pos.remainder_size = remainder
        pos.fill_added = added
        pos.just_opened = just_opened
        return pos

    def try_fill_from_prints(
        self, prints: list[TradePrint], *, partial: bool = False
    ) -> OpenPosition | None:
        """Paper fill. A resting buy fills on a later sell at or through the bid.

        A resting sell fills on a later buy at or through the ask. Prints
        from before the order was posted do not fill it. A print with no
        aggressor side does not fill (fail closed). This is not a market order.
        """
        if not prints:
            return None
        coin = canon_coin(prints[0].coin)
        st = self._coins.get(coin)
        if st is None or st.working is None:
            return None
        if st.position is not None and not partial:
            return None
        order = st.working
        # An adopted exchange order is not filled from the public tape.
        if order.external:
            return None
        for print_ in sorted(prints, key=lambda p: (p.ts, p.seq)):
            if print_.coin != coin or print_.ts + 1e-9 < order.posted_at:
                continue
            if print_.side not in ("buy", "sell"):
                continue
            if (
                order.side == "long"
                and print_.side == "sell"
                and print_.price <= order.limit_px + 1e-9
            ):
                if partial:
                    return self._fill(order, order.limit_px, print_.ts, size=float(print_.size))
                return self._fill(order, order.limit_px, print_.ts)
            if (
                order.side == "short"
                and print_.side == "buy"
                and print_.price >= order.limit_px - 1e-9
            ):
                if partial:
                    return self._fill(order, order.limit_px, print_.ts, size=float(print_.size))
                return self._fill(order, order.limit_px, print_.ts)
        return None

    def cancel_if_target_traded(self, coin: str, price: float) -> list[WorkingOrder]:
        """Cancel a resting Alo once price trades the target before it fills.

        The limit is at the level. The target is the opposite level. A print
        that reaches the target without trading the limit will not fill, so
        the ticket is stale.
        """
        st = self._coins.get(canon_coin(coin))
        if st is None or st.working is None or price <= 0:
            return []
        order = st.working
        if order.external:
            return []
        if order.side == "long":
            hit = price >= float(order.take_profit) - 1e-9 and price > float(order.limit_px) + 1e-9
        else:
            hit = price <= float(order.take_profit) + 1e-9 and price < float(order.limit_px) - 1e-9
        if not hit:
            return []
        st.consumed.add(order.swing_id)
        st.working = None
        return [order]

    def paper_tp1(self, coin: str, price: float, *, runner_frac: float = 0.5) -> OpenPosition | None:
        """Paper partial at TP1 when a runner is on. Stop moves to the fill.

        The stop quantity is the size still open. A full exit is left to
        ``try_exit`` when the runner is off or TP1 already filled.
        """
        st = self._coins.get(canon_coin(coin))
        if st is None or st.position is None or price <= 0:
            return None
        pos = st.position
        if not pos.runner_on or pos.tp1_filled or pos.adopted or not pos.runner_px:
            return None
        frac = float(runner_frac)
        part = pos.size * (1.0 - frac)
        if part <= 0 or part >= pos.size:
            return None
        if not self._take_runner_tp1(pos, price, part):
            return None
        return pos

    def apply_user_fill(
        self,
        *,
        coin: str,
        oid: object | None,
        price: float,
        ts: float,
        crossed: bool | None,
        size: float | None = None,
        direction: str | None = None,
    ) -> OpenPosition | CloseEvent | None:
        """Live fills.

        ``crossed is False`` is a maker Alo fill. The first drip opens the
        position at that size and leaves any unfilled remainder working.
        Later drips on the same order grow the position.         ``crossed is True``
        while a position is open is the resting stop/TP bracket firing — it
        closes, it does not add. A print between the stop and the target
        still flats the book: the pending remainder is dropped with the
        position, so its margin does not wait for the thesis to go stale.
        A taker fill with no position is ignored (no market entry).
        Unknown ``crossed`` is ignored.

        ``direction`` (the fill's "Open Long" / "Close Long" / ...) refines a
        taker fill: an "Open ..." fill adds exposure and is not a close; a
        "Close ..." fill smaller than the open size is a partial reduce (a
        stop dripping, the guard's oversize cut, a manual trim). The book
        keeps the rest open with its stop and TP, and the final close
        reports the whole size and PnL. Without ``direction`` every taker
        fill is still treated as a full close (old behaviour).
        """
        st = self._coins.get(canon_coin(coin))
        if crossed is True:
            if st is None or st.position is None:
                return None
            d = (direction or "").strip().lower()
            if d.startswith("open"):
                return None
            pos = st.position
            if self._take_runner_tp1(pos, price, size):
                return pos
            dust = max(1e-12, abs(pos.size) * 1e-6)
            if (
                d.startswith("close")
                and size is not None
                and 0 < float(size) < pos.size - dust
                and price > 0
            ):
                part = float(size)
                if pos.side == "long":
                    pos.closed_pnl += (float(price) - pos.entry) * part
                else:
                    pos.closed_pnl += (pos.entry - float(price)) * part
                pos.closed_size += part
                pos.size -= part
                pos.stop_size = float(pos.size)
                pos.needs_stop_resize = True
                return None
            closed = self.try_exit(coin, price)
            if closed is not None:
                return closed
            return self.force_flat(coin, price)
        if crossed is not False:
            return None
        if st is None or st.working is None:
            return None
        order = st.working
        if oid is not None and order.oid is not None and oid != order.oid:
            return None
        if order.external and (st.position is None or st.position.adopted):
            return self._fill_external(
                order,
                price if price > 0 else order.limit_px,
                ts,
                size=size,
            )
        return self._fill(
            order,
            price if price > 0 else order.limit_px,
            ts,
            size=size,
        )

    def _take_runner_tp1(self, pos: OpenPosition, price: float, size: float | None) -> bool:
        """A partial fill at TP1. Stop goes to breakeven; the runner stays open.

        A fill of the whole position, a trim that has not reached TP1, or a
        reduce on the stop side is left to the normal close / drip path.
        The exchange stop is not cancelled here — the loop rests the new
        one first.
        """
        if not pos.runner_on or pos.tp1_filled or price <= 0 or size is None:
            return False
        part = float(size)
        if part <= 0:
            return False
        dust = max(1e-12, abs(pos.size) * 1e-6)
        if part >= pos.size - dust:
            return False
        tol = max(abs(pos.take_profit) * 1e-4, 1e-8)
        if pos.side == "long":
            on_tp = price + tol >= pos.take_profit > pos.entry and price > pos.stop
        elif pos.side == "short":
            on_tp = price - tol <= pos.take_profit < pos.entry and price < pos.stop
        else:
            return False
        if not on_tp:
            return False
        if pos.side == "long":
            pos.closed_pnl += (float(price) - pos.entry) * part
        else:
            pos.closed_pnl += (pos.entry - float(price)) * part
        pos.closed_size += part
        pos.size -= part
        if pos.planned_stop <= 0:
            pos.planned_stop = float(pos.stop)
        pos.stop = float(pos.entry)
        if pos.runner_px:
            pos.take_profit = float(pos.runner_px)
        pos.tp1_filled = True
        pos.tp_oid = None
        pos.tp1_size = 0.0
        pos.runner_size = float(pos.size)
        pos.stop_size = float(pos.size)
        pos.needs_stop_resize = True
        pos.runner_event = "tp1"
        pos.just_opened = False
        return True

    def try_exit(self, coin: str, price: float) -> CloseEvent | None:
        """Stop or TP only. Flow / delta is not consulted."""
        st = self._coins.get(canon_coin(coin))
        if st is None or st.position is None or price <= 0:
            return None
        pos = st.position
        # Adopted stops are unknown. A price must not flat the coin.
        if pos.adopted:
            return None
        reason: str | None = None
        if pos.side == "long":
            if price <= pos.stop:
                reason = "stop"
            elif price >= pos.take_profit:
                reason = "tp"
        else:
            if price >= pos.stop:
                reason = "stop"
            elif price <= pos.take_profit:
                reason = "tp"
        if reason is None:
            return None
        exit_px = pos.stop if reason == "stop" else pos.take_profit
        return self._close_open(st, pos, exit_px, reason)

    def force_flat(self, coin: str, price: float, *, reason: str = "flat") -> CloseEvent | None:
        """Clear an open position and its pending remainder immediately.

        A bracket fill can print between the stop and the target. The
        exchange is flat either way. The resting Alo must not keep
        reserving margin until the thesis goes stale.
        """
        st = self._coins.get(canon_coin(coin))
        if st is None or st.position is None or price <= 0:
            return None
        return self._close_open(st, st.position, float(price), reason)

    def _close_open(self, st: _CoinState, pos: OpenPosition, exit_px: float, reason: str) -> CloseEvent:
        if pos.side == "long":
            pnl = (exit_px - pos.entry) * pos.size
        else:
            pnl = (pos.entry - exit_px) * pos.size
        pnl += pos.closed_pnl
        remainder = st.working
        st.working = None
        event = CloseEvent(
            coin=pos.coin,
            side=pos.side,
            size=pos.size + pos.closed_size,
            entry=pos.entry,
            exit=exit_px,
            pnl=pnl,
            reason=reason,
            swing_id=pos.swing_id,
            remainder=remainder,
        )
        st.consumed.add(pos.swing_id)
        st.position = None
        return event

    def propose_stop(self, coin: str, proposed: float) -> float | None:
        """Heal path. A tighter stop cannot replace a wider one."""
        st = self._coins.get(canon_coin(coin))
        if st is None or st.position is None:
            return None
        st.position.stop = heal_stop(st.position.side, st.position.stop, proposed)
        return st.position.stop

    def flatten(self, price_for: dict[str, float] | None = None) -> list[CloseEvent]:
        """Emergency flatten (operator kill switch). Not a flow exit."""
        prices = price_for or {}
        events: list[CloseEvent] = []
        self.flatten_cancels = []
        for coin, st in list(self._coins.items()):
            working = st.working
            if working is not None:
                st.consumed.add(working.swing_id)
                st.working = None
            if st.position is None:
                if working is not None:
                    self.flatten_cancels.append(working)
                continue
            px = prices.get(coin, st.position.entry)
            pos = st.position
            if pos.side == "long":
                pnl = (px - pos.entry) * pos.size
            else:
                pnl = (pos.entry - px) * pos.size
            events.append(
                CloseEvent(
                    coin=coin,
                    side=pos.side,
                    size=pos.size,
                    entry=pos.entry,
                    exit=px,
                    pnl=pnl,
                    reason="kill_switch",
                    swing_id=pos.swing_id,
                    remainder=working,
                )
            )
            st.consumed.add(pos.swing_id)
            st.position = None
        return events

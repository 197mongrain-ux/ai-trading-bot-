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
    arm_take_profit,
    collides_with_fill,
    heal_stop,
    place_stop,
    stop_is_valid,
    widen_stop_for_fill,
)
from hl_bot.strategy.model_b.types import AloIntent, TradePrint

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

    def _state(self, coin: str) -> _CoinState:
        coin = coin.upper()
        st = self._coins.get(coin)
        if st is None:
            st = _CoinState()
            self._coins[coin] = st
        return st

    def block_reason(self, coin: str, swing_id: str) -> str | None:
        st = self._coins.get(coin.upper())
        if st is None:
            return None
        if st.position is not None:
            return AVERAGE_DOWN
        if st.working is not None:
            return SECOND_ALO
        if swing_id in st.consumed:
            return THESIS_DONE
        return None

    def post(self, intent: AloIntent, now: float, oid: object | None = None) -> WorkingOrder:
        if intent.tif != "Alo" or intent.market_fallback:
            raise ValueError("Model B is post-only Alo; market fallback is off")
        if int(intent.leverage) != 20:
            raise ValueError("Model B is 20x only (40x off)")
        reason = self.block_reason(intent.coin, intent.swing_id)
        if reason:
            raise ValueError(reason)
        order = WorkingOrder(
            coin=intent.coin.upper(),
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
            if order is None:
                continue
            # A partial fill may already have a position. The timer still
            # drops only the resting remainder; the position stays.
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
        st = self._coins.get(coin.upper())
        if st is None or st.working is None:
            return []
        # A partial fill keeps the position. Stale still cancels the
        # unfilled remainder and does not flatten the filled size.
        order = st.working
        if order.sweep_px is None:
            return []
        for print_ in sorted(prints, key=lambda p: (p.ts, p.seq)):
            if print_.coin.upper() != order.coin or print_.ts + 1e-9 < order.posted_at:
                continue
            if _print_stales_order(order, print_):
                st.consumed.add(order.swing_id)
                st.working = None
                return [order]
        return []

    def working(self, coin: str) -> WorkingOrder | None:
        st = self._coins.get(coin.upper())
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
        st = self._coins.get(coin.upper())
        if st is None or st.working is None or st.position is not None:
            return None
        order = st.working
        st.consumed.add(order.swing_id)
        st.working = None
        return order

    def position(self, coin: str) -> OpenPosition | None:
        st = self._coins.get(coin.upper())
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
                pushed = place_stop(order.side, price, price, order.tick, tp_r=order.tp_r)
                if (
                    pushed is not None
                    and stop_is_valid(order.side, price, pushed)
                    and not collides_with_fill(price, pushed, order.tick)
                ):
                    stop = pushed
            tp = arm_take_profit(order.side, price, stop, order.pool_px, tp_r=order.tp_r)
            if tp is None:
                tp = order.take_profit
            pos = OpenPosition(
                coin=order.coin,
                side=order.side,
                size=added,
                entry=price,
                stop=stop,
                take_profit=tp,
                swing_id=order.swing_id,
                opened_at=ts,
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

    def try_fill_from_prints(self, prints: list[TradePrint]) -> OpenPosition | None:
        """Paper fill. A resting buy fills on a later sell at or through the bid.

        A resting sell fills on a later buy at or through the ask. Prints
        from before the order was posted do not fill it. A print with no
        aggressor side does not fill (fail closed). This is not a market order.
        """
        if not prints:
            return None
        coin = prints[0].coin.upper()
        st = self._coins.get(coin)
        if st is None or st.working is None or st.position is not None:
            return None
        order = st.working
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
                return self._fill(order, order.limit_px, print_.ts)
            if (
                order.side == "short"
                and print_.side == "buy"
                and print_.price >= order.limit_px - 1e-9
            ):
                return self._fill(order, order.limit_px, print_.ts)
        return None

    def apply_user_fill(
        self,
        *,
        coin: str,
        oid: object | None,
        price: float,
        ts: float,
        crossed: bool | None,
        size: float | None = None,
    ) -> OpenPosition | CloseEvent | None:
        """Live fills.

        ``crossed is False`` is a maker Alo fill. The first drip opens the
        position at that size and leaves any unfilled remainder working.
        Later drips on the same order grow the position. ``crossed is True``
        while a position is open is the resting stop/TP bracket firing — it
        closes, it does not add. A taker fill with no position is ignored
        (no market entry). Unknown ``crossed`` is ignored.
        """
        st = self._coins.get(coin.upper())
        if crossed is True:
            if st is None or st.position is None:
                return None
            return self.try_exit(coin, price)
        if crossed is not False:
            return None
        if st is None or st.working is None:
            return None
        order = st.working
        if oid is not None and order.oid is not None and oid != order.oid:
            return None
        return self._fill(
            order,
            price if price > 0 else order.limit_px,
            ts,
            size=size,
        )

    def try_exit(self, coin: str, price: float) -> CloseEvent | None:
        """Stop or TP only. Flow / delta is not consulted."""
        st = self._coins.get(coin.upper())
        if st is None or st.position is None or price <= 0:
            return None
        pos = st.position
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
        if pos.side == "long":
            pnl = (exit_px - pos.entry) * pos.size
        else:
            pnl = (pos.entry - exit_px) * pos.size
        remainder = st.working
        st.working = None
        event = CloseEvent(
            coin=pos.coin,
            side=pos.side,
            size=pos.size,
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
        st = self._coins.get(coin.upper())
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

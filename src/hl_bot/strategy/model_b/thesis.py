"""One thesis per coin.

No average-down, no second Alo, no market fallback, no re-entry on the
same swing after a 20s cancel or a stop. A new swing id may start a new
thesis once the coin is flat and no order is working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hl_bot.strategy.model_b.risk import heal_stop
from hl_bot.strategy.model_b.types import AloIntent, TradePrint

WORK_SEC = 20.0

SECOND_ALO = "SECOND_ALO"
AVERAGE_DOWN = "AVERAGE_DOWN"
THESIS_DONE = "THESIS_DONE"


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


@dataclass
class _CoinState:
    working: WorkingOrder | None = None
    position: OpenPosition | None = None
    consumed: set[str] = field(default_factory=set)


class ThesisBook:
    def __init__(self, work_sec: float = WORK_SEC):
        self.work_sec = float(work_sec)
        self._coins: dict[str, _CoinState] = {}

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
        )
        self._state(intent.coin).working = order
        return order

    def expire(self, now: float) -> list[WorkingOrder]:
        """Cancel working orders that have rested ``work_sec`` without a fill.

        The swing is consumed. That thesis is done until a new swing id.
        """
        cancelled: list[WorkingOrder] = []
        for st in self._coins.values():
            order = st.working
            if order is None or st.position is not None:
                continue
            if float(now) + 1e-9 >= order.posted_at + self.work_sec:
                st.consumed.add(order.swing_id)
                st.working = None
                cancelled.append(order)
        return cancelled

    def working(self, coin: str) -> WorkingOrder | None:
        st = self._coins.get(coin.upper())
        return None if st is None else st.working

    def position(self, coin: str) -> OpenPosition | None:
        st = self._coins.get(coin.upper())
        return None if st is None else st.position

    def _fill(self, order: WorkingOrder, price: float, ts: float) -> OpenPosition:
        st = self._state(order.coin)
        pos = OpenPosition(
            coin=order.coin,
            side=order.side,
            size=order.size,
            entry=price,
            stop=order.stop,
            take_profit=order.take_profit,
            swing_id=order.swing_id,
            opened_at=ts,
        )
        st.position = pos
        st.working = None
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
    ) -> OpenPosition | CloseEvent | None:
        """Live fills.

        ``crossed is False`` is a maker Alo fill and opens the position.
        ``crossed is True`` while a position is open is the resting stop/TP
        bracket firing — it closes, it does not add. A taker fill with no
        position is ignored (no market entry). Unknown ``crossed`` is ignored.
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
        return self._fill(order, price if price > 0 else order.limit_px, ts)

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
        event = CloseEvent(
            coin=pos.coin,
            side=pos.side,
            size=pos.size,
            entry=pos.entry,
            exit=exit_px,
            pnl=pnl,
            reason=reason,
            swing_id=pos.swing_id,
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
        for coin, st in list(self._coins.items()):
            if st.working is not None:
                st.consumed.add(st.working.swing_id)
                st.working = None
            if st.position is None:
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
                )
            )
            st.consumed.add(pos.swing_id)
            st.position = None
        return events

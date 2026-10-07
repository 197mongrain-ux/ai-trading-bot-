"""Exchange account snapshot for Model B margin, adoption, and closes.

Spot USDC ``total`` does not shrink when a perp holds margin. Builder-dex
positions (``xyz:XYZ100``) live on that dex's clearinghouse, not the
default one. After a restart the in-memory book is empty, so margin,
same-coin blocks, and close detection have to read the exchange.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.exchange.hl_trades import UserFill, parse_user_fill
from hl_bot.strategy.model_b.risk import initial_margin
from hl_bot.strategy.model_b.universe import canon_coin


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PerpPosition:
    """One open perp. ``margin_used`` is the exchange figure when it sent one."""

    coin: str
    szi: float
    entry: float
    margin_used: float

    @property
    def side(self) -> str:
        return "long" if self.szi > 0 else "short"

    @property
    def size(self) -> float:
        return abs(float(self.szi))

    def held_margin(self) -> float:
        """USDC this position locks.

        A missing or zero ``marginUsed`` falls back to notional / 20, the
        same initial margin a resting Alo uses. A real zero with no size
        contributes nothing.
        """
        if self.size <= 0:
            return 0.0
        if self.margin_used > 0:
            return float(self.margin_used)
        if self.entry > 0:
            return initial_margin(self.size, self.entry)
        return 0.0


@dataclass(frozen=True)
class EntryOrder:
    """Resting entry (not a reduce-only stop or TP)."""

    coin: str
    oid: object
    side: str
    limit_px: float
    size: float
    timestamp: float | None = None

    def held_margin(self) -> float:
        if self.size <= 0 or self.limit_px <= 0:
            return 0.0
        return initial_margin(self.size, self.limit_px)


@dataclass
class AccountSnapshot:
    """One pass over the dexes this hunt can trade.

    ``ok`` is False when a dex read failed. Callers must not treat that as
    a flat account: a missed ``xyz`` book would drop a live position and
    invent a close.
    """

    ok: bool
    positions: tuple[PerpPosition, ...] = ()
    entry_orders: tuple[EntryOrder, ...] = ()
    fills: tuple[UserFill, ...] = ()
    reported_margin: float = 0.0
    dexs: tuple[str, ...] = ()

    def position(self, coin: str) -> PerpPosition | None:
        name = canon_coin(coin)
        for pos in self.positions:
            if pos.coin == name and pos.size > 0:
                return pos
        return None

    def entry_order(self, coin: str) -> EntryOrder | None:
        name = canon_coin(coin)
        for order in self.entry_orders:
            if order.coin == name and order.size > 0:
                return order
        return None

    def position_margin(self) -> float:
        return sum(pos.held_margin() for pos in self.positions)

    def entry_margin(self) -> float:
        return sum(order.held_margin() for order in self.entry_orders)

    def margin_held(self) -> float:
        """USDC already committed, across the default dex and builder dexes.

        Per-position ``marginUsed`` plus resting entry orders. If the
        clearinghouse reported a larger ``totalMarginUsed`` (it sometimes
        includes the orders already), keep the larger figure so free margin
        cannot exceed what the exchange will actually fund.
        """
        summed = self.position_margin() + self.entry_margin()
        reported = max(0.0, float(self.reported_margin))
        return max(summed, reported)


def dexs_for_coins(coins: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """``""`` plus each builder dex in the hunt. ``""`` is the original perp dex."""
    dexs: list[str] = [""]
    for coin in coins:
        name = canon_coin(coin)
        if ":" not in name:
            continue
        dex = name.split(":", 1)[0]
        if dex and dex not in dexs:
            dexs.append(dex)
    return tuple(dexs)


def parse_clearinghouse(payload: object) -> tuple[list[PerpPosition], float]:
    """Positions and ``totalMarginUsed`` from one ``clearinghouseState`` body.

    An empty ``assetPositions`` list is a real flat dex. A non-dict body is
    a failed read: the caller must not add a zero and must not mark the
    snapshot ok.
    """
    if not isinstance(payload, dict):
        raise ValueError("clearinghouseState body is not an object")
    reported = 0.0
    summary = payload.get("marginSummary")
    if isinstance(summary, dict):
        parsed = _as_float(summary.get("totalMarginUsed"))
        if parsed is not None and parsed > 0:
            reported = parsed
    positions: list[PerpPosition] = []
    rows = payload.get("assetPositions")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("assetPositions is not a list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        pos = row.get("position") if isinstance(row.get("position"), dict) else row
        if not isinstance(pos, dict):
            continue
        coin = canon_coin(pos.get("coin"))
        szi = _as_float(pos.get("szi"))
        if not coin or szi is None or abs(szi) <= 0:
            continue
        entry = _as_float(pos.get("entryPx")) or 0.0
        margin = _as_float(pos.get("marginUsed")) or 0.0
        positions.append(
            PerpPosition(coin=coin, szi=float(szi), entry=float(entry), margin_used=float(margin))
        )
    return positions, reported


def _is_reduce_only(raw: dict) -> bool:
    if raw.get("reduceOnly") is True or raw.get("reduce_only") is True:
        return True
    if raw.get("isPositionTpsl") is True or raw.get("isTrigger") is True:
        return True
    text = str(raw.get("orderType") or raw.get("order_type") or "").lower()
    if "stop" in text or "take" in text or "trigger" in text:
        return True
    return False


def parse_entry_orders(payload: object) -> list[EntryOrder]:
    """Resting entries from ``frontendOpenOrders`` or ``openOrders``.

    Reduce-only stops and take-profits are not entries. They must not
    block a new coin or reserve a second ticket's margin.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError("open orders body is not a list")
    orders: list[EntryOrder] = []
    for raw in payload:
        if not isinstance(raw, dict) or _is_reduce_only(raw):
            continue
        coin = canon_coin(raw.get("coin"))
        limit = _as_float(raw.get("limitPx", raw.get("limit_px")))
        size = _as_float(raw.get("sz", raw.get("size")))
        if not coin or limit is None or size is None or limit <= 0 or size <= 0:
            continue
        side_raw = str(raw.get("side") or "").strip().upper()
        if side_raw in {"B", "BUY", "BID"}:
            side = "long"
        elif side_raw in {"A", "SELL", "ASK", "S"}:
            side = "short"
        else:
            continue
        ts = _as_float(raw.get("timestamp"))
        if ts is not None and ts > 1e12:
            ts = ts / 1000.0
        orders.append(
            EntryOrder(
                coin=coin,
                oid=raw.get("oid"),
                side=side,
                limit_px=float(limit),
                size=float(size),
                timestamp=ts,
            )
        )
    return orders


def parse_account_fills(payload: object) -> list[UserFill]:
    """``userFills`` / ``userFillsByTime`` rows. Bad rows are skipped."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        rows = payload.get("fills")
        if not isinstance(rows, list):
            return []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    fills: list[UserFill] = []
    for raw in rows:
        if isinstance(raw, dict):
            parsed = parse_user_fill(raw)
            if parsed is not None:
                fills.append(parsed)
    return fills


def fill_reduces_position(fill: UserFill, side: str | None) -> bool:
    """True when this fill reduces an open long or short.

    ``dir`` of ``Close Long`` / ``Close Short`` counts, including a flip
    (``Long > Short``) and a liquidation. A sell against a long (or a buy
    against a short) counts when ``dir`` is missing. An opening fill does not.
    """
    text = str(fill.direction or "").strip().lower()
    if text.startswith("close") or "liquidat" in text or ">" in text:
        return True
    start = fill.start_position
    aggressor = fill.side
    if start is not None and aggressor in ("buy", "sell"):
        if start > 0 and aggressor == "sell":
            return True
        if start < 0 and aggressor == "buy":
            return True
        return False
    if side == "long" and aggressor == "sell":
        return True
    if side == "short" and aggressor == "buy":
        return True
    return False


def fill_end_position(fill: UserFill) -> float | None:
    """Position size after this fill. Positive is long. ``None`` if unknown."""
    start = fill.start_position
    if start is None or fill.side not in ("buy", "sell"):
        return None
    if fill.side == "sell":
        return float(start) - float(fill.size)
    return float(start) + float(fill.size)


def fills_flatten(fills: list[UserFill]) -> bool:
    """True when the last fill with a known end size is flat, or ``dir`` says close.

    A single ``Close Long`` with no ``startPosition`` still counts: that is
    how a take-profit fill is labeled when the field is absent.
    """
    if not fills:
        return False
    end: float | None = None
    for fill in fills:
        parsed = fill_end_position(fill)
        if parsed is not None:
            end = parsed
        text = str(fill.direction or "").strip().lower()
        if end is None and (text.startswith("close") or "liquidat" in text):
            end = 0.0
    if end is None:
        return False
    return abs(end) <= 1e-9


def classify_close_reason(
    side: str | None,
    price: float,
    entry: float | None,
    stop: float | None,
    tp: float | None,
) -> str:
    """``tp`` or ``stop`` when the fill is on that side, including a little slippage.

    The Oct 7 ``xyz:XYZ100`` take-profit triggered at 31165 and filled at
    31162. A strict ``price >= tp`` check misses that fill. Closer to the
    target than to the stop, and on the profit side of the entry, is a tp.
    """
    if side not in ("long", "short") or price <= 0:
        return "reconcile"
    stop_px = float(stop) if stop else 0.0
    tp_px = float(tp) if tp else 0.0
    entry_px = float(entry) if entry else 0.0
    if side == "long":
        if tp_px > 0 and price >= tp_px:
            return "tp"
        if stop_px > 0 and price <= stop_px:
            return "stop"
    else:
        if tp_px > 0 and price <= tp_px:
            return "tp"
        if stop_px > 0 and price >= stop_px:
            return "stop"
    if tp_px > 0 and stop_px > 0 and entry_px > 0:
        dist_tp = abs(price - tp_px)
        dist_stop = abs(price - stop_px)
        if side == "long" and price > entry_px and dist_tp <= dist_stop:
            return "tp"
        if side == "long" and price < entry_px and dist_stop < dist_tp:
            return "stop"
        if side == "short" and price < entry_px and dist_tp <= dist_stop:
            return "tp"
        if side == "short" and price > entry_px and dist_stop < dist_tp:
            return "stop"
    return "reconcile"


@dataclass(frozen=True)
class PlannedClose:
    """A close the journal does not have yet, built from exchange fills."""

    coin: str
    side: str
    size: float
    entry: float
    exit: float
    pnl: float
    reason: str
    source: str = "reconcile"


def unmatched_journal_opens(rows: list[dict]) -> dict[str, dict]:
    """Latest still-open journal ``open`` per coin. A later ``close`` pops it."""
    stacks: dict[str, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        coin = canon_coin(row.get("symbol") or row.get("coin") or "")
        if not coin:
            continue
        if event == "open":
            stacks.setdefault(coin, []).append(row)
        elif event == "close" and stacks.get(coin):
            stacks[coin].pop()
    return {coin: items[-1] for coin, items in stacks.items() if items}


def _weighted_exit(fills: list[UserFill]) -> float:
    notional = 0.0
    size = 0.0
    for fill in fills:
        if fill.price <= 0 or fill.size <= 0:
            continue
        notional += float(fill.price) * float(fill.size)
        size += float(fill.size)
    if size <= 0:
        return fills[-1].price
    return notional / size


def _pnl_from_fills(fills: list[UserFill], side: str, entry: float, size: float, exit_px: float) -> float:
    closed = [fill.closed_pnl for fill in fills if fill.closed_pnl is not None]
    if closed:
        return float(sum(closed))
    if side == "long":
        return (exit_px - entry) * size
    return (entry - exit_px) * size


def plan_close_for_open(
    *,
    coin: str,
    side: str | None,
    size: float,
    entry: float,
    stop: float | None,
    tp: float | None,
    since_ts: float,
    fills: list[UserFill],
) -> PlannedClose | None:
    """One journal close when reducing fills flatten the coin after ``since_ts``.

    No fill means no invented price. The position stays missing from the
    journal until a fill shows up.
    """
    name = canon_coin(coin)
    relevant = [
        fill
        for fill in fills
        if fill.coin == name and fill.ts + 1e-9 >= float(since_ts) and fill_reduces_position(fill, side)
    ]
    relevant.sort(key=lambda fill: (fill.ts, fill.tid or 0))
    if not relevant or not fills_flatten(relevant):
        return None
    if side not in ("long", "short"):
        text = str(relevant[-1].direction or "").lower()
        if "short" in text:
            side = "short" if "close" in text else "long"
        elif "long" in text:
            side = "long" if "close" in text else "short"
        else:
            side = "long"
    exit_px = _weighted_exit(relevant)
    qty = float(size) if size > 0 else sum(fill.size for fill in relevant)
    pnl = _pnl_from_fills(relevant, side, float(entry), qty, exit_px)
    reason = classify_close_reason(side, exit_px, entry, stop, tp)
    return PlannedClose(
        coin=name,
        side=side,
        size=qty,
        entry=float(entry),
        exit=exit_px,
        pnl=pnl,
        reason=reason,
    )


@dataclass
class BookView:
    """Open positions the in-memory book already tracks. Used when planning closes."""

    coin: str
    side: str
    size: float
    entry: float
    stop: float
    take_profit: float
    opened_at: float


def plan_reconcile_closes(
    journal_rows: list[dict],
    snapshot: AccountSnapshot,
    book_positions: list[BookView],
) -> list[PlannedClose]:
    """Closes to journal because the exchange is flat and a fill shows it.

    A snapshot that failed (``ok`` is False) returns nothing. Missing a dex
    must not journal a close for a position that is still open.
    """
    if not snapshot.ok:
        return []
    exchange_open = {pos.coin for pos in snapshot.positions if pos.size > 0}
    plans: list[PlannedClose] = []
    seen: set[str] = set()
    opens = unmatched_journal_opens(journal_rows)
    for coin, row in opens.items():
        if coin in exchange_open:
            continue
        try:
            since = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            since = 0.0
        plan = plan_close_for_open(
            coin=coin,
            side=row.get("side") if isinstance(row.get("side"), str) else None,
            size=_as_float(row.get("size")) or 0.0,
            entry=_as_float(row.get("price")) or 0.0,
            stop=_as_float(row.get("stop")),
            tp=_as_float(row.get("tp")),
            since_ts=since,
            fills=list(snapshot.fills),
        )
        if plan is None:
            continue
        plans.append(plan)
        seen.add(coin)
    for pos in book_positions:
        coin = canon_coin(pos.coin)
        if coin in exchange_open or coin in seen:
            continue
        plan = plan_close_for_open(
            coin=coin,
            side=pos.side,
            size=pos.size,
            entry=pos.entry,
            stop=pos.stop if pos.stop > 0 else None,
            tp=pos.take_profit if pos.take_profit > 0 else None,
            since_ts=pos.opened_at,
            fills=list(snapshot.fills),
        )
        if plan is None:
            continue
        plans.append(plan)
        seen.add(coin)
    return plans


def position_coins_label(positions: list[PerpPosition] | tuple[PerpPosition, ...]) -> str:
    names = [pos.coin for pos in positions if pos.size > 0]
    return ",".join(names)

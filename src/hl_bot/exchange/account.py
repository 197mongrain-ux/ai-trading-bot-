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
    # Stable ids (``tid:`` / ``hash:`` / ``fill:``). One id closes one open.
    fill_ids: tuple[str, ...] = ()


def fill_id(fill: UserFill) -> str:
    """Identity of one exchange fill. A tid wins, then the tx hash.

    The fallback still changes when the fill does, so a row with neither
    field cannot be reused on the next open.
    """
    tid = getattr(fill, "tid", None)
    if tid is not None and str(tid).strip() != "":
        return f"tid:{tid}"
    tx_hash = getattr(fill, "hash", None)
    if tx_hash is not None and str(tx_hash).strip() != "":
        return f"hash:{tx_hash}"
    oid = getattr(fill, "oid", None)
    return f"fill:{fill.coin}:{float(fill.ts):.3f}:{fill.price}:{fill.size}:{oid}"


def consumed_fill_ids(rows: list[dict]) -> set[str]:
    """Fill ids already written on a journal close. Survives a restart."""
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("event") != "close":
            continue
        raw = row.get("fill_ids")
        items: list[object]
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = []
        for item in items:
            if item is not None and str(item).strip() != "":
                found.add(str(item))
        tid = row.get("tid")
        if tid is not None and str(tid).strip() != "":
            found.add(f"tid:{tid}")
        tx_hash = row.get("hash")
        if tx_hash is not None and str(tx_hash).strip() != "":
            found.add(f"hash:{tx_hash}")
    return found


def _row_coin(row: dict) -> str:
    return canon_coin(row.get("symbol") or row.get("coin") or "")


def _row_ts(row: dict) -> float:
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm_token(value: object) -> str:
    return str(value or "").strip().lower()


def annotate_opens(rows: list[dict]) -> list[dict]:
    """Each journal open, with the network and account of the start before it.

    A row's own ``network`` / ``account`` wins. Otherwise it inherits the
    latest ``start`` above it, so a testnet session stays testnet after a
    later mainnet start is appended.
    """
    network = ""
    account = ""
    annotated: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        if event == "start":
            if row.get("network"):
                network = _norm_token(row.get("network"))
            acct = row.get("account") or row.get("account_address")
            if acct:
                account = _norm_token(acct)
            continue
        if event != "open":
            continue
        own_net = row.get("network")
        own_acct = row.get("account") or row.get("account_address")
        annotated.append(
            {
                "index": index,
                "row": row,
                "coin": _row_coin(row),
                "network": _norm_token(own_net) if own_net else network,
                "account": _norm_token(own_acct) if own_acct else account,
                "ts": _row_ts(row),
            }
        )
    return annotated


def _latest_close_index(rows: list[dict], coin: str) -> int:
    """File order, not the stack. One close covers every open above it."""
    last = -1
    name = canon_coin(coin)
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("event") != "close":
            continue
        if _row_coin(row) == name:
            last = index
    return last


def _network_ok(item: dict, network: str) -> bool:
    """Untagged history is allowed. An explicit other network is not."""
    own = item.get("network") or ""
    current = _norm_token(network)
    if not own or not current:
        return True
    return own == current


def _account_ok(item: dict, account: str) -> bool:
    """Untagged history is allowed. Both sides set and different is not."""
    own = item.get("account") or ""
    current = _norm_token(account)
    if not own or not current:
        return True
    return own == current


def _entry_matches(row: dict, entry: float, side: str) -> bool:
    """The journal open is the position we adopted, not an older ticket."""
    if side in ("long", "short") and row.get("side") not in (None, "", side):
        return False
    price = _as_float(row.get("price"))
    if price is None or entry <= 0 or price <= 0:
        return False
    return abs(price - entry) <= max(1e-6, abs(entry) * 1e-4)


def unmatched_journal_opens(rows: list[dict]) -> dict[str, dict]:
    """Latest still-open journal ``open`` per coin. A later ``close`` pops it.

    Kept for callers that want the stack view. Reconcile does not use it:
    popping one close exposed the next stale open to the same fill.
    """
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
    used_ids: set[str] | None = None,
) -> PlannedClose | None:
    """One journal close when reducing fills flatten the coin after ``since_ts``.

    No fill means no invented price. A fill id already on a journal close
    is skipped, so the same tid cannot close the next open.
    """
    name = canon_coin(coin)
    spent = used_ids or set()
    relevant = [
        fill
        for fill in fills
        if fill.coin == name
        and fill.ts + 1e-9 >= float(since_ts)
        and fill_id(fill) not in spent
        and fill_reduces_position(fill, side)
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
        fill_ids=tuple(fill_id(fill) for fill in relevant),
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


@dataclass(frozen=True)
class ReconcileResult:
    """Closes to write, and stale opens to mention once."""

    closes: tuple[PlannedClose, ...]
    # ``(coin, open_count, reason)``. ``not_held`` is a flat book.
    # ``stale_open`` is an older or other-network open beside a live position.
    ignored: tuple[tuple[str, int, str], ...] = ()


def plan_reconcile_closes(
    journal_rows: list[dict],
    snapshot: AccountSnapshot,
    book_positions: list[BookView],
    *,
    network: str = "",
    account: str = "",
    session_started_at: float = 0.0,
) -> ReconcileResult:
    """Close a position this process is holding when the exchange is flat.

    A failed snapshot returns nothing. A journal open the book is not
    holding is not closed: that is how a testnet leftover and a recent
    fill produced a new loss every loop. One fill id can close only the
    single open after that coin's latest journaled close, on this
    network and account, matching the position we adopted. Older opens
    are reported in ``ignored`` and are not paired with the fill.
    """
    if not snapshot.ok:
        return ReconcileResult(closes=())
    used = consumed_fill_ids(journal_rows)
    annotated = annotate_opens(journal_rows)
    exchange_open = {pos.coin for pos in snapshot.positions if pos.size > 0}
    held = {canon_coin(pos.coin): pos for pos in book_positions if pos.size > 0}
    coins = {item["coin"] for item in annotated if item["coin"]}
    coins.update(held)
    plans: list[PlannedClose] = []
    ignored: list[tuple[str, int, str]] = []
    for coin in sorted(coins):
        last_close = _latest_close_index(journal_rows, coin)
        leftover = [
            item
            for item in annotated
            if item["coin"] == coin and item["index"] > last_close
        ]
        pos = held.get(coin)
        if pos is None:
            if leftover:
                ignored.append((coin, len(leftover), "not_held"))
            continue
        if coin in exchange_open:
            stale = [
                item
                for item in leftover
                if not _network_ok(item, network)
                or not _account_ok(item, account)
                or not _entry_matches(item["row"], pos.entry, pos.side)
            ]
            if stale:
                ignored.append((coin, len(stale), "stale_open"))
            continue
        chosen, stale = _choose_open(leftover, pos, network=network, account=account)
        if stale:
            ignored.append((coin, len(stale), "stale_open"))
        if chosen is not None:
            row = chosen["row"]
            since = chosen["ts"]
            side = row.get("side") if isinstance(row.get("side"), str) else pos.side
            entry = _as_float(row.get("price")) or pos.entry
            stop = _as_float(row.get("stop"))
            tp = _as_float(row.get("tp"))
            size = pos.size
        else:
            # Adopted, then flat, and the journal open does not match.
            # Still one close for the position we are holding. The fill
            # is consumed so it cannot be spent on a stale open later.
            since = float(pos.opened_at)
            side = pos.side
            entry = pos.entry
            stop = pos.stop if pos.stop > 0 else None
            tp = pos.take_profit if pos.take_profit > 0 else None
            size = pos.size
            if session_started_at and since + 1e-9 < float(session_started_at):
                since = float(session_started_at)
        plan = plan_close_for_open(
            coin=coin,
            side=side,
            size=size,
            entry=entry,
            stop=stop,
            tp=tp,
            since_ts=since,
            fills=list(snapshot.fills),
            used_ids=used,
        )
        if plan is None:
            continue
        plans.append(plan)
        used.update(plan.fill_ids)
    return ReconcileResult(closes=tuple(plans), ignored=tuple(ignored))


def _choose_open(
    leftover: list[dict],
    pos: BookView,
    *,
    network: str,
    account: str,
) -> tuple[dict | None, list[dict]]:
    """The newest open that belongs to this adopted position. The rest are stale.

    Eligible means: this network, this account, and the same side and
    entry as the position we adopted. Anything older than that open, or
    from another network, is not adoptable state.
    """
    stale: list[dict] = []
    candidates: list[dict] = []
    for item in leftover:
        if not _network_ok(item, network) or not _account_ok(item, account):
            stale.append(item)
            continue
        if not _entry_matches(item["row"], pos.entry, pos.side):
            stale.append(item)
            continue
        candidates.append(item)
    if not candidates:
        return None, stale
    chosen = max(candidates, key=lambda item: (item["ts"], item["index"]))
    for item in candidates:
        if item is not chosen:
            stale.append(item)
    return chosen, stale


def position_coins_label(positions: list[PerpPosition] | tuple[PerpPosition, ...]) -> str:
    names = [pos.coin for pos in positions if pos.size > 0]
    return ",".join(names)

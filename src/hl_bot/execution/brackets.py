"""Live position protection: full-size reduce-only TP/SL and halt gating.

A filled order (including an Alo that drips in) must never leave a naked
position. Brackets are sized to the exchange position (``szi``), triggers are
Decimal-quantized onto the Hyperliquid perp tick, and a process stop is
refused until those brackets exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

# Perps: at most 5 significant figures, and at most (6 - szDecimals) decimal places.
_PERP_MAX_DECIMALS = 6
_SIG_FIGS = 5


def _to_decimal(value: Decimal | int | str | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_trigger_px(px: Decimal | int | str | float, sz_decimals: int) -> Decimal:
    """Round a trigger onto the Hyperliquid perp tick using Decimal.

    Float ``round(float(f"{px:.5g}"), dp)`` leaves binary dust that the
    exchange rejects (MORPHO tick). This keeps the same grid — 5 significant
    figures and at most ``6 - szDecimals`` decimal places — with Decimal
    quantization so the wire string is exact.
    """
    d = _to_decimal(px)
    if d <= 0:
        raise ValueError(f"trigger px must be > 0, got {px}")
    max_dp = _PERP_MAX_DECIMALS - int(sz_decimals)
    adj = d.normalize().adjusted()  # floor(log10(|d|))
    sig_dp = _SIG_FIGS - 1 - adj
    dp = min(max_dp, sig_dp)
    if dp >= 0:
        quant = Decimal(1).scaleb(-dp)
        return d.quantize(quant, rounding=ROUND_HALF_UP)
    quant = Decimal(1).scaleb(-dp)
    return (d / quant).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quant


def quantize_size(sz: Decimal | int | str | float, sz_decimals: int) -> Decimal:
    """Size on the asset step. Round down so reduce-only never exceeds szi."""
    d = abs(_to_decimal(sz))
    quant = Decimal(1).scaleb(-int(sz_decimals))
    return d.quantize(quant, rounding=ROUND_DOWN)


def wire_float(value: Decimal) -> float:
    """Float stable under the SDK's 8-decimal wire check."""
    text = format(value, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        frac = frac[:8].rstrip("0")
        text = whole if not frac else f"{whole}.{frac}"
    x = float(text)
    rounded = f"{x:.8f}"
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError(f"value {value} is not wire-stable ({x})")
    return x


def recompute_brackets(
    side: str,
    entry: Decimal,
    stop_pct: Decimal,
    tp_r: Decimal,
) -> tuple[Decimal, Decimal]:
    """Stop beyond entry (long below / short above) and TP at the configured R.

    Uses the existing ``stop_pct`` and ``tp_r`` knobs — nothing new.
    """
    if entry <= 0:
        raise ValueError("entry must be > 0 to recompute brackets")
    if side == "long":
        stop = entry * (Decimal(1) - stop_pct)
        risk = entry - stop
        tp = entry + risk * tp_r
    else:
        stop = entry * (Decimal(1) + stop_pct)
        risk = stop - entry
        tp = entry - risk * tp_r
    return stop, tp


def levels_valid(side: str, entry: Decimal, stop: Decimal, tp: Decimal) -> bool:
    if entry <= 0 or stop <= 0 or tp <= 0:
        return False
    if side == "long":
        return stop < entry < tp
    return tp < entry < stop


@dataclass(frozen=True)
class BracketTicket:
    coin: str
    side: str
    stop: Decimal
    take_profit: Decimal
    entry: Decimal
    sz_decimals: int = 0
    trade_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())
        object.__setattr__(self, "stop", _to_decimal(self.stop))
        object.__setattr__(self, "take_profit", _to_decimal(self.take_profit))
        object.__setattr__(self, "entry", _to_decimal(self.entry))


@dataclass(frozen=True)
class ExchangePosition:
    coin: str
    szi: Decimal
    entry_px: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())
        object.__setattr__(self, "szi", _to_decimal(self.szi))
        object.__setattr__(self, "entry_px", _to_decimal(self.entry_px))

    @property
    def abs_size(self) -> Decimal:
        return abs(self.szi)

    @property
    def side(self) -> str:
        return "long" if self.szi > 0 else "short"


@dataclass(frozen=True)
class ExchangeOrder:
    oid: int
    coin: str
    sz: Decimal
    reduce_only: bool
    tpsl: str | None = None
    trigger_px: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())
        object.__setattr__(self, "sz", _to_decimal(self.sz))
        if self.trigger_px is not None:
            object.__setattr__(self, "trigger_px", _to_decimal(self.trigger_px))


@dataclass(frozen=True)
class AccountSnapshot:
    positions: tuple[ExchangePosition, ...]
    orders: tuple[ExchangeOrder, ...]
    sz_decimals: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ResizeRequest:
    coin: str
    side: str
    size: Decimal
    stop_px: Decimal
    take_profit_px: Decimal
    sz_decimals: int
    cancel_oids: tuple[int, ...] = ()
    # (oid, "tp"|"sl") for legs that can be modified in place.
    existing_legs: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class ProtectionPlan:
    """Actions required before any process stop.

    ``journal_events`` never contains a process ``stop``. Opens for new size
    are listed before anything else the caller might append.
    """

    journal_events: tuple[dict[str, Any], ...]
    resizes: tuple[ResizeRequest, ...]
    cancel_entry_orders: tuple[tuple[str, int], ...]
    naked_coins: tuple[str, ...]
    allow_process_stop: bool
    block_new_entries: bool
    next_last_abs: dict[str, Decimal]
    next_journaled_abs: dict[str, Decimal]
    tickets: dict[str, BracketTicket]


def _dec_text(value: Decimal) -> str:
    return format(value, "f")


def _classify_tpsl(raw: dict[str, Any]) -> str | None:
    label = str(raw.get("orderType") or raw.get("origType") or "").lower()
    if "take profit" in label or label in {"tp"}:
        return "tp"
    if "stop" in label or label in {"sl"}:
        return "sl"
    if raw.get("isPositionTpsl") and raw.get("isTrigger"):
        # Trigger without a parsed label still belongs to the bracket set.
        cond = str(raw.get("triggerCondition") or "").lower()
        if "tp" in cond or "take" in cond:
            return "tp"
        return "sl"
    return None


def parse_position(raw: dict[str, Any]) -> ExchangePosition | None:
    pos = raw.get("position", raw)
    szi = _to_decimal(pos.get("szi", "0"))
    if szi == 0:
        return None
    entry = pos.get("entryPx") or "0"
    return ExchangePosition(
        coin=str(pos.get("coin") or ""),
        szi=szi,
        entry_px=_to_decimal(entry),
    )


def parse_frontend_order(raw: dict[str, Any]) -> ExchangeOrder:
    trigger = raw.get("triggerPx")
    if trigger in (None, "", "0") and raw.get("isTrigger"):
        trigger = raw.get("limitPx")
    return ExchangeOrder(
        oid=int(raw.get("oid") or 0),
        coin=str(raw.get("coin") or ""),
        sz=_to_decimal(raw.get("sz") or "0"),
        reduce_only=bool(raw.get("reduceOnly") or raw.get("reduce_only")),
        tpsl=_classify_tpsl(raw),
        trigger_px=_to_decimal(trigger) if trigger not in (None, "") else None,
    )


def account_from_raw(
    state: dict[str, Any],
    frontend_orders: list[dict[str, Any]] | None,
    sz_decimals: dict[str, int],
) -> AccountSnapshot:
    positions: list[ExchangePosition] = []
    for item in state.get("assetPositions") or []:
        parsed = parse_position(item)
        if parsed is not None and parsed.coin:
            positions.append(parsed)
    orders = [parse_frontend_order(raw) for raw in (frontend_orders or []) if raw.get("coin")]
    return AccountSnapshot(
        positions=tuple(positions),
        orders=tuple(orders),
        sz_decimals={k.upper(): int(v) for k, v in sz_decimals.items()},
    )


def order_response_ok(resp: Any) -> bool:
    """True when an exchange ack has no top-level or per-order error."""
    if not isinstance(resp, dict):
        return True
    if resp.get("status") == "err":
        return False
    data = resp.get("response", {})
    if isinstance(data, dict):
        statuses = data.get("data", {})
        if isinstance(statuses, dict):
            statuses = statuses.get("statuses", [])
        else:
            statuses = []
        for st in statuses or []:
            if isinstance(st, dict) and st.get("error"):
                return False
    return True


def _sizes_equal(a: Decimal, b: Decimal, sz_decimals: int) -> bool:
    return quantize_size(a, sz_decimals) == quantize_size(b, sz_decimals)


def _trigger_equal(order_px: Decimal | None, want: Decimal, sz_decimals: int) -> bool:
    if order_px is None:
        return False
    return quantize_trigger_px(order_px, sz_decimals) == quantize_trigger_px(want, sz_decimals)


def _brackets_match(
    coin: str,
    abs_sz: Decimal,
    side: str,
    orders: tuple[ExchangeOrder, ...] | list[ExchangeOrder],
    ticket: BracketTicket,
    sz_decimals: int,
) -> bool:
    sl_px = quantize_trigger_px(ticket.stop, sz_decimals)
    tp_px = quantize_trigger_px(ticket.take_profit, sz_decimals)
    triggers = [
        o
        for o in orders
        if o.coin == coin and o.reduce_only and o.tpsl in {"tp", "sl"}
    ]
    if len(triggers) != 2:
        return False
    found = {"tp": False, "sl": False}
    for order in triggers:
        if not _sizes_equal(order.sz, abs_sz, sz_decimals):
            return False
        want = tp_px if order.tpsl == "tp" else sl_px
        # Long closes by selling: trigger still has to sit on the ticket tick.
        if not _trigger_equal(order.trigger_px, want, sz_decimals):
            return False
        if order.tpsl:
            found[order.tpsl] = True
    return found["tp"] and found["sl"] and side in {"long", "short"}


def _close_reason(
    side: str,
    mark: Decimal | None,
    stop: Decimal,
    tp: Decimal,
) -> str:
    if mark is None or mark <= 0:
        return "exchange_flat"
    if side == "long":
        if mark >= tp:
            return "tp"
        if mark <= stop:
            return "sl"
    else:
        if mark <= tp:
            return "tp"
        if mark >= stop:
            return "sl"
    return "exchange_flat"


def _ticket_from_open_event(ev: dict[str, Any]) -> BracketTicket | None:
    coin = str(ev.get("symbol") or "").upper()
    if not coin:
        return None
    try:
        stop = _to_decimal(ev.get("stop") if ev.get("stop") not in (None, "") else ev.get("stop_price") or "0")
        tp = _to_decimal(ev.get("tp") if ev.get("tp") not in (None, "") else ev.get("take_profit") or "0")
        entry = _to_decimal(ev.get("price") or "0")
    except Exception:
        return None
    side = str(ev.get("side") or "long")
    if side not in {"long", "short"}:
        side = "long"
    trade_id = ev.get("trade_id")
    return BracketTicket(
        coin=coin,
        side=side,
        stop=stop,
        take_profit=tp,
        entry=entry,
        trade_id=str(trade_id) if trade_id else None,
    )


class LiveBracketGuard:
    """In-memory ticket / size memory for one process."""

    def __init__(self, stop_pct: float, tp_r: float):
        self.stop_pct = _to_decimal(stop_pct)
        self.tp_r = _to_decimal(tp_r)
        self.tickets: dict[str, BracketTicket] = {}
        self.last_abs: dict[str, Decimal] = {}
        self.journaled_abs: dict[str, Decimal] = {}

    def seed_from_journal(self, rows: list[dict[str, Any]]) -> None:
        """Recover tickets from opens that never got a close.

        ``last_abs`` starts at the journaled size so a restart onto a flat
        exchange emits the missing close (TP filled while the process was down).
        """
        tickets: dict[str, BracketTicket] = {}
        sizes: dict[str, Decimal] = {}
        for ev in rows:
            event = ev.get("event")
            coin = str(ev.get("symbol") or "").upper()
            if not coin:
                continue
            if event == "open":
                ticket = _ticket_from_open_event(ev)
                if ticket is not None:
                    tickets[coin] = ticket
                try:
                    sizes[coin] = abs(_to_decimal(ev.get("size") or "0"))
                except Exception:
                    sizes[coin] = Decimal(0)
            elif event == "close":
                tickets.pop(coin, None)
                sizes[coin] = Decimal(0)
        self.tickets = tickets
        self.journaled_abs = dict(sizes)
        self.last_abs = dict(sizes)

    def note_local_open(self, ticket: BracketTicket, size: Decimal | int | str | float) -> None:
        coin = ticket.coin.upper()
        self.tickets[coin] = ticket
        sz = abs(_to_decimal(size))
        prev = self.journaled_abs.get(coin, Decimal(0))
        if sz > prev:
            self.journaled_abs[coin] = sz

    def note_local_flat(self, coin: str) -> None:
        c = coin.upper()
        self.last_abs[c] = Decimal(0)
        self.journaled_abs[c] = Decimal(0)

    def update_levels(
        self,
        coin: str,
        *,
        side: str,
        stop: Decimal | int | str | float,
        take_profit: Decimal | int | str | float,
        entry: Decimal | int | str | float,
        trade_id: str | None = None,
    ) -> None:
        c = coin.upper()
        prev = self.tickets.get(c)
        self.tickets[c] = BracketTicket(
            coin=c,
            side=side,
            stop=_to_decimal(stop),
            take_profit=_to_decimal(take_profit),
            entry=_to_decimal(entry),
            sz_decimals=prev.sz_decimals if prev else 0,
            trade_id=trade_id if trade_id is not None else (prev.trade_id if prev else None),
        )

    def commit(self, plan: ProtectionPlan) -> None:
        self.last_abs = dict(plan.next_last_abs)
        self.journaled_abs = dict(plan.next_journaled_abs)
        self.tickets = dict(plan.tickets)

    def plan(
        self,
        snapshot: AccountSnapshot,
        *,
        halt_requested: bool,
        marks: dict[str, Decimal] | None = None,
    ) -> ProtectionPlan:
        return plan_protection(
            positions=snapshot.positions,
            orders=snapshot.orders,
            tickets=self.tickets,
            last_abs=self.last_abs,
            journaled_abs=self.journaled_abs,
            halt_requested=halt_requested,
            marks=marks,
            stop_pct=self.stop_pct,
            tp_r=self.tp_r,
            sz_decimals=snapshot.sz_decimals,
        )


def plan_protection(
    *,
    positions: tuple[ExchangePosition, ...] | list[ExchangePosition],
    orders: tuple[ExchangeOrder, ...] | list[ExchangeOrder],
    tickets: dict[str, BracketTicket],
    last_abs: dict[str, Decimal],
    journaled_abs: dict[str, Decimal],
    halt_requested: bool,
    marks: dict[str, Decimal] | None = None,
    stop_pct: Decimal = Decimal("0.0015"),
    tp_r: Decimal = Decimal("2"),
    sz_decimals: dict[str, int] | None = None,
) -> ProtectionPlan:
    """Decide journals, resizes, and whether the process may stop.

    Every size increase emits ``open`` here, before the caller is allowed to
    journal a process stop. Undersized or missing reduce-only TP/SL produce a
    resize to the exact absolute position. Halt is not allowed while any
    position is naked or a non-reduce-only entry (resting Alo) could still fill.
    """
    sz_map = {k.upper(): int(v) for k, v in (sz_decimals or {}).items()}
    order_list = list(orders)
    by_coin: dict[str, ExchangePosition] = {p.coin: p for p in positions if p.abs_size > 0}
    tickets_out = dict(tickets)
    events: list[dict[str, Any]] = []
    resizes: list[ResizeRequest] = []
    naked: list[str] = []
    next_last: dict[str, Decimal] = {}
    next_journaled = dict(journaled_abs)

    coins = set(by_coin) | {c.upper() for c in last_abs if last_abs[c] > 0}
    for coin in sorted(coins):
        pos = by_coin.get(coin)
        abs_sz = pos.abs_size if pos is not None else Decimal(0)
        prev = last_abs.get(coin, Decimal(0))
        declared = sz_map.get(coin)
        sz_dec = declared if declared is not None else _infer_sz_decimals(abs_sz if abs_sz > 0 else prev)
        side = pos.side if pos is not None else (tickets_out.get(coin).side if coin in tickets_out else "long")
        entry = pos.entry_px if pos is not None and pos.entry_px > 0 else Decimal(0)
        ticket = _resolve_ticket(
            coin,
            side,
            entry,
            tickets_out.get(coin),
            stop_pct,
            tp_r,
            sz_dec,
        )
        if ticket is not None:
            tickets_out[coin] = ticket

        journaled = next_journaled.get(coin, Decimal(0))
        if abs_sz > journaled:
            # Growth (first partial or a later drip) — open before any halt.
            events.append(
                _open_event(
                    coin,
                    side,
                    abs_sz,
                    abs_sz - journaled,
                    ticket,
                    entry,
                )
            )
            next_journaled[coin] = abs_sz
        elif 0 < abs_sz < journaled:
            # Position is smaller than the size we already journaled (partial
            # vs intent, or a scale-out). Track live size so the next drip emits.
            next_journaled[coin] = abs_sz
        elif abs_sz == 0 and prev > 0:
            mark = None
            if marks and coin in marks:
                mark = _to_decimal(marks[coin])
            reason = "exchange_flat"
            if ticket is not None:
                side = ticket.side
                reason = _close_reason(side, mark, ticket.stop, ticket.take_profit)
            events.append(
                {
                    "event": "close",
                    "symbol": coin,
                    "side": side,
                    "size": _dec_text(prev),
                    "price": _dec_text(mark) if mark is not None else None,
                    "reason": reason,
                    "trade_id": ticket.trade_id if ticket else None,
                    "stop": _dec_text(ticket.stop) if ticket else None,
                    "tp": _dec_text(ticket.take_profit) if ticket else None,
                }
            )
            next_journaled[coin] = Decimal(0)
            tickets_out.pop(coin, None)

        next_last[coin] = abs_sz
        if abs_sz > 0:
            matched = (
                ticket is not None
                and levels_valid(ticket.side, ticket.entry if ticket.entry > 0 else entry, ticket.stop, ticket.take_profit)
                and _brackets_match(coin, abs_sz, ticket.side, order_list, ticket, sz_dec)
            )
            if not matched:
                naked.append(coin)
                if ticket is not None and _can_resize(ticket, entry):
                    sized = quantize_size(abs_sz, sz_dec)
                    if sized > 0:
                        resizes.append(
                            ResizeRequest(
                                coin=coin,
                                side=ticket.side,
                                size=sized,
                                stop_px=quantize_trigger_px(ticket.stop, sz_dec),
                                take_profit_px=quantize_trigger_px(ticket.take_profit, sz_dec),
                                sz_decimals=sz_dec,
                                cancel_oids=tuple(
                                    o.oid
                                    for o in order_list
                                    if o.coin == coin and o.reduce_only and o.tpsl in {"tp", "sl"}
                                ),
                                existing_legs=tuple(
                                    (o.oid, o.tpsl)
                                    for o in order_list
                                    if o.coin == coin
                                    and o.reduce_only
                                    and o.tpsl in {"tp", "sl"}
                                ),
                            )
                        )

    # Preserve last_abs for coins we didn't touch.
    for coin, sz in last_abs.items():
        next_last.setdefault(coin, sz)

    entry_orders: list[tuple[str, int]] = []
    if halt_requested:
        for order in order_list:
            if not order.reduce_only and order.tpsl is None:
                entry_orders.append((order.coin, order.oid))

    allow = not naked and not entry_orders
    return ProtectionPlan(
        journal_events=tuple(events),
        resizes=tuple(resizes),
        cancel_entry_orders=tuple(entry_orders),
        naked_coins=tuple(naked),
        allow_process_stop=allow,
        block_new_entries=bool(naked),
        next_last_abs=next_last,
        next_journaled_abs=next_journaled,
        tickets=tickets_out,
    )


def _infer_sz_decimals(sz: Decimal) -> int:
    if sz == 0:
        return 0
    exp = sz.normalize().as_tuple().exponent
    if isinstance(exp, int) and exp < 0:
        return -exp
    return 0


def _can_resize(ticket: BracketTicket, entry: Decimal) -> bool:
    ref = ticket.entry if ticket.entry > 0 else entry
    return levels_valid(ticket.side, ref, ticket.stop, ticket.take_profit)


def _resolve_ticket(
    coin: str,
    side: str,
    entry: Decimal,
    ticket: BracketTicket | None,
    stop_pct: Decimal,
    tp_r: Decimal,
    sz_decimals: int,
) -> BracketTicket | None:
    if ticket is not None and ticket.side == side and _can_resize(ticket, entry):
        if ticket.sz_decimals != sz_decimals or (ticket.entry <= 0 and entry > 0):
            return BracketTicket(
                coin=coin,
                side=ticket.side,
                stop=ticket.stop,
                take_profit=ticket.take_profit,
                entry=ticket.entry if ticket.entry > 0 else entry,
                sz_decimals=sz_decimals,
                trade_id=ticket.trade_id,
            )
        return ticket
    if entry <= 0:
        return ticket
    try:
        stop, tp = recompute_brackets(side, entry, stop_pct, tp_r)
    except ValueError:
        return ticket
    return BracketTicket(
        coin=coin,
        side=side,
        stop=stop,
        take_profit=tp,
        entry=entry,
        sz_decimals=sz_decimals,
        trade_id=ticket.trade_id if ticket else None,
    )


def _open_event(
    coin: str,
    side: str,
    abs_sz: Decimal,
    fill_sz: Decimal,
    ticket: BracketTicket | None,
    entry: Decimal,
) -> dict[str, Any]:
    price = entry
    if price <= 0 and ticket is not None:
        price = ticket.entry
    return {
        "event": "open",
        "symbol": coin,
        "side": side if ticket is None else ticket.side,
        "size": _dec_text(abs_sz),
        "fill_sz": _dec_text(fill_sz),
        "price": _dec_text(price) if price > 0 else None,
        "stop": _dec_text(ticket.stop) if ticket else None,
        "tp": _dec_text(ticket.take_profit) if ticket else None,
        "trade_id": ticket.trade_id if ticket else None,
        "reason": "live_fill",
    }

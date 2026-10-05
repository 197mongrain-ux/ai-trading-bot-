"""Model 3 ticket arming.

A Model 3 SIGNAL is sent only when the stop sits beyond obvious liquidity
(sweep / SSL / BSL plus buffer) and the risk distance is wide enough to
size. Tight noise stops are not a fallback, and a stop already on the
book is never moved closer to entry.

``MODEL3_MIN_SCORE`` stays at 7. Volume (``HEAVY`` / ``VOL_OK``) is a tag
only and never a reason to pass.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

MODEL3_MIN_SCORE = 7

# Default and floor. A requested buffer below this is raised to 2 bps.
MODEL3_STOP_LIQ_BUFFER_BPS = 2

# 0.2% stub. Reference for the noise zone only — never an arm fallback.
MODEL3_STUB_STOP_PCT = 0.002

# Risk-fixed sizing skips stops inside ~0.5% of entry.
MODEL3_MIN_STOP_PCT = 0.005

# Model 3 ticket R. Does not change the VWAP ``TP_R_MULTIPLE`` default.
MODEL3_TP_R = 3.0

VOLUME_HEAVY = "HEAVY"
VOLUME_OK = "VOL_OK"


def resolve_stop_liq_buffer_bps(requested: float | None = None) -> float:
    """Buffer in bps. Default 2. Never below the floor of 2.

    When ``requested`` is omitted, ``MODEL3_STOP_LIQ_BUFFER_BPS`` from the
    environment is used if it is set, then raised to the floor.
    """
    if requested is None:
        raw = os.getenv("MODEL3_STOP_LIQ_BUFFER_BPS")
        if raw is not None and raw.strip():
            requested = float(raw)
        else:
            requested = float(MODEL3_STOP_LIQ_BUFFER_BPS)
    return max(float(MODEL3_STOP_LIQ_BUFFER_BPS), float(requested))


def _finite_pos(value: float | None) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def protective_distance(side: str, entry: float, stop: float | None) -> float | None:
    """Price distance of a stop that actually protects ``side``. None if invalid."""
    if not _finite_pos(entry) or not _finite_pos(stop):
        return None
    assert stop is not None
    if side == "long" and stop < entry:
        return entry - stop
    if side == "short" and stop > entry:
        return stop - entry
    return None


def stop_distance_pct(side: str, entry: float, stop: float | None) -> float | None:
    dist = protective_distance(side, entry, stop)
    if dist is None or entry <= 0:
        return None
    return dist / entry


def _is_tighter(side: str, entry: float, proposed: float, current: float) -> bool:
    """True when ``proposed`` is closer to entry than ``current``."""
    new = protective_distance(side, entry, proposed)
    old = protective_distance(side, entry, current)
    if new is None:
        return True
    if old is None:
        return False
    return new + entry * 1e-12 < old


def place_stop_beyond_liquidity(
    side: str,
    entry: float,
    liquidity_px: float,
    stop_liq_buffer_bps: float | None = None,
) -> float | None:
    """Stop just beyond sweep / SSL / BSL by the buffer.

    Long: liquidity is below entry (SSL or sweep low); stop is further below.
    Short: liquidity is above entry (BSL or sweep high); stop is further above.
    Returns None when that stop cannot sit on the protective side of entry.
    """
    side = (side or "").lower()
    if side not in {"long", "short"} or not _finite_pos(entry) or not _finite_pos(liquidity_px):
        return None
    buf = resolve_stop_liq_buffer_bps(stop_liq_buffer_bps) / 10_000.0
    if side == "long":
        if liquidity_px >= entry:
            return None
        stop = liquidity_px * (1.0 - buf)
        if stop <= 0 or stop >= entry:
            return None
        return stop
    if liquidity_px <= entry:
        return None
    stop = liquidity_px * (1.0 + buf)
    if stop <= entry:
        return None
    return stop


def stop_beyond_liquidity_ok(
    side: str,
    entry: float,
    stop: float,
    liquidity_px: float,
    stop_liq_buffer_bps: float | None = None,
) -> bool:
    """True when ``stop`` is beyond the liquidity price by at least the buffer."""
    side = (side or "").lower()
    if not _finite_pos(entry) or not _finite_pos(stop) or not _finite_pos(liquidity_px):
        return False
    buf = resolve_stop_liq_buffer_bps(stop_liq_buffer_bps) / 10_000.0
    if side == "long":
        if not (stop < entry and liquidity_px < entry):
            return False
        beyond = liquidity_px * (1.0 - buf)
        return stop <= beyond * (1.0 + 1e-12)
    if side == "short":
        if not (stop > entry and liquidity_px > entry):
            return False
        beyond = liquidity_px * (1.0 + buf)
        return stop >= beyond * (1.0 - 1e-12)
    return False


@dataclass(frozen=True)
class StopFit:
    """Result of fitting a structural stop. ``stop`` is set only when armable."""

    action: str  # "stop" | "pass"
    reason: str
    stop: float | None = None


def fit_stop(
    *,
    side: str,
    entry: float,
    liquidity_px: float,
    stop_liq_buffer_bps: float | None = None,
    current_stop: float | None = None,
    stub_pct: float = MODEL3_STUB_STOP_PCT,
    min_stop_pct: float = MODEL3_MIN_STOP_PCT,
) -> StopFit:
    """Fit the stop that may be armed.

    Does not fall back to the 0.2% stub. A structural price inside that
    noise zone, or closer than the stop already on the book, is a pass.
    Stops inside ``min_stop_pct`` (~0.5%) are a pass so sizing cannot
    balloon notional.
    """
    side = (side or "").lower()
    if side not in {"long", "short"} or not _finite_pos(entry) or not _finite_pos(liquidity_px):
        return StopFit("pass", "invalid_prices")

    beyond = place_stop_beyond_liquidity(
        side, entry, liquidity_px, stop_liq_buffer_bps=stop_liq_buffer_bps
    )
    if beyond is None or not stop_beyond_liquidity_ok(
        side, entry, beyond, liquidity_px, stop_liq_buffer_bps=stop_liq_buffer_bps
    ):
        return StopFit("pass", "stop_not_beyond_liq")

    pct = stop_distance_pct(side, entry, beyond)
    if pct is None:
        return StopFit("pass", "stop_not_beyond_liq")
    if pct < stub_pct:
        return StopFit("pass", "structural_inside_noise")
    if current_stop is not None and _is_tighter(side, entry, beyond, current_stop):
        return StopFit("pass", "refuse_tighten")
    if pct < min_stop_pct:
        return StopFit("pass", "stop_inside_half_pct")
    return StopFit("stop", "beyond_liq", stop=beyond)


# Name used by the desk's stop-fit path.
stop_fit = fit_stop


@dataclass(frozen=True)
class StopUpdate:
    """Post-fill bracket update. Never carries a stop closer than the one resting."""

    action: str  # "update" | "keep" | "drop"
    reason: str
    stop: float | None = None

    @property
    def drop_trade(self) -> bool:
        return self.action == "drop"


def apply_bracket_stop(
    *,
    side: str,
    entry: float,
    liquidity_px: float,
    current_stop: float | None = None,
    stop_liq_buffer_bps: float | None = None,
    stub_pct: float = MODEL3_STUB_STOP_PCT,
    min_stop_pct: float = MODEL3_MIN_STOP_PCT,
) -> StopUpdate:
    """Attach or replace a stop after a fill.

    Widening to a stop beyond liquidity is allowed. A structural price
    inside the noise zone, or any proposed stop closer than ``current_stop``,
    drops the update and leaves the wider resting stop in place.
    """
    fit = fit_stop(
        side=side,
        entry=entry,
        liquidity_px=liquidity_px,
        stop_liq_buffer_bps=stop_liq_buffer_bps,
        current_stop=current_stop,
        stub_pct=stub_pct,
        min_stop_pct=min_stop_pct,
    )
    if fit.action == "stop" and fit.stop is not None:
        if current_stop is not None and not _is_tighter(side, entry, fit.stop, current_stop):
            old = protective_distance(side, entry, current_stop)
            new = protective_distance(side, entry, fit.stop)
            if old is not None and new is not None and abs(new - old) <= entry * 1e-9:
                return StopUpdate("keep", fit.reason, stop=current_stop)
            if old is not None and new is not None and new > old:
                return StopUpdate("update", fit.reason, stop=fit.stop)
            if old is not None:
                return StopUpdate("keep", fit.reason, stop=current_stop)
        return StopUpdate("update", fit.reason, stop=fit.stop)

    if current_stop is not None and protective_distance(side, entry, current_stop) is not None:
        return StopUpdate("drop", fit.reason, stop=current_stop)
    return StopUpdate("drop", fit.reason, stop=None)


@dataclass(frozen=True)
class SizeDecision:
    allowed: bool
    reason: str
    size: float = 0.0
    dollar_risk: float = 0.0
    notional: float = 0.0


def size_for_real_stop(
    *,
    entry: float,
    stop: float,
    dollar_risk: float,
    side: str | None = None,
    min_stop_pct: float = MODEL3_MIN_STOP_PCT,
) -> SizeDecision:
    """Risk-fixed size from the real stop. Skip when the stop is inside ~0.5%.

    Size is ``dollar_risk / stop_distance``. A stop inside ``min_stop_pct``
    returns size 0 instead of the multi-thousand notional that tiny stops
    produce (taker fees then consume the risk budget).
    """
    if not _finite_pos(entry) or not _finite_pos(stop) or not _finite_pos(dollar_risk):
        return SizeDecision(False, "invalid_prices")
    if side is None:
        if stop < entry:
            side = "long"
        elif stop > entry:
            side = "short"
        else:
            return SizeDecision(False, "stop distance is zero")
    dist = protective_distance(side, entry, stop)
    if dist is None or dist <= 0:
        return SizeDecision(False, "stop distance is zero")
    pct = dist / entry
    if pct < min_stop_pct:
        return SizeDecision(False, "stop_inside_half_pct", dollar_risk=dollar_risk)
    size = dollar_risk / dist
    if size <= 0:
        return SizeDecision(False, "computed size <= 0", dollar_risk=dollar_risk)
    return SizeDecision(
        True,
        "ok",
        size=size,
        dollar_risk=dollar_risk,
        notional=size * entry,
    )


@dataclass(frozen=True)
class Model3Ticket:
    symbol: str
    side: str
    entry: float
    stop: float
    take_profit: float
    stop_pct: float
    size: float | None
    dollar_risk: float
    score: int
    volume_tag: str
    window_end: float
    trade_id: str | None
    action: str = "SIGNAL"


@dataclass(frozen=True)
class Model3Decision:
    action: str  # "SIGNAL" | "PASS"
    reason: str
    ticket: Model3Ticket | None = None
    stop: float | None = None
    volume_tag: str = ""

    @property
    def send_signal(self) -> bool:
        return self.action == "SIGNAL" and self.ticket is not None


def _pass(reason: str, volume_tag: str) -> Model3Decision:
    return Model3Decision("PASS", reason, ticket=None, stop=None, volume_tag=volume_tag)


def _tp(side: str, entry: float, stop: float, tp_r: float) -> float:
    dist = abs(entry - stop)
    if side == "long":
        return entry + dist * tp_r
    return entry - dist * tp_r


@dataclass
class _Leg:
    trade_id: str
    price: float
    window_end: float
    stop: float | None = None
    stopped: bool = False


def _same_price(a: float, b: float) -> bool:
    if not _finite_pos(a) or not _finite_pos(b):
        return False
    return abs(a - b) <= max(a, b) * 1e-8


class ThesisBook:
    """One Model 3 thesis per coin for the rest of the ticket window.

    After a stop or loss close, a new entry on that symbol is blocked until
    ``window_end``. A partial that grows the same ``trade_id`` at the same
    price is not a new thesis. A different price on that id is averaging
    down and is blocked. The 120s entry cooldown is not this window.
    """

    def __init__(self) -> None:
        self._open: dict[str, _Leg] = {}
        self._blocked_until: dict[str, float] = {}

    def note_open(
        self,
        symbol: str,
        trade_id: str,
        price: float,
        window_end: float,
        stop: float | None = None,
    ) -> None:
        self._open[symbol.upper()] = _Leg(
            trade_id=trade_id,
            price=price,
            window_end=window_end,
            stop=stop,
        )

    def note_stop(
        self,
        symbol: str,
        *,
        now: float,
        window_end: float | None = None,
    ) -> None:
        """Block new entries until the ticket window ends.

        ``now`` records when the stop fired. The block does not expire
        120s later; it lasts through ``window_end``.
        """
        _ = now
        sym = symbol.upper()
        leg = self._open.get(sym)
        end = window_end if window_end is not None else (leg.window_end if leg else None)
        if end is None:
            return
        prev = self._blocked_until.get(sym)
        self._blocked_until[sym] = end if prev is None else max(prev, end)
        if leg is not None:
            leg.stopped = True

    def open_leg(self, symbol: str) -> _Leg | None:
        return self._open.get(symbol.upper())

    def blocked_until(self, symbol: str) -> float | None:
        return self._blocked_until.get(symbol.upper())

    def allow_entry(
        self,
        symbol: str,
        *,
        now: float,
        trade_id: str | None,
        price: float,
    ) -> tuple[bool, str]:
        sym = symbol.upper()
        leg = self._open.get(sym)
        if leg is not None and trade_id and trade_id == leg.trade_id:
            if _same_price(price, leg.price):
                return True, "same_trade_partial"
            return False, "averaging_down"
        until = self._blocked_until.get(sym)
        if until is not None and now < until:
            return False, "one_thesis_window"
        return True, "ok"


def build_ticket(
    *,
    symbol: str,
    side: str,
    entry: float,
    liquidity_px: float,
    score: int,
    window_end: float,
    volume_tag: str = VOLUME_OK,
    trade_id: str | None = None,
    current_stop: float | None = None,
    stop_liq_buffer_bps: float | None = None,
    tp_r: float = MODEL3_TP_R,
    book: ThesisBook | None = None,
    now: float | None = None,
    dollar_risk: float | None = None,
    min_score: int = MODEL3_MIN_SCORE,
    stub_pct: float = MODEL3_STUB_STOP_PCT,
    min_stop_pct: float = MODEL3_MIN_STOP_PCT,
) -> Model3Decision:
    """Arm a Model 3 ticket or PASS.

    PASS (no SIGNAL) when the stop cannot clear liquidity, when the
    structural stop sits inside the 0.2% noise zone, when arming would
    tighten a wider stop, when the distance is inside ~0.5%, or when this
    symbol is still inside a stopped thesis window. The 0.2% stub is never
    substituted in.
    """
    tag = (volume_tag or VOLUME_OK).strip().upper() or VOLUME_OK
    side_n = (side or "").lower()
    if score < min_score:
        return _pass("score_below_min", tag)

    if book is not None:
        now_ts = time.time() if now is None else now
        allowed, why = book.allow_entry(
            symbol,
            now=now_ts,
            trade_id=trade_id,
            price=entry,
        )
        if not allowed:
            return _pass(why, tag)
        if why == "same_trade_partial":
            leg = book.open_leg(symbol)
            # Same trade_id at the same price. Keep the resting stop so a
            # tighter structural price cannot shrink it, and so a stop
            # inside ~0.5% is not resized into a large notional.
            kept = leg.stop if leg is not None and leg.stop is not None else current_stop
            return _ticket_from_stop(
                symbol=symbol,
                side=side_n,
                entry=entry,
                stop=kept,
                score=score,
                volume_tag=tag,
                window_end=window_end,
                trade_id=trade_id,
                tp_r=tp_r,
                dollar_risk=dollar_risk,
                min_stop_pct=min_stop_pct,
                reason="same_trade_partial",
            )

    fit = fit_stop(
        side=side_n,
        entry=entry,
        liquidity_px=liquidity_px,
        stop_liq_buffer_bps=stop_liq_buffer_bps,
        current_stop=current_stop,
        stub_pct=stub_pct,
        min_stop_pct=min_stop_pct,
    )
    if fit.action != "stop" or fit.stop is None:
        return _pass(fit.reason, tag)
    return _ticket_from_stop(
        symbol=symbol,
        side=side_n,
        entry=entry,
        stop=fit.stop,
        score=score,
        volume_tag=tag,
        window_end=window_end,
        trade_id=trade_id,
        tp_r=tp_r,
        dollar_risk=dollar_risk,
        min_stop_pct=min_stop_pct,
        reason=fit.reason,
    )


def _ticket_from_stop(
    *,
    symbol: str,
    side: str,
    entry: float,
    stop: float | None,
    score: int,
    volume_tag: str,
    window_end: float,
    trade_id: str | None,
    tp_r: float,
    dollar_risk: float | None,
    min_stop_pct: float,
    reason: str,
) -> Model3Decision:
    pct = stop_distance_pct(side, entry, stop)
    if stop is None or pct is None:
        return _pass("stop_not_beyond_liq", volume_tag)
    if pct < min_stop_pct:
        return _pass("stop_inside_half_pct", volume_tag)
    size: float | None = None
    risk = 0.0
    if dollar_risk is not None:
        sized = size_for_real_stop(
            entry=entry,
            stop=stop,
            dollar_risk=dollar_risk,
            side=side,
            min_stop_pct=min_stop_pct,
        )
        if not sized.allowed:
            return _pass(sized.reason, volume_tag)
        size = sized.size
        risk = sized.dollar_risk
    ticket = Model3Ticket(
        symbol=symbol.upper(),
        side=side,
        entry=entry,
        stop=stop,
        take_profit=_tp(side, entry, stop, tp_r),
        stop_pct=pct,
        size=size,
        dollar_risk=risk,
        score=score,
        volume_tag=volume_tag,
        window_end=window_end,
        trade_id=trade_id,
        action="SIGNAL",
    )
    return Model3Decision("SIGNAL", reason, ticket=ticket, stop=stop, volume_tag=volume_tag)


def heal_stop_tp(
    side: str,
    entry: float,
    existing_stop: float | None,
    existing_tp: float | None,
    *,
    stop_pct: float,
    tp_r: float,
) -> tuple[float | None, float | None]:
    """Keep a valid stop/TP that is already at least as wide as ``stop_pct``.

    Heal must not replace that wider pair with brackets recomputed from the
    default ``stop_pct``. A missing stop can still be filled from
    ``stop_pct``. A wider stop with a missing TP gets a target from the
    existing stop distance, not from the tighter default.
    """
    side_n = (side or "").lower()
    if side_n not in {"long", "short"} or not _finite_pos(entry) or stop_pct <= 0 or tp_r <= 0:
        return existing_stop, existing_tp

    default_dist = entry * stop_pct
    if side_n == "long":
        default_stop = entry - default_dist
        default_tp = entry + default_dist * tp_r
    else:
        default_stop = entry + default_dist
        default_tp = entry - default_dist * tp_r

    existing_dist = protective_distance(side_n, entry, existing_stop)
    if existing_dist is not None and existing_dist + entry * 1e-12 >= default_dist:
        tp = existing_tp
        if not _tp_valid(side_n, entry, existing_tp):
            if side_n == "long":
                tp = entry + existing_dist * tp_r
            else:
                tp = entry - existing_dist * tp_r
        return existing_stop, tp
    return default_stop, default_tp


def _tp_valid(side: str, entry: float, tp: float | None) -> bool:
    if not _finite_pos(entry) or not _finite_pos(tp):
        return False
    assert tp is not None
    if side == "long":
        return tp > entry
    if side == "short":
        return tp < entry
    return False


def alo_cancel_oids(orders: list[dict] | tuple[dict, ...]) -> list[int]:
    """Entry Alo oids safe to cancel. Reduce-only TP/SL brackets are omitted."""
    cancel: list[int] = []
    for order in orders:
        if _is_protected_bracket(order):
            continue
        oid = order.get("oid")
        if oid is None:
            continue
        cancel.append(int(oid))
    return cancel


def _is_protected_bracket(order: dict) -> bool:
    if bool(order.get("reduce_only") or order.get("reduceOnly")):
        return True
    label = str(
        order.get("tpsl") or order.get("orderType") or order.get("origType") or ""
    ).lower()
    if label in {"tp", "sl"}:
        return True
    if "take profit" in label or "stop" in label:
        return True
    return False

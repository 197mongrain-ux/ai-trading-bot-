"""Model B risk. Fixed policy — not the scalp 0.5% / VWAP stop.

- The stop is placed past opposing liquidity, then the order is sized.
  A real wick, swing, or local extreme more than one tick past the fill
  owns the stop. Buffer past that print is ``max(3 ticks, 2 bps,
  0.5×ATR14)``. A print inside the old 0.15% room is kept; it is not
  lifted onto that floor.
- When nothing sits more than one tick past the fill, the 3-tick / 2 bp
  buffer is still inside wick room. The stop then clears the next
  opposing print outside that room, or the room itself plus the same
  3-tick / 2 bp pad. It is not left a few ticks off the fill.
- A distance past 1.5% of price is still armed. Size shrinks with the
  distance (``size_adjust=wide_stop`` on the journal). The stop is not
  tightened back into the wick to make 1.5%.
- Size = spot USDC × ``RISK_PER_TRADE`` / that stop distance. Model B
  requires the fraction to be 0.02. Wider stop, smaller size. Notional
  is capped at the coin's max leverage times that balance (20× when the
  coin max is unknown). A very tight stop is trimmed. Dollar risk never
  goes above the 2% target.
- ``BAD_STOP`` is only impossible geometry: wrong side of the fill,
  stop == fill, or a one-tick collision (LIT). A size that rounds to
  zero is the same fail-closed.
- Env ``LEVERAGE`` stays 20. Margin uses the exchange max for that coin.
- TP is the next liquidity in the trade direction (confirmed swing or
  untaken pool) that clears the round-trip fee and at least 1R of the
  stop. A closer pool is skipped. Past 2R is still that level. The
  1.5R default, clamped to [1, 2], is the fallback when no level clears
  the band. If that fallback cannot clear it either, the arm fails.
- Heal keeps the wider stop. A tighter proposal does not overwrite it.
- Soft-prop off. Strategy kill off. Flow is not an exit. No market fallback.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Locked Model B fraction. Sizing reads the caller's risk_pct (from
# RISK_PER_TRADE). This constant is the value that setting must be.
MODEL_B_RISK_PCT = 0.02
# Chris's absolute rule: no open position may ever lose more than 2% of the
# account. Ticket sizing, the guard's LOSS_KILL and its cap stop all use this.
# Settings may lower it (MODEL_B_MAX_LOSS_PCT) but never raise it.
HARD_MAX_LOSS_PCT = 0.02
RISK_PCT = MODEL_B_RISK_PCT
LEVERAGE = 20
DEFAULT_TP_R = 1.5
TP_R_MIN = 1.0
TP_R_MAX = 2.0
STOP_PAST_EXTREME_TICKS = 1.0
# Room past the liquidity anchor. The ATR term is skipped when ATR14 is
# not available (fewer than 14 true ranges).
STOP_BUFFER_TICKS = 3.0
STOP_BUFFER_BPS = 2.0
STOP_ATR_FRACTION = 0.5
# Minimum distance the structural stop must already have cleared.
# Fees at or under half an R: dist * 0.5 >= price * round-trip fee.
STOP_FLOOR_TICKS = 10.0
STOP_FLOOR_PCT = 0.0015
STOP_FEE_R = 0.5
# Journal threshold, not a reject. A structural stop wider than this is
# armed at a smaller size (``size_adjust=wide_stop``). Do not pull the
# stop back up into the wick to fit 1.5%.
STOP_CAP_PCT = 0.015
SIZE_ADJUST_WIDE_STOP = "wide_stop"
# Hyperliquid base tier: maker 1.5 bps, taker 4.5 bps.
# https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
MAKER_FEE_RATE = 0.00015
TAKER_FEE_RATE = 0.00045
ROUND_TRIP_FEE_RATE = MAKER_FEE_RATE + TAKER_FEE_RATE

SOFT_PROP_ENABLED = False
STRATEGY_KILL_ENABLED = False
FLOW_EXIT_ENABLED = False
MARKET_FALLBACK_ENABLED = False


def assert_policy() -> None:
    if SOFT_PROP_ENABLED or STRATEGY_KILL_ENABLED or FLOW_EXIT_ENABLED or MARKET_FALLBACK_ENABLED:
        raise RuntimeError("Model B policy flags must stay off")
    if LEVERAGE != 20 or RISK_PCT != 0.02:
        raise RuntimeError("Model B risk is 2% at 20x only")
    if HARD_MAX_LOSS_PCT > 0.02 or RISK_PCT > HARD_MAX_LOSS_PCT:
        raise RuntimeError("Model B loss cap is 2% of the account per position")


def assert_leverage(leverage: int) -> int:
    """Env ``LEVERAGE`` stays 20. Per-coin margin does not use this check."""
    if int(leverage) != LEVERAGE:
        raise ValueError(f"Model B is 20x only (40x off); got {leverage}")
    return LEVERAGE


def usable_leverage(coin_max: int | None, cap: int | None = None) -> int:
    """Leverage for margin and the notional cap.

    A missing coin max falls back to 20 so the ticket is never sized for
    a leverage the exchange was not told to use. ``cap``
    (``MODEL_B_MAX_LEVERAGE``) only lowers that max. It never raises it.
    """
    base = LEVERAGE
    if coin_max is not None:
        try:
            parsed = int(coin_max)
        except (TypeError, ValueError):
            parsed = 0
        if parsed >= 1:
            base = parsed
    if cap is not None:
        try:
            cap_i = int(cap)
        except (TypeError, ValueError):
            cap_i = 0
        if cap_i >= 1:
            base = min(base, cap_i)
    return base


def _margin_leverage(leverage: int) -> int:
    try:
        lev = int(leverage)
    except (TypeError, ValueError):
        return LEVERAGE
    return lev if lev >= 1 else LEVERAGE


def clamp_tp_r(tp_r: float) -> float:
    return min(TP_R_MAX, max(TP_R_MIN, float(tp_r)))


def stop_beyond_extreme(side: str, extreme: float, tick: float) -> float:
    """One tick past the sweep extreme, away from the entry.

    This is the old collision (stop == fill on a fine book). It is not a
    valid Model B stop. Callers use it to show the price that must be rejected.
    """
    if side == "long":
        return extreme - STOP_PAST_EXTREME_TICKS * tick
    return extreme + STOP_PAST_EXTREME_TICKS * tick


def stop_buffer(entry: float, tick: float, atr: float | None = None) -> float:
    """``max(3 ticks, 2 bps, 0.5×ATR14)`` past the liquidity anchor."""
    if entry <= 0 or tick <= 0:
        return 0.0
    room = max(STOP_BUFFER_TICKS * tick, abs(entry) * STOP_BUFFER_BPS / 10_000.0)
    if atr is not None and float(atr) > 0:
        room = max(room, STOP_ATR_FRACTION * float(atr))
    return room


def floor_distance(entry: float, tick: float) -> float:
    """Old minimum distance. A structural stop inside it still arms.

    ``max(10 ticks, 0.15% of entry, the distance that keeps a maker+taker
    round trip inside 0.5R)``. This is not where the stop is parked, and
    a structural stop inside it is still armed.
    """
    if entry <= 0 or tick <= 0:
        return 0.0
    fee_dist = abs(entry) * ROUND_TRIP_FEE_RATE / STOP_FEE_R
    return max(STOP_FLOOR_TICKS * tick, abs(entry) * STOP_FLOOR_PCT, fee_dist)


def min_stop_distance(entry: float, tick: float, tp_r: float = DEFAULT_TP_R) -> float:
    """Old fee/tick floor. ``place_stop`` does not reject inside it.

    Kept so a caller can compare a structural distance to that floor.
    ``tp_r`` is accepted so older callers keep working. The floor does not
    shrink when R is raised: the fee term is capped at 0.5R, not at the
    TP multiple.
    """
    del tp_r
    return floor_distance(entry, tick)


def stop_clears_fees(
    entry: float,
    stop: float,
    *,
    tick: float = 0.0,
    tp_r: float = DEFAULT_TP_R,
) -> bool:
    """True when ``tp_r`` × stop distance covers the round-trip fee on ``entry``."""
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return False
    if tick > 0 and dist + 1e-12 < tick * (1 - 1e-9):
        return False
    r = clamp_tp_r(tp_r)
    return dist * r + 1e-9 >= abs(entry) * ROUND_TRIP_FEE_RATE


def _level(price: float | None) -> float | None:
    if price is None:
        return None
    px = float(price)
    if px <= 0:
        return None
    return px


def liquidity_anchor(
    side: str,
    extreme: float,
    entry: float,
    swing: float | None = None,
    local_extreme: float | None = None,
) -> float:
    """Furthest liquidity against the trade, not through the fill.

    Long anchor is the lowest of the sweep wick, the swing, and the local
    bar extreme, and never above the entry. Short is the mirror. A swing
    that the sweep already traded through (long swing above the Alo) does
    not pull the anchor back toward price.
    """
    levels = [entry]
    for price in (extreme, swing, local_extreme):
        px = _level(price)
        if px is not None:
            levels.append(px)
    if side == "long":
        return min(levels)
    return max(levels)


def _snap_away(side: str, price: float, tick: float) -> float:
    if side == "long":
        return math.floor(price / tick + 1e-9) * tick
    return math.ceil(price / tick - 1e-9) * tick


def _beyond_tick(price: float, entry: float, tick: float, *, above: bool) -> bool:
    """True when ``price`` is more than one tick past ``entry``."""
    gap = float(tick) * (1.0 + 1e-9)
    if above:
        return float(price) > float(entry) + gap
    return float(price) < float(entry) - gap


def extension_pad(entry: float, tick: float) -> float:
    """Pad past the next opposing print. ATR is not added a second time."""
    if entry <= 0 or tick <= 0:
        return 0.0
    return max(STOP_BUFFER_TICKS * tick, abs(entry) * STOP_BUFFER_BPS / 10_000.0)


def next_opposing_level(
    side: str,
    entry: float,
    room: float,
    further: Sequence[float] | None,
) -> float | None:
    """Nearest stop-side print strictly outside ``room``.

    Long: the highest low still below ``entry - room``. Short: the lowest
    high still above ``entry + room``. Prints inside the room are skipped
    so a shallow wick is not treated as the level that clears it.
    """
    if not further or entry <= 0 or side not in ("long", "short"):
        return None
    best: float | None = None
    for raw in further:
        px = _level(raw)
        if px is None:
            continue
        if side == "long":
            if float(entry) - px <= float(room) + 1e-9:
                continue
            if best is None or px > best:
                best = px
        else:
            if px - float(entry) <= float(room) + 1e-9:
                continue
            if best is None or px < best:
                best = px
    return best


def min_tp_distance(
    entry: float,
    stop: float,
    *,
    min_r: float = TP_R_MIN,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> float:
    """Closest target that still pays: at least ``min_r`` and the round-trip fee.

    ``min_r`` defaults to 1. The fee is the maker entry plus the taker
    exit on ``entry``. A pool inside the larger of those two is not a target.
    ``maker_fee`` / ``taker_fee`` default to the published base tier.
    A coin that pays more (xyz outside growth mode) passes its own rates
    so the floor moves with the fee. Prices are not moved here.
    """
    dist = abs(float(entry) - float(stop))
    maker = MAKER_FEE_RATE if maker_fee is None else float(maker_fee)
    taker = TAKER_FEE_RATE if taker_fee is None else float(taker_fee)
    fee = abs(float(entry)) * (maker + taker)
    return max(float(min_r) * dist, fee)


def next_liquidity(
    side: str,
    entry: float,
    tick: float,
    levels: Sequence[float] | None,
    *,
    stop: float | None = None,
    min_dist: float | None = None,
) -> float | None:
    """Nearest supportive level past the entry that clears the TP floor.

    Long wants the lowest level still above the fill. Short wants the
    highest level still below it. The caller passes confirmed swings in
    the trade direction and untaken pools on that side. A bar wick is
    not a target. One tick off the fill is noise.

    ``min_dist`` (or ``stop``, which builds that floor from 1R and fees)
    skips a pool that would not clear the band, so the next real level
    can win. Without either, only the one-tick filter applies.
    """
    if side not in ("long", "short") or tick <= 0 or entry <= 0 or not levels:
        return None
    if min_dist is not None:
        floor = float(min_dist)
    elif stop is not None and float(stop) > 0:
        floor = min_tp_distance(entry, stop)
    else:
        floor = 0.0
    above = side == "long"
    best: float | None = None
    for raw in levels:
        px = _level(raw)
        if px is None or not _beyond_tick(px, entry, tick, above=above):
            continue
        gap = (px - float(entry)) if above else (float(entry) - px)
        if floor > 0 and gap + 1e-12 < floor:
            continue
        if best is None or (px < best if above else px > best):
            best = px
    return best


def collides_with_fill(entry: float, stop: float, tick: float) -> bool:
    """True when the stop is the fill, or only one tick off it.

    LIT shorts did this: one tick past the sweep was already the Alo, so
    the stop and the fill were the same price.
    """
    dist = abs(float(entry) - float(stop))
    if dist <= 1e-9:
        return True
    if tick > 0 and dist <= float(tick) * (1.0 + 1e-9):
        return True
    return False


def size_adjust_tag(entry: float, stop: float) -> str | None:
    """``wide_stop`` when distance is past 1.5% of entry, else ``None``.

    The arm still happens. Size is risk / distance, so this tag means the
    order was reduced instead of scrapped. It is not a gate.
    """
    if entry <= 0 or stop <= 0:
        return None
    dist = abs(float(entry) - float(stop))
    if dist > abs(float(entry)) * STOP_CAP_PCT + 1e-6:
        return SIZE_ADJUST_WIDE_STOP
    return None


def place_stop(
    side: str,
    extreme: float,
    entry: float,
    tick: float,
    tp_r: float = DEFAULT_TP_R,
    *,
    swing: float | None = None,
    local_extreme: float | None = None,
    atr: float | None = None,
    further: Sequence[float] | None = None,
    clear_wick_room: bool = True,
) -> float | None:
    """Stop past opposing liquidity, or ``None`` when the geometry is impossible.

    A wick, swing, or local extreme more than one tick past the fill owns
    the stop. The buffer is added beyond that print and snapped away from
    the entry, even when the print is still inside the old 0.15% room
    (ETH 2704 stays near 2703.4; it is not lifted to 2701.7). A deeper
    wick is never pulled back toward price. Past 1.5% of ``entry`` the
    same price is kept and size shrinks.

    When the anchor is the fill itself, a 3-tick / 2 bp buffer is still
    inside that room. ``clear_wick_room`` then places the stop past the
    nearest ``further`` print that already sits outside the room, or past
    the room by the 3-tick / 2 bp pad when no such print exists. A
    0.5×ATR buffer that already clears the room is kept. Fill-time
    recomputes pass ``clear_wick_room=False`` so they cannot drag a
    real-wick stop out to the room edge.

    ``None`` is only impossible geometry: the stop is not strictly past
    the fill, it sits on the fill, or it is only one tick away. ``tp_r``
    does not reject the stop.
    """
    del tp_r
    if side not in ("long", "short") or tick <= 0 or extreme <= 0 or entry <= 0:
        return None
    anchor = liquidity_anchor(side, extreme, entry, swing, local_extreme)
    buffer = stop_buffer(entry, tick, atr)
    if buffer <= 0:
        return None

    def _finish(raw: float) -> float | None:
        stop = _snap_away(side, raw, tick)
        if side == "long" and stop <= 0:
            return None
        if not stop_is_valid(side, entry, stop) or collides_with_fill(entry, stop, tick):
            return None
        return stop

    above = side == "short"
    real = _beyond_tick(anchor, entry, tick, above=above)
    if side == "long":
        buffered = _finish(anchor - buffer)
    else:
        buffered = _finish(anchor + buffer)
    # A real print owns the stop. A fill-only recompute must not invent
    # room it was not given a wick for.
    if real or not clear_wick_room:
        return buffered
    room = floor_distance(entry, tick)
    if buffered is not None and abs(float(entry) - buffered) + 1e-12 >= room:
        return buffered
    pad = extension_pad(entry, tick)
    if pad <= 0:
        return buffered
    level = next_opposing_level(side, entry, room, further)
    if level is not None:
        raw = (level - pad) if side == "long" else (level + pad)
    else:
        raw = (float(entry) - (room + pad)) if side == "long" else (float(entry) + (room + pad))
    extended = _finish(raw)
    return extended if extended is not None else buffered


def widen_stop_for_fill(
    side: str,
    fill: float,
    stop: float,
    limit: float,
    tick: float,
    tp_r: float = DEFAULT_TP_R,
) -> float:
    """Keep the planned stop unless a structural recompute is wider.

    A fill-only recompute has no wick and does not clear wick room, so it
    cannot replace a real-wick stop with the room edge. A one-tick
    collision is never substituted. Distance inside the old floor or past
    1.5% does not drop the arm stop.
    """
    del limit, tp_r
    if tick <= 0 or fill <= 0:
        return stop
    pushed = place_stop(side, fill, fill, tick, clear_wick_room=False)
    if pushed is None:
        return stop
    if side == "long":
        wider = min(stop, pushed)
    else:
        wider = max(stop, pushed)
    if stop_is_valid(side, fill, wider) and not collides_with_fill(fill, wider, tick):
        return wider
    if stop_is_valid(side, fill, stop) and not collides_with_fill(fill, stop, tick):
        return stop
    return pushed


def initial_margin(size: float, price: float, leverage: int = LEVERAGE) -> float:
    """USDC initial margin for one ticket: notional / leverage.

    ``price`` is the Alo limit for a resting order and the fill for an
    open position. ``leverage`` is the coin max set on that order. Unknown
    meta uses 20, the same fallback ``usable_leverage`` returns. This is
    not a second balance, and it does not change the 2% size.
    """
    if size <= 0 or price <= 0:
        return 0.0
    return float(size) * float(price) / float(_margin_leverage(leverage))


def ticket_fits(
    equity: float,
    committed: float,
    size: float,
    price: float,
    leverage: int = LEVERAGE,
) -> bool:
    """True when ``equity - committed`` covers this ticket at its full size.

    ``equity`` is the Model B sizing balance (spot USDC, or paper equity).
    ``size`` is already the 2% ticket. This does not shrink it to fit.
    """
    if equity <= 0 or size <= 0 or price <= 0:
        return False
    need = initial_margin(size, price, leverage)
    free = float(equity) - float(committed)
    return need <= free + 1e-6


# Fraction of free-margin capacity held for the preferred close coin.
# The hunt picks that coin (highest score, then closer in bps). This is
# not a BTC setting. 0 turns the reserve off.
CLOSE_MARGIN_RESERVE = 0.60


def reserve_headroom(
    capacity: float,
    fraction: float = CLOSE_MARGIN_RESERVE,
) -> float:
    """USDC that must stay free for the preferred close coin.

    ``capacity`` is the sizing balance minus margin this rule will not
    cancel (open positions and a resting Alo on the preferred coin).
    ``fraction`` is ``MODEL_B_CLOSE_MARGIN_RESERVE``. 0 leaves no headroom
    because the reserve is off.
    """
    if capacity <= 0 or fraction <= 0:
        return 0.0
    return float(capacity) * float(fraction)


def other_margin_cap(
    capacity: float,
    fraction: float = CLOSE_MARGIN_RESERVE,
) -> float:
    """Initial margin every other coin may use while the reserve is on."""
    if capacity <= 0:
        return 0.0
    frac = min(max(float(fraction), 0.0), 1.0)
    return float(capacity) * (1.0 - frac)


def leaves_reserve_headroom(
    capacity: float,
    committed_other: float,
    new_need: float,
    fraction: float = CLOSE_MARGIN_RESERVE,
) -> bool:
    """True when other-coin margin, including ``new_need``, stays inside the cap.

    After the new ticket, free margin is still at least ``fraction`` of
    ``capacity``. The preferred coin is not part of ``committed_other``.
    """
    return (
        float(committed_other) + float(new_need)
        <= other_margin_cap(capacity, fraction) + 1e-6
    )


def fee_per_unit(
    entry: float,
    stop: float,
    *,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> float:
    """USDC fees per coin for a stop-out: maker Alo entry + taker exit at the stop.

    Omitted rates are the published base tier (1.5 / 4.5 bps).
    """
    maker = MAKER_FEE_RATE if maker_fee is None else float(maker_fee)
    taker = TAKER_FEE_RATE if taker_fee is None else float(taker_fee)
    return abs(float(entry)) * maker + abs(float(stop)) * taker


def loss_at_stop(
    size: float,
    entry: float,
    stop: float,
    *,
    include_fees: bool = True,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> float:
    """Dollar loss if ``size`` is stopped out: price move, plus both fees when asked."""
    per = abs(float(entry) - float(stop))
    if include_fees:
        per += fee_per_unit(entry, stop, maker_fee=maker_fee, taker_fee=taker_fee)
    return max(0.0, float(size)) * per


def cap_size_to_loss(
    size: float,
    entry: float,
    stop: float,
    account: float,
    max_loss_pct: float = HARD_MAX_LOSS_PCT,
    round_down=None,
    *,
    include_fees: bool = False,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> float:
    """Largest size <= ``size`` whose loss at the stop is <= the hard cap.

    ``max_loss_pct`` is clamped to HARD_MAX_LOSS_PCT. ``round_down`` floors
    to the coin's size step (never rounds up). 0 when nothing fits.
    ``include_fees`` counts the maker entry and taker exit fee in that
    loss (Oct 6 ETH: 2.00% on price was 2.56% after fees).
    """
    pct = min(float(max_loss_pct), HARD_MAX_LOSS_PCT)
    dist = abs(float(entry) - float(stop))
    if size <= 0 or dist <= 0 or account <= 0 or pct <= 0:
        return 0.0
    if include_fees:
        dist += fee_per_unit(entry, stop, maker_fee=maker_fee, taker_fee=taker_fee)
    cap = pct * float(account)
    if size * dist <= cap * (1 + 1e-12):
        return float(size)
    out = cap / dist
    if round_down is not None:
        try:
            out = float(round_down(out))
        except Exception:
            return 0.0
    else:
        out = math.floor(out * 1e8) / 1e8
    while out > 0 and out * dist > cap * (1 + 1e-12):
        out = math.floor(out * 0.999999 * 1e8) / 1e8
    return max(0.0, out)


def size_from_stop(
    spot_usdc: float,
    entry: float,
    stop: float,
    *,
    risk_pct: float,
    leverage: int = LEVERAGE,
    notional_leverage: int | None = None,
    min_stop_bps: float = 0.0,
    include_fees: bool = False,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> tuple[float, float]:
    """Return ``(size, dollar_risk)`` from stop distance and ``risk_pct``.

    ``spot_usdc`` is the spot USDC balance (paper tests pass that balance
    in directly). It is not perp account value. ``risk_pct`` is
    ``RISK_PER_TRADE`` (0.02 for Model B), so dollar risk is at most 2% of
    spot USDC. A wider stop returns a smaller size.

    Two brakes can only shrink the ticket:

    - ``min_stop_bps``: size is computed from ``max(|entry - stop|,
      entry * min_stop_bps / 1e4)``. The stop price is not moved; a
      liquidity stop tighter than the floor just gets a smaller size
      (Oct 7 23:02: a 23-point BTC stop sized ~0.14 BTC on $293).
    - Notional is capped at ``min(leverage, notional_leverage)`` times
      the same balance. ``leverage`` is the coin max the exchange is set
      to; ``notional_leverage`` is ``MODEL_B_MAX_LEVERAGE`` (20).

    ``include_fees`` (``MODEL_B_CAP_INCLUDES_FEES``, on by default in the
    engine) sizes so the price loss *plus* the maker entry and taker exit
    fee at the stop is at most ``risk_pct`` of the balance. Without it a
    stop-out loses 2% + ~6 bps of notional (2.8% at 13x). The stop and
    target prices are not moved.

    ``dollar_risk`` is the loss if the real stop is hit at the returned size.
    """
    lev = _margin_leverage(leverage)
    if spot_usdc <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("invalid size inputs")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be > 0")
    dist = abs(entry - stop)
    if dist <= 0:
        raise ValueError("stop distance is zero")
    floor_dist = float(entry) * max(0.0, float(min_stop_bps or 0.0)) / 10_000.0
    sizing_dist = max(dist, floor_dist)
    target = spot_usdc * float(risk_pct)
    fees = (
        fee_per_unit(entry, stop, maker_fee=maker_fee, taker_fee=taker_fee)
        if include_fees
        else 0.0
    )
    size = target / (sizing_dist + fees)
    cap_lev = lev
    if notional_leverage is not None:
        try:
            brake = int(notional_leverage)
        except (TypeError, ValueError):
            brake = 0
        if brake >= 1:
            cap_lev = min(cap_lev, brake)
    max_notional = spot_usdc * cap_lev
    trimmed = sizing_dist > dist or fees > 0
    if size * entry > max_notional:
        size = max_notional / entry
        trimmed = True
    size = math.floor(size * 1_000_000) / 1_000_000
    if size <= 0:
        raise ValueError("size rounded to zero")
    dollar = size * dist if trimmed else target
    return size, min(dollar, target)


def take_profit(
    side: str,
    entry: float,
    stop: float,
    pool_price: float | None,
    tp_r: float = DEFAULT_TP_R,
) -> float:
    """Liquidity in the trade direction, else ``tp_r`` off the stop.

    ``pool_price`` is the next supportive level (nearest confirmed swing
    or untaken pool). On the correct side of the entry it is the target,
    including inside 1R and past 2R. It is not pulled back to 2R. With
    no level on the trade's side the target is ``tp_r`` off
    ``abs(entry - stop)``, clamped to 1–2 (default 1.5).
    """
    if pool_price is not None and float(pool_price) > 0:
        px = float(pool_price)
        if side == "long" and px > float(entry):
            return px
        if side == "short" and px < float(entry):
            return px
    r = clamp_tp_r(tp_r)
    dist = abs(float(entry) - float(stop))
    if side == "long":
        return float(entry) + r * dist
    return float(entry) - r * dist


def arm_take_profit(
    side: str,
    entry: float,
    stop: float,
    pool_price: float | None,
    tp_r: float = DEFAULT_TP_R,
) -> float | None:
    """Target for an arm, or ``None`` when it is not strictly beyond the entry.

    A liquidity price is kept even past 2R. With no liquidity the fallback
    is ``tp_r`` (clamped 1–2) of the structural stop. Zero stop distance
    cannot build that fallback.
    """
    dist = abs(float(entry) - float(stop))
    if dist <= 0 or float(entry) <= 0 or side not in ("long", "short"):
        return None
    tp = take_profit(side, entry, stop, pool_price, tp_r)
    if not tp_is_valid(side, entry, tp):
        return None
    return tp


def _valid_target(side: str, entry: float, price: float | None) -> float | None:
    if price is None:
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px > 0 and tp_is_valid(side, float(entry), px):
        return px
    return None


def locked_take_profit(
    side: str,
    entry: float,
    armed_tp: float,
    pool_px: float | None,
    stop: float,
    tp_r: float = DEFAULT_TP_R,
) -> float:
    """Liquidity target for a fill. A 2R cap does not replace it.

    ``pool_px`` is the level chosen at the arm. When it sits strictly
    beyond the fill it is the target, including past 2R, even if
    ``armed_tp`` was a closer 2R price. With no level, the fallback is
    ``tp_r`` of ``stop``.
    """
    pool = _valid_target(side, entry, pool_px)
    if pool is not None:
        return pool
    fallback = arm_take_profit(side, entry, stop, None, tp_r)
    if fallback is not None:
        return fallback
    armed = _valid_target(side, entry, armed_tp)
    if armed is not None:
        return armed
    return float(armed_tp)


def tp_fail_detail(
    side: str,
    entry: float,
    stop: float,
    pool_price: float | None,
    tp: float,
) -> dict[str, float | str | None]:
    """Measurements for a ``BAD_TP`` log line. Does not accept or reject.

    ``r_distance`` is ``abs(entry - stop)`` for the structural stop the
    caller sized from. ``pool_distance`` and ``pool_r`` are that same
    direction: positive when the pool is in front of the entry. ``why``
    is ``over_2r`` only when the rejected target itself is past 2R,
    ``pool_too_close`` when the pool is inside 1R, ``fees`` when 1R is
    shorter than the round-trip fee, or ``under_1r``. A far pool does
    not label a target that never left the entry as ``over_2r``.
    """
    r_distance = abs(float(entry) - float(stop))
    pool_distance: float | None
    if pool_price is None or float(entry) <= 0 or float(pool_price) <= 0:
        pool_distance = None
    elif side == "long":
        pool_distance = float(pool_price) - float(entry)
    else:
        pool_distance = float(entry) - float(pool_price)
    pool_r = (
        pool_distance / r_distance
        if pool_distance is not None and r_distance > 0
        else None
    )
    if side == "long":
        tp_dist = float(tp) - float(entry)
    else:
        tp_dist = float(entry) - float(tp)
    tp_r = tp_dist / r_distance if r_distance > 0 else None
    if tp_r is not None and tp_r > 2.0 + 1e-12:
        why = "over_2r"
    elif pool_r is not None and 0 < pool_r < 1.0 - 1e-12:
        why = "pool_too_close"
    elif (
        r_distance > 0
        and float(entry) > 0
        and r_distance < abs(float(entry)) * ROUND_TRIP_FEE_RATE - 1e-12
    ):
        why = "fees"
    else:
        why = "under_1r"
    return {
        "r_distance": r_distance,
        "pool_distance": pool_distance,
        "pool_r": pool_r,
        "why": why,
    }


def tp_is_valid(side: str, entry: float, tp: float) -> bool:
    if side == "long":
        return tp > entry
    return tp < entry


def stop_is_valid(side: str, entry: float, stop: float) -> bool:
    if side == "long":
        return stop < entry
    return stop > entry


def heal_stop(side: str, current: float, proposed: float) -> float:
    """Keep the wider stop. Heal must not overwrite it with a tighter one.

    Long: wider means lower. Short: wider means higher. A proposal that
    gives the trade more room is accepted; a tighter heal is ignored.
    """
    if side == "long":
        return min(current, proposed)
    if side == "short":
        return max(current, proposed)
    raise ValueError(f"bad side {side}")


def flow_exit_reason(**_ignored: object) -> None:
    """Flow is not an exit. Delta flipping does not close the position."""
    return None


def soft_prop_allows(**_ignored: object) -> bool:
    """Soft-prop is off. This never grants an entry."""
    return False

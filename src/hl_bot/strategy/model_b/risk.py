"""Model B risk. Fixed policy — not the scalp 0.5% / VWAP stop.

- Stop sits past the further of the sweep extreme and the Alo, by enough
  room that a 2.5R target clears a maker+taker round trip. One tick is not
  enough on a fine book (ETH, LIT): that R is smaller than the fee.
- Size = spot USDC × ``RISK_PER_TRADE`` / stop distance. Model B requires
  that fraction to be 0.02. The base is the spot USDC balance, not perp
  account value. Notional capped at 20× that balance.
- 20× only. 40× is rejected.
- TP1 defaults to 2.5R, clamped to [1.0, 3.0] before the pool cap.
  The pool cap may pull TP inside 1R. It may not push TP through the pool.
- Heal keeps the wider stop. A tighter proposal does not overwrite it.
- Soft-prop off. Strategy kill off. Flow is not an exit. No market fallback.
"""

from __future__ import annotations

import math

# Locked Model B fraction. Sizing reads the caller's risk_pct (from
# RISK_PER_TRADE). This constant is the value that setting must be.
MODEL_B_RISK_PCT = 0.02
RISK_PCT = MODEL_B_RISK_PCT
LEVERAGE = 20
DEFAULT_TP_R = 2.5
TP_R_MIN = 1.0
TP_R_MAX = 3.0
STOP_PAST_EXTREME_TICKS = 1.0
# At least one tick, so a coarse book still clears the fill. The fee term
# below is what rejects an ETH/LIT stop that is only one tick wide.
STOP_MIN_TICKS_FROM_ENTRY = 1.0
# Hyperliquid base tier: maker 1.5 bps, taker 4.5 bps.
# https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
# The Alo is the maker entry. The stop trigger is a taker. Round trip = 6 bps.
MAKER_FEE_RATE = 0.00015
TAKER_FEE_RATE = 0.00045
ROUND_TRIP_FEE_RATE = MAKER_FEE_RATE + TAKER_FEE_RATE
# Fraction of price the stop must clear so ``DEFAULT_TP_R`` gross covers
# that round trip: 0.0006 / 2.5 = 2.4 bps. A tighter R needs more room;
# ``min_stop_distance`` divides by the clamped R actually used.
MIN_STOP_PCT = ROUND_TRIP_FEE_RATE / DEFAULT_TP_R

SOFT_PROP_ENABLED = False
STRATEGY_KILL_ENABLED = False
FLOW_EXIT_ENABLED = False
MARKET_FALLBACK_ENABLED = False


def assert_policy() -> None:
    if SOFT_PROP_ENABLED or STRATEGY_KILL_ENABLED or FLOW_EXIT_ENABLED or MARKET_FALLBACK_ENABLED:
        raise RuntimeError("Model B policy flags must stay off")
    if LEVERAGE != 20 or RISK_PCT != 0.02:
        raise RuntimeError("Model B risk is 2% at 20x only")


def assert_leverage(leverage: int) -> int:
    if int(leverage) != LEVERAGE:
        raise ValueError(f"Model B is 20x only (40x off); got {leverage}")
    return LEVERAGE


def clamp_tp_r(tp_r: float) -> float:
    return min(TP_R_MAX, max(TP_R_MIN, float(tp_r)))


def stop_beyond_extreme(side: str, extreme: float, tick: float) -> float:
    """One tick past the sweep extreme, away from the entry."""
    if side == "long":
        return extreme - STOP_PAST_EXTREME_TICKS * tick
    return extreme + STOP_PAST_EXTREME_TICKS * tick


def min_stop_distance(entry: float, tick: float, tp_r: float = DEFAULT_TP_R) -> float:
    """Smallest stop distance whose R-multiple clears a maker+taker round trip.

    ``max(one tick, price × fee / R)``. On BTC a 1.0 tick is already far
    more than the fee. On ETH (tick 0.1 near 2700) and LIT (tick 0.0001
    near 3.9) the fee term is several ticks, so a 1-tick stop is rejected.
    """
    if entry <= 0 or tick <= 0:
        return 0.0
    r = clamp_tp_r(tp_r)
    # MIN_STOP_PCT is the fraction at 2.5R. A lower R needs a wider stop
    # so the target still clears the same round trip.
    fee_room = abs(entry) * MIN_STOP_PCT * (DEFAULT_TP_R / r)
    return max(STOP_MIN_TICKS_FROM_ENTRY * tick, fee_room)


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


def place_stop(
    side: str,
    extreme: float,
    entry: float,
    tick: float,
    tp_r: float = DEFAULT_TP_R,
) -> float | None:
    """Stop past the further of the sweep extreme and the Alo.

    Long anchor is ``min(extreme, entry)``; short anchor is the max. The
    minimum distance (one tick, or the fee room, whichever is larger) is
    added beyond that anchor and snapped away from the entry onto the tick
    grid. ``None`` when the stop is not a positive price or the snapped
    distance still cannot clear fees at ``tp_r`` — the arm fails closed
    ``BAD_STOP``.
    """
    if side not in ("long", "short") or tick <= 0 or extreme <= 0 or entry <= 0:
        return None
    room = min_stop_distance(entry, tick, tp_r)
    if room <= 0:
        return None
    if side == "long":
        anchor = min(extreme, entry)
        stop = math.floor((anchor - room) / tick + 1e-9) * tick
        if stop <= 0 or not stop_is_valid(side, entry, stop):
            return None
    else:
        anchor = max(extreme, entry)
        stop = math.ceil((anchor + room) / tick - 1e-9) * tick
        if not stop_is_valid(side, entry, stop):
            return None
    if stop_clears_fees(entry, stop, tick=tick, tp_r=tp_r):
        return stop
    # Snap landed a hair inside the fee bar. One more tick away, or give up.
    if side == "long":
        stop = math.floor((stop - tick) / tick + 1e-9) * tick
        if stop <= 0:
            return None
    else:
        stop = math.ceil((stop + tick) / tick - 1e-9) * tick
    if not stop_clears_fees(entry, stop, tick=tick, tp_r=tp_r):
        return None
    return stop


def widen_stop_for_fill(
    side: str,
    fill: float,
    stop: float,
    limit: float,
    tick: float,
    tp_r: float = DEFAULT_TP_R,
) -> float:
    """Keep a stop that still clears fees after the fill price.

    A price improvement can land on a stop that was measured from the
    limit. Push to the fee room past the fill. Never tighten a stop that
    already has that room, and never fall back to a one-tick nudge.
    """
    del limit  # room is measured from the fill, not from the old limit gap
    if tick <= 0 or fill <= 0:
        return stop
    pushed = place_stop(side, fill, fill, tick, tp_r=tp_r)
    if pushed is None:
        return stop
    if side == "long":
        wider = min(stop, pushed)
    else:
        wider = max(stop, pushed)
    if stop_is_valid(side, fill, wider) and stop_clears_fees(fill, wider, tick=tick, tp_r=tp_r):
        return wider
    return pushed


def size_from_stop(
    spot_usdc: float,
    entry: float,
    stop: float,
    *,
    risk_pct: float,
    leverage: int = LEVERAGE,
) -> tuple[float, float]:
    """Return ``(size, dollar_risk)`` from stop distance and ``risk_pct``.

    ``spot_usdc`` is the spot USDC balance (paper tests pass that balance
    in directly). It is not perp account value. ``risk_pct`` is
    ``RISK_PER_TRADE`` (0.02 for Model B), so dollar risk is at most 2% of
    spot USDC. Notional is capped at 20× that same balance, which trims
    size when the stop is tight. The fraction is not hard-wired here.
    """
    assert_leverage(leverage)
    if spot_usdc <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("invalid size inputs")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be > 0")
    dist = abs(entry - stop)
    if dist <= 0:
        raise ValueError("stop distance is zero")
    dollar = spot_usdc * float(risk_pct)
    size = dollar / dist
    max_notional = spot_usdc * LEVERAGE
    if size * entry > max_notional:
        size = max_notional / entry
        dollar = size * dist
    size = math.floor(size * 1_000_000) / 1_000_000
    if size <= 0:
        raise ValueError("size rounded to zero")
    return size, dollar


def take_profit(
    side: str,
    entry: float,
    stop: float,
    pool_price: float | None,
    tp_r: float = DEFAULT_TP_R,
) -> float:
    """TP at ``tp_r`` (clamped to 1–3, default 2.5), then capped at the pool.

    The pool cap wins even when that leaves less than 1R. TP is never
    placed beyond the untaken pool.
    """
    r = clamp_tp_r(tp_r)
    dist = abs(entry - stop)
    if side == "long":
        raw = entry + r * dist
        if pool_price is not None and pool_price > entry:
            return min(raw, pool_price)
        return raw
    raw = entry - r * dist
    if pool_price is not None and pool_price < entry:
        return max(raw, pool_price)
    return raw


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

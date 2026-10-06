"""Model B risk. Fixed policy — not the scalp 0.5% / VWAP stop.

- The stop is placed past the liquidity being swept, then the order is
  sized. Anchor is the further of the sweep wick, the swing, and the
  local 1-minute extreme. Buffer is ``max(3 ticks, 2 bps, 0.5×ATR14)``.
- That structural stop is the stop. A distance inside the old 0.15% /
  10-tick / fee floor is not parked on the floor and is not scrapped.
  A deeper wick is kept: nothing pulls the stop back inside it.
- A distance past 1.5% of price is still armed. Size shrinks with the
  distance (``size_adjust=wide_stop`` on the journal). The stop is not
  tightened back into the wick to make 1.5%.
- Size = spot USDC × ``RISK_PER_TRADE`` / that stop distance. Model B
  requires the fraction to be 0.02. Wider stop, smaller size. Notional
  capped at 20× the same balance, which trims a very tight stop.
- ``BAD_STOP`` is only impossible geometry: wrong side of the fill,
  stop == fill, or a one-tick collision (LIT). A size that rounds to
  zero is the same fail-closed.
- 20× only. 40× is rejected.
- TP1 defaults to 1.5R, clamped to [1.0, 2.0] before the pool cap.
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


def assert_leverage(leverage: int) -> int:
    if int(leverage) != LEVERAGE:
        raise ValueError(f"Model B is 20x only (40x off); got {leverage}")
    return LEVERAGE


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
) -> float | None:
    """Stop past sweep liquidity, or ``None`` when the geometry is impossible.

    The anchor is the further of the sweep wick, the swing, and the local
    extreme. The buffer is added beyond that anchor and snapped away from
    the entry. That price is the stop:

    - Inside the old 0.15% floor, it is still used. It is not lifted onto
      the floor (ETH 2705.8 must not rest at 2701.7) and it is not rejected.
    - Past 1.5% of ``entry``, it is still used. Size shrinks. It is not
      tightened into the wick.
    - A deeper wick is never pulled back toward price.

    ``None`` is only impossible geometry: the stop is not strictly past
    the fill, it sits on the fill, or it is only one tick away. ``tp_r``
    does not reject the stop. TP is applied later from the distance.
    """
    del tp_r
    if side not in ("long", "short") or tick <= 0 or extreme <= 0 or entry <= 0:
        return None
    anchor = liquidity_anchor(side, extreme, entry, swing, local_extreme)
    buffer = stop_buffer(entry, tick, atr)
    if buffer <= 0:
        return None
    if side == "long":
        structural = anchor - buffer
        stop = _snap_away(side, structural, tick)
        if stop <= 0:
            return None
    else:
        structural = anchor + buffer
        stop = _snap_away(side, structural, tick)
    if not stop_is_valid(side, entry, stop) or collides_with_fill(entry, stop, tick):
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
    """Keep the planned stop unless a structural recompute is wider.

    A fill-only recompute has no wick. It must not replace the arm stop
    with a tighter buffer, and a one-tick collision is never substituted.
    Distance inside the old floor or past 1.5% does not drop the arm stop.
    """
    del limit, tp_r
    if tick <= 0 or fill <= 0:
        return stop
    pushed = place_stop(side, fill, fill, tick)
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
    spot USDC. A wider stop returns a smaller size. Notional is capped at
    20× that same balance, which trims size when the stop is very tight.
    The fraction is not hard-wired here.
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
    """TP at ``tp_r`` (clamped to 1–2, default 1.5), then capped at the pool.

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

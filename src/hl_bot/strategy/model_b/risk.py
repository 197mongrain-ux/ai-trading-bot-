"""Model B risk. Fixed policy — not the scalp 0.5% / VWAP stop.

- Stop sits 1 tick past the sweep extreme (beyond the liquidity that printed).
- Size = unified equity × ``RISK_PER_TRADE`` / stop distance. Model B requires
  that fraction to be 0.02. Notional capped at 20× equity.
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


def size_from_stop(
    equity: float,
    entry: float,
    stop: float,
    *,
    risk_pct: float,
    leverage: int = LEVERAGE,
) -> tuple[float, float]:
    """Return ``(size, dollar_risk)`` from stop distance and ``risk_pct``.

    ``equity`` is unified account equity. ``risk_pct`` is ``RISK_PER_TRADE``
    (0.02 for Model B). The fraction is not hard-wired inside this function.
    """
    assert_leverage(leverage)
    if equity <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("invalid size inputs")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be > 0")
    dist = abs(entry - stop)
    if dist <= 0:
        raise ValueError("stop distance is zero")
    dollar = equity * float(risk_pct)
    size = dollar / dist
    max_notional = equity * LEVERAGE
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

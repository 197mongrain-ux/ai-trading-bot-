"""Post-only Alo anchor. Never crosses. No market fallback.

Long: rest at the swept low when that price is strictly below the ask,
otherwise join the best bid when that bid is strictly below the ask.
Short: mirror — swept high when it is strictly above the bid, else best ask.
"""

from __future__ import annotations

import math


def _floor_tick(price: float, tick: float) -> float:
    return math.floor(price / tick + 1e-9) * tick


def _ceil_tick(price: float, tick: float) -> float:
    return math.ceil(price / tick - 1e-9) * tick


def alo_limit(
    side: str,
    swept: float,
    best_bid: float | None,
    best_ask: float | None,
    tick: float,
) -> float | None:
    """Return a passive limit, or ``None`` when a post-only price cannot be proven.

    Missing bid/ask fails closed: Model B will not guess a price that might cross.
    """
    if tick <= 0 or swept <= 0:
        return None

    if side == "long":
        if best_ask is None or best_ask <= 0:
            return None
        if swept < best_ask:
            price = _floor_tick(swept, tick)
        elif best_bid is None or best_bid <= 0 or best_bid >= best_ask:
            return None
        else:
            price = _floor_tick(best_bid, tick)
        if price <= 0 or price >= best_ask:
            return None
        return price

    if best_bid is None or best_bid <= 0:
        return None
    if swept > best_bid:
        price = _ceil_tick(swept, tick)
    elif best_ask is None or best_ask <= 0 or best_ask <= best_bid:
        return None
    else:
        price = _ceil_tick(best_ask, tick)
    if price <= best_bid:
        return None
    return price


def market_ref(
    best_bid: float | None,
    best_ask: float | None,
    last: float | None,
) -> float | None:
    """Mid when the book is two-sided, otherwise the last trade."""
    if (
        best_bid is not None
        and best_ask is not None
        and best_bid > 0
        and best_ask > best_bid
    ):
        return (best_bid + best_ask) / 2.0
    if last is not None and last > 0:
        return float(last)
    return None


def distance_to_fill_bps(side: str, limit_px: float, ref_px: float) -> float | None:
    """Basis points the market must travel before this Alo can fill.

    A long rests under the market, so the gap is ``ref - limit``. A short
    rests above it, so the gap is ``limit - ref``. Zero means the limit is
    already at or through the reference (as close as a maker gets). Smaller
    is closer to a fill. Bps, not ticks, so BTC and ETH gaps compare.
    """
    if side not in ("long", "short") or limit_px <= 0 or ref_px <= 0:
        return None
    gap = (ref_px - limit_px) if side == "long" else (limit_px - ref_px)
    if gap <= 0:
        return 0.0
    return gap / ref_px * 10_000.0


def is_closer_to_fill(challenger_bps: float, resting_bps: float) -> bool:
    """True only when the challenger is strictly closer. A tie keeps the resting Alo."""
    return float(challenger_bps) < float(resting_bps) - 1e-6


def resting_score_blocks_closer_cancel(
    resting_score: int | None,
    candidate_score: int | None,
) -> bool:
    """Keep the resting Alo when its score is strictly higher.

    Equal scores do not block. A strictly closer limit still swaps, the
    same closer-bps rule as before this guard. A missing score does not
    block either: the bps comparison stands.
    """
    if resting_score is None or candidate_score is None:
        return False
    return int(resting_score) > int(candidate_score)


def resting_quality_blocks_closer_cancel(
    resting_quality: float | None,
    candidate_quality: float | None,
) -> bool:
    """Keep the resting Alo when its setup quality is strictly higher.

    Equal quality does not block: the closer-bps swap still stands.
    A missing quality does not block either.
    """
    if resting_quality is None or candidate_quality is None:
        return False
    return float(resting_quality) > float(candidate_quality) + 1e-9

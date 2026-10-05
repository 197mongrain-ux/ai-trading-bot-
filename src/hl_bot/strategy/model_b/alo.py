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

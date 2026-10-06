"""Nearest untaken pool sets bias. Bias never arms by itself.

Pools are PDH, PDL, WKH, WKL.

- nearest untaken pool above price → long only (shorts dropped)
- nearest untaken pool below price → short only (longs dropped)
- no untaken pool, or an exact tie above and below → NONE (both sides allowed)

The pool is a TP liquidity target for that direction, not the entry. A pool already
traded (``taken``) is ignored. Price sitting on the level counts as taken.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.types import Bias, Pool


def format_pool(pool: Pool | None) -> str | None:
    if pool is None:
        return None
    return f"{pool.name}@{pool.price:g}"


def _nearest(cands: list[Pool], last_price: float) -> Pool | None:
    if not cands:
        return None
    return min(cands, key=lambda pool: abs(pool.price - last_price))


def resolve_bias(last_price: float, pools: list[Pool]) -> Bias:
    if last_price <= 0:
        return Bias("NONE")

    live: list[Pool] = []
    for pool in pools:
        if pool.taken or pool.price <= 0:
            continue
        # Touching the level takes it. It is not a target beyond price.
        if abs(pool.price - last_price) <= max(abs(last_price), 1.0) * 1e-9:
            continue
        live.append(pool)

    pool_above = _nearest([p for p in live if p.price > last_price], last_price)
    pool_below = _nearest([p for p in live if p.price < last_price], last_price)
    if pool_above is None and pool_below is None:
        return Bias("NONE")
    if pool_above is not None and pool_below is not None:
        dist_above = abs(pool_above.price - last_price)
        dist_below = abs(pool_below.price - last_price)
        if abs(dist_above - dist_below) <= max(dist_above, 1.0) * 1e-9:
            # Tie: no directional filter. Both sides stay allowed.
            return Bias("NONE", None, pool_above, pool_below)
        if dist_above < dist_below:
            return Bias("long", pool_above, pool_above, pool_below)
        return Bias("short", pool_below, pool_above, pool_below)
    if pool_above is not None:
        return Bias("long", pool_above, pool_above, None)
    return Bias("short", pool_below, None, pool_below)

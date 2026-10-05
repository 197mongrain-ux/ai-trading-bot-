"""Nearest untaken pool sets bias. Bias never arms.

Pools are PDH, PDL, WKH, WKL. The nearest untaken pool is the target:

- pool above price → long only (shorts dropped); pool is the TP cap
- pool below price → short only (longs dropped)
- no untaken pool, or an exact tie above and below → NONE (both dropped)

A pool already traded (``taken``) is ignored. Price sitting on the level
counts as taken.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.types import Bias, Pool


def format_pool(pool: Pool | None) -> str | None:
    if pool is None:
        return None
    return f"{pool.name}@{pool.price:g}"


def resolve_bias(last_price: float, pools: list[Pool]) -> Bias:
    if last_price <= 0:
        return Bias("NONE", None)

    live: list[Pool] = []
    for pool in pools:
        if pool.taken or pool.price <= 0:
            continue
        # Touching the level takes it. It is not a target beyond price.
        if abs(pool.price - last_price) <= max(abs(last_price), 1.0) * 1e-9:
            continue
        live.append(pool)
    if not live:
        return Bias("NONE", None)

    best = min(abs(p.price - last_price) for p in live)
    nearest = [
        p
        for p in live
        if abs(abs(p.price - last_price) - best) <= max(best, 1.0) * 1e-9
    ]
    above = [p for p in nearest if p.price > last_price]
    below = [p for p in nearest if p.price < last_price]
    if above and below:
        return Bias("NONE", None)
    pool = nearest[0]
    if pool.price > last_price:
        return Bias("long", pool)
    return Bias("short", pool)

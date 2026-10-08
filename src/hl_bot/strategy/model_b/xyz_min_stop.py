"""Minimum stop distance for xyz coins.

Oct 8 2026, xyz:XYZ100 short filled at 30971 into a 1m bar that traded
31061. The ~18 bp liquidity stop was inside that bar. A 40 bp floor
clears the spike (stop 31095, 34 pts of room) and the later 1.5R
target still prints. 25 bp does not clear 31061. 30 bp clears it by
3 pts, which is not a cushion.

The same floor does not save the other two Oct 8 losses. BTC's stop
was already wider than the 15m swing and price kept going. SKHX ran to
1209; 40 bp only reaches 1201.5, and 1193 / 1187.3 never printed.

The floor only loosens a stop that is tighter than ``bps``. Main-dex
coins are unchanged. ``shadow`` logs the stop it would use and posts
today's stop. ``1`` places the wider stop and the TP walk, including
the 1.5R minimum, runs from that price. A setup that cannot pay 1.5R
off the wider stop does not arm.
"""

from __future__ import annotations

import math

from hl_bot.strategy.model_b.universe import canon_coin

# Recommended floor from the Oct 7-8 replay. The mode default is shadow,
# so this number is what shadow logs and what ``1`` would place.
RECOMMENDED_XYZ_MIN_STOP_BPS = 40.0


def is_xyz(coin: str) -> bool:
    return canon_coin(coin).startswith("xyz:")


def _snap_away(side: str, price: float, tick: float) -> float:
    if tick <= 0:
        return float(price)
    if side == "long":
        return math.floor(price / tick + 1e-9) * tick
    return math.ceil(price / tick - 1e-9) * tick


def xyz_floor_stop(
    side: str,
    entry: float,
    stop: float,
    tick: float,
    coin: str,
    bps: float,
) -> float | None:
    """Stop at least ``bps`` from ``entry``, or None when nothing moves.

    None means: not an xyz coin, ``bps`` is off, or the liquidity stop
    is already at least that far. The returned price is always further
    from the entry than ``stop``.
    """
    if side not in ("long", "short") or entry <= 0 or stop <= 0 or tick <= 0:
        return None
    if not is_xyz(coin) or float(bps) <= 0:
        return None
    dist = abs(float(entry)) * float(bps) / 10_000.0
    if abs(float(entry) - float(stop)) + 1e-12 >= dist:
        return None
    raw = float(entry) + dist if side == "short" else float(entry) - dist
    widened = _snap_away(side, raw, tick)
    if widened <= 0:
        return None
    if side == "short" and widened <= float(stop) + 1e-12:
        return None
    if side == "long" and widened >= float(stop) - 1e-12:
        return None
    return float(f"{widened:.10g}")

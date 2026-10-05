"""90-second aggressor tape: sweep, reclaim, absorb, delta.

Fail reasons, first match wins:

``NO_SIDE``, ``THIN_TAPE``, ``NO_SWEEP``, ``NO_RECLAIM``, ``ABSORB``,
``DELTA``, ``LAST_15s``.

Long, all true: a print at least 1 tick through the swing low; the last
trade back above that low; absorb = sell size from sweep→reclaim / buy
size from reclaim→now ≥ 1.5; 90s delta not negative; last 15s delta not
negative. Short is the mirror (buy/sell swapped, deltas not positive).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_bot.strategy.model_b.types import TradePrint

WINDOW_SEC = 90.0
# Mainnet floor. A 2026-10-05 NY-session sample on
# wss://api.hyperliquid.xyz/ws (trades, BTC) saw 84 prints in 20s
# (252/min). The subscribe snapshot of the last 30 spanned 10.2s.
MIN_PRINTS = 30
MAINNET_BTC_PRINTS_PER_MIN = 252.0
# Same-day desk probe on testnet: ~7 BTC prints/min (~10.5 in a 90s window).
TESTNET_BTC_PRINTS_PER_MIN = 7.0
# Auto scale never drops below this. 30 * (7/252) rounds to 1, so the
# testnet floor is this bound, not 1.
MIN_PRINTS_FLOOR = 3
LAST_SEC = 15.0
ABSORB_MIN = 1.5
SWEEP_TICKS = 1.0

NO_SIDE = "NO_SIDE"
THIN_TAPE = "THIN_TAPE"
NO_SWEEP = "NO_SWEEP"
NO_RECLAIM = "NO_RECLAIM"
ABSORB = "ABSORB"
DELTA = "DELTA"
LAST_15S = "LAST_15s"

TAPE_FAILS = (NO_SIDE, THIN_TAPE, NO_SWEEP, NO_RECLAIM, ABSORB, DELTA, LAST_15S)


def density_min_prints(
    base: int = MIN_PRINTS,
    testnet_per_min: float = TESTNET_BTC_PRINTS_PER_MIN,
    mainnet_per_min: float = MAINNET_BTC_PRINTS_PER_MIN,
) -> int:
    """Same density as ``base`` prints on mainnet, applied to the testnet rate.

    ``max(MIN_PRINTS_FLOOR, round(base * testnet_rate / mainnet_rate))``.
    Mainnet itself stays at ``base`` (30). This does not change absorb,
    delta, or sweep rules.
    """
    if mainnet_per_min <= 0:
        return int(base)
    scaled = int(round(int(base) * (float(testnet_per_min) / float(mainnet_per_min))))
    return max(MIN_PRINTS_FLOOR, scaled)


@dataclass(frozen=True)
class TapeMetrics:
    absorb: float | None
    window_delta: float | None
    last_15s_delta: float | None
    sweep_price: float | None
    fail_reason: str | None


def window_prints(
    prints: list[TradePrint],
    *,
    coin: str,
    now: float,
    window_sec: float = WINDOW_SEC,
) -> list[TradePrint]:
    coin = coin.upper()
    start = float(now) - window_sec
    selected = [
        p
        for p in prints
        if p.coin == coin
        and p.price > 0
        and p.size > 0
        and start - 1e-9 <= p.ts <= float(now) + 1e-9
    ]
    selected.sort(key=lambda p: (p.ts, p.seq))
    return selected


def signed_delta(prints: list[TradePrint]) -> float:
    buy = sum(p.size for p in prints if p.side == "buy")
    sell = sum(p.size for p in prints if p.side == "sell")
    return buy - sell


def missing_side(prints: list[TradePrint]) -> bool:
    return any(p.side not in ("buy", "sell") for p in prints)


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator > 1e-12:
        return numerator / denominator
    if numerator > 1e-12:
        return math.inf
    return None


def _delta_aligned(side: str, value: float) -> bool:
    """Long: not negative. Short: not positive. Zero passes both."""
    if side == "long":
        return value >= -1e-9
    return value <= 1e-9


def analyze_tape(
    prints: list[TradePrint],
    *,
    side: str,
    swing: float,
    tick: float,
    now: float,
) -> TapeMetrics:
    """Score the tape. Caller has already rejected ``NO_SIDE`` and ``THIN_TAPE``."""
    window_delta = signed_delta(prints)
    last_cut = float(now) - LAST_SEC
    last_15 = signed_delta([p for p in prints if p.ts >= last_cut - 1e-9])

    if side == "long":
        def _through(price: float) -> bool:
            return price <= swing - tick * SWEEP_TICKS + tick * 1e-6

        def _reclaim(price: float) -> bool:
            return price > swing

        pick = min
        sweep_side = "sell"
        reclaim_side = "buy"
    else:
        def _through(price: float) -> bool:
            return price >= swing + tick * SWEEP_TICKS - tick * 1e-6

        def _reclaim(price: float) -> bool:
            return price < swing

        pick = max
        sweep_side = "buy"
        reclaim_side = "sell"

    sweep_idx = next((i for i, p in enumerate(prints) if _through(p.price)), None)
    if sweep_idx is None:
        return TapeMetrics(None, window_delta, last_15, None, NO_SWEEP)

    reclaim_idx = next(
        (i for i, p in enumerate(prints) if i > sweep_idx and _reclaim(p.price)),
        None,
    )
    end = reclaim_idx if reclaim_idx is not None else len(prints)
    through_leg = [p for p in prints[sweep_idx:end] if _through(p.price)]
    sweep_price = pick(p.price for p in through_leg) if through_leg else prints[sweep_idx].price

    absorb: float | None = None
    if reclaim_idx is not None:
        num = sum(p.size for p in prints[sweep_idx:reclaim_idx] if p.side == sweep_side)
        den = sum(p.size for p in prints[reclaim_idx:] if p.side == reclaim_side)
        absorb = _ratio(num, den)

    if not _reclaim(prints[-1].price):
        return TapeMetrics(absorb, window_delta, last_15, sweep_price, NO_RECLAIM)
    if absorb is None or not (absorb >= ABSORB_MIN - 1e-12):
        return TapeMetrics(absorb, window_delta, last_15, sweep_price, ABSORB)
    if not _delta_aligned(side, window_delta):
        return TapeMetrics(absorb, window_delta, last_15, sweep_price, DELTA)
    if not _delta_aligned(side, last_15):
        return TapeMetrics(absorb, window_delta, last_15, sweep_price, LAST_15S)
    return TapeMetrics(absorb, window_delta, last_15, sweep_price, None)

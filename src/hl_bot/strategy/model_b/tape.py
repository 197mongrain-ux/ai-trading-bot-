"""90-second aggressor tape: sweep, reclaim, absorb, delta.

Fail reasons, first match wins:

``NO_SIDE``, ``THIN_TAPE``, ``NO_SWEEP``, ``NO_RECLAIM``, ``ABSORB``,
``DELTA``, ``LAST_15s``.

Long, all true: a print at least 1 tick through the swing low; the last
trade back above that low; absorb = sell size from sweep→reclaim / buy
size from reclaim→now ≥ 1.3; 90s delta and last-15s delta at or above
``-eps`` coins. Short is the mirror (buy/sell swapped, deltas at or
below ``+eps``).

Delta is coin size: sum of buy print sizes minus sum of sell print sizes.
It is not dollars and not a ratio. The flat band is a USDC notional
(``DELTA_FLAT_USDC`` / mid) so SOL and BTC share the same dollar tolerance.
``DELTA_FLAT_EPS`` is the coin-size fallback when that notional is 0.
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
# Sell size from sweep→reclaim over buy size from reclaim→now. Short swaps
# the sides. 2026-10-06: 1.5 was vetoing majors after sweep and reclaim had
# cleared. The closest ETH short peaked at 1.34. The floor is 1.3.
ABSORB_MIN = 1.3
SWEEP_TICKS = 1.0
# Notional flat band. Divided by the coin's mid (else last) to get coin
# size. 0.05 coins was ~$6 on SOL and ~$4k on BTC. $100 is the same
# tolerance on both: about 0.67 SOL and about 0.0012 BTC at those prices.
# 0 turns the notional off and the coin-size fallback below is used.
DELTA_FLAT_USDC = 100.0
# Coin-size fallback (buy sz − sell sz) when ``DELTA_FLAT_USDC`` is 0,
# or when there is no price to convert with. 0 is the strict sign check.
DELTA_FLAT_EPS = 0.05
# Dust inside this was already treated as zero before the flat band.
_DELTA_STRICT_EPS = 1e-9

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
    # ``window``, ``last_15s``, or ``both`` when the flat band passed a
    # delta the strict sign check would have failed. Null otherwise.
    delta_flat: str | None = None


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


def flat_eps_coins(
    usdc: float,
    price: float | None,
    coin_eps: float = DELTA_FLAT_EPS,
) -> float:
    """Coin-size flat band for one check.

    ``usdc > 0`` and a positive price: ``usdc / price``. Otherwise the
    coin-size fallback. ``0`` is the strict sign check.
    """
    if float(usdc) > 0 and price is not None and float(price) > 0:
        return float(usdc) / float(price)
    if float(coin_eps) <= 0:
        return 0.0
    return float(coin_eps)


def _delta_aligned(side: str, value: float, eps: float = DELTA_FLAT_EPS) -> bool:
    """Long passes at or above ``-eps``. Short passes at or below ``+eps``.

    ``eps`` is coin size. ``eps <= 0`` is the old strict sign check:
    long passes when the value is at least ``-1e-9``, short when it is
    at most ``+1e-9``.
    """
    if eps <= 0:
        if side == "long":
            return value >= -_DELTA_STRICT_EPS
        return value <= _DELTA_STRICT_EPS
    band = float(eps)
    if side == "long":
        return value >= -band - 1e-12
    return value <= band + 1e-12


def _strict_delta_aligned(side: str, value: float) -> bool:
    """The pre-band sign check. Used only to tag a flat-band save."""
    return _delta_aligned(side, value, 0.0)


def delta_flat_tag(
    side: str,
    window_delta: float,
    last_15s_delta: float,
    eps: float,
) -> str | None:
    """Which leg the flat band saved, or ``None`` when both were already strict.

    Returned only when the tape is otherwise passing. A leg that is still
    outside the band is a real ``DELTA`` / ``LAST_15s`` fail and is not tagged.
    """
    saved_window = _delta_aligned(side, window_delta, eps) and not _strict_delta_aligned(
        side, window_delta
    )
    saved_last = _delta_aligned(side, last_15s_delta, eps) and not _strict_delta_aligned(
        side, last_15s_delta
    )
    if saved_window and saved_last:
        return "both"
    if saved_window:
        return "window"
    if saved_last:
        return "last_15s"
    return None


def analyze_tape(
    prints: list[TradePrint],
    *,
    side: str,
    swing: float,
    tick: float,
    now: float,
    delta_flat_eps: float = DELTA_FLAT_EPS,
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
    if not _delta_aligned(side, window_delta, delta_flat_eps):
        return TapeMetrics(absorb, window_delta, last_15, sweep_price, DELTA)
    if not _delta_aligned(side, last_15, delta_flat_eps):
        return TapeMetrics(absorb, window_delta, last_15, sweep_price, LAST_15S)
    return TapeMetrics(
        absorb,
        window_delta,
        last_15,
        sweep_price,
        None,
        delta_flat_tag(side, window_delta, last_15, delta_flat_eps),
    )

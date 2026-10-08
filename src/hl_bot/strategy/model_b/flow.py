"""Counter-flow veto for the sweep window (log + gate).

The 90s window delta only sees the last 90 seconds. On Oct 7 22:53-22:55
ET BTC printed -20 to -30 BTC of 90s delta through the sweep, then a
single +14 BTC burst in the 15s before 22:56 flipped the 90s window
positive and armed a long into that selling.

This looks back ``lookback_sec`` (default 300s) and takes the most
adverse rolling 90s delta (sell-heavy for a long, buy-heavy for a short).
When its size clears the band — the larger of ``usdc / mid`` and a coin
floor, the same scaling as ``DELTA_FLAT`` — the setup is skipped as
``COUNTER_FLOW`` unless the flow has clearly flipped and held:

- the current 90s delta is on the trade side by at least ``flip_ratio``
  times the adverse burst (1.0 = buyers fully matched the sellers), and
- the rolling 90s delta has stayed on the trade side for ``hold_sec``.

Delta is coin size (buy size minus sell size), as in ``tape``.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.strategy.model_b.tape import WINDOW_SEC
from hl_bot.strategy.model_b.types import TradePrint
from hl_bot.strategy.model_b.universe import canon_coin

COUNTER_FLOW = "COUNTER_FLOW"
LOOKBACK_SEC = 300.0
COUNTER_FLOW_USDC = 1_000_000.0
COUNTER_FLOW_EPS = 0.0
FLIP_RATIO = 1.0
HOLD_SEC = 30.0


@dataclass(frozen=True)
class FlowCheck:
    blocked: bool
    adverse: float  # most adverse rolling 90s delta (signed, coins)
    adverse_usdc: float
    band: float  # coins
    current: float  # current 90s delta (signed, coins)
    held_sec: float

    def label(self) -> str:
        return (
            f"adverse={self.adverse:.4f} adverse_usdc={self.adverse_usdc:.0f} "
            f"band={self.band:.4f} now={self.current:.4f} held={self.held_sec:.0f}s"
        )


def _rolling(prints: list[TradePrint], window: float) -> list[tuple[float, float]]:
    """(ts, delta over (ts-window, ts]) for each print, two-pointer."""
    out: list[tuple[float, float]] = []
    lo = 0
    run = 0.0
    for p in prints:
        run += p.size if p.side == "buy" else -p.size
        while prints[lo].ts <= p.ts - window:
            q = prints[lo]
            run -= q.size if q.side == "buy" else -q.size
            lo += 1
        out.append((p.ts, run))
    return out


def counter_flow(
    side: str,
    prints: list[TradePrint],
    *,
    coin: str,
    now: float,
    mid: float | None,
    lookback_sec: float = LOOKBACK_SEC,
    usdc: float = COUNTER_FLOW_USDC,
    eps: float = COUNTER_FLOW_EPS,
    flip_ratio: float = FLIP_RATIO,
    hold_sec: float = HOLD_SEC,
    window_sec: float = WINDOW_SEC,
) -> FlowCheck:
    name = canon_coin(coin)
    start = float(now) - float(lookback_sec) - float(window_sec)
    sel = sorted(
        (
            p
            for p in prints
            if canon_coin(p.coin) == name
            and p.size > 0
            and p.side in ("buy", "sell")
            and start - 1e-9 <= p.ts <= float(now) + 1e-9
        ),
        key=lambda p: (p.ts, p.seq),
    )
    sign = 1.0 if side == "long" else -1.0
    usdc_band = (float(usdc) / float(mid)) if (usdc and mid and mid > 0) else 0.0
    band = max(usdc_band, float(eps or 0.0))
    if not sel or band <= 0:
        return FlowCheck(False, 0.0, 0.0, band, 0.0, 0.0)
    series = _rolling(sel, float(window_sec))
    look_start = float(now) - float(lookback_sec)
    in_look = [(ts, d) for ts, d in series if ts >= look_start - 1e-9]
    if not in_look:
        return FlowCheck(False, 0.0, 0.0, band, 0.0, 0.0)
    # Adverse = most negative (long) / most positive (short) rolling delta.
    adverse = min(in_look, key=lambda item: sign * item[1])[1]
    current = series[-1][1]
    # How long the rolling delta has been on the trade side, up to now.
    held = 0.0
    for ts, d in reversed(in_look):
        if sign * d < 0:
            break
        held = float(now) - ts
    adverse_mag = max(0.0, -sign * adverse)
    px = float(mid) if mid and mid > 0 else 0.0
    if adverse_mag < band:
        return FlowCheck(False, adverse, adverse_mag * px, band, current, held)
    flipped = sign * current >= float(flip_ratio) * adverse_mag
    held_ok = held + 1e-9 >= float(hold_sec)
    return FlowCheck(not (flipped and held_ok), adverse, adverse_mag * px, band, current, held)

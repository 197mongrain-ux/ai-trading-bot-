"""Setup quality for ranking armed candidates. Not an arm gate.

``score`` stays tape density (prints // 10, capped at 9). Nearly every
coin that reaches an arm is a 9, so margin, the closer-ticker guard, and
the 60% reserve were effectively SYMBOLS order then closest bps.

Quality is a float computed only for an armed intent. Higher is better.
``MODEL_B_QUALITY_RANK=1`` sorts armed hunts by it before margin is
handed out, and the closer guard / reserve compare it instead of the
0–9 density number. ``shadow`` logs the same number and does not change
order. ``0`` leaves today's density score and hunt order alone.

Components (each logged on the ARM line):

- ``tp1_r``: R from the entry to the PR #15 TP1. Capped at 5 so one
  far pool cannot own the book.
- ``absorb``: reclaim-side tape ratio. Missing or infinite counts as 0.
  Capped at 4.
- ``macro_adx`` / ``macro_align``: combined 1h/4h macro. Align is +1
  with the side, -1 against, 0 for range or no read. Strength is the
  4h ADX when we have it, else the 1h ADX.
- ``stop_atr``: stop distance / ATR14. Fitness peaks at 0.75 ATR
  (a normal structural stop) and falls off when the stop is a few
  ticks or several ATRs.
- ``sweep_bps``: how far the sweep traded through the swing, in bps.
- ``fee_drag``: maker entry + taker stop fee, as a fraction of 1R.
  Higher fees lower the score.

The number is not compared to a cutoff. A low-quality arm still posts
when it is the only one and margin fits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_bot.strategy.model_b.risk import MAKER_FEE_RATE, TAKER_FEE_RATE
from hl_bot.strategy.model_b.swings import atr14
from hl_bot.strategy.model_b.universe import canon_coin


@dataclass(frozen=True)
class SetupQuality:
    quality: float
    tp1_r: float
    absorb: float
    macro: str
    macro_adx: float
    macro_align: float
    stop_atr: float
    stop_fit: float
    sweep_bps: float
    fee_drag: float

    def parts(self) -> dict:
        return {
            "quality": round(self.quality, 4),
            "tp1_r": round(self.tp1_r, 4),
            "absorb": None if not math.isfinite(self.absorb) else round(self.absorb, 4),
            "macro": self.macro,
            "macro_adx": round(self.macro_adx, 2),
            "macro_align": self.macro_align,
            "stop_atr": round(self.stop_atr, 4),
            "stop_fit": round(self.stop_fit, 4),
            "sweep_bps": round(self.sweep_bps, 2),
            "fee_drag": round(self.fee_drag, 4),
        }


def sweep_depth_bps(side: str, swing: float | None, sweep: float | None) -> float:
    """Bps the sweep traded through the swing. 0 when it did not."""
    if swing is None or sweep is None:
        return 0.0
    try:
        swing_px = float(swing)
        sweep_px = float(sweep)
    except (TypeError, ValueError):
        return 0.0
    if swing_px <= 0 or sweep_px <= 0:
        return 0.0
    depth = (swing_px - sweep_px) if side == "long" else (sweep_px - swing_px)
    if depth <= 0:
        return 0.0
    return depth / swing_px * 10_000.0


def _absorb_value(absorb: float | None) -> float:
    if absorb is None:
        return 0.0
    try:
        value = float(absorb)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _stop_fit(stop_atr: float) -> float:
    """1 at 0.75 ATR, about 0.5 near 0.25 or 1.5, near 0 at 0 or past 3."""
    if stop_atr <= 0:
        return 0.5
    return math.exp(-((stop_atr - 0.75) / 0.70) ** 2)


def setup_quality(
    *,
    side: str,
    entry: float,
    stop: float,
    take_profit: float,
    absorb: float | None = None,
    swing: float | None = None,
    sweep: float | None = None,
    macro: str = "range",
    macro_adx: float = 0.0,
    atr: float | None = None,
    maker_fee: float = MAKER_FEE_RATE,
    taker_fee: float = TAKER_FEE_RATE,
) -> SetupQuality:
    """One armed setup. Every input is optional except entry, stop, and TP."""
    dist = abs(float(entry) - float(stop))
    tp1_r = abs(float(take_profit) - float(entry)) / dist if dist > 0 else 0.0
    absorb_v = _absorb_value(absorb)
    direction = str(macro or "range")
    if direction == "up":
        align = 1.0 if side == "long" else -1.0
    elif direction == "down":
        align = 1.0 if side == "short" else -1.0
    else:
        align = 0.0
    try:
        adx = max(0.0, float(macro_adx or 0.0))
    except (TypeError, ValueError):
        adx = 0.0
    if atr is not None and float(atr) > 0 and dist > 0:
        stop_atr = dist / float(atr)
    else:
        stop_atr = 0.0
    fit = _stop_fit(stop_atr)
    sweep_bps = sweep_depth_bps(side, swing, sweep)
    fee = abs(float(entry)) * float(maker_fee) + abs(float(stop)) * float(taker_fee)
    fee_drag = fee / dist if dist > 0 else 1.0

    quality = (
        min(tp1_r, 5.0) * 1.2
        + min(absorb_v, 4.0) * 0.6
        + align * min(adx, 60.0) / 30.0
        + fit * 1.0
        + min(sweep_bps, 40.0) / 25.0
        - min(max(fee_drag, 0.0), 1.0) * 2.0
    )
    return SetupQuality(
        quality=float(quality),
        tp1_r=float(tp1_r),
        absorb=absorb_v,
        macro=direction,
        macro_adx=adx,
        macro_align=align,
        stop_atr=float(stop_atr),
        stop_fit=float(fit),
        sweep_bps=float(sweep_bps),
        fee_drag=float(fee_drag),
    )


def macro_for_quality(engine, coin: str) -> tuple[str, float]:
    """Combined macro direction and the leading ADX, or range / 0."""
    cache = getattr(engine, "_macro_cache", None) or {}
    hit = cache.get(canon_coin(coin))
    if not hit or len(hit) < 2:
        return "range", 0.0
    read = hit[1]
    direction = str(getattr(read, "macro", "range") or "range")
    h4 = getattr(read, "h4", None)
    h1 = getattr(read, "h1", None)
    adx = None
    if h4 is not None and getattr(h4, "adx", None) is not None:
        adx = h4.adx
    elif h1 is not None and getattr(h1, "adx", None) is not None:
        adx = h1.adx
    try:
        strength = float(adx) if adx is not None else 0.0
    except (TypeError, ValueError):
        strength = 0.0
    return direction, strength


def quality_for_decision(decision, bars, engine, *, now: float, maker_fee: float, taker_fee: float) -> SetupQuality | None:
    """Quality for an armed decision. None when there is no intent."""
    intent = getattr(decision, "intent", None)
    if intent is None or not getattr(decision, "armed", False):
        return None
    side = str(getattr(intent, "side", None) or getattr(decision, "side", None) or "")
    if side not in ("long", "short"):
        return None
    macro, adx = macro_for_quality(engine, decision.coin)
    try:
        atr = atr14(bars or [], now)
    except Exception:
        atr = None
    return setup_quality(
        side=side,
        entry=float(intent.limit_px),
        stop=float(intent.stop),
        take_profit=float(intent.take_profit),
        absorb=getattr(decision, "absorb", None),
        swing=getattr(decision, "swing", None),
        sweep=getattr(decision, "sweep_price", None),
        macro=macro,
        macro_adx=adx,
        atr=atr,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def rank_armed_hunts(hunts: list, mode: str) -> list:
    """Reorder armed, postable hunts by quality. Other slots stay put.

    ``on`` only. ``shadow`` and ``off`` return the same list object order.
    A missing quality sorts last among the armed slots. Ties keep the
    earlier hunt index (SYMBOLS order).
    """
    if mode != "on" or len(hunts) < 2:
        return list(hunts)
    armed = [
        i
        for i, hunt in enumerate(hunts)
        if hunt.get("post")
        and getattr(hunt.get("decision"), "armed", False)
        and getattr(hunt.get("decision"), "intent", None) is not None
    ]
    if len(armed) < 2:
        return list(hunts)
    order = sorted(
        armed,
        key=lambda i: (
            -float(getattr(hunts[i]["decision"], "quality", None) if getattr(hunts[i]["decision"], "quality", None) is not None else -1e18),
            i,
        ),
    )
    out = list(hunts)
    for slot, src in zip(armed, order):
        out[slot] = hunts[src]
    return out

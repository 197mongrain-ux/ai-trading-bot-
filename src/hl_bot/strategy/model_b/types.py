"""Shared Model B types.

Trade prints are aggressor-side tape: ``{ts, coin, price, size, side}``.
``side`` is ``"buy"`` or ``"sell"``. ``None`` means the feed did not supply
an aggressor side — the engine fails closed (``NO_SIDE``) and does not infer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from hl_bot.strategy.model_b.universe import canon_coin


@dataclass(frozen=True)
class TradePrint:
    ts: float
    coin: str
    price: float
    size: float
    side: str | None
    seq: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", canon_coin(self.coin))


@dataclass(frozen=True)
class Swing:
    kind: str  # "low" | "high"
    price: float
    ts: float  # bar open, seconds


@dataclass(frozen=True)
class Pool:
    name: str  # PDH | PDL | WKH | WKL
    price: float
    taken: bool = False


@dataclass(frozen=True)
class Bias:
    """Directional bias drops the other side. ``NONE`` allows both.

    ``pool`` is the nearest untaken pool when the side is long or short.
    ``pool_above`` / ``pool_below`` are the nearest untaken pools on each
    side and are that side's liquidity target, including when ``side``
    is ``NONE``.
    """

    side: str  # long | short | NONE
    pool: Pool | None = None
    pool_above: Pool | None = None
    pool_below: Pool | None = None


@dataclass(frozen=True)
class AloIntent:
    """Post-only Add-Liquidity-Only order. Never a market or IOC."""

    coin: str
    side: str
    limit_px: float
    size: float
    stop: float
    take_profit: float
    swing_id: str
    tif: str = "Alo"
    market_fallback: bool = False
    leverage: int = 20
    # 0 = rest until the thesis is stale. A positive value is an optional timer.
    work_sec: float = 0.0
    sweep_px: float | None = None
    tick: float = 0.0
    # Liquidity target on this side (nearest swing or untaken pool).
    # Reused if the stop is widened on fill. Not a 2R cap.
    pool_px: float | None = None
    tp_r: float = 1.5


@dataclass
class Decision:
    """One arm or one fail. ``score`` is logged and is not an arm gate.

    The closer-ticker cancel reads the score stored on the resting Alo.
    """

    coin: str
    bias: str
    pool: str | None
    swing: float | None
    sweep_price: float | None
    absorb: float | None
    window_delta: float | None
    last_15s_delta: float | None
    score: int
    volume_tag: str
    armed: bool
    fail_reason: str | None
    intent: AloIntent | None = None
    # Prints inside the 90s window, and the floor they were compared to.
    # Logged on THIN_TAPE as prints=N/M. Not a second gate.
    print_count: int = 0
    min_prints: int = 30
    extra: dict = field(default_factory=dict)
    # Session volume profile. Logged only. evaluate() does not read these.
    vp_poc: float | None = None
    vp_vah: float | None = None
    vp_val: float | None = None
    nearest_lvn_on_side: float | None = None
    sweep_to_val_bps: float | None = None
    sweep_to_lvn_bps: float | None = None
    vp_tag: str = "none"
    catalyst_flag: bool = False
    # ``wide_stop`` when the armed stop is wider than 1.5% of entry.
    # Size was reduced (risk / distance) instead of scrapping the idea.
    # Null on a normal arm and on every fail. Not a gate.
    size_adjust: str | None = None
    # ``window``, ``last_15s``, or ``both`` when the flat delta band
    # passed a print the strict sign check would have failed. Null when
    # the delta was already non-adverse, and on every fail.
    delta_flat: str | None = None
    # Coin size actually compared (the larger of the two terms below),
    # each term, and the price used to scale the USDC band.
    delta_flat_eps: float | None = None
    delta_flat_usdc_eps: float | None = None
    delta_flat_coin_eps: float | None = None
    delta_flat_px: float | None = None
    # BAD_TP log only. Null on an arm and on every other fail.
    r_distance: float | None = None
    pool_distance: float | None = None
    pool_r: float | None = None
    bad_tp_why: str | None = None
    # 15m / 1h structure label (``15m:bear,1h:range``) and the counter-flow
    # read. STRUCTURE / COUNTER_FLOW are gates; these are their log fields.
    structure: str | None = None
    counter_flow: str | None = None
    # "would_block" when MODEL_B_STRUCTURE_FILTER=shadow would have blocked.
    structure_shadow: str | None = None
    # Sizing brake read: notional cap leverage and the stop distance the
    # size was computed from (>= the real stop distance).
    sizing_dist: float | None = None
    # Two-sided hunt only (MODEL_B_TWO_SIDED=1): the side this decision is
    # for, and the other side's decision (logged as its own FAIL line).
    # Both stay None / empty with the flag off, so the old log is unchanged.
    side: str | None = None
    other_sides: tuple = ()
    # TP next-pool rule only (MODEL_B_TP_MIN_POOL_R > 0): the 15m/1h trend
    # read on arms and on TP decisions. None (and not journaled) otherwise.
    trend: str | None = None
    # MODEL_B_MACRO_SIDE_ONLY on/shadow: the 1h/4h ADX macro read on arms and
    # MACRO_SIDE fails ("would_block ..." in shadow). None (not journaled) off.
    macro: str | None = None

    def to_log(self) -> dict:
        def _num(value: float | None) -> float | None:
            if value is None:
                return None
            # Infinite absorb is a zero reclaim-side denominator. Journal
            # null. Do not write 1e6; that reads as a real ratio.
            if isinstance(value, float) and math.isinf(value):
                return None
            if isinstance(value, float) and math.isnan(value):
                return None
            return value

        return {
            "coin": self.coin,
            "bias": self.bias,
            "pool": self.pool,
            "swing": self.swing,
            "sweep_price": self.sweep_price,
            "absorb": _num(self.absorb),
            "window_delta": _num(self.window_delta),
            "last_15s_delta": _num(self.last_15s_delta),
            "score": self.score,
            "volume_tag": self.volume_tag,
            "fail_reason": self.fail_reason,
            "armed": self.armed,
            "print_count": self.print_count,
            "min_prints": self.min_prints,
            "vp_poc": _num(self.vp_poc),
            "vp_vah": _num(self.vp_vah),
            "vp_val": _num(self.vp_val),
            "nearest_lvn_on_side": _num(self.nearest_lvn_on_side),
            "sweep_to_val_bps": _num(self.sweep_to_val_bps),
            "sweep_to_lvn_bps": _num(self.sweep_to_lvn_bps),
            "vp_tag": self.vp_tag,
            "catalyst_flag": bool(self.catalyst_flag),
            "size_adjust": self.size_adjust,
            "delta_flat": self.delta_flat,
            "delta_flat_eps": _num(self.delta_flat_eps),
            "delta_flat_usdc_eps": _num(self.delta_flat_usdc_eps),
            "delta_flat_coin_eps": _num(self.delta_flat_coin_eps),
            "delta_flat_px": _num(self.delta_flat_px),
            "r_distance": _num(self.r_distance),
            "pool_distance": _num(self.pool_distance),
            "pool_r": _num(self.pool_r),
            "bad_tp_why": self.bad_tp_why,
            "structure": self.structure,
            "counter_flow": self.counter_flow,
            "structure_shadow": self.structure_shadow,
            "sizing_dist": _num(self.sizing_dist),
            **({"trend": self.trend} if self.trend is not None else {}),
            **({"macro": self.macro} if self.macro is not None else {}),
            **(
                {
                    "side": self.side,
                    "other_sides": [
                        {
                            "side": o.side,
                            "fail_reason": o.fail_reason,
                            "armed": o.armed,
                            "swing": o.swing,
                            "sweep_price": o.sweep_price,
                            "window_delta": _num(o.window_delta),
                            "last_15s_delta": _num(o.last_15s_delta),
                        }
                        for o in self.other_sides
                    ],
                }
                if self.side is not None
                else {}
            ),
        }

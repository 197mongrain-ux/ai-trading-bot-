"""Shared Model B types.

Trade prints are aggressor-side tape: ``{ts, coin, price, size, side}``.
``side`` is ``"buy"`` or ``"sell"``. ``None`` means the feed did not supply
an aggressor side — the engine fails closed (``NO_SIDE``) and does not infer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class TradePrint:
    ts: float
    coin: str
    price: float
    size: float
    side: str | None
    seq: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())


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
    side and cap that side's TP, including when ``side`` is ``NONE``.
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
    # Untaken pool on this side. Caps TP again if the stop is widened on fill.
    pool_px: float | None = None
    tp_r: float = 1.5


@dataclass
class Decision:
    """One arm or one fail. ``score`` is logged and never read as a gate."""

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

    def to_log(self) -> dict:
        def _num(value: float | None) -> float | None:
            if value is None:
                return None
            if isinstance(value, float) and math.isinf(value):
                return 1e6 if value > 0 else -1e6
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
        }

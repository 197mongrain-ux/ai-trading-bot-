"""Footprint bars and a session volume profile from aggressor tape.

Hyperliquid prints use ``B`` for a buyer aggressor (lifts the ask) and
``A`` for a seller aggressor (hits the bid). Delta is buy size minus
sell size. CVD is the running sum of delta, reset at each UTC day.

A sell imbalance is diagonal: sell size at a price bucket versus buy
size one tick higher. A buy imbalance is buy size at a price versus
sell size one tick lower. An empty opposite side is an imbalance when
the aggressive side clears the minimum size. Stacked means that many
imbalances on consecutive ticks.
"""

from __future__ import annotations

import gzip
import math
from dataclasses import dataclass
from pathlib import Path

BAR_SEC = {"1m": 60, "3m": 180, "5m": 300}
VALUE_AREA_FRACTION = 0.70
MIN_PROFILE_PRINTS = 30


def hl_tick(price: float) -> float:
    """Hyperliquid 5-significant-figure increment. BTC-scale prices land on 1.0."""
    if price <= 0:
        return 1.0
    magnitude = math.floor(math.log10(abs(price)))
    return max(10 ** (magnitude - 4), 1e-6)


def bucket_price(price: float, tick: float) -> float:
    step = float(tick) if tick and tick > 0 else 1.0
    return round(round(float(price) / step) * step, 10)


@dataclass(frozen=True)
class TapePrint:
    ts: float
    price: float
    size: float
    side: str  # buy | sell


@dataclass(frozen=True)
class BookLevel:
    price: float
    buy: float
    sell: float


@dataclass(frozen=True)
class FootprintBar:
    t: float
    o: float
    h: float
    l: float
    c: float
    buy: float
    sell: float
    delta: float
    cvd: float
    levels: tuple[BookLevel, ...]


@dataclass(frozen=True)
class SessionProfile:
    poc: float | None
    vah: float | None
    val: float | None
    session_low: float
    session_high: float
    total: float
    prints: int
    ok: bool


def map_side(raw: object) -> str | None:
    if raw is None:
        return None
    key = str(raw).strip().upper()
    if key in {"B", "BUY"}:
        return "buy"
    if key in {"A", "SELL"}:
        return "sell"
    if key.lower() in {"buy", "sell"}:
        return key.lower()
    return None


def parse_trade(raw: dict) -> TapePrint | None:
    """One recorder line. Missing side, price, or size is dropped."""
    if not isinstance(raw, dict):
        return None
    side = map_side(raw.get("side"))
    try:
        price = float(raw.get("px"))
        size = float(raw.get("sz"))
        ts = float(raw.get("time"))
    except (TypeError, ValueError):
        return None
    if side is None or price <= 0 or size <= 0:
        return None
    if ts > 1e11:
        ts = ts / 1000.0
    return TapePrint(ts, price, size, side)


def read_trades(path: Path) -> list[TapePrint]:
    """JSONL or gzip JSONL, including a multi-member gzip the recorder appends."""
    opener = gzip.open if str(path).endswith(".gz") else open
    out: list[TapePrint] = []
    with opener(path, "rt") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            import json

            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            print_ = parse_trade(raw)
            if print_ is not None:
                out.append(print_)
    out.sort(key=lambda item: (item.ts, item.price))
    return out


def build_footprint(
    prints: list[TapePrint],
    *,
    bar_sec: int,
    tick: float,
) -> list[FootprintBar]:
    """Closed-form bars. CVD resets when the UTC day changes."""
    if bar_sec <= 0 or not prints:
        return []
    step = float(tick) if tick and tick > 0 else 1.0
    groups: dict[int, list[TapePrint]] = {}
    for print_ in prints:
        if print_.side not in {"buy", "sell"} or print_.price <= 0 or print_.size <= 0:
            continue
        key = int(print_.ts // bar_sec) * bar_sec
        groups.setdefault(key, []).append(print_)
    bars: list[FootprintBar] = []
    cvd = 0.0
    day = None
    for key in sorted(groups):
        bar_day = int(key // 86400)
        if day is None or bar_day != day:
            cvd = 0.0
            day = bar_day
        chunk = groups[key]
        buy = 0.0
        sell = 0.0
        book: dict[float, list[float]] = {}
        high = low = chunk[0].price
        for print_ in chunk:
            high = max(high, print_.price)
            low = min(low, print_.price)
            px = bucket_price(print_.price, step)
            cell = book.setdefault(px, [0.0, 0.0])
            if print_.side == "buy":
                buy += print_.size
                cell[0] += print_.size
            else:
                sell += print_.size
                cell[1] += print_.size
        delta = buy - sell
        cvd += delta
        levels = tuple(
            BookLevel(price, vols[0], vols[1]) for price, vols in sorted(book.items())
        )
        bars.append(
            FootprintBar(
                t=float(key),
                o=float(chunk[0].price),
                h=float(high),
                l=float(low),
                c=float(chunk[-1].price),
                buy=buy,
                sell=sell,
                delta=delta,
                cvd=cvd,
                levels=levels,
            )
        )
    return bars


def _clears(volume: float, min_volume: float) -> bool:
    if volume <= 0:
        return False
    if min_volume > 0 and volume + 1e-12 < min_volume:
        return False
    return True


def _imbalanced(aggressive: float, opposite: float, ratio: float, min_volume: float) -> bool:
    if not _clears(aggressive, min_volume):
        return False
    if opposite <= 0:
        return True
    return aggressive / opposite + 1e-12 >= float(ratio)


def diagonal_imbalances(
    bar: FootprintBar,
    *,
    ratio: float,
    min_volume: float,
    tick: float,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Sell-imbalance prices, then buy-imbalance prices, ascending."""
    step = float(tick) if tick and tick > 0 else 1.0
    book = {bucket_price(level.price, step): level for level in bar.levels}
    sells: list[float] = []
    buys: list[float] = []
    for price, level in book.items():
        above = book.get(bucket_price(price + step, step))
        below = book.get(bucket_price(price - step, step))
        opp_buy = 0.0 if above is None else above.buy
        opp_sell = 0.0 if below is None else below.sell
        if _imbalanced(level.sell, opp_buy, ratio, min_volume):
            sells.append(price)
        if _imbalanced(level.buy, opp_sell, ratio, min_volume):
            buys.append(price)
    return tuple(sells), tuple(buys)


def stacked_runs(prices: tuple[float, ...] | list[float], tick: float) -> list[tuple[float, ...]]:
    """Runs of prices exactly one tick apart. A gap starts a new run."""
    step = float(tick) if tick and tick > 0 else 1.0
    ordered = sorted(set(bucket_price(price, step) for price in prices))
    if not ordered:
        return []
    runs: list[list[float]] = [[ordered[0]]]
    for price in ordered[1:]:
        gap = round((price - runs[-1][-1]) / step)
        if gap == 1:
            runs[-1].append(price)
        else:
            runs.append([price])
    return [tuple(run) for run in runs]


def near_extreme(prices: tuple[float, ...] | list[float], bar: FootprintBar, frac: float, side: str) -> bool:
    """Every price sits in the bottom fraction (long) or top fraction (short) of the bar."""
    if not prices:
        return False
    span = float(bar.h) - float(bar.l)
    if span <= 0:
        return True
    band = max(0.0, min(1.0, float(frac))) * span
    if side == "long":
        ceiling = float(bar.l) + band
        return all(price <= ceiling + 1e-9 for price in prices)
    floor = float(bar.h) - band
    return all(price >= floor - 1e-9 for price in prices)


def heavy_side(bar: FootprintBar, frac: float, side: str) -> bool:
    """Aggressive volume in the extreme band is at least the other side, and positive."""
    span = float(bar.h) - float(bar.l)
    band = 0.0 if span <= 0 else max(0.0, min(1.0, float(frac))) * span
    buy = 0.0
    sell = 0.0
    for level in bar.levels:
        if side == "long":
            if span > 0 and level.price > float(bar.l) + band + 1e-9:
                continue
        elif span > 0 and level.price < float(bar.h) - band - 1e-9:
            continue
        buy += level.buy
        sell += level.sell
    if side == "long":
        return sell > 0 and sell + 1e-12 >= buy
    return buy > 0 and buy + 1e-12 >= sell


def cvd_slope(bars: list[FootprintBar], index: int, lookback: int) -> float:
    """Change in CVD per bar over the last ``lookback`` bars, ending at ``index``."""
    if not bars or index < 0 or index >= len(bars):
        return 0.0
    start = max(0, index - max(1, int(lookback)))
    span = index - start
    if span <= 0:
        return float(bars[index].delta)
    return (float(bars[index].cvd) - float(bars[start].cvd)) / float(span)


def session_volume_profile(
    prints: list[TapePrint],
    *,
    tick: float,
    start: float,
    end: float,
) -> SessionProfile:
    """UTC-session profile from prints in ``[start, end)``.

    POC is the fullest tick. The value area is 70% of volume expanded
    from the POC. Fewer than 30 prints, or a single price, is not a level.
    """
    empty = SessionProfile(None, None, None, 0.0, 0.0, 0.0, 0, False)
    chosen = [item for item in prints if start - 1e-9 <= item.ts < end and item.price > 0 and item.size > 0]
    if len(chosen) < MIN_PROFILE_PRINTS:
        return empty
    step = float(tick) if tick and tick > 0 else 1.0
    lo = min(item.price for item in chosen)
    hi = max(item.price for item in chosen)
    if hi <= lo:
        return empty
    n = int(round((hi - lo) / step)) + 1
    if n < 2 or n > 20000:
        return empty
    hist = [0.0] * n
    for item in chosen:
        idx = int(round((bucket_price(item.price, step) - bucket_price(lo, step)) / step))
        idx = min(n - 1, max(0, idx))
        hist[idx] += item.size
    total = sum(hist)
    if total <= 0:
        return empty
    poc_i = max(range(n), key=lambda i: (hist[i], -i))
    area_lo, area_hi = _value_area(hist, poc_i, total)
    origin = bucket_price(lo, step)
    return SessionProfile(
        poc=round(origin + poc_i * step, 10),
        vah=round(origin + area_hi * step, 10),
        val=round(origin + area_lo * step, 10),
        session_low=float(lo),
        session_high=float(hi),
        total=total,
        prints=len(chosen),
        ok=True,
    )


def _value_area(hist: list[float], poc_i: int, total: float) -> tuple[int, int]:
    target = VALUE_AREA_FRACTION * total
    lo = hi = poc_i
    acc = hist[poc_i]
    n = len(hist)
    while acc + 1e-9 < target and (lo > 0 or hi < n - 1):
        can_left = lo > 0
        can_right = hi < n - 1
        left = hist[lo - 1] if can_left else -1.0
        right = hist[hi + 1] if can_right else -1.0
        if can_left and can_right and left == right:
            lo -= 1
            hi += 1
            acc += hist[lo] + hist[hi]
        elif right > left and can_right:
            hi += 1
            acc += hist[hi]
        elif can_left:
            lo -= 1
            acc += hist[lo]
        else:
            hi += 1
            acc += hist[hi]
    return lo, hi

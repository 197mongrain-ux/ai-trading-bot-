"""Take-profit targeting for Model B.

Three switches, each with a rollback to today's pick:

- ``MODEL_B_TP_UNTAKEN_ONLY`` drops TP-side 1m swings price has already
  traded through. Pools still use their own taken flag. The 1R/fee floor,
  the 1.5R next-pool walk, the 3R countertrend skip, and the moved-stop
  ``tp_r`` path are unchanged.
- ``MODEL_B_TP_REFRESH_ON_FILL`` re-runs that same pick at the fill when
  the planned target was traded through while the Alo rested. The stop
  and the size are not touched. A failed re-pick keeps the planned TP.
- ``MODEL_B_TP_RUNNER`` (default shadow) splits the exit: TP1 is the
  normal target for ``1 - frac``, and the runner aims at the next
  significant untaken pool. After TP1 the stop goes to breakeven and
  trails confirmed 1m swings, never loosening. Shadow only logs.

Size, leverage, and margin are not computed here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import min_tp_distance, next_liquidity
from hl_bot.strategy.model_b.swings import _closed_bars, confirmed_swings
from hl_bot.strategy.model_b.universe import canon_coin

TRI_MODES = ("off", "on", "shadow")


def parse_tri_mode(raw: str | None, default: str) -> str:
    """``1``/``on``, ``0``/``off``, ``shadow``. Anything else is returned for validation."""
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return "on"
    if value in {"0", "false", "no", "off"}:
        return "off"
    if value in {"shadow", "log", "log_only", "logonly"}:
        return "shadow"
    return value


def level_spent(
    side: str,
    price: float,
    bars: list[dict],
    now: float,
    last: float | None,
    *,
    formed_ts: float | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> bool:
    """True when price has traded through a TP-side level.

    A long target is a high: a later bar high, or the last trade, at or
    above it. A short target is a low: a later bar low, or the last trade,
    at or below it. ``formed_ts`` skips the swing's own bar. ``since_ts`` /
    ``until_ts`` limit the window (arm to fill).
    """
    if price <= 0 or side not in ("long", "short"):
        return False
    high = side == "long"
    for open_sec, bar in _closed_bars(bars, now):
        if formed_ts is not None and open_sec <= float(formed_ts) + 1e-9:
            continue
        if since_ts is not None and open_sec + 60.0 <= float(since_ts) + 1e-9:
            continue
        if until_ts is not None and open_sec > float(until_ts) + 1e-9:
            continue
        extreme = float(bar.get("h" if high else "l") or 0)
        if extreme <= 0:
            continue
        if high and extreme + 1e-12 >= float(price):
            return True
        if not high and extreme - 1e-12 <= float(price):
            return True
    if last is not None and float(last) > 0:
        last_f = float(last)
        if high and last_f + 1e-12 >= float(price):
            return True
        if not high and last_f - 1e-12 <= float(price):
            return True
    return False


def filter_spent_swing_prices(
    side: str,
    swings,
    bars: list[dict],
    now: float,
    last: float | None,
) -> set[float]:
    """Prices whose latest TP-side swing has already been traded through.

    An older swing at the same price does not condemn a later one. Spent
    is only what happened after the most recent swing there.
    """
    latest: dict[float, float] = {}
    for swing in swings:
        price = float(swing.price)
        formed = float(swing.ts)
        prev = latest.get(price)
        if prev is None or formed >= prev:
            latest[price] = formed
    spent: set[float] = set()
    for price, formed in latest.items():
        if level_spent(side, price, bars, now, last, formed_ts=formed):
            spent.add(price)
    return spent


@dataclass
class TpWalk:
    """One pass of the existing liquidity walk. ``fail`` is a hunt reason or None."""

    target: float | None = None
    fail: str | None = None
    pool_r: float = 0.0
    trend_label: str | None = None
    logs: list[tuple[str, tuple]] = field(default_factory=list)


def walk_liquidity_tp(
    side: str,
    entry: float,
    stop: float,
    tick: float,
    levels: list[float],
    *,
    coin: str,
    tp_r: float,
    min_pool_r: float,
    max_pool_r: float,
    far_skip: bool,
    moved_floor: float | None,
    trend_fn,
    distance_fn=None,
) -> TpWalk:
    """The PR #14 TP walk. Levels in, target or a fail reason out.

    ``trend_fn`` is called only when the nearest pool is under
    ``min_pool_r``, matching the hunt (no trend read on a normal arm).
    ``distance_fn`` is the engine's ``min_tp_distance`` so a test patch
    on that name still applies.
    """
    out = TpWalk()
    floor_of = distance_fn or min_tp_distance
    floor = floor_of(entry, stop)
    if moved_floor is not None:
        target = next_liquidity(side, entry, tick, levels, min_dist=float(moved_floor))
        if target is None:
            out.fail = "STOP_TOO_TIGHT"
            return out
        floor = float(moved_floor)
    target = next_liquidity(side, entry, tick, levels, min_dist=floor)
    if (
        target is not None
        and moved_floor is None
        and float(min_pool_r) > 0
    ):
        want = floor_of(entry, stop, min_r=float(min_pool_r))
        dist = abs(entry - stop)
        gap = abs(target - entry)
        if gap + 1e-12 < want:
            trend = trend_fn()
            out.trend_label = trend.label()
            with_trend = trend.is_with(side)
            out.logs.append(
                ("MODEL_B TREND %s %s side=%s", (coin, out.trend_label, side))
            )
            farther = next_liquidity(side, entry, tick, levels, min_dist=want)
            near_r = gap / dist if dist > 0 else 0.0
            if farther is not None:
                far_r = abs(farther - entry) / dist if dist > 0 else 0.0
                too_far = float(max_pool_r) > 0 and far_r > float(max_pool_r) + 1e-9
                if too_far and not with_trend and far_skip:
                    out.logs.append(
                        (
                            "MODEL_B TP_TOO_FAR_COUNTERTREND %s %s nearest=%s r=%.2f next=%s r=%.2f max_r=%s %s",
                            (coin, side, target, near_r, farther, far_r, max_pool_r, out.trend_label),
                        )
                    )
                    out.fail = "TP_TOO_FAR_COUNTERTREND"
                    out.pool_r = round(far_r, 4)
                    out.target = target
                    return out
                out.logs.append(
                    (
                        "MODEL_B TP_NEXT_POOL %s %s nearest=%s r=%.2f -> tp=%s r=%.2f min_r=%s%s",
                        (
                            coin,
                            side,
                            target,
                            near_r,
                            farther,
                            far_r,
                            min_pool_r,
                            " far_with_trend=1" if too_far else "",
                        ),
                    )
                )
                target = farther
            else:
                if not with_trend and far_skip:
                    out.logs.append(
                        (
                            "MODEL_B TP_UNDER_1_5R_COUNTERTREND %s %s r=%.2f tp=%s min_r=%s %s",
                            (coin, side, near_r, target, min_pool_r, out.trend_label),
                        )
                    )
                    out.fail = "TP_UNDER_1_5R_COUNTERTREND"
                    out.pool_r = round(near_r, 4)
                    out.target = target
                    return out
                out.logs.append(
                    (
                        "MODEL_B TP_UNDER_1_5R %s %s r=%.2f tp=%s min_r=%s (no pool >= min_r; nearest kept)",
                        (coin, side, near_r, target, min_pool_r),
                    )
                )
    out.target = target
    return out


def _resample(bars: list[dict], seconds: float, now: float) -> list[dict]:
    buckets: dict[int, dict] = {}
    for open_sec, bar in _closed_bars(bars, now):
        key = int(open_sec // seconds * seconds)
        if key + seconds > float(now) + 1e-9:
            continue
        row = buckets.get(key)
        if row is None:
            buckets[key] = {
                "t": float(key),
                "o": bar.get("o"),
                "h": bar.get("h"),
                "l": bar.get("l"),
                "c": bar.get("c"),
            }
        else:
            row["h"] = max(float(row["h"] or 0), float(bar.get("h") or 0))
            row["l"] = min(float(row["l"] or 0) or 1e18, float(bar.get("l") or 0))
            row["c"] = bar.get("c")
    return [buckets[k] for k in sorted(buckets)]


def significant_levels(
    side: str,
    entry: float,
    bars: list[dict],
    now: float,
    last: float | None,
    tick: float,
) -> list[float]:
    """Untaken 5m/15m swings, equal 1m highs/lows, and PDH/PDL/WKH/WKL.

    Equal means at least two 1m swings within ``max(2 ticks, 1.5 bps)``.
    The pool is the cluster extreme, and it is spent once price trades
    through it after the later swing.
    """
    kind = "high" if side == "long" else "low"
    found: list[float] = []
    for sec in (300.0, 900.0):
        resampled = _resample(bars, sec, now)
        for swing in confirmed_swings(resampled, kind=kind, now=now):
            # The swing bar itself printed this extreme. Spent only after it closed.
            if level_spent(side, swing.price, bars, now, last, formed_ts=swing.ts + sec - 60.0):
                continue
            found.append(float(swing.price))
    swings = confirmed_swings(bars, kind=kind, now=now)
    tol = max(2.0 * float(tick), abs(float(entry)) * 1.5e-4)
    ordered = sorted(swings, key=lambda s: s.price)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1].price - ordered[i].price <= tol + 1e-12:
            j += 1
        if j > i:
            group = ordered[i : j + 1]
            px = max(s.price for s in group) if kind == "high" else min(s.price for s in group)
            formed = max(s.ts for s in group)
            if not level_spent(side, px, bars, now, last, formed_ts=formed):
                found.append(float(px))
        i = j + 1
    try:
        pools = pools_from_bars(bars, now, last_price=last if last and last > 0 else entry)
    except Exception:
        pools = []
    for pool in pools:
        if pool.taken or pool.price <= 0:
            continue
        if side == "long" and pool.price > entry:
            found.append(float(pool.price))
        elif side == "short" and pool.price < entry:
            found.append(float(pool.price))
    return found


def runner_target(
    side: str,
    entry: float,
    stop: float,
    tp1: float,
    levels: list[float],
    *,
    max_r: float = 5.0,
) -> float:
    """Nearest significant pool at ``>= max(TP1_R + 1, 2.5)`` and ``<= max_r``.

    No such pool: ``max_r`` itself, on the trade side of the entry.
    """
    dist = abs(float(entry) - float(stop))
    if dist <= 0 or entry <= 0:
        return float(tp1)
    tp1_r = abs(float(tp1) - float(entry)) / dist
    min_r = max(tp1_r + 1.0, 2.5)
    cap = float(max_r) if float(max_r) > 0 else 5.0
    best: float | None = None
    best_gap = 0.0
    for raw in levels:
        px = float(raw)
        gap = (px - float(entry)) if side == "long" else (float(entry) - px)
        if gap + 1e-12 < min_r * dist:
            continue
        if gap > cap * dist + 1e-9:
            continue
        if best is None or gap < best_gap:
            best = px
            best_gap = gap
    if best is not None:
        return best
    return float(entry) + cap * dist if side == "long" else float(entry) - cap * dist


def split_runner_size(size: float, frac: float) -> tuple[float, float]:
    """``(tp1_size, runner_size)`` summing to ``size``. Runner is the remainder."""
    size = float(size)
    frac = float(frac)
    if size <= 0 or not (0.0 < frac < 1.0):
        return size, 0.0
    tp1 = math.floor(size * (1.0 - frac) * 1e8) / 1e8
    runner = size - tp1
    if tp1 <= 0 or runner <= 0:
        return size, 0.0
    return tp1, runner


def trail_stop(
    side: str,
    bars: list[dict],
    now: float,
    tick: float,
    current: float,
) -> float | None:
    """Latest confirmed 1m swing, one tick beyond it, only if that tightens.

    Long: swing low minus one tick, and only when that is above ``current``.
    Short: swing high plus one tick, and only when that is below ``current``.
    """
    if tick <= 0 or current <= 0 or side not in ("long", "short"):
        return None
    kind = "low" if side == "long" else "high"
    swings = confirmed_swings(bars, kind=kind, now=now)
    if not swings:
        return None
    pivot = float(swings[-1].price)
    if side == "long":
        cand = pivot - float(tick)
        if cand <= float(current) + 1e-12:
            return None
        return cand
    cand = pivot + float(tick)
    if cand >= float(current) - 1e-12:
        return None
    return cand


def restore_plan(rows: list[dict], coin: str) -> dict | None:
    """Journal plan for a restarted position, or None when this coin has no open.

    The latest ``open`` that is not followed by a ``close`` wins. A
    ``model_b_tp1`` after that open means TP1 already filled: the fallback
    stop is breakeven (the entry) and the TP is the runner when one was
    stored. Otherwise the stop and TP are the ones journaled on the open.
    """
    name = canon_coin(coin)
    last_open: dict | None = None
    tp1 = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = canon_coin(row.get("symbol") or row.get("coin") or "")
        if sym != name:
            continue
        event = row.get("event")
        if event == "open":
            last_open = row
            tp1 = False
        elif event == "close" and last_open is not None:
            last_open = None
            tp1 = False
        elif event == "model_b_tp1" and last_open is not None:
            tp1 = True
    if last_open is None:
        return None
    try:
        entry = float(last_open.get("price") or 0)
        stop = float(last_open.get("stop") or 0)
        tp = float(last_open.get("tp") or 0)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or stop <= 0 or tp <= 0:
        return None
    runner = last_open.get("runner_px")
    try:
        runner_px = float(runner) if runner not in (None, "") else None
    except (TypeError, ValueError):
        runner_px = None
    if runner_px is not None and runner_px <= 0:
        runner_px = None
    if tp1:
        use_stop = entry
        use_tp = runner_px if runner_px else tp
    else:
        use_stop = stop
        use_tp = tp
    try:
        size = float(last_open.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    return {
        "entry": entry,
        "stop": use_stop,
        "planned_stop": stop,
        "tp": use_tp,
        "runner_px": runner_px,
        "tp1_filled": tp1,
        "runner_on": bool(last_open.get("runner_on")),
        "size": size,
        "side": last_open.get("side") or "",
    }

"""One cash-session week. Research only. Not a verdict.

Window: Monday 5 Oct 2026 09:30 through Friday 9 Oct 2026 16:00,
America/Toronto. Entries only while that session is open. The swing
grid is the locked ``swing_search`` grid. Both 15m and 1h are run,
because this week is inside the 15m cache. That is 2,592 cells. Ranking
the top 5 after seeing them is a multiple comparison. Five sessions
cannot clear the 90-day bar, and nothing here changes a default.

The scalp book is the live sweep-and-reclaim geometry on 1m candles.
Absorption, print count, delta, and counter-flow need aggressor prints.
Those gates are off. No prints are invented.

Run: ``python -m hl_bot.research.last_week --cache /tmp/hl_swing_cache``
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hl_bot.exchange.info_client import _post_candle_snapshot
from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.footprint import hl_tick
from hl_bot.strategy.model_b.risk import (
    arm_take_profit,
    cap_size_to_loss,
    collides_with_fill,
    min_tp_distance,
    place_stop,
    size_from_stop,
    stop_buffer,
    stop_is_valid,
)
from hl_bot.strategy.model_b.engine import min_sweep_bps_for
from hl_bot.strategy.model_b.stop_slip import default_stop_slip_bps
from hl_bot.strategy.model_b.structure import structure_blocks, structure_states
from hl_bot.strategy.model_b.swing import (
    atr14_of,
    build_levels,
    closed_candles,
    exit_net,
    find_sweep,
    range_edges,
    read_swing_macro,
    session_open,
)
from hl_bot.strategy.model_b.swing_replay import COINS
from hl_bot.strategy.model_b.swing_search import (
    CRYPTO,
    ENTRIES,
    EQUITY0,
    LEVELS,
    MACROS,
    MIN_SWEEP_BPS,
    SESSION,
    Cell,
    Signal,
    Trade,
    _MACRO_4H,
    _MACRO_BOTH,
    _LEVEL_PARAMS,
    _expectancy,
    _stats,
    extract_signals,
    iter_cells,
    penalty,
    simulate,
    touch_reclaim,
)
from hl_bot.strategy.model_b.swings import (
    atr14,
    bar_open_sec,
    closed_prices,
    confirmed_swings,
    local_bar_extreme,
    select_swing,
    swing_id,
)
from hl_bot.strategy.model_b.tp_select import filter_spent_swing_prices, walk_liquidity_tp
from hl_bot.strategy.model_b.universe import DEFAULT_HUNT_COINS

# Fixed before any week result is read.
WEEK_MIN_TRADES = 5
TOP_N = 5
TZ = ZoneInfo("America/Toronto")
API = "https://api.hyperliquid.xyz"
# Friday 16:00 ET. A cache that ends earlier has not seen the cash close.
WEEK_OPEN = datetime(2026, 10, 5, 9, 30, tzinfo=TZ)
WEEK_FLAT = datetime(2026, 10, 9, 16, 0, tzinfo=TZ)
# Sunday night UTC. Friday's 3-day hold is not finished by then; say so.
MANAGE_UNTIL = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)
WARMUP = datetime(2026, 10, 3, 12, 0, tzinfo=TZ)

SCALP_RISK = 0.02
SCALP_TP_R = 1.5
SCALP_MIN_POOL_R = 1.5
SCALP_MAX_POOL_R = 3.0
SCALP_MIN_STOP_BPS = 15.0
SCALP_MIN_SWEEP = "0.3"
# Latest swing and stop prints: six hours. Structure resample: sixteen,
# which covers the 12h of 15m the live filter keeps.
SCALP_SWING_BARS = 360
SCALP_STRUCT_BARS = 16 * 60

DAYS = (
    "2026-10-05",
    "2026-10-06",
    "2026-10-07",
    "2026-10-08",
    "2026-10-09",
)


def week_start() -> float:
    return WEEK_OPEN.timestamp()


def week_end() -> float:
    """16:00 Friday. Session entries are strictly earlier."""
    return WEEK_FLAT.timestamp()


def cells_for_week() -> list[Cell]:
    """Every locked cell. 15m is inside this week, so none are skipped."""
    return iter_cells(90.0)


def merge_candles(old: list[dict], new: list[dict]) -> list[dict]:
    """Union by open time. A later copy of the same open replaces the older."""
    by: dict[int, dict] = {}
    for bar in list(old) + list(new):
        if not isinstance(bar, dict) or "t" not in bar:
            continue
        try:
            key = int(float(bar["t"]))
            row = {
                "t": float(bar["t"]),
                "o": float(bar["o"]),
                "h": float(bar["h"]),
                "l": float(bar["l"]),
                "c": float(bar["c"]),
                "v": float(bar.get("v") or 0),
            }
        except (TypeError, ValueError):
            continue
        if row["h"] <= 0 or row["l"] <= 0 or row["h"] < row["l"]:
            continue
        by[key] = row
    return [by[key] for key in sorted(by)]


def _close_ts(bar: dict, tf_sec: int) -> float:
    return bar_open_sec(float(bar["t"])) + tf_sec


def last_close(rows: list[dict], tf_sec: int) -> float:
    if not rows:
        return 0.0
    return _close_ts(rows[-1], tf_sec)


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return raw if isinstance(raw, list) else []


def _save_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def _coin_file(cache: Path, coin: str, tf: str) -> Path:
    return cache / f"{coin.replace(':', '_')}_{tf}.json"


def _fetch_range(coin: str, interval: str, start_ms: int, end_ms: int) -> tuple[list[dict], str]:
    """One or more candleSnapshot calls. 429 backs off. Empty is a note, not a raise."""
    span = 3_000 * 60 * 1000 if interval == "1m" else max(1, end_ms - start_ms)
    cursor = start_ms
    out: list[dict] = []
    note = ""
    while cursor < end_ms:
        nxt = min(end_ms, cursor + span)
        status, raw = 0, None
        for attempt in range(4):
            try:
                status, raw = _post_candle_snapshot(API, coin, interval, cursor, nxt, timeout=20.0)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                status, raw = 0, None
                note = f"{coin} {interval}: {exc}"
            if status == 429 or status >= 500 or status == 0:
                time.sleep(2.0 * (attempt + 1))
                continue
            break
        if status == 429:
            note = f"{coin} {interval}: HTTP 429"
            break
        if not isinstance(raw, list):
            note = f"{coin} {interval}: HTTP {status}"
            break
        out.extend(raw)
        cursor = nxt
        time.sleep(0.25)
    return out, note


def ensure_candles(cache: Path) -> dict:
    """Fill 1m, and extend 15m/1h through Friday's close, from Hyperliquid.

    Candles stay under ``cache`` (``/tmp``). They are not part of the repo.
    """
    notes: list[str] = []
    flat = week_end()
    manage_ms = int(MANAGE_UNTIL.timestamp() * 1000)
    for coin in COINS:
        for tf, sec in (("1h", 3600), ("15m", 900)):
            path = _coin_file(cache, coin, tf)
            rows = _load_json(path)
            have = last_close(rows, sec)
            if have + 1e-6 >= MANAGE_UNTIL.timestamp():
                continue
            start = flat - 3600 if have <= 0 else min(have, flat) - sec
            if have > 0:
                start = bar_open_sec(float(rows[-1]["t"]))
            fetched, note = _fetch_range(coin, tf, int(start * 1000), manage_ms)
            if note:
                notes.append(note)
            merged = merge_candles(rows, fetched)
            if merged:
                _save_json(path, merged)
            print(f"cache {coin} {tf} {len(rows)} -> {len(merged)}", flush=True)
    for coin in DEFAULT_HUNT_COINS:
        path = _coin_file(cache, coin, "1m")
        rows = _load_json(path)
        have = last_close(rows, 60)
        if have + 1e-6 >= MANAGE_UNTIL.timestamp() and rows:
            first = bar_open_sec(float(rows[0]["t"]))
            if first <= WARMUP.timestamp() + 60:
                continue
        start_ms = int(WARMUP.timestamp() * 1000)
        if rows:
            start_ms = int(bar_open_sec(float(rows[0]["t"])) * 1000)
            if have > 0 and have < MANAGE_UNTIL.timestamp():
                # Keep the warmup already on disk; only ask for the missing tail
                # when the file already starts early enough.
                if bar_open_sec(float(rows[0]["t"])) <= WARMUP.timestamp() + 3600:
                    start_ms = int(bar_open_sec(float(rows[-1]["t"])) * 1000)
        fetched, note = _fetch_range(coin, "1m", start_ms, manage_ms)
        if note:
            notes.append(note)
        merged = merge_candles(rows, fetched)
        if merged:
            _save_json(path, merged)
        print(f"cache {coin} 1m {len(rows)} -> {len(merged)} {note}", flush=True)
    return {"notes": notes}


def _load_tf(cache: Path, coins: tuple[str, ...], tf: str) -> dict[str, list[dict]]:
    book: dict[str, list[dict]] = {}
    for coin in coins:
        rows = merge_candles(_load_json(_coin_file(cache, coin, tf)), [])
        if rows:
            book[coin] = rows
    return book


def _management(rows: list[dict], tf_sec: int) -> list[tuple[float, float, float, float]]:
    return [
        (_close_ts(bar, tf_sec), float(bar["h"]), float(bar["l"]), float(bar["c"]))
        for bar in rows
    ]


def _hourly_count(hourly: list[dict], now: float) -> int:
    lo, hi = 0, len(hourly)
    while lo < hi:
        mid = (lo + hi) // 2
        if _close_ts(hourly[mid], 3600) <= now + 1e-9:
            lo = mid + 1
        else:
            hi = mid
    return lo


def extract_confirm(
    hourly: dict[str, list[dict]],
    confirm: dict[str, list[dict]],
    tf_sec: int,
    *,
    since: float,
    end: float,
) -> dict[tuple[str, str, str], list[Signal]]:
    """Sweep and touch on the confirm bar. Levels, macro, and ATR stay on 1h.

    Same rules as ``extract_signals``. A 15m bar does not use a 1h bar that
    has not closed yet.
    """
    out: dict[tuple[str, str, str], list[Signal]] = {
        key: [] for key in ((e, lv, m) for e in ENTRIES for lv in LEVELS for m in MACROS)
    }
    for coin, bars in confirm.items():
        series = hourly.get(coin)
        if not series or len(bars) < 3:
            continue
        cache: dict[int, tuple] = {}
        for idx in range(2, len(bars)):
            now = _close_ts(bars[idx], tf_sec)
            if now < since:
                continue
            if now >= end:
                break
            if not session_open(now, SESSION):
                continue
            closed = closed_candles(bars[max(0, idx - 3) : idx + 1], tf_sec, now)
            if len(closed) < 2:
                continue
            n_hour = _hourly_count(series, now)
            if n_hour < 21:
                continue
            packed = cache.get(n_hour)
            if packed is None:
                window = series[max(0, n_hour - 21) : n_hour]
                atr = atr14_of(window)
                if atr is None or atr <= 0:
                    cache[n_hour] = (None, {}, {})
                else:
                    both = read_swing_macro(series, now, _MACRO_BOTH)
                    four = read_swing_macro(series, now, _MACRO_4H)
                    levels = {
                        source: build_levels(series, now, _LEVEL_PARAMS[source]) for source in LEVELS
                    }
                    cache[n_hour] = (float(atr), {"4h+1d": both, "4h": four, "none": None}, levels)
                packed = cache[n_hour]
            atr, macros, levels_by = packed
            if atr is None:
                continue
            last = closed[-1]
            for source in LEVELS:
                levels = levels_by.get(source) or []
                if not levels:
                    continue
                for macro_name, snap in macros.items():
                    use = levels
                    allowed = {"long", "short"}
                    if snap is not None:
                        state = snap.macro
                        if state == "unknown":
                            continue
                        if state == "up":
                            allowed = {"long"}
                        elif state == "down":
                            allowed = {"short"}
                        elif state == "range":
                            use = range_edges(levels, series, now)
                    if not use:
                        continue
                    opposites = tuple((level.kind, float(level.price)) for level in use)
                    for level in use:
                        side = "long" if level.kind == "support" else "short"
                        if side not in allowed:
                            continue
                        if touch_reclaim(last, level.price, side):
                            out[("touch", source, macro_name)].append(
                                Signal(now, coin, side, float(level.price), atr, float(level.score), opposites)
                            )
                        if find_sweep(closed, level.price, side, MIN_SWEEP_BPS) is not None:
                            out[("sweep", source, macro_name)].append(
                                Signal(now, coin, side, float(level.price), atr, float(level.score), opposites)
                            )
    return out


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), TZ).strftime("%Y-%m-%d")


def _day_rows(trades: list[Trade]) -> list[dict]:
    buckets: dict[str, list[Trade]] = {day: [] for day in DAYS}
    other: list[Trade] = []
    for trade in trades:
        day = _day(trade.opened)
        if day in buckets:
            buckets[day].append(trade)
        else:
            other.append(trade)
    rows = []
    for day in DAYS:
        group = buckets[day]
        wins = [trade for trade in group if trade.net > 0]
        rows.append(
            {
                "day": day,
                "trades": len(group),
                "win_rate": (len(wins) / len(group)) if group else 0.0,
                "expectancy_r": _expectancy(group),
                "net": sum(trade.net for trade in group),
            }
        )
    if other:
        wins = [trade for trade in other if trade.net > 0]
        rows.append(
            {
                "day": "other",
                "trades": len(other),
                "win_rate": (len(wins) / len(other)) if other else 0.0,
                "expectancy_r": _expectancy(other),
                "net": sum(trade.net for trade in other),
            }
        )
    return rows


def _exits(trades: list[Trade]) -> dict[str, int]:
    out: dict[str, int] = {}
    for trade in trades:
        out[trade.reason] = out.get(trade.reason, 0) + 1
    return out


def run_swing(
    hourly: dict[str, list[dict]],
    m15: dict[str, list[dict]],
) -> dict:
    """Session entries for every locked cell. Rank by expectancy, floor of 5 trades."""
    since = week_start()
    end = week_end()
    cells = cells_for_week()
    pen = penalty(len(cells))
    print("extract 1h", flush=True)
    sig_1h = extract_signals(hourly, since=since, end=end)
    print("extract 15m", flush=True)
    sig_15 = extract_confirm(hourly, m15, 900, since=since, end=end)
    books = {"1h": _mgmt_map(hourly, 3600), "15m": _mgmt_map(m15, 900)}
    pools = {"1h": sig_1h, "15m": sig_15}
    coin_books = {"all": set(COINS), "crypto": set(CRYPTO)}
    rows = []
    n_sig = sum(len(v) for v in sig_1h.values()) + sum(len(v) for v in sig_15.values())
    print(f"signals {n_sig} cells {len(cells)}", flush=True)
    for i, cell in enumerate(cells):
        pool = [
            sig
            for sig in pools[cell.confirm][(cell.entry, cell.level, cell.macro)]
            if sig.coin in coin_books[cell.book]
        ]
        trades, _equity, max_dd = simulate(
            pool,
            books[cell.confirm],
            stop_k=cell.stop_k,
            target=cell.target,
            session=cell.session,
        )
        in_window = [trade for trade in trades if since <= trade.opened < end]
        equity1 = EQUITY0 + sum(trade.net for trade in in_window)
        stats = _stats(in_window, EQUITY0, equity1, max_dd)
        per = {}
        for coin in COINS:
            coin_trades = [trade for trade in in_window if trade.coin == coin]
            per[coin] = {
                "trades": len(coin_trades),
                "expectancy_r": _expectancy(coin_trades),
                "net": sum(trade.net for trade in coin_trades),
            }
        rows.append(
            {
                "name": cell.name,
                "confirm": cell.confirm,
                "session": cell.session,
                **stats,
                "exits": _exits(in_window),
                "per_coin": per,
                "per_day": _day_rows(in_window),
                "_trades": in_window,
            }
        )
        if (i + 1) % 400 == 0:
            print(f"simulated {i + 1}/{len(cells)}", flush=True)
    ranked = [row for row in rows if row["trades"] >= WEEK_MIN_TRADES]
    ranked.sort(key=lambda row: (float(row["expectancy_r"]), row["trades"], row["name"]), reverse=True)
    any_trade = [row for row in rows if row["trades"] >= 1]
    any_trade.sort(key=lambda row: (float(row["expectancy_r"]), row["trades"], row["name"]), reverse=True)
    positive = [row for row in rows if row["trades"] >= 1 and row["expectancy_r"] > 0]
    for row in rows:
        row.pop("_trades", None)
    return {
        "since": since,
        "end": end,
        "cells": len(cells),
        "penalty": pen,
        "signals": n_sig,
        "signals_1h": sum(len(v) for v in sig_1h.values()),
        "signals_15m": sum(len(v) for v in sig_15.values()),
        "with_5": len(ranked),
        "positive": len(positive),
        "positive_max_trades": max((row["trades"] for row in positive), default=0),
        "zero": sum(1 for row in rows if row["trades"] == 0),
        "under_floor": sum(1 for row in rows if 0 < row["trades"] < WEEK_MIN_TRADES),
        "top": ranked[:TOP_N],
        "anecdote": any_trade[0] if any_trade else None,
        "rows": [{k: v for k, v in row.items() if k != "_trades"} for row in rows],
    }


def _mgmt_map(data: dict[str, list[dict]], tf_sec: int) -> dict[str, list[tuple[float, float, float, float]]]:
    return {coin: _management(rows, tf_sec) for coin, rows in data.items()}


@dataclass(frozen=True)
class ScalpSignal:
    ts: float
    coin: str
    side: str
    entry: float
    stop: float
    tp: float
    swing_key: str


def _widen_min_stop(
    side: str,
    limit: float,
    stop: float,
    tick: float,
    bars: list[dict],
    now: float,
) -> tuple[float, float | None] | None:
    """15 bp floor. A tighter liquidity stop walks to the next swing, or fails."""
    min_dist = limit * SCALP_MIN_STOP_BPS / 10_000.0
    if abs(limit - stop) + 1e-12 >= min_dist:
        return stop, None
    kind = "low" if side == "long" else "high"
    structural = [item.price for item in confirmed_swings(bars, kind=kind, now=now)]
    buf = stop_buffer(limit, tick, atr14(bars, now))
    moved: float | None = None
    for level in structural:
        if side == "long":
            cand = level - buf
            if level >= limit or limit - cand + 1e-12 < min_dist:
                continue
            if moved is None or cand > moved:
                moved = cand
        else:
            cand = level + buf
            if level <= limit or cand - limit + 1e-12 < min_dist:
                continue
            if moved is None or cand < moved:
                moved = cand
    if moved is not None and tick > 0:
        steps = moved / tick
        moved = math.floor(steps + 1e-9) * tick if side == "long" else math.ceil(steps - 1e-9) * tick
        moved = float(f"{moved:.10g}")
    if (
        moved is None
        or moved <= 0
        or not stop_is_valid(side, limit, moved)
        or collides_with_fill(limit, moved, tick)
    ):
        return None
    floor = max(min_tp_distance(limit, moved), SCALP_TP_R * abs(limit - moved))
    return moved, floor


class _UnknownTrend:
    def label(self) -> str:
        return "15m=unknown 1h=unknown"

    def is_with(self, side: str) -> bool:
        del side
        return False


def scalp_candidate(coin: str, bars: list[dict], now: float) -> ScalpSignal | None:
    """One structural arm, or None.

    The wick has to print at least one tick through the latest confirmed
    swing, and the close has to be back through it. That is the candle
    stand-in for the tape sweep. There is no book, so the limit is the
    swept wick snapped to the tick: the price the live Alo uses when that
    wick does not cross.
    """
    if len(bars) < 20:
        return None
    last = bars[-1]
    try:
        close = float(last["c"])
        high = float(last["h"])
        low = float(last["l"])
    except (TypeError, ValueError):
        return None
    if close <= 0 or high < low:
        return None
    tick = hl_tick(close)
    if tick <= 0:
        return None
    best: ScalpSignal | None = None
    best_depth = -1.0
    for side in ("long", "short"):
        kind = "low" if side == "long" else "high"
        swing = select_swing(confirmed_swings(bars[-SCALP_SWING_BARS:], kind=kind, now=now), close, tick)
        if swing is None:
            continue
        if side == "long":
            through = low <= swing.price - tick + tick * 1e-6
            reclaim = close > swing.price
            extreme = low
        else:
            through = high >= swing.price + tick - tick * 1e-6
            reclaim = close < swing.price
            extreme = high
        if not through or not reclaim or extreme <= 0:
            continue
        depth = abs(swing.price - extreme) / swing.price * 10_000.0
        if depth + 1e-9 < min_sweep_bps_for(coin, SCALP_MIN_SWEEP):
            continue
        struct_bars = bars[-SCALP_STRUCT_BARS:]
        try:
            states = structure_states(struct_bars, now, close)
        except Exception:
            states = []
        if structure_blocks(side, states):
            continue
        if side == "long":
            limit = math.floor(extreme / tick + 1e-9) * tick
            stop_field, stop_kind, tp_kind = "l", "low", "high"
        else:
            limit = math.ceil(extreme / tick - 1e-9) * tick
            stop_field, stop_kind, tp_kind = "h", "high", "low"
        if limit <= 0:
            continue
        further = closed_prices(bars[-SCALP_SWING_BARS:], field=stop_field, now=now)
        stop_swings = confirmed_swings(bars[-SCALP_SWING_BARS:], kind=stop_kind, now=now)
        further.extend(item.price for item in stop_swings)
        stop = place_stop(
            side,
            extreme,
            limit,
            tick,
            tp_r=SCALP_TP_R,
            swing=swing.price,
            local_extreme=local_bar_extreme(bars, side=side, now=now),
            atr=atr14(bars, now),
            further=further,
        )
        if stop is None or not stop_is_valid(side, limit, stop) or collides_with_fill(limit, stop, tick):
            continue
        widened = _widen_min_stop(side, limit, stop, tick, bars[-SCALP_SWING_BARS:], now)
        if widened is None:
            continue
        stop, moved_floor = widened
        tp_swings = confirmed_swings(bars[-SCALP_SWING_BARS:], kind=tp_kind, now=now)
        levels = [item.price for item in tp_swings]
        spent = filter_spent_swing_prices(side, tp_swings, bars[-SCALP_SWING_BARS:], now, close)
        levels = [px for px in levels if px not in spent]

        def _trend(bars=struct_bars, now=now):
            try:
                from hl_bot.strategy.model_b.trend import read_trend

                return read_trend(bars, now)
            except Exception:
                return _UnknownTrend()

        walked = walk_liquidity_tp(
            side,
            limit,
            stop,
            tick,
            levels,
            coin=coin,
            tp_r=SCALP_TP_R,
            min_pool_r=SCALP_MIN_POOL_R,
            max_pool_r=SCALP_MAX_POOL_R,
            far_skip=True,
            moved_floor=moved_floor,
            trend_fn=_trend,
            distance_fn=min_tp_distance,
        )
        if walked.fail:
            continue
        tp = arm_take_profit(side, limit, stop, walked.target, tp_r=SCALP_TP_R)
        if tp is None:
            continue
        sig = ScalpSignal(now, coin, side, float(limit), float(stop), float(tp), swing_id(coin, swing))
        if depth > best_depth:
            best = sig
            best_depth = depth
    return best


def extract_scalp(data: dict[str, list[dict]], *, since: float, end: float) -> list[ScalpSignal]:
    """First successful structural arm per swing. Later minutes of the same swing are not re-armed."""
    found: list[ScalpSignal] = []
    seen: set[str] = set()
    for coin, bars in data.items():
        for idx in range(20, len(bars)):
            now = _close_ts(bars[idx], 60)
            if now < since:
                continue
            if now >= end:
                break
            if not session_open(now, SESSION):
                continue
            window = bars[max(0, idx - SCALP_STRUCT_BARS + 1) : idx + 1]
            sig = scalp_candidate(coin, window, now)
            if sig is None or sig.swing_key in seen:
                continue
            seen.add(sig.swing_key)
            found.append(sig)
        print(f"scalp {coin} signals {sum(1 for s in found if s.coin == coin)}", flush=True)
    found.sort(key=lambda sig: (sig.ts, DEFAULT_HUNT_COINS.index(sig.coin) if sig.coin in DEFAULT_HUNT_COINS else 99))
    return found


def _scalp_pnl(side: str, size: float, entry: float, exit_px: float, reason: str, coin: str) -> float:
    fee = conservative_fees(coin)
    slip = default_stop_slip_bps(coin)[0]
    booked = "tp" if reason == "tp" else "max_hold"
    return exit_net(
        side=side,
        size=size,
        entry=entry,
        exit_px=exit_px,
        reason=booked,
        maker_fee=fee.maker,
        taker_fee=fee.taker,
        slip_bps=slip,
    )


def _scalp_size(coin: str, entry: float, stop: float, equity: float) -> float:
    fee = conservative_fees(coin)
    try:
        size, _dollar = size_from_stop(
            equity,
            entry,
            stop,
            risk_pct=SCALP_RISK,
            leverage=20,
            notional_leverage=20,
            min_stop_bps=SCALP_MIN_STOP_BPS,
            include_fees=True,
            maker_fee=fee.maker,
            taker_fee=fee.taker,
            slip_bps=0.0,
        )
    except ValueError:
        return 0.0
    return cap_size_to_loss(
        size,
        entry,
        stop,
        equity,
        include_fees=True,
        maker_fee=fee.maker,
        taker_fee=fee.taker,
        slip_bps=0.0,
    )


def simulate_scalp(
    signals: list[ScalpSignal],
    bars: dict[str, list[tuple[float, float, float, float]]],
    *,
    equity0: float = EQUITY0,
) -> tuple[list[Trade], float, int]:
    """One position. Working orders cancel at 16:00. An open trade does not.

    Live can hold more than one coin. This book does not, so the trade
    count is a lower bound on a multi-coin account. ``skipped`` is how
    many later signals printed while a ticket was working or open.
    """
    ordered = list(signals)
    index = {coin: [row[0] for row in rows] for coin, rows in bars.items()}
    trades: list[Trade] = []
    eq = float(equity0)
    peak = eq
    max_dd = 0.0
    cursor = 0
    skipped = 0
    n = len(ordered)

    def _mark(unreal: float) -> None:
        nonlocal peak, max_dd
        marked = eq + unreal
        if marked > peak:
            peak = marked
        if peak > 0:
            max_dd = max(max_dd, (peak - marked) / peak)

    while cursor < n:
        ts0 = ordered[cursor].ts
        group = []
        while cursor < n and ordered[cursor].ts == ts0:
            group.append(ordered[cursor])
            cursor += 1
        armed = None
        for sig in group:
            if sig.coin not in bars:
                continue
            size = _scalp_size(sig.coin, sig.entry, sig.stop, eq)
            if size <= 0:
                continue
            armed = (sig, size)
            break
        if armed is None or eq <= 0:
            continue
        skipped += len(group) - 1
        sig, size = armed
        rows = bars[sig.coin]
        starts = index[sig.coin]
        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= sig.ts:
                lo = mid + 1
            else:
                hi = mid
        bar_i = lo
        mode = "working"
        opened = 0.0
        while bar_i < len(rows) and mode != "flat":
            now, high, low, close = rows[bar_i]
            bar_i += 1
            while cursor < n and ordered[cursor].ts <= now:
                if mode != "flat":
                    skipped += 1
                cursor += 1
            if mode == "working":
                if session_flat_due_safe(now):
                    mode = "flat"
                    break
                if sig.side == "long":
                    filled = low <= sig.entry + 1e-9
                    hit_stop = low <= sig.stop + 1e-9
                    hit_tp = high >= sig.tp - 1e-9
                else:
                    filled = high >= sig.entry - 1e-9
                    hit_stop = high >= sig.stop - 1e-9
                    hit_tp = low <= sig.tp + 1e-9
                if hit_tp and not filled:
                    mode = "flat"
                    break
                if not filled:
                    continue
                if hit_stop:
                    _book(trades, sig, sig.stop, size, "stop", now, now, eq_box := [eq])
                    eq = eq_box[0]
                    mode = "flat"
                    _mark(0.0)
                    break
                if hit_tp:
                    _book(trades, sig, sig.tp, size, "tp", now, now, eq_box := [eq])
                    eq = eq_box[0]
                    mode = "flat"
                    _mark(0.0)
                    break
                mode = "open"
                opened = now
                _mark(_scalp_pnl(sig.side, size, sig.entry, close, "max_hold", sig.coin))
                continue
            if sig.side == "long":
                hit_stop = low <= sig.stop + 1e-9
                hit_tp = high >= sig.tp - 1e-9
            else:
                hit_stop = high >= sig.stop - 1e-9
                hit_tp = low <= sig.tp + 1e-9
            if hit_stop:
                _book(trades, sig, sig.stop, size, "stop", opened, now, eq_box := [eq])
                eq = eq_box[0]
                mode = "flat"
            elif hit_tp:
                _book(trades, sig, sig.tp, size, "tp", opened, now, eq_box := [eq])
                eq = eq_box[0]
                mode = "flat"
            if mode == "open":
                _mark(_scalp_pnl(sig.side, size, sig.entry, close, "max_hold", sig.coin))
            else:
                _mark(0.0)
        if mode == "open" and rows:
            now, _high, _low, close = rows[-1]
            _book(trades, sig, close, size, "data_end", opened, now, eq_box := [eq])
            eq = eq_box[0]
            _mark(0.0)
    return trades, max_dd, skipped


def session_flat_due_safe(now: float) -> bool:
    from hl_bot.strategy.model_b.swing import session_flat_due

    return session_flat_due(now, SESSION)


def _book(trades, sig: ScalpSignal, exit_px: float, size: float, reason: str, opened: float, closed: float, eq_box: list) -> None:
    net = _scalp_pnl(sig.side, size, sig.entry, exit_px, reason, sig.coin)
    risk = size * abs(sig.entry - sig.stop)
    trades.append(
        Trade(
            sig.coin,
            sig.side,
            sig.entry,
            float(exit_px),
            sig.stop,
            size,
            net,
            (net / risk) if risk > 0 else 0.0,
            reason,
            opened,
            closed,
        )
    )
    eq_box[0] = eq_box[0] + net


def run_scalp(minutes: dict[str, list[dict]]) -> dict:
    since = week_start()
    end = week_end()
    signals = extract_scalp(minutes, since=since, end=end)
    bars = _mgmt_map(minutes, 60)
    trades, max_dd, skipped = simulate_scalp(signals, bars)
    in_window = [trade for trade in trades if since <= trade.opened < end]
    equity1 = EQUITY0 + sum(trade.net for trade in in_window)
    stats = _stats(in_window, EQUITY0, equity1, max_dd)
    per = {}
    for coin in DEFAULT_HUNT_COINS:
        coin_sig = [sig for sig in signals if sig.coin == coin]
        if coin not in bars:
            per[coin] = {"signals": 0, "trades": 0, "expectancy_r": None, "net": 0.0, "missing": True}
            continue
        coin_trades, _dd, _skip = simulate_scalp(coin_sig, {coin: bars[coin]})
        kept = [trade for trade in coin_trades if since <= trade.opened < end]
        per[coin] = {
            "signals": len(coin_sig),
            "trades": len(kept),
            "expectancy_r": _expectancy(kept),
            "net": sum(trade.net for trade in kept),
            "missing": False,
        }
    return {
        "signals": len(signals),
        "skipped": skipped,
        "missing": [coin for coin in DEFAULT_HUNT_COINS if coin not in minutes],
        "loaded": [coin for coin in DEFAULT_HUNT_COINS if coin in minutes],
        **stats,
        "exits": _exits(in_window),
        "per_day": _day_rows(in_window),
        "signal_days": _signal_days(signals),
        "per_coin": per,
    }


def _signal_days(signals: list[ScalpSignal]) -> list[dict]:
    buckets = {day: 0 for day in DAYS}
    for sig in signals:
        day = _day(sig.ts)
        if day in buckets:
            buckets[day] += 1
    return [{"day": day, "signals": buckets[day]} for day in DAYS]


def _family(name: str) -> str:
    """Cell identity ignoring target and the 16:00 rule. Those share entries."""
    parts = str(name).split("/")
    if len(parts) < 8:
        return str(name)
    return "/".join(parts[:5] + parts[7:])


def _fmt_e(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def _fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def _exit_line(exits: dict) -> str:
    order = ("stop", "tp", "SESSION_CLOSE", "max_hold", "data_end")
    names = [name for name in order if exits.get(name)]
    names.extend(name for name in sorted(exits) if name not in order)
    if not names:
        return "none"
    return ", ".join(f"{name} {exits[name]}" for name in names)


def _coin_line(per: dict) -> str:
    parts = []
    for coin, row in per.items():
        if not row.get("trades"):
            continue
        parts.append(f"{coin} {row['trades']}/{_fmt_e(row.get('expectancy_r'))}R")
    return ", ".join(parts) if parts else "none"


def _day_table(rows: list[dict]) -> list[str]:
    lines = ["| Day | Trades | Win% | Expectancy | Net $ |", "|---|---:|---:|---:|---:|"]
    for row in rows:
        exp = _fmt_e(row.get("expectancy_r"))
        lines.append(
            f"| {row['day']} | {row['trades']} | {_fmt_pct(100.0 * float(row['win_rate']))} | {exp} | {row['net']:.2f} |"
        )
    return lines


def _setup_line(row: dict) -> str:
    trades = int(row["trades"])
    win = float(row["win_rate"])
    avg_w = "—" if trades and win <= 0 else _fmt_e(row["avg_win_r"])
    avg_l = "—" if trades and win >= 1 else _fmt_e(row["avg_loss_r"])
    return (
        f"{trades} trades, win {_fmt_pct(100.0 * win)}, "
        f"avg winner {avg_w} R, avg loser {avg_l} R, "
        f"expectancy {_fmt_e(row['expectancy_r'])} R, net {row['net_pct']:.2f}%, "
        f"max DD {row['max_dd_pct']:.2f}%"
    )


def render(result: dict) -> str:
    swing = result["swing"]
    scalp = result["scalp"]
    cells = int(swing["cells"])
    pen = float(swing["penalty"])
    lines = [
        "# Last week, US cash session",
        "",
        "Paper research only. One week is not a verdict. This does not change Monday's paper book, and it does not place an order.",
        "",
        "## Window",
        "",
        "Monday 5 Oct 2026 09:30 through Friday 9 Oct 2026 16:00, America/Toronto. Entries only while that cash session is open (start inclusive, 16:00 exclusive, weekdays). Overnight and +1R swing variants may exit after 16:00. The 3-day max hold still applies. The candle file ends Sunday night UTC (2026-10-12 01:00), so a Friday fill does not get its full three days. A position still open on the last bar is `data_end`.",
        "",
        "## Sample",
        "",
        f"Five sessions. {cells} swing cells were tried. That is the whole locked `swing_search` grid (2 entries × 3 level sources × 2 confirm timeframes × 3 macro settings × 3 stop multiples × 4 targets × 3 session rules × 2 coin books = {cells}). The 90-day search left 15m unrun because those bars do not cover 90 days. This week they do, so 15m is included. Ranking the top 5 by expectancy after seeing all {cells} cells is a multiple comparison.",
        "",
        f"The 90-day penalty formula on {cells} cells is {pen:.3f} R. It is not applied as a pass mark. This week has no holdout, and it cannot clear the 30-trade rule. A cell with fewer than {WEEK_MIN_TRADES} trades is not in the top 5. The single best cell with no trade floor is an anecdote, written separately.",
        "",
        f"Cells with no trades: {swing['zero']}. Cells with 1 to {WEEK_MIN_TRADES - 1} trades: {swing['under_floor']}. Cells with at least {WEEK_MIN_TRADES} trades: {swing['with_5']}. Cells with expectancy above zero (any trade count): {swing['positive']}, and the largest of those has {swing['positive_max_trades']} trades.",
        "",
        "## Costs",
        "",
        "Fees are on. Funding is not. Swing slip is the booked 25 bp on main coins and 30 bp on xyz, inside the size and on a stop, a 16:00 flatten, a 3-day max hold, and a data-end flatten. A target is a maker fill with no slip.",
        "",
        "Scalp size is the live shadow size: 2% of a $10,000 research book, 20x, fees inside the 2% cap, and the 5 bp / 20 bp stop-slip allowance not taken out of the posted size. Stops, max-hold marks, and data-end flattens then pay that 5 bp (main) or 20 bp (xyz) plus the taker fee. Targets pay the maker fee and no slip. The $10,000 book is not the live account.",
        "",
        "A second book at slip measured from live fills was not run. `reports/model_b_trade_notes.json` is not in this repository, and there is no per-trade fill file to measure. The only stop-market print written down is xyz:SKHX on 8 Oct 2026: trigger 1198.8, fill 1200.6, 1.8 points, 15.0 bp of the 1196.7 entry (`stop_slip.py`). SKHX is not in this week's coin list. The xyz floor used above is already 20 bp, which sits above that print. The same week, an xyz:XYZ100 take-profit triggered at 31165 and filled at 31162 (`account.py`). That is three points toward the target, not a stop-slip sample. It is not applied to the other coins.",
        "",
        "## Candles",
        "",
        "1h and 15m come from the local cache, extended from Hyperliquid `candleSnapshot` when Friday's 16:00 ET close was missing. Both cover Monday's open through Friday's close. 1m was not in the cache and was fetched the same way. The public 1m snapshot only keeps about 5,000 bars, which from this fetch reaches back to Thursday 8 Oct and no further. Requests for Monday through Wednesday returned no bars. Nothing fetched is committed.",
        "",
        result.get("coverage", ""),
        "",
        "## Swing",
        "",
        "Flow is off. There is no aggressor tape, so absorption, print count, and delta are not in any cell. Confirm is the bar that must sweep or touch. 1h cells are managed on 1h bars. 15m cells are managed on 15m bars. Levels, the 4h/daily macro, and the ATR used in the stop are 1h in both, which is how `plan_trade` already reads them. One position. A working order is not replaced, and it is cancelled at 16:00, so a signal whose next bar closes at 16:00 does not fill.",
        "",
        f"Signals: {swing['signals_1h']} on 1h, {swing['signals_15m']} on 15m.",
        "",
    ]
    top = swing["top"]
    if len(top) < TOP_N:
        lines.append(
            f"Fewer than {TOP_N} cells had {WEEK_MIN_TRADES} trades. The table is every cell that cleared the floor ({len(top)})."
        )
        lines.append("")
    lines.append(f"Top {min(TOP_N, len(top))} by expectancy, among cells with at least {WEEK_MIN_TRADES} trades. This is not a pick for Monday.")
    lines.append("")
    if top and all(int(row["trades"]) == WEEK_MIN_TRADES for row in top):
        lines.append(
            f"Each of these cells has {WEEK_MIN_TRADES} trades, the ranking floor. "
            f"That is what the best of {cells} cells looks like on five sessions."
        )
        lines.append("")
    families: dict[str, list[int]] = {}
    for i, row in enumerate(top, start=1):
        families.setdefault(_family(row["name"]), []).append(i)
    for fam, idxs in families.items():
        if len(idxs) < 2:
            continue
        listed = ", ".join(str(i) for i in idxs)
        lines.append(
            f"Rows {listed} share entries (`{fam}`). Only the target or the 16:00 rule changes. "
            "They are one sample counted more than once, not separate setups."
        )
        lines.append("")
    if not top:
        lines.append(f"No cell produced {WEEK_MIN_TRADES} trades.")
        lines.append("")
    for i, row in enumerate(top, start=1):
        lines.append(f"### {i}. `{row['name']}`")
        lines.append("")
        lines.append(_setup_line(row))
        lines.append("")
        lines.append(f"Exits: {_exit_line(row.get('exits') or {})}. Coins: {_coin_line(row.get('per_coin') or {})}.")
        lines.append("")
        lines.extend(_day_table(row.get("per_day") or []))
        lines.append("")
    anecdote = swing.get("anecdote")
    lines.append("## Unrestricted best cell")
    lines.append("")
    if anecdote is None:
        lines.append("No cell had a trade.")
    else:
        lines.append(
            f"`{anecdote['name']}` has the highest expectancy with no trade floor: {_setup_line(anecdote)}. "
            f"That is one cell out of {cells}. It is an anecdote, not a result."
        )
    lines.extend(
        [
            "",
            "## Scalp, structural, tape off",
            "",
            "Reference only, against the live rules, not a reconstruction of the live tickets. Live hunts the same coins after 16:00. This replay does not take those entries. Fills have to print on a later bar that is still inside the cash session. An open position is not flattened at 16:00. It runs to the stop, the target, or the last 1m bar.",
            "",
            "Monday, Tuesday, and Wednesday have no 1m candles, so a zero on those days is a missing file, not a day without a setup. Thursday starts when each coin's file starts (late morning for BTC). Friday is covered.",
            "",
            "What is on, from the current live defaults: latest confirmed 1m swing at least 3 ticks from the close; wick at least 1 tick and 0.3 bp through that swing with the close back through it; 15m and 1h structure filter (a long is blocked when either is bearish, a short when either is bullish); stop past the wick by `max(3 ticks, 2 bp, 0.5×ATR14)`, then the 15 bp minimum-stop walk; take-profit is the next untaken confirmed swing that clears the fee and 1R, walked out to 1.5R when the nearest is closer, skipped when that walk is countertrend and too far, otherwise the 1.5R fallback inside [1, 2]. One thesis per swing. One open position for the headline book, majors first when two coins signal on the same minute.",
            "",
            "What is off, because the candles have no aggressor prints: absorption at 1.3, the 30-print minimum, window delta, the last 15 seconds, and counter-flow. No bid or ask, so the post-only join cannot be proven and the limit is the swept wick. No untaken-pool tape, so a pool that was not also a 1m swing is invisible. Do not read this as the live blotter.",
            "",
            _setup_line(scalp) if scalp.get("trades") else "No structural scalp trade filled inside the window.",
            "",
            f"Signals {scalp.get('signals', 0)}. Signals skipped because a ticket was already working or open: {scalp.get('skipped', 0)}. Exits: {_exit_line(scalp.get('exits') or {})}.",
            "",
            "Coins with no 1m candles: " + (", ".join(scalp.get("missing") or []) or "none") + ".",
            "",
            "Per day, one-position book (the day is the fill, in Toronto):",
            "",
        ]
    )
    lines.extend(_day_table(scalp.get("per_day") or []))
    lines.append("")
    lines.append("Structural signals per day, before the one-position cut:")
    lines.append("")
    lines.append("| Day | Signals |")
    lines.append("|---|---:|")
    for row in scalp.get("signal_days") or []:
        lines.append(f"| {row['day']} | {row['signals']} |")
    lines.append("")
    lines.append(
        "Per coin, each coin given its own $10,000 book. These nets do not add. Adding them would pretend every coin had a separate account."
    )
    lines.append("")
    lines.append("| Coin | Signals | Trades | Expectancy | Net $ |")
    lines.append("|---|---:|---:|---:|---:|")
    for coin, row in (scalp.get("per_coin") or {}).items():
        if row.get("missing"):
            lines.append(f"| {coin} | 0 | 0 | — | — |")
            continue
        lines.append(
            f"| {coin} | {row['signals']} | {row['trades']} | {_fmt_e(row.get('expectancy_r'))} | {row['net']:.2f} |"
        )
    lines.extend(
        [
            "",
            "### Live sample already written down",
            "",
            "PR #17 records 14 closed mainnet round trips from 6 Oct 2026 23:58 ET through 8 Oct 2026 15:31 ET. On that path, today's stops were −2.31R / −$11.26 on a $244 book. The same fills are not stored in this repo, so this replay is not joined to those 14 tickets. That window also includes after-hours entries this study does not take. `reports/model_b_trade_notes.json` is still absent.",
            "",
            "## What this does not say",
            "",
            f"One week, {cells} swing cells, tape off on both books. No setup is promoted. No default moves.",
            "",
        ]
    )
    return "\n".join(lines)


def _coverage_line(hourly: dict, m15: dict, minutes: dict) -> str:
    def _last(book: dict, sec: int) -> str:
        if not book:
            return "missing"
        last = min(last_close(rows, sec) for rows in book.values())
        stamp = datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        flag = "covers Friday 16:00 ET" if last + 1e-6 >= week_end() else "DOES NOT cover Friday 16:00 ET"
        return f"{stamp} ({flag})"

    def _open_span(book: dict) -> str:
        if not book:
            return "missing"
        ranked = sorted(
            ((coin, bar_open_sec(float(rows[0]["t"]))) for coin, rows in book.items() if rows),
            key=lambda item: item[1],
        )
        if not ranked:
            return "missing"
        early_coin, early = ranked[0]
        late_coin, late = ranked[-1]
        early_s = datetime.fromtimestamp(early, TZ).strftime("%Y-%m-%d %H:%M ET")
        late_s = datetime.fromtimestamp(late, TZ).strftime("%Y-%m-%d %H:%M ET")
        note = ""
        if early > week_start() + 60:
            note = " This is after Monday's open, so earlier cash sessions are absent."
        return f"{early_coin} {early_s} through {late_coin} {late_s}.{note}"

    return (
        f"1h last close, earliest coin: {_last(hourly, 3600)}. "
        f"15m last close, earliest coin: {_last(m15, 900)}. "
        f"1m coins loaded: {len(minutes)} of {len(DEFAULT_HUNT_COINS)}. "
        f"1m first opens: {_open_span(minutes)} "
        f"1m last close, earliest loaded coin: {_last(minutes, 60)}."
    )


def _public(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: v for k, v in row.items() if k != "_trades"}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="One-week US-session replay. Research only.")
    parser.add_argument("--cache", default="/tmp/hl_swing_cache")
    parser.add_argument("--out", default="docs/pnl/last_week_session.md")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    if not args.no_fetch:
        info = ensure_candles(cache)
    else:
        info = {"notes": []}
    hourly = _load_tf(cache, COINS, "1h")
    m15 = _load_tf(cache, COINS, "15m")
    minutes = _load_tf(cache, DEFAULT_HUNT_COINS, "1m")
    coverage = _coverage_line(hourly, m15, minutes)
    if info["notes"]:
        coverage += " Fetch notes: " + "; ".join(info["notes"]) + "."
    print(coverage, flush=True)
    swing = run_swing(hourly, m15)
    scalp = run_scalp(minutes)
    result = {"swing": swing, "scalp": scalp, "coverage": coverage}
    text = render(result)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    payload = {
        "coverage": coverage,
        "fetch_notes": info["notes"],
        "swing": {
            "cells": swing["cells"],
            "penalty": swing["penalty"],
            "signals_1h": swing["signals_1h"],
            "signals_15m": swing["signals_15m"],
            "with_5": swing["with_5"],
            "positive": swing["positive"],
            "positive_max_trades": swing["positive_max_trades"],
            "zero": swing["zero"],
            "under_floor": swing["under_floor"],
            "top": [_public(row) for row in swing["top"]],
            "anecdote": _public(swing["anecdote"]),
        },
        "scalp": scalp,
    }
    dest.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str))
    print(text[:1500])
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

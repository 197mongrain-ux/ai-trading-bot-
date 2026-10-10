"""Swing Model B. Same sweep/reclaim idea, smaller size, higher-timeframe levels.

Off unless ``MODEL_B_STYLE=swing``. The scalp engine never calls this.

Levels are 4h and daily swing highs and lows, plus the previous day's
and previous week's high and low. Only the top-scored levels are traded.
Macro is ADX(14): with the trend, or both sides at the edges of a range.
The entry is a sweep and reclaim of that level on the confirm timeframe,
then the existing tape gates, then a post-only Alo at the level. The stop
is beyond the sweep wick by an ATR buffer. The target is the opposite
level when that distance is between 1R and 5R.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.strategy.model_b.alo import alo_limit
from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.risk import (
    cap_size_to_loss,
    loss_at_stop,
    size_from_stop,
)
from hl_bot.strategy.model_b.swings import bar_open_sec
from hl_bot.strategy.model_b.tape import (
    analyze_tape,
    flat_eps_coins,
    missing_side,
    window_prints,
)
from hl_bot.strategy.model_b.trend import adx_series, adx_state, combine_macro
from hl_bot.strategy.model_b.types import AloIntent, Decision, TradePrint
from hl_bot.strategy.model_b.universe import canon_coin

TF_SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
SWING_COINS = ("BTC", "ETH", "SOL", "xyz:GOLD", "xyz:SP500", "xyz:XYZ100")

NO_LEVEL = "NO_LEVEL"
NO_ATR = "NO_ATR"
TP_UNDER_MIN = "TP_UNDER_MIN"
TP_OVER_MAX = "TP_OVER_MAX"
TP_OUT_OF_RANGE = "TP_OUT_OF_RANGE"
NO_TARGET = "NO_TARGET"
SIZE_ZERO = "SIZE_ZERO"


@dataclass(frozen=True)
class SwingParams:
    confirm_tf: str = "15m"
    macro_tfs: tuple[str, ...] = ("4h", "1d")
    macro_mode: str = "both"
    adx_min: float = 20.0
    atr_tf: str = "1h"
    atr_frac: float = 0.5
    min_r: float = 1.0
    max_r: float = 5.0
    top_n: int = 2
    min_score: float = 3.0
    cluster_bps: float = 20.0
    pivot: int = 2
    hold_days: float = 3.0
    runner: bool = False
    runner_frac: float = 0.5
    slip_main_bps: float = 25.0
    slip_xyz_bps: float = 30.0
    min_sweep_bps: float = 5.0
    flow: str = "tape"
    fill_hours: float = 24.0
    # Study filters. Defaults leave the paper book unchanged.
    level_set: str = "all"  # all | 4h | 1d | session
    min_touches: int = 0
    min_room_pct: float = 0.0
    session: str = "all"  # all | us (13:30–20:00 UTC)
    side_only: str = "both"  # both | long | short
    partial_r: float = 0.0  # replay only: scale half off at this R, stop to entry
    max_sweep_bps: float = 0.0  # 0 = no cap on sweep depth
    min_adx_gate: float = 0.0  # 0 = no extra ADX floor on the higher timeframe
    trend_only: bool = False  # drop range; trade only a directional macro
    # Wide reclaim bar. off leaves the paper book unchanged.
    # skip drops the setup. retest rests the limit only after price leaves
    # the level and trades back to it. A bar is wide when its range exceeds
    # max_reclaim_bps or max_reclaim_atr × ATR(14), whichever cap is set.
    reclaim_mode: str = "off"  # off | skip | retest
    max_reclaim_bps: float = 0.0
    max_reclaim_atr: float = 0.0
    # Fail-fast scratch. 0 leaves it off. Time scratch exits at the market
    # when MFE has not reached scratch_mfe_r within scratch_minutes.
    # MAE scratch exits when adverse excursion reaches scratch_mae_r before
    # +0.5R favorable. Both pay taker fees and adverse slip.
    scratch_mfe_r: float = 0.0
    scratch_minutes: float = 0.0
    scratch_mae_r: float = 0.0
    # sweep is the paper book. trapped is the footprint entry and stays off.
    entry: str = "sweep"
    trap_bar: str = "1m"
    trap_imbalance: float = 3.0
    trap_stacked: int = 3
    trap_zone_bps: float = 20.0
    trap_near_frac: float = 0.25
    trap_min_volume: float = 0.0
    trap_stop_bps: float = 5.0
    trap_entry: str = "failure_close"  # failure_close | lift
    trap_lift_bps: float = 5.0
    trap_cvd_bars: int = 3
    trap_delta_mode: str = "either"  # either | delta | cvd | both
    # Stop and slip for the trapped entry only. Defaults keep the trap-bar
    # extreme plus trap_stop_bps, and the coin 25/30 bp allowance.
    # zone anchors beyond the level instead of the wick. A min distance of
    # max(bps, k×ATR(14) of the footprint bars) pushes a tighter stop out.
    # proportional slip reserves min(the flat allowance, the stop's own bps).
    trap_stop_anchor: str = "trap"  # trap | zone
    trap_min_stop_bps: float = 0.0
    trap_min_stop_atr: float = 0.0
    trap_slip_bps: float = 0.0  # 0 = the coin allowance (25 main, 30 xyz)
    trap_slip_mode: str = "flat"  # flat | proportional

    @classmethod
    def from_settings(cls, settings) -> SwingParams:
        raw_tfs = str(getattr(settings, "model_b_swing_macro", "4h,1d") or "4h,1d")
        tfs = tuple(part.strip() for part in raw_tfs.split(",") if part.strip())
        return cls(
            confirm_tf=str(getattr(settings, "model_b_swing_confirm", "15m") or "15m"),
            macro_tfs=tfs or ("4h", "1d"),
            macro_mode=str(getattr(settings, "model_b_swing_macro_mode", "both") or "both"),
            adx_min=float(getattr(settings, "model_b_macro_adx_min", 20.0) or 20.0),
            atr_tf=str(getattr(settings, "model_b_swing_atr_tf", "1h") or "1h"),
            atr_frac=float(getattr(settings, "model_b_swing_atr_frac", 0.5) or 0.5),
            min_r=float(getattr(settings, "model_b_swing_min_r", 1.0) or 1.0),
            max_r=float(getattr(settings, "model_b_swing_max_r", 5.0) or 5.0),
            top_n=int(getattr(settings, "model_b_swing_top_n", 2) or 2),
            min_score=float(getattr(settings, "model_b_swing_min_score", 3.0) or 3.0),
            cluster_bps=float(getattr(settings, "model_b_swing_cluster_bps", 20.0) or 20.0),
            pivot=int(getattr(settings, "model_b_swing_pivot", 2) or 2),
            hold_days=float(getattr(settings, "model_b_swing_hold_days", 3.0) or 3.0),
            runner=bool(getattr(settings, "model_b_swing_runner", False)),
            runner_frac=float(getattr(settings, "model_b_swing_runner_frac", 0.5) or 0.5),
            slip_main_bps=float(getattr(settings, "model_b_swing_slip_main_bps", 25.0) or 25.0),
            slip_xyz_bps=float(getattr(settings, "model_b_swing_slip_xyz_bps", 30.0) or 30.0),
            min_sweep_bps=float(getattr(settings, "model_b_swing_min_sweep_bps", 5.0) or 5.0),
            flow=str(getattr(settings, "model_b_swing_flow", "tape") or "tape"),
            fill_hours=float(getattr(settings, "model_b_swing_fill_hours", 24.0) or 24.0),
            reclaim_mode=str(getattr(settings, "model_b_swing_reclaim_mode", "off") or "off"),
            max_reclaim_bps=float(getattr(settings, "model_b_swing_reclaim_bps", 0.0) or 0.0),
            max_reclaim_atr=float(getattr(settings, "model_b_swing_reclaim_atr", 0.0) or 0.0),
            scratch_mfe_r=float(getattr(settings, "model_b_swing_scratch_mfe_r", 0.0) or 0.0),
            scratch_minutes=float(getattr(settings, "model_b_swing_scratch_minutes", 0.0) or 0.0),
            scratch_mae_r=float(getattr(settings, "model_b_swing_scratch_mae_r", 0.0) or 0.0),
            entry=str(getattr(settings, "model_b_swing_entry", "sweep") or "sweep"),
            trap_bar=str(getattr(settings, "model_b_swing_trap_bar", "1m") or "1m"),
            trap_imbalance=float(getattr(settings, "model_b_swing_trap_imbalance", 3.0) or 3.0),
            trap_stacked=int(getattr(settings, "model_b_swing_trap_stacked", 3) or 3),
            trap_zone_bps=float(getattr(settings, "model_b_swing_trap_zone_bps", 20.0) or 20.0),
            trap_near_frac=float(getattr(settings, "model_b_swing_trap_near_frac", 0.25) or 0.25),
            trap_min_volume=float(getattr(settings, "model_b_swing_trap_min_volume", 0.0) or 0.0),
            trap_stop_bps=float(getattr(settings, "model_b_swing_trap_stop_bps", 5.0) or 5.0),
            trap_entry=str(getattr(settings, "model_b_swing_trap_entry", "failure_close") or "failure_close"),
            trap_lift_bps=float(getattr(settings, "model_b_swing_trap_lift_bps", 5.0) or 5.0),
            trap_cvd_bars=int(getattr(settings, "model_b_swing_trap_cvd_bars", 3) or 3),
            trap_delta_mode=str(getattr(settings, "model_b_swing_trap_delta_mode", "either") or "either"),
            trap_stop_anchor=str(getattr(settings, "model_b_swing_trap_stop_anchor", "trap") or "trap"),
            trap_min_stop_bps=float(getattr(settings, "model_b_swing_trap_min_stop_bps", 0.0) or 0.0),
            trap_min_stop_atr=float(getattr(settings, "model_b_swing_trap_min_stop_atr", 0.0) or 0.0),
            trap_slip_bps=float(getattr(settings, "model_b_swing_trap_slip_bps", 0.0) or 0.0),
            trap_slip_mode=str(getattr(settings, "model_b_swing_trap_slip_mode", "flat") or "flat"),
        )


@dataclass(frozen=True)
class Level:
    price: float
    kind: str  # support | resistance
    score: float
    touches: int
    sources: tuple[str, ...]
    ts: float


@dataclass(frozen=True)
class Plan:
    side: str
    level: float
    level_ts: float
    entry: float
    stop: float
    take_profit: float
    runner_px: float | None
    size: float
    r_multiple: float
    score: float
    sweep: float
    macro: str
    sources: tuple[str, ...]
    touches: int = 0
    sweep_bps: float = 0.0
    reclaim_bps: float = 0.0
    retest: bool = False
    # Dollars reserved at the arm (stop + slip + fees). 0 on plans built
    # before this field. Tape R is net $ / this budget, not the raw stop.
    budget: float = 0.0
    slip_bps: float = 0.0


def _source_ok(source: str, level_set: str) -> bool:
    """Keep a raw swing or session print for this level family.

    ``4h`` / ``1d`` / ``session`` drop the other families before clustering,
    so the top-ranked levels are chosen from that family alone.
    """
    if level_set in ("", "all"):
        return True
    if level_set == "4h":
        return source.startswith("4h_")
    if level_set == "1d":
        return source.startswith("1d_")
    if level_set == "session":
        return source in {"PDH", "PDL", "PWH", "PWL"}
    if level_set == "swing_htf":
        return source.startswith("1d_") or source in {"PDH", "PDL", "PWH", "PWL"}
    return True


def _labeled_adx(label: str, tf: str) -> float | None:
    """Read ``4h=up(adx=23.0)`` out of a macro label. Missing means no gate."""
    import re

    match = re.search(rf"\b{re.escape(tf)}=(?:up|down|range|unknown)\(adx=([0-9.]+)\)", label or "")
    if not match:
        return None
    return float(match.group(1))


def session_open(now: float, session: str) -> bool:
    """US cash session is 13:30–20:00 UTC. Any other value keeps every hour."""
    if session != "us":
        return True
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(float(now), timezone.utc)
    minutes = dt.hour * 60 + dt.minute
    return 13 * 60 + 30 <= minutes < 20 * 60


def slip_bps_for(coin: str, params: SwingParams) -> float:
    name = canon_coin(coin)
    if ":" in name and name.split(":", 1)[0] == "xyz":
        return float(params.slip_xyz_bps)
    return float(params.slip_main_bps)


def trap_flat_allowance(coin: str, params: SwingParams) -> float:
    """Flat slip bps reserved for a trapped trade.

    ``trap_slip_bps`` of 0 keeps the coin allowance (25 main, 30 xyz).
    """
    override = float(getattr(params, "trap_slip_bps", 0.0) or 0.0)
    if override > 0:
        return override
    return slip_bps_for(coin, params)


def reserved_slip_bps(coin: str, entry: float, stop: float, params: SwingParams) -> float:
    """Slip bps baked into the size, and the same number the exit must pay.

    Sweep keeps the coin allowance. Trapped flat uses ``trap_flat_allowance``.
    Proportional, the one definition registered for this study, is the
    minimum of that allowance and the stop distance in bps. A stop tighter
    than the flat allowance then reserves only its own distance.
    """
    if str(getattr(params, "entry", "sweep") or "sweep") != "trapped":
        return slip_bps_for(coin, params)
    flat = trap_flat_allowance(coin, params)
    mode = str(getattr(params, "trap_slip_mode", "flat") or "flat")
    if mode == "proportional" and float(entry) > 0:
        stop_bps = abs(float(entry) - float(stop)) / float(entry) * 10_000.0
        return min(flat, max(0.0, stop_bps))
    return flat


def exit_slip_bps(coin: str, params: SwingParams, reserved: float | None) -> float:
    """Slip the paper close pays. ``None`` is the coin allowance.

    A trapped arm stores the reserved number, including 0, so a later
    change to the flag cannot make the exit pay a different slip than
    the size was built with.
    """
    if reserved is None:
        return slip_bps_for(coin, params)
    return float(reserved)


def closed_candles(bars: list[dict] | None, tf_sec: int, now: float) -> list[dict]:
    """Closed candles of ``tf_sec`` from bars that are already that width."""
    if not bars or tf_sec <= 0:
        return []
    dedup: dict[float, dict] = {}
    for bar in bars:
        if "t" not in bar:
            continue
        t = bar_open_sec(float(bar["t"]))
        if t + tf_sec > float(now) + 1e-9:
            continue
        try:
            high = float(bar.get("h") or 0)
            low = float(bar.get("l") or 0)
            close = float(bar.get("c") or 0)
        except (TypeError, ValueError):
            continue
        if high <= 0 or low <= 0 or high < low:
            continue
        dedup[t] = {
            "t": t,
            "h": high,
            "l": low,
            "c": close if close > 0 else (high + low) / 2.0,
            "v": float(bar.get("v") or 0),
        }
    return [dedup[k] for k in sorted(dedup)]


def aggregate(bars: list[dict] | None, src_sec: int, dst_sec: int, now: float) -> list[dict]:
    """Closed ``dst_sec`` candles. ``bars`` are ``src_sec`` wide. No finer than the source."""
    src = closed_candles(bars, src_sec, now)
    if dst_sec == src_sec:
        return src
    if dst_sec < src_sec or not src:
        return []
    buckets: dict[int, dict] = {}
    for bar in src:
        key = int(bar["t"] // dst_sec) * dst_sec
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = {
                "t": float(key),
                "h": bar["h"],
                "l": bar["l"],
                "c": bar["c"],
                "v": bar["v"],
            }
        else:
            cur["h"] = max(cur["h"], bar["h"])
            cur["l"] = min(cur["l"], bar["l"])
            cur["c"] = bar["c"]
            cur["v"] = cur["v"] + bar["v"]
    return [buckets[k] for k in sorted(buckets) if k + dst_sec <= float(now) + 1e-9]


def _fractals(candles: list[dict], tf_sec: int, pivot: int, kind: str) -> list[tuple[float, float]]:
    width = max(1, int(pivot))
    field = "l" if kind == "support" else "h"
    found: list[tuple[float, float]] = []
    for i in range(width, len(candles) - width):
        price = float(candles[i][field])
        left = candles[i - width : i]
        right = candles[i + 1 : i + 1 + width]
        if kind == "support":
            ok = all(price < c["l"] for c in left) and all(price <= c["l"] for c in right)
        else:
            ok = all(price > c["h"] for c in left) and all(price >= c["h"] for c in right)
        if not ok:
            continue
        # Known only once the right-hand pivot bar has closed.
        confirm = float(candles[i + width]["t"]) + tf_sec
        found.append((price, confirm))
    return found


def _prev_day(daily: list[dict], now: float) -> dict | None:
    today = int(float(now) // 86400) * 86400
    closed = [bar for bar in daily if bar["t"] + 86400 <= today + 1e-9]
    return closed[-1] if closed else None


def _prev_week(hourly: list[dict], now: float) -> tuple[float, float, float] | None:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromtimestamp(float(now), timezone.utc)
    monday = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_end = monday.timestamp()
    prev_start = prev_end - 7 * 86400
    window = [bar for bar in hourly if prev_start - 1e-9 <= bar["t"] < prev_end]
    if not window:
        return None
    return max(bar["h"] for bar in window), min(bar["l"] for bar in window), prev_end


def _cluster(raw: list[tuple[float, str, float]], bps: float) -> list[dict]:
    if not raw:
        return []
    ordered = sorted(raw, key=lambda item: item[0])
    groups: list[dict] = []
    for price, source, ts in ordered:
        if groups:
            anchor = groups[-1]["price"]
            dist = abs(price - anchor) / anchor * 10_000.0 if anchor else 1e9
        else:
            dist = 1e9
        if not groups or dist > bps:
            groups.append({"price": price, "n": 1, "sources": [source], "ts": ts})
        else:
            group = groups[-1]
            group["n"] += 1
            group["price"] = (group["price"] * (group["n"] - 1) + price) / group["n"]
            group["sources"].append(source)
            group["ts"] = max(group["ts"], ts)
    return groups


def _score_group(group: dict, candles_4h: list[dict], kind: str, bps: float) -> Level | None:
    """Score distinct swing and session events, not every bar that sat nearby.

    A flat range prints the same low on hundreds of 4h bars. That is one
    floor, not hundreds of tests. A level price returned to as a separate
    4h or daily swing counts once per event.
    """
    del candles_4h, bps
    price = float(group["price"])
    if price <= 0:
        return None
    touches = int(group["n"])
    sources = tuple(dict.fromkeys(group["sources"]))
    src = set(sources)
    bonus = 0.0
    if "PDH" in src or "PDL" in src:
        bonus += 2.0
    if "PWH" in src or "PWL" in src:
        bonus += 2.0
    frames = set()
    for name in src:
        if name.endswith("_low") or name.endswith("_high"):
            frames.add(name.split("_", 1)[0])
            bonus += 1.0
    if len(frames) >= 2:
        bonus += 1.0
    return Level(price, kind, float(touches) + bonus, touches, sources, float(group["ts"]))


_LEVEL_CACHE: dict[tuple, list] = {}
_MACRO_CACHE: dict[tuple, object] = {}
_ATR_CACHE: dict[tuple, float | None] = {}


def build_levels(
    hourly: list[dict] | None,
    now: float,
    params: SwingParams,
) -> list[Level]:
    """Top support and resistance from 4h/daily swings, PDH/PDL, and PWH/PWL."""
    cache_key = (
        id(hourly) if hourly is not None else 0,
        int(float(now) // 14400),
        int(params.pivot),
        round(float(params.cluster_bps), 4),
        int(params.top_n),
        round(float(params.min_score), 4),
        str(getattr(params, "level_set", "all") or "all"),
        int(getattr(params, "min_touches", 0) or 0),
    )
    cached = _LEVEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    h4 = aggregate(hourly, 3600, 14400, now)
    daily = aggregate(hourly, 3600, 86400, now)
    pivot = max(1, int(params.pivot))
    raw_support: list[tuple[float, str, float]] = []
    raw_resist: list[tuple[float, str, float]] = []
    for price, ts in _fractals(h4, 14400, pivot, "support"):
        if ts <= float(now) + 1e-9:
            raw_support.append((price, "4h_low", ts))
    for price, ts in _fractals(h4, 14400, pivot, "resistance"):
        if ts <= float(now) + 1e-9:
            raw_resist.append((price, "4h_high", ts))
    for price, ts in _fractals(daily, 86400, pivot, "support"):
        if ts <= float(now) + 1e-9:
            raw_support.append((price, "1d_low", ts))
    for price, ts in _fractals(daily, 86400, pivot, "resistance"):
        if ts <= float(now) + 1e-9:
            raw_resist.append((price, "1d_high", ts))
    prev = _prev_day(daily, now)
    if prev is not None:
        known = float(prev["t"]) + 86400.0
        raw_resist.append((float(prev["h"]), "PDH", known))
        raw_support.append((float(prev["l"]), "PDL", known))
    week = _prev_week(aggregate(hourly, 3600, 3600, now), now)
    if week is not None:
        high, low, known = week
        raw_resist.append((high, "PWH", known))
        raw_support.append((low, "PWL", known))
    level_set = str(getattr(params, "level_set", "all") or "all")
    raw_support = [row for row in raw_support if _source_ok(row[1], level_set)]
    raw_resist = [row for row in raw_resist if _source_ok(row[1], level_set)]
    min_touches = int(getattr(params, "min_touches", 0) or 0)
    bps = float(params.cluster_bps)
    levels: list[Level] = []
    for kind, raw in (("support", raw_support), ("resistance", raw_resist)):
        ranked = []
        for group in _cluster(raw, bps):
            level = _score_group(group, h4, kind, bps)
            if level is None or level.score + 1e-9 < float(params.min_score):
                continue
            if min_touches and level.touches < min_touches:
                continue
            ranked.append(level)
        ranked.sort(key=lambda level: (level.score, level.ts), reverse=True)
        levels.extend(ranked[: max(1, int(params.top_n))])
    if len(_LEVEL_CACHE) > 200_000:
        _LEVEL_CACHE.clear()
    _LEVEL_CACHE[cache_key] = levels
    return levels


def range_edges(levels: list[Level], hourly: list[dict] | None, now: float) -> list[Level]:
    """In a range, longs only at support under the midpoint and shorts only above it."""
    h4 = aggregate(hourly, 3600, 14400, now)
    recent = h4[-42:]
    if len(recent) < 5:
        return levels
    high = max(bar["h"] for bar in recent)
    low = min(bar["l"] for bar in recent)
    mid = (high + low) / 2.0
    kept: list[Level] = []
    for level in levels:
        if level.kind == "support" and level.price <= mid:
            kept.append(level)
        elif level.kind == "resistance" and level.price >= mid:
            kept.append(level)
    return kept


def atr14_of(candles: list[dict]) -> float | None:
    if len(candles) < 15:
        return None
    window = candles[-15:]
    ranges: list[float] = []
    for i in range(1, len(window)):
        high = window[i]["h"]
        low = window[i]["l"]
        prev = window[i - 1]["c"]
        if high < low:
            return None
        ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
    if len(ranges) < 14:
        return None
    return sum(ranges) / float(len(ranges))


@dataclass(frozen=True)
class MacroSnap:
    macro: str
    label: str


def read_swing_macro(hourly: list[dict] | None, now: float, params: SwingParams) -> MacroSnap:
    cache_key = (
        id(hourly) if hourly is not None else 0,
        int(float(now) // 14400),
        tuple(params.macro_tfs),
        params.macro_mode,
        round(float(params.adx_min), 4),
    )
    cached = _MACRO_CACHE.get(cache_key)
    if cached is not None:
        return cached
    states: list[str] = []
    parts: list[str] = []
    for tf in params.macro_tfs:
        sec = TF_SEC.get(tf)
        if sec is None:
            states.append("unknown")
            parts.append(f"{tf}=unknown")
            continue
        candles = aggregate(hourly, 3600, sec, now)
        series = adx_series(candles, 14)
        last = series[-1] if series else None
        if last is None:
            state = "unknown"
            parts.append(f"{tf}=unknown")
        else:
            adx, plus, minus = last
            state = adx_state(adx, plus, minus, float(params.adx_min))
            parts.append(f"{tf}={state}(adx={adx:.1f})")
        states.append(state)
    if len(states) >= 2:
        mode = params.macro_mode if params.macro_mode in ("both", "4h_lead", "4h_only", "4h_lead_1h_fill") else "both"
        macro = combine_macro(states[0], states[1], mode)
    else:
        macro = states[0] if states else "unknown"
    snap = MacroSnap(macro, " ".join(parts) + f" macro={macro}")
    if len(_MACRO_CACHE) > 200_000:
        _MACRO_CACHE.clear()
    _MACRO_CACHE[cache_key] = snap
    return snap


def _sweep_candle(candle: dict, level: float, side: str, min_bps: float) -> float | None:
    price = float(level)
    if price <= 0:
        return None
    if side == "long":
        depth = (price - float(candle["l"])) / price * 10_000.0
        if depth + 1e-9 >= min_bps and float(candle["c"]) > price:
            return float(candle["l"])
        return None
    depth = (float(candle["h"]) - price) / price * 10_000.0
    if depth + 1e-9 >= min_bps and float(candle["c"]) < price:
        return float(candle["h"])
    return None


def find_sweep(candles: list[dict], level: float, side: str, min_bps: float) -> tuple[float, float] | None:
    """Sweep wick and the candle time. Same-bar reclaim, or sweep then the next close."""
    if len(candles) < 1:
        return None
    last = candles[-1]
    wick = _sweep_candle(last, level, side, min_bps)
    if wick is not None:
        return wick, float(last["t"])
    if len(candles) < 2:
        return None
    prev = candles[-2]
    if side == "long":
        depth = (float(level) - float(prev["l"])) / float(level) * 10_000.0
        reclaimed = float(last["c"]) > float(level) and float(prev["l"]) < float(level)
        if depth + 1e-9 >= min_bps and reclaimed:
            return float(prev["l"]), float(last["t"])
        return None
    depth = (float(prev["h"]) - float(level)) / float(level) * 10_000.0
    reclaimed = float(last["c"]) < float(level) and float(prev["h"]) > float(level)
    if depth + 1e-9 >= min_bps and reclaimed:
        return float(prev["h"]), float(last["t"])
    return None


def _nearest_room_pct(side: str, entry: float, levels: list[Level]) -> float:
    """Percent distance from the entry to the nearest opposite level.

    No opposite level is treated as plenty of room so the target rule,
    not this filter, rejects it.
    """
    if entry <= 0:
        return 0.0
    nearest: float | None = None
    for level in levels:
        if side == "long" and level.kind == "resistance" and level.price > entry:
            dist = (level.price - entry) / entry * 100.0
        elif side == "short" and level.kind == "support" and level.price < entry:
            dist = (entry - level.price) / entry * 100.0
        else:
            continue
        nearest = dist if nearest is None else min(nearest, dist)
    if nearest is None:
        return 1e9
    return nearest


def stop_beyond_wick(side: str, wick: float, entry: float, atr: float, frac: float) -> float | None:
    buffer = float(frac) * float(atr)
    if buffer <= 0 or wick <= 0 or entry <= 0:
        return None
    if side == "long":
        stop = float(wick) - buffer
        if stop < entry and stop < wick:
            return stop
        return None
    stop = float(wick) + buffer
    if stop > entry and stop > wick:
        return stop
    return None


def pick_targets(
    side: str,
    entry: float,
    stop: float,
    levels: list[Level],
    min_r: float,
    max_r: float,
    runner: bool,
) -> tuple[float, float, float | None] | str:
    dist = abs(float(entry) - float(stop))
    if dist <= 0 or entry <= 0:
        return "BAD_STOP"
    opposites: list[tuple[float, Level]] = []
    for level in levels:
        if side == "long" and level.kind == "resistance" and level.price > entry:
            opposites.append(((level.price - entry) / dist, level))
        elif side == "short" and level.kind == "support" and level.price < entry:
            opposites.append(((entry - level.price) / dist, level))
    if not opposites:
        return NO_TARGET
    opposites.sort(key=lambda item: item[0])
    valid = [(r, level) for r, level in opposites if min_r - 1e-9 <= r <= max_r + 1e-9]
    if not valid:
        if all(r < min_r for r, _level in opposites):
            return TP_UNDER_MIN
        if all(r > max_r for r, _level in opposites):
            return TP_OVER_MAX
        return TP_OUT_OF_RANGE
    first_r, first = valid[0]
    runner_px = valid[-1][1].price if runner and len(valid) >= 2 else None
    return first.price, first_r, runner_px


def size_swing(
    coin: str,
    entry: float,
    stop: float,
    equity: float,
    params: SwingParams,
    *,
    risk_pct: float,
    leverage: int = 20,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
    max_leverage: int = 20,
    slip_bps: float | None = None,
) -> tuple[float, float]:
    """Size so stop distance + slip + fees stays inside the swing risk and 20x.

    Returns ``(size, loss_at_stop_including_slip_and_fees)``. Size 0 does not arm.
    The loss cap is the swing risk, and never above 2% of ``equity``.
    ``slip_bps`` overrides the allowance. ``None`` uses the coin allowance,
    or the trapped rule when ``params.entry`` is ``trapped``.
    """
    fees = conservative_fees(coin)
    maker = float(maker_fee if maker_fee is not None else fees.maker)
    taker = float(taker_fee if taker_fee is not None else fees.taker)
    if slip_bps is None:
        slip = reserved_slip_bps(coin, entry, stop, params)
    else:
        slip = float(slip_bps)
    risk = float(risk_pct)
    size, _dollar = size_from_stop(
        equity,
        entry,
        stop,
        risk_pct=risk,
        leverage=int(leverage),
        notional_leverage=int(max_leverage),
        include_fees=True,
        maker_fee=maker,
        taker_fee=taker,
        slip_bps=slip,
    )
    capped = cap_size_to_loss(
        size,
        entry,
        stop,
        equity,
        max_loss_pct=min(risk, 0.02),
        include_fees=True,
        maker_fee=maker,
        taker_fee=taker,
        slip_bps=slip,
    )
    loss = loss_at_stop(
        capped, entry, stop, include_fees=True, maker_fee=maker, taker_fee=taker, slip_bps=slip
    )
    # A short stop pays the taker fee on the slipped price, which is a hair
    # more than loss_at_stop. Trapped scales that fill back inside the cap.
    # Sweep and scalp sizes are left as cap_size_to_loss computed them.
    if str(getattr(params, "entry", "sweep") or "sweep") == "trapped" and capped > 0 and equity > 0:
        side = "long" if float(stop) < float(entry) else "short"
        paid = -exit_net(
            side=side,
            size=capped,
            entry=float(entry),
            exit_px=float(stop),
            reason="stop",
            maker_fee=maker,
            taker_fee=taker,
            slip_bps=slip,
        )
        cap_dollars = min(risk, 0.02) * float(equity)
        if paid > cap_dollars > 0:
            capped = capped * cap_dollars / paid
            loss = loss_at_stop(
                capped, entry, stop, include_fees=True, maker_fee=maker, taker_fee=taker, slip_bps=slip
            )
            paid = -exit_net(
                side=side,
                size=capped,
                entry=float(entry),
                exit_px=float(stop),
                reason="stop",
                maker_fee=maker,
                taker_fee=taker,
                slip_bps=slip,
            )
        loss = max(loss, paid)
    return capped, loss


def reclaim_action(reclaim_bps: float, bar_range: float, atr: float, params: SwingParams) -> str:
    """``ok``, ``skip``, or ``retest``. Caps at 0 do nothing."""
    mode = str(getattr(params, "reclaim_mode", "off") or "off")
    if mode not in ("skip", "retest"):
        return "ok"
    bps_cap = float(getattr(params, "max_reclaim_bps", 0.0) or 0.0)
    atr_cap = float(getattr(params, "max_reclaim_atr", 0.0) or 0.0)
    if bps_cap <= 0 and atr_cap <= 0:
        return "ok"
    wide_bps = bps_cap > 0 and float(reclaim_bps) > bps_cap + 1e-9
    wide_atr = atr_cap > 0 and atr > 0 and float(bar_range) > atr_cap * float(atr) + 1e-12
    if wide_bps or wide_atr:
        return mode
    return "ok"


# MAE scratch is disarmed once favorable excursion has reached this R.
# It is not a grid knob.
SCRATCH_ARM_R = 0.5


def scratch_trigger(
    *,
    side: str,
    entry: float,
    stop: float,
    prior_mfe: float,
    mfe: float,
    high: float,
    low: float,
    close: float,
    opened: float,
    now: float,
    scratch_mfe_r: float,
    scratch_minutes: float,
    scratch_mae_r: float,
) -> float | None:
    """Market/trigger price for a scratch, or None.

    Same-bar stop is the caller's job. If this bar prints both the MAE
    trigger and +0.5R, the scratch wins: the path inside the bar is unknown
    and the worse fill is the one this rule is allowed to take.
    """
    risk = abs(float(entry) - float(stop))
    if risk <= 0 or entry <= 0:
        return None
    if side == "long":
        bar_adv = (float(entry) - float(low)) / risk
    else:
        bar_adv = (float(high) - float(entry)) / risk
    mae_r = float(scratch_mae_r or 0.0)
    if mae_r > 0 and float(prior_mfe) < SCRATCH_ARM_R - 1e-12 and bar_adv + 1e-12 >= mae_r:
        if side == "long":
            return float(entry) - mae_r * risk
        return float(entry) + mae_r * risk
    need = float(scratch_mfe_r or 0.0)
    minutes = float(scratch_minutes or 0.0)
    if need > 0 and minutes > 0:
        elapsed = float(now) - float(opened)
        if elapsed + 1e-9 >= minutes * 60.0 and float(mfe) + 1e-12 < need:
            return float(close)
    return None


def paper_scratch_exit(pos, last: float, now: float, params: SwingParams) -> float | None:
    """Last-price scratch for the paper loop. None when the flags are off.

    A single print cannot be both +0.5R and −0.6R. The running favorable
    extreme is what disarms the MAE scratch.
    """
    mae_r = float(getattr(params, "scratch_mae_r", 0.0) or 0.0)
    need = float(getattr(params, "scratch_mfe_r", 0.0) or 0.0)
    minutes = float(getattr(params, "scratch_minutes", 0.0) or 0.0)
    if mae_r <= 0 and not (need > 0 and minutes > 0):
        return None
    entry = float(pos.entry)
    stop = float(pos.stop)
    risk = abs(entry - stop)
    if risk <= 0 or entry <= 0 or last <= 0:
        return None
    if pos.side == "long":
        fav = (float(last) - entry) / risk
        adv = (entry - float(last)) / risk
    else:
        fav = (entry - float(last)) / risk
        adv = (float(last) - entry) / risk
    prior = float(getattr(pos, "mfe_r", 0.0) or 0.0)
    pos.mfe_r = max(prior, fav)
    pos.mae_r = max(float(getattr(pos, "mae_r", 0.0) or 0.0), adv)
    if mae_r > 0 and prior < SCRATCH_ARM_R - 1e-12 and adv + 1e-12 >= mae_r:
        return float(last)
    if need > 0 and minutes > 0:
        if float(now) - float(pos.opened_at) + 1e-9 >= minutes * 60.0 and pos.mfe_r + 1e-12 < need:
            return float(last)
    return None


def plan_trade(
    coin: str,
    now: float,
    confirm: list[dict] | None,
    hourly: list[dict] | None,
    params: SwingParams,
    equity: float,
    *,
    risk_pct: float,
    leverage: int = 20,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
    max_leverage: int = 20,
) -> Plan | str:
    """One swing plan, or a fail reason. No tape and no order yet."""
    macro = read_swing_macro(hourly, now, params)
    if macro.macro == "unknown":
        return "MACRO_UNKNOWN"
    if not session_open(now, str(getattr(params, "session", "all") or "all")):
        return "OFF_SESSION"
    if bool(getattr(params, "trend_only", False)) and macro.macro == "range":
        return "RANGE"
    adx_floor = float(getattr(params, "min_adx_gate", 0.0) or 0.0)
    if adx_floor > 0:
        higher = params.macro_tfs[-1] if params.macro_tfs else "4h"
        higher_adx = _labeled_adx(macro.label, higher)
        if higher_adx is None or higher_adx + 1e-9 < adx_floor:
            return "ADX_LOW"
    if macro.macro == "up":
        allowed = {"long"}
    elif macro.macro == "down":
        allowed = {"short"}
    else:
        allowed = {"long", "short"}
    side_only = str(getattr(params, "side_only", "both") or "both")
    if side_only == "long":
        allowed &= {"long"}
    elif side_only == "short":
        allowed &= {"short"}
    if not allowed:
        return "SIDE_FILTER"
    levels = build_levels(hourly, now, params)
    if macro.macro == "range":
        levels = range_edges(levels, hourly, now)
    if not levels:
        return NO_LEVEL
    confirm_sec = TF_SEC.get(params.confirm_tf, 900)
    # The sweep only looks at the last two closed bars.
    tail = confirm[-4:] if confirm and len(confirm) > 4 else confirm
    candles = closed_candles(tail, confirm_sec, now)
    if len(candles) < 2:
        return "NO_SWEEP"
    atr_sec = TF_SEC.get(params.atr_tf, 3600)
    atr_key = (
        id(hourly) if hourly is not None else 0,
        id(confirm) if confirm is not None and atr_sec < 3600 else 0,
        int(float(now) // max(atr_sec, 1)),
        params.atr_tf,
        params.confirm_tf,
    )
    if atr_key in _ATR_CACHE:
        atr = _ATR_CACHE[atr_key]
    else:
        if atr_sec >= 3600:
            atr_bars = aggregate(hourly, 3600, atr_sec, now)
        elif confirm is not None and confirm_sec <= atr_sec:
            atr_bars = aggregate(confirm, confirm_sec, atr_sec, now)
        else:
            atr_bars = aggregate(hourly, 3600, 3600, now)
        atr = atr14_of(atr_bars)
        if len(_ATR_CACHE) > 200_000:
            _ATR_CACHE.clear()
        _ATR_CACHE[atr_key] = atr
    if atr is None or atr <= 0:
        return NO_ATR
    best: Plan | None = None
    fail = "NO_SWEEP"
    for level in levels:
        side = "long" if level.kind == "support" else "short"
        if side not in allowed:
            fail = fail if fail != "NO_SWEEP" else "MACRO_SIDE"
            continue
        swept = find_sweep(candles, level.price, side, float(params.min_sweep_bps))
        if swept is None:
            continue
        wick, sweep_ts = swept
        sweep_bps = abs(level.price - wick) / level.price * 10_000.0
        cap = float(getattr(params, "max_sweep_bps", 0.0) or 0.0)
        if cap > 0 and sweep_bps > cap + 1e-9:
            fail = "SWEEP_DEEP"
            continue
        stop = stop_beyond_wick(side, wick, level.price, atr, float(params.atr_frac))
        if stop is None:
            fail = "BAD_STOP"
            continue
        room = float(getattr(params, "min_room_pct", 0.0) or 0.0)
        if room > 0 and _nearest_room_pct(side, level.price, levels) + 1e-9 < room:
            fail = "TOO_CLOSE"
            continue
        target = pick_targets(
            side,
            level.price,
            stop,
            levels,
            float(params.min_r),
            float(params.max_r),
            bool(params.runner),
        )
        if isinstance(target, str):
            fail = target
            continue
        tp, r_mult, runner_px = target
        try:
            size, loss = size_swing(
                coin,
                level.price,
                stop,
                equity,
                params,
                risk_pct=risk_pct,
                leverage=leverage,
                maker_fee=maker_fee,
                taker_fee=taker_fee,
                max_leverage=max_leverage,
            )
        except ValueError:
            fail = SIZE_ZERO
            continue
        if size <= 0:
            fail = SIZE_ZERO
            continue
        bar_range = float(candles[-1]["h"]) - float(candles[-1]["l"])
        reclaim_bps = bar_range / level.price * 10_000.0 if level.price else 0.0
        action = reclaim_action(reclaim_bps, bar_range, atr, params)
        if action == "skip":
            fail = "RECLAIM_WIDE"
            continue
        plan = Plan(
            side=side,
            level=float(level.price),
            level_ts=float(sweep_ts),
            entry=float(level.price),
            stop=float(stop),
            take_profit=float(tp),
            runner_px=None if runner_px is None else float(runner_px),
            size=float(size),
            r_multiple=float(r_mult),
            score=float(level.score),
            sweep=float(wick),
            macro=macro.label,
            sources=level.sources,
            touches=int(level.touches),
            sweep_bps=float(sweep_bps),
            reclaim_bps=float(reclaim_bps),
            retest=(action == "retest"),
            budget=float(loss),
            slip_bps=slip_bps_for(coin, params),
        )
        if best is None or (plan.score, plan.r_multiple) > (best.score, best.r_multiple):
            best = plan
    return best if best is not None else fail


def _decision(
    coin: str,
    reason: str | None,
    *,
    armed: bool = False,
    intent: AloIntent | None = None,
    plan: Plan | None = None,
    absorb: float | None = None,
    window_delta: float | None = None,
    last_15: float | None = None,
    prints_n: int = 0,
    min_prints: int = 30,
    extra: dict | None = None,
) -> Decision:
    return Decision(
        coin=canon_coin(coin),
        bias="NONE",
        pool=None if plan is None else ",".join(plan.sources),
        swing=None if plan is None else plan.level,
        sweep_price=None if plan is None else plan.sweep,
        absorb=absorb,
        window_delta=window_delta,
        last_15s_delta=last_15,
        score=0 if plan is None else int(min(9, round(plan.score))),
        volume_tag="swing",
        armed=armed,
        fail_reason=reason,
        intent=intent,
        print_count=prints_n,
        min_prints=min_prints,
        side=None if plan is None else plan.side,
        macro=None if plan is None else plan.macro,
        extra=extra or {},
        quality=None if plan is None else plan.score,
    )


def _touch_px(prints, mark, bid, ask) -> float | None:
    if prints:
        px = float(prints[-1].price)
        if px > 0:
            return px
    if mark is not None and float(mark) > 0:
        return float(mark)
    if bid and ask and float(bid) > 0 and float(ask) > 0:
        return (float(bid) + float(ask)) / 2.0
    return None


def _retest_ready(slot: dict, plan: Plan, now: float, px: float | None) -> str:
    """``wait``, ``fail``, or ``arm``. The birth timestamp never arms."""
    if float(now) <= float(slot.get("born", slot["since"])) + 1e-9:
        return "wait"
    if px is None or px <= 0:
        return "wait"
    if not slot.get("left"):
        if plan.side == "long":
            if px <= plan.stop:
                return "fail"
            if px > plan.level:
                slot["left"] = True
        else:
            if px >= plan.stop:
                return "fail"
            if px < plan.level:
                slot["left"] = True
        return "wait"
    back = px <= plan.level if plan.side == "long" else px >= plan.level
    return "arm" if back else "wait"


def evaluate_swing(
    engine,
    coin: str,
    *,
    now: float,
    prints: list[TradePrint],
    bars: list[dict],
    pools,
    best_bid: float | None,
    best_ask: float | None,
    equity: float,
    tick: float,
    mark: float | None = None,
    leverage: int = 20,
    htf_bars: list[dict] | None = None,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> Decision:
    """Swing arm. ``bars`` are the confirm timeframe. ``htf_bars`` are 1h."""
    del pools
    coin_u = canon_coin(coin)
    params = getattr(engine, "swing_params", None) or SwingParams()
    min_prints = int(getattr(engine, "min_prints", 30) or 30)
    if coin_u not in set(getattr(engine, "coins", ()) or ()):
        return _decision(coin_u, "OUT_OF_SESSION")
    if str(getattr(params, "entry", "sweep") or "sweep") == "trapped":
        from hl_bot.strategy.model_b.trapped import evaluate_trapped

        return evaluate_trapped(
            engine,
            coin_u,
            now=now,
            prints=prints,
            bars=bars,
            best_bid=best_bid,
            best_ask=best_ask,
            equity=equity,
            tick=tick,
            mark=mark,
            leverage=leverage,
            htf_bars=htf_bars,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
        )
    hourly = htf_bars if htf_bars else (bars if params.confirm_tf == "1h" else None)
    pending_book = getattr(engine, "swing_retest", None)
    if pending_book is None:
        engine.swing_retest = {}
        pending_book = engine.swing_retest
    slot = pending_book.get(coin_u)
    if slot is not None and float(now) > float(slot["since"]) + float(params.fill_hours) * 3600.0:
        pending_book.pop(coin_u, None)
        slot = None
    if slot is not None:
        planned = slot["plan"]
        step = _retest_ready(slot, planned, now, _touch_px(prints, mark, best_bid, best_ask))
        if step == "fail":
            pending_book.pop(coin_u, None)
            return _decision(coin_u, "RETEST_FAILED", plan=planned)
        if step != "arm":
            return _decision(coin_u, "RETEST_WAIT", plan=planned)
        pending_book.pop(coin_u, None)
    else:
        planned = plan_trade(
            coin_u,
            now,
            bars,
            hourly,
            params,
            float(equity),
            risk_pct=float(getattr(engine, "risk_pct", 0.01) or 0.01),
            leverage=int(leverage),
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            max_leverage=int(getattr(engine, "max_notional_leverage", 20) or 20),
        )
        if isinstance(planned, str):
            macro = read_swing_macro(hourly, now, params).label if hourly else "macro=unknown"
            return _decision(coin_u, planned, extra={"macro": macro})
        if planned.retest:
            slot = {
                "key": (planned.side, round(planned.level, 8), int(planned.level_ts)),
                "left": False,
                "since": float(now),
                "plan": planned,
                "born": float(now),
            }
            pending_book[coin_u] = slot
            # The signal tick is not the retest. Wait for a later price.
            return _decision(coin_u, "RETEST_WAIT", plan=planned)
    if params.flow != "off":
        flow_sec = float(TF_SEC.get(params.confirm_tf, 900))
        window = window_prints(prints, coin=coin_u, now=now, window_sec=flow_sec)
        if missing_side(window):
            return _decision(coin_u, "NO_SIDE", plan=planned, prints_n=len(window), min_prints=min_prints)
        if len(window) < min_prints:
            return _decision(
                coin_u, "THIN_TAPE", plan=planned, prints_n=len(window), min_prints=min_prints
            )
        ref = planned.entry
        eps = flat_eps_coins(
            float(getattr(engine, "delta_flat_usdc", 0.0) or 0.0),
            ref,
            float(getattr(engine, "delta_flat_eps", 0.0) or 0.0),
        )
        metrics = analyze_tape(
            window,
            side=planned.side,
            swing=planned.level,
            tick=tick if tick > 0 else ref * 1e-6,
            now=now,
            delta_flat_eps=eps,
        )
        if metrics.fail_reason:
            return _decision(
                coin_u,
                metrics.fail_reason,
                plan=planned,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
                prints_n=len(window),
                min_prints=min_prints,
            )
        absorb = metrics.absorb
        window_delta = metrics.window_delta
        last_15 = metrics.last_15s_delta
        prints_n = len(window)
    else:
        absorb = window_delta = last_15 = None
        prints_n = 0
    sid = f"{coin_u}:swing:{planned.side}:{planned.level:.8f}:{int(planned.level_ts)}"
    blocked = engine.thesis.block_reason(coin_u, sid)
    if blocked:
        return _decision(coin_u, blocked, plan=planned, absorb=absorb, window_delta=window_delta, last_15=last_15)
    limit = alo_limit(
        planned.side,
        planned.level,
        best_bid,
        best_ask,
        tick if tick > 0 else planned.level * 1e-6,
    )
    if limit is None or abs(limit - planned.level) > max(tick, planned.level * 1e-8):
        return _decision(coin_u, "NO_ALO", plan=planned, absorb=absorb, window_delta=window_delta, last_15=last_15)
    if not (planned.stop > 0 and planned.stop != limit):
        return _decision(coin_u, "BAD_STOP", plan=planned)
    intent = AloIntent(
        coin=coin_u,
        side=planned.side,
        limit_px=float(limit),
        size=float(planned.size),
        stop=float(planned.stop),
        take_profit=float(planned.take_profit),
        swing_id=sid,
        tif="Alo",
        market_fallback=False,
        leverage=min(20, int(leverage) if int(leverage) >= 1 else 20),
        work_sec=0.0,
        sweep_px=float(planned.sweep),
        tick=float(tick if tick > 0 else planned.level * 1e-6),
        pool_px=float(planned.take_profit),
        tp_r=float(min(2.0, max(1.0, planned.r_multiple))),
        runner_px=planned.runner_px,
        runner_mode="on" if planned.runner_px else "off",
    )
    return _decision(
        coin_u,
        None,
        armed=True,
        intent=intent,
        plan=planned,
        absorb=absorb,
        window_delta=window_delta,
        last_15=last_15,
        prints_n=prints_n,
        min_prints=min_prints,
        extra={"r": planned.r_multiple, "hold_days": params.hold_days, "sources": list(planned.sources)},
    )


def exit_net(
    *,
    side: str,
    size: float,
    entry: float,
    exit_px: float,
    reason: str,
    maker_fee: float,
    taker_fee: float,
    slip_bps: float,
    closed_pnl: float = 0.0,
) -> float:
    """Price PnL minus exit slip on a stop or time stop, minus fees.

    A target is a maker fill. A stop or max-hold is a taker fill worse by
    ``slip_bps``. ``closed_pnl`` is profit already banked on a partial.
    """
    px = float(exit_px)
    taker_exit = reason in ("stop", "max_hold", "scratch", "tape_end")
    if taker_exit and slip_bps > 0 and entry > 0:
        slip = abs(float(entry)) * float(slip_bps) / 10_000.0
        px = px - slip if side == "long" else px + slip
    if side == "long":
        gross = (px - float(entry)) * float(size)
    else:
        gross = (float(entry) - px) * float(size)
    exit_fee = float(taker_fee) if taker_exit else float(maker_fee)
    fees = float(size) * float(entry) * float(maker_fee) + float(size) * abs(px) * exit_fee
    return gross + float(closed_pnl) - fees


def funding_pnl(
    rates: list[tuple[float, float]] | None,
    *,
    side: str,
    size: float,
    entry: float,
    opened_at: float,
    closed_at: float,
) -> float:
    """Funding paid (negative) or received while the position was open.

    A positive Hyperliquid rate means longs pay shorts.
    """
    if not rates or size <= 0 or entry <= 0:
        return 0.0
    paid = 0.0
    for ts, rate in rates:
        if float(opened_at) < float(ts) <= float(closed_at) + 1e-9:
            signed = float(rate) if side == "long" else -float(rate)
            paid -= signed * float(size) * float(entry)
    return paid

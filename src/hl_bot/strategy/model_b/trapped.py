"""Trapped sellers at support, trapped buyers at resistance.

Off unless ``MODEL_B_SWING_ENTRY=trapped``. The sweep-and-reclaim swing
book does not call this.

Long, in order:

1. The trap bar's low is at a pre-marked support (PDL, swing low, session
   VAL, or a POC in the lower half of the session) and not above the
   midpoint of that support and the nearest resistance.
2. The footprint shows stacked diagonal sell imbalances in the bottom
   fraction of the bar, and sell size there is at least buy size.
3. The next bar does not make a lower low and closes higher.
4. That failure bar's delta is non-negative, or the CVD slope into it is.
5. The paper order is a post-only Alo at the failure close (or a small
   lift off the trap low). The stop is beyond the lower of the two lows
   by a small buffer. The target is the opposite level inside 1R to 5R.

Short mirrors every comparison. Same sizing, fees, and slip as swing.
The resting order still uses the swing thesis: stale cancel if the
target trades before the fill, and the stop size follows partial fills.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_bot.strategy.model_b.alo import alo_limit
from hl_bot.strategy.model_b.footprint import (
    BAR_SEC,
    FootprintBar,
    SessionProfile,
    TapePrint,
    build_footprint,
    cvd_slope,
    diagonal_imbalances,
    heavy_side,
    hl_tick,
    near_extreme,
    session_volume_profile,
    stacked_runs,
)
from hl_bot.strategy.model_b.swing import (
    Level,
    Plan,
    SwingParams,
    _decision,
    build_levels,
    pick_targets,
    session_open,
    size_swing,
)
from hl_bot.strategy.model_b.types import AloIntent, Decision, TradePrint
from hl_bot.strategy.model_b.universe import canon_coin

NO_ZONE = "NO_ZONE"
MID_RANGE = "MID_RANGE"
NO_TRAP = "NO_TRAP"
NO_FAILURE = "NO_FAILURE"
NO_DELTA = "NO_DELTA"


@dataclass(frozen=True)
class TrapSignal:
    side: str
    level: Level
    trap: FootprintBar
    failure: FootprintBar
    entry: float
    stop: float
    absorption: float
    delta: float
    cvd_slope: float


def tape_from_prints(prints: list[TradePrint] | list[TapePrint] | None) -> list[TapePrint]:
    out: list[TapePrint] = []
    for print_ in prints or []:
        if isinstance(print_, TapePrint):
            if print_.side in {"buy", "sell"} and print_.price > 0 and print_.size > 0:
                out.append(print_)
            continue
        side = str(getattr(print_, "side", "") or "")
        if side not in {"buy", "sell"}:
            continue
        price = float(print_.price)
        size = float(print_.size)
        if price <= 0 or size <= 0:
            continue
        out.append(TapePrint(float(print_.ts), price, size, side))
    out.sort(key=lambda item: item.ts)
    return out


def profile_levels(profile: SessionProfile) -> list[Level]:
    """VAL is support. VAH is resistance. POC is support only in the lower half."""
    if not profile.ok or profile.poc is None or profile.val is None or profile.vah is None:
        return []
    if profile.session_high <= profile.session_low:
        return []
    ts = 0.0
    levels = [
        Level(float(profile.val), "support", 3.0, 1, ("VAL",), ts),
        Level(float(profile.vah), "resistance", 3.0, 1, ("VAH",), ts),
    ]
    mid = (float(profile.session_low) + float(profile.session_high)) / 2.0
    if float(profile.poc) <= mid:
        levels.append(Level(float(profile.poc), "support", 3.0, 1, ("POC",), ts))
    else:
        levels.append(Level(float(profile.poc), "resistance", 3.0, 1, ("POC",), ts))
    return levels


def merge_levels(structural: list[Level] | None, profile: SessionProfile | None) -> list[Level]:
    levels = list(structural or [])
    if profile is not None:
        levels.extend(profile_levels(profile))
    return levels


def tag_level(side: str, extreme: float, levels: list[Level], zone_bps: float) -> Level | None:
    """Nearest support (long) or resistance (short) within ``zone_bps`` of the extreme."""
    if extreme <= 0:
        return None
    best: Level | None = None
    best_dist = None
    for level in levels:
        if side == "long" and level.kind != "support":
            continue
        if side == "short" and level.kind != "resistance":
            continue
        if level.price <= 0:
            continue
        dist = abs(float(extreme) - float(level.price)) / float(level.price) * 10_000.0
        if dist <= float(zone_bps) + 1e-9 and (best_dist is None or dist < best_dist):
            best = level
            best_dist = dist
    return best


def is_mid_range(side: str, extreme: float, level: Level, levels: list[Level]) -> bool:
    """True when the extreme is on the far side of the midpoint toward the opposing level.

    No opposing level is not mid-range. The target rule rejects that case.
    """
    if side == "long":
        above = [item.price for item in levels if item.kind == "resistance" and item.price > level.price]
        if not above:
            return False
        mid = (float(level.price) + min(above)) / 2.0
        return float(extreme) > mid + 1e-9
    below = [item.price for item in levels if item.kind == "support" and item.price < level.price]
    if not below:
        return False
    mid = (float(level.price) + max(below)) / 2.0
    return float(extreme) < mid - 1e-9


def has_stacked_trap(bar: FootprintBar, side: str, params: SwingParams, tick: float) -> bool:
    sells, buys = diagonal_imbalances(
        bar,
        ratio=float(params.trap_imbalance),
        min_volume=float(params.trap_min_volume),
        tick=tick,
    )
    prices = sells if side == "long" else buys
    need = max(2, int(params.trap_stacked))
    frac = float(params.trap_near_frac)
    for run in stacked_runs(prices, tick):
        if len(run) < need:
            continue
        if near_extreme(run, bar, frac, side) and heavy_side(bar, frac, side):
            return True
    return False


def is_failure(trap: FootprintBar, failure: FootprintBar, side: str) -> bool:
    """Next bar does not extend the extreme and closes back the other way."""
    if side == "long":
        return float(failure.l) + 1e-12 >= float(trap.l) and float(failure.c) > float(trap.c) + 1e-12
    return float(failure.h) <= float(trap.h) + 1e-12 and float(failure.c) < float(trap.c) - 1e-12


def delta_flipped(side: str, delta: float, slope: float, mode: str) -> bool:
    """Positive or neutral for a long. Negative or neutral for a short."""
    if side == "long":
        delta_ok = float(delta) >= -1e-12
        slope_ok = float(slope) >= -1e-12
    else:
        delta_ok = float(delta) <= 1e-12
        slope_ok = float(slope) <= 1e-12
    which = mode if mode in {"either", "delta", "cvd", "both"} else "either"
    if which == "delta":
        return delta_ok
    if which == "cvd":
        return slope_ok
    if which == "both":
        return delta_ok and slope_ok
    return delta_ok or slope_ok


def _stop_buffer(entry: float, tick: float, params: SwingParams) -> float:
    bps = float(entry) * float(params.trap_stop_bps) / 10_000.0
    return max(float(tick) if tick > 0 else 0.0, bps)


def check_trapped(
    side: str,
    trap: FootprintBar,
    failure: FootprintBar,
    bars: list[FootprintBar],
    failure_index: int,
    levels: list[Level],
    params: SwingParams,
    tick: float,
) -> TrapSignal | str:
    """One side of the pattern, or the first failing check."""
    if side not in {"long", "short"}:
        return NO_ZONE
    only = str(getattr(params, "side_only", "both") or "both")
    if only in {"long", "short"} and side != only:
        return "SIDE_FILTER"
    extreme = float(trap.l if side == "long" else trap.h)
    level = tag_level(side, extreme, levels, float(params.trap_zone_bps))
    if level is None:
        return NO_ZONE
    if is_mid_range(side, extreme, level, levels):
        return MID_RANGE
    if not has_stacked_trap(trap, side, params, tick):
        return NO_TRAP
    if not is_failure(trap, failure, side):
        return NO_FAILURE
    slope = cvd_slope(bars, failure_index, int(params.trap_cvd_bars))
    if not delta_flipped(side, failure.delta, slope, str(params.trap_delta_mode)):
        return NO_DELTA
    absorption = min(float(trap.l), float(failure.l)) if side == "long" else max(float(trap.h), float(failure.h))
    if str(params.trap_entry) == "lift":
        lift = float(absorption) * float(params.trap_lift_bps) / 10_000.0
        raw_entry = float(absorption) + lift if side == "long" else float(absorption) - lift
    else:
        raw_entry = float(failure.c)
    step = float(tick) if tick and tick > 0 else 1.0
    if side == "long":
        entry = math.floor(raw_entry / step + 1e-9) * step
    else:
        entry = math.ceil(raw_entry / step - 1e-9) * step
    buffer = _stop_buffer(entry, tick, params)
    stop = float(absorption) - buffer if side == "long" else float(absorption) + buffer
    if entry <= 0 or buffer <= 0:
        return "BAD_STOP"
    if side == "long" and not (stop < absorption and stop < entry):
        return "BAD_STOP"
    if side == "short" and not (stop > absorption and stop > entry):
        return "BAD_STOP"
    return TrapSignal(
        side=side,
        level=level,
        trap=trap,
        failure=failure,
        entry=float(entry),
        stop=float(stop),
        absorption=float(absorption),
        delta=float(failure.delta),
        cvd_slope=float(slope),
    )


def plan_from_signal(
    coin: str,
    signal: TrapSignal,
    levels: list[Level],
    params: SwingParams,
    equity: float,
    *,
    risk_pct: float,
    leverage: int = 20,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
    max_leverage: int = 20,
) -> Plan | str:
    target = pick_targets(
        signal.side,
        signal.entry,
        signal.stop,
        levels,
        float(params.min_r),
        float(params.max_r),
        bool(params.runner),
    )
    if isinstance(target, str):
        return target
    tp, r_mult, runner_px = target
    try:
        size, _loss = size_swing(
            coin,
            signal.entry,
            signal.stop,
            equity,
            params,
            risk_pct=risk_pct,
            leverage=leverage,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            max_leverage=max_leverage,
        )
    except ValueError:
        return "SIZE_ZERO"
    if size <= 0:
        return "SIZE_ZERO"
    return Plan(
        side=signal.side,
        level=float(signal.level.price),
        level_ts=float(signal.trap.t),
        entry=float(signal.entry),
        stop=float(signal.stop),
        take_profit=float(tp),
        runner_px=None if runner_px is None else float(runner_px),
        size=float(size),
        r_multiple=float(r_mult),
        score=float(signal.level.score),
        sweep=float(signal.absorption),
        macro="trapped",
        sources=signal.level.sources,
        touches=int(signal.level.touches),
        sweep_bps=0.0,
        reclaim_bps=0.0,
        retest=False,
    )


def assess_pair(
    coin: str,
    bars: list[FootprintBar],
    index: int,
    levels: list[Level],
    params: SwingParams,
    tick: float,
    equity: float,
    *,
    risk_pct: float,
    leverage: int = 20,
    maker_fee: float | None = None,
    taker_fee: float | None = None,
    max_leverage: int = 20,
) -> tuple[Plan | None, TrapSignal | None, str]:
    """Plan at failure bar ``index``, plus the signal, or a fail reason.

    Both sides are checked. A finished plan wins. Otherwise the check that
    got furthest is the reason, so a near-miss is not hidden by the other side.
    """
    if index < 1 or index >= len(bars):
        return None, None, "NO_TAPE"
    rank = {
        "SIDE_FILTER": 0,
        NO_ZONE: 1,
        MID_RANGE: 2,
        NO_TRAP: 3,
        NO_FAILURE: 4,
        NO_DELTA: 5,
        "BAD_STOP": 6,
        "NO_TARGET": 7,
        "TP_UNDER_MIN": 7,
        "TP_OVER_MAX": 7,
        "TP_OUT_OF_RANGE": 7,
        "SIZE_ZERO": 8,
    }
    best_fail = "NO_ZONE"
    best_rank = -1
    trap = bars[index - 1]
    failure = bars[index]
    for side in ("long", "short"):
        found = check_trapped(side, trap, failure, bars, index, levels, params, tick)
        if isinstance(found, str):
            score = rank.get(found, 0)
            if score > best_rank:
                best_rank = score
                best_fail = found
            continue
        planned = plan_from_signal(
            coin,
            found,
            levels,
            params,
            equity,
            risk_pct=risk_pct,
            leverage=leverage,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            max_leverage=max_leverage,
        )
        if isinstance(planned, str):
            score = rank.get(planned, 0)
            if score > best_rank:
                best_rank = score
                best_fail = planned
            continue
        return planned, found, ""
    return None, None, best_fail


def _levels_for_trap(
    hourly: list[dict] | None,
    prints: list[TapePrint],
    trap: FootprintBar,
    params: SwingParams,
    tick: float,
) -> list[Level]:
    structural = build_levels(hourly, float(trap.t), params) if hourly else []
    day = int(float(trap.t) // 86400) * 86400
    profile = session_volume_profile(prints, tick=tick, start=float(day), end=float(trap.t))
    return merge_levels(structural, profile)


def evaluate_trapped(
    engine,
    coin: str,
    *,
    now: float,
    prints: list[TradePrint],
    bars: list[dict],
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
    """Paper arm for the trapped-flow entry. Sweep tape gates are not applied."""
    del bars, mark
    coin_u = canon_coin(coin)
    params = getattr(engine, "swing_params", None) or SwingParams()
    if not session_open(now, str(getattr(params, "session", "all") or "all")):
        return _decision(coin_u, "OFF_SESSION")
    tape = tape_from_prints(prints)
    step = float(tick) if tick and tick > 0 else hl_tick(tape[-1].price if tape else 1.0)
    bar_name = str(getattr(params, "trap_bar", "1m") or "1m")
    bar_sec = BAR_SEC.get(bar_name, 60)
    built = build_footprint(tape, bar_sec=bar_sec, tick=step)
    closed = [bar for bar in built if bar.t + bar_sec <= float(now) + 1e-9]
    if len(closed) < 2:
        return _decision(coin_u, "NO_TAPE", prints_n=len(tape))
    failure_index = len(closed) - 1
    trap = closed[failure_index - 1]
    hourly = htf_bars
    levels = _levels_for_trap(hourly, tape, trap, params, step)
    planned, signal, reason = assess_pair(
        coin_u,
        closed,
        failure_index,
        levels,
        params,
        step,
        float(equity),
        risk_pct=float(getattr(engine, "risk_pct", 0.01) or 0.01),
        leverage=int(leverage),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        max_leverage=int(getattr(engine, "max_notional_leverage", 20) or 20),
    )
    if planned is None or signal is None:
        return _decision(coin_u, reason or "NO_TRAP", prints_n=len(tape))
    sid = f"{coin_u}:trapped:{planned.side}:{planned.level:.8f}:{int(signal.trap.t)}"
    blocked = engine.thesis.block_reason(coin_u, sid)
    if blocked:
        return _decision(coin_u, blocked, plan=planned, prints_n=len(tape))
    limit = alo_limit(
        planned.side,
        planned.entry,
        best_bid,
        best_ask,
        step,
    )
    if limit is None or abs(limit - planned.entry) > max(step, planned.entry * 1e-8):
        return _decision(coin_u, "NO_ALO", plan=planned, prints_n=len(tape))
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
        tick=float(step),
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
        window_delta=signal.delta,
        prints_n=len(tape),
        extra={
            "entry": "trapped",
            "r": planned.r_multiple,
            "absorption": signal.absorption,
            "cvd_slope": signal.cvd_slope,
            "sources": list(planned.sources),
            "hold_days": params.hold_days,
        },
    )

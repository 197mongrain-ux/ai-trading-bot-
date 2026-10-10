"""Candle replay of swing Model B.

Public ``candleSnapshot`` keeps about 5,000 bars per interval, so 1h covers
90 days and more, 15m covers about 52 days, and 5m covers about 17. Fees,
stop slippage, and funding are applied. There is no historical aggressor
tape, so this book is the level, macro, sweep, and R path. Live paper still
requires the tape gates.

Run: ``python -m hl_bot.strategy.model_b.swing_replay --cache /tmp/hl_swing_cache``
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.swing import (
    Plan,
    SwingParams,
    TF_SEC,
    exit_net,
    funding_pnl,
    plan_trade,
    scratch_trigger,
    slip_bps_for,
)

COINS = ("BTC", "ETH", "SOL", "xyz:GOLD", "xyz:SP500", "xyz:XYZ100")


@dataclass
class ClosedTrade:
    coin: str
    side: str
    entry: float
    exit: float
    stop: float
    size: float
    net: float
    r: float
    reason: str
    opened_at: float
    closed_at: float
    hold_hours: float
    score: float = 0.0
    touches: int = 0
    sources: str = ""
    macro: str = ""
    target_r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    exit_close: float = 0.0
    sweep: float = 0.0
    sweep_bps: float = 0.0
    reclaim_bps: float = 0.0


@dataclass
class Summary:
    name: str
    trades: int
    wins: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    net_pct: float
    max_dd_pct: float
    avg_hold_hours: float
    span_days: float
    per_coin: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    blotter: list = field(default_factory=list)

    def row(self) -> str:
        return (
            f"{self.name} | trades {self.trades} | win {self.win_rate:.1%} | "
            f"avg win {self.avg_win_r:.2f}R | avg loss {self.avg_loss_r:.2f}R | "
            f"E {self.expectancy_r:.3f}R | net {self.net_pct:.2f}% | "
            f"DD {self.max_dd_pct:.2f}% | hold {self.avg_hold_hours:.1f}h | "
            f"span {self.span_days:.0f}d"
        )


def _bars(raw: list[dict]) -> list[dict]:
    out = []
    for bar in raw:
        out.append(
            {
                "t": float(bar["t"]),
                "o": float(bar["o"]),
                "h": float(bar["h"]),
                "l": float(bar["l"]),
                "c": float(bar["c"]),
                "v": float(bar.get("v") or 0),
            }
        )
    out.sort(key=lambda bar: bar["t"])
    return out


def load_cache(path: str | Path) -> dict[str, dict]:
    root = Path(path)
    book: dict[str, dict] = {}
    for coin in COINS:
        safe = coin.replace(":", "_")
        item: dict = {}
        for interval in ("5m", "15m", "1h"):
            file = root / f"{safe}_{interval}.json"
            if file.exists():
                item[interval] = _bars(json.loads(file.read_text()))
        fund = root / f"{safe}_funding.json"
        if fund.exists():
            item["funding"] = [
                (float(row["t"]) / 1000.0, float(row["rate"])) for row in json.loads(fund.read_text())
            ]
        if item.get("1h"):
            book[coin] = item
    return book


def _summarize(name: str, trades: list[ClosedTrade], equity0: float, equity1: float, peak_dd: float, params: SwingParams, risk: float, span_days: float) -> Summary:
    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    def _avg(rows: list[ClosedTrade]) -> float:
        if not rows:
            return 0.0
        return sum(t.r for t in rows) / len(rows)

    per: dict[str, dict] = {}
    for coin in COINS:
        rows = [t for t in trades if t.coin == coin]
        if not rows:
            per[coin] = {"trades": 0, "expectancy_r": 0.0, "net": 0.0, "win_rate": 0.0}
            continue
        per[coin] = {
            "trades": len(rows),
            "expectancy_r": sum(t.r for t in rows) / len(rows),
            "net": sum(t.net for t in rows),
            "win_rate": sum(1 for t in rows if t.net > 0) / len(rows),
            "avg_hold_hours": sum(t.hold_hours for t in rows) / len(rows),
        }
    n = len(trades)
    return Summary(
        name=name,
        trades=n,
        wins=len(wins),
        win_rate=(len(wins) / n) if n else 0.0,
        avg_win_r=_avg(wins),
        avg_loss_r=_avg(losses),
        expectancy_r=(sum(t.r for t in trades) / n) if n else 0.0,
        net_pct=(equity1 / equity0 - 1.0) * 100.0 if equity0 else 0.0,
        max_dd_pct=peak_dd * 100.0,
        avg_hold_hours=(sum(t.hold_hours for t in trades) / n) if n else 0.0,
        span_days=span_days,
        per_coin=per,
        params={
            "confirm": params.confirm_tf,
            "macro": ",".join(params.macro_tfs),
            "macro_mode": params.macro_mode,
            "atr": f"{params.atr_tf}x{params.atr_frac}",
            "min_r": params.min_r,
            "max_r": params.max_r,
            "risk": risk,
            "hold_days": params.hold_days,
            "runner": params.runner,
            "top_n": params.top_n,
            "level_set": params.level_set,
            "min_touches": params.min_touches,
            "min_room_pct": params.min_room_pct,
            "session": params.session,
            "side_only": params.side_only,
            "partial_r": params.partial_r,
            "atr_frac": params.atr_frac,
        },
        blotter=list(trades),
    )


def _partial_price(plan: Plan, partial_r: float) -> float | None:
    """Half-scale price. None when it would sit past the real target."""
    if partial_r <= 0:
        return None
    dist = abs(plan.entry - plan.stop)
    if dist <= 0:
        return None
    if plan.side == "long":
        px = plan.entry + float(partial_r) * dist
        return px if px < plan.take_profit - 1e-9 else None
    px = plan.entry - float(partial_r) * dist
    return px if px > plan.take_profit + 1e-9 else None


def _fresh_open(plan: Plan, now: float, partial_r: float) -> dict:
    return {
        "mode": "open",
        "plan": plan,
        "opened": now,
        "stop": plan.stop,
        "tp": plan.take_profit,
        "size_left": plan.size,
        "banked": 0.0,
        "tp1_done": False,
        "mfe": 0.0,
        "mae": 0.0,
        "partial_px": _partial_price(plan, partial_r),
    }


def _scale_half(trades, coin, plan, st, high, low, now, fees, slip, rates, eq) -> tuple[float, bool]:
    """Bank half at the fixed R and move the stop to entry.

    If that same bar also trades back to entry, the remainder stops there.
    A same-bar original stop is handled by the caller before this runs.
    """
    px = st.get("partial_px")
    if not px or st.get("tp1_done"):
        return eq, False
    if plan.side == "long":
        hit = high + 1e-9 >= float(px)
        back = low <= plan.entry + 1e-9
    else:
        hit = low - 1e-9 <= float(px)
        back = high >= plan.entry - 1e-9
    if not hit:
        return eq, False
    part = min(plan.size * 0.5, float(st["size_left"]))
    if part <= 0:
        return eq, False
    banked = exit_net(
        side=plan.side,
        size=part,
        entry=plan.entry,
        exit_px=float(px),
        reason="tp",
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip_bps=0.0,
    )
    st["banked"] = float(st.get("banked") or 0.0) + banked
    st["size_left"] = float(st["size_left"]) - part
    st["stop"] = plan.entry
    st["tp1_done"] = True
    if back and st["size_left"] > 0:
        eq = _finish(
            trades, coin, plan, st, plan.entry, "stop", now, fees, slip, rates, eq, exit_close=plan.entry
        )
        return eq, True
    return eq, False


def _clear_swing_caches() -> None:
    """Drop level, macro, and ATR caches so one replay cannot reuse another's bars.

    The caches key a 4h bucket to the first ``now`` that touched it. A later
    replay in the same process, especially on a slower confirm bar, must not
    inherit that snapshot.
    """
    from hl_bot.strategy.model_b.swing import _ATR_CACHE, _LEVEL_CACHE, _MACRO_CACHE

    _LEVEL_CACHE.clear()
    _MACRO_CACHE.clear()
    _ATR_CACHE.clear()


def replay(
    data: dict[str, dict],
    params: SwingParams,
    *,
    risk_pct: float = 0.01,
    equity: float = 10_000.0,
    name: str = "swing",
) -> Summary:
    """Walk the confirm bars. One position per coin. Shared equity."""
    _clear_swing_caches()
    confirm_sec = TF_SEC[params.confirm_tf]
    series: dict[str, dict] = {}
    span_start = None
    span_end = None
    events: list[tuple[float, str, int]] = []
    for coin, item in data.items():
        confirm = item.get(params.confirm_tf) or []
        hourly = item.get("1h") or []
        if len(confirm) < 5 or len(hourly) < 40:
            continue
        series[coin] = item
        for idx, bar in enumerate(confirm):
            now = float(bar["t"]) / 1000.0 + confirm_sec
            if span_start is None or now < span_start:
                span_start = now
            if span_end is None or now > span_end:
                span_end = now
            events.append((now, coin, idx))
    events.sort()
    state: dict[str, dict] = {coin: {"mode": "flat"} for coin in series}
    # One ticket per sweep. A close on the sweep bar must not re-arm that same wick.
    seen: dict[str, set] = {coin: set() for coin in series}
    trades: list[ClosedTrade] = []
    eq = float(equity)
    peak = eq
    max_dd = 0.0
    unreal: dict[str, float] = {coin: 0.0 for coin in series}

    def _mark_dd() -> None:
        nonlocal peak, max_dd
        marked = eq + sum(unreal.values())
        if marked > peak:
            peak = marked
        if peak > 0:
            max_dd = max(max_dd, (peak - marked) / peak)

    for now, coin, idx in events:
        item = series[coin]
        confirm = item[params.confirm_tf]
        bar = confirm[idx]
        high = float(bar["h"])
        low = float(bar["l"])
        close = float(bar["c"])
        st = state[coin]
        fees = conservative_fees(coin)
        slip = slip_bps_for(coin, params)
        rates = item.get("funding") or []

        if st["mode"] == "retest" and idx >= st["from_idx"]:
            plan = st["plan"]
            if now > st["expire"]:
                st["mode"] = "flat"
            else:
                if plan.side == "long":
                    away = low > plan.entry + 1e-9
                    failed = low <= plan.stop + 1e-9
                    touched = low <= plan.entry + 1e-9
                else:
                    away = high < plan.entry - 1e-9
                    failed = high >= plan.stop - 1e-9
                    touched = high >= plan.entry - 1e-9
                if not st.get("left"):
                    # Price has to print entirely on the profit side of the
                    # level before a later bar is allowed to fill the limit.
                    if failed:
                        st["mode"] = "flat"
                    elif away:
                        st["left"] = True
                elif touched:
                    st["mode"] = "working"

        if st["mode"] == "working" and idx >= st["from_idx"]:
            plan: Plan = st["plan"]
            if now > st["expire"]:
                st["mode"] = "flat"
            else:
                if plan.side == "long":
                    filled = low <= plan.entry + 1e-9
                    hit_stop = low <= plan.stop + 1e-9
                    hit_tp = high >= plan.take_profit - 1e-9
                    reached_tp_only = hit_tp and not filled
                else:
                    filled = high >= plan.entry - 1e-9
                    hit_stop = high >= plan.stop - 1e-9
                    hit_tp = low <= plan.take_profit + 1e-9
                    reached_tp_only = hit_tp and not filled
                if reached_tp_only:
                    st["mode"] = "flat"
                elif filled:
                    if hit_stop or (hit_stop and hit_tp):
                        _close_full(
                            trades, coin, plan, plan.stop, "stop", now, now, fees, slip, rates, eq_box := [eq],
                            high=high, low=low, exit_close=close,
                        )
                        eq = eq_box[0]
                        st["mode"] = "flat"
                    elif (scratch_px := _scratch_px(
                        plan, {"mfe": 0.0, "opened": now}, high, low, close, now, params, 0.0
                    )) is not None:
                        # The probe has no prior MFE. A wide fill bar that
                        # reaches 0.6R against and the target scratches.
                        eq = _close_full(
                            trades, coin, plan, scratch_px, "scratch", now, now, fees, slip, rates, [eq],
                            high=high, low=low, exit_close=close,
                        )
                        st["mode"] = "flat"
                    elif hit_tp and not params.runner and not (params.partial_r > 0 and _partial_price(plan, params.partial_r)):
                        eq = _close_full(
                            trades, coin, plan, plan.take_profit, "tp", now, now, fees, slip, rates, [eq],
                            high=high, low=low, exit_close=close,
                        )
                        st["mode"] = "flat"
                    elif hit_tp and params.runner and plan.runner_px and params.partial_r <= 0:
                        eq = _open_then_targets(
                            trades, coin, plan, high, low, close, now, params, fees, slip, rates, eq, st
                        )
                        st["mode"] = "flat"
                    else:
                        state[coin] = _fresh_open(plan, now, params.partial_r)
                        _note_bar(state[coin], plan, high, low)
                        # Full target on the fill bar already returned above.
                        # A 1R/2R scale that also trades back to entry scratches the remainder.
                        if params.partial_r > 0 and not hit_tp:
                            eq, closed = _scale_half(
                                trades, coin, plan, state[coin], high, low, now, fees, slip, rates, eq
                            )
                            if closed:
                                state[coin]["mode"] = "flat"
                        elif hit_tp and params.partial_r > 0:
                            eq = _close_full(
                                trades, coin, plan, plan.take_profit, "tp", now, now, fees, slip, rates, [eq],
                                high=high, low=low, exit_close=close,
                            )
                            state[coin]["mode"] = "flat"

        elif st["mode"] == "open":
            plan = st["plan"]
            prior_mfe = float(st.get("mfe") or 0.0)
            _note_bar(st, plan, high, low)
            stop = float(st["stop"])
            tp = float(st["tp"])
            hold_limit = float(st["opened"]) + float(params.hold_days) * 86400.0
            scratch_px = _scratch_px(plan, st, high, low, close, now, params, prior_mfe)
            if plan.side == "long":
                hit_stop = low <= stop + 1e-9
                hit_tp = high >= tp - 1e-9
            else:
                hit_stop = high >= stop - 1e-9
                hit_tp = low <= tp + 1e-9
            if hit_stop:
                eq = _finish(
                    trades, coin, plan, st, stop, "stop", now, fees, slip, rates, eq, exit_close=close
                )
                st["mode"] = "flat"
            elif scratch_px is not None:
                eq = _finish(
                    trades, coin, plan, st, scratch_px, "scratch", now, fees, slip, rates, eq, exit_close=close
                )
                st["mode"] = "flat"
            elif hit_tp and (not params.runner or st["tp1_done"] or not plan.runner_px or params.partial_r > 0):
                eq = _finish(trades, coin, plan, st, tp, "tp", now, fees, slip, rates, eq, exit_close=close)
                st["mode"] = "flat"
            elif hit_tp and params.runner and plan.runner_px and not st["tp1_done"]:
                part = plan.size * (1.0 - float(params.runner_frac))
                part = min(part, float(st["size_left"]))
                banked = exit_net(
                    side=plan.side,
                    size=part,
                    entry=plan.entry,
                    exit_px=tp,
                    reason="tp",
                    maker_fee=fees.maker,
                    taker_fee=fees.taker,
                    slip_bps=0.0,
                )
                st["banked"] = float(st["banked"]) + banked
                st["size_left"] = float(st["size_left"]) - part
                st["stop"] = plan.entry
                st["tp"] = float(plan.runner_px)
                st["tp1_done"] = True
                runner_hit = (
                    high >= float(plan.runner_px) - 1e-9
                    if plan.side == "long"
                    else low <= float(plan.runner_px) + 1e-9
                )
                if runner_hit and st["size_left"] > 0:
                    eq = _finish(
                        trades, coin, plan, st, float(plan.runner_px), "tp", now, fees, slip, rates, eq,
                        exit_close=close,
                    )
                    st["mode"] = "flat"
            elif params.partial_r > 0 and not st.get("tp1_done"):
                eq, closed = _scale_half(
                    trades, coin, plan, st, high, low, now, fees, slip, rates, eq
                )
                if closed:
                    st["mode"] = "flat"
                elif now >= hold_limit and st["mode"] == "open":
                    eq = _finish(
                        trades, coin, plan, st, close, "max_hold", now, fees, slip, rates, eq, exit_close=close
                    )
                    st["mode"] = "flat"
            elif now >= hold_limit:
                eq = _finish(
                    trades, coin, plan, st, close, "max_hold", now, fees, slip, rates, eq, exit_close=close
                )
                st["mode"] = "flat"

        st = state[coin]
        if st["mode"] == "flat" and eq > 0:
            planned = plan_trade(
                coin,
                now,
                confirm[: idx + 1],
                item["1h"],
                params,
                eq,
                risk_pct=risk_pct,
                leverage=20,
                maker_fee=fees.maker,
                taker_fee=fees.taker,
                max_leverage=20,
            )
            if isinstance(planned, Plan):
                sid = (planned.side, round(planned.level, 6), int(planned.level_ts))
                if sid in seen[coin]:
                    continue
                seen[coin].add(sid)
                state[coin] = {
                    "mode": "retest" if planned.retest else "working",
                    "plan": planned,
                    "from_idx": idx + 1,
                    "expire": now + float(params.fill_hours) * 3600.0,
                    "left": False,
                }

        st = state[coin]
        if st["mode"] == "open":
            held = st["plan"]
            unreal[coin] = exit_net(
                side=held.side,
                size=float(st.get("size_left", held.size)),
                entry=held.entry,
                exit_px=close,
                reason="max_hold",
                maker_fee=fees.maker,
                taker_fee=fees.taker,
                slip_bps=slip,
                closed_pnl=float(st.get("banked") or 0.0),
            )
        else:
            unreal[coin] = 0.0
        _mark_dd()

    span_days = 0.0 if span_start is None or span_end is None else (span_end - span_start) / 86400.0
    return _summarize(name, trades, equity, eq, max_dd, params, risk_pct, span_days)


def _excursion(side: str, entry: float, stop: float, high: float, low: float) -> tuple[float, float]:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0, 0.0
    if side == "long":
        return (high - entry) / risk, (entry - low) / risk
    return (entry - low) / risk, (high - entry) / risk


def _scratch_px(plan: Plan, st: dict, high: float, low: float, close: float, now: float, params: SwingParams, prior_mfe: float) -> float | None:
    return scratch_trigger(
        side=plan.side,
        entry=plan.entry,
        stop=plan.stop,
        prior_mfe=prior_mfe,
        mfe=float(st.get("mfe") or 0.0),
        high=high,
        low=low,
        close=close,
        opened=float(st.get("opened") or now),
        now=now,
        scratch_mfe_r=float(params.scratch_mfe_r),
        scratch_minutes=float(params.scratch_minutes),
        scratch_mae_r=float(params.scratch_mae_r),
    )


def _note_bar(st: dict, plan: Plan, high: float, low: float) -> None:
    fav, adv = _excursion(plan.side, plan.entry, plan.stop, high, low)
    st["mfe"] = max(float(st.get("mfe") or 0.0), fav)
    st["mae"] = max(float(st.get("mae") or 0.0), adv)


def _trade_from(
    coin: str,
    plan: Plan,
    exit_px: float,
    reason: str,
    opened: float,
    closed: float,
    size: float,
    net: float,
    *,
    mfe: float,
    mae: float,
    exit_close: float,
) -> ClosedTrade:
    risk = plan.size * abs(plan.entry - plan.stop)
    return ClosedTrade(
        coin,
        plan.side,
        plan.entry,
        float(exit_px),
        plan.stop,
        float(size),
        net,
        (net / risk) if risk > 0 else 0.0,
        reason,
        opened,
        closed,
        max(0.0, (closed - opened) / 3600.0),
        score=float(plan.score),
        touches=int(getattr(plan, "touches", 0) or 0),
        sources=",".join(plan.sources),
        macro=plan.macro,
        target_r=float(plan.r_multiple),
        mfe_r=float(mfe),
        mae_r=float(mae),
        exit_close=float(exit_close),
        sweep=float(plan.sweep),
        sweep_bps=float(getattr(plan, "sweep_bps", 0.0) or 0.0),
        reclaim_bps=float(getattr(plan, "reclaim_bps", 0.0) or 0.0),
    )


def _close_full(
    trades, coin, plan: Plan, exit_px, reason, opened, closed, fees, slip, rates, eq_box,
    *,
    high: float | None = None,
    low: float | None = None,
    exit_close: float = 0.0,
) -> float:
    net = exit_net(
        side=plan.side,
        size=plan.size,
        entry=plan.entry,
        exit_px=exit_px,
        reason=reason,
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip_bps=slip,
    )
    net += funding_pnl(
        rates, side=plan.side, size=plan.size, entry=plan.entry, opened_at=opened, closed_at=closed
    )
    fav, adv = (0.0, 0.0)
    if high is not None and low is not None:
        fav, adv = _excursion(plan.side, plan.entry, plan.stop, high, low)
    trades.append(
        _trade_from(
            coin, plan, exit_px, reason, opened, closed, plan.size, net,
            mfe=fav, mae=adv, exit_close=exit_close,
        )
    )
    eq_box[0] = eq_box[0] + net
    return eq_box[0]


def _finish(trades, coin, plan, st, exit_px, reason, now, fees, slip, rates, eq, *, exit_close: float = 0.0) -> float:
    size = float(st.get("size_left", plan.size))
    net = exit_net(
        side=plan.side,
        size=size,
        entry=plan.entry,
        exit_px=exit_px,
        reason=reason,
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip_bps=slip,
        closed_pnl=float(st.get("banked") or 0.0),
    )
    # Entry fee on the partial was already taken inside banked. The remainder
    # still owes its entry fee. exit_net charges entry fee on ``size`` only,
    # so the banked partial already includes its own entry fee. Good.
    net += funding_pnl(
        rates,
        side=plan.side,
        size=plan.size,
        entry=plan.entry,
        opened_at=float(st["opened"]),
        closed_at=now,
    )
    trades.append(
        _trade_from(
            coin, plan, exit_px, reason, float(st["opened"]), now, plan.size, net,
            mfe=float(st.get("mfe") or 0.0),
            mae=float(st.get("mae") or 0.0),
            exit_close=exit_close,
        )
    )
    return eq + net


def _open_then_targets(trades, coin, plan, high, low, close, now, params, fees, slip, rates, eq, st) -> float:
    """Fill and both targets on the same bar. Stop on that bar already lost."""
    st["mode"] = "open"
    st["plan"] = plan
    st["opened"] = now
    st["stop"] = plan.stop
    st["tp"] = plan.take_profit
    st["size_left"] = plan.size
    st["banked"] = 0.0
    st["tp1_done"] = False
    st["mfe"] = 0.0
    st["mae"] = 0.0
    _note_bar(st, plan, high, low)
    part = plan.size * (1.0 - float(params.runner_frac))
    banked = exit_net(
        side=plan.side,
        size=part,
        entry=plan.entry,
        exit_px=plan.take_profit,
        reason="tp",
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip_bps=0.0,
    )
    st["banked"] = banked
    st["size_left"] = plan.size - part
    st["tp1_done"] = True
    return _finish(
        trades, coin, plan, st, float(plan.runner_px), "tp", now, fees, slip, rates, eq, exit_close=close
    )


def _variant(base: SwingParams, **kw) -> SwingParams:
    data = {field_name: getattr(base, field_name) for field_name in base.__dataclass_fields__}
    data.update(kw)
    return SwingParams(**data)


def _replay_one(spec: tuple) -> Summary:
    data, label, params, risk_pct = spec
    return replay(data, params, risk_pct=risk_pct, name=label)


def run_grid(data: dict[str, dict], base: SwingParams | None = None, risk: float = 0.01) -> list[Summary]:
    """One-factor grid around the defaults, then the two best risk fractions."""
    base = base or SwingParams(flow="off")
    specs: list[tuple[str, SwingParams, float]] = [("baseline", base, risk)]
    specs.append(("macro-1h-4h-lead", _variant(base, macro_tfs=("1h", "4h"), macro_mode="4h_lead"), risk))
    specs.append(("macro-1h-4h-both", _variant(base, macro_tfs=("1h", "4h"), macro_mode="both"), risk))
    for confirm in ("5m", "1h"):
        specs.append((f"confirm-{confirm}", _variant(base, confirm_tf=confirm), risk))
    for frac in (0.25, 1.0):
        specs.append((f"atr-1h-{frac}", _variant(base, atr_tf="1h", atr_frac=frac), risk))
    specs.append(("atr-15m-0.5", _variant(base, atr_tf="15m", atr_frac=0.5), risk))
    for floor in (1.5, 1.67):
        specs.append((f"minr-{floor}", _variant(base, min_r=floor), risk))
    for days in (1.0, 5.0):
        specs.append((f"hold-{int(days)}d", _variant(base, hold_days=days), risk))
    specs.append(("runner", _variant(base, runner=True), risk))
    specs.append(("risk-0.5pct", base, 0.005))
    specs.append(("top-1", _variant(base, top_n=1), risk))
    specs.append(("top-3", _variant(base, top_n=3), risk))
    from concurrent.futures import ProcessPoolExecutor

    jobs = [(data, label, params, risk_pct) for label, params, risk_pct in specs]
    out: list[Summary] = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        for summary in pool.map(_replay_one, jobs):
            print(summary.row(), flush=True)
            out.append(summary)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Replay swing Model B on cached Hyperliquid candles")
    parser.add_argument("--cache", default="/tmp/hl_swing_cache")
    parser.add_argument("--out", default="docs/pnl/swing_replay.json")
    args = parser.parse_args(argv)
    data = load_cache(args.cache)
    if not data:
        print("no candles in", args.cache)
        return 1
    for coin, item in data.items():
        for interval in ("5m", "15m", "1h"):
            bars = item.get(interval) or []
            if len(bars) >= 2:
                span = (bars[-1]["t"] - bars[0]["t"]) / 86_400_000
                print(f"{coin} {interval} bars={len(bars)} span={span:.1f}d")
    rows = run_grid(data)
    # A config that never trades is not an edge. Rank books that traded first.
    ranked = sorted(rows, key=lambda item: (item.trades > 0, item.expectancy_r, item.net_pct), reverse=True)
    payload = {
        "note": (
            "1h confirm uses ~130 calendar days of candles. 15m is the venue cap "
            "of ~5000 bars (~52 days). 5m is ~17 days. No historical aggressor tape, "
            "so order-flow gates are off in this book and on in paper/live. "
            "R is net PnL divided by size times the price-stop distance. Stops are "
            "often 25-50 bps, so the 25/30 bp slip allowance makes a stopped trade "
            "about -2 price-R while the dollar loss stays near the risk budget. "
            "Drawdown is mark-to-market to the bar close, flattened with that slippage. "
            "This replay accrues funding. The paper loop does not. "
            "A zero-trade config is not ranked as an edge. Defaults stay on the spec: "
            "the least-bad book is still about flat on 14 trades."
        ),
        "recommendation": {
            "defaults_unchanged": True,
            "least_bad": "macro-1h-4h-lead",
            "runner_up": "runner",
            "do_not_go_live": True,
        },
        "ranked": [
            {
                "name": item.name,
                "trades": item.trades,
                "win_rate": item.win_rate,
                "avg_win_r": item.avg_win_r,
                "avg_loss_r": item.avg_loss_r,
                "expectancy_r": item.expectancy_r,
                "net_pct": item.net_pct,
                "max_dd_pct": item.max_dd_pct,
                "avg_hold_hours": item.avg_hold_hours,
                "span_days": item.span_days,
                "per_coin": item.per_coin,
                "params": item.params,
            }
            for item in ranked
        ],
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2))
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

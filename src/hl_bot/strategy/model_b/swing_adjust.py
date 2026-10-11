"""Overfit-aware adjustment study for swing Model B.

The rules below are fixed before the grid runs.

A change is an improvement only when all of these hold against that frame's
reference, on the same coins and the same costs:

- at least 20 trades
- expectancy R higher in the first half of the calendar span and in the second
- expectancy R higher on at least 4 of the 6 coins (a coin counts only when
  the variant actually traded it)

A half with fewer than 8 trades is too thin to call, even if the sign agrees.
Profitable means expectancy R is above zero in both halves, not merely less
negative than the reference.

Combos are not a search. After the one-factor grid, take up to three variants
that beat the reference on the full sample with at least 20 trades. Test the
pair of the top two, and the triple if three exist. One pass. No retuning.

R is net PnL divided by size times the price-stop distance. Fees, stop
slippage, and funding are on. There is no historical aggressor tape.

Run: ``python -m hl_bot.strategy.model_b.swing_adjust --cache /tmp/hl_swing_cache``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hl_bot.strategy.model_b.swing import SwingParams
from hl_bot.strategy.model_b.swing_replay import (
    COINS,
    ClosedTrade,
    Summary,
    _variant,
    load_cache,
    replay,
)

LOOKAHEAD_SEC = 5 * 86400
THIN_HALF = 8
MIN_TRADES = 20
MIN_COINS = 4

# References. Paper defaults are the spec book, not these study frames.
BASELINE = SwingParams(flow="off")
BEST = SwingParams(flow="off", macro_tfs=("1h", "4h"), macro_mode="4h_lead")
STRUCT = SwingParams(flow="off", confirm_tf="1h", macro_tfs=("1h", "4h"), macro_mode="4h_lead")
DAILY = SwingParams(flow="off", confirm_tf="1h", macro_tfs=("4h", "1d"), macro_mode="both")


def _factors(base: SwingParams) -> list[tuple[str, SwingParams, dict]]:
    """One change at a time. The reference itself is not in this list."""
    specs: list[tuple[str, SwingParams, dict]] = []

    def add(name: str, **kw) -> None:
        specs.append((name, _variant(base, **kw), kw))

    for frac in (1.0, 1.5, 2.0):
        add(f"atr-{frac}", atr_tf="1h", atr_frac=frac)
    for touches in (2, 3, 4):
        add(f"touches-{touches}", min_touches=touches)
    add("levels-4h", level_set="4h")
    add("levels-daily", level_set="1d")
    add("levels-session", level_set="session")
    for floor in (1.5, 2.0, 3.0):
        add(f"minr-{floor}", min_r=floor)
    add("partial-1r", partial_r=1.0)
    add("partial-2r", partial_r=2.0)
    add("time-12h", hold_days=0.5)
    add("time-24h", hold_days=1.0)
    add("time-48h", hold_days=2.0)
    add("time-none", hold_days=30.0)
    for confirm in ("5m", "15m", "1h"):
        if confirm != base.confirm_tf:
            add(f"confirm-{confirm}", confirm_tf=confirm)
    for room in (0.5, 1.0, 2.0):
        add(f"room-{room}", min_room_pct=room)
    add("session-us", session="us")
    add("long-only", side_only="long")
    add("short-only", side_only="short")
    return specs


def _run_one(spec: tuple) -> tuple[str, str, Summary, dict]:
    data, frame, label, params, delta, drop = spec
    if drop:
        data = {coin: item for coin, item in data.items() if coin != drop}
    summary = replay(data, params, risk_pct=0.01, name=label)
    return frame, label, summary, delta


def _expectancy(trades: list[ClosedTrade]) -> float:
    if not trades:
        return 0.0
    return sum(trade.r for trade in trades) / len(trades)


def _stats(trades: list[ClosedTrade], equity0: float = 10_000.0) -> dict:
    wins = [trade for trade in trades if trade.net > 0]
    losses = [trade for trade in trades if trade.net <= 0]
    eq = equity0
    peak = eq
    max_dd = 0.0
    for trade in sorted(trades, key=lambda item: item.closed_at):
        eq += trade.net
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    def _avg(rows: list[ClosedTrade]) -> float:
        if not rows:
            return 0.0
        return sum(trade.r for trade in rows) / len(rows)

    return {
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "avg_win_r": _avg(wins),
        "avg_loss_r": _avg(losses),
        "expectancy_r": _expectancy(trades),
        "net_pct": ((eq / equity0) - 1.0) * 100.0 if equity0 else 0.0,
        "max_dd_pct": max_dd * 100.0,
    }


def _midpoint(data: dict, confirm: str) -> float:
    stamps: list[float] = []
    for item in data.values():
        bars = item.get(confirm) or []
        if len(bars) < 2:
            continue
        stamps.append(float(bars[0]["t"]) / 1000.0)
        stamps.append(float(bars[-1]["t"]) / 1000.0)
    if not stamps:
        return 0.0
    return (min(stamps) + max(stamps)) / 2.0


def _coin_expectancy(trades: list[ClosedTrade]) -> dict[str, dict]:
    out = {}
    for coin in COINS:
        rows = [trade for trade in trades if trade.coin == coin]
        out[coin] = {"trades": len(rows), "expectancy_r": _expectancy(rows)}
    return out


def _coins_better(variant: list[ClosedTrade], reference: list[ClosedTrade]) -> int:
    better = 0
    for coin in COINS:
        rows = [trade for trade in variant if trade.coin == coin]
        if not rows:
            continue
        base = [trade for trade in reference if trade.coin == coin]
        if _expectancy(rows) > _expectancy(base):
            better += 1
    return better


def _judge(variant: list[ClosedTrade], reference: list[ClosedTrade], mid: float) -> dict:
    h1 = [trade for trade in variant if trade.opened_at < mid]
    h2 = [trade for trade in variant if trade.opened_at >= mid]
    r1 = [trade for trade in reference if trade.opened_at < mid]
    r2 = [trade for trade in reference if trade.opened_at >= mid]
    e1, e2 = _expectancy(h1), _expectancy(h2)
    b1, b2 = _expectancy(r1), _expectancy(r2)
    coins = _coins_better(variant, reference)
    thin = len(h1) < THIN_HALF or len(h2) < THIN_HALF
    improves = (
        len(variant) >= MIN_TRADES
        and e1 > b1
        and e2 > b2
        and coins >= MIN_COINS
        and not thin
    )
    profitable = improves and e1 > 0 and e2 > 0
    return {
        "half1": _stats(h1),
        "half2": _stats(h2),
        "ref_half1_e": b1,
        "ref_half2_e": b2,
        "coins_better": coins,
        "thin_half": thin,
        "improves": improves,
        "profitable": profitable,
    }


def _parse_macro(label: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (label or "").split():
        if part.startswith("macro="):
            out["macro"] = part.split("=", 1)[1]
            continue
        if "=" not in part:
            continue
        name, rest = part.split("=", 1)
        if name in ("1h", "4h", "1d"):
            out[name] = rest.split("(", 1)[0]
    return out


def _look_after(trade: ClosedTrade, hourly: list[dict]) -> dict:
    risk = abs(trade.entry - trade.stop)
    if risk <= 0:
        return {"target_hit": False, "level_broke": False, "mfe_after_r": 0.0}
    if trade.side == "long":
        target = trade.entry + trade.target_r * risk
    else:
        target = trade.entry - trade.target_r * risk
    end = trade.closed_at + LOOKAHEAD_SEC
    fav = 0.0
    target_hit = False
    beyond: list[bool] = []
    for bar in hourly:
        opened = float(bar["t"]) / 1000.0
        if opened + 3600 <= trade.closed_at + 1e-9:
            continue
        if opened >= end:
            break
        high = float(bar["h"])
        low = float(bar["l"])
        close = float(bar["c"])
        if trade.side == "long":
            fav = max(fav, (high - trade.entry) / risk)
            target_hit = target_hit or high >= target - 1e-9
            beyond.append(close < trade.entry)
        else:
            fav = max(fav, (trade.entry - low) / risk)
            target_hit = target_hit or low <= target + 1e-9
            beyond.append(close > trade.entry)
    return {
        "target_hit": target_hit,
        "level_broke": len(beyond) >= 3 and all(beyond[:3]),
        "mfe_after_r": fav,
    }


def _diagnose(trade: ClosedTrade, hourly: list[dict]) -> dict:
    path = _look_after(trade, hourly)
    states = _parse_macro(trade.macro)
    oppose = "down" if trade.side == "long" else "up"
    agree = "up" if trade.side == "long" else "down"
    tf_states = [states[name] for name in ("1h", "4h", "1d") if name in states]
    macro_against = states.get("macro") == oppose
    split = any(state == agree for state in tf_states) and any(state == oppose for state in tf_states)
    risk = abs(trade.entry - trade.stop) or 1e-12
    if trade.side == "long":
        close_through = (trade.stop - trade.exit_close) / risk
    else:
        close_through = (trade.exit_close - trade.stop) / risk
    wick = trade.reason == "stop" and close_through <= 0.25
    stop_too_tight = trade.reason == "stop" and wick and path["target_hit"]
    weak = trade.touches < 2
    bad_level = bool(weak or (trade.reason == "stop" and path["level_broke"] and not path["target_hit"]))
    early = trade.reason == "stop" and trade.mfe_r < 0.25 and not path["target_hit"]
    stop_bps = abs(trade.entry - trade.stop) / trade.entry * 10_000.0 if trade.entry else 0.0
    slip_bps = 30.0 if trade.coin.startswith("xyz:") else 25.0
    slip_dominates = stop_bps + 1e-9 < slip_bps
    if trade.net > 0:
        primary = "win"
    elif macro_against:
        primary = "against_trend"
    elif stop_too_tight:
        primary = "stop_too_tight"
    elif bad_level:
        primary = "bad_level"
    elif early:
        primary = "entry_too_early"
    elif trade.reason == "max_hold":
        primary = "time_stop"
    elif trade.reason == "stop" and slip_dominates:
        primary = "slip_wider_than_stop"
    else:
        primary = "other"
    return {
        "coin": trade.coin,
        "side": trade.side,
        "reason": trade.reason,
        "r": trade.r,
        "net": trade.net,
        "hold_hours": trade.hold_hours,
        "target_r": trade.target_r,
        "mfe_r": trade.mfe_r,
        "mae_r": trade.mae_r,
        "mfe_after_r": path["mfe_after_r"],
        "target_hit_later": path["target_hit"],
        "touches": trade.touches,
        "sources": trade.sources,
        "macro": trade.macro,
        "stop_bps": stop_bps,
        "wick": wick,
        "against_trend": macro_against,
        "split_trend": split,
        "stop_too_tight": stop_too_tight,
        "bad_level": bad_level,
        "entry_too_early": early,
        "slip_dominates": slip_dominates,
        "level_broke": path["level_broke"],
        "primary": primary,
        "opened": datetime.fromtimestamp(trade.opened_at, timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }


def _pack(frame: str, label: str, summary: Summary, reference: list[ClosedTrade], mid: float, delta: dict, drop: str) -> dict:
    trades = list(summary.blotter)
    full = _stats(trades)
    # Prefer the replay's own equity path for the full sample.
    full["net_pct"] = summary.net_pct
    full["max_dd_pct"] = summary.max_dd_pct
    full["win_rate"] = summary.win_rate
    full["avg_win_r"] = summary.avg_win_r
    full["avg_loss_r"] = summary.avg_loss_r
    full["expectancy_r"] = summary.expectancy_r
    full["trades"] = summary.trades
    verdict = _judge(trades, reference, mid)
    return {
        "frame": frame,
        "name": label,
        "delta": delta,
        "drop": drop,
        "span_days": summary.span_days,
        "full": full,
        "per_coin": _coin_expectancy(trades),
        "verdict": verdict,
        "row": summary.row(),
    }


def _worst(trades: list[ClosedTrade]) -> str | None:
    scored = []
    for coin in COINS:
        rows = [trade for trade in trades if trade.coin == coin]
        if not rows:
            continue
        scored.append((_expectancy(rows), sum(trade.r for trade in rows), coin))
    if not scored:
        return None
    scored.sort()
    return scored[0][2]


def _combo_seeds(rows: list[dict]) -> list[dict]:
    seeds = [
        row for row in rows
        if row["name"] != "ref" and row["full"]["trades"] >= MIN_TRADES and row["full"]["expectancy_r"] > 0
        and row["verdict"]["half1"]["expectancy_r"] > row["verdict"]["ref_half1_e"]
        and row["verdict"]["half2"]["expectancy_r"] > row["verdict"]["ref_half2_e"]
    ]
    # The pre-registered combo pool is anything that beats the reference on the
    # full sample with enough trades. Profit in both halves is reported, not
    # required to enter the combo pass, or a flat book would skip the test.
    if not seeds:
        seeds = [
            row for row in rows
            if row["name"] != "ref"
            and row["full"]["trades"] >= MIN_TRADES
            and row["full"]["expectancy_r"] > rows[0]["full"]["expectancy_r"]
        ]
    seeds.sort(key=lambda row: row["full"]["expectancy_r"], reverse=True)
    return seeds[:3]


def _fmt(row: dict) -> str:
    full = row["full"]
    verdict = row["verdict"]
    return (
        f"| {row['name']} | {full['trades']} | {full['win_rate']:.1%} | "
        f"{full['avg_win_r']:.2f} | {full['avg_loss_r']:.2f} | {full['expectancy_r']:.3f} | "
        f"{full['net_pct']:.2f}% | {full['max_dd_pct']:.2f}% | "
        f"{verdict['half1']['trades']}/{verdict['half1']['expectancy_r']:.3f} | "
        f"{verdict['half2']['trades']}/{verdict['half2']['expectancy_r']:.3f} | "
        f"{verdict['coins_better']} | {verdict['thin_half']} | {verdict['improves']} | {verdict['profitable']} |"
    )


def _render(payload: dict) -> str:
    lines = [
        "# Swing Model B adjustment study",
        "",
        "Paper only. This does not change the Monday paper defaults.",
        "",
        payload["rules"],
        "",
        "## Diagnosis",
        "",
        "Primary label, in this order: macro result opposes the trade, stop was a wick "
        "(close within 0.25R of the stop) and the original target traded within 5 days, "
        "bad level (under 2 touches, or the next three 1h closes stayed through the level "
        "and the target did not come back), entry too early (favorable excursion under 0.25R "
        "and the target did not come back), time stop, stop narrower than the 25/30 bp "
        "slip allowance, otherwise other. Flags can overlap. The primary column is one label.",
        "",
    ]
    for book in payload["diagnosis"]:
        lines.append(f"### {book['name']} ({book['trades']} trades, {book['span_days']:.0f}d)")
        lines.append("")
        counts: dict[str, int] = {}
        for row in book["rows"]:
            if row["primary"] == "win":
                continue
            counts[row["primary"]] = counts.get(row["primary"], 0) + 1
        lines.append(
            "Loss labels: " + (", ".join(f"{key} {counts[key]}" for key in sorted(counts)) or "none")
        )
        lines.append("")
        lines.append("| when | coin | side | exit | R | stop bp | touches | MFE R | target R | target later | primary | macro |")
        lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |")
        for row in book["rows"]:
            if row["primary"] == "win":
                continue
            lines.append(
                f"| {row['opened']} | {row['coin']} | {row['side']} | {row['reason']} | {row['r']:.2f} | "
                f"{row['stop_bps']:.0f} | {row['touches']} | {row['mfe_r']:.2f} | {row['target_r']:.2f} | "
                f"{row['target_hit_later']} | {row['primary']} | {row['macro']} |"
            )
        lines.append("")
        lines.append("| when | coin | side | exit | target R | MFE R | 5d after R | hold h |")
        lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: |")
        for row in book["rows"]:
            if row["primary"] != "win":
                continue
            lines.append(
                f"| {row['opened']} | {row['coin']} | {row['side']} | {row['reason']} | "
                f"{row['target_r']:.2f} | {row['mfe_r']:.2f} | {row['mfe_after_r']:.2f} | {row['hold_hours']:.1f} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Grid",
            "",
            "Half columns are trades and expectancy for entries before and after the midpoint of that frame. "
            "A coin counts as better only when the variant traded it and its expectancy beat the reference.",
            "",
        ]
    )
    header = (
        "| config | trades | win | avg win R | avg loss R | E | net | max DD | "
        "H1 n/E | H2 n/E | coins better | thin | improves | profitable |"
    )
    sep = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |"
    for frame in payload["frames"]:
        lines.append(f"### {frame['name']}")
        lines.append("")
        lines.append(frame["note"])
        lines.append("")
        lines.append(header)
        lines.append(sep)
        for row in frame["rows"]:
            lines.append(_fmt(row))
        lines.append("")
    lines.extend(["## Verdict", "", payload["verdict"], ""])
    return "\n".join(lines)


def _public_row(row: dict) -> dict:
    packed = dict(row)
    return packed


def main(argv: list[str] | None = None) -> int:
    import argparse
    from concurrent.futures import ProcessPoolExecutor

    parser = argparse.ArgumentParser(description="Adjustment study for swing Model B")
    parser.add_argument("--cache", default="/tmp/hl_swing_cache")
    parser.add_argument("--out", default="docs/pnl/swing_adjust.json")
    parser.add_argument("--report", default="docs/pnl/swing_adjust.md")
    args = parser.parse_args(argv)
    data = load_cache(args.cache)
    if not data:
        print("no candles in", args.cache)
        return 1

    frames = {
        "best-15m": (BEST, "15m book, 1h+4h ADX with 4h leading. About 52 days."),
        "struct-1h": (STRUCT, "Same macro, entries on the 1h close. About 130 days."),
        "daily-1h": (DAILY, "4h and daily levels, ADX on 4h and daily, entries on the 1h close. About 130 days."),
    }
    jobs = []
    for frame, (params, _note) in frames.items():
        jobs.append((data, frame, "ref", params, {}, ""))
        for label, variant, delta in _factors(params):
            jobs.append((data, frame, label, variant, delta, ""))
    jobs.append((data, "baseline-15m", "ref", BASELINE, {}, ""))

    print(f"jobs {len(jobs)}", flush=True)
    results: list[tuple[str, str, Summary, dict]] = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        for frame, label, summary, delta in pool.map(_run_one, jobs):
            print(frame, summary.row(), flush=True)
            results.append((frame, label, summary, delta))

    by_frame: dict[str, list[tuple[str, Summary, dict]]] = {}
    for frame, label, summary, delta in results:
        by_frame.setdefault(frame, []).append((label, summary, delta))

    # Drop the worst coin on each study frame. Worst is lowest expectancy,
    # then most negative total R, among coins that traded.
    drop_jobs = []
    worst_of: dict[str, str] = {}
    for frame, (params, _note) in frames.items():
        ref = next(summary for label, summary, _delta in by_frame[frame] if label == "ref")
        coin = _worst(list(ref.blotter))
        if not coin:
            continue
        worst_of[frame] = coin
        drop_jobs.append((data, frame, f"drop-{coin}", params, {"drop": coin}, coin))
    if drop_jobs:
        with ProcessPoolExecutor(max_workers=4) as pool:
            for frame, label, summary, delta in pool.map(_run_one, drop_jobs):
                print(frame, summary.row(), flush=True)
                by_frame[frame].append((label, summary, delta))

    # Combos from the pre-registered seed rule. Built after the one-factor
    # numbers exist, with no further search.
    packed_for_seed: dict[str, list[dict]] = {}
    mids = {
        "best-15m": _midpoint(data, "15m"),
        "struct-1h": _midpoint(data, "1h"),
        "daily-1h": _midpoint(data, "1h"),
        "baseline-15m": _midpoint(data, "15m"),
    }
    combo_jobs = []
    for frame, (params, _note) in frames.items():
        ref_summary = next(summary for label, summary, _delta in by_frame[frame] if label == "ref")
        ref_trades = list(ref_summary.blotter)
        rows = []
        for label, summary, delta in by_frame[frame]:
            rows.append(_pack(frame, label, summary, ref_trades, mids[frame], delta, ""))
        # Reference expectancy for the seed fallback is the ref row.
        rows.sort(key=lambda row: row["name"] != "ref")
        ref_row = next(row for row in rows if row["name"] == "ref")
        ordered = [ref_row] + [row for row in rows if row["name"] != "ref"]
        seeds = _combo_seeds(ordered)
        packed_for_seed[frame] = seeds
        if len(seeds) >= 2:
            deltas = [seed["delta"] for seed in seeds[:2]]
            if _merge_ok(deltas):
                combo_jobs.append(
                    (data, frame, "combo-2", _variant(params, **_merged(deltas)), _merged(deltas), _drop_of(deltas))
                )
        if len(seeds) >= 3:
            deltas = [seed["delta"] for seed in seeds[:3]]
            if _merge_ok(deltas):
                combo_jobs.append(
                    (data, frame, "combo-3", _variant(params, **_merged(deltas)), _merged(deltas), _drop_of(deltas))
                )
    if combo_jobs:
        with ProcessPoolExecutor(max_workers=4) as pool:
            for frame, label, summary, delta in pool.map(_run_one, combo_jobs):
                print(frame, summary.row(), flush=True)
                by_frame[frame].append((label, summary, delta))

    diagnosis = []
    for frame, title in (
        ("best-15m", "Best book: 15m confirm, 1h+4h ADX, 4h leads"),
        ("baseline-15m", "Baseline book: 15m confirm, 4h+daily ADX, both"),
    ):
        summary = next(item for label, item, _delta in by_frame[frame] if label == "ref")
        rows = []
        for trade in summary.blotter:
            hourly = data[trade.coin]["1h"]
            rows.append(_diagnose(trade, hourly))
        diagnosis.append(
            {
                "name": title,
                "trades": summary.trades,
                "span_days": summary.span_days,
                "rows": rows,
            }
        )

    frame_payload = []
    any_profitable = []
    any_improve = []
    for frame, (params, note) in list(frames.items()) + [("baseline-15m", (BASELINE, "Spec defaults on the 15m window. Reference for the diagnosis, not a grid."))]:
        if frame == "baseline-15m":
            continue
        ref_summary = next(item for label, item, _delta in by_frame[frame] if label == "ref")
        ref_trades = list(ref_summary.blotter)
        rows = [
            _pack(frame, label, summary, ref_trades, mids[frame], delta, str(delta.get("drop") or ""))
            for label, summary, delta in by_frame[frame]
        ]
        rows.sort(key=lambda row: (row["full"]["expectancy_r"], row["full"]["net_pct"]), reverse=True)
        for row in rows:
            if row["verdict"]["profitable"]:
                any_profitable.append(row)
            elif row["verdict"]["improves"]:
                any_improve.append(row)
        frame_payload.append({"name": frame, "note": note, "rows": rows, "seeds": [seed["name"] for seed in packed_for_seed.get(frame, [])]})

    if any_profitable:
        verdict = (
            "Robust and profitable on the pre-registered bar: "
            + ", ".join(f"{row['frame']}/{row['name']}" for row in any_profitable)
            + ". See the grid before treating a single window as a live edge. "
            "Order-flow gates were off in every row."
        )
    elif any_improve:
        verdict = (
            "No config is profitable in both halves. These beat the reference on both halves, "
            "on at least four coins, with at least 20 trades and neither half thinner than 8: "
            + ", ".join(f"{row['frame']}/{row['name']} E {row['full']['expectancy_r']:.3f}R" for row in any_improve)
            + ". They are less bad, not a reason to go live. Monday paper stays on the spec defaults."
        )
    else:
        verdict = (
            "Nothing is robustly profitable. No one-factor change and no pre-registered combo "
            "cleared expectancy above zero in both halves, with the improvement holding on at least "
            "four coins, at least 20 trades, and at least 8 trades in each half. "
            "Monday paper stays on the spec defaults: 15m confirm, 4h and daily ADX both, "
            "ATR 1h times 0.5, 1R to 5R, 1% risk, 3-day hold, runner off. "
            "The 14-trade 1h+4h lead book stays a research note, not the paper config."
        )

    payload = {
        "rules": (
            "Fixed before the run. Costs stay on (fees, 25 bp main / 30 bp xyz stop slip, funding). "
            "Flow gates stay off because the venue has no historical aggressor tape. "
            "Halves are split at the midpoint of each frame's candle span, and a trade belongs to the half it opened in. "
            "Levels still use only bars that had closed by the decision. "
            "Improvement requires at least 20 trades, higher expectancy than that frame's reference in both halves, "
            "and higher expectancy on at least 4 of 6 coins. A half with fewer than 8 trades is thin and cannot pass. "
            "Profitable requires expectancy above zero in both halves on top of that. "
            "US hours are 13:30–20:00 UTC. A partial banks half at 1R or 2R and moves the stop to entry; "
            "the same bar trading back to entry stops the remainder. A stop on the same bar as the target still wins. "
            "Time-none is a 30-day cap so the sample can end the trade. "
            f"Worst coins dropped: {worst_of}."
        ),
        "worst_coin": worst_of,
        "diagnosis": diagnosis,
        "frames": frame_payload,
        "profitable": [f"{row['frame']}/{row['name']}" for row in any_profitable],
        "improves": [f"{row['frame']}/{row['name']}" for row in any_improve],
        "verdict": verdict,
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2))
    report = Path(args.report)
    report.write_text(_render(payload))
    print("wrote", dest, "and", report)
    print(verdict)
    return 0


def _merge_ok(deltas: list[dict]) -> bool:
    seen: set[str] = set()
    for delta in deltas:
        for key in delta:
            if key == "drop":
                continue
            if key in seen:
                return False
            seen.add(key)
    return bool(seen)


def _merged(deltas: list[dict]) -> dict:
    out: dict = {}
    for delta in deltas:
        for key, value in delta.items():
            if key != "drop":
                out[key] = value
    return out


def _drop_of(deltas: list[dict]) -> str:
    for delta in deltas:
        if delta.get("drop"):
            return str(delta["drop"])
    return ""


if __name__ == "__main__":
    raise SystemExit(main())

"""Winner-vs-loser profile, with filters chosen on the first half only.

The choice function receives first-half trades of the 130-day 1h book and
nothing else. Five hypotheses are scored there, in a fixed order. A hypothesis
is kept only when winners and losers separate and the first half's expectancy
rises, with at least 8 trades left. At most four are kept. The second half is
replayed after the choice and is not used to drop or retune a filter.

The 15m books are too small to set a threshold. They are a transfer check of
the frozen 1h rules.

``reports/model_b_trade_notes.json`` is not in this repo, so the live scalp
book is not profiled here.

Run: ``python -m hl_bot.strategy.model_b.swing_winners --cache /tmp/hl_swing_cache``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hl_bot.strategy.model_b.swing import SwingParams, _labeled_adx
from hl_bot.strategy.model_b.swing_adjust import (
    BASELINE,
    BEST,
    STRUCT,
    _expectancy,
    _midpoint,
    _stats,
)
from hl_bot.strategy.model_b.swing_replay import COINS, ClosedTrade, Summary, _variant, load_cache, replay

MIN_WINS = 5
MIN_KEPT = 8


def _median(values: list[float]) -> float | None:
    rows = sorted(value for value in values if value is not None)
    if not rows:
        return None
    mid = len(rows) // 2
    if len(rows) % 2:
        return float(rows[mid])
    return (float(rows[mid - 1]) + float(rows[mid])) / 2.0


def _pct(values: list[float], q: float) -> float | None:
    rows = sorted(value for value in values if value is not None)
    if not rows:
        return None
    idx = min(len(rows) - 1, max(0, int(round((len(rows) - 1) * q))))
    return float(rows[idx])


def _families(sources: str) -> str:
    names = [part for part in (sources or "").split(",") if part]
    found = []
    if any(name.startswith("4h_") for name in names):
        found.append("4h")
    if any(name.startswith("1d_") for name in names):
        found.append("daily")
    if any(name in {"PDH", "PDL", "PWH", "PWL"} for name in names):
        found.append("session")
    return "+".join(found) or "none"


def _feature(trade: ClosedTrade, atr_frac: float = 0.5) -> dict:
    when = datetime.fromtimestamp(trade.opened_at, timezone.utc)
    minutes = when.hour * 60 + when.minute
    stop_bps = abs(trade.entry - trade.stop) / trade.entry * 10_000.0 if trade.entry else 0.0
    buffer = abs(trade.stop - trade.sweep)
    atr = (buffer / atr_frac) if buffer > 0 and atr_frac > 0 else None
    stop_atr = (abs(trade.entry - trade.stop) / atr) if atr else None
    macro = ""
    for part in (trade.macro or "").split():
        if part.startswith("macro="):
            macro = part.split("=", 1)[1]
    aligned = (trade.side == "long" and macro == "up") or (trade.side == "short" and macro == "down")
    htf = _families(trade.sources)
    parts = set(htf.split("+")) if htf != "none" else set()
    # Mixed clusters (a 4h swing that also printed PDH) are not a daily-only level.
    htf_level = bool(parts) and parts <= {"daily", "session"}
    mixed_htf = bool(parts & {"daily", "session"})
    return {
        "win": trade.net > 0,
        "coin": trade.coin,
        "side": trade.side,
        "family": htf,
        "htf_level": htf_level,
        "mixed_htf": mixed_htf,
        "touches": trade.touches,
        "aligned": aligned,
        "range": macro == "range",
        "adx_1h": _labeled_adx(trade.macro, "1h"),
        "adx_4h": _labeled_adx(trade.macro, "4h"),
        "adx_1d": _labeled_adx(trade.macro, "1d"),
        "hour": when.hour,
        "us": 13 * 60 + 30 <= minutes < 20 * 60,
        "stop_bps": stop_bps,
        "stop_atr": stop_atr,
        "target_r": trade.target_r,
        "sweep_bps": trade.sweep_bps,
        "reclaim_bps": trade.reclaim_bps,
        "hold_hours": trade.hold_hours,
        "time_to_tp": trade.hold_hours if trade.reason == "tp" else None,
        "mfe_r": trade.mfe_r,
        "mae_r": trade.mae_r,
    }


def _split_stats(rows: list[dict]) -> dict:
    wins = [row for row in rows if row["win"]]
    losses = [row for row in rows if not row["win"]]

    def num(key: str) -> dict:
        return {
            "win": _median([row[key] for row in wins]),
            "loss": _median([row[key] for row in losses]),
        }

    def rate(key: str) -> dict:
        def _rate(group: list[dict]) -> float | None:
            if not group:
                return None
            return sum(1 for row in group if row[key]) / len(group)

        return {"win": _rate(wins), "loss": _rate(losses)}

    families: dict[str, dict] = {}
    for name in sorted({row["family"] for row in rows}):
        families[name] = {
            "win": sum(1 for row in wins if row["family"] == name),
            "loss": sum(1 for row in losses if row["family"] == name),
        }
    coins = {}
    for coin in COINS:
        coins[coin] = {
            "win": sum(1 for row in wins if row["coin"] == coin),
            "loss": sum(1 for row in losses if row["coin"] == coin),
        }
    return {
        "n_win": len(wins),
        "n_loss": len(losses),
        "touches": num("touches"),
        "adx_4h": num("adx_4h"),
        "adx_1d": num("adx_1d"),
        "adx_1h": num("adx_1h"),
        "stop_bps": num("stop_bps"),
        "stop_atr": num("stop_atr"),
        "target_r": num("target_r"),
        "sweep_bps": num("sweep_bps"),
        "reclaim_bps": num("reclaim_bps"),
        "hold_hours": num("hold_hours"),
        "hour": num("hour"),
        "time_to_tp": {"win": _median([row["time_to_tp"] for row in wins if row["time_to_tp"] is not None])},
        "mfe_r": num("mfe_r"),
        "mae_r": num("mae_r"),
        "aligned": rate("aligned"),
        "us": rate("us"),
        "htf_level": rate("htf_level"),
        "mixed_htf": rate("mixed_htf"),
        "range": rate("range"),
        "families": families,
        "coins": coins,
    }


def _screen(trades: list[ClosedTrade], pred) -> tuple[float, int]:
    kept = [trade for trade in trades if pred(trade)]
    return _expectancy(kept), len(kept)


def choose_filters(h1: list[ClosedTrade]) -> dict:
    """Pick filters from first-half trades only. No second-half argument exists."""
    wins = [trade for trade in h1 if trade.net > 0]
    losses = [trade for trade in h1 if trade.net <= 0]
    base_e = _expectancy(h1)
    info: dict = {"n": len(h1), "wins": len(wins), "base_e": base_e, "kept": []}
    if len(wins) < MIN_WINS:
        info["abort"] = f"only {len(wins)} first-half winners; not enough to set a filter"
        return info

    def consider(name: str, pred, note: str, params: dict) -> None:
        exp, n = _screen(h1, pred)
        item = {"name": name, "note": note, "params": params, "h1_e": exp, "h1_n": n, "kept": False}
        if n >= MIN_KEPT and exp > base_e:
            item["kept"] = True
            info["kept"].append(item)
        info.setdefault("scored", []).append(item)

    # 1. Directional macro only. Kept when winners are more often with-trend than losers.
    w_aligned = sum(1 for row in _rows(wins) if row["aligned"]) / len(wins)
    l_aligned = (sum(1 for row in _rows(losses) if row["aligned"]) / len(losses)) if losses else 0.0
    info["aligned_win"] = w_aligned
    info["aligned_loss"] = l_aligned
    if w_aligned >= l_aligned + 0.10:
        consider(
            "trend-only",
            lambda trade: _feature(trade)["aligned"],
            "Drop range. Winners were at least 10 points more often with the macro than losers.",
            {"trend_only": True},
        )
    else:
        info.setdefault("scored", []).append(
            {"name": "trend-only", "kept": False, "note": "winners were not more aligned than losers", "h1_n": 0, "h1_e": base_e, "params": {}}
        )

    # 2. Higher-timeframe ADX floor, only if winners' 4h ADX median clears losers by 2 points.
    w_adx = _median([row["adx_4h"] for row in _rows(wins)])
    l_adx = _median([row["adx_4h"] for row in _rows(losses)])
    info["adx4_win"] = w_adx
    info["adx4_loss"] = l_adx
    if w_adx is not None and l_adx is not None and w_adx >= l_adx + 2:
        threshold = max(20, int(round(w_adx / 5.0) * 5))
        consider(
            f"adx-4h-{threshold}",
            lambda trade, threshold=threshold: (
                _feature(trade)["aligned"] and (_feature(trade)["adx_4h"] or 0) >= threshold
            ),
            f"With the macro, and 4h ADX at least {threshold} (first-half winner median rounded to 5).",
            {"trend_only": True, "min_adx_gate": float(threshold)},
        )
    else:
        info.setdefault("scored", []).append(
            {"name": "adx-4h", "kept": False, "note": "4h ADX did not separate winners from losers", "h1_n": 0, "h1_e": base_e, "params": {}}
        )

    # 3. Daily or session level with at least 3 touches, only if winners carry that tag more often.
    def _htf3(trade: ClosedTrade) -> bool:
        row = _feature(trade)
        return row["htf_level"] and row["touches"] >= 3

    w_htf = sum(1 for trade in wins if _htf3(trade)) / len(wins)
    l_htf = (sum(1 for trade in losses if _htf3(trade)) / len(losses)) if losses else 0.0
    info["htf3_win"] = w_htf
    info["htf3_loss"] = l_htf
    if w_htf >= l_htf + 0.10:
        consider(
            "daily-or-session-3",
            _htf3,
            "Daily or PDH/PDL/PWH/PWL only (no 4h swing in the cluster), at least 3 touches.",
            {"level_set": "swing_htf", "min_touches": 3},
        )
    else:
        info.setdefault("scored", []).append(
            {
                "name": "daily-or-session-3",
                "kept": False,
                "note": (
                    f"Exclusive daily/session rate was {w_htf:.0%} of winners and {l_htf:.0%} of losers. "
                    "A mixed 4h cluster that also tagged PDH does not count."
                ),
                "h1_n": 0,
                "h1_e": base_e,
                "params": {"level_set": "swing_htf", "min_touches": 3},
            }
        )

    # 4. US cash session, only if winners fall in it more often.
    w_us = sum(1 for row in _rows(wins) if row["us"]) / len(wins)
    l_us = (sum(1 for row in _rows(losses) if row["us"]) / len(losses)) if losses else 0.0
    info["us_win"] = w_us
    info["us_loss"] = l_us
    if w_us >= l_us + 0.10:
        consider(
            "session-us",
            lambda trade: _feature(trade)["us"],
            "Arm only 13:30–20:00 UTC. Winners were at least 10 points more often in that window.",
            {"session": "us"},
        )
    else:
        info.setdefault("scored", []).append(
            {"name": "session-us", "kept": False, "note": "winners were not more often in the US window", "h1_n": 0, "h1_e": base_e, "params": {}}
        )

    # 5. Sweep-depth band = first-half winner 25th to 75th, only if the loser median sits outside it.
    lo = _pct([trade.sweep_bps for trade in wins], 0.25)
    hi = _pct([trade.sweep_bps for trade in wins], 0.75)
    loss_med = _median([trade.sweep_bps for trade in losses])
    info["sweep_band"] = [lo, hi]
    info["sweep_loss_median"] = loss_med
    if lo is not None and hi is not None and hi >= lo + 1 and loss_med is not None and (loss_med < lo or loss_med > hi):
        consider(
            f"sweep-{lo:.0f}-{hi:.0f}bp",
            lambda trade, lo=lo, hi=hi: lo - 1e-9 <= trade.sweep_bps <= hi + 1e-9,
            f"Sweep depth between {lo:.0f} and {hi:.0f} bps. The loser median sat outside that winner band.",
            {"min_sweep_bps": float(max(5.0, lo)), "max_sweep_bps": float(hi)},
        )
    else:
        info.setdefault("scored", []).append(
            {"name": "sweep-band", "kept": False, "note": "loser median was inside the winner sweep band, or the band was empty", "h1_n": 0, "h1_e": base_e, "params": {}}
        )

    info["kept"] = info["kept"][:4]
    return info


def _rows(trades: list[ClosedTrade]) -> list[dict]:
    return [_feature(trade) for trade in trades]


def _run_one(spec: tuple) -> tuple[str, str, Summary]:
    data, book, label, params = spec
    summary = replay(data, params, risk_pct=0.01, name=label)
    return book, label, summary


def _judge_row(summary: Summary, ref: list[ClosedTrade], mid: float) -> dict:
    from hl_bot.strategy.model_b.swing_adjust import _coins_better

    trades = list(summary.blotter)
    h1 = [trade for trade in trades if trade.opened_at < mid]
    h2 = [trade for trade in trades if trade.opened_at >= mid]
    r1 = [trade for trade in ref if trade.opened_at < mid]
    r2 = [trade for trade in ref if trade.opened_at >= mid]
    thin = len(h1) < 8 or len(h2) < 8
    improves = (
        len(trades) >= 20
        and _expectancy(h1) > _expectancy(r1)
        and _expectancy(h2) > _expectancy(r2)
        and _coins_better(trades, ref) >= 4
        and not thin
    )
    return {
        "full": {
            "trades": summary.trades,
            "win_rate": summary.win_rate,
            "avg_win_r": summary.avg_win_r,
            "avg_loss_r": summary.avg_loss_r,
            "expectancy_r": summary.expectancy_r,
            "net_pct": summary.net_pct,
            "max_dd_pct": summary.max_dd_pct,
        },
        "half1": _stats(h1),
        "half2": _stats(h2),
        "ref_h1": _expectancy(r1),
        "ref_h2": _expectancy(r2),
        "coins_better": _coins_better(trades, ref),
        "thin": thin,
        "improves": improves,
        "profitable": improves and _expectancy(h1) > 0 and _expectancy(h2) > 0,
        "under_20": summary.trades < 20,
    }


def _fmt_pair(pair: dict | None) -> str:
    if not pair:
        return "—"
    win, loss = pair.get("win"), pair.get("loss")
    def _n(value) -> str:
        if value is None:
            return "—"
        if isinstance(value, float) and abs(value) <= 1:
            return f"{value:.0%}" if value <= 1 else f"{value:.2f}"
        return f"{value:.2f}" if isinstance(value, float) else str(value)
    # Rates are 0-1. Medians above 1 stay numeric. Aligned/us/htf are rates.
    if isinstance(win, float) and win <= 1 and (loss is None or (isinstance(loss, float) and loss <= 1)) and (win != 0 or loss not in (None, 0)):
        # Ambiguous for medians like 0.8 ATR. Caller passes a flag via values > 1 typically.
        pass
    return f"{_n(win)} / {_n(loss)}"


def _num_cell(pair: dict) -> str:
    def _n(value) -> str:
        if value is None:
            return "—"
        return f"{value:.2f}"
    return f"{_n(pair.get('win'))} / {_n(pair.get('loss'))}"


def _rate_cell(pair: dict) -> str:
    def _n(value) -> str:
        if value is None:
            return "—"
        return f"{value:.0%}"
    return f"{_n(pair.get('win'))} / {_n(pair.get('loss'))}"


def _render(payload: dict) -> str:
    lines = [
        "# Winner profile and first-half filters",
        "",
        "Filters were chosen from the first half of the 130-day 1h book only. "
        "The second half was not shown to that choice. A filter that looks better "
        "on the first half was selected because it looked better there, so that half "
        "is not evidence. The 15m books did not set any threshold.",
        "",
        payload["choice_note"],
        "",
        "## Winners vs losers",
        "",
        "Cells are winner median / loser median, or winner rate / loser rate. "
        "First half is the only sample that was allowed to choose a filter.",
        "",
    ]
    for book in payload["books"]:
        lines.append(f"### {book['name']}")
        lines.append("")
        lines.append("| feature | first half win/loss | second half win/loss |")
        lines.append("| --- | --- | --- |")
        h1, h2 = book["h1"], book["h2"]
        rows = [
            ("trades (win, loss)", f"{h1['n_win']}, {h1['n_loss']}", f"{h2['n_win']}, {h2['n_loss']}"),
            ("with macro", _rate_cell(h1["aligned"]), _rate_cell(h2["aligned"])),
            ("range macro", _rate_cell(h1["range"]), _rate_cell(h2["range"])),
            ("ADX 4h", _num_cell(h1["adx_4h"]), _num_cell(h2["adx_4h"])),
            ("ADX daily", _num_cell(h1["adx_1d"]), _num_cell(h2["adx_1d"])),
            ("ADX 1h", _num_cell(h1["adx_1h"]), _num_cell(h2["adx_1h"])),
            ("daily/session only", _rate_cell(h1["htf_level"]), _rate_cell(h2["htf_level"])),
            ("cluster includes daily/session", _rate_cell(h1["mixed_htf"]), _rate_cell(h2["mixed_htf"])),
            ("touches", _num_cell(h1["touches"]), _num_cell(h2["touches"])),
            ("US session", _rate_cell(h1["us"]), _rate_cell(h2["us"])),
            ("hour UTC", _num_cell(h1["hour"]), _num_cell(h2["hour"])),
            ("stop bps", _num_cell(h1["stop_bps"]), _num_cell(h2["stop_bps"])),
            ("stop in ATR", _num_cell(h1["stop_atr"]), _num_cell(h2["stop_atr"])),
            ("target R", _num_cell(h1["target_r"]), _num_cell(h2["target_r"])),
            ("sweep bps", _num_cell(h1["sweep_bps"]), _num_cell(h2["sweep_bps"])),
            ("reclaim bar bps", _num_cell(h1["reclaim_bps"]), _num_cell(h2["reclaim_bps"])),
            ("MFE R", _num_cell(h1["mfe_r"]), _num_cell(h2["mfe_r"])),
            ("MAE R", _num_cell(h1["mae_r"]), _num_cell(h2["mae_r"])),
            ("hold hours", _num_cell(h1["hold_hours"]), _num_cell(h2["hold_hours"])),
            ("hours to TP (winners)", _num_cell(h1["time_to_tp"]), _num_cell(h2["time_to_tp"])),
        ]
        for name, left, right in rows:
            lines.append(f"| {name} | {left} | {right} |")
        lines.append("")
        lines.append("Level families (win, loss) first half: " + ", ".join(
            f"{name} {counts['win']}/{counts['loss']}" for name, counts in h1["families"].items()
        ))
        lines.append("")
        lines.append("Coins (win, loss) first half: " + ", ".join(
            f"{coin} {counts['win']}/{counts['loss']}" for coin, counts in h1["coins"].items()
        ))
        lines.append("")
    lines.extend(["## What the first half was allowed to keep", ""])
    for item in payload["choice"].get("scored") or []:
        flag = "kept" if item.get("kept") else "not kept"
        note = str(item.get("note") or "").rstrip(".")
        if item.get("kept"):
            tail = f"In-sample screen {item.get('h1_n', 0)} trades, E {item.get('h1_e', 0):.3f}R."
        else:
            tail = "Not applied as a chosen filter. The separation rule failed, so this row was not used to set a threshold."
        lines.append(f"- {item['name']}: {flag}. {note}. {tail}")
    lines.append("")
    lines.extend([
        "## Filters",
        "",
        "A kept rule is replayed on the full books. The first half was the fitting sample, so only the second half is out of sample. "
        "A row named check: is the owner's example replayed for transparency. It was not chosen from winners, and it is not the combined config.",
        "",
    ])
    lines.append("| book | filter | trades | win | avg W R | avg L R | E | net | DD | H1 n/E | H2 n/E | coins | <20 | improves | profitable |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |")
    for row in payload["tests"]:
        full = row["full"]
        lines.append(
            f"| {row['book']} | {row['name']} | {full['trades']} | {full['win_rate']:.1%} | "
            f"{full['avg_win_r']:.2f} | {full['avg_loss_r']:.2f} | {full['expectancy_r']:.3f} | "
            f"{full['net_pct']:.2f}% | {full['max_dd_pct']:.2f}% | "
            f"{row['half1']['trades']}/{row['half1']['expectancy_r']:.3f} | "
            f"{row['half2']['trades']}/{row['half2']['expectancy_r']:.3f} | "
            f"{row['coins_better']} | {row['under_20']} | {row['improves']} | {row['profitable']} |"
        )
    lines.extend(["", "## Verdict", "", payload["verdict"], ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from concurrent.futures import ProcessPoolExecutor

    parser = argparse.ArgumentParser(description="Winner profile for swing Model B")
    parser.add_argument("--cache", default="/tmp/hl_swing_cache")
    parser.add_argument("--out", default="docs/pnl/swing_winners.json")
    parser.add_argument("--report", default="docs/pnl/swing_winners.md")
    args = parser.parse_args(argv)
    data = load_cache(args.cache)
    if not data:
        print("no candles")
        return 1
    books = {
        "15m-best": BEST,
        "15m-baseline": BASELINE,
        "1h-130d": STRUCT,
    }
    print("replay references", flush=True)
    refs: dict[str, Summary] = {}
    with ProcessPoolExecutor(max_workers=3) as pool:
        for book, _label, summary in pool.map(
            _run_one, [(data, name, "ref", params) for name, params in books.items()]
        ):
            print(book, summary.row(), flush=True)
            refs[book] = summary

    profiles = []
    mids = {}
    for name, params in books.items():
        trades = list(refs[name].blotter)
        confirm = params.confirm_tf
        mid = _midpoint(data, confirm)
        mids[name] = mid
        h1 = [trade for trade in trades if trade.opened_at < mid]
        h2 = [trade for trade in trades if trade.opened_at >= mid]
        profiles.append({"name": name, "h1": _split_stats(_rows(h1)), "h2": _split_stats(_rows(h2))})

    # Choice sees only the 1h first half.
    h1_1h = [trade for trade in refs["1h-130d"].blotter if trade.opened_at < mids["1h-130d"]]
    choice = choose_filters(h1_1h)
    print("choice", json.dumps({k: v for k, v in choice.items() if k != "scored"}, default=str), flush=True)

    jobs = []
    kept = choice.get("kept") or []
    # The owner example is replayed even when the first half does not select it,
    # so its effect is in the table. It is not treated as a chosen filter.
    check_names: set[str] = set()
    if not any(item["name"] == "daily-or-session-3" and item.get("kept") for item in kept):
        extra = {
            "name": "check:daily-or-session-3",
            "params": {"level_set": "swing_htf", "min_touches": 3},
        }
        check_names.add(extra["name"])
    else:
        extra = None
    for item in kept:
        params = _variant(STRUCT, **item["params"])
        jobs.append((data, "1h-130d", item["name"], params))
        jobs.append((data, "15m-best", item["name"], _variant(BEST, **item["params"])))
        jobs.append((data, "15m-baseline", item["name"], _variant(BASELINE, **item["params"])))
    if extra is not None:
        jobs.append((data, "1h-130d", extra["name"], _variant(STRUCT, **extra["params"])))
        jobs.append((data, "15m-best", extra["name"], _variant(BEST, **extra["params"])))
        jobs.append((data, "15m-baseline", extra["name"], _variant(BASELINE, **extra["params"])))
    if len(kept) >= 2:
        merged: dict = {}
        for item in kept:
            merged.update(item["params"])
        jobs.append((data, "1h-130d", "combined", _variant(STRUCT, **merged)))
        jobs.append((data, "15m-best", "combined", _variant(BEST, **merged)))
        jobs.append((data, "15m-baseline", "combined", _variant(BASELINE, **merged)))
        choice["combined_params"] = merged

    tests = []
    if jobs:
        print(f"replay {len(jobs)} filter jobs", flush=True)
        with ProcessPoolExecutor(max_workers=4) as pool:
            for book, label, summary in pool.map(_run_one, jobs):
                print(book, summary.row(), flush=True)
                ref_trades = list(refs[book].blotter)
                row = _judge_row(summary, ref_trades, mids[book])
                row["book"] = book
                row["name"] = label
                tests.append(row)
    # Reference rows so the table has a baseline.
    for book, summary in refs.items():
        row = _judge_row(summary, list(summary.blotter), mids[book])
        row["book"] = book
        row["name"] = "ref"
        row["improves"] = False
        row["profitable"] = False
        tests.append(row)
    tests.sort(key=lambda row: (row["book"], row["name"] != "ref", row["full"]["expectancy_r"]))

    chosen_rows = [
        row for row in tests
        if row["book"] == "1h-130d" and not row["name"].startswith("check:") and row["name"] != "ref"
    ]
    oos_pass = [row for row in chosen_rows if row["profitable"]]
    oos_better = [row for row in chosen_rows if row["improves"]]
    if choice.get("abort"):
        verdict = choice["abort"] + " Monday paper stays on the spec defaults. Combined config: none."
    elif not kept:
        verdict = (
            "No winner-based filter separated on the first half of the 130-day 1h book "
            "under the pre-registered rules (winners had to differ from losers, and the "
            "first half had to keep at least 8 trades at a higher expectancy). "
            "The daily-or-session level with at least 3 touches was still replayed as a check "
            "(rows named check:daily-or-session-3). It was not a chosen filter, and it does not "
            "count toward the robustness bar. Combined config: none. Monday paper stays on the spec defaults. "
            "Live scalp notes (reports/model_b_trade_notes.json) are not in this repo, so they were not used. "
            "Overfitting: five hypotheses were scored on the first half. That half is not evidence, "
            "and a second-half pass would still be one look after those screens. "
            "MFE, MAE, and time in the trade separate winners from losers only after the trade is open. "
            "They were not used as entry filters."
        )
    elif oos_pass:
        verdict = (
            "A frozen first-half filter was profitable on both halves of the 1h book: "
            + ", ".join(row["name"] for row in oos_pass)
            + ". That is one out-of-sample look after several in-sample screens. "
            "It is not a live edge until order flow is on the tape. "
            "Do not retune it on the second half."
        )
    elif oos_better:
        verdict = (
            "The first-half filters that were replayed beat the 1h reference on both halves "
            "but stayed at or below zero expectancy on at least one half: "
            + ", ".join(f"{row['name']} E {row['full']['expectancy_r']:.3f}R" for row in oos_better)
            + ". Less bad, not profitable. Monday paper stays on the spec defaults. "
            "The first half was used to choose them, so only the second half is out of sample."
        )
    else:
        verdict = (
            "The filters suggested by first-half winners did not survive the second half "
            "of the 130-day 1h book under the robustness bar (higher expectancy in both halves, "
            "at least 4 of 6 coins, at least 20 trades, neither half under 8 trades). "
            "Monday paper stays on the spec defaults. "
            "Choosing them on the first half already used up that half."
        )

    note = (
        f"First-half 1h trades {choice.get('n')}, winners {choice.get('wins')}. "
        f"Kept: {', '.join(item['name'] for item in kept) or 'none'}. "
        "Order-flow gates are off in this replay. They are not in the winner profile."
    )
    payload = {
        "choice_note": note,
        "choice": choice,
        "books": profiles,
        "tests": tests,
        "verdict": verdict,
        "scalp": (
            "reports/model_b_trade_notes.json is not in this repository. "
            "The Oct 6–8 scalp expectancy sample cited on PR #17 is 14 trades and is not stored "
            "here as a per-trade feature file, so it was not mixed into the filter choice."
        ),
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, default=str))
    Path(args.report).write_text(_render(payload))
    print(verdict)
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

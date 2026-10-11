"""Tape replay of the swing sweep entry on the same window as the trapped book.

Locked to the least-bad study frame: 15m confirm, macro 1h and 4h with
the 4h leading, flow off. The fill walks the prints. A post-only limit
rests at the level. A print through the target or the stop before a fill
cancels it. A fill pays the maker fee. A stop, max-hold, or end-of-tape
mark pays the taker fee plus the coin slip (25 bps main, 30 bps xyz).
A target pays the maker fee. One position per coin. Funding is not in
the tape files, so it is not charged.

R is net dollars divided by the risk budget reserved at the arm. A
``tape_end`` row is a mark, reported on its own and inside the with-marks
total. A short tape usually cannot compute 4h ADX and returns
``MACRO_UNKNOWN``. That is the result of the window, not a skipped run.

::

    python -m hl_bot.research.sweep_tape_backtest \\
        --tape /workspace/hl_tape --from 2026-10-09 --to 2026-10-09 \\
        --coins BTC,xyz:SP500 --out docs/pnl/sweep_tape_sample
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from hl_bot.research.trapped_backtest import (
    Working,
    _fmt,
    _iso,
    _span,
    coin_dirname,
    expectancy_split,
    load_coin,
    minute_candles,
    simulate,
    write_csv,
)
from hl_bot.strategy.model_b.footprint import TapePrint
from hl_bot.strategy.model_b.swing import SwingParams, aggregate, plan_trade
from hl_bot.strategy.model_b.swings import bar_open_sec
from hl_bot.strategy.model_b.universe import canon_coin

CONFIRM_SEC = 900
CONFIG_NAME = "sweep-15m-1h4h-4h_lead"


def sweep_params() -> SwingParams:
    """The 1h+4h, 4h-lead book. Not the Monday paper default."""
    return SwingParams(
        flow="off",
        macro_tfs=("1h", "4h"),
        macro_mode="4h_lead",
        confirm_tf="15m",
        entry="sweep",
    )


def confirm_bars(minutes: list[dict]) -> list[dict]:
    if not minutes:
        return []
    end = max(bar_open_sec(bar["t"]) for bar in minutes) + 120.0
    return aggregate(minutes, 60, CONFIRM_SEC, end)


def scan_sweep(
    coin: str,
    confirm: list[dict],
    hourly: list[dict] | None,
    params: SwingParams,
    equity: float,
    risk: float,
) -> tuple[list[Working], Counter]:
    """One plan per closed 15m bar that clears the sweep rules."""
    fails: Counter = Counter()
    orders: list[Working] = []
    if len(confirm) < 2:
        fails["NO_SWEEP"] += 1
        return orders, fails
    for index in range(1, len(confirm)):
        opened = bar_open_sec(confirm[index]["t"])
        now = opened + CONFIRM_SEC
        planned = plan_trade(
            coin,
            now,
            confirm[: index + 1],
            hourly,
            params,
            equity,
            risk_pct=risk,
        )
        if isinstance(planned, str):
            fails[planned] += 1
            continue
        orders.append(
            Working(
                coin=canon_coin(coin),
                side=planned.side,
                signal_ts=float(now),
                expire_ts=float(now) + float(params.fill_hours) * 3600.0,
                entry=float(planned.entry),
                stop=float(planned.stop),
                take_profit=float(planned.take_profit),
                size=float(planned.size),
                level=float(planned.level),
                sources="|".join(planned.sources),
                absorption=float(planned.sweep),
                delta=0.0,
                slope=0.0,
                config=CONFIG_NAME,
                budget=float(planned.budget),
                slip_bps=float(planned.slip_bps),
            )
        )
    return orders, fails


def replay(
    books: dict[str, tuple[list[TapePrint], list[dict], list[dict]]],
    *,
    equity: float = 5000.0,
    risk: float = 0.01,
    params: SwingParams | None = None,
) -> dict:
    """``books`` maps a coin to ``(prints, 15m confirm, hourly)``."""
    params = params or sweep_params()
    orders: list[Working] = []
    fails: Counter = Counter()
    coverage: dict[str, dict] = {}
    tape_map: dict[str, list[TapePrint]] = {}
    for coin, (prints, confirm, hourly) in books.items():
        name = canon_coin(coin)
        found, cell_fails = scan_sweep(name, confirm, hourly, params, equity, risk)
        fails.update(cell_fails)
        orders.extend(found)
        tape_map[name] = prints
        coverage[name] = {
            "prints": len(prints),
            "confirm": len(confirm),
            "hourly": len(hourly or []),
        }
    rows, counts = simulate(orders, tape_map, params, equity, risk)
    return {
        "config": CONFIG_NAME,
        "params": params,
        "coverage": coverage,
        "fails": fails,
        "counts": counts,
        "rows": rows,
        "split": expectancy_split(rows),
        "net": float(counts.get("net", 0.0)),
        "max_dd": float(counts.get("max_dd", 0.0)),
        "equity": equity,
        "risk": risk,
        "scanned": sum(max(item["confirm"] - 1, 0) for item in coverage.values()),
    }


def run(
    root: Path,
    coins: list[str],
    start: str,
    end: str,
    *,
    equity: float = 5000.0,
    risk: float = 0.01,
) -> dict:
    params = sweep_params()
    books: dict[str, tuple[list[TapePrint], list[dict], list[dict]]] = {}
    loaded: dict[str, tuple[list[TapePrint], list[dict]]] = {}
    for coin in coins:
        name = canon_coin(coin)
        prints, hourly = load_coin(root, coin, start, end)
        loaded[name] = (prints, hourly)
        folder = root / coin_dirname(coin)
        if not folder.is_dir():
            alt = root / name
            folder = alt if alt.is_dir() else folder
        minutes = minute_candles(folder, prints)
        books[name] = (prints, confirm_bars(minutes), hourly)
    result = replay(books, equity=equity, risk=risk, params=params)
    result.update(
        {
            "coins": [canon_coin(coin) for coin in coins],
            "start": start,
            "end": end,
            "span": _span(loaded),
            "hourly": {coin: len(hourly) for coin, (_p, hourly) in loaded.items()},
        }
    )
    return result


def render_markdown(result: dict) -> str:
    split = result["split"]
    fails: Counter = result["fails"]
    lines = [
        "# Sweep entry on the recorded tape",
        "",
        "Pipeline check on the tape that was passed in. A couple of hours of prints cannot say whether the sweep has an edge. The config is locked to the least-bad study frame: 15m confirm, macro 1h and 4h with the 4h leading, flow off. Monday paper stays on 15m confirm with 4h and daily, both required. Nothing here is deployed.",
        "",
        f"Window `{result.get('start', '')}` → `{result.get('end', '')}`. Equity {result['equity']:g}, risk {result['risk']:.2%}. R is net dollars divided by the risk budget reserved at the arm. A `tape_end` row is a mark. Closed expectancy leaves it out. The with-marks total includes it. Funding is not charged. The stop loss stays inside the 2% cap.",
        "",
        "## Tape read",
        "",
        "| coin | prints | 15m bars | 1h bars | span |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    coins = result.get("coins") or list(result["coverage"])
    span_map = result.get("span") or {}
    for coin in coins:
        cover = result["coverage"].get(coin, {})
        span = span_map.get(coin)
        span_text = "no file" if not span else f"{span['from']} → {span['to']} ({span['hours']:.2f}h)"
        lines.append(
            f"| {coin} | {cover.get('prints', 0)} | {cover.get('confirm', 0)} | "
            f"{cover.get('hourly', 0)} | {span_text} |"
        )
    unknown = int(fails.get("MACRO_UNKNOWN", 0))
    scanned = int(result.get("scanned") or 0)
    lines.extend(
        [
            "",
            "## Scan",
            "",
            f"Closed 15m bars walked: {scanned}. Signals {result['counts'].get('signaled', 0)}, "
            f"fills {result['counts'].get('filled', 0)}.",
            "",
        ]
    )
    if scanned == 0:
        lines.append("No closed 15m pair to test. The tape did not build two confirm bars.")
    elif unknown == scanned and not result["rows"]:
        lines.append(
            f"Every walked bar returned `MACRO_UNKNOWN` ({unknown}). "
            "4h ADX(14) needs about 29 closed 4h bars, which this window does not have. "
            "No sweep armed. That is the pipeline result for a short tape."
        )
    else:
        parts = ", ".join(f"{key} {fails[key]}" for key in sorted(fails)) or "none"
        lines.append(f"Bars that did not arm: {parts}.")
    budget = "—" if split["avg_budget"] is None else f"{split['avg_budget']:.2f}"
    lines.extend(
        [
            "",
            "## Result",
            "",
            "| config | closed | marks | E closed | E marks | E with marks | avg budget $ | net | max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            (
                f"| {result['config']} | {split['n_closed']} | {split['n_marks']} | "
                f"{_fmt(split['e_closed'])} | {_fmt(split['e_marks'])} | {_fmt(split['e_with_marks'])} | "
                f"{budget} | {result['net']:.2f} | {result['max_dd']:.2%} |"
            ),
            "",
            "The same cell would need both calendar halves, at least 4 of 6 coins, and at least 20 trades before it could move the Monday paper book. This file does not meet that bar.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the swing sweep entry on recorded tape.")
    parser.add_argument("--tape", required=True, help="Root directory of <coin>/<YYYY-MM-DD>.trades.jsonl.gz")
    parser.add_argument("--from", dest="start", required=True, help="UTC day YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="UTC day YYYY-MM-DD")
    parser.add_argument(
        "--coins",
        default="BTC,ETH,SOL,xyz:GOLD,xyz:SP500,xyz:XYZ100",
        help="Comma-separated coins. Directory names use _ for :",
    )
    parser.add_argument("--out", default="docs/pnl/sweep_tape_sample", help="Directory for the CSV and markdown")
    parser.add_argument("--equity", type=float, default=5000.0)
    parser.add_argument("--risk", type=float, default=0.01)
    args = parser.parse_args(argv)
    coins = [part.strip() for part in str(args.coins).split(",") if part.strip()]
    result = run(
        Path(args.tape),
        coins,
        args.start,
        args.end,
        equity=float(args.equity),
        risk=float(args.risk),
    )
    out = Path(args.out)
    write_csv(out / "sweep_trades.csv", result["rows"])
    text = render_markdown(result)
    (out / "sweep_summary.md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

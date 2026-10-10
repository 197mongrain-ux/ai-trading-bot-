"""Tape backtest for the trapped-seller / trapped-buyer entry.

The entry grid is fixed: bar 1m/3m/5m, imbalance 2.5/3/4, stacked 2/3/4,
zone tolerance 10/20/40 bps. The default cell is 1m, 3:1, 3 stacked,
20 bps. Seeing a sample does not add or drop a cell.

A second grid, also fixed, runs only on that default cell. Stop anchor
is trap or zone, min distance is max(X bps, k×ATR(14)) with X 20/30/40
and k 0.5/1.0. Slip on the default geometry is flat 10 bps, flat 15 bps,
or proportional (min of the coin 25/30 allowance and the stop's own bps).
Those cells are not crossed with the 81 entry cells.

R is net dollars divided by the risk budget reserved at the arm (stop
distance plus the slip that was sized, plus fees). It is not the raw
stop distance. ``tape_end`` rows are marks. Closed expectancy leaves
them out. The with-marks total includes them. A stop's dollar loss stays
inside the 2% cap.

Fill model, locked with the grid: the signal is known at the failure
bar's close. A post-only limit rests at the entry. A later print through
the target before a fill cancels the order. A print through the stop
before a fill cancels it. A fill is the limit price and pays the maker
fee. A stop, max-hold, or end-of-tape flatten pays the taker fee plus
the slip that sizing reserved. A target pays the maker fee.
One position per coin. Funding is not in the tape files, so it is not
charged.

::

    python -m hl_bot.research.trapped_backtest \\
        --tape /workspace/hl_tape --from 2026-10-09 --to 2026-10-09 \\
        --coins BTC,xyz:SP500 --out docs/pnl/trapped_sample
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.footprint import (
    BAR_SEC,
    TapePrint,
    build_footprint,
    hl_tick,
    read_trades,
    session_volume_profile,
)
from hl_bot.strategy.model_b.swing import (
    SwingParams,
    aggregate,
    build_levels,
    exit_net,
    reserved_slip_bps,
    size_swing,
)
from hl_bot.strategy.model_b.swings import bar_open_sec
from hl_bot.strategy.model_b.trapped import TrapSignal, assess_pair, merge_levels
from hl_bot.strategy.model_b.universe import canon_coin

# Pre-registered. Do not extend this after looking at a tape.
GRID_BARS = ("1m", "3m", "5m")
GRID_IMBALANCE = (2.5, 3.0, 4.0)
GRID_STACKED = (2, 3, 4)
GRID_ZONE_BPS = (10.0, 20.0, 40.0)
DEFAULT_CELL = ("1m", 3.0, 3, 20.0)
# Pre-registered stop/slip study. Default entry cell only. Do not extend
# after looking at a tape, and do not cross it with the 81 entry cells.
GRID_STOP_ANCHOR = ("trap", "zone")
GRID_MIN_STOP_BPS = (20.0, 30.0, 40.0)
GRID_MIN_STOP_ATR = (0.5, 1.0)
GRID_SLIP_BPS = (10.0, 15.0)

COLUMNS = (
    "config",
    "coin",
    "side",
    "signal_time",
    "fill_time",
    "exit_time",
    "entry",
    "stop",
    "exit",
    "reason",
    "size",
    "net",
    "r",
    "budget",
    "slip_bps",
    "stop_bps",
    "level",
    "sources",
    "absorption",
    "failure_delta",
    "cvd_slope",
)


@dataclass(frozen=True)
class Cell:
    bar: str
    imbalance: float
    stacked: int
    zone_bps: float

    @property
    def name(self) -> str:
        imb = f"{self.imbalance:g}"
        zone = f"{self.zone_bps:g}"
        return f"{self.bar}-imb{imb}-stack{self.stacked}-z{zone}"

    @property
    def is_default(self) -> bool:
        return (self.bar, float(self.imbalance), int(self.stacked), float(self.zone_bps)) == (
            DEFAULT_CELL[0],
            float(DEFAULT_CELL[1]),
            int(DEFAULT_CELL[2]),
            float(DEFAULT_CELL[3]),
        )


def grid_cells() -> list[Cell]:
    cells = [
        Cell(bar, imb, stacked, zone)
        for bar in GRID_BARS
        for imb in GRID_IMBALANCE
        for stacked in GRID_STACKED
        for zone in GRID_ZONE_BPS
    ]
    return cells


def base_params(cell: Cell) -> SwingParams:
    return SwingParams(
        entry="trapped",
        trap_bar=cell.bar,
        trap_imbalance=float(cell.imbalance),
        trap_stacked=int(cell.stacked),
        trap_zone_bps=float(cell.zone_bps),
        flow="off",
    )


@dataclass(frozen=True)
class StopCell:
    """One pre-registered stop or slip variant of the default entry cell."""

    kind: str  # stop | slip
    anchor: str = "trap"
    min_bps: float = 0.0
    min_atr: float = 0.0
    slip_bps: float = 0.0
    slip_mode: str = "flat"

    @property
    def name(self) -> str:
        if self.kind == "slip":
            if self.slip_mode == "proportional":
                return "slip-proportional"
            return f"slip-flat{self.slip_bps:g}"
        return f"stop-{self.anchor}-x{self.min_bps:g}-k{self.min_atr:g}"


def stop_cells() -> list[StopCell]:
    cells = [
        StopCell("stop", anchor, bps, k)
        for anchor in GRID_STOP_ANCHOR
        for bps in GRID_MIN_STOP_BPS
        for k in GRID_MIN_STOP_ATR
    ]
    cells.append(StopCell("slip", slip_bps=GRID_SLIP_BPS[0], slip_mode="flat"))
    cells.append(StopCell("slip", slip_bps=GRID_SLIP_BPS[1], slip_mode="flat"))
    cells.append(StopCell("slip", slip_mode="proportional"))
    return cells


def params_for_stop(cell: StopCell) -> SwingParams:
    base = base_params(Cell(*DEFAULT_CELL))
    return replace(
        base,
        trap_stop_anchor=cell.anchor,
        trap_min_stop_bps=float(cell.min_bps),
        trap_min_stop_atr=float(cell.min_atr),
        trap_slip_bps=float(cell.slip_bps),
        trap_slip_mode=cell.slip_mode,
    )


def coin_dirname(coin: str) -> str:
    return canon_coin(coin).replace(":", "_")


def _parse_day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def tape_files(root: Path, coin: str, start: str, end: str) -> list[Path]:
    folder = root / coin_dirname(coin)
    if not folder.is_dir():
        alt = root / canon_coin(coin)
        folder = alt if alt.is_dir() else folder
    if not folder.is_dir():
        return []
    lo = _parse_day(start).date()
    hi = _parse_day(end).date()
    found: list[Path] = []
    for path in sorted(folder.iterdir()):
        name = path.name
        if ".trades.jsonl" not in name:
            continue
        day_text = name[:10]
        try:
            day = _parse_day(day_text).date()
        except ValueError:
            continue
        if lo <= day <= hi:
            found.append(path)
    return found


def load_coin(root: Path, coin: str, start: str, end: str) -> tuple[list[TapePrint], list[dict]]:
    prints: list[TapePrint] = []
    for path in tape_files(root, coin, start, end):
        prints.extend(read_trades(path))
    prints.sort(key=lambda item: item.ts)
    hourly = _hourly(root / coin_dirname(coin), prints)
    return prints, hourly


def minute_candles(folder: Path, prints: list[TapePrint]) -> list[dict]:
    """1m bars. ``candles_1m.jsonl`` wins a minute the tape also printed."""
    candles: dict[float, dict] = {}
    path = folder / "candles_1m.jsonl"
    if path.is_file():
        import json

        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    t = float(raw["t"])
                    if t > 1e11:
                        t = t / 1000.0
                    candles[t] = {
                        "t": t * 1000.0,
                        "o": float(raw["o"]),
                        "h": float(raw["h"]),
                        "l": float(raw["l"]),
                        "c": float(raw["c"]),
                        "v": float(raw.get("v") or 0),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
    if prints:
        bars = build_footprint(prints, bar_sec=60, tick=hl_tick(prints[0].price))
        for bar in bars:
            key = float(bar.t)
            if key in candles:
                continue
            candles[key] = {
                "t": key * 1000.0,
                "o": bar.o,
                "h": bar.h,
                "l": bar.l,
                "c": bar.c,
                "v": bar.buy + bar.sell,
            }
    return [candles[key] for key in sorted(candles)]


def _hourly(folder: Path, prints: list[TapePrint]) -> list[dict]:
    """1h bars from ``candles_1m.jsonl`` when it is there, filled by the tape."""
    one_min = minute_candles(folder, prints)
    if not one_min:
        return []
    end = max(bar_open_sec(bar["t"]) for bar in one_min) + 120.0
    return aggregate(one_min, 60, 3600, end)


@dataclass
class Prepared:
    coin: str
    prints: list[TapePrint]
    tick: float
    bars: list
    levels: list
    profiles_ok: int


def prepare_coin(coin: str, prints: list[TapePrint], hourly: list[dict], bar: str, params: SwingParams) -> Prepared:
    tick = hl_tick(prints[0].price) if prints else 1.0
    bar_sec = BAR_SEC[bar]
    bars = build_footprint(prints, bar_sec=bar_sec, tick=tick)
    levels = []
    profiles_ok = 0
    structural_cache: dict[int, list] = {}
    for index, bar_ in enumerate(bars):
        bucket = int(bar_.t // 14400)
        if bucket not in structural_cache:
            structural_cache[bucket] = build_levels(hourly, float(bar_.t), params) if hourly else []
        day = int(bar_.t // 86400) * 86400
        profile = session_volume_profile(prints, tick=tick, start=float(day), end=float(bar_.t))
        if profile.ok:
            profiles_ok += 1
        levels.append(merge_levels(structural_cache[bucket], profile))
        del index
    return Prepared(coin, prints, tick, bars, levels, profiles_ok)


@dataclass(frozen=True)
class Working:
    coin: str
    side: str
    signal_ts: float
    expire_ts: float
    entry: float
    stop: float
    take_profit: float
    size: float
    level: float
    sources: str
    absorption: float
    delta: float
    slope: float
    config: str
    budget: float = 0.0
    slip_bps: float | None = None


@dataclass
class OpenPos:
    work: Working
    opened: float


def scan_cell(prep: Prepared, params: SwingParams, equity: float, risk: float) -> tuple[list[tuple[float, Working]], Counter]:
    """Signals that pass every check, and how far the other pairs got."""
    fails: Counter = Counter()
    orders: list[tuple[float, Working]] = []
    # Size is applied later from the running equity. A placeholder size is replaced.
    for index in range(1, len(prep.bars)):
        planned, signal, reason = assess_pair(
            prep.coin,
            prep.bars,
            index,
            prep.levels[index - 1],
            params,
            prep.tick,
            equity,
            risk_pct=risk,
        )
        if planned is None or signal is None:
            fails[reason or "NO_TRAP"] += 1
            continue
        close_ts = float(signal.failure.t) + BAR_SEC[params.trap_bar]
        orders.append((close_ts, _working(prep.coin, planned, signal, close_ts, params, "")))
    return orders, fails


def _working(coin: str, planned, signal: TrapSignal, close_ts: float, params: SwingParams, config: str) -> Working:
    return Working(
        coin=canon_coin(coin),
        side=planned.side,
        signal_ts=float(close_ts),
        expire_ts=float(close_ts) + float(params.fill_hours) * 3600.0,
        entry=float(planned.entry),
        stop=float(planned.stop),
        take_profit=float(planned.take_profit),
        size=float(planned.size),
        level=float(planned.level),
        sources="|".join(planned.sources),
        absorption=float(signal.absorption),
        delta=float(signal.delta),
        slope=float(signal.cvd_slope),
        config=config,
    )


def _through_stop(side: str, price: float, stop: float) -> bool:
    if side == "long":
        return price <= stop + 1e-12
    return price >= stop - 1e-12


def _through_target(side: str, price: float, target: float) -> bool:
    if side == "long":
        return price >= target - 1e-12
    return price <= target + 1e-12


def _through_entry(side: str, price: float, entry: float) -> bool:
    if side == "long":
        return price <= entry + 1e-12
    return price >= entry - 1e-12


def simulate(
    orders: list[Working],
    prints: dict[str, list[TapePrint]],
    params: SwingParams,
    equity: float,
    risk: float,
) -> tuple[list[dict], dict]:
    """Rest the limits, then mark stops and targets. Returns trade rows and counts."""
    events: list[tuple] = []
    for order in orders:
        events.append((order.signal_ts, 0, "signal", order.coin, order))
    for coin, tape in prints.items():
        for print_ in tape:
            events.append((print_.ts, 1, "print", canon_coin(coin), print_))
    events.sort(key=lambda item: (item[0], item[1], item[3]))
    cash = float(equity)
    start = float(equity)
    working: dict[str, Working] = {}
    open_pos: dict[str, OpenPos] = {}
    rows: list[dict] = []
    counts = Counter()
    peak = cash
    max_dd = 0.0
    hold = float(params.hold_days) * 86400.0

    def mark_dd() -> None:
        nonlocal peak, max_dd
        peak = max(peak, cash)
        if peak > 0:
            max_dd = max(max_dd, (peak - cash) / peak)

    def close(pos: OpenPos, ts: float, price: float, reason: str) -> None:
        nonlocal cash
        work = pos.work
        fees = conservative_fees(work.coin)
        slip = work.slip_bps
        if slip is None:
            slip = reserved_slip_bps(work.coin, work.entry, work.stop, params)
        net = exit_net(
            side=work.side,
            size=work.size,
            entry=work.entry,
            exit_px=price,
            reason=reason,
            maker_fee=fees.maker,
            taker_fee=fees.taker,
            slip_bps=float(slip),
        )
        budget = float(work.budget)
        if budget <= 0:
            budget = work.size * abs(work.entry - work.stop)
        r_mult = net / budget if budget > 0 else 0.0
        stop_bps = (
            abs(work.entry - work.stop) / work.entry * 10_000.0 if work.entry > 0 else 0.0
        )
        cash += net
        mark_dd()
        counts[reason] += 1
        rows.append(
            {
                "config": work.config,
                "coin": work.coin,
                "side": work.side,
                "signal_time": _iso(work.signal_ts),
                "fill_time": _iso(pos.opened),
                "exit_time": _iso(ts),
                "entry": work.entry,
                "stop": work.stop,
                "exit": price,
                "reason": reason,
                "size": work.size,
                "net": net,
                "r": r_mult,
                "budget": budget,
                "slip_bps": float(slip),
                "stop_bps": stop_bps,
                "level": work.level,
                "sources": work.sources,
                "absorption": work.absorption,
                "failure_delta": work.delta,
                "cvd_slope": work.slope,
            }
        )

    for ts, _prio, kind, coin, payload in events:
        if kind == "signal":
            order: Working = payload
            if coin in working or coin in open_pos:
                counts["busy"] += 1
                continue
            # Resize from the equity at the signal, not the scan-time placeholder.
            sized = _resize(order, cash, risk, params)
            if sized is None:
                counts["SIZE_ZERO"] += 1
                continue
            working[coin] = sized
            counts["signaled"] += 1
            continue
        print_: TapePrint = payload
        if coin in working and print_.ts + 1e-9 >= working[coin].signal_ts:
            order = working[coin]
            if print_.ts > order.expire_ts + 1e-9:
                working.pop(coin, None)
                counts["expired"] += 1
            elif _through_target(order.side, print_.price, order.take_profit):
                working.pop(coin, None)
                counts["tp_before_fill"] += 1
            elif _through_stop(order.side, print_.price, order.stop):
                working.pop(coin, None)
                counts["stop_before_fill"] += 1
            elif _through_entry(order.side, print_.price, order.entry):
                working.pop(coin, None)
                open_pos[coin] = OpenPos(order, float(print_.ts))
                counts["filled"] += 1
            continue
        if coin in open_pos:
            pos = open_pos[coin]
            if _through_stop(pos.work.side, print_.price, pos.work.stop):
                open_pos.pop(coin, None)
                close(pos, print_.ts, pos.work.stop, "stop")
            elif _through_target(pos.work.side, print_.price, pos.work.take_profit):
                open_pos.pop(coin, None)
                close(pos, print_.ts, pos.work.take_profit, "tp")
            elif print_.ts + 1e-9 >= pos.opened + hold:
                open_pos.pop(coin, None)
                close(pos, print_.ts, print_.price, "max_hold")
    for coin, pos in list(open_pos.items()):
        last = prints.get(coin) or prints.get(canon_coin(coin)) or []
        px = last[-1].price if last else pos.work.entry
        ts = last[-1].ts if last else pos.opened
        open_pos.pop(coin, None)
        close(pos, ts, px, "tape_end")
    for coin in list(working):
        working.pop(coin, None)
        counts["expired"] += 1
    counts["net"] = cash - start
    counts["max_dd"] = max_dd
    return rows, counts


def _resize(order: Working, equity: float, risk: float, params: SwingParams) -> Working | None:
    slip = reserved_slip_bps(order.coin, order.entry, order.stop, params)
    try:
        size, loss = size_swing(
            order.coin,
            order.entry,
            order.stop,
            equity,
            params,
            risk_pct=risk,
            leverage=20,
            max_leverage=20,
            slip_bps=slip,
        )
    except ValueError:
        return None
    if size <= 0 or loss <= 0:
        return None
    return replace(order, size=float(size), budget=float(loss), slip_bps=float(slip))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _mean(rows: list[dict], key: str) -> float | None:
    if not rows:
        return None
    return sum(float(row[key]) for row in rows) / len(rows)


def expectancy_split(rows: list[dict]) -> dict:
    """Closed exits, end-of-tape marks, and the total that includes both.

    R on each row is net dollars / the risk budget reserved at the arm.
    """
    closed = [row for row in rows if row.get("reason") != "tape_end"]
    marks = [row for row in rows if row.get("reason") == "tape_end"]
    return {
        "n": len(rows),
        "n_closed": len(closed),
        "n_marks": len(marks),
        "e_closed": _mean(closed, "r"),
        "e_marks": _mean(marks, "r"),
        "e_with_marks": _mean(rows, "r"),
        "avg_budget": _mean(rows, "budget") if rows and "budget" in rows[0] else None,
        "avg_stop_bps": _mean(rows, "stop_bps") if rows and "stop_bps" in rows[0] else None,
    }


def run(
    root: Path,
    coins: list[str],
    start: str,
    end: str,
    *,
    equity: float = 5000.0,
    risk: float = 0.01,
    use_grid: bool = True,
) -> dict:
    loaded: dict[str, tuple[list[TapePrint], list[dict]]] = {}
    for coin in coins:
        loaded[canon_coin(coin)] = load_coin(root, coin, start, end)
    cells = grid_cells() if use_grid else [Cell(*DEFAULT_CELL)]
    by_bar: dict[str, dict[str, Prepared]] = {}
    # Structural params do not vary across the grid. Profiles neither.
    skeleton = SwingParams(entry="trapped", flow="off", min_score=3.0)
    for bar in {cell.bar for cell in cells}:
        by_bar[bar] = {}
        for coin, (prints, hourly) in loaded.items():
            by_bar[bar][coin] = prepare_coin(coin, prints, hourly, bar, skeleton)
    reports = []
    all_rows: list[dict] = []
    tape_map = {coin: loaded[coin][0] for coin in loaded}

    def _play(preps: dict[str, Prepared], params: SwingParams, name: str) -> dict:
        orders: list[Working] = []
        fails: Counter = Counter()
        for coin, prep in preps.items():
            found, cell_fails = scan_cell(prep, params, equity, risk)
            fails.update(cell_fails)
            for _ts, order in found:
                orders.append(replace(order, config=name))
        rows, counts = simulate(orders, tape_map, params, equity, risk)
        return {
            "fails": fails,
            "counts": counts,
            "rows": rows,
            "split": expectancy_split(rows),
            "net": float(counts.get("net", 0.0)),
            "max_dd": float(counts.get("max_dd", 0.0)),
        }

    for cell in cells:
        played = _play(by_bar[cell.bar], base_params(cell), cell.name)
        all_rows.extend(played["rows"])
        reports.append({"cell": cell, **played})
    # Stop and slip, default entry cell only. Not crossed with the other 80.
    default_bar = DEFAULT_CELL[0]
    if default_bar not in by_bar:
        by_bar[default_bar] = {
            coin: prepare_coin(coin, prints, hourly, default_bar, skeleton)
            for coin, (prints, hourly) in loaded.items()
        }
    stop_reports = []
    for cell in stop_cells():
        played = _play(by_bar[default_bar], params_for_stop(cell), cell.name)
        all_rows.extend(played["rows"])
        stop_reports.append({"cell": cell, **played})
    span = _span(loaded)
    return {
        "coins": [canon_coin(coin) for coin in coins],
        "start": start,
        "end": end,
        "loaded": {coin: len(prints) for coin, (prints, _h) in loaded.items()},
        "hourly": {coin: len(hourly) for coin, (_p, hourly) in loaded.items()},
        "bars": {
            bar: {coin: len(prep.bars) for coin, prep in prepared.items()}
            for bar, prepared in by_bar.items()
        },
        "profiles": {
            bar: {coin: prep.profiles_ok for coin, prep in prepared.items()}
            for bar, prepared in by_bar.items()
        },
        "span": span,
        "reports": reports,
        "stop_reports": stop_reports,
        "rows": all_rows,
        "equity": equity,
        "risk": risk,
    }


def _span(loaded: dict) -> dict:
    out = {}
    for coin, (prints, _hourly) in loaded.items():
        if not prints:
            out[coin] = None
            continue
        out[coin] = {
            "from": _iso(prints[0].ts),
            "to": _iso(prints[-1].ts),
            "hours": (prints[-1].ts - prints[0].ts) / 3600.0,
        }
    return out


def _csv_value(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return value


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in COLUMNS})


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _result_row(name: str, report: dict) -> str:
    counts = report["counts"]
    split = report["split"]
    budget = "—" if split["avg_budget"] is None else f"{split['avg_budget']:.2f}"
    return (
        f"| {name} | {counts.get('signaled', 0)} | {counts.get('filled', 0)} | "
        f"{split['n_closed']} | {split['n_marks']} | {_fmt(split['e_closed'])} | "
        f"{_fmt(split['e_marks'])} | {_fmt(split['e_with_marks'])} | {budget} | "
        f"{report['net']:.2f} | {report['max_dd']:.2%} | "
        f"{'yes' if split['n'] < 20 else 'no'} |"
    )


def render_markdown(result: dict) -> str:
    lines = [
        "# Trapped sellers / trapped buyers",
        "",
        "Pipeline check on the tape that was passed in. A couple of hours of prints cannot say whether this entry has an edge. The entry grid was fixed before the run (bar 1m/3m/5m, imbalance 2.5/3/4, stacked 2/3/4, zone 10/20/40 bps). The default cell is 1m, 3:1, stacked 3, zone 20 bps. The stop and slip grid below is also fixed, and it runs only on that default cell. `MODEL_B_SWING_ENTRY` stays `sweep` unless it is set to `trapped`. Nothing here is deployed.",
        "",
        f"Window `{result['start']}` → `{result['end']}`. Equity {result['equity']:g}, risk {result['risk']:.2%}. R is net dollars divided by the risk budget reserved at the arm (stop distance, the slip that sizing used, and fees). It is not the raw stop distance. A `tape_end` row is a mark of a position still open when the file ends. Closed expectancy leaves those marks out. The with-marks total includes them. Funding is not in these files, so it is not charged. A stop's dollar loss stays inside the 2% cap.",
        "",
        "## Tape read",
        "",
        "| coin | prints | 1h bars | span |",
        "| --- | ---: | ---: | --- |",
    ]
    for coin in result["coins"]:
        span = result["span"].get(coin)
        span_text = "no file" if not span else f"{span['from']} → {span['to']} ({span['hours']:.2f}h)"
        lines.append(
            f"| {coin} | {result['loaded'].get(coin, 0)} | {result['hourly'].get(coin, 0)} | {span_text} |"
        )
    lines.append("")
    lines.append("Footprint bars and how many of them had a session profile (VAL/VAH/POC) from prints before the bar:")
    lines.append("")
    lines.append("| bar | " + " | ".join(result["coins"]) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in result["coins"]) + " |")
    for bar in GRID_BARS:
        if bar not in result["bars"]:
            continue
        cells = []
        for coin in result["coins"]:
            n = result["bars"][bar].get(coin, 0)
            prof = result["profiles"].get(bar, {}).get(coin, 0)
            cells.append(f"{n} bars, {prof} profiles")
        lines.append(f"| {bar} | " + " | ".join(cells) + " |")
    lines.extend(
        [
            "",
            "## Grid",
            "",
            "The same fill can show up in more than one cell. That is one event counted again, not a new trade. The entry grid uses the wick stop and the coin slip (25 bps main, 30 bps xyz).",
            "",
        ]
    )
    header = (
        "| config | signals | fills | closed | marks | E closed | E marks | E with marks | "
        "avg budget $ | net | max DD | under 20 |"
    )
    align = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
    lines.append(header)
    lines.append(align)
    for report in result["reports"]:
        lines.append(_result_row(report["cell"].name, report))
    default = next((item for item in result["reports"] if item["cell"].is_default), None)
    lines.extend(["", "## Default cell", ""])
    if default is None:
        lines.append("The default cell was not in this run.")
    else:
        fails = default["fails"]
        parts = ", ".join(f"{key} {fails[key]}" for key in sorted(fails)) or "none"
        lines.append(
            f"`{default['cell'].name}`. Pairs that did not become a signal: {parts}."
        )
        lines.append(
            "Cancels: "
            f"target before fill {default['counts'].get('tp_before_fill', 0)}, "
            f"stop before fill {default['counts'].get('stop_before_fill', 0)}, "
            f"expired {default['counts'].get('expired', 0)}, "
            f"busy {default['counts'].get('busy', 0)}."
        )
        if not default["rows"]:
            lines.append("No fill on this cell.")
    lines.extend(
        [
            "",
            "## Stop and slip",
            "",
            "Fixed before the run, and only on the default entry cell (`1m-imb3-stack3-z20`). Anchor `trap` is beyond the trap-bar extreme. Anchor `zone` is beyond the support or resistance. The min distance is max(X bps, k×ATR(14) of the footprint bars up to the trap bar), with X 20/30/40 and k 0.5/1.0. Those twelve cells keep the coin slip. The slip rows keep the wick stop and no min floor: flat 10 bps, flat 15 bps, and proportional, which reserves min(the coin 25/30 allowance, the stop distance in bps). `ref` is the default cell above. Avg budget is the mean dollars reserved per fill. A wider stop or a smaller slip changes how much of that budget is the stop versus unused slip, and therefore the R of the same price path. This is not crossed with the 81 entry cells. Seeing the table does not add a grid point.",
            "",
            header,
            align,
        ]
    )
    if default is not None:
        lines.append(_result_row("ref-trap-slip25/30", default))
    for report in result.get("stop_reports") or []:
        lines.append(_result_row(report["cell"].name, report))
    lines.extend(
        [
            "",
            "## Read this as a pipeline check",
            "",
            "The same cell would have to be judged on both the 15m and the 1h books, in both halves, on at least 4 of 6 coins, with at least 20 trades, before the paper flag would turn on. This file does not meet that bar. A stop or slip chosen on a few hours of one session would be fit to that session. Monday paper stays on the sweep entry, with the wick stop and the 25/30 bp allowance.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest trapped sellers/buyers on recorded tape.")
    parser.add_argument("--tape", required=True, help="Root directory of <coin>/<YYYY-MM-DD>.trades.jsonl.gz")
    parser.add_argument("--from", dest="start", required=True, help="UTC day YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="UTC day YYYY-MM-DD")
    parser.add_argument(
        "--coins",
        default="BTC,ETH,SOL,xyz:GOLD,xyz:SP500,xyz:XYZ100",
        help="Comma-separated coins. Directory names use _ for :",
    )
    parser.add_argument("--out", default="docs/pnl/trapped_sample", help="Directory for the CSV and markdown")
    parser.add_argument("--equity", type=float, default=5000.0)
    parser.add_argument("--risk", type=float, default=0.01)
    parser.add_argument(
        "--no-grid",
        action="store_true",
        help="Skip the other 80 entry cells. The default cell and the stop/slip study still run.",
    )
    args = parser.parse_args(argv)
    coins = [part.strip() for part in str(args.coins).split(",") if part.strip()]
    result = run(
        Path(args.tape),
        coins,
        args.start,
        args.end,
        equity=float(args.equity),
        risk=float(args.risk),
        use_grid=not args.no_grid,
    )
    out = Path(args.out)
    write_csv(out / "trapped_trades.csv", result["rows"])
    text = render_markdown(result)
    (out / "trapped_summary.md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

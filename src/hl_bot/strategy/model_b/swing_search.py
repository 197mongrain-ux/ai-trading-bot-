"""Pre-registered 90-day search for a swing setup. Research only.

Candles only. Flow is off: there is no aggressor tape, so absorption, print
count, and delta are not in any cell. Fees and the 25/30 bp slip are on.
Funding is not. Nothing here changes the paper defaults.

The grid, the 60/30 split, the half split, the 30-trade floor, and the
multiple-comparison penalty are fixed in this file. Do not add a cell or
move a threshold after the holdout is seen.

15-minute candles in the public cache cover about 52 days, which cannot
fill a 60-day tune plus a 30-day holdout. Those cells are registered and
not run. The penalty uses the number of cells that were run.

Run: ``python -m hl_bot.strategy.model_b.swing_search --cache /tmp/hl_swing_cache``
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from itertools import product
from pathlib import Path

from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.swing import (
    Level,
    SwingParams,
    atr14_of,
    build_levels,
    closed_candles,
    exit_net,
    find_sweep,
    overnight_decision,
    pick_targets,
    range_edges,
    read_swing_macro,
    session_close_id,
    session_flat_due,
    session_open,
    size_swing,
    slip_bps_for,
    tighten_to_breakeven,
)
from hl_bot.strategy.model_b.swing_replay import COINS

# --- locked before the run ---
WINDOW_DAYS = 90.0
TUNE_DAYS = 60.0
HOLDOUT_DAYS = 30.0
HALF_DAYS = 45.0
MIN_TRADES = 30
TUNE_MIN = 15
PENALTY_COEF = 0.05
HOLD_DAYS = 3.0
RISK = 0.01
EQUITY0 = 10_000.0
SESSION = "09:30-16:00"
MIN_SWEEP_BPS = 5.0
TOP_N = 5

ENTRIES = ("sweep", "touch")
LEVELS = ("4h", "session", "daily")
CONFIRMS = ("15m", "1h")
MACROS = ("4h+1d", "4h", "none")
STOPS = (0.5, 1.0, 1.5)
TARGETS = ("1.5", "2", "3", "level")
SESSIONS = ("overnight", "be", "flat")
BOOKS = ("all", "crypto")
CRYPTO = ("BTC", "ETH", "SOL")

_LEVEL_PARAMS = {
    "4h": SwingParams(level_set="4h", major=True, min_touches=2, min_score=3.0, top_n=2, flow="off"),
    "session": SwingParams(level_set="session", major=False, min_score=3.0, top_n=2, flow="off"),
    "daily": SwingParams(level_set="1d", major=False, min_score=1.0, top_n=2, flow="off"),
}
_MACRO_BOTH = SwingParams(macro_tfs=("4h", "1d"), macro_mode="both", flow="off")
_MACRO_4H = SwingParams(macro_tfs=("4h",), macro_mode="both", flow="off")
_SIZE = SwingParams(max_lev=10, flow="off")
_SESSION_PARAMS = {
    "overnight": SwingParams(session=SESSION, hold_overnight="always", hold_days=HOLD_DAYS),
    "be": SwingParams(session=SESSION, hold_overnight="be", hold_days=HOLD_DAYS),
    "flat": SwingParams(session=SESSION, hold_overnight="off", hold_days=HOLD_DAYS),
}


@dataclass(frozen=True)
class Cell:
    entry: str
    level: str
    confirm: str
    macro: str
    stop_k: float
    target: str
    session: str
    book: str
    eligible: bool

    @property
    def name(self) -> str:
        return (
            f"{self.entry}/{self.level}/{self.confirm}/{self.macro}/"
            f"k{self.stop_k:g}/tp{self.target}/{self.session}/{self.book}"
        )


@dataclass(frozen=True)
class Signal:
    ts: float
    coin: str
    side: str
    entry: float
    atr: float
    score: float
    opposites: tuple[tuple[str, float], ...]


@dataclass
class Trade:
    coin: str
    side: str
    entry: float
    exit: float
    stop: float
    size: float
    net: float
    r: float
    reason: str
    opened: float
    closed: float


def penalty(n_cells: int, coef: float = PENALTY_COEF) -> float:
    """Holdout expectancy must clear this many R.

    ``coef * sqrt(2 * ln N)`` with N the number of cells that were run.
    A single cell has no penalty.
    """
    if n_cells < 2:
        return 0.0
    return float(coef) * math.sqrt(2.0 * math.log(float(n_cells)))


def survives(
    trades: int,
    holdout_e: float | None,
    first_half_e: float | None,
    second_half_e: float | None,
    pen: float,
) -> tuple[bool, bool]:
    """Raw bar, then the same bar after the penalty.

    Raw: at least 30 trades, holdout expectancy above zero, and both
    calendar halves above zero. The penalty subtracts from the holdout
    expectancy. A missing slice is not positive.
    """
    raw = (
        int(trades) >= MIN_TRADES
        and holdout_e is not None
        and float(holdout_e) > 0.0
        and first_half_e is not None
        and float(first_half_e) > 0.0
        and second_half_e is not None
        and float(second_half_e) > 0.0
    )
    cleared = raw and float(holdout_e) > float(pen)
    return raw, cleared


def iter_cells(span_15m_days: float) -> list[Cell]:
    """Full registered grid. 15m is eligible only when its span covers 90 days."""
    cells: list[Cell] = []
    for entry, level, confirm, macro, stop_k, target, session, book in product(
        ENTRIES, LEVELS, CONFIRMS, MACROS, STOPS, TARGETS, SESSIONS, BOOKS
    ):
        eligible = confirm != "15m" or float(span_15m_days) + 1e-9 >= WINDOW_DAYS
        cells.append(
            Cell(entry, level, confirm, macro, float(stop_k), target, session, book, eligible)
        )
    return cells


def stop_beyond_level(side: str, level: float, atr: float, k: float) -> float | None:
    """Stop k ATR beyond the level, on the loss side of the entry."""
    buffer = float(k) * float(atr)
    if buffer <= 0 or float(level) <= 0:
        return None
    if side == "long":
        stop = float(level) - buffer
        return stop if stop < float(level) else None
    stop = float(level) + buffer
    return stop if stop > float(level) else None


def fixed_target(side: str, entry: float, stop: float, r_multiple: float) -> float | None:
    dist = abs(float(entry) - float(stop))
    if dist <= 0 or float(r_multiple) <= 0:
        return None
    if side == "long":
        return float(entry) + float(r_multiple) * dist
    return float(entry) - float(r_multiple) * dist


def touch_reclaim(candle: dict, level: float, side: str) -> bool:
    """Same-bar touch of the level and a close back through it.

    No minimum depth. A two-bar sweep that does not touch on the reclaim
    bar is not a touch. Sweep still requires 5 bps and may use that second bar.
    """
    price = float(level)
    if price <= 0:
        return False
    if side == "long":
        return float(candle["l"]) <= price and float(candle["c"]) > price
    return float(candle["h"]) >= price and float(candle["c"]) < price


def _expectancy(trades: list[Trade]) -> float | None:
    if not trades:
        return None
    return sum(trade.r for trade in trades) / len(trades)


def _avg(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    return sum(trade.r for trade in trades) / len(trades)


def _stats(trades: list[Trade], equity0: float, equity1: float, max_dd: float) -> dict:
    wins = [trade for trade in trades if trade.net > 0]
    losses = [trade for trade in trades if trade.net <= 0]
    n = len(trades)
    return {
        "trades": n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "avg_win_r": _avg(wins),
        "avg_loss_r": _avg(losses),
        "expectancy_r": _expectancy(trades) or 0.0,
        "net_pct": ((equity1 / equity0) - 1.0) * 100.0 if equity0 else 0.0,
        "max_dd_pct": max_dd * 100.0,
    }


def _pnl(side: str, size: float, entry: float, exit_px: float, reason: str, maker: float, taker: float, slip: float) -> float:
    """Target is maker. Stop, time stop, session flatten, and data end are taker plus slip."""
    booked = "tp" if reason == "tp" else "max_hold"
    return exit_net(
        side=side,
        size=size,
        entry=entry,
        exit_px=exit_px,
        reason=booked,
        maker_fee=maker,
        taker_fee=taker,
        slip_bps=slip,
    )


def _bracket(sig: Signal, stop_k: float, target: str, equity: float, maker: float, taker: float) -> tuple | None:
    stop = stop_beyond_level(sig.side, sig.entry, sig.atr, stop_k)
    if stop is None:
        return None
    if target == "level":
        levels = [Level(price, kind, 1.0, 1, (), 0.0) for kind, price in sig.opposites]
        picked = pick_targets(sig.side, sig.entry, stop, levels, 1.0, 3.0, False)
        if isinstance(picked, str):
            return None
        tp, r_multiple, _runner = picked
    else:
        r_multiple = float(target)
        tp = fixed_target(sig.side, sig.entry, stop, r_multiple)
        if tp is None:
            return None
    try:
        size, _loss = size_swing(
            sig.coin,
            sig.entry,
            stop,
            equity,
            _SIZE,
            risk_pct=RISK,
            leverage=20,
            maker_fee=maker,
            taker_fee=taker,
            max_leverage=20,
        )
    except ValueError:
        return None
    if size <= 0:
        return None
    return stop, float(tp), float(r_multiple), float(size)


def simulate(
    signals: list[Signal],
    bars: dict[str, list[tuple[float, float, float, float]]],
    *,
    stop_k: float,
    target: str,
    session: str,
    equity0: float = EQUITY0,
    hold_days: float = HOLD_DAYS,
) -> tuple[list[Trade], float, float]:
    """One book. Returns trades, final equity, and peak-to-trough drawdown.

    One position. At a timestamp the highest score is armed if the book is
    flat. A working order is not replaced. It is cancelled at the session
    end, after 24 hours, or if the target trades before the limit. The fill
    is on a later bar. A position still open on the last bar is flattened
    there as ``data_end``.
    """
    params = _SESSION_PARAMS[session]
    index = {coin: [row[0] for row in rows] for coin, rows in bars.items()}
    fees = {coin: conservative_fees(coin) for coin in bars}
    slips = {coin: slip_bps_for(coin, _SIZE) for coin in bars}
    ordered = sorted(signals, key=lambda sig: (sig.ts, -sig.score, sig.coin))
    trades: list[Trade] = []
    eq = float(equity0)
    peak = eq
    max_dd = 0.0
    cursor = 0
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
        group.sort(key=lambda sig: (sig.score, sig.coin), reverse=True)
        armed = None
        for sig in group:
            if sig.coin not in bars:
                continue
            if not session_open(sig.ts, SESSION):
                continue
            fee = fees[sig.coin]
            bracket = _bracket(sig, stop_k, target, eq, fee.maker, fee.taker)
            if bracket is None:
                continue
            armed = (sig, bracket)
            break
        if armed is None or eq <= 0:
            continue
        sig, (stop0, tp, _r_mult, size) = armed
        rows = bars[sig.coin]
        starts = index[sig.coin]
        # First bar that closes after the signal bar.
        lo = 0
        hi = len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= sig.ts:
                lo = mid + 1
            else:
                hi = mid
        bar_i = lo
        fee = fees[sig.coin]
        slip = slips[sig.coin]
        mode = "working"
        expire = sig.ts + 24.0 * 3600.0
        stop = stop0
        opened = 0.0
        decided = ""
        while bar_i < len(rows) and mode != "flat":
            now, high, low, close = rows[bar_i]
            bar_i += 1
            # Signals that printed while this ticket was working are skipped.
            while cursor < n and ordered[cursor].ts <= now:
                cursor += 1
            if mode == "working":
                if now > expire or session_flat_due(now, SESSION):
                    mode = "flat"
                    break
                if sig.side == "long":
                    filled = low <= sig.entry + 1e-9
                    hit_stop = low <= stop + 1e-9
                    hit_tp = high >= tp - 1e-9
                    reached_tp_only = hit_tp and not filled
                else:
                    filled = high >= sig.entry - 1e-9
                    hit_stop = high >= stop - 1e-9
                    hit_tp = low <= tp + 1e-9
                    reached_tp_only = hit_tp and not filled
                if reached_tp_only:
                    mode = "flat"
                    break
                if not filled:
                    continue
                if hit_stop:
                    _close(trades, sig, stop, stop0, size, "stop", now, now, fee, slip, eq_box := [eq])
                    eq = eq_box[0]
                    mode = "flat"
                    _mark(0.0)
                    break
                if hit_tp:
                    _close(trades, sig, tp, stop0, size, "tp", now, now, fee, slip, eq_box := [eq])
                    eq = eq_box[0]
                    mode = "flat"
                    _mark(0.0)
                    break
                mode = "open"
                opened = now
                _mark(_pnl(sig.side, size, sig.entry, close, "max_hold", fee.maker, fee.taker, slip))
                continue
            action = overnight_decision(
                now,
                params,
                side=sig.side,
                entry=sig.entry,
                risk_stop=stop0,
                price=close,
                decided=decided,
            )
            if sig.side == "long":
                hit_stop = low <= stop + 1e-9
                hit_tp = high >= tp - 1e-9
            else:
                hit_stop = high >= stop - 1e-9
                hit_tp = low <= tp + 1e-9
            if hit_stop:
                _close(trades, sig, stop, stop0, size, "stop", opened, now, fee, slip, eq_box := [eq])
                eq = eq_box[0]
                mode = "flat"
            elif action == "flat":
                _close(trades, sig, close, stop0, size, "SESSION_CLOSE", opened, now, fee, slip, eq_box := [eq])
                eq = eq_box[0]
                mode = "flat"
            elif hit_tp:
                _close(trades, sig, tp, stop0, size, "tp", opened, now, fee, slip, eq_box := [eq])
                eq = eq_box[0]
                mode = "flat"
            elif now >= opened + float(hold_days) * 86400.0:
                _close(trades, sig, close, stop0, size, "max_hold", opened, now, fee, slip, eq_box := [eq])
                eq = eq_box[0]
                mode = "flat"
            elif action == "be":
                stop = tighten_to_breakeven(sig.side, sig.entry, stop)
                decided = session_close_id(now, SESSION) or decided
            if mode == "open":
                _mark(_pnl(sig.side, size, sig.entry, close, "max_hold", fee.maker, fee.taker, slip))
            else:
                _mark(0.0)
        if mode == "open" and rows:
            now, _high, _low, close = rows[-1]
            _close(trades, sig, close, stop0, size, "data_end", opened, now, fee, slip, eq_box := [eq])
            eq = eq_box[0]
            _mark(0.0)
    return trades, eq, max_dd


def _close(trades, sig: Signal, exit_px: float, stop0: float, size: float, reason: str, opened: float, closed: float, fee, slip: float, eq_box: list) -> None:
    net = _pnl(sig.side, size, sig.entry, exit_px, reason, fee.maker, fee.taker, slip)
    risk = size * abs(sig.entry - stop0)
    trades.append(
        Trade(
            sig.coin,
            sig.side,
            sig.entry,
            float(exit_px),
            stop0,
            size,
            net,
            (net / risk) if risk > 0 else 0.0,
            reason,
            opened,
            closed,
        )
    )
    eq_box[0] = eq_box[0] + net


def _between(trades: list[Trade], start: float, end: float, *, last: bool) -> list[Trade]:
    if last:
        return [trade for trade in trades if start <= trade.opened <= end + 1e-6]
    return [trade for trade in trades if start <= trade.opened < end]


def extract_signals(
    data: dict[str, list[dict]],
    *,
    since: float,
    end: float,
) -> dict[tuple[str, str, str], list[Signal]]:
    """Signals inside the session, keyed by entry, level source, and macro."""
    out: dict[tuple[str, str, str], list[Signal]] = {
        key: [] for key in product(ENTRIES, LEVELS, MACROS)
    }
    for coin, hourly in data.items():
        for idx in range(20, len(hourly)):
            now = float(hourly[idx]["t"]) / 1000.0 + 3600.0
            if now < since:
                continue
            if now > end:
                break
            if not session_open(now, SESSION):
                continue
            closed = closed_candles(hourly[max(0, idx - 3) : idx + 1], 3600, now)
            if len(closed) < 2:
                continue
            atr = atr14_of(hourly[max(0, idx - 20) : idx + 1])
            if atr is None or atr <= 0:
                continue
            last = closed[-1]
            both = read_swing_macro(hourly, now, _MACRO_BOTH)
            four = read_swing_macro(hourly, now, _MACRO_4H)
            macros: list[tuple[str, object]] = [("4h+1d", both), ("4h", four), ("none", None)]
            for source in LEVELS:
                levels = build_levels(hourly, now, _LEVEL_PARAMS[source])
                if not levels:
                    continue
                for macro_name, snap in macros:
                    use = levels
                    allowed = {"long", "short"}
                    if snap is not None:
                        state = snap.macro  # type: ignore[attr-defined]
                        if state == "unknown":
                            continue
                        if state == "up":
                            allowed = {"long"}
                        elif state == "down":
                            allowed = {"short"}
                        elif state == "range":
                            use = range_edges(levels, hourly, now)
                    if not use:
                        continue
                    opposites = tuple((level.kind, float(level.price)) for level in use)
                    for level in use:
                        side = "long" if level.kind == "support" else "short"
                        if side not in allowed:
                            continue
                        if touch_reclaim(last, level.price, side):
                            out[( "touch", source, macro_name)].append(
                                Signal(now, coin, side, float(level.price), float(atr), float(level.score), opposites)
                            )
                        if find_sweep(closed, level.price, side, MIN_SWEEP_BPS) is not None:
                            out[("sweep", source, macro_name)].append(
                                Signal(now, coin, side, float(level.price), float(atr), float(level.score), opposites)
                            )
    return out


def _span_days(path: Path) -> float | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    if len(raw) < 2:
        return None
    return (float(raw[-1]["t"]) - float(raw[0]["t"])) / 86_400_000.0


def load_hourly(cache: Path) -> dict[str, list[dict]]:
    book: dict[str, list[dict]] = {}
    for coin in COINS:
        file = cache / f"{coin.replace(':', '_')}_1h.json"
        if not file.exists():
            continue
        rows = []
        for bar in json.loads(file.read_text()):
            rows.append(
                {
                    "t": float(bar["t"]),
                    "o": float(bar["o"]),
                    "h": float(bar["h"]),
                    "l": float(bar["l"]),
                    "c": float(bar["c"]),
                    "v": float(bar.get("v") or 0),
                }
            )
        rows.sort(key=lambda bar: bar["t"])
        if rows:
            book[coin] = rows
    return book


def _bars_for(data: dict[str, list[dict]]) -> dict[str, list[tuple[float, float, float, float]]]:
    out = {}
    for coin, rows in data.items():
        out[coin] = [
            (float(bar["t"]) / 1000.0 + 3600.0, float(bar["h"]), float(bar["l"]), float(bar["c"]))
            for bar in rows
        ]
    return out


def _cell_row(cell: Cell, stats: dict, parts: dict, per_coin: dict) -> dict:
    return {
        "name": cell.name,
        "entry": cell.entry,
        "level": cell.level,
        "confirm": cell.confirm,
        "macro": cell.macro,
        "stop_k": cell.stop_k,
        "target": cell.target,
        "session": cell.session,
        "book": cell.book,
        **stats,
        **parts,
        "per_coin": per_coin,
    }


def _fmt_e(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def run_search(data: dict[str, list[dict]], *, span_15m: float) -> dict:
    end = 0.0
    for rows in data.values():
        if rows:
            end = max(end, float(rows[-1]["t"]) / 1000.0 + 3600.0)
    since = end - WINDOW_DAYS * 86400.0
    tune_end = since + TUNE_DAYS * 86400.0
    mid = since + HALF_DAYS * 86400.0
    cells = iter_cells(span_15m)
    tried = [cell for cell in cells if cell.eligible and cell.confirm == "1h"]
    pen = penalty(len(tried))
    print(f"extracting signals {since:.0f}..{end:.0f}", flush=True)
    signals = extract_signals(data, since=since, end=end)
    bars = _bars_for(data)
    books = {"all": set(COINS), "crypto": set(CRYPTO)}
    rows = []
    n_sig = sum(len(v) for v in signals.values())
    print(f"signals {n_sig} cells {len(tried)}", flush=True)
    for i, cell in enumerate(tried):
        pool = [
            sig
            for sig in signals[(cell.entry, cell.level, cell.macro)]
            if sig.coin in books[cell.book]
        ]
        trades, equity, max_dd = simulate(
            pool, bars, stop_k=cell.stop_k, target=cell.target, session=cell.session
        )
        in_window = [trade for trade in trades if since <= trade.opened <= end + 1e-6]
        tune = _between(in_window, since, tune_end, last=False)
        hold = _between(in_window, tune_end, end, last=True)
        first = _between(in_window, since, mid, last=False)
        second = _between(in_window, mid, end, last=True)
        equity1 = EQUITY0 + sum(trade.net for trade in in_window)
        stats = _stats(in_window, EQUITY0, equity1, max_dd)
        # Equity above is the whole sim, including any trade opened inside the window.
        hold_e = _expectancy(hold)
        h1 = _expectancy(first)
        h2 = _expectancy(second)
        tune_e = _expectancy(tune)
        raw, cleared = survives(len(in_window), hold_e, h1, h2, pen)
        per = {}
        for coin in COINS:
            coin_trades = [trade for trade in in_window if trade.coin == coin]
            per[coin] = {
                "trades": len(coin_trades),
                "expectancy_r": _expectancy(coin_trades),
                "net": sum(trade.net for trade in coin_trades),
            }
        exits: dict[str, int] = {}
        for trade in in_window:
            exits[trade.reason] = exits.get(trade.reason, 0) + 1
        rows.append(
            {
                **_cell_row(
                    cell,
                    stats,
                    {
                        "tune_trades": len(tune),
                        "tune_e": tune_e,
                        "holdout_trades": len(hold),
                        "holdout_e": hold_e,
                        "half1_trades": len(first),
                        "half1_e": h1,
                        "half2_trades": len(second),
                        "half2_e": h2,
                        "raw_pass": raw,
                        "penalty_pass": cleared,
                        "exits": exits,
                    },
                    per,
                )
            }
        )
        if (i + 1) % 100 == 0:
            print(f"simulated {i + 1}/{len(tried)}", flush=True)
    ranked = [row for row in rows if row["trades"] >= MIN_TRADES and row["holdout_trades"] >= 1]
    ranked.sort(key=lambda row: (float(row["holdout_e"]), row["trades"], row["name"]), reverse=True)
    tune_pool = [row for row in rows if row["trades"] >= MIN_TRADES and row["tune_trades"] >= TUNE_MIN and row["tune_e"] is not None]
    tune_pool.sort(key=lambda row: (float(row["tune_e"]), row["tune_trades"], row["name"]), reverse=True)
    return {
        "since": since,
        "end": end,
        "registered": len(cells),
        "tried": len(tried),
        "skipped_15m": sum(1 for cell in cells if not cell.eligible),
        "span_15m": span_15m,
        "penalty": pen,
        "signals": n_sig,
        "raw_pass": sum(1 for row in rows if row["raw_pass"]),
        "penalty_pass": sum(1 for row in rows if row["penalty_pass"]),
        "top": ranked[:TOP_N],
        "walk_forward": tune_pool[0] if tune_pool else None,
        "rows": rows,
    }


def _coin_line(per: dict) -> str:
    parts = []
    for coin, row in per.items():
        if not row["trades"]:
            continue
        exp = row["expectancy_r"]
        parts.append(f"{coin} {row['trades']}/{_fmt_e(exp)}R")
    return ", ".join(parts) if parts else "none"


def _exit_line(exits: dict) -> str:
    order = ("stop", "tp", "SESSION_CLOSE", "max_hold", "data_end")
    names = [name for name in order if exits.get(name)]
    names.extend(name for name in sorted(exits) if name not in order)
    if not names:
        return "none"
    return ", ".join(f"{name} {exits[name]}" for name in names)


def render(result: dict) -> str:
    pen = float(result["penalty"])
    lines = [
        "# Swing search",
        "",
        "Paper research only. This does not change Monday's paper book.",
        "",
        "## Limit",
        "",
        "Candles only. Flow is off. There is no historical aggressor tape, so absorption, print count, and delta are not in any cell. Costs are maker/taker fees and the 25 bp / 30 bp slip. Funding is not applied. A target is a maker fill. A stop, a 3-day max hold, a 16:00 flatten, and a position still open on the last bar pay taker plus slip.",
        "",
        f"The 1h cache covers the last {WINDOW_DAYS:.0f} days. The 15m cache spans {float(result['span_15m']):.1f} days, which cannot fill a {TUNE_DAYS:.0f}-day tune and a {HOLDOUT_DAYS:.0f}-day holdout. Those cells were registered and not run.",
        "",
        "## Rules, fixed before the run",
        "",
        "Grid: entry sweep or same-bar touch-reclaim; level source 4h pivots with at least two touches, prior-day/week high or low, or a daily fractal; confirm 15m or 1h; macro 4h+daily both, 4h only, or none; stop beyond the level by 0.5, 1.0, or 1.5 times the 1h ATR(14); target a fixed 1.5R, 2R, or 3R, or the next opposite level inside 1R to 3R; session hold overnight, hold only if the 16:00 close is at least +1R and then stop at entry, or flatten at 16:00; coins all six or BTC/ETH/SOL.",
        "",
        "Touch-reclaim is a same-bar low through support and a close back above it, or the short mirror. It has no 5 bp minimum. Sweep is the existing rule: at least 5 bp through the level, same bar or the next bar's close. Entries are only 09:30–16:00 America/Toronto, Monday to Friday. The fill is on a later bar. One position, the higher level score. Unfilled orders cancel at 16:00. Fixed risk 1%, swing notional cap 10x, loss cap 2%, equity 10,000. The 3-day max hold stays. A 4h level uses the paper score floor of 3. A prior-day or prior-week print scores 3 with its bonus. A daily fractal is kept from score 1 so a single daily swing is eligible. Top two levels per side.",
        "",
        "A trade belongs to the window that contains its fill. Tune is the first 60 days. Holdout is the last 30. The two halves split the 90 days at 45. The walk-forward pick is the highest tune expectancy among cells with at least 30 trades overall and at least 15 trades in the tune window. The top 5 below are ranked by holdout expectancy among cells with at least 30 trades and at least one holdout trade. That ranking looks at the test window. The walk-forward pick is the cell the tune window would have chosen.",
        "",
        f"A cell passes the raw bar when it has at least {MIN_TRADES} trades, holdout expectancy above zero, and both halves above zero. The penalty is {PENALTY_COEF:g} * sqrt(2 * ln N), where N is the number of cells run. It is {pen:.3f} R. A cell survives the penalty only when it passes the raw bar and the holdout expectancy is above that number.",
        "",
        "## Result",
        "",
        f"- Cells registered: {result['registered']}",
        f"- Cells run: {result['tried']}",
        f"- Cells not run (15m history {float(result['span_15m']):.1f} days): {result['skipped_15m']}",
        f"- Signals found: {result['signals']}",
        f"- Cells passing the raw bar: {result['raw_pass']}",
        f"- Cells surviving the penalty: {result['penalty_pass']}",
        "",
    ]
    if result["penalty_pass"] == 0:
        lines.append("No setup survives. None of the cells that were run cleared the raw bar and the multiple-comparison penalty.")
        lines.append("")
    else:
        lines.append(f"{result['penalty_pass']} cell(s) survive the penalty. They are still one candle study with flow off.")
        lines.append("")
    both = [
        row for row in result["rows"]
        if row["half1_e"] is not None and row["half1_e"] > 0
        and row["half2_e"] is not None and row["half2_e"] > 0
        and row["holdout_e"] is not None and row["holdout_e"] > 0
    ]
    full_pos = [row for row in result["rows"] if row["expectancy_r"] > 0]
    if both:
        biggest = max(row["trades"] for row in both)
        lines.append(
            f"{len(both)} cells were positive in both halves and in the holdout. "
            f"The largest of those has {biggest} trades, under the {MIN_TRADES}-trade floor."
        )
        lines.append("")
    if full_pos:
        lines.append(
            f"{len(full_pos)} cells have a positive full-window expectancy. "
            f"The largest of those has {max(row['trades'] for row in full_pos)} trades."
        )
        lines.append("")
    lines.append("## Top 5 by holdout expectancy")
    lines.append("")
    if not result["top"]:
        lines.append("No cell had 30 trades and a holdout trade.")
        lines.append("")
    else:
        lines.append("| setup | trades | win | avg win R | avg loss R | E | net | max DD | tune E | holdout n/E | H1 n/E | H2 n/E | raw | penalty |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
        for row in result["top"]:
            lines.append(
                "| {name} | {trades} | {win:.1%} | {aw:.3f} | {al:.3f} | {e:.3f} | {net:.2f}% | {dd:.2f}% | {te} | {hn}/{he} | {h1n}/{h1} | {h2n}/{h2} | {raw} | {pen} |".format(
                    name=row["name"],
                    trades=row["trades"],
                    win=row["win_rate"],
                    aw=row["avg_win_r"],
                    al=row["avg_loss_r"],
                    e=row["expectancy_r"],
                    net=row["net_pct"],
                    dd=row["max_dd_pct"],
                    te=_fmt_e(row["tune_e"]),
                    hn=row["holdout_trades"],
                    he=_fmt_e(row["holdout_e"]),
                    h1n=row["half1_trades"],
                    h1=_fmt_e(row["half1_e"]),
                    h2n=row["half2_trades"],
                    h2=_fmt_e(row["half2_e"]),
                    raw="yes" if row["raw_pass"] else "no",
                    pen="yes" if row["penalty_pass"] else "no",
                )
            )
        lines.append("")
        for row in result["top"]:
            lines.append(f"- `{row['name']}` exits: {_exit_line(row['exits'])}. Per coin: {_coin_line(row['per_coin'])}.")
        lines.append("")
    lines.append("## Walk-forward pick")
    lines.append("")
    chosen = result["walk_forward"]
    if chosen is None:
        lines.append("No cell had 30 trades overall and 15 trades in the first 60 days.")
    else:
        lines.append(
            f"`{chosen['name']}` had tune expectancy {_fmt_e(chosen['tune_e'])} R on {chosen['tune_trades']} trades. "
            f"Holdout: {chosen['holdout_trades']} trades, {_fmt_e(chosen['holdout_e'])} R. "
            f"Full window: {chosen['trades']} trades, win {chosen['win_rate']:.1%}, "
            f"avg win {chosen['avg_win_r']:.3f} R, avg loss {chosen['avg_loss_r']:.3f} R, "
            f"expectancy {chosen['expectancy_r']:.3f} R, net {chosen['net_pct']:.2f}%, "
            f"max DD {chosen['max_dd_pct']:.2f}%. "
            f"Halves: {chosen['half1_trades']}/{_fmt_e(chosen['half1_e'])} R and {chosen['half2_trades']}/{_fmt_e(chosen['half2_e'])} R. "
            f"Raw bar: {'yes' if chosen['raw_pass'] else 'no'}. Penalty: {'yes' if chosen['penalty_pass'] else 'no'}."
        )
        lines.append("")
        lines.append(f"Exits: {_exit_line(chosen['exits'])}. Per coin: {_coin_line(chosen['per_coin'])}.")
    lines.append("")
    lines.append("R is net dollars divided by size times the original stop distance. The stop is beyond the level, not beyond the sweep wick.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Pre-registered 90-day swing search. Research only.")
    parser.add_argument("--cache", default="/tmp/hl_swing_cache")
    parser.add_argument("--out", default="docs/pnl/swing_search.md")
    args = parser.parse_args(argv)
    cache = Path(args.cache)
    data = load_hourly(cache)
    if not data:
        print("no 1h candles in", cache)
        return 1
    span_15m = _span_days(cache / "BTC_15m.json")
    if span_15m is None:
        span_15m = 0.0
    result = run_search(data, span_15m=span_15m)
    text = render(result)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    payload = {key: value for key, value in result.items() if key != "rows"}
    payload["top_names"] = [row["name"] for row in result["top"]]
    # Keep the full grid out of the markdown. The json keeps every cell's summary
    # without the per-bar blotter.
    slim = []
    for row in result["rows"]:
        item = dict(row)
        item["per_coin"] = {
            coin: {"trades": info["trades"], "expectancy_r": info["expectancy_r"], "net": info["net"]}
            for coin, info in row["per_coin"].items()
            if info["trades"]
        }
        slim.append(item)
    payload["cells"] = slim
    dest.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(text)
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

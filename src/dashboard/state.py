"""Pure journal → dashboard state reconstruction (no I/O side effects beyond reads)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SYMBOLS = [
    {"coin": "BTC", "tv": "HYPERLIQUID:BTCUSDC.P"},
    {"coin": "SOL", "tv": "HYPERLIQUID:SOLUSDC.P"},
    {"coin": "XRP", "tv": "HYPERLIQUID:XRPUSDC.P"},
]

TRADE_EVENTS = frozenset({"open", "close"})


def resolve_journal_path(
    journal_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> Path:
    """Resolve JOURNAL_PATH; relative paths are under project_root (cwd default)."""
    import os

    raw = journal_path if journal_path is not None else os.getenv("JOURNAL_PATH", "logs/trades.jsonl")
    path = Path(raw)
    if path.is_absolute():
        return path
    root = Path(project_root) if project_root is not None else Path.cwd()
    return (root / path).resolve()


def read_journal(path: str | Path) -> list[dict[str, Any]]:
    """Read JSONL journal; skip blank/corrupt lines."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _utc_day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _session_start_ts(events: list[dict[str, Any]]) -> float | None:
    """Latest start event timestamp; session = since that start (or whole file)."""
    last: float | None = None
    for ev in events:
        if ev.get("event") == "start":
            try:
                last = float(ev.get("ts") or 0)
            except (TypeError, ValueError):
                continue
    return last


def _normalize_trade(ev: dict[str, Any]) -> dict[str, Any]:
    """Normalize open/close journal row for the tape."""
    event = str(ev.get("event") or "")
    out: dict[str, Any] = {
        "ts": float(ev.get("ts") or 0),
        "event": event,
        "symbol": str(ev.get("symbol") or "").upper() or None,
        "side": ev.get("side"),
        "size": ev.get("size"),
        "price": ev.get("price"),
        "trade_id": ev.get("trade_id"),
        "stop": ev.get("stop") if "stop" in ev else ev.get("stop_price"),
        "tp": ev.get("tp") if "tp" in ev else ev.get("take_profit"),
        "pnl": ev.get("pnl"),
        "reason": ev.get("reason"),
        "action": ev.get("action"),
        "vwap": ev.get("vwap"),
        "dollar_risk": ev.get("dollar_risk"),
        "leverage": ev.get("leverage"),
        "stop_pct": ev.get("stop_pct"),
    }
    return out


def reconstruct_open_positions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological open/close by trade_id (fallback: symbol).

    An open without a matching close remains in the list.
    """
    opens: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for ev in events:
        event = ev.get("event")
        if event == "open":
            trade_id = ev.get("trade_id")
            symbol = str(ev.get("symbol") or "").upper()
            key = str(trade_id) if trade_id else f"sym:{symbol}"
            pos = {
                "trade_id": trade_id,
                "symbol": symbol,
                "side": ev.get("side"),
                "size": float(ev.get("size") or 0),
                "entry": float(ev.get("price") or 0),
                "stop": float(ev.get("stop") or ev.get("stop_price") or 0),
                "tp": float(ev.get("tp") or ev.get("take_profit") or 0),
                "opened_at": float(ev.get("ts") or 0),
                "vwap": ev.get("vwap"),
                "dollar_risk": ev.get("dollar_risk"),
                "leverage": ev.get("leverage"),
                "mark": None,
                "upnl": None,
            }
            if key not in opens:
                order.append(key)
            opens[key] = pos
        elif event == "close":
            trade_id = ev.get("trade_id")
            symbol = str(ev.get("symbol") or "").upper()
            key = None
            if trade_id is not None and str(trade_id) in opens:
                key = str(trade_id)
            else:
                # match oldest open for symbol
                for k in list(order):
                    if k in opens and opens[k].get("symbol") == symbol:
                        key = k
                        break
            if key is not None and key in opens:
                del opens[key]
                if key in order:
                    order.remove(key)

    return [opens[k] for k in order if k in opens]


def compute_stats(
    events: list[dict[str, Any]],
    *,
    day_key: str | None = None,
    session_start: float | None = None,
) -> dict[str, Any]:
    """Day (UTC) and session stats from open/close events."""
    day = day_key or _today_utc()
    if session_start is None:
        session_start = _session_start_ts(events)

    day_opens = day_closes = 0
    day_pnl = 0.0
    sess_opens = sess_closes = 0
    sess_pnl = 0.0
    wins = losses = 0
    sess_wins = sess_losses = 0

    for ev in events:
        event = ev.get("event")
        if event not in TRADE_EVENTS:
            continue
        try:
            ts = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        in_day = _utc_day_key(ts) == day
        in_sess = session_start is None or ts >= session_start

        if event == "open":
            if in_day:
                day_opens += 1
            if in_sess:
                sess_opens += 1
        elif event == "close":
            try:
                pnl = float(ev.get("pnl") or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            if in_day:
                day_closes += 1
                day_pnl += pnl
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
            if in_sess:
                sess_closes += 1
                sess_pnl += pnl
                if pnl > 0:
                    sess_wins += 1
                elif pnl < 0:
                    sess_losses += 1

    return {
        "day_utc": day,
        "day_opens": day_opens,
        "day_closes": day_closes,
        "day_realized_pnl": round(day_pnl, 6),
        "day_wins": wins,
        "day_losses": losses,
        "session_opens": sess_opens,
        "session_closes": sess_closes,
        "session_realized_pnl": round(sess_pnl, 6),
        "session_wins": sess_wins,
        "session_losses": sess_losses,
        "session_start_ts": session_start,
    }


def compute_status(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Bot status strip: last journal time + mode from latest start."""
    last_ts: float | None = None
    mode = "UNKNOWN"
    symbols: list[str] = []
    last_start: dict[str, Any] | None = None
    last_event: str | None = None

    for ev in events:
        try:
            ts = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if last_ts is None or ts >= last_ts:
            last_ts = ts
            last_event = str(ev.get("event") or "")
        if ev.get("event") == "start":
            last_start = ev
            mode = str(ev.get("mode") or "UNKNOWN").upper()
            raw_syms = ev.get("symbols")
            if isinstance(raw_syms, list):
                symbols = [str(s).upper() for s in raw_syms]
            elif ev.get("symbol"):
                symbols = [str(ev["symbol"]).upper()]

    return {
        "mode": mode,
        "paper": mode != "LIVE",
        "last_journal_ts": last_ts,
        "last_event": last_event,
        "symbols": symbols or [s["coin"] for s in DEFAULT_SYMBOLS],
        "running": last_event not in (None, "stop"),
        "start_equity": (last_start or {}).get("equity"),
        "stop_pct": (last_start or {}).get("stop_pct"),
        "leverage": (last_start or {}).get("leverage"),
    }


def apply_marks_to_positions(
    positions: list[dict[str, Any]],
    marks: dict[str, float] | None,
) -> list[dict[str, Any]]:
    """Attach mark + unrealized pnl when mids are available."""
    if not marks:
        return positions
    out: list[dict[str, Any]] = []
    for pos in positions:
        p = dict(pos)
        sym = str(p.get("symbol") or "").upper()
        mark = marks.get(sym)
        if mark is None:
            out.append(p)
            continue
        entry = float(p.get("entry") or 0)
        size = float(p.get("size") or 0)
        side = p.get("side")
        if side == "long":
            upnl = (mark - entry) * size
        elif side == "short":
            upnl = (entry - mark) * size
        else:
            upnl = None
        p["mark"] = mark
        p["upnl"] = None if upnl is None else round(upnl, 6)
        out.append(p)
    return out


def build_state(
    events: list[dict[str, Any]],
    *,
    last_n: int = 80,
    marks: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Full /api/state payload from journal events (+ optional marks)."""
    trade_rows = [_normalize_trade(e) for e in events if e.get("event") in TRADE_EVENTS]
    trades = trade_rows[-last_n:] if last_n > 0 else trade_rows
    # newest first for tape
    trades = list(reversed(trades))

    open_positions = reconstruct_open_positions(events)
    open_positions = apply_marks_to_positions(open_positions, marks)

    status = compute_status(events)
    stats = compute_stats(events, session_start=_session_start_ts(events))

    total_upnl = 0.0
    has_upnl = False
    for p in open_positions:
        if p.get("upnl") is not None:
            total_upnl += float(p["upnl"])
            has_upnl = True

    stats["open_count"] = len(open_positions)
    stats["unrealized_pnl"] = round(total_upnl, 6) if has_upnl else None

    return {
        "trades": trades,
        "open_positions": open_positions,
        "stats": stats,
        "status": status,
        "symbols": DEFAULT_SYMBOLS,
        "marks": marks or {},
    }


def load_state(
    journal_path: str | Path,
    *,
    last_n: int = 80,
    marks: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Read journal file and build state."""
    events = read_journal(journal_path)
    return build_state(events, last_n=last_n, marks=marks)

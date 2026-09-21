"""Tests for dashboard journal state reconstruction (pure functions)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashboard.state import (
    apply_marks_to_positions,
    build_state,
    compute_stats,
    compute_status,
    load_state,
    read_journal,
    reconstruct_open_positions,
    resolve_journal_path,
)


def _ev(event: str, ts: float, **kw):
    return {"ts": ts, "event": event, **kw}


def test_resolve_journal_path_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = resolve_journal_path("logs/trades.jsonl", project_root=tmp_path)
    assert p == (tmp_path / "logs" / "trades.jsonl").resolve()


def test_resolve_journal_path_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_PATH", "custom/j.jsonl")
    p = resolve_journal_path(project_root=tmp_path)
    assert p == (tmp_path / "custom" / "j.jsonl").resolve()


def test_read_journal_skips_corrupt(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(_ev("start", 1000.0, mode="PAPER")) + "\n"
        + "not-json\n"
        + "\n"
        + json.dumps(_ev("open", 1001.0, symbol="BTC", side="long", size=0.1, price=100.0, trade_id="a1"))
        + "\n",
        encoding="utf-8",
    )
    rows = read_journal(path)
    assert len(rows) == 2
    assert rows[0]["event"] == "start"


def test_reconstruct_open_by_trade_id():
    events = [
        _ev("start", 1.0, mode="PAPER", symbols=["BTC", "SOL"]),
        _ev(
            "open",
            2.0,
            symbol="BTC",
            side="long",
            size=0.2,
            price=86000.0,
            stop=85871.0,
            tp=86258.0,
            trade_id="btc1",
        ),
        _ev(
            "open",
            3.0,
            symbol="SOL",
            side="short",
            size=1.5,
            price=150.0,
            stop=150.225,
            tp=149.55,
            trade_id="sol1",
        ),
        _ev(
            "close",
            4.0,
            symbol="BTC",
            side="long",
            size=0.2,
            price=86100.0,
            pnl=20.0,
            reason="take_profit",
            trade_id="btc1",
            action="close",
        ),
    ]
    opens = reconstruct_open_positions(events)
    assert len(opens) == 1
    assert opens[0]["symbol"] == "SOL"
    assert opens[0]["side"] == "short"
    assert opens[0]["entry"] == 150.0
    assert opens[0]["stop"] == 150.225
    assert opens[0]["tp"] == 149.55
    assert opens[0]["trade_id"] == "sol1"


def test_reconstruct_close_fallback_by_symbol():
    events = [
        _ev("open", 1.0, symbol="XRP", side="long", size=100, price=0.5, stop=0.499, tp=0.502, trade_id="x1"),
        _ev("close", 2.0, symbol="XRP", side="long", size=100, price=0.49, pnl=-1.0, reason="stop"),
    ]
    assert reconstruct_open_positions(events) == []


def test_apply_marks_upnl():
    positions = [
        {
            "symbol": "BTC",
            "side": "long",
            "size": 0.1,
            "entry": 100.0,
            "stop": 99.0,
            "tp": 102.0,
            "trade_id": "a",
            "opened_at": 1.0,
        },
        {
            "symbol": "SOL",
            "side": "short",
            "size": 2.0,
            "entry": 50.0,
            "stop": 51.0,
            "tp": 48.0,
            "trade_id": "b",
            "opened_at": 2.0,
        },
    ]
    out = apply_marks_to_positions(positions, {"BTC": 110.0, "SOL": 45.0})
    assert out[0]["mark"] == 110.0
    assert out[0]["upnl"] == pytest.approx(1.0)  # (110-100)*0.1
    assert out[1]["mark"] == 45.0
    assert out[1]["upnl"] == pytest.approx(10.0)  # (50-45)*2


def test_compute_stats_day_and_session():
    # Fixed timestamps: day 2024-01-15 UTC and session after start at ts=1705300000
    # 1705300000 = 2024-01-15 05:06:40 UTC
    start = 1705300000.0
    events = [
        _ev("start", start, mode="PAPER", equity=5000),
        _ev("open", start + 10, symbol="BTC", side="long", size=0.1, price=100, trade_id="1"),
        _ev(
            "close",
            start + 20,
            symbol="BTC",
            side="long",
            size=0.1,
            price=110,
            pnl=1.0,
            trade_id="1",
            reason="take_profit",
        ),
        _ev("open", start + 30, symbol="SOL", side="short", size=1, price=50, trade_id="2"),
        _ev(
            "close",
            start + 40,
            symbol="SOL",
            side="short",
            size=1,
            price=55,
            pnl=-5.0,
            trade_id="2",
            reason="stop",
        ),
    ]
    stats = compute_stats(events, day_key="2024-01-15", session_start=start)
    assert stats["day_opens"] == 2
    assert stats["day_closes"] == 2
    assert stats["day_realized_pnl"] == pytest.approx(-4.0)
    assert stats["day_wins"] == 1
    assert stats["day_losses"] == 1
    assert stats["session_opens"] == 2
    assert stats["session_realized_pnl"] == pytest.approx(-4.0)


def test_compute_status_mode_and_running():
    events = [
        _ev("start", 1.0, mode="PAPER", symbols=["BTC", "SOL", "XRP"], equity=5000),
        _ev("open", 2.0, symbol="BTC", side="long", size=0.1, price=1, trade_id="1"),
    ]
    st = compute_status(events)
    assert st["mode"] == "PAPER"
    assert st["paper"] is True
    assert st["running"] is True
    assert st["last_event"] == "open"
    assert st["symbols"] == ["BTC", "SOL", "XRP"]

    events.append(_ev("stop", 3.0, equity=5000))
    st2 = compute_status(events)
    assert st2["running"] is False
    assert st2["last_event"] == "stop"


def test_build_state_tape_newest_first_and_open():
    events = [
        _ev("start", 1.0, mode="PAPER", symbols=["BTC"]),
        _ev(
            "open",
            2.0,
            symbol="BTC",
            side="long",
            size=0.1,
            price=100,
            stop=99,
            tp=102,
            trade_id="t1",
        ),
        _ev(
            "close",
            3.0,
            symbol="BTC",
            side="long",
            size=0.1,
            price=101,
            pnl=0.1,
            trade_id="t1",
            reason="take_profit",
            action="close",
        ),
        _ev(
            "open",
            4.0,
            symbol="BTC",
            side="short",
            size=0.05,
            price=101,
            stop=101.15,
            tp=100.7,
            trade_id="t2",
        ),
    ]
    state = build_state(events, last_n=10, marks={"BTC": 100.5})
    assert state["trades"][0]["event"] == "open"
    assert state["trades"][0]["trade_id"] == "t2"
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["trade_id"] == "t2"
    assert state["open_positions"][0]["upnl"] == pytest.approx(0.025)  # short: (101-100.5)*0.05
    assert state["status"]["mode"] == "PAPER"
    assert state["stats"]["open_count"] == 1
    assert state["symbols"][0]["tv"].startswith("HYPERLIQUID:")


def test_load_state_from_file(tmp_path):
    path = tmp_path / "trades.jsonl"
    rows = [
        _ev("start", 10.0, mode="LIVE", symbols=["BTC"]),
        _ev("open", 11.0, symbol="BTC", side="long", size=1, price=10, stop=9, tp=12, trade_id="z"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    state = load_state(path)
    assert state["status"]["mode"] == "LIVE"
    assert state["status"]["paper"] is False
    assert len(state["open_positions"]) == 1


def test_empty_journal(tmp_path):
    path = tmp_path / "missing.jsonl"
    state = load_state(path)
    assert state["trades"] == []
    assert state["open_positions"] == []
    assert state["status"]["mode"] == "UNKNOWN"

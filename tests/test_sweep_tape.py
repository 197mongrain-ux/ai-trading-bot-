"""Sweep entry on a recorded tape. Synthetic prints only. Paper stays on the spec book."""

from __future__ import annotations

import csv
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hl_bot.research.sweep_tape_backtest import main, run, scan_sweep, sweep_params
from hl_bot.research.trapped_backtest import expectancy_split, simulate
from hl_bot.strategy.model_b.footprint import TapePrint
from hl_bot.strategy.model_b.swing import SwingParams, plan_trade


def _hours(days: int, *, step: float, base: float, start: float) -> list[dict]:
    bars = []
    for i in range(days * 24):
        px = base + i * step
        bars.append(
            {
                "t": (start + i * 3600) * 1000.0,
                "o": px,
                "h": px + abs(step) + 0.2,
                "l": px - 0.15,
                "c": px + step * 0.5,
                "v": 1.0,
            }
        )
    return bars


def _book():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    hourly = _hours(40, step=0.05, base=100.0, start=start)
    signal = start + 30 * 86400 + 12 * 3600
    day = int(signal // 86400) * 86400
    prev = [bar for bar in hourly if day - 86400 <= bar["t"] / 1000.0 < day]
    pdl = min(bar["l"] for bar in prev)
    confirm = [
        {
            "t": (signal - 1800) * 1000.0,
            "o": pdl + 0.4,
            "h": pdl + 0.6,
            "l": pdl + 0.3,
            "c": pdl + 0.5,
            "v": 1.0,
        },
        {
            "t": (signal - 900) * 1000.0,
            "o": pdl + 0.3,
            "h": pdl + 0.4,
            "l": pdl * (1.0 - 12.0 / 10_000.0),
            "c": pdl * (1.0 + 8.0 / 10_000.0),
            "v": 1.0,
        },
    ]
    return hourly, confirm, pdl


def test_sweep_scan_arms_on_injected_hourly_history_and_a_print_stops_it():
    hourly, confirm, _pdl = _book()
    params = sweep_params()
    assert params.macro_tfs == ("1h", "4h")
    assert params.macro_mode == "4h_lead"
    assert params.confirm_tf == "15m"
    assert params.flow == "off"
    assert params.entry == "sweep"
    orders, fails = scan_sweep("BTC", confirm, hourly, params, 10_000.0, 0.01)
    assert orders, dict(fails)
    order = orders[-1]
    assert order.side == "long"
    assert order.budget > 0
    assert order.stop < order.entry < order.take_profit
    stopped = [
        TapePrint(order.signal_ts + 1, order.entry, 1.0, "sell"),
        TapePrint(order.signal_ts + 2, order.stop - 0.01, 1.0, "sell"),
    ]
    rows, _counts = simulate([order], {"BTC": stopped}, params, 10_000.0, 0.01)
    assert len(rows) == 1
    assert rows[0]["reason"] == "stop"
    assert float(rows[0]["r"]) == pytest.approx(float(rows[0]["net"]) / float(rows[0]["budget"]))
    assert float(rows[0]["net"]) < 0
    assert abs(float(rows[0]["net"])) <= 10_000.0 * 0.02 + 1e-4
    price_risk = float(rows[0]["size"]) * abs(float(rows[0]["entry"]) - float(rows[0]["stop"]))
    assert float(rows[0]["budget"]) > price_risk
    held = [
        TapePrint(order.signal_ts + 1, order.entry, 1.0, "sell"),
        TapePrint(order.signal_ts + 5, order.entry, 1.0, "buy"),
    ]
    marked, _counts = simulate([order], {"BTC": held}, params, 10_000.0, 0.01)
    assert marked[0]["reason"] == "tape_end"
    split = expectancy_split(marked)
    assert split["n_closed"] == 0 and split["n_marks"] == 1
    assert split["e_with_marks"] == pytest.approx(split["e_marks"])
    # The geometry the harness asked plan_trade for is a real plan, not a stub.
    now = confirm[-1]["t"] / 1000.0 + 900.0
    planned = plan_trade("BTC", now, confirm, hourly, params, 10_000.0, risk_pct=0.01)
    assert not isinstance(planned, str)
    assert planned.budget > 0


def test_short_tape_reports_how_many_bars_were_scanned(tmp_path: Path):
    folder = tmp_path / "BTC"
    folder.mkdir()
    start = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc).timestamp()
    lines = []
    for i in range(180):
        lines.append(
            json.dumps(
                {
                    "coin": "BTC",
                    "side": "B" if i % 2 == 0 else "A",
                    "px": "82000",
                    "sz": "0.01",
                    "time": int((start + i * 60) * 1000),
                    "tid": i,
                }
            )
        )
    with gzip.open(folder / "2026-10-09.trades.jsonl.gz", "wt") as handle:
        handle.write("\n".join(lines) + "\n")
    out = tmp_path / "out"
    code = main(
        [
            "--tape",
            str(tmp_path),
            "--from",
            "2026-10-09",
            "--to",
            "2026-10-09",
            "--coins",
            "BTC",
            "--out",
            str(out),
            "--equity",
            "5000",
        ]
    )
    assert code == 0
    text = (out / "sweep_summary.md").read_text()
    assert "MACRO_UNKNOWN" in text
    assert "with-marks" in text
    assert "risk budget" in text
    with (out / "sweep_trades.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []
    result = run(tmp_path, ["BTC"], "2026-10-09", "2026-10-09")
    assert result["rows"] == []
    assert result["scanned"] > 0
    assert result["fails"]["MACRO_UNKNOWN"] == result["scanned"]
    assert result["coverage"]["BTC"]["hourly"] < 10


def test_monday_paper_params_are_not_the_tape_frame():
    paper = SwingParams()
    tape = sweep_params()
    assert paper.macro_tfs == ("4h", "1d")
    assert paper.macro_mode == "both"
    assert tape.macro_mode == "4h_lead"
    assert paper.entry == "sweep"

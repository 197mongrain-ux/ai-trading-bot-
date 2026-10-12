"""The one-week replay states its sample and does not invent tape."""

from datetime import datetime
from zoneinfo import ZoneInfo

from hl_bot.research.last_week import (
    WEEK_MIN_TRADES,
    TZ,
    cells_for_week,
    merge_candles,
    render,
    scalp_candidate,
    week_end,
    week_start,
)
from hl_bot.strategy.model_b.swing import session_open
from hl_bot.strategy.model_b.swing_search import CONFIRMS, iter_cells

ET = ZoneInfo("America/Toronto")


def test_cash_session_bounds_are_monday_open_through_friday_close():
    start = week_start()
    end = week_end()
    assert datetime.fromtimestamp(start, ET) == datetime(2026, 10, 5, 9, 30, tzinfo=ET)
    assert datetime.fromtimestamp(start, ET).weekday() == 0
    assert datetime.fromtimestamp(end, ET) == datetime(2026, 10, 9, 16, 0, tzinfo=ET)
    assert datetime.fromtimestamp(end, ET).weekday() == 4
    assert session_open(start, "09:30-16:00")
    assert not session_open(start - 60, "09:30-16:00")
    assert not session_open(end, "09:30-16:00")
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=ET).timestamp()
    assert not session_open(saturday, "09:30-16:00")
    assert TZ.key == "America/Toronto"


def test_week_runs_the_full_grid_and_ranks_on_a_five_trade_floor():
    cells = cells_for_week()
    assert cells == iter_cells(90.0)
    assert len(cells) == 2592
    assert {cell.confirm for cell in cells} == set(CONFIRMS)
    assert all(cell.eligible for cell in cells)
    assert WEEK_MIN_TRADES == 5


def test_merge_keeps_the_later_bar_and_sorts_by_open():
    older = [{"t": 2000, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1}]
    newer = [
        {"t": 1000, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1},
        {"t": "2000", "o": 1, "h": 3, "l": 1, "c": 2, "v": 4},
    ]
    merged = merge_candles(older, newer)
    assert [row["t"] for row in merged] == [1000.0, 2000.0]
    assert merged[1]["h"] == 3.0
    assert merged[1]["c"] == 2.0


def _minute(i: int, low: float, high: float, close: float) -> dict:
    return {
        "t": 1_700_000_000_000 + i * 60_000,
        "o": close,
        "h": high,
        "l": low,
        "c": close,
        "v": 1,
    }


def _tape(last_low: float, last_close: float) -> tuple[list[dict], float]:
    bars = [_minute(i, 100.0, 101.0, 100.5) for i in range(40)]
    bars[15] = _minute(15, 99.0, 101.0, 100.2)
    bars[-1] = _minute(39, last_low, 100.8, last_close)
    now = float(bars[-1]["t"]) / 1000.0 + 60.0
    return bars, now


def test_structural_scalp_needs_a_reclaim_and_does_not_invent_prints():
    bars, now = _tape(98.5, 100.5)
    armed = scalp_candidate("BTC", bars, now)
    assert armed is not None
    assert armed.side == "long"
    assert armed.entry <= 98.5 + 1e-9
    assert armed.stop < armed.entry
    assert armed.tp > armed.entry
    flat, flat_now = _tape(98.5, 98.2)
    assert scalp_candidate("BTC", flat, flat_now) is None


def test_coverage_flags_1m_that_starts_after_monday():
    from datetime import timezone

    from hl_bot.research.last_week import _coverage_line

    early = int(datetime(2026, 10, 8, 10, 31, tzinfo=timezone.utc).timestamp() * 1000)
    late = int(datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        {"t": early, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1},
        {"t": late, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1},
    ]
    text = _coverage_line({"BTC": rows}, {"BTC": rows}, {"TAO": rows})
    assert "after Monday's open" in text
    assert "earlier cash sessions are absent" in text


def test_shared_entries_are_not_counted_as_separate_setups():
    day = {
        "day": "2026-10-05",
        "trades": 5,
        "win_rate": 0.6,
        "expectancy_r": 0.2,
        "net": 10.0,
    }

    def setup(name: str) -> dict:
        return {
            "name": name,
            "trades": 5,
            "win_rate": 0.6,
            "avg_win_r": 2.0,
            "avg_loss_r": -1.0,
            "expectancy_r": 0.2,
            "net_pct": 1.0,
            "max_dd_pct": 1.0,
            "exits": {"tp": 3, "stop": 2},
            "per_coin": {},
            "per_day": [day],
        }

    text = render(
        {
            "coverage": "cache check",
            "swing": {
                "cells": 2592,
                "penalty": 0.2,
                "zero": 0,
                "under_floor": 0,
                "with_5": 2,
                "positive": 2,
                "positive_max_trades": 5,
                "signals_1h": 1,
                "signals_15m": 1,
                "top": [
                    setup("touch/4h/15m/none/k1.5/tp3/be/all"),
                    setup("touch/4h/15m/none/k1.5/tp3/overnight/all"),
                ],
                "anecdote": {
                    **setup("sweep/4h/1h/4h+1d/k1.5/tp3/overnight/crypto"),
                    "trades": 1,
                    "win_rate": 1.0,
                },
            },
            "scalp": {
                "trades": 0,
                "signals": 0,
                "skipped": 0,
                "exits": {},
                "missing": [],
                "per_day": [day],
                "signal_days": [{"day": "2026-10-05", "signals": 0}],
                "per_coin": {},
            },
        }
    )
    assert "one sample counted more than once" in text
    assert "ranking floor" in text
    assert "avg loser — R" in text
    assert "Monday, Tuesday, and Wednesday have no 1m candles" in text


def test_report_says_one_week_is_not_a_verdict_and_counts_the_cells():
    day = {
        "day": "2026-10-05",
        "trades": 1,
        "win_rate": 1.0,
        "expectancy_r": 0.2,
        "net": 10.0,
    }
    setup = {
        "name": "sweep/4h/1h/none/k1/tp1.5/flat/all",
        "trades": 6,
        "win_rate": 0.5,
        "avg_win_r": 1.2,
        "avg_loss_r": -1.1,
        "expectancy_r": 0.05,
        "net_pct": 0.4,
        "max_dd_pct": 1.2,
        "exits": {"stop": 3, "tp": 3},
        "per_coin": {"BTC": {"trades": 6, "expectancy_r": 0.05, "net": 40.0}},
        "per_day": [day],
    }
    text = render(
        {
            "coverage": "cache check",
            "swing": {
                "cells": 2592,
                "penalty": 0.2,
                "zero": 10,
                "under_floor": 20,
                "with_5": 6,
                "positive": 4,
                "positive_max_trades": 8,
                "signals_1h": 3,
                "signals_15m": 5,
                "top": [setup],
                "anecdote": setup,
            },
            "scalp": {
                "trades": 2,
                "win_rate": 0.5,
                "avg_win_r": 1.0,
                "avg_loss_r": -1.0,
                "expectancy_r": 0.0,
                "net_pct": 0.0,
                "max_dd_pct": 1.0,
                "signals": 4,
                "skipped": 1,
                "exits": {"stop": 1, "tp": 1},
                "missing": [],
                "per_day": [day],
                "signal_days": [{"day": "2026-10-05", "signals": 4}],
                "per_coin": {"BTC": {"signals": 4, "trades": 2, "expectancy_r": 0.0, "net": 0.0}},
            },
        }
    )
    assert "One week is not a verdict." in text
    assert "2592" in text
    assert "not a pick for Monday" in text
    assert "No setup is promoted." in text
    assert "reports/model_b_trade_notes.json" in text
    assert "15.0 bp" in text

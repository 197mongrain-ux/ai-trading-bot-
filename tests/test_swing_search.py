"""The 90-day search bar is locked: grid size, penalty, touch, and one position."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hl_bot.strategy.model_b.swing import find_sweep
from hl_bot.strategy.model_b.swing_search import (
    BOOKS,
    CONFIRMS,
    ENTRIES,
    LEVELS,
    MACROS,
    MIN_TRADES,
    SESSIONS,
    STOPS,
    TARGETS,
    Signal,
    iter_cells,
    penalty,
    simulate,
    stop_beyond_level,
    survives,
    touch_reclaim,
    fixed_target,
)


def test_registered_grid_and_the_fifteen_minute_limit():
    cells = iter_cells(52.1)
    assert len(cells) == (
        len(ENTRIES) * len(LEVELS) * len(CONFIRMS) * len(MACROS) * len(STOPS) * len(TARGETS) * len(SESSIONS) * len(BOOKS)
    )
    assert len(cells) == 2592
    tried = [cell for cell in cells if cell.eligible]
    assert len(tried) == 1296
    assert {cell.confirm for cell in tried} == {"1h"}
    assert sum(1 for cell in cells if not cell.eligible) == 1296
    full = iter_cells(90.0)
    assert all(cell.eligible for cell in full)


def test_penalty_and_the_survival_bar():
    pen = penalty(1296)
    assert pen == pytest.approx(0.05 * (2.0 * __import__("math").log(1296)) ** 0.5)
    assert pen > 0.15
    raw, cleared = survives(MIN_TRADES, 0.01, 0.01, 0.01, pen)
    assert raw and not cleared
    raw, cleared = survives(MIN_TRADES, 0.50, 0.10, 0.10, pen)
    assert raw and cleared
    assert survives(MIN_TRADES - 1, 0.50, 0.10, 0.10, pen) == (False, False)
    assert survives(MIN_TRADES, 0.50, -0.01, 0.10, pen) == (False, False)
    assert survives(MIN_TRADES, -0.01, 0.10, 0.10, pen) == (False, False)
    assert survives(MIN_TRADES, None, 0.10, 0.10, pen) == (False, False)
    assert penalty(1) == 0.0


def test_touch_has_no_five_bp_floor_and_the_stop_is_beyond_the_level():
    shallow = {"t": 0.0, "h": 100.2, "l": 99.97, "c": 100.05}
    assert touch_reclaim(shallow, 100.0, "long")
    assert find_sweep([shallow], 100.0, "long", 5.0) is None
    assert stop_beyond_level("long", 100.0, 10.0, 0.5) == pytest.approx(95.0)
    assert stop_beyond_level("short", 100.0, 10.0, 1.5) == pytest.approx(115.0)
    assert fixed_target("long", 100.0, 95.0, 2.0) == pytest.approx(110.0)
    assert not touch_reclaim({"t": 0.0, "h": 101.0, "l": 100.1, "c": 100.4}, 100.0, "long")


def _ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 7, day, hour, minute, tzinfo=ZoneInfo("America/Toronto")).timestamp()


def _bar(ts: float, high: float, low: float, close: float) -> tuple[float, float, float, float]:
    return (ts, high, low, close)


def test_one_position_takes_the_higher_score_and_sessions_differ():
    arm = _ts(15, 11)
    fill = _ts(15, 12)
    close = _ts(15, 16)
    later = _ts(18, 12)
    eth = Signal(arm, "ETH", "long", 100.0, 2.0, 9.0, ())
    btc = Signal(arm, "BTC", "long", 100.0, 2.0, 2.0, ())
    bars = {
        "ETH": [
            _bar(fill, 110.0, 100.0, 105.0),
            _bar(close, 110.0, 100.2, 105.0),
        ],
        "BTC": [
            _bar(fill, 100.5, 98.0, 99.0),
            _bar(close, 100.5, 98.0, 99.0),
        ],
    }
    trades, _eq, _dd = simulate([btc, eth], bars, stop_k=0.5, target="2", session="flat")
    assert len(trades) == 1
    assert trades[0].coin == "ETH"
    assert trades[0].reason == "tp"

    flat_bars = {
        "BTC": [
            _bar(fill, 100.4, 100.0, 100.2),
            _bar(close, 100.4, 100.1, 100.2),
            _bar(later, 100.4, 100.1, 100.2),
        ]
    }
    sig = Signal(arm, "BTC", "long", 100.0, 2.0, 4.0, ())
    flat, _eq, _dd = simulate([sig], flat_bars, stop_k=0.5, target="2", session="flat", hold_days=3)
    assert len(flat) == 1 and flat[0].reason == "SESSION_CLOSE"
    held, _eq, _dd = simulate([sig], flat_bars, stop_k=0.5, target="2", session="overnight", hold_days=3)
    assert len(held) == 1 and held[0].reason == "max_hold"
    be_bars = {
        "BTC": [
            _bar(fill, 100.4, 100.0, 100.2),
            _bar(close, 101.2, 100.5, 101.0),
            _bar(_ts(15, 17), 101.0, 100.0, 100.2),
        ]
    }
    be, _eq, _dd = simulate([sig], be_bars, stop_k=0.5, target="3", session="be", hold_days=3)
    assert len(be) == 1 and be[0].reason == "stop" and be[0].exit == pytest.approx(100.0)
    weak, _eq, _dd = simulate([sig], flat_bars, stop_k=0.5, target="2", session="be", hold_days=3)
    assert len(weak) == 1 and weak[0].reason == "SESSION_CLOSE"

"""Trapped sellers and trapped buyers. Synthetic tape only. The flag stays off."""

from __future__ import annotations

import csv
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hl_bot.config import Settings, load_settings
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.footprint import (
    BookLevel,
    FootprintBar,
    TapePrint,
    build_footprint,
    diagonal_imbalances,
    parse_trade,
    session_volume_profile,
    stacked_runs,
)
from hl_bot.research.trapped_backtest import grid_cells, main
from hl_bot.strategy.model_b.swing import Level, SwingParams
from hl_bot.strategy.model_b.trapped import (
    check_trapped,
    is_mid_range,
    plan_from_signal,
    tag_level,
)
from hl_bot.strategy.model_b.types import TradePrint


def _bar(t, o, h, l, c, levels, delta, cvd) -> FootprintBar:
    buy = sum(item[1] for item in levels)
    sell = sum(item[2] for item in levels)
    return FootprintBar(
        t=float(t),
        o=float(o),
        h=float(h),
        l=float(l),
        c=float(c),
        buy=buy,
        sell=sell,
        delta=float(delta),
        cvd=float(cvd),
        levels=tuple(BookLevel(px, b, s) for px, b, s in levels),
    )


def _params(**kwargs) -> SwingParams:
    base = dict(
        entry="trapped",
        trap_bar="1m",
        trap_imbalance=3.0,
        trap_stacked=3,
        trap_zone_bps=20.0,
        trap_near_frac=0.25,
        trap_min_volume=1.0,
        trap_stop_bps=5.0,
        trap_entry="failure_close",
        trap_delta_mode="either",
        min_score=3.0,
        top_n=4,
    )
    base.update(kwargs)
    return SwingParams(**base)


def _levels():
    return [
        Level(100.0, "support", 3.0, 1, ("PDL",), 1.0),
        Level(120.0, "resistance", 3.0, 1, ("PDH",), 1.0),
    ]


def _long_trap():
    return _bar(
        0,
        112,
        112,
        100,
        104,
        [
            (100, 1, 10),
            (101, 1, 10),
            (102, 1, 10),
            (103, 1, 0),
            (112, 1, 0),
        ],
        delta=-20,
        cvd=-20,
    )


def _long_failure(low=100, close=106, delta=8, cvd=-12):
    return _bar(
        60,
        104,
        max(close, low),
        low,
        close,
        [(low, 0, 1), (close, max(delta, 0), max(-delta, 0))],
        delta=delta,
        cvd=cvd,
    )


def test_diagonal_imbalance_compares_the_next_tick_not_the_same_price():
    raw = {"coin": "BTC", "side": "A", "px": "100", "sz": "1.5", "time": 1_700_000_000_000}
    parsed = parse_trade(raw)
    assert parsed is not None and parsed.side == "sell" and parsed.ts == pytest.approx(1_700_000_000)
    assert parse_trade({"side": "B", "px": "1", "sz": "1", "time": 1}) .side == "buy"
    sold = _bar(
        0, 100, 101, 100, 100,
        [(100, 0, 9), (101, 3, 0)],
        delta=-6, cvd=-6,
    )
    sells, buys = diagonal_imbalances(sold, ratio=3, min_volume=1, tick=1)
    assert sells == (100,)
    blocked = _bar(
        0, 100, 101, 100, 100,
        [(100, 0, 9), (101, 100, 0)],
        delta=-9, cvd=-9,
    )
    sells, _buys = diagonal_imbalances(blocked, ratio=3, min_volume=1, tick=1)
    assert sells == ()
    assert stacked_runs((100, 101, 102, 104), 1) == [(100, 101, 102), (104,)]


def test_zone_requires_a_major_level_and_rejects_the_middle():
    levels = _levels()
    assert tag_level("long", 100.0, levels, 20) is not None
    assert tag_level("long", 110.0, levels, 20) is None
    assert tag_level("short", 120.0, levels, 20) is not None
    wide = [
        Level(100.0, "support", 3, 1, ("PDL",), 1),
        Level(102.0, "resistance", 3, 1, ("PDH",), 1),
    ]
    assert is_mid_range("long", 101.5, wide[0], wide)
    assert not is_mid_range("long", 100.0, wide[0], wide)
    trap = _bar(0, 101.5, 101.8, 101.5, 101.6, [(101.5, 0, 1)], -1, -1)
    failure = _bar(60, 101.6, 101.9, 101.6, 101.7, [(101.7, 1, 0)], 1, 0)
    reason = check_trapped(
        "long", trap, failure, [trap, failure], 1, wide, _params(trap_zone_bps=200), 0.1
    )
    assert reason == "MID_RANGE"
    missing = check_trapped(
        "long", _long_trap(), _long_failure(), [_long_trap(), _long_failure()], 1, levels, _params(), 1
    )
    assert not isinstance(missing, str)


def test_trap_failure_and_delta_flip_each_block_a_long():
    levels = _levels()
    trap = _long_trap()
    good = _long_failure()
    bars = [trap, good]
    signal = check_trapped("long", trap, good, bars, 1, levels, _params(), 1)
    assert not isinstance(signal, str)
    assert signal.stop < signal.absorption <= 100
    assert signal.entry == pytest.approx(106)
    assert signal.side == "long"
    lower = _long_failure(low=99, close=106, delta=8, cvd=-12)
    assert check_trapped("long", trap, lower, [trap, lower], 1, levels, _params(), 1) == "NO_FAILURE"
    flat_close = _long_failure(low=100, close=104, delta=8, cvd=-12)
    assert check_trapped("long", trap, flat_close, [trap, flat_close], 1, levels, _params(), 1) == "NO_FAILURE"
    negative = _long_failure(delta=-5, cvd=-25)
    assert check_trapped("long", trap, negative, [trap, negative], 1, levels, _params(), 1) == "NO_DELTA"
    neutral = _long_failure(delta=0, cvd=-20)
    assert not isinstance(check_trapped("long", trap, neutral, [trap, neutral], 1, levels, _params(), 1), str)
    thin = _bar(
        0, 112, 112, 100, 104,
        [(100, 1, 10), (101, 1, 10), (112, 1, 0)],
        -10, -10,
    )
    assert check_trapped("long", thin, good, [thin, good], 1, levels, _params(), 1) == "NO_TRAP"
    high_stack = _bar(
        0, 100, 112, 100, 104,
        [(109, 1, 10), (110, 1, 10), (111, 1, 10), (112, 1, 0)],
        -20, -20,
    )
    assert check_trapped("long", high_stack, good, [high_stack, good], 1, levels, _params(), 1) == "NO_TRAP"


def test_delta_modes_and_cvd_slope_can_disagree():
    levels = _levels()
    prior = _bar(0, 100, 100, 100, 100, [(100, 1, 1)], 0, 0)
    trap = _long_trap()
    trap = FootprintBar(60, trap.o, trap.h, trap.l, trap.c, trap.buy, trap.sell, -10, -10, trap.levels)
    failure = _long_failure(delta=1, cvd=-9)
    failure = FootprintBar(120, failure.o, failure.h, failure.l, failure.c, failure.buy, failure.sell, 1, -9, failure.levels)
    bars = [prior, trap, failure]
    assert not isinstance(
        check_trapped("long", trap, failure, bars, 2, levels, _params(trap_delta_mode="either"), 1), str
    )
    assert check_trapped(
        "long", trap, failure, bars, 2, levels, _params(trap_delta_mode="both"), 1
    ) == "NO_DELTA"
    assert check_trapped(
        "long", trap, failure, bars, 2, levels, _params(trap_delta_mode="cvd"), 1
    ) == "NO_DELTA"
    assert not isinstance(
        check_trapped("long", trap, failure, bars, 2, levels, _params(trap_delta_mode="delta"), 1), str
    )


def test_mirror_short_at_resistance():
    levels = _levels()
    trap = _bar(
        0, 108, 120, 108, 115,
        [
            (117, 0, 1),
            (118, 10, 1),
            (119, 10, 1),
            (120, 10, 0),
        ],
        delta=20,
        cvd=20,
    )
    failure = _bar(60, 115, 119, 112, 112, [(119, 1, 0), (112, 0, 6)], delta=-5, cvd=15)
    signal = check_trapped("short", trap, failure, [trap, failure], 1, levels, _params(), 1)
    assert not isinstance(signal, str)
    assert signal.side == "short"
    assert signal.stop > signal.absorption >= 120
    assert signal.entry == pytest.approx(112)
    higher = _bar(60, 115, 121, 112, 112, [(121, 1, 0), (112, 0, 6)], -5, 15)
    assert check_trapped("short", trap, higher, [trap, higher], 1, levels, _params(), 1) == "NO_FAILURE"
    plan = plan_from_signal("BTC", signal, levels, _params(), 10_000, risk_pct=0.01)
    assert not isinstance(plan, str)
    assert plan.take_profit == pytest.approx(100)
    assert 1.0 <= plan.r_multiple <= 5.0
    long_plan = plan_from_signal(
        "BTC",
        check_trapped("long", _long_trap(), _long_failure(), [_long_trap(), _long_failure()], 1, levels, _params(), 1),
        levels,
        _params(),
        10_000,
        risk_pct=0.01,
    )
    assert long_plan.stop < 100
    assert long_plan.take_profit == pytest.approx(120)
    assert 1.0 <= long_plan.r_multiple <= 5.0


def _oct(day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


def test_paper_flag_arms_an_alo_and_stays_off_by_default(monkeypatch):
    monkeypatch.setenv("ENTRY_MODE", "model_b")
    monkeypatch.setenv("MODEL_B_STYLE", "swing")
    monkeypatch.delenv("MODEL_B_SWING_ENTRY", raising=False)
    settings = load_settings()
    assert settings.model_b_swing_entry == "sweep"
    Settings(
        entry_mode="model_b",
        model_b_style="swing",
        risk_per_trade=0.01,
        leverage=20,
        model_b_swing_entry="trapped",
    ).validate()
    with pytest.raises(ValueError, match="MODEL_B_SWING_ENTRY"):
        Settings(
            entry_mode="model_b",
            model_b_style="swing",
            risk_per_trade=0.01,
            leverage=20,
            model_b_swing_entry="market",
        ).validate()
    hourly = []
    start = _oct(8, 0)
    for i in range(48):
        low = 100.0 if i == 3 else 108.0
        high = 120.0 if i == 10 else 112.0
        hourly.append(
            {"t": (start + i * 3600) * 1000.0, "o": 110.0, "h": high, "l": low, "c": 110.0, "v": 1.0}
        )
    trap_open = _oct(9, 12)
    prints = [
        TradePrint(trap_open + 1, "BTC", 112, 1, "buy"),
        TradePrint(trap_open + 2, "BTC", 100, 10, "sell"),
        TradePrint(trap_open + 3, "BTC", 101, 1, "buy"),
        TradePrint(trap_open + 4, "BTC", 101, 10, "sell"),
        TradePrint(trap_open + 5, "BTC", 102, 1, "buy"),
        TradePrint(trap_open + 6, "BTC", 102, 10, "sell"),
        TradePrint(trap_open + 7, "BTC", 103, 1, "buy"),
        TradePrint(trap_open + 8, "BTC", 104, 1, "sell"),
        TradePrint(trap_open + 61, "BTC", 100, 1, "sell"),
        TradePrint(trap_open + 90, "BTC", 106, 8, "buy"),
    ]
    engine = ModelBEngine(
        style="swing",
        coins=("BTC",),
        risk_pct=0.01,
        swing_params=_params(),
    )
    decision = engine.evaluate(
        "BTC",
        now=trap_open + 120,
        prints=prints,
        bars=[],
        pools=[],
        best_bid=105,
        best_ask=107,
        equity=10_000,
        tick=1,
        htf_bars=hourly,
    )
    assert decision.armed
    assert decision.intent is not None
    assert decision.intent.tif == "Alo"
    assert decision.intent.limit_px == pytest.approx(106)
    assert decision.intent.stop < 100
    assert decision.intent.take_profit == pytest.approx(120)
    sweep = ModelBEngine(
        style="swing",
        coins=("BTC",),
        risk_pct=0.01,
        swing_params=SwingParams(),
    )
    plain = sweep.evaluate(
        "BTC",
        now=trap_open + 120,
        prints=prints,
        bars=[],
        pools=[],
        best_bid=105,
        best_ask=107,
        equity=10_000,
        tick=1,
        htf_bars=hourly,
    )
    assert plain.armed is False


def _line(ts_ms, side, px, sz, coin="BTC"):
    return json.dumps(
        {"coin": coin, "side": side, "px": str(px), "sz": str(sz), "time": ts_ms, "tid": ts_ms}
    )


def test_backtest_cli_on_a_synthetic_tape(tmp_path: Path):
    assert len(grid_cells()) == 81
    folder = tmp_path / "BTC"
    folder.mkdir()
    trap = int(_oct(9, 12) * 1000)
    lines = []
    base = int(_oct(9, 11) * 1000)
    for i in range(30):
        lines.append(_line(base + i, "B", 20000, 1))
    minute = base + 60_000
    for i in range(10):
        lines.append(_line(minute + i, "A", 20040, 1))
    minute = base + 120_000
    for i in range(10):
        lines.append(_line(minute + i, "B", 20080, 1))
    lines.extend(
        [
            _line(trap + 1_000, "B", 20012, 1),
            _line(trap + 2_000, "A", 20000, 10),
            _line(trap + 3_000, "B", 20001, 1),
            _line(trap + 4_000, "A", 20001, 10),
            _line(trap + 5_000, "B", 20002, 1),
            _line(trap + 6_000, "A", 20002, 10),
            _line(trap + 7_000, "B", 20003, 1),
            _line(trap + 8_000, "A", 20004, 1),
            _line(trap + 61_000, "A", 20000, 1),
            _line(trap + 90_000, "B", 20006, 8),
            _line(trap + 120_000, "B", 20006, 1),
            _line(trap + 180_000, "B", 20040, 1),
        ]
    )
    target = folder / "2026-10-09.trades.jsonl.gz"
    with gzip.open(target, "wt") as handle:
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
            "--no-grid",
            "--equity",
            "10000",
            "--risk",
            "0.01",
        ]
    )
    assert code == 0
    text = (out / "trapped_summary.md").read_text()
    assert "cannot say whether this entry has an edge" in text
    with (out / "trapped_trades.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["reason"] == "tp"
    assert rows[0]["side"] == "long"
    assert float(rows[0]["r"]) > 0
    prints = [TapePrint(_oct(9, 11) + i * 0.01, 20000, 1, "buy") for i in range(40)]
    prints += [TapePrint(_oct(9, 11, 1) + i * 0.01, 20040, 1, "sell") for i in range(10)]
    profile = session_volume_profile(prints, tick=1, start=_oct(9, 11), end=_oct(9, 12))
    assert profile.ok and profile.val == pytest.approx(20000) and profile.poc == pytest.approx(20000)
    bars = build_footprint(prints, bar_sec=60, tick=1)
    assert bars and bars[0].delta > 0

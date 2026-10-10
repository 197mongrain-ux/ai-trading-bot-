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
from hl_bot.research.trapped_backtest import (
    Working,
    expectancy_split,
    grid_cells,
    main,
    simulate,
    stop_cells,
)
from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.swing import (
    Level,
    SwingParams,
    exit_net,
    exit_slip_bps,
    reserved_slip_bps,
    size_swing,
)
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.trapped import (
    check_trapped,
    footprint_atr,
    is_mid_range,
    place_trap_stop,
    plan_from_signal,
    tag_level,
)
from hl_bot.strategy.model_b.types import AloIntent, TradePrint


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
    assert len(stop_cells()) == 15
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
    assert "with-marks" in text
    assert "risk budget" in text
    with (out / "trapped_trades.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    default_rows = [row for row in rows if row["config"] == "1m-imb3-stack3-z20"]
    assert len(default_rows) == 1
    assert default_rows[0]["reason"] == "tp"
    assert default_rows[0]["side"] == "long"
    assert float(default_rows[0]["r"]) > 0
    assert float(default_rows[0]["r"]) == pytest.approx(
        float(default_rows[0]["net"]) / float(default_rows[0]["budget"])
    )
    flat = [row for row in rows if row["config"] == "slip-flat10"]
    assert len(flat) == 1
    assert float(flat[0]["slip_bps"]) == pytest.approx(10)
    assert float(default_rows[0]["slip_bps"]) == pytest.approx(25)
    prints = [TapePrint(_oct(9, 11) + i * 0.01, 20000, 1, "buy") for i in range(40)]
    prints += [TapePrint(_oct(9, 11, 1) + i * 0.01, 20040, 1, "sell") for i in range(10)]
    profile = session_volume_profile(prints, tick=1, start=_oct(9, 11), end=_oct(9, 12))
    assert profile.ok and profile.val == pytest.approx(20000) and profile.poc == pytest.approx(20000)
    bars = build_footprint(prints, bar_sec=60, tick=1)
    assert bars and bars[0].delta > 0


def _tight_pair():
    """Wick and close a few bps apart, so the default buffer is about 5 bps."""
    tick = 0.01
    trap = _bar(
        0,
        10000.05,
        10000.08,
        10000.0,
        10000.04,
        [
            (10000.0, 0.1, 10),
            (10000.01, 0.1, 10),
            (10000.02, 0.1, 10),
            (10000.08, 1, 0),
        ],
        delta=-20,
        cvd=-20,
    )
    failure = _bar(
        60,
        10000.04,
        10000.10,
        10000.03,
        10000.06,
        [(10000.06, 8, 0)],
        delta=8,
        cvd=-12,
    )
    levels = [
        Level(10000.0, "support", 3.0, 1, ("PDL",), 1.0),
        Level(10200.0, "resistance", 3.0, 1, ("PDH",), 1.0),
    ]
    return trap, failure, levels, tick


def _ranged(n, start, width, close=10000.0):
    bars = []
    for i in range(n):
        bars.append(
            _bar(
                start + i * 60,
                close,
                close + width,
                close,
                close,
                [(close, 1, 1)],
                delta=0,
                cvd=0,
            )
        )
    return bars


def test_defaults_keep_the_wick_stop_and_the_coin_slip():
    assert SwingParams().trap_stop_anchor == "trap"
    assert SwingParams().trap_min_stop_bps == 0
    assert SwingParams().trap_min_stop_atr == 0
    assert SwingParams().trap_slip_bps == 0
    assert SwingParams().trap_slip_mode == "flat"
    names = [cell.name for cell in stop_cells()]
    assert names[0] == "stop-trap-x20-k0.5"
    assert "stop-zone-x40-k1" in names
    assert names[-3:] == ["slip-flat10", "slip-flat15", "slip-proportional"]


def test_zone_stop_sits_beyond_the_level_and_the_floor_widens_a_tight_wick():
    trap, failure, levels, tick = _tight_pair()
    # Pierce the level so the wick stop is further than the zone stop.
    pierced = _bar(
        0,
        10000.05,
        10000.08,
        9990.0,
        10000.04,
        [
            (9990.0, 0.1, 10),
            (9990.01, 0.1, 10),
            (9990.02, 0.1, 10),
            (10000.08, 1, 0),
        ],
        delta=-20,
        cvd=-20,
    )
    wide = _params(trap_zone_bps=20)
    # 10 points under 10000 is 10 bps, inside a 20 bp zone.
    zone = check_trapped(
        "long", pierced, failure, [pierced, failure], 1, levels, _params(trap_stop_anchor="zone", trap_zone_bps=20), tick
    )
    wick = check_trapped("long", pierced, failure, [pierced, failure], 1, levels, wide, tick)
    assert not isinstance(zone, str) and not isinstance(wick, str)
    assert zone.stop < 10000
    assert wick.stop < pierced.l
    assert zone.stop > wick.stop
    raw = check_trapped("long", trap, failure, [trap, failure], 1, levels, _params(), tick)
    assert not isinstance(raw, str)
    raw_bps = (raw.entry - raw.stop) / raw.entry * 10_000
    assert raw_bps < 12
    floored = check_trapped(
        "long",
        trap,
        failure,
        [trap, failure],
        1,
        levels,
        _params(trap_min_stop_bps=20, trap_min_stop_atr=0),
        tick,
    )
    assert not isinstance(floored, str)
    assert (floored.entry - floored.stop) / floored.entry * 10_000 == pytest.approx(20, abs=0.05)
    assert floored.stop < raw.stop


def test_min_distance_is_the_max_of_bps_and_atr_and_ignores_the_failure_bar():
    trap, failure, levels, tick = _tight_pair()
    history = _ranged(14, -14 * 60, width=50.0)
    bars = history + [trap, failure]
    huge = _bar(60, 10000, 11000, 10000, 10000.06, [(10000.06, 8, 0)], delta=8, cvd=-12)
    params = _params(trap_min_stop_bps=20, trap_min_stop_atr=0.5)
    signal = check_trapped("long", trap, failure, bars, len(bars) - 1, levels, params, tick)
    assert not isinstance(signal, str)
    atr = footprint_atr(bars, len(bars) - 2)
    assert atr is not None and atr == pytest.approx(50, rel=0.15)
    need = max(signal.entry * 20 / 10_000, 0.5 * atr)
    assert signal.entry - signal.stop == pytest.approx(need, rel=0.02)
    # The failure bar's 1000-point range is not in that ATR.
    atr_with_failure = footprint_atr(history + [trap, huge], len(history) + 1)
    assert atr_with_failure > atr + 10
    # ATR missing: the bps floor still applies. A larger bps wins over a small ATR.
    only = check_trapped(
        "long",
        trap,
        failure,
        [trap, failure],
        1,
        levels,
        _params(trap_min_stop_bps=40, trap_min_stop_atr=1.0),
        tick,
    )
    assert footprint_atr([trap, failure], 0) is None
    assert not isinstance(only, str)
    assert (only.entry - only.stop) / only.entry * 10_000 == pytest.approx(40, abs=0.05)
    small_atr = check_trapped(
        "long",
        trap,
        failure,
        _ranged(14, -14 * 60, width=10.0) + [trap, failure],
        15,
        levels,
        _params(trap_min_stop_bps=40, trap_min_stop_atr=0.5),
        tick,
    )
    assert not isinstance(small_atr, str)
    assert (small_atr.entry - small_atr.stop) / small_atr.entry * 10_000 == pytest.approx(40, abs=0.5)


def test_proportional_slip_shrinks_the_reserve_and_changes_r():
    trap, failure, levels, tick = _tight_pair()
    signal = check_trapped("long", trap, failure, [trap, failure], 1, levels, _params(), tick)
    assert not isinstance(signal, str)
    stop_bps = (signal.entry - signal.stop) / signal.entry * 10_000
    assert stop_bps < 15
    proportional = reserved_slip_bps(
        "BTC", signal.entry, signal.stop, _params(trap_slip_mode="proportional")
    )
    assert proportional == pytest.approx(stop_bps)
    assert proportional < 25
    assert reserved_slip_bps("xyz:SP500", signal.entry, signal.stop, _params()) == pytest.approx(30)
    equity = 10_000.0
    fees = conservative_fees("BTC")
    wide = _params()
    tight = _params(trap_slip_bps=10)
    size_wide, budget_wide = size_swing(
        "BTC", signal.entry, signal.stop, equity, wide, risk_pct=0.01, slip_bps=25
    )
    size_tight, budget_tight = size_swing(
        "BTC", signal.entry, signal.stop, equity, tight, risk_pct=0.01, slip_bps=10
    )
    assert budget_tight / size_tight < budget_wide / size_wide
    assert budget_wide <= equity * 0.01 + 1e-4
    assert budget_tight <= equity * 0.01 + 1e-4
    target = signal.entry + (signal.entry - signal.stop)
    net_wide = exit_net(
        side="long", size=size_wide, entry=signal.entry, exit_px=target,
        reason="tp", maker_fee=fees.maker, taker_fee=fees.taker, slip_bps=25,
    )
    net_tight = exit_net(
        side="long", size=size_tight, entry=signal.entry, exit_px=target,
        reason="tp", maker_fee=fees.maker, taker_fee=fees.taker, slip_bps=10,
    )
    assert net_tight / budget_tight > net_wide / budget_wide


def test_a_stop_cannot_lose_more_than_two_percent_or_the_budget():
    equity = 5_000.0
    entry = 10_000.0
    stop = entry * (1.0 - 0.0001)
    params = _params(trap_slip_bps=30)
    size, budget = size_swing("BTC", entry, stop, equity, params, risk_pct=0.02, slip_bps=30)
    fees = conservative_fees("BTC")
    paid = -exit_net(
        side="long", size=size, entry=entry, exit_px=stop, reason="stop",
        maker_fee=fees.maker, taker_fee=fees.taker, slip_bps=30,
    )
    assert paid <= equity * 0.02 + 1e-6
    assert paid <= budget + 1e-6
    short_stop = entry * (1.0 + 0.0001)
    size_s, budget_s = size_swing(
        "xyz:SP500", entry, short_stop, equity, params, risk_pct=0.02, slip_bps=30
    )
    fees_x = conservative_fees("xyz:SP500")
    paid_s = -exit_net(
        side="short", size=size_s, entry=entry, exit_px=short_stop, reason="stop",
        maker_fee=fees_x.maker, taker_fee=fees_x.taker, slip_bps=30,
    )
    assert paid_s <= equity * 0.02 + 1e-6
    assert paid_s <= budget_s + 1e-4


def test_summary_r_is_the_budget_and_marks_are_separate():
    params = SwingParams(entry="trapped", flow="off", hold_days=3, fill_hours=24, trap_slip_bps=25)
    btc = Working(
        coin="BTC", side="long", signal_ts=1_000, expire_ts=90_000,
        entry=100.0, stop=90.0, take_profit=120.0, size=1.0, level=100.0,
        sources="PDL", absorption=99.0, delta=1.0, slope=1.0, config="t",
    )
    eth = Working(
        coin="ETH", side="long", signal_ts=1_000, expire_ts=90_000,
        entry=100.0, stop=90.0, take_profit=120.0, size=1.0, level=100.0,
        sources="PDL", absorption=99.0, delta=1.0, slope=1.0, config="t",
    )
    prints = {
        "BTC": [TapePrint(1_100, 100.0, 1, "sell"), TapePrint(1_200, 120.0, 1, "buy")],
        "ETH": [TapePrint(1_100, 100.0, 1, "sell"), TapePrint(1_300, 101.0, 1, "buy")],
    }
    rows, _counts = simulate([btc, eth], prints, params, 10_000, 0.01)
    by_coin = {row["coin"]: row for row in rows}
    assert by_coin["BTC"]["reason"] == "tp"
    assert by_coin["ETH"]["reason"] == "tape_end"
    for row in rows:
        assert float(row["r"]) == pytest.approx(float(row["net"]) / float(row["budget"]))
        price_risk = float(row["size"]) * abs(float(row["entry"]) - float(row["stop"]))
        assert float(row["budget"]) > price_risk
    split = expectancy_split(rows)
    assert split["n_closed"] == 1 and split["n_marks"] == 1
    assert split["e_closed"] == pytest.approx(float(by_coin["BTC"]["r"]))
    assert split["e_marks"] == pytest.approx(float(by_coin["ETH"]["r"]))
    assert split["e_with_marks"] == pytest.approx(
        (float(by_coin["BTC"]["r"]) + float(by_coin["ETH"]["r"])) / 2
    )


def test_paper_close_pays_the_slip_the_arm_reserved():
    assert exit_slip_bps("BTC", SwingParams(), None) == pytest.approx(25)
    assert exit_slip_bps("xyz:GOLD", SwingParams(), None) == pytest.approx(30)
    assert exit_slip_bps("BTC", SwingParams(), 10) == pytest.approx(10)
    book = ThesisBook()
    intent = AloIntent(
        coin="BTC",
        side="long",
        limit_px=100.0,
        size=1.0,
        stop=99.0,
        take_profit=104.0,
        swing_id="trap-1",
        tif="Alo",
        leverage=20,
        sweep_px=99.5,
        tick=0.1,
        pool_px=104.0,
        exit_slip_bps=10.0,
    )
    book.post(intent, 0.0)
    pos = book.try_fill_from_prints(
        [TradePrint(ts=1.0, coin="BTC", price=100.0, size=1.0, side="sell")],
        partial=True,
    )
    assert pos is not None
    assert pos.exit_slip_bps == pytest.approx(10)
    closed = book.force_flat("BTC", 101.0, reason="tp")
    assert closed is not None and closed.exit_slip_bps == pytest.approx(10)
    Settings(
        entry_mode="model_b",
        model_b_style="swing",
        risk_per_trade=0.01,
        leverage=20,
        model_b_swing_trap_stop_anchor="zone",
        model_b_swing_trap_min_stop_bps=30,
        model_b_swing_trap_min_stop_atr=0.5,
        model_b_swing_trap_slip_bps=10,
        model_b_swing_trap_slip_mode="proportional",
    ).validate()
    with pytest.raises(ValueError, match="TRAP_STOP_ANCHOR"):
        Settings(
            entry_mode="model_b",
            model_b_style="swing",
            risk_per_trade=0.01,
            leverage=20,
            model_b_swing_trap_stop_anchor="wick",
        ).validate()
    # place_trap_stop rejects a stop that is not beyond the anchor.
    assert place_trap_stop("long", 100, 100, 100, 0, _params(trap_stop_bps=0), None) is None

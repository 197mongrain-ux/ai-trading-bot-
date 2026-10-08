"""TP next pool + too-far trend rule (Chris, Oct 8 10:47 / 10:48 ET).

"Have the bot target next pool if under 1.5" and "if next pool is too far
skip unless on the good trend side". GOLD short Oct 8 10:30 armed with the
TP at exactly 1.0R (entry 4138.5, stop 4145.7, TP 4131.3).

- Nearest liquidity TP under MODEL_B_TP_MIN_POOL_R (1.5R, same fee/R floor
  math as the 1R pick) -> the first real level at >= 1.5R.
- That level past MODEL_B_TP_MAX_POOL_R (3R), or no level at 1.5R at all:
  kept only WITH the 15m/1h trend; otherwise skipped
  (TP_TOO_FAR_COUNTERTREND / TP_UNDER_1_5R_COUNTERTREND) unless
  MODEL_B_TP_FAR_SKIP_COUNTERTREND=0.
- MODEL_B_TP_MIN_POOL_R=0 is the old code path exactly.
- Stops, size, leverage, margin caps, gates and the guard do not change.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

import hl_bot.strategy.model_b.engine as engine_mod
from hl_bot.config import load_settings
from hl_bot.execution.model_b_loop import build_model_b_engine
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.risk import (
    HARD_MAX_LOSS_PCT,
    loss_at_stop,
    min_tp_distance,
)
from hl_bot.strategy.model_b.trend import TfTrend, TrendRead
from tests.test_model_b_oct7_replay import EQUITY, SETUPS, TOMORROW_ENV
from tests.test_model_b_two_sided import _eval, _setup

XYZ_LONG = "XYZ100 L 10-07 10:05"  # nearest 31005 (1.02R) -> 31034 (1.56R)
SOL_SHORT = "SOL S 10-07 00:26"  # nearest 118.0 (1.12R) -> 117.85 (1.72R)
# The TPs the live bot armed on Oct 7 (journal), i.e. the old picker.
OLD_TP = {
    "ETH L 10-06 23:47": 2616.2,
    "SOL S 10-07 00:26": 118.0,
    "PUMP S 10-07 08:04": 0.006382,
    "XYZ100 L 10-07 08:16": 31062.0,
    "XYZ100 L 10-07 10:05": 31005.0,
    "XYZ100 L 10-07 12:38": 31128.0,
}


def _settings(tmp_path, **env):
    # load_settings exports the file into os.environ: always write all three
    # knobs so one call cannot leak into the next.
    env = {
        "MODEL_B_TP_MIN_POOL_R": 1.5,
        "MODEL_B_TP_MAX_POOL_R": 3.0,
        "MODEL_B_TP_FAR_SKIP_COUNTERTREND": 1,
        # These cases lock the next-pool walk on the PR #14 level list.
        # Untaken filtering is covered in test_model_b_tp_targeting.py.
        "MODEL_B_TP_UNTAKEN_ONLY": 0,
        "MODEL_B_TP_RUNNER": 0,
        **env,
    }
    path = tmp_path / "e.env"
    extra = "".join(f"{k}={v}\n" for k, v in env.items())
    path.write_text(TOMORROW_ENV + extra)
    return load_settings(env_file=str(path))


def _run(tmp_path, label, **env):
    setup = _setup(label)
    d, _ = _eval(build_model_b_engine(_settings(tmp_path, **env), (setup[1],)), setup)
    return d


def _r(intent):
    return abs(intent.take_profit - intent.limit_px) / abs(intent.limit_px - intent.stop)


def _fake_trend(monkeypatch, m15: str, h1: str):
    read = TrendRead((TfTrend("15m", m15), TfTrend("1h", h1)), "flat")
    monkeypatch.setattr(engine_mod, "read_trend", lambda bars, now: read)


def test_knobs_default_and_parse(tmp_path):
    bare = tmp_path / "bare.env"
    bare.write_text(TOMORROW_ENV)
    d = load_settings(env_file=str(bare))
    assert (d.model_b_tp_min_pool_r, d.model_b_tp_max_pool_r, d.model_b_tp_far_skip_countertrend) == (1.5, 3.0, True)
    s = _settings(tmp_path)
    assert s.model_b_tp_min_pool_r == 1.5
    assert s.model_b_tp_max_pool_r == 3.0
    assert s.model_b_tp_far_skip_countertrend is True
    e = build_model_b_engine(s, ("SOL",))
    assert (e.tp_min_pool_r, e.tp_max_pool_r, e.tp_far_skip_countertrend) == (1.5, 3.0, True)
    off = _settings(tmp_path, MODEL_B_TP_MIN_POOL_R=0, MODEL_B_TP_FAR_SKIP_COUNTERTREND=0)
    assert off.model_b_tp_min_pool_r == 0.0 and off.model_b_tp_far_skip_countertrend is False
    with pytest.raises(ValueError):
        _settings(tmp_path, MODEL_B_TP_MIN_POOL_R=-1)
    # A bare engine (old callers / tests) is off.
    assert ModelBEngine().tp_min_pool_r == 0.0


@pytest.mark.parametrize("label", [XYZ_LONG, SOL_SHORT], ids=["long", "short"])
def test_nearest_under_1_5r_walks_to_the_next_pool(label, tmp_path, caplog):
    old = _run(tmp_path, label, MODEL_B_TP_MIN_POOL_R=0)
    with caplog.at_level(logging.INFO):
        new = _run(tmp_path, label)
    assert old.armed and new.armed, (old.fail_reason, new.fail_reason)
    o, n = old.intent, new.intent
    assert o.take_profit == pytest.approx(OLD_TP[label]) and _r(o) < 1.5
    # Next real level at >= 1.5R (same fee/R math), on the trade's side.
    gap = abs(n.take_profit - n.limit_px)
    assert gap + 1e-12 >= min_tp_distance(n.limit_px, n.stop, min_r=1.5)
    assert (n.take_profit > o.take_profit) if o.side == "long" else (n.take_profit < o.take_profit)
    assert n.pool_px == pytest.approx(n.take_profit)  # locked at fill as before
    # Only the target moved: entry, stop, size, leverage are identical.
    assert replace(
        n,
        take_profit=o.take_profit,
        pool_px=o.pool_px,
        runner_px=o.runner_px,
        runner_mode=o.runner_mode,
    ) == o
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("MODEL_B TP_NEXT_POOL ") for m in msgs)
    assert any(m.startswith("MODEL_B TREND ") and "with_side=" in m for m in msgs)
    assert new.trend is not None and new.to_log()["trend"] == new.trend


def test_expected_next_pools_on_the_oct7_setups(tmp_path):
    assert _run(tmp_path, XYZ_LONG).intent.take_profit == pytest.approx(31034.0)
    assert _run(tmp_path, SOL_SHORT).intent.take_profit == pytest.approx(117.85)


def test_nearest_at_or_past_the_min_r_is_unchanged(tmp_path, caplog):
    # XYZ100 10:05 nearest is 1.0185R: with the knob at 1.0 it already
    # qualifies, so nothing moves and no TP_ line is written.
    old = _run(tmp_path, XYZ_LONG, MODEL_B_TP_MIN_POOL_R=0)
    with caplog.at_level(logging.INFO):
        same = _run(tmp_path, XYZ_LONG, MODEL_B_TP_MIN_POOL_R=1.0)
    assert same.intent == old.intent
    assert not any(
        "MODEL_B TP_" in r.getMessage() and "TP_RUNNER" not in r.getMessage()
        for r in caplog.records
    )


def test_no_pool_at_min_r_keeps_nearest_and_logs_when_skip_is_off(tmp_path, caplog):
    # SOL's furthest level is ~5.3R: at 6R nothing qualifies.
    old = _run(tmp_path, SOL_SHORT, MODEL_B_TP_MIN_POOL_R=0)
    with caplog.at_level(logging.INFO):
        kept = _run(
            tmp_path, SOL_SHORT, MODEL_B_TP_MIN_POOL_R=6, MODEL_B_TP_FAR_SKIP_COUNTERTREND=0
        )
    assert kept.armed and kept.intent == old.intent
    line = next(r.getMessage() for r in caplog.records if "TP_UNDER_1_5R " in r.getMessage())
    assert line.startswith("MODEL_B TP_UNDER_1_5R SOL short r=1.12")


def test_no_pool_at_min_r_with_trend_keeps_nearest(tmp_path, monkeypatch):
    _fake_trend(monkeypatch, "down", "range")  # short is with the trend
    old = _run(tmp_path, SOL_SHORT, MODEL_B_TP_MIN_POOL_R=0)
    kept = _run(tmp_path, SOL_SHORT, MODEL_B_TP_MIN_POOL_R=6)
    assert kept.armed and kept.intent == old.intent


@pytest.mark.parametrize("m15,h1", [("range", "range"), ("up", "range"), ("down", "up")])
def test_no_pool_at_min_r_counter_or_range_skips(m15, h1, tmp_path, monkeypatch, caplog):
    _fake_trend(monkeypatch, m15, h1)
    with caplog.at_level(logging.INFO):
        d = _run(tmp_path, SOL_SHORT, MODEL_B_TP_MIN_POOL_R=6)
    assert not d.armed and d.intent is None
    assert d.fail_reason == "TP_UNDER_1_5R_COUNTERTREND"
    assert d.trend is not None and "with_side=" in d.trend
    assert any("MODEL_B TP_UNDER_1_5R_COUNTERTREND SOL short" in r.getMessage() for r in caplog.records)


def test_too_far_next_pool_counter_trend_skips(tmp_path, monkeypatch, caplog):
    # Max 1.55R makes the 1.56R next pool "too far"; long vs a down read.
    _fake_trend(monkeypatch, "down", "down")
    with caplog.at_level(logging.INFO):
        d = _run(tmp_path, XYZ_LONG, MODEL_B_TP_MAX_POOL_R=1.55)
    assert not d.armed and d.fail_reason == "TP_TOO_FAR_COUNTERTREND"
    assert any("MODEL_B TP_TOO_FAR_COUNTERTREND xyz:XYZ100 long" in r.getMessage() for r in caplog.records)


def test_too_far_next_pool_with_trend_keeps_the_far_pool(tmp_path, monkeypatch):
    _fake_trend(monkeypatch, "up", "range")
    d = _run(tmp_path, XYZ_LONG, MODEL_B_TP_MAX_POOL_R=1.55)
    # No pool cap existed before (liquidity is kept past 2R): the far pool.
    assert d.armed and d.intent.take_profit == pytest.approx(31034.0)


def test_too_far_skip_off_keeps_the_far_pool(tmp_path, monkeypatch):
    _fake_trend(monkeypatch, "down", "down")
    d = _run(tmp_path, XYZ_LONG, MODEL_B_TP_MAX_POOL_R=1.55, MODEL_B_TP_FAR_SKIP_COUNTERTREND=0)
    assert d.armed and d.intent.take_profit == pytest.approx(31034.0)


def test_under_max_r_walks_regardless_of_trend(tmp_path, monkeypatch):
    _fake_trend(monkeypatch, "down", "down")  # counter-trend long
    d = _run(tmp_path, XYZ_LONG)  # 1.56R < 3R: not too far
    assert d.armed and d.intent.take_profit == pytest.approx(31034.0)


@pytest.mark.parametrize("label", list(OLD_TP))
def test_knob_zero_is_the_old_tp_exactly(label, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        d = _run(tmp_path, label, MODEL_B_TP_MIN_POOL_R=0)
    assert d.armed and d.intent.take_profit == pytest.approx(OLD_TP[label])
    assert d.trend is None and "trend" not in d.to_log()
    msgs = [r.getMessage() for r in caplog.records]
    assert not any(
        ("MODEL_B TP_" in m and "TP_RUNNER" not in m) or "MODEL_B TREND" in m for m in msgs
    )


def test_widened_stop_keeps_its_1_67r_pool_floor_and_size_cap(tmp_path, caplog):
    """No change to size, leverage or margin caps; moved-stop floor unchanged.

    A sweep one tick past the swing gives a very tight liquidity stop that the
    15 bps minimum moves out; that path's TP floor (tp_r = 1.67R) is not
    touched by the new rule, and size stays inside the 2% cap and 20x.
    """
    setup = list(_setup(SOL_SHORT))
    setup[5] = setup[4] + setup[10]
    setup = tuple(setup)
    off = _eval(build_model_b_engine(_settings(tmp_path, MODEL_B_TP_MIN_POOL_R=0), ("SOL",)), setup)[0]
    with caplog.at_level(logging.INFO):
        on = _eval(build_model_b_engine(_settings(tmp_path), ("SOL",)), setup)[0]
    assert off.armed and on.armed, (off.fail_reason, on.fail_reason)
    assert on.intent == off.intent
    i = on.intent
    assert abs(i.limit_px - i.stop) / i.limit_px * 10_000 >= 15.0 - 1e-9
    assert abs(i.take_profit - i.limit_px) + 1e-9 >= 1.67 * abs(i.limit_px - i.stop)
    assert loss_at_stop(i.size, i.limit_px, i.stop, include_fees=True) <= EQUITY * HARD_MAX_LOSS_PCT + 1e-9
    assert i.size * i.limit_px <= 20 * EQUITY + 1e-6
    assert not any(
        "MODEL_B TP_" in r.getMessage() and "TP_RUNNER" not in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("label", list(OLD_TP))
def test_stop_size_leverage_unchanged_on_every_oct7_setup(label, tmp_path):
    old = _run(tmp_path, label, MODEL_B_TP_MIN_POOL_R=0).intent
    new = _run(tmp_path, label).intent
    assert (new.limit_px, new.stop, new.size, new.leverage) == (old.limit_px, old.stop, old.size, old.leverage)
    assert loss_at_stop(new.size, new.limit_px, new.stop, include_fees=True) <= EQUITY * HARD_MAX_LOSS_PCT + 1e-9

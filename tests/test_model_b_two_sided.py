"""Two-sided hunt (MODEL_B_TWO_SIDED, Chris Oct 8 10:10 ET).

The nearest draw pool used to pick the only side Model B looked at. BTC
logged ``bias=long pool=PDH@84369 ... reason=NO_SWEEP`` every cycle while
15m made lower highs and price reclaimed back under a swept high, so the
short was never evaluated. With the flag on, every cycle evaluates the
long AND the short with the same gates, SL/TP, sizing and caps; the pool
is only that side's TP target. Flag off is the old code path exactly.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from hl_bot.config import load_settings
from hl_bot.execution.model_b_loop import build_model_b_engine, format_model_b_fail
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import HARD_MAX_LOSS_PCT, loss_at_stop
from hl_bot.strategy.model_b.structure import TfStructure
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.types import Pool
from tests.test_model_b_oct7_replay import EQUITY, SETUPS, TOMORROW_ENV, _bars, _tape


def _settings(tmp_path, two_sided: str | None):
    env = tmp_path / "e.env"
    text = TOMORROW_ENV + (f"MODEL_B_TWO_SIDED={two_sided}\n" if two_sided is not None else "")
    env.write_text(text)
    return load_settings(env_file=str(env))


def _setup(label):
    return next(s for s in SETUPS if s[0] == label)


def _eval(engine, setup, pools=None):
    label, coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick, lev, _stop = setup
    prints, last = _tape(coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick)
    bars = _bars(coin, now)
    if pools is None:
        pools = pools_from_bars(bars, now, last_price=last)
    return engine.evaluate(
        coin, now=now, prints=prints, bars=bars, pools=pools,
        best_bid=last - tick, best_ask=last + tick, equity=EQUITY, tick=tick, leverage=lev,
    ), last


SOL_SHORT = "SOL S 10-07 00:26"


def test_flag_defaults_on_and_zero_turns_it_off(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_B_TWO_SIDED", raising=False)
    assert _settings(tmp_path, None).model_b_two_sided is True
    assert build_model_b_engine(_settings(tmp_path, None), ("SOL",)).two_sided is True
    off = _settings(tmp_path, "0")
    assert off.model_b_two_sided is False
    assert build_model_b_engine(off, ("SOL",)).two_sided is False


def test_short_arms_on_sweep_above_and_reclaim_below_even_when_the_pool_says_long(tmp_path):
    setup = _setup(SOL_SHORT)
    _label, coin, now, side, swing, sweep, *_rest, stop = setup
    _, last = _eval(build_model_b_engine(_settings(tmp_path, "1"), (coin,)), setup)
    # A draw pool just above price: the old bias says long only.
    pools = [Pool("PDH", round(last + 0.05, 2), False), Pool("PDL", 100.0, False)]

    old, _ = _eval(build_model_b_engine(_settings(tmp_path, "0"), (coin,)), setup, pools)
    assert old.bias == "long" and not old.armed  # the short was never looked at

    new, _ = _eval(build_model_b_engine(_settings(tmp_path, "1"), (coin,)), setup, pools)
    assert new.bias == "long"  # still logged: it is the draw, not the gate
    assert new.armed and new.side == "short" and new.intent.side == "short"
    assert new.swing == swing and new.sweep_price == pytest.approx(sweep)
    assert new.intent.limit_px == pytest.approx(sweep)
    assert new.intent.stop == pytest.approx(stop)  # same liquidity stop as Oct 7
    assert new.intent.take_profit < new.intent.limit_px
    assert [o.side for o in new.other_sides] == ["long"]
    assert not new.other_sides[0].armed


@pytest.mark.parametrize("label", [s[0] for s in SETUPS])
def test_oct7_setups_arm_identically_with_and_without_the_flag(label, tmp_path):
    setup = _setup(label)
    coin = setup[1]
    off, _ = _eval(build_model_b_engine(_settings(tmp_path, "0"), (coin,)), setup)
    on, _ = _eval(build_model_b_engine(_settings(tmp_path, "1"), (coin,)), setup)
    assert off.armed and on.armed, (label, off.fail_reason, on.fail_reason)
    # Same side, entry, stop, TP and size: nothing about sizing / SL / TP moved.
    assert on.intent == off.intent
    assert on.side == off.intent.side


def test_flag_off_is_the_old_decision_and_log_line_exactly(tmp_path):
    setup = _setup("XYZ100 L 10-07 10:05")
    coin = setup[1]
    d, _ = _eval(build_model_b_engine(_settings(tmp_path, "0"), (coin,)), setup)
    assert d.side is None and d.other_sides == ()
    assert "side" not in d.to_log() and "other_sides" not in d.to_log()
    failed = replace(d, armed=False, intent=None, fail_reason="NO_SWEEP")
    assert format_model_b_fail(failed).startswith(f"MODEL_B FAIL {coin} bias=")
    # A bare engine (tests, old callers) is also off by default.
    assert ModelBEngine().two_sided is False


def test_fail_lines_carry_the_side_one_per_side(tmp_path):
    setup = _setup(SOL_SHORT)
    coin = setup[1]
    on, _ = _eval(build_model_b_engine(_settings(tmp_path, "1"), (coin,)), setup)
    lines = [format_model_b_fail(on)] + [format_model_b_fail(o) for o in on.other_sides]
    assert lines[0].startswith(f"MODEL_B FAIL {coin} side=short ")
    assert lines[1].startswith(f"MODEL_B FAIL {coin} side=long ")
    row = on.to_log()
    assert row["side"] == "short"
    assert row["other_sides"][0]["side"] == "long"


def test_one_ticket_per_coin_resting_long_blocks_the_short(tmp_path):
    setup = _setup(SOL_SHORT)
    coin = setup[1]
    settings = _settings(tmp_path, "1")
    engine = build_model_b_engine(settings, (coin,))
    first, _ = _eval(engine, setup)
    assert first.armed
    # Rest that ticket; a second evaluation on the same coin cannot arm
    # either side while it works.
    engine.thesis.post(first.intent, now=setup[2], oid=1)
    second, _ = _eval(engine, setup)
    assert not second.armed
    reasons = {second.fail_reason} | {o.fail_reason for o in second.other_sides}
    assert reasons <= {"SECOND_ALO", "NO_SWEEP", "NO_RECLAIM", "NO_SWING", "ABSORB", "DELTA", "LAST_15s"}
    assert "SECOND_ALO" in reasons
    with pytest.raises(ValueError):
        engine.thesis.post(replace(first.intent, side="long"), now=setup[2], oid=2)


def _armed(side, absorb=1.0, score=5):
    d, _ = _eval(ModelBEngine(two_sided=False, thesis=ThesisBook()), _setup(SOL_SHORT))
    return replace(d, side=side, armed=True, absorb=absorb, score=score,
                   intent=replace(d.intent, side=side) if d.intent else None)


@pytest.mark.parametrize(
    "states,absorb_long,absorb_short,winner",
    [
        # Structure decides first: bear 15m+1h -> short, even with less absorb.
        ([("15m", "bear"), ("1h", "bear")], 9.0, 1.0, "short"),
        ([("15m", "bull"), ("1h", "range")], 1.0, 9.0, "long"),
        # Structure split / range -> higher absorb.
        ([("15m", "bull"), ("1h", "bear")], 1.0, 9.0, "short"),
        ([("15m", "range"), ("1h", "range")], 3.0, 2.0, "long"),
        # Full tie -> long (the old NONE tie rule).
        ([("15m", "range"), ("1h", "range")], 2.0, 2.0, "long"),
    ],
)
def test_both_sides_pass_tie_break(states, absorb_long, absorb_short, winner):
    long_d = _armed("long", absorb_long)
    short_d = _armed("short", absorb_short)
    sts = [TfStructure(tf, st) for tf, st in states]
    chosen = ModelBEngine._pick_two_sided([long_d, short_d], lambda: sts)
    assert chosen.side == winner and chosen.intent.side == winner
    assert len(chosen.other_sides) == 1 and chosen.other_sides[0].side != winner


def test_neither_side_arms_reports_the_furthest_side():
    a = replace(_armed("long"), armed=False, intent=None, fail_reason="NO_SWEEP")
    b = replace(_armed("short"), armed=False, intent=None, fail_reason="COUNTER_FLOW")
    chosen = ModelBEngine._pick_two_sided([a, b], lambda: [])
    assert chosen.side == "short" and chosen.fail_reason == "COUNTER_FLOW"
    assert chosen.other_sides[0].fail_reason == "NO_SWEEP"


def test_size_cap_still_holds_on_a_two_sided_short_with_a_very_tight_stop(tmp_path):
    """No change to size, leverage or margin caps: a 1-tick stop short."""
    setup = list(_setup(SOL_SHORT))
    # Sweep only one tick past the swing -> the liquidity stop is tight and
    # is moved/braked by min_stop_bps; size stays inside 2% and 20x.
    setup[5] = setup[4] + setup[10]
    setup = tuple(setup)
    d, _ = _eval(build_model_b_engine(_settings(tmp_path, "1"), (setup[1],)), setup)
    assert d.armed and d.intent.side == "short", d.fail_reason
    i = d.intent
    # The 1-tick liquidity stop is moved out past 15 bps; size stays capped.
    assert abs(i.limit_px - i.stop) / i.limit_px * 10_000 >= 15.0 - 1e-9
    assert loss_at_stop(i.size, i.limit_px, i.stop, include_fees=True) <= EQUITY * HARD_MAX_LOSS_PCT + 1e-9
    assert i.size * i.limit_px <= 20 * EQUITY + 1e-6


def test_loop_logs_one_fail_line_per_side(tmp_path, caplog):
    from hl_bot.execution.model_b_loop import run_model_b
    from tests.test_model_b import _live_account_settings, _now
    from tests.test_model_b_protection import FakeLive, _arm_world

    now = _now()
    info, feed = _arm_world(now)
    # Pool at 130 above -> bias long; the long fixture arms and the short
    # side (no sweep above a swing high) logs its own FAIL line.
    with caplog.at_level(logging.INFO):
        run_model_b(
            _live_account_settings(tmp_path, "ts.jsonl", model_b_two_sided=True),
            max_iterations=1, info=info, feed=feed, exchange=FakeLive(),
            sleep_fn=lambda _s: None, now_fn=lambda: now, connect_feed=False,
            coins=("BTC",), pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("two_sided=1" in m for m in msgs if m.startswith("MODEL_B FILTERS"))
    arms = [m for m in msgs if m.startswith("MODEL_B ARM BTC")]
    fails = [m for m in msgs if m.startswith("MODEL_B FAIL BTC side=")]
    # The long fixture arms; the short side logs exactly one FAIL line.
    assert len(arms) == 1 and " long " in arms[0]
    assert len(fails) == 1 and fails[0].startswith("MODEL_B FAIL BTC side=short ")

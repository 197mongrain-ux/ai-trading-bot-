"""Macro side filter (Chris, Oct 8 11:05 / 11:07 ET).

"trend identification 1h and 4h. new rule. trade only on the side of macro"
and "average directional index to use for trend id". ADX(14, Wilder) with
+DI/-DI on 1h and 4h: macro up -> longs only, down -> shorts only, range ->
MODEL_B_MACRO_RANGE_POLICY. MODEL_B_MACRO_SIDE_ONLY=1 blocks (FAIL reason
MACRO_SIDE), shadow logs would_block only, 0 is the old path exactly.
No change to stops, TP, size, leverage, margin caps or the guard.
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import hl_bot.strategy.model_b.engine as engine_mod
from hl_bot.config import load_settings
from hl_bot.execution.model_b_loop import _macro_bars, build_model_b_engine, format_model_b_fail
from hl_bot.strategy.model_b.risk import HARD_MAX_LOSS_PCT, loss_at_stop
from hl_bot.strategy.model_b.trend import (
    AdxTrend,
    MacroRead,
    adx_series,
    adx_state,
    combine_macro,
    read_macro,
    tf_adx,
)
from tests.test_model_b_oct7_replay import EQUITY, TOMORROW_ENV
from tests.test_model_b_two_sided import _eval, _setup

SOL_SHORT = "SOL S 10-07 00:26"
XYZ_LONG = "XYZ100 L 10-07 10:05"
FIX = pathlib.Path(__file__).parent / "fixtures" / "oct8_btc_1h.json"
ET = ZoneInfo("America/Toronto")


def _settings(tmp_path, **env):
    env = {
        "MODEL_B_TWO_SIDED": 1,
        "MODEL_B_MACRO_SIDE_ONLY": "1",
        "MODEL_B_MACRO_RANGE_POLICY": "both",
        "MODEL_B_MACRO_ADX_MIN": 20,
        "MODEL_B_MACRO_MODE": "4h_lead",
        **env,
    }
    path = tmp_path / "m.env"
    path.write_text(TOMORROW_ENV + "".join(f"{k}={v}\n" for k, v in env.items()))
    return load_settings(env_file=str(path))


def _macro(macro: str) -> MacroRead:
    st = {"up": "up", "down": "down"}.get(macro, "range")
    return MacroRead(AdxTrend("1h", st, 30.0, 25.0, 10.0), AdxTrend("4h", st, 30.0, 25.0, 10.0), macro)


def _fake(monkeypatch, macro: str):
    calls = []

    def fake(bars, now, **kw):
        calls.append((len(bars), now, kw))
        return _macro(macro)

    monkeypatch.setattr(engine_mod, "read_macro", fake)
    return calls


def _run(tmp_path, label, **env):
    setup = _setup(label)
    engine = build_model_b_engine(_settings(tmp_path, **env), (setup[1],))
    return _eval(engine, setup)[0]


# --- ADX ---------------------------------------------------------------------

def _line(start, step, n, wiggle=0.0, t0=1_790_000_000):
    bars = []
    px = start
    for i in range(n):
        o = px
        px = px + step + (wiggle if i % 2 else -wiggle)
        bars.append({"t": (t0 + i * 3600) * 1000, "o": o, "h": max(o, px) + 1, "l": min(o, px) - 1, "c": px})
    return bars, t0 + n * 3600


def test_adx_up_down_and_chop():
    up, t = _line(100, 2.0, 200)
    r = tf_adx(up, "1h", t)
    assert r.state == "up" and r.adx > 25 and r.plus_di > r.minus_di
    down, t = _line(800, -2.0, 200)
    assert tf_adx(down, "1h", t).state == "down"
    chop, t = _line(100, 0.0, 200, wiggle=3.0)
    c = tf_adx(chop, "1h", t)
    assert c.state == "range" and c.adx < 20
    assert tf_adx(up, "4h", t).state in ("up", "unknown")


def test_adx_threshold_and_not_enough_history():
    assert adx_state(19.9, 30, 10, 20) == "range"
    assert adx_state(20.0, 30, 10, 20) == "up"
    assert adx_state(30.0, 10, 30, 25) == "down"
    short, t = _line(100, 1.0, 20)
    assert tf_adx(short, "1h", t).state == "unknown"
    assert adx_series([]) == []


def test_combine_variants():
    assert combine_macro("unknown", "up", "4h_lead") == "unknown"
    assert combine_macro("range", "unknown", "4h_only") == "unknown"
    assert combine_macro("range", "up", "4h_lead") == "up"
    assert combine_macro("down", "up", "4h_lead") == "range"
    assert combine_macro("down", "down", "4h_lead") == "down"
    assert combine_macro("up", "range", "4h_lead") == "range"
    assert combine_macro("up", "range", "4h_lead_1h_fill") == "up"
    assert combine_macro("range", "up", "both") == "range"
    assert combine_macro("up", "up", "both") == "up"
    assert combine_macro("down", "up", "4h_only") == "up"


def test_btc_real_1h_reads_macro_down_on_oct8():
    raw = json.loads(FIX.read_text())["bars"]
    bars = [{"t": t, "o": o, "h": h, "l": l, "c": c} for t, o, h, l, c in raw]
    now = datetime(2026, 10, 8, 11, 1, tzinfo=ET).timestamp()
    m = read_macro([b for b in bars if b["t"] / 1000 + 3600 <= now], now)
    assert m.h1.state == "down" and m.h4.state == "down" and m.macro == "down"
    assert m.allowed() == ("short",)
    assert m.h4.adx > 25 and m.h4.minus_di > m.h4.plus_di
    assert m.label().startswith("1h=down(adx=")


# --- settings ----------------------------------------------------------------

def test_settings_defaults_and_parse(tmp_path):
    bare = tmp_path / "b.env"
    bare.write_text(TOMORROW_ENV)
    s = load_settings(env_file=str(bare))
    assert (s.model_b_macro_side_only, s.model_b_macro_range_policy, s.model_b_macro_adx_min, s.model_b_macro_mode) == (
        "shadow", "both", 20.0, "4h_lead")
    assert _settings(tmp_path, MODEL_B_MACRO_SIDE_ONLY="1").model_b_macro_side_only == "on"
    assert _settings(tmp_path, MODEL_B_MACRO_SIDE_ONLY="0").model_b_macro_side_only == "off"
    assert _settings(tmp_path, MODEL_B_MACRO_SIDE_ONLY="shadow").model_b_macro_side_only == "shadow"
    with pytest.raises(ValueError):
        _settings(tmp_path, MODEL_B_MACRO_RANGE_POLICY="maybe")
    with pytest.raises(ValueError):
        _settings(tmp_path, MODEL_B_MACRO_SIDE_ONLY="sometimes")
    e = build_model_b_engine(_settings(tmp_path), ("SOL",))
    assert (e.macro_side_only, e.macro_range_policy, e.macro_adx_min) == ("on", "both", 20.0)


# --- the rule ----------------------------------------------------------------

def test_macro_up_only_long_short_blocked(tmp_path, monkeypatch):
    _fake(monkeypatch, "up")
    d = _run(tmp_path, SOL_SHORT)
    assert not d.armed
    sides = {d.side: d} | {o.side: o for o in d.other_sides}
    assert sides["short"].fail_reason == "MACRO_SIDE"
    assert sides["short"].macro.endswith("macro=up allowed=long")
    assert sides["long"].fail_reason != "MACRO_SIDE"  # long still evaluated
    line = format_model_b_fail(sides["short"])
    assert line.startswith("MODEL_B FAIL SOL side=short ") and "reason=MACRO_SIDE" in line
    # The allowed side arms normally.
    up_long = _run(tmp_path, XYZ_LONG)
    assert up_long.armed and up_long.intent.side == "long" and "macro=up" in up_long.macro


def test_macro_down_only_short(tmp_path, monkeypatch):
    _fake(monkeypatch, "down")
    short = _run(tmp_path, SOL_SHORT)
    assert short.armed and short.intent.side == "short"
    long_ = _run(tmp_path, XYZ_LONG)
    assert not long_.armed
    sides = {long_.side: long_} | {o.side: o for o in long_.other_sides}
    assert sides["long"].fail_reason == "MACRO_SIDE"


def test_range_policy_both_trades_both_none_trades_neither(tmp_path, monkeypatch):
    _fake(monkeypatch, "range")
    assert _run(tmp_path, SOL_SHORT).armed
    assert _run(tmp_path, XYZ_LONG).armed
    d = _run(tmp_path, SOL_SHORT, MODEL_B_MACRO_RANGE_POLICY="none")
    assert not d.armed and d.fail_reason == "MACRO_SIDE"
    assert {o.fail_reason for o in d.other_sides} == {"MACRO_SIDE"}


@pytest.mark.parametrize("label", [SOL_SHORT, XYZ_LONG])
def test_allowed_side_intent_is_identical_to_off(label, tmp_path, monkeypatch):
    side_macro = "down" if label == SOL_SHORT else "up"
    _fake(monkeypatch, side_macro)
    on = _run(tmp_path, label)
    off = _run(tmp_path, label, MODEL_B_MACRO_SIDE_ONLY="0")
    assert on.armed and off.armed and on.intent == off.intent


def test_knob_zero_is_the_old_path_exactly(tmp_path, monkeypatch, caplog):
    def boom(*a, **k):
        raise AssertionError("macro must not be read when off")

    monkeypatch.setattr(engine_mod, "read_macro", boom)
    with caplog.at_level(logging.INFO):
        d = _run(tmp_path, SOL_SHORT, MODEL_B_MACRO_SIDE_ONLY="0")
    assert d.armed and d.macro is None and "macro" not in d.to_log()
    assert all(o.macro is None for o in d.other_sides)
    assert not any("MACRO" in r.getMessage() for r in caplog.records)


def test_shadow_logs_would_block_and_does_not_block(tmp_path, monkeypatch, caplog):
    _fake(monkeypatch, "up")  # short is counter-macro
    off = _run(tmp_path, SOL_SHORT, MODEL_B_MACRO_SIDE_ONLY="0")
    with caplog.at_level(logging.INFO):
        sh = _run(tmp_path, SOL_SHORT, MODEL_B_MACRO_SIDE_ONLY="shadow")
    assert sh.armed and sh.intent == off.intent
    assert sh.macro.startswith("would_block ") and sh.to_log()["macro"] == sh.macro
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("MODEL_B SHADOW SOL reason=MACRO_SIDE would_block=1 side=short") for m in msgs)
    assert not any("reason=MACRO_SIDE" in format_model_b_fail(o) for o in sh.other_sides)


def test_macro_line_once_per_coin_per_1h_bar(tmp_path, monkeypatch, caplog):
    calls = _fake(monkeypatch, "down")
    setup = _setup(SOL_SHORT)
    engine = build_model_b_engine(_settings(tmp_path), ("SOL",))
    with caplog.at_level(logging.INFO):
        _eval(engine, setup)
        _eval(engine, setup)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("MODEL_B MACRO SOL ")]
    assert len(lines) == 1 and len(calls) == 1
    assert "1h=down(adx=30.0 +di=25.0 -di=10.0)" in lines[0] and "macro=down allowed=short" in lines[0]
    later = list(setup)
    later[2] = setup[2] + 3600
    engine._macro_read("SOL", [], later[2])
    assert len(calls) == 2


def test_htf_bars_are_used_when_passed(tmp_path, monkeypatch):
    calls = _fake(monkeypatch, "down")
    setup = _setup(SOL_SHORT)
    _label, coin, now, *_ = setup
    engine = build_model_b_engine(_settings(tmp_path), (coin,))
    htf = [{"t": (now - 3600 * (i + 2)) * 1000, "o": 1, "h": 2, "l": 0.5, "c": 1} for i in range(7)]
    engine._macro_cache.clear()
    from tests.test_model_b_oct7_replay import _bars, _tape
    prints, last = _tape(coin, now, *setup[3:10], setup[10])
    engine.evaluate(coin, now=now, prints=prints, bars=_bars(coin, now), pools=[], best_bid=last - 0.01,
                    best_ask=last + 0.01, equity=EQUITY, tick=0.01, leverage=20, htf_bars=htf)
    assert calls[-1][0] == len(htf)


def test_short_history_is_unknown_and_allows_neither_side():
    """Not enough 1h/4h candles is not a range, so both sides stay closed."""
    short, t = _line(100, 1.0, 20)
    read = read_macro(short, t)
    assert read.macro == "unknown"
    assert read.h1.state == "unknown" or read.h4.state == "unknown"
    assert read.allowed() == ()
    assert read.allows("long") is False and read.allows("short") is False
    assert "allowed=none" in read.label()
    ranged = MacroRead(
        AdxTrend("1h", "range", 10.0, 12.0, 11.0),
        AdxTrend("4h", "range", 10.0, 12.0, 11.0),
        "range",
    )
    assert ranged.allowed() == ("long", "short")


def test_unknown_macro_blocks_the_arm(tmp_path, monkeypatch):
    """15:35 ET Oct 8: ETH macro was unknown 9s after restart and the arm went out."""

    def fake(bars, now, **kw):
        return MacroRead(AdxTrend("1h", "unknown"), AdxTrend("4h", "unknown"), "unknown")

    monkeypatch.setattr(engine_mod, "read_macro", fake)
    for mode in ("1", "shadow"):
        d = _run(tmp_path, SOL_SHORT, MODEL_B_MACRO_SIDE_ONLY=mode)
        assert not d.armed and d.intent is None
        reasons = {d.fail_reason} | {o.fail_reason for o in d.other_sides}
        assert reasons == {"MACRO_UNKNOWN"}
        text = format_model_b_fail(d)
        assert "reason=MACRO_UNKNOWN" in text
        assert "macro=unknown" in text and "allowed=none" in text
    off = _run(tmp_path, SOL_SHORT, MODEL_B_MACRO_SIDE_ONLY="0")
    assert off.armed


def test_unknown_macro_is_reread_inside_the_same_hour(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake(bars, now, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return MacroRead(AdxTrend("1h", "unknown"), AdxTrend("4h", "unknown"), "unknown")
        return _macro("down")

    monkeypatch.setattr(engine_mod, "read_macro", fake)
    engine = build_model_b_engine(_settings(tmp_path), ("ETH",))
    now = 1_700_000_000.0
    first = engine._macro_read("ETH", [], now)
    second = engine._macro_read("ETH", [], now + 30)
    assert first.macro == "unknown"
    assert second.macro == "down"
    assert calls["n"] == 2
    third = engine._macro_read("ETH", [], now + 90)
    assert third.macro == "down" and calls["n"] == 2


def test_tight_stop_size_cap_unchanged_with_macro_on(tmp_path, monkeypatch):
    """No change to size, leverage or margin caps (1-tick sweep, moved stop)."""
    _fake(monkeypatch, "down")
    setup = list(_setup(SOL_SHORT))
    setup[5] = setup[4] + setup[10]
    setup = tuple(setup)
    on = _eval(build_model_b_engine(_settings(tmp_path), ("SOL",)), setup)[0]
    off = _eval(build_model_b_engine(_settings(tmp_path, MODEL_B_MACRO_SIDE_ONLY="0"), ("SOL",)), setup)[0]
    assert on.armed and on.intent == off.intent
    i = on.intent
    assert loss_at_stop(i.size, i.limit_px, i.stop, include_fees=True) <= EQUITY * HARD_MAX_LOSS_PCT + 1e-9
    assert i.size * i.limit_px <= 20 * EQUITY + 1e-6


# --- loop candle cache --------------------------------------------------------

class _Info:
    def __init__(self):
        self.calls = []

    def get_candles(self, coin, interval="1m", start_ms=None, end_ms=None):
        self.calls.append((coin, interval, start_ms, end_ms))
        return [{"t": end_ms - 3_600_000, "o": 1, "h": 1, "l": 1, "c": 1}]


def test_macro_bars_fetch_1h_40d_cached_per_bar():
    info, cache = _Info(), {}
    bucket = 497_630
    t = bucket * 3600 + 3300.0  # 55 min into the bar
    _macro_bars(info, "BTC", t, cache)
    coin, interval, start, end = info.calls[0]
    assert interval == "1h" and (end - start) == 40 * 86_400_000
    _macro_bars(info, "BTC", t + 120, cache)
    assert len(info.calls) == 1  # same bar, < 10 min: cached
    _macro_bars(info, "BTC", (bucket + 1) * 3600 + 2.0, cache)
    assert len(info.calls) == 2  # new 1h bar: refetch at once
    _macro_bars(info, "BTC", (bucket + 1) * 3600 + 700.0, cache)
    assert len(info.calls) == 3  # > 10 min old: refresh


def test_macro_bars_failure_keeps_last_good():
    info, cache = _Info(), {}
    t = 1_791_470_000.0
    good = _macro_bars(info, "BTC", t, cache)

    def bad(*a, **k):
        raise RuntimeError("429")

    info.get_candles = bad
    assert _macro_bars(info, "BTC", t + 4000, cache) == good

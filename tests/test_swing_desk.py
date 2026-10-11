"""Desk flags: 10x swing cap, one position, cash-session flatten. Defaults stay off."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hl_bot.config import Settings, load_settings
from hl_bot.strategy.model_b.risk import LEVERAGE, assert_policy
from hl_bot.strategy.model_b.swing import (
    Level,
    Plan,
    SwingParams,
    TP_OVER_MAX,
    _is_major,
    build_levels,
    normalize_hold_overnight,
    overnight_decision,
    parse_session_clock,
    pick_targets,
    session_close_id,
    session_flat_due,
    session_open,
    size_swing,
    swing_slot,
    tighten_to_breakeven,
)
from hl_bot.strategy.model_b.swing_replay import replay


def test_swing_lev_cannot_pass_10x_or_the_two_percent_loss():
    assert LEVERAGE == 20
    assert_policy()
    tight = SwingParams(max_lev=10, slip_main_bps=0.0, slip_xyz_bps=0.0)
    wide = SwingParams(max_lev=20, slip_main_bps=0.0, slip_xyz_bps=0.0)
    entry = 100_000.0
    stop = entry * (1.0 - 0.0001)
    for equity in (250.0, 5_000.0):
        size, loss = size_swing(
            "BTC", entry, stop, equity, tight, risk_pct=0.01, leverage=40, max_leverage=20
        )
        size20, _loss20 = size_swing(
            "BTC", entry, stop, equity, wide, risk_pct=0.01, leverage=40, max_leverage=20
        )
        assert size * entry <= equity * 10 + 1e-6
        assert size20 * entry <= equity * 20 + 1e-6
        assert size20 > size
        assert loss <= equity * 0.01 + 1e-6
        assert loss <= equity * 0.02 + 1e-6
    # A 50 bp stop with the 25 bp paper slip still respects both caps.
    slipped = SwingParams(max_lev=10)
    size_s, loss_s = size_swing(
        "BTC", entry, entry * (1.0 - 0.005), 5_000.0, slipped, risk_pct=0.01, leverage=20
    )
    assert size_s * entry <= 5_000.0 * 10 + 1e-6
    assert loss_s <= 5_000.0 * 0.02 + 1e-4


def test_one_position_keeps_the_higher_quality_setup():
    blocked = swing_slot([(2.0, "BTC"), (9.0, "ETH")], open_coins=["SOL"], pending=[], cap=1)
    assert blocked.limited and blocked.allow == ()
    picked = swing_slot([(2.0, "BTC"), (9.0, "ETH")], open_coins=[], pending=[], cap=1)
    assert picked.allow == ("ETH",)
    kept = swing_slot([(9.0, "ETH")], open_coins=[], pending=[("BTC", 4.0)], cap=1)
    assert kept.allow == ("ETH",)
    assert "BTC" in kept.cancel
    same = swing_slot([(3.0, "ETH")], open_coins=[], pending=[("BTC", 4.0)], cap=1)
    assert same.allow == ()
    assert swing_slot([(1.0, "BTC"), (2.0, "ETH")], open_coins=[], pending=[], cap=0).limited is False


def test_session_is_toronto_cash_hours_and_flattens_at_the_close():
    tz = ZoneInfo("America/Toronto")
    july = datetime(2026, 7, 15, 9, 30, tzinfo=tz)
    january = datetime(2026, 1, 14, 9, 30, tzinfo=tz)
    assert july.weekday() == 2 and january.weekday() == 2
    assert july.astimezone(ZoneInfo("UTC")).hour == 13
    assert january.astimezone(ZoneInfo("UTC")).hour == 14
    assert session_open(july.timestamp(), "09:30-16:00")
    assert session_open(january.timestamp(), "09:30-16:00")
    assert not session_open(datetime(2026, 7, 15, 9, 29, tzinfo=tz).timestamp(), "09:30-16:00")
    close = datetime(2026, 7, 15, 16, 0, tzinfo=tz).timestamp()
    assert not session_open(close, "09:30-16:00")
    assert session_flat_due(close, "09:30-16:00")
    assert session_flat_due(datetime(2026, 7, 18, 12, 0, tzinfo=tz).timestamp(), "09:30-16:00")
    assert not session_flat_due(close, "all")
    assert not session_flat_due(close, "us")
    assert parse_session_clock("09:30-16:00") == (9 * 60 + 30, 16 * 60)
    assert parse_session_clock("all") is None


def _hours(n: int, close0: float) -> list[dict]:
    bars = []
    for i in range(n):
        close = close0 + i * 3600.0
        bars.append(
            {
                "t": (close - 3600.0) * 1000.0,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.2,
                "v": 1.0,
            }
        )
    return bars


def _plan(coin_score: float, level_ts: float) -> Plan:
    return Plan(
        side="long",
        level=100.0,
        level_ts=level_ts,
        entry=100.0,
        stop=90.0,
        take_profit=140.0,
        runner_px=None,
        size=0.01,
        r_multiple=4.0,
        score=coin_score,
        sweep=99.0,
        macro="up",
        sources=("4h_low", "4h_low"),
        touches=2,
        budget=1.0,
        slip_bps=25.0,
    )


def test_replay_flattens_one_position_at_the_session_close(monkeypatch):
    tz = ZoneInfo("America/Toronto")
    arm_at = datetime(2026, 7, 15, 10, 0, tzinfo=tz).timestamp()
    bars = _hours(70, arm_at - 50 * 3600.0)

    def fake(coin, now, *_args, **_kwargs):
        if abs(float(now) - arm_at) > 1:
            return "OFF_SESSION"
        return _plan(9.0 if coin == "ETH" else 2.0, arm_at)

    monkeypatch.setattr("hl_bot.strategy.model_b.swing_replay.plan_trade", fake)
    params = SwingParams(
        confirm_tf="1h",
        flow="off",
        max_positions=1,
        max_lev=10,
        session="09:30-16:00",
        hold_days=3,
    )
    book = {coin: {"1h": bars} for coin in ("BTC", "ETH")}
    summary = replay(book, params, risk_pct=0.01, equity=5_000.0, name="desk")
    assert summary.trades == 1
    trade = summary.blotter[0]
    assert trade.coin == "ETH"
    assert trade.reason == "SESSION_CLOSE"
    assert trade.r != 0.0

    def both(coin, now, *_args, **_kwargs):
        if abs(float(now) - arm_at) > 1:
            return "OFF_SESSION"
        return _plan(9.0 if coin == "ETH" else 2.0, arm_at + (0 if coin == "ETH" else 1))

    monkeypatch.setattr("hl_bot.strategy.model_b.swing_replay.plan_trade", both)
    open_book = replay(
        book,
        SwingParams(confirm_tf="1h", flow="off", max_positions=0, session="09:30-16:00"),
        risk_pct=0.01,
        equity=5_000.0,
        name="two",
    )
    assert open_book.trades == 2
    assert {row.coin for row in open_book.blotter} == {"BTC", "ETH"}


def test_major_level_needs_two_4h_pivots_or_a_prior_day_print():
    assert _is_major({"sources": ["4h_low", "4h_low"]}, SwingParams(major=True, min_touches=2))
    assert not _is_major({"sources": ["4h_low"]}, SwingParams(major=True, min_touches=2))
    assert _is_major({"sources": ["PDL"]}, SwingParams(major=True, min_touches=2))
    assert not _is_major({"sources": ["1d_low", "1d_low"]}, SwingParams(major=True, min_touches=2))
    start = 1_700_000_000.0
    bars = []
    for i in range(40 * 24):
        low = 149.0
        high = 151.0
        if i in (20, 20 + 24 * 6, 20 + 24 * 12):
            low = 100.0
        if i == 20 + 24 * 18:
            low = 80.0
        bars.append(
            {
                "t": (start + i * 3600) * 1000.0,
                "o": 150.0,
                "h": high,
                "l": low,
                "c": 150.0,
                "v": 1.0,
            }
        )
    now = bars[-1]["t"] / 1000.0 + 3600
    levels = build_levels(bars, now, SwingParams(top_n=4, min_score=1, major=True, min_touches=2))
    supports = [level.price for level in levels if level.kind == "support"]
    assert any(abs(price - 100.0) / 100.0 < 0.02 for price in supports)
    assert all(abs(price - 80.0) / 80.0 > 0.02 for price in supports)


def test_desk_flags_default_off_and_reject_a_higher_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRY_MODE", "model_b")
    monkeypatch.setenv("MODEL_B_STYLE", "swing")
    monkeypatch.setenv("MODEL_B_JOURNAL_PATH", str(tmp_path / "swing.jsonl"))
    settings = load_settings()
    assert settings.model_b_swing_max_lev == 20
    assert settings.model_b_swing_max_positions == 0
    assert settings.model_b_swing_major is False
    assert settings.model_b_swing_session == "all"
    params = SwingParams.from_settings(settings)
    assert params.max_lev == 20 and params.max_positions == 0 and params.major is False
    assert params.min_r == pytest.approx(1.0) and params.max_r == pytest.approx(5.0)
    assert params.hold_overnight == "off"
    assert settings.model_b_swing_rr_max is None
    monkeypatch.setenv("MODEL_B_SWING_MAX_LEV", "40")
    with pytest.raises(ValueError, match="MODEL_B_SWING_MAX_LEV"):
        load_settings()
    monkeypatch.setenv("MODEL_B_SWING_MAX_LEV", "10")
    monkeypatch.setenv("MODEL_B_SWING_MAX_POSITIONS", "1")
    monkeypatch.setenv("MODEL_B_SWING_MAJOR", "1")
    monkeypatch.setenv("MODEL_B_SWING_MIN_TOUCHES", "2")
    monkeypatch.setenv("MODEL_B_SWING_SESSION", "09:30-16:00")
    turned = load_settings()
    assert turned.model_b_swing_max_lev == 10
    assert turned.model_b_swing_max_positions == 1
    assert turned.model_b_swing_major is True
    assert SwingParams.from_settings(turned).session == "09:30-16:00"
    assert SwingParams.from_settings(turned).hold_overnight == "off"
    assert SwingParams.from_settings(turned).max_r == pytest.approx(5.0)
    Settings(entry_mode="model_b", model_b_style="scalp", leverage=20, risk_per_trade=0.02).validate()


def test_rr_max_caps_targets_at_3r_and_rejects_a_wider_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRY_MODE", "model_b")
    monkeypatch.setenv("MODEL_B_STYLE", "swing")
    monkeypatch.setenv("MODEL_B_JOURNAL_PATH", str(tmp_path / "swing.jsonl"))
    monkeypatch.setenv("MODEL_B_SWING_RR_MAX", "9")
    with pytest.raises(ValueError, match="MODEL_B_SWING_RR_MAX"):
        load_settings()
    monkeypatch.setenv("MODEL_B_SWING_RR_MAX", "3")
    turned = load_settings()
    assert turned.model_b_swing_rr_max == pytest.approx(3.0)
    assert SwingParams.from_settings(turned).max_r == pytest.approx(3.0)
    assert SwingParams.from_settings(turned).min_r == pytest.approx(1.0)
    far = Level(140.0, "resistance", 1.0, 2, ("4h_high",), 0.0)
    near = Level(120.0, "resistance", 1.0, 2, ("4h_high",), 0.0)
    assert pick_targets("long", 100.0, 90.0, [far], 1.0, 3.0, False) == TP_OVER_MAX
    picked = pick_targets("long", 100.0, 90.0, [near, far], 1.0, 3.0, False)
    assert picked[0] == pytest.approx(120.0)
    assert picked[1] == pytest.approx(2.0)
    kept = pick_targets("long", 100.0, 90.0, [far], 1.0, 5.0, False)
    assert kept[0] == pytest.approx(140.0)


def test_hold_overnight_flag_and_the_breakeven_rule(monkeypatch, tmp_path):
    tz = ZoneInfo("America/Toronto")
    close = datetime(2026, 7, 15, 16, 0, tzinfo=tz).timestamp()
    morning = datetime(2026, 7, 16, 8, 0, tzinfo=tz).timestamp()
    monday = datetime(2026, 7, 20, 8, 0, tzinfo=tz).timestamp()
    assert session_close_id(close, "09:30-16:00") == "2026-07-15"
    assert session_close_id(morning, "09:30-16:00") == "2026-07-15"
    assert session_close_id(datetime(2026, 7, 18, 12, 0, tzinfo=tz).timestamp(), "09:30-16:00") == "2026-07-17"
    assert session_close_id(monday, "09:30-16:00") == "2026-07-17"
    assert session_close_id(datetime(2026, 7, 15, 10, 0, tzinfo=tz).timestamp(), "09:30-16:00") is None
    assert session_close_id(close, "all") is None
    assert normalize_hold_overnight("1") == "always"
    assert normalize_hold_overnight("be") == "be"
    assert normalize_hold_overnight(None) == "off"
    clock = SwingParams(session="09:30-16:00", hold_overnight="off")
    always = SwingParams(session="09:30-16:00", hold_overnight="always")
    be = SwingParams(session="09:30-16:00", hold_overnight="be")
    assert overnight_decision(close, clock, side="long", entry=100, risk_stop=90, price=110) == "flat"
    assert overnight_decision(close, always, side="long", entry=100, risk_stop=90, price=100) == "hold"
    assert not session_open(close, "09:30-16:00")
    assert overnight_decision(close, be, side="long", entry=100, risk_stop=90, price=110) == "be"
    assert overnight_decision(close, be, side="long", entry=100, risk_stop=90, price=109) == "flat"
    assert overnight_decision(close, be, side="short", entry=100, risk_stop=110, price=90) == "be"
    assert overnight_decision(close, be, side="short", entry=100, risk_stop=110, price=95) == "flat"
    assert overnight_decision(morning, be, side="long", entry=100, risk_stop=90, price=100, decided="2026-07-15") == "hold"
    assert tighten_to_breakeven("long", 100, 90) == pytest.approx(100)
    assert tighten_to_breakeven("long", 100, 105) == pytest.approx(105)
    assert tighten_to_breakeven("short", 100, 110) == pytest.approx(100)
    monkeypatch.setenv("ENTRY_MODE", "model_b")
    monkeypatch.setenv("MODEL_B_STYLE", "swing")
    monkeypatch.setenv("MODEL_B_JOURNAL_PATH", str(tmp_path / "swing.jsonl"))
    monkeypatch.setenv("MODEL_B_SWING_HOLD_OVERNIGHT", "2")
    with pytest.raises(ValueError, match="MODEL_B_SWING_HOLD_OVERNIGHT"):
        load_settings()
    monkeypatch.setenv("MODEL_B_SWING_HOLD_OVERNIGHT", "1")
    monkeypatch.setenv("MODEL_B_SWING_RR_MAX", "3")
    held = SwingParams.from_settings(load_settings())
    assert held.hold_overnight == "always"
    assert held.max_r == pytest.approx(3.0)
    monkeypatch.setenv("MODEL_B_SWING_HOLD_OVERNIGHT", "be")
    assert SwingParams.from_settings(load_settings()).hold_overnight == "be"


def _span(first_close: float, last_close: float) -> list[dict]:
    bars = []
    close = first_close
    while close <= last_close + 1:
        bars.append(
            {
                "t": (close - 3600.0) * 1000.0,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.2,
                "v": 1.0,
            }
        )
        close += 3600.0
    return bars


def test_replay_holds_overnight_and_flattens_a_flat_close(monkeypatch):
    tz = ZoneInfo("America/Toronto")
    arm_at = datetime(2026, 7, 15, 10, 0, tzinfo=tz).timestamp()
    close_16 = datetime(2026, 7, 15, 16, 0, tzinfo=tz).timestamp()
    bars = _span(arm_at - 5 * 3600.0, arm_at + 80 * 3600.0)

    def fake(coin, now, *_args, **_kwargs):
        if abs(float(now) - arm_at) > 1:
            return "OFF_SESSION"
        return _plan(9.0, arm_at)

    monkeypatch.setattr("hl_bot.strategy.model_b.swing_replay.plan_trade", fake)
    base = dict(
        confirm_tf="1h",
        flow="off",
        max_positions=1,
        max_lev=10,
        session="09:30-16:00",
        min_r=1.0,
        max_r=3.0,
        hold_days=3,
    )
    held = replay({"BTC": {"1h": bars}}, SwingParams(hold_overnight="always", **base), equity=5_000.0)
    assert held.trades == 1
    assert held.blotter[0].reason == "max_hold"
    assert held.blotter[0].hold_hours == pytest.approx(72.0, abs=1.5)

    flat = replay({"BTC": {"1h": bars}}, SwingParams(hold_overnight="be", **base), equity=5_000.0)
    assert flat.trades == 1
    assert flat.blotter[0].reason == "SESSION_CLOSE"

    for bar in bars:
        stamp = bar["t"] / 1000.0 + 3600.0
        if abs(stamp - close_16) < 1:
            bar["h"] = 112.0
            bar["l"] = 105.0
            bar["c"] = 110.0
        elif stamp > close_16:
            bar["l"] = 100.0
            bar["c"] = 100.5
    winner = replay({"BTC": {"1h": bars}}, SwingParams(hold_overnight="be", **base), equity=5_000.0)
    assert winner.trades == 1
    trade = winner.blotter[0]
    assert trade.reason == "stop"
    assert trade.exit == pytest.approx(100.0)
    assert trade.hold_hours >= 6


def test_overnight_hold_still_refuses_an_entry_at_the_close(monkeypatch):
    tz = ZoneInfo("America/Toronto")
    arm_at = datetime(2026, 7, 15, 15, 0, tzinfo=tz).timestamp()
    bars = _span(arm_at - 45 * 3600.0, arm_at + 6 * 3600.0)

    def fake(coin, now, *_args, **_kwargs):
        if abs(float(now) - arm_at) > 1:
            return "OFF_SESSION"
        return _plan(4.0, arm_at)

    monkeypatch.setattr("hl_bot.strategy.model_b.swing_replay.plan_trade", fake)
    blocked = replay(
        {"BTC": {"1h": bars}},
        SwingParams(
            confirm_tf="1h",
            flow="off",
            session="09:30-16:00",
            hold_overnight="always",
            hold_days=3,
        ),
        equity=5_000.0,
    )
    assert blocked.trades == 0
    filled = replay(
        {"BTC": {"1h": bars}},
        SwingParams(confirm_tf="1h", flow="off", session="all", hold_overnight="always", hold_days=0.01),
        equity=5_000.0,
    )
    assert filled.trades == 1
    assert filled.blotter[0].reason == "max_hold"

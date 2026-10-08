"""Better TP targeting: untaken swings, refresh at the fill, partial runner.

Size, leverage, and margin are not part of the pick. The tight-stop test
below pins the 20x notional cap and the 2% loss cap (fees included).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from hl_bot.config import load_settings
from hl_bot.exchange.account import AccountSnapshot, PerpPosition, ProtectiveOrder
from hl_bot.execution.guard import PositionGuard, plans_from_book
from hl_bot.execution.model_b_loop import (
    _place_brackets,
    _prepare_fill_targets,
    _replace_stop,
    _restore_adopted,
    _trail_runner,
)
from hl_bot.journal import TradeJournal
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.risk import cap_size_to_loss, loss_at_stop, size_from_stop
from hl_bot.strategy.model_b.thesis import OpenPosition, ThesisBook
from hl_bot.strategy.model_b.tp_select import (
    TpWalk,
    restore_plan,
    runner_target,
    split_runner_size,
    trail_stop,
)
from hl_bot.strategy.model_b.tp_select import filter_spent_swing_prices
from hl_bot.strategy.model_b.types import AloIntent, Swing


def _bar(t, lo, hi, close=None):
    mid = (lo + hi) / 2.0 if close is None else close
    return {"t": t, "o": mid, "h": hi, "l": lo, "c": mid}


def _eth_bars():
    """Short analogue of #9: 84 was traded through, 80.9 was not.

    Entry 100, stop 110 (10 points). 84 is 1.6R, 80.9 is 1.91R.
    """
    return [
        _bar(1000, 90, 96),
        _bar(1060, 84, 95),
        _bar(1120, 90, 96),
        _bar(1180, 83, 94),
        _bar(1240, 86, 95),
        _bar(1300, 88, 96),
        _bar(1360, 80.9, 92),
        _bar(1420, 88, 96),
    ]


NOW = 1600.0
ENTRY = 100.0
STOP = 110.0
TICK = 0.1


def _engine(mode, **kw):
    return ModelBEngine(
        tp_min_pool_r=kw.pop("tp_min_pool_r", 1.5),
        tp_untaken_only=mode,
        tp_runner=kw.pop("tp_runner", "off"),
        coins=("ETH",),
        min_stop_bps=0.0,
        **kw,
    )


def _pick(engine):
    return engine.pick_target(
        "short",
        ENTRY,
        STOP,
        TICK,
        _eth_bars(),
        [],
        NOW,
        92.0,
        coin="ETH",
    )


class _Info:
    def __init__(self, bars):
        self.bars = bars

    def get_candles(self, coin, interval="1m", start_ms=0, end_ms=0):
        return list(self.bars)


class _SeqLive:
    def __init__(self, *, fail_stops_after=None):
        self.events = []
        self._n = 700
        self.fail_stops_after = fail_stops_after

    def _oid(self):
        self._n += 1
        return self._n

    def _ok(self, oid):
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def set_stop_loss(self, coin, is_buy, size, trigger_px):
        self.events.append(("stop", coin, round(size, 8), trigger_px))
        if self.fail_stops_after is not None and len(
            [e for e in self.events if e[0] == "stop"]
        ) > self.fail_stops_after:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "rejected"}]}}}
        return self._ok(self._oid())

    def set_take_profit(self, coin, is_buy, size, trigger_px):
        self.events.append(("tp", coin, round(size, 8), trigger_px))
        return self._ok(self._oid())

    def cancel_order(self, coin, oid):
        self.events.append(("cancel", coin, oid))
        return {"status": "ok"}

    def reduce_only_ioc(self, coin, is_buy, size, ref_px=None, slippage=0.05):
        self.events.append(("reduce", coin, size))
        return {"status": "ok"}


def _settings(**kw):
    base = dict(
        model_b_tp_refresh_on_fill="on",
        model_b_tp_runner="off",
        model_b_tp_runner_frac=0.5,
        model_b_tp_runner_max_r=5.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _pos(**kw):
    fields = dict(
        coin="ETH",
        side="short",
        size=1.0,
        entry=ENTRY,
        stop=STOP,
        take_profit=84.0,
        swing_id="s",
        opened_at=NOW,
        armed_at=1120.0,
        tick=TICK,
        planned_stop=STOP,
    )
    fields.update(kw)
    return OpenPosition(**fields)


# ---------------------------------------------------------------- config


def test_tp_flag_defaults_are_untaken_on_refresh_on_runner_shadow():
    settings = load_settings()
    assert settings.model_b_tp_untaken_only == "on"
    assert settings.model_b_tp_refresh_on_fill == "on"
    assert settings.model_b_tp_runner == "shadow"
    assert settings.model_b_tp_runner_frac == pytest.approx(0.5)
    assert settings.model_b_tp_runner_max_r == pytest.approx(5.0)


def test_tp_flags_parse_zero_shadow_and_one(monkeypatch):
    monkeypatch.setenv("MODEL_B_TP_UNTAKEN_ONLY", "0")
    monkeypatch.setenv("MODEL_B_TP_REFRESH_ON_FILL", "shadow")
    monkeypatch.setenv("MODEL_B_TP_RUNNER", "1")
    monkeypatch.setenv("MODEL_B_TP_RUNNER_FRAC", "0.25")
    settings = load_settings()
    assert settings.model_b_tp_untaken_only == "off"
    assert settings.model_b_tp_refresh_on_fill == "shadow"
    assert settings.model_b_tp_runner == "on"
    assert settings.model_b_tp_runner_frac == pytest.approx(0.25)


def test_tp_flag_rejects_garbage(monkeypatch):
    monkeypatch.setenv("MODEL_B_TP_RUNNER", "sometimes")
    with pytest.raises(ValueError, match="MODEL_B_TP_RUNNER"):
        load_settings()


# ---------------------------------------------------------------- untaken


def test_untaken_on_skips_the_spent_swing_like_eth_9():
    spent = _pick(_engine("off"))
    fresh = _pick(_engine("on"))
    assert spent.fail is None and spent.target == pytest.approx(84.0)
    assert fresh.fail is None and fresh.target == pytest.approx(80.9)


def test_a_later_swing_at_the_same_price_is_not_spent():
    bars = [
        _bar(1000, 90, 96),
        _bar(1060, 84, 95),
        _bar(1120, 80, 94),  # trades through the first 84
        _bar(1180, 90, 96),
        _bar(1240, 84, 95),  # the level prints again
        _bar(1300, 90, 96),
    ]
    swings = [Swing("low", 84.0, 1060.0), Swing("low", 84.0, 1240.0)]
    spent = filter_spent_swing_prices("short", swings, bars, 1400.0, 92.0)
    assert 84.0 not in spent


def test_untaken_off_matches_another_off_engine():
    a = _pick(_engine("off"))
    b = _pick(_engine("off"))
    assert a.target == b.target == pytest.approx(84.0)


def test_untaken_shadow_pick_stays_on_the_spent_level():
    """Shadow's order uses today's levels. The untaken walk is the would-be."""
    shadow = _engine("shadow")
    assert shadow.tp_untaken_only == "shadow"
    assert _pick(shadow).target == pytest.approx(84.0)
    assert _pick(_engine("on")).target == pytest.approx(80.9)


def test_untaken_shadow_logs_the_would_be_on_a_real_arm(tmp_path, caplog):
    from tests.test_model_b_tp_next_pool import XYZ_LONG, _run

    with caplog.at_level(logging.INFO):
        decision = _run(
            tmp_path,
            XYZ_LONG,
            MODEL_B_TP_UNTAKEN_ONLY="shadow",
            MODEL_B_TP_MIN_POOL_R=0,
            MODEL_B_TP_RUNNER=0,
        )
    assert decision.armed
    assert decision.intent.take_profit == pytest.approx(31005.0)
    assert any(
        "MODEL_B TP_UNTAKEN shadow" in rec.getMessage() and "would=" in rec.getMessage()
        for rec in caplog.records
    )


# ---------------------------------------------------------------- refresh


def test_refresh_moves_a_spent_tp_and_leaves_the_stop(caplog):
    pos = _pos()
    before_stop = pos.stop
    engine = _engine("on")
    with caplog.at_level(logging.INFO):
        _prepare_fill_targets(engine, _Info(_eth_bars()), _settings(), pos, NOW)
    assert pos.take_profit == pytest.approx(80.9)
    assert pos.stop == before_stop
    assert any("MODEL_B TP_REFRESH ETH short" in r.getMessage() and "reason=spent" in r.getMessage() for r in caplog.records)
    assert any("old=84" in r.getMessage() and "new=80.9" in r.getMessage() for r in caplog.records)


def test_refresh_keeps_the_tp_when_it_was_not_spent():
    pos = _pos(take_profit=80.9)
    _prepare_fill_targets(_engine("on"), _Info(_eth_bars()), _settings(), pos, NOW)
    assert pos.take_profit == pytest.approx(80.9)
    assert pos.stop == STOP


def test_refresh_shadow_logs_and_does_not_change_the_order(caplog):
    pos = _pos()
    with caplog.at_level(logging.INFO):
        _prepare_fill_targets(
            _engine("on"),
            _Info(_eth_bars()),
            _settings(model_b_tp_refresh_on_fill="shadow"),
            pos,
            NOW,
        )
    assert pos.take_profit == pytest.approx(84.0)
    assert pos.stop == STOP
    assert any("mode=shadow" in r.getMessage() and "TP_REFRESH" in r.getMessage() for r in caplog.records)


def test_refresh_keeps_the_plan_when_the_repick_fails():
    class _Boom:
        def pick_target(self, *args, **kwargs):
            raise RuntimeError("candles")

    pos = _pos()
    _prepare_fill_targets(_Boom(), _Info(_eth_bars()), _settings(), pos, NOW)
    assert pos.take_profit == pytest.approx(84.0)
    assert pos.stop == STOP


def test_refresh_keeps_the_plan_when_the_walk_fails():
    class _Fail:
        def pick_target(self, *args, **kwargs):
            return TpWalk(target=70.0, fail="TP_TOO_FAR_COUNTERTREND")

    pos = _pos()
    _prepare_fill_targets(_Fail(), _Info(_eth_bars()), _settings(), pos, NOW)
    assert pos.take_profit == pytest.approx(84.0)
    assert pos.stop == STOP


def test_refresh_off_does_not_move_a_spent_level():
    pos = _pos()
    _prepare_fill_targets(
        _engine("on"),
        _Info(_eth_bars()),
        _settings(model_b_tp_refresh_on_fill="off"),
        pos,
        NOW,
    )
    assert pos.take_profit == pytest.approx(84.0)


def test_btc11_style_level_traded_through_while_resting_retargets():
    """Planned 1.6R was spent between arm and fill; the fresh pool is 2.2R-class."""
    pos = _pos(take_profit=84.0, armed_at=1120.0)
    _prepare_fill_targets(_engine("on"), _Info(_eth_bars()), _settings(), pos, NOW)
    assert pos.take_profit == pytest.approx(80.9)
    assert abs(ENTRY - pos.take_profit) / abs(ENTRY - STOP) == pytest.approx(1.91)
    assert pos.stop == STOP


# ---------------------------------------------------------------- runner


def test_split_sums_to_the_ticket():
    tp1, runner = split_runner_size(0.08887, 0.5)
    assert tp1 + runner == pytest.approx(0.08887)
    assert tp1 > 0 and runner > 0


def test_runner_target_uses_the_significant_pool_else_max_r():
    dist = 10.0
    tp1 = ENTRY - 1.91 * dist
    hit = runner_target("short", ENTRY, STOP, tp1, [ENTRY - 3.2 * dist, ENTRY - 6 * dist], max_r=5)
    assert hit == pytest.approx(ENTRY - 3.2 * dist)
    fallback = runner_target("short", ENTRY, STOP, tp1, [], max_r=5)
    assert fallback == pytest.approx(ENTRY - 5 * dist)


def test_runner_on_splits_the_order_and_stops_the_full_size():
    pos = _pos(size=2.0)
    _prepare_fill_targets(
        _engine("on"),
        _Info(_eth_bars()),
        _settings(model_b_tp_runner="on"),
        pos,
        NOW,
    )
    assert pos.runner_on
    assert pos.tp1_size + pos.runner_size == pytest.approx(pos.size)
    assert pos.runner_px is not None and pos.runner_px < pos.take_profit
    live = _SeqLive()
    _place_brackets(live, pos)
    kinds = [e[0] for e in live.events]
    assert kinds[0] == "stop"
    assert live.events[0][2] == pytest.approx(pos.size)
    assert live.events[0][3] == pytest.approx(pos.stop)
    tps = [e for e in live.events if e[0] == "tp"]
    assert len(tps) == 2
    assert tps[0][2] + tps[1][2] == pytest.approx(pos.size)
    assert tps[0][3] == pytest.approx(pos.take_profit)
    assert tps[1][3] == pytest.approx(pos.runner_px)


def test_runner_shadow_does_not_split_or_change_the_stop():
    pos = _pos()
    _prepare_fill_targets(
        _engine("on"),
        _Info(_eth_bars()),
        _settings(model_b_tp_runner="shadow"),
        pos,
        NOW,
    )
    assert pos.runner_on is False
    assert pos.runner_shadow is True
    assert pos.stop == STOP
    live = _SeqLive()
    _place_brackets(live, pos)
    tps = [e for e in live.events if e[0] == "tp"]
    assert len(tps) == 1
    assert tps[0][2] == pytest.approx(pos.size)


def test_tp1_fill_moves_the_stop_to_breakeven_and_keeps_the_runner():
    book = ThesisBook()
    book.post(
        AloIntent(
            coin="ETH",
            side="short",
            limit_px=ENTRY,
            size=2.0,
            stop=STOP,
            take_profit=84.0,
            swing_id="s",
            tick=TICK,
            runner_px=70.0,
            runner_mode="on",
        ),
        now=1000.0,
    )
    opened = book.apply_user_fill(
        coin="ETH", oid=None, price=ENTRY, ts=1100.0, crossed=False, size=2.0
    )
    assert opened.runner_on
    opened.stop = STOP
    opened.take_profit = 84.0
    opened.runner_px = 70.0
    opened.planned_stop = 0.0
    opened.tp1_size = 1.0
    opened.runner_size = 1.0
    opened.stop_oid = 11
    partial = book.apply_user_fill(
        coin="ETH",
        oid=None,
        price=84.0,
        ts=1200.0,
        crossed=True,
        size=1.0,
        direction="Close Short",
    )
    assert partial is opened
    assert opened.tp1_filled
    assert opened.size == pytest.approx(1.0)
    assert opened.stop == pytest.approx(ENTRY)
    assert opened.take_profit == pytest.approx(70.0)
    assert opened.planned_stop == pytest.approx(STOP)
    live = _SeqLive()
    _replace_stop(live, opened, opened.stop)
    assert live.events[0][0] == "stop"
    assert live.events[0][2] == pytest.approx(1.0)
    assert live.events[0][3] == pytest.approx(ENTRY)
    assert live.events[1] == ("cancel", "ETH", 11)


def test_a_stop_side_partial_is_not_tp1():
    book = ThesisBook()
    book.post(
        AloIntent(
            coin="ETH",
            side="short",
            limit_px=ENTRY,
            size=2.0,
            stop=STOP,
            take_profit=84.0,
            swing_id="s2",
            tick=TICK,
            runner_px=70.0,
            runner_mode="on",
        ),
        now=1000.0,
    )
    opened = book.apply_user_fill(
        coin="ETH", oid=None, price=ENTRY, ts=1100.0, crossed=False, size=2.0
    )
    opened.stop = STOP
    opened.take_profit = 84.0
    opened.runner_on = True
    out = book.apply_user_fill(
        coin="ETH",
        oid=None,
        price=108.0,
        ts=1200.0,
        crossed=True,
        size=0.4,
        direction="Close Short",
    )
    assert out is None
    assert opened.tp1_filled is False
    assert opened.stop == STOP
    assert opened.size == pytest.approx(1.6)


def test_failed_stop_replace_does_not_cancel_the_old_one():
    pos = _pos(size=1.0, stop_oid=11, runner_on=True, tp1_filled=True, stop=ENTRY)
    live = _SeqLive(fail_stops_after=0)
    assert _replace_stop(live, pos, ENTRY) is False
    assert ("cancel", "ETH", 11) not in live.events
    assert pos.stop_oid == 11


def test_trail_tightens_behind_the_latest_1m_swing_and_does_not_loosen():
    # Latest swing low is 101, one tick under it is tighter than breakeven.
    bars = [
        _bar(1000, 103, 106),
        _bar(1060, 101, 105),
        _bar(1120, 103, 106),
        _bar(1180, 99, 104),  # older extreme, not the latest fractal
    ]
    # 99 is also a fractal if neighbors are higher. Latest confirmed is 99's bar
    # only if a later bar exists. Put the tight swing last.
    bars = [
        _bar(1000, 99, 104),
        _bar(1060, 102, 106),
        _bar(1120, 101, 105),
        _bar(1180, 103, 106),
    ]
    now = 1300.0
    tightened = trail_stop("long", bars, now, 0.1, 100.0)
    assert tightened == pytest.approx(100.9)
    assert trail_stop("long", bars, now, 0.1, tightened) is None
    pos = _pos(
        coin="BTC",
        side="long",
        entry=100.0,
        stop=100.0,
        take_profit=110.0,
        runner_on=True,
        tp1_filled=True,
        planned_stop=95.0,
    )
    live = _SeqLive()
    pos.stop_oid = 4
    _trail_runner(pos, bars, now, 0.1, live, _settings(model_b_tp_runner="on"))
    assert pos.stop == pytest.approx(100.9)
    assert live.events[0][0] == "stop"
    assert live.events[1][0] == "cancel"
    shadow = _pos(
        coin="BTC",
        side="long",
        entry=100.0,
        stop=100.0,
        take_profit=110.0,
        runner_shadow=True,
        tp1_filled=True,
    )
    _trail_runner(shadow, bars, now, 0.1, _SeqLive(), _settings(model_b_tp_runner="shadow"))
    assert shadow.stop == pytest.approx(100.0)


# ---------------------------------------------------------------- guard


def _guard(live, snap, plans):
    info = SimpleNamespace(load_account_snapshot=lambda *a, **k: snap)
    guard = PositionGuard(
        live,
        info,
        user="0xabc",
        dexs=("",),
        risk_pct=0.02,
        max_leverage=20,
        include_fees=False,
    )
    guard.set_plans(plans)
    return guard


def _short_pos(size, entry, stop, tp, **kw):
    pos = OpenPosition(
        coin="ETH",
        side="short",
        size=size,
        entry=entry,
        stop=stop,
        take_profit=tp,
        swing_id="g",
        opened_at=NOW,
        intended_size=2.0,
        planned_risk=20.0,
        planned_stop=STOP,
        **kw,
    )
    book = ThesisBook()
    book.adopt_position(coin="ETH", side="short", size=size, entry=entry, now=NOW)
    held = book.position("ETH")
    held.adopted = False
    held.stop = stop
    held.take_profit = tp
    held.intended_size = pos.intended_size
    held.planned_risk = pos.planned_risk
    held.planned_stop = pos.planned_stop
    for name in (
        "runner_on",
        "runner_px",
        "runner_size",
        "tp1_size",
        "tp1_filled",
        "tp2_oid",
    ):
        if name in kw or hasattr(pos, name):
            setattr(held, name, getattr(pos, name))
    return book, held


def test_guard_does_not_top_up_full_size_at_tp1_or_cancel_the_runner():
    book, held = _short_pos(
        2.0,
        ENTRY,
        STOP,
        84.0,
        runner_on=True,
        runner_px=70.0,
        tp1_size=1.0,
        runner_size=1.0,
    )
    plan = plans_from_book(book)["ETH"]
    assert plan.tp1_size == pytest.approx(1.0)
    assert plan.runner_tp == pytest.approx(70.0)
    snap = AccountSnapshot(
        ok=True,
        positions=(PerpPosition(coin="ETH", szi=-2.0, entry=ENTRY, margin_used=10.0, mark=92.0, unrealized_pnl=1.0),),
        protective_orders=(
            ProtectiveOrder("ETH", 1, "sl", "buy", STOP, 2.0),
            ProtectiveOrder("ETH", 2, "tp", "buy", 84.0, 0.4),
            ProtectiveOrder("ETH", 3, "tp", "buy", 70.0, 1.0),
        ),
    )
    live = _SeqLive()
    guard = _guard(live, snap, plans_from_book(book))
    guard._own["ETH"] = {2, 3}
    guard.run_once(snap, spot=1000.0)
    cancels = [e for e in live.events if e[0] == "cancel"]
    assert ("cancel", "ETH", 3) not in cancels
    tps = [e for e in live.events if e[0] == "tp"]
    assert tps
    assert all(e[3] == pytest.approx(84.0) for e in tps)
    assert all(e[2] <= 1.0 + 1e-9 for e in tps)


def test_guard_after_tp1_covers_the_remainder_at_the_runner_only():
    book, held = _short_pos(
        1.0,
        ENTRY,
        ENTRY,
        70.0,
        runner_on=True,
        runner_px=70.0,
        tp1_filled=True,
        tp1_size=0.0,
        runner_size=1.0,
    )
    plan = plans_from_book(book)["ETH"]
    assert plan.tp1_size == 0
    assert plan.stop == pytest.approx(ENTRY)
    assert plan.take_profit == pytest.approx(70.0)
    assert plan.planned_risk == pytest.approx(20.0)
    snap = AccountSnapshot(
        ok=True,
        positions=(
            PerpPosition(
                coin="ETH", szi=-1.0, entry=ENTRY, margin_used=5.0, mark=ENTRY, unrealized_pnl=0.0
            ),
        ),
        protective_orders=(
            ProtectiveOrder("ETH", 1, "sl", "buy", ENTRY, 1.0),
            ProtectiveOrder("ETH", 3, "tp", "buy", 70.0, 1.0),
        ),
    )
    live = _SeqLive()
    _guard(live, snap, plans_from_book(book)).run_once(snap, spot=1000.0)
    assert not any(e[0] in ("tp", "reduce", "cancel") for e in live.events)


def test_flat_at_breakeven_is_not_a_loss_kill():
    book, _held = _short_pos(1.0, ENTRY, ENTRY, 70.0, runner_on=True, tp1_filled=True, runner_px=70.0)
    snap = AccountSnapshot(
        ok=True,
        positions=(
            PerpPosition(
                coin="ETH", szi=-1.0, entry=ENTRY, margin_used=5.0, mark=ENTRY, unrealized_pnl=0.0
            ),
        ),
        protective_orders=(ProtectiveOrder("ETH", 1, "sl", "buy", ENTRY, 1.0),),
    )
    live = _SeqLive()
    _guard(live, snap, plans_from_book(book)).run_once(snap, spot=1000.0)
    assert not any(e[0] == "reduce" for e in live.events)


def test_missing_stop_is_replaced_at_full_size():
    book, _held = _short_pos(2.0, ENTRY, STOP, 84.0)
    snap = AccountSnapshot(
        ok=True,
        positions=(
            PerpPosition(
                coin="ETH", szi=-2.0, entry=ENTRY, margin_used=10.0, mark=92.0, unrealized_pnl=1.0
            ),
        ),
    )
    live = _SeqLive()
    _guard(live, snap, plans_from_book(book)).run_once(snap, spot=1000.0)
    stops = [e for e in live.events if e[0] == "stop"]
    assert stops and stops[0][2] == pytest.approx(2.0)
    assert stops[0][3] == pytest.approx(STOP)


# ---------------------------------------------------------------- restart


def test_restore_without_tp1_uses_the_journal_stop_and_tp(tmp_path):
    journal = TradeJournal(tmp_path / "j.jsonl")
    journal.log(
        "open",
        symbol="ETH",
        side="short",
        size=2.0,
        price=ENTRY,
        stop=STOP,
        tp=84.0,
        runner_px=70.0,
        runner_on=True,
    )
    book = ThesisBook()
    book.adopt_position(coin="ETH", side="short", size=2.0, entry=ENTRY, now=NOW)
    item = SimpleNamespace(coin="ETH", side="short")
    _restore_adopted(book, journal, item, _settings())
    pos = book.position("ETH")
    assert pos.adopted is False
    assert pos.stop == pytest.approx(STOP)
    assert pos.take_profit == pytest.approx(84.0)
    assert pos.tp1_filled is False
    assert pos.runner_on
    assert pos.tp1_size + pos.runner_size == pytest.approx(2.0)


def test_restore_after_tp1_is_breakeven_and_the_runner(tmp_path):
    journal = TradeJournal(tmp_path / "j.jsonl")
    journal.log(
        "open",
        symbol="ETH",
        side="short",
        size=2.0,
        price=ENTRY,
        stop=STOP,
        tp=84.0,
        runner_px=70.0,
        runner_on=True,
    )
    journal.log("model_b_tp1", symbol="ETH", side="short", size=1.0, price=ENTRY, stop=ENTRY, tp=70.0)
    book = ThesisBook()
    book.adopt_position(coin="ETH", side="short", size=1.0, entry=ENTRY, now=NOW)
    _restore_adopted(book, journal, SimpleNamespace(coin="ETH"), _settings())
    pos = book.position("ETH")
    assert pos.adopted is False
    assert pos.stop == pytest.approx(ENTRY)
    assert pos.take_profit == pytest.approx(70.0)
    assert pos.tp1_filled
    assert pos.planned_stop == pytest.approx(STOP)


def test_restore_without_a_journal_open_stays_adopted(tmp_path):
    journal = TradeJournal(tmp_path / "j.jsonl")
    book = ThesisBook()
    book.adopt_position(coin="ETH", side="short", size=1.0, entry=ENTRY, now=NOW)
    _restore_adopted(book, journal, SimpleNamespace(coin="ETH"), _settings())
    pos = book.position("ETH")
    assert pos.adopted
    assert pos.stop == 0


def test_restore_plan_ignores_a_closed_trade():
    rows = [
        {"event": "open", "symbol": "ETH", "side": "short", "price": 100, "stop": 110, "tp": 84, "runner_on": True, "runner_px": 70},
        {"event": "close", "symbol": "ETH"},
    ]
    assert restore_plan(rows, "ETH") is None


# ---------------------------------------------------------------- size cap (unchanged)


def test_tight_stop_size_is_capped_at_20x_and_loss_stays_within_2pct():
    equity = 10_000.0
    entry = 80_000.0
    stop = entry * (1.0 - 0.0004)  # 4 bps, a few bps
    size, _risk = size_from_stop(
        equity,
        entry,
        stop,
        risk_pct=0.02,
        leverage=20,
        notional_leverage=20,
        include_fees=True,
    )
    size = cap_size_to_loss(size, entry, stop, equity, include_fees=True)
    assert size * entry <= equity * 20.0 + 1e-6
    assert loss_at_stop(size, entry, stop, include_fees=True) <= 0.02 * equity + 1e-6
    # The cap is what binds: 2% of equity on a 4 bp stop would be far past 20x.
    assert size * entry == pytest.approx(equity * 20.0, rel=1e-6)

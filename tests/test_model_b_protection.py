"""Never an open position without a stop (Oct 7 23:02 ET BTC incident).

Covers: the snapshot-before-websocket fill path, partial fills, adopted
naked positions, stop rejects -> market close, price through the stop,
loss kill, oversize cut, the fast guard thread, the 20x / min-stop sizing
brakes, and the 22:56 BTC arm replay (COUNTER_FLOW / STOP_TOO_TIGHT /
STRUCTURE / SHALLOW_SWEEP gates).
"""

from __future__ import annotations

import json
import logging
import pathlib
import time

import pytest

from hl_bot.exchange.account import (
    AccountSnapshot,
    EntryOrder,
    PerpPosition,
    ProtectiveOrder,
    parse_clearinghouse,
    parse_entry_orders,
    parse_protective_orders,
)
from hl_bot.exchange.hl_trades import MemoryFeed, UserFill
from hl_bot.exchange.info_client import InfoClient
from hl_bot.execution.guard import Plan, PositionGuard, plans_from_book
from hl_bot.execution.model_b_loop import run_model_b
from hl_bot.journal import TradeJournal
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.flow import counter_flow
from hl_bot.strategy.model_b.risk import HARD_MAX_LOSS_PCT, cap_size_to_loss, size_from_stop
from hl_bot.strategy.model_b.structure import classify, structure_blocks
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.types import AloIntent, Pool, TradePrint
from tests.test_model_b import _bars, _live_account_settings, _long_prints, _now

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "btc_20261007_2256_1m.json"


def _ok(oid):
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _err(text="Order would immediately trigger"):
    return {"status": "ok", "response": {"data": {"statuses": [{"error": text}]}}}


class FakeLive:
    def __init__(self, *, stop_resp=None, start_oid=500):
        self.alos = []
        self.stops = []
        self.tps = []
        self.cancels = []
        self.reduces = []
        self._oid = start_oid
        self._stop_resp = stop_resp

    def _next(self):
        self._oid += 1
        return self._oid

    def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
        self.alos.append((coin, is_buy, size, limit_px, leverage))
        return _ok(11)

    def cancel_order(self, coin, oid):
        self.cancels.append((coin, oid))
        return {"status": "ok"}

    def set_stop_loss(self, coin, is_buy, size, trigger_px):
        self.stops.append((coin, is_buy, round(size, 8), trigger_px))
        if self._stop_resp is not None:
            return self._stop_resp(len(self.stops))
        return _ok(self._next())

    def set_take_profit(self, coin, is_buy, size, trigger_px):
        self.tps.append((coin, is_buy, round(size, 8), trigger_px))
        return _ok(self._next())

    def reduce_only_ioc(self, coin, is_buy, size, ref_px=None, slippage=0.05):
        self.reduces.append((coin, is_buy, round(size, 8), ref_px))
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 9}}]}}}

    def market_close(self, coin, size=None):
        self.reduces.append((coin, None, size, None))
        return {"status": "ok"}


class FakeInfo:
    """Guard-only info: one snapshot, one spot balance."""

    def __init__(self, snapshot, spot=1000.0):
        self.snapshot = snapshot
        self.spot = spot

    def load_account_snapshot(self, user, dexs=None, **_kw):
        return self.snapshot

    def spot_usdc_balance(self, user):
        return self.spot


def _guard(live, snapshot, spot=1000.0, **kw):
    return PositionGuard(
        live,
        FakeInfo(snapshot, spot),
        user="0xabc",
        dexs=("", "xyz"),
        risk_pct=0.02,
        max_leverage=kw.pop("max_leverage", 20),
        loss_kill_r=kw.pop("loss_kill_r", 1.0),
        oversize_ratio=kw.pop("oversize_ratio", 1.1),
        # Price-only cap numbers below; the fee-inclusive cap (the default)
        # is covered in test_model_b_audit.py.
        include_fees=kw.pop("include_fees", False),
        **kw,
    )


def _pos(coin="BTC", szi=0.05, entry=83106.0, mark=83100.0, upnl=None):
    return PerpPosition(coin=coin, szi=szi, entry=entry, margin_used=0.0, mark=mark, unrealized_pnl=upnl)


# ---------------------------------------------------------------- parsing


def test_clearinghouse_parses_mark_and_upnl_and_open_orders_split():
    positions, _ = parse_clearinghouse(
        {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.14",
                        "entryPx": "83106",
                        "positionValue": "11634.0",
                        "unrealizedPnl": "-0.84",
                        "marginUsed": "290.8",
                    }
                }
            ]
        }
    )
    assert positions[0].mark == pytest.approx(83100.0)
    assert positions[0].unrealized_pnl == pytest.approx(-0.84)
    raw = [
        {"coin": "BTC", "side": "B", "limitPx": "83106", "sz": "0.05", "oid": 11, "orderType": "Limit"},
        {"coin": "BTC", "side": "A", "limitPx": "83083", "sz": "0.09", "oid": 31,
         "orderType": "Stop Market", "isTrigger": True, "triggerPx": "83083", "reduceOnly": True},
        {"coin": "BTC", "side": "A", "limitPx": "83160", "sz": "0.0", "oid": 32,
         "orderType": "Take Profit Market", "isTrigger": True, "triggerPx": "83160",
         "isPositionTpsl": True, "reduceOnly": True},
    ]
    assert [o.oid for o in parse_entry_orders(raw)] == [11]
    prot = parse_protective_orders(raw)
    assert [(o.oid, o.kind, o.side, o.full_position) for o in prot] == [
        (31, "sl", "sell", False),
        (32, "tp", "sell", True),
    ]
    assert prot[0].trigger_px == pytest.approx(83083)


# ---------------------------------------------------------------- guard


def test_adopted_naked_position_gets_a_stop_for_full_exchange_size(caplog):
    snap = AccountSnapshot(ok=True, positions=(_pos("xyz:XYZ100", 0.0056, 31074.0, 31070.0),))
    live = FakeLive()
    with caplog.at_level(logging.INFO):
        _guard(live, snap, spot=250.0).run_once()
    # No plan: the stop is where 2% of the account is lost.
    expected = 31074.0 - 0.02 * 250.0 / 0.0056
    assert live.stops == [("xyz:XYZ100", False, 0.0056, pytest.approx(expected))]
    assert live.reduces == []
    assert any("MODEL_B GUARD STOP xyz:XYZ100" in r.message for r in caplog.records)


def test_planned_position_gets_stop_and_tp_at_ticket_prices():
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=83110.0),))
    live = FakeLive()
    # spot 400: 2% cap = $8 > 0.05 x 136 = $6.80 at the planned stop.
    g = _guard(live, snap, spot=400.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82970.0, 83337.0, 0.05, 6.8)})
    g.run_once()
    assert live.stops == [("BTC", False, 0.05, 82970.0)]
    assert live.tps == [("BTC", False, 0.05, 83337.0)]


def test_existing_full_stop_is_left_alone():
    snap = AccountSnapshot(
        ok=True,
        positions=(_pos(szi=0.05, mark=83110.0),),
        protective_orders=(ProtectiveOrder("BTC", 31, "sl", "sell", 82970.0, 0.05),),
    )
    live = FakeLive()
    _guard(live, snap, spot=400.0).run_once()
    assert live.stops == [] and live.cancels == [] and live.reduces == []


def test_undersized_own_stop_is_resized_after_a_drip():
    live = FakeLive()
    g = _guard(live, AccountSnapshot(ok=True, positions=(_pos(szi=0.02, mark=83110.0),)), spot=293.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82970.0, 0.0, 0.0431, 5.86)})
    g.run_once()
    first = live._oid
    assert live.stops == [("BTC", False, 0.02, 82970.0)]
    # Drip: position 0.04 on the exchange, our 0.02 stop still resting.
    g.info.snapshot = AccountSnapshot(
        ok=True,
        positions=(_pos(szi=0.04, mark=83110.0),),
        protective_orders=(ProtectiveOrder("BTC", first, "sl", "sell", 82970.0, 0.02),),
    )
    g.run_once()
    assert live.stops[-1] == ("BTC", False, 0.04, 82970.0)
    assert ("BTC", first) in live.cancels  # old undersized stop removed after the new one rests


def test_stop_reject_on_two_passes_market_closes_and_alerts(tmp_path, caplog):
    snap = AccountSnapshot(
        ok=True,
        positions=(_pos(szi=0.05, mark=83110.0),),
        entry_orders=(EntryOrder("BTC", 11, "long", 83106.0, 0.04),),
    )
    live = FakeLive(stop_resp=lambda n: _err())
    journal = TradeJournal(tmp_path / "g.jsonl")
    g = _guard(live, snap, spot=293.0, journal=journal)
    g.set_plans({"BTC": Plan("BTC", "long", 82970.0, 83337.0, 0.05, 5.86)})
    with caplog.at_level(logging.INFO):
        g.run_once()
        assert len(live.stops) == 2  # first try + one in-pass retry
        # Not closed yet: entries blocked, the next pass (~3s) retries.
        assert live.reduces == [] and "BTC" in g.unprotected
        assert any("STOP_RETRY BTC" in r.message for r in caplog.records)
        g.run_once()
    assert len(live.stops) == 4
    assert live.reduces == [("BTC", False, 0.05, 83110.0)]  # reduce-only sell, full size
    assert ("BTC", 11) in live.cancels  # resting entry pulled so it cannot refill naked
    assert any("MODEL_B NAKED_CLOSE BTC" in r.message for r in caplog.records)
    assert any("MODEL_B ALERT kind=NAKED_CLOSE coin=BTC" in r.message for r in caplog.records)
    rows = [r for r in journal.read_all() if r["event"] == "model_b_alert"]
    assert rows and rows[0]["kind"] == "NAKED_CLOSE" and rows[0]["reason"] == "stop_reject"
    assert "BTC" in g.unprotected


def test_price_through_planned_stop_closes_instead_of_placing(caplog):
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=82960.0, upnl=-0.1),))
    live = FakeLive()
    g = _guard(live, snap, spot=293.0, loss_kill_r=100.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82970.0, 83337.0, 0.05, 5.86)})
    with caplog.at_level(logging.INFO):
        g.run_once()
    assert live.stops == []
    assert live.reduces == [("BTC", False, 0.05, 82960.0)]
    assert any("reason=past_stop" in r.message for r in caplog.records)


def test_loss_kill_on_planned_risk_even_with_a_stop(caplog):
    snap = AccountSnapshot(
        ok=True,
        positions=(_pos(szi=0.05, mark=83000.0, upnl=-6.5),),
        protective_orders=(ProtectiveOrder("BTC", 31, "sl", "sell", 82900.0, 0.05),),
    )
    live = FakeLive()
    g = _guard(live, snap, spot=293.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82900.0, 83337.0, 0.05, 5.86)})
    with caplog.at_level(logging.INFO):
        events = g.run_once()
    assert live.reduces == [("BTC", False, 0.05, 83000.0)]
    assert events[0]["kind"] == "LOSS_KILL" and events[0]["basis"] == "planned_risk"
    assert any("MODEL_B ALERT kind=LOSS_KILL coin=BTC" in r.message for r in caplog.records)


def test_loss_kill_uses_two_percent_of_account_when_plan_unknown():
    # The incident: ~$11.6k BTC on a ~$293 account, no stop, -$35 open.
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.1402, mark=82856.0, upnl=-35.05),))
    live = FakeLive()
    events = _guard(live, snap, spot=293.0, max_leverage=50).run_once()
    assert events[0]["kind"] == "LOSS_KILL" and events[0]["basis"] == "hard_cap_2.00pct"
    assert live.reduces == [("BTC", False, 0.1402, 82856.0)]


def test_oversize_cut_back_to_ticket_and_to_20x():
    live = FakeLive()
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.10, mark=83110.0, upnl=0.0),))
    g = _guard(live, snap, spot=1000.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82970.0, 0.0, 0.05, 7.0)})
    events = g.run_once()
    assert events[0]["kind"] == "OVERSIZE_CUT"
    assert live.reduces[0] == ("BTC", False, 0.05, 83110.0)
    assert live.stops[-1][2] == pytest.approx(0.05)  # stop sized to what is left
    # No plan: 0.14 BTC on $293 is ~40x -> cut to 20x of the account.
    live2 = FakeLive()
    snap2 = AccountSnapshot(ok=True, positions=(_pos(szi=0.14, mark=83100.0, upnl=0.0),))
    ev2 = _guard(live2, snap2, spot=293.0).run_once()
    assert ev2[0]["kind"] == "OVERSIZE_CUT"
    assert live2.reduces[0][2] == pytest.approx(0.14 - 20 * 293.0 / 83100.0, rel=1e-6)


def test_own_triggers_are_pulled_when_the_coin_goes_flat():
    live = FakeLive()
    g = _guard(live, AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=83110.0),)), spot=293.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82970.0, 83337.0, 0.05, 5.86)})
    g.run_once()
    placed = g.own_oids("BTC")
    assert len(placed) == 2
    g.info.snapshot = AccountSnapshot(ok=True)
    g.run_once()
    assert {oid for _c, oid in live.cancels} == placed
    assert g.own_oids("BTC") == set()


def test_guard_thread_runs_on_a_fast_cadence():
    live = FakeLive()
    calls = {"n": 0}

    class CountingInfo(FakeInfo):
        def load_account_snapshot(self, user, dexs=None, **_kw):
            calls["n"] += 1
            return AccountSnapshot(ok=True)

    g = PositionGuard(live, CountingInfo(None), user="0xabc", dexs=("",))
    g.start(0.01)
    time.sleep(0.2)
    g.stop()
    assert calls["n"] >= 5


# ---------------------------------------------------------------- loop


def _arm_world(now):
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(5000.0)
    info.inject_account_snapshot(AccountSnapshot(ok=True))
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})
    return info, feed


def test_snapshot_fill_before_websocket_is_bracketed_not_adopted_naked(tmp_path, caplog):
    """Oct 7 23:02 replay: the snapshot saw the fill first.

    Old path: ADOPT (stop 0), websocket fills added on top (0.17774 >
    0.141), brackets skipped, thesis_stale cancelled the remainder, no
    stop ever. Now: the ticket's stop/TP are claimed, the size is the
    exchange size, the stop goes on in the same pass, and the stale
    cancel pulls only the entry.
    """
    now = _now()
    info, feed = _arm_world(now)
    live = FakeLive()
    state = {}

    def sleep_fn(_sec):
        n = state.get("n", 0) + 1
        state["n"] = n
        if n == 1:
            assert live.alos, "fixture must arm"
            coin, _buy, size, px, _lev = live.alos[0]
            state["px"], state["size"] = px, size
            state["part"] = round(size * 0.4, 6)
            # The exchange already holds 40% while the websocket is silent.
            info.inject_account_snapshot(
                AccountSnapshot(
                    ok=True,
                    positions=(PerpPosition("BTC", state["part"], px, 0.0, mark=px),),
                    entry_orders=(EntryOrder("BTC", 11, "long", px, size - state["part"]),),
                )
            )
        elif n == 2:
            px, part = state["px"], state["part"]
            # The websocket now delivers the same fill (two drips) late, and a
            # print through the sweep makes the thesis stale.
            feed._fills.append(UserFill("BTC", 11, px, part * 0.75, now + 1, False))
            feed._fills.append(UserFill("BTC", 11, px, part * 0.25, now + 1.5, False))
            stop_oid = 501
            # A buy print below the 99 sweep: thesis stale for the remainder.
            feed._prints.append(
                TradePrint(ts=now + 2, coin="BTC", price=98.5, size=0.1, side="buy", seq=999)
            )
            info.inject_account_snapshot(
                AccountSnapshot(
                    ok=True,
                    positions=(PerpPosition("BTC", part, px, 0.0, mark=px),),
                    entry_orders=(EntryOrder("BTC", 11, "long", px, state["size"] - part),),
                    protective_orders=(
                        ProtectiveOrder("BTC", stop_oid, "sl", "sell", state["stop"], part),
                    ),
                )
            )

    orig_stop = live.set_stop_loss

    def record_stop(coin, is_buy, size, trigger_px):
        state.setdefault("stop", trigger_px)
        return orig_stop(coin, is_buy, size, trigger_px)

    live.set_stop_loss = record_stop
    with caplog.at_level(logging.INFO):
        run_model_b(
            _live_account_settings(tmp_path, "claim.jsonl"),
            max_iterations=3,
            info=info,
            feed=feed,
            exchange=live,
            sleep_fn=sleep_fn,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC",),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    part = state["part"]
    # Exactly one stop, sized to the exchange position, at the ticket stop.
    assert len(live.stops) == 1
    assert live.stops[0][2] == pytest.approx(part)
    assert live.stops[0][3] < state["px"]
    assert live.tps and live.tps[0][2] == pytest.approx(part)
    msgs = [r.message for r in caplog.records]
    assert any("source=snapshot" in m and "MODEL_B FILL BTC" in m for m in msgs)
    assert not any("MODEL_B ADOPT BTC long" in m for m in msgs)
    # thesis_stale pulled only the unfilled entry; the stop stayed.
    assert ("BTC", 11) in live.cancels
    assert all(oid != 501 for _c, oid in live.cancels)
    assert any("remainder cancelled reason=thesis_stale" in m for m in msgs)
    rows = TradeJournal(tmp_path / "claim.jsonl").read_all()
    opens = [r for r in rows if r["event"] == "open"]
    assert len(opens) == 1 and opens[0]["stop"] > 0 and opens[0]["size"] == pytest.approx(part)


def test_websocket_partial_fill_gets_bracket_in_the_same_pass(tmp_path):
    """Websocket drip first, snapshot after: stop sized to the exchange size."""
    now = _now()
    info, feed = _arm_world(now)
    live = FakeLive()
    state = {}

    def sleep_fn(_sec):
        state["n"] = state.get("n", 0) + 1
        if state["n"] == 1:
            _c, _b, size, px, _l = live.alos[0]
            part = round(size * 0.3, 6)
            state["part"] = part
            feed._fills.append(UserFill("BTC", 11, px, part, now + 1, False))
            info.inject_account_snapshot(
                AccountSnapshot(
                    ok=True,
                    positions=(PerpPosition("BTC", part, px, 0.0, mark=px),),
                    entry_orders=(EntryOrder("BTC", 11, "long", px, size - part),),
                )
            )

    run_model_b(
        _live_account_settings(tmp_path, "ws.jsonl"),
        max_iterations=2,
        info=info,
        feed=feed,
        exchange=live,
        sleep_fn=sleep_fn,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert len(live.stops) == 1
    assert live.stops[0][2] == pytest.approx(state["part"])
    assert live.reduces == []


def test_thesis_book_never_tracks_more_than_the_ticket():
    book = ThesisBook()
    intent = AloIntent(
        coin="BTC", side="long", limit_px=83106.0, size=0.14105, stop=83083.0,
        take_profit=83160.0, swing_id="s1", tif="Alo", market_fallback=False,
        leverage=40, work_sec=0.0, sweep_px=83106.0, tick=1.0, pool_px=83160.0, tp_r=1.67,
    )
    book.post(intent, 0.0, oid=11)
    claimed = book.claim_fill(coin="BTC", side="long", size=0.08887, entry=83106.0, now=1.0)
    assert claimed is not None and not claimed.adopted and claimed.stop > 0
    # Late websocket copies of the same fills.
    book.apply_user_fill(coin="BTC", oid=11, price=83106.0, ts=2.0, crossed=False, size=0.08887)
    book.apply_user_fill(coin="BTC", oid=11, price=83106.0, ts=2.1, crossed=False, size=0.001)
    assert book.position("BTC").size <= 0.14105 + 1e-12
    # The snapshot sync puts it back on the exchange size.
    book.adopt_position(coin="BTC", side="long", size=0.08987, entry=83106.0, now=3.0)
    assert book.position("BTC").size == pytest.approx(0.08987)
    plans = plans_from_book(book)
    assert plans["BTC"].stop == book.position("BTC").stop
    assert plans["BTC"].intended_size == pytest.approx(0.14105)


# ---------------------------------------------------------------- sizing


def test_tight_stop_size_is_capped_at_20x_and_min_stop_distance():
    spot, entry, stop = 293.06, 83106.0, 83083.0
    old, _ = size_from_stop(spot, entry, stop, risk_pct=0.02, leverage=40)
    assert old * entry == pytest.approx(spot * 40, rel=1e-4)  # PR #8: ~$11.7k
    capped, _ = size_from_stop(spot, entry, stop, risk_pct=0.02, leverage=40, notional_leverage=20)
    assert capped * entry <= spot * 20 + 1e-6
    braked, risk = size_from_stop(
        spot, entry, stop, risk_pct=0.02, leverage=40, notional_leverage=20, min_stop_bps=15
    )
    assert braked == pytest.approx(spot * 0.02 / (entry * 15 / 10_000), rel=1e-4)
    assert braked * entry < spot * 20
    assert risk <= spot * 0.02 + 1e-9  # never above 2%


# ---------------------------------------------------------------- 22:56 replay


def _replay_world():
    fx = json.loads(FIXTURE.read_text())
    now = fx["now"]
    bars = [{"t": t, "o": o, "h": h, "l": l, "c": c} for t, o, h, l, c in fx["bars"]]
    prints = []
    seq = 0

    def add(ts, px, sz, side):
        nonlocal seq
        seq += 1
        prints.append(TradePrint(ts=now + ts, coin="BTC", price=px, size=sz, side=side, seq=seq))

    # 22:53:34-22:54:37: -32 BTC of selling through the 83138 swing
    # (the bot logged dW -20 to -30 across 22:53:51-22:55:12).
    for i in range(64):
        add(-175 + i, 83137 - i * 0.5, 0.5, "sell")
    for i in range(4):
        add(-110 + i, 83120, 0.5, "buy")
    # Buyers back above the swing, then a last sweep to ~83106 and a
    # small reclaim burst: the 90s window reads +16.5, absorb ~2.7.
    for i in range(38):
        add(-90 + i * 1.7, 83145, 0.5, "buy")
    for i in range(8):
        add(-20 + i * 0.5, 83110 - i * 0.5, 0.5, "sell")
    for i in range(6):
        add(-13 + i * 2, 83140, 0.25, "buy")
    return now, bars, prints


def _replay(**kw):
    now, bars, prints = _replay_world()
    kw.setdefault("cap_includes_fees", False)
    engine = ModelBEngine(tp_r=1.67, risk_pct=0.02, coins=("BTC",), **kw)
    return engine.evaluate(
        "BTC",
        now=now,
        prints=prints,
        bars=bars,
        pools=[Pool("PDH", 86670.0, False)],
        best_bid=83139.0,
        best_ask=83141.0,
        equity=293.06,
        tick=1.0,
        leverage=40,
    )


def test_replay_2256_btc_arm_reproduces_with_old_settings():
    d = _replay(
        counter_flow_filter=False,
        structure_filter=False,
        min_stop_bps=0.0,
        max_notional_leverage=40,
        min_sweep_bps=0.0,
    )
    assert d.armed and d.intent is not None
    assert d.bias == "long"
    assert (d.intent.limit_px, d.intent.stop, d.intent.take_profit) == (83106.0, 83083.0, 83160.0)
    assert d.intent.size == pytest.approx(0.141053, rel=1e-4)  # ~$11.7k on $293


def test_replay_2256_btc_arm_is_rejected_now(caplog):
    d = _replay()
    assert not d.armed
    assert d.fail_reason == "COUNTER_FLOW"
    assert d.counter_flow is not None and "adverse=-32" in d.counter_flow
    # Without the flow veto the 2.8 bps liquidity stop is not kept: it moves
    # out past 15 bps to real liquidity and the TP must be a pool at 1.67R.
    d2 = _replay(counter_flow_filter=False)
    assert d2.fail_reason in (None, "STOP_TOO_TIGHT")
    if d2.armed:
        i = d2.intent
        assert abs(i.limit_px - i.stop) / i.limit_px * 10_000 >= 15 - 1e-9
        assert abs(i.take_profit - i.limit_px) >= 1.67 * abs(i.limit_px - i.stop) - 1e-9
        assert i.size * i.limit_px <= 293.06 * 20 + 1e-6
        assert i.size * abs(i.limit_px - i.stop) <= 293.06 * 0.02 + 1e-9
    # No liquidity past the min distance -> skip.
    d3 = _replay(counter_flow_filter=False, min_stop_bps=400.0)
    assert d3.fail_reason == "STOP_TOO_TIGHT"
    # A per-coin sweep floor deeper than the 22:56 sweep (~3 bps) blocks it.
    d4 = _replay(counter_flow_filter=False, min_sweep_bps="BTC:5,default:0.3")
    assert d4.fail_reason == "SHALLOW_SWEEP"


# ---------------------------------------------------------------- filters


def _trend_bars(now, step):
    """1m bars over 2 days with a zig-zag drifting by ``step`` per leg."""
    bars = []
    px = 1000.0
    t0 = int(now // 60) * 60 - 2 * 24 * 3600
    for i in range(2 * 24 * 60):
        leg = (i // 45) % 2  # 45m up, 45m down
        drift = step / 45.0
        px += (drift + 1.0) if leg == 0 else (drift - 1.0)
        bars.append({"t": (t0 + i * 60) * 1000, "o": px, "h": px + 0.5, "l": px - 0.5, "c": px})
    return bars


def test_structure_bear_blocks_longs_and_bull_blocks_shorts():
    now = 1_791_428_189.0
    bear = _trend_bars(now, -10.0)
    bull = _trend_bars(now, +10.0)
    s_bear = [classify(bear, tf, now) for tf in ("15m", "1h")]
    s_bull = [classify(bull, tf, now) for tf in ("15m", "1h")]
    assert any(s.state == "bear" for s in s_bear)
    assert any(s.state == "bull" for s in s_bull)
    assert structure_blocks("long", s_bear) and not structure_blocks("short", s_bear)
    assert structure_blocks("short", s_bull) and not structure_blocks("long", s_bull)
    # Too little history is unknown and does not block.
    assert classify(bear[-30:], "1h", now).state == "unknown"


def test_counter_flow_needs_a_real_flip_that_holds():
    now = 10_000.0
    prints = [TradePrint(ts=now - 200 + i, coin="BTC", price=100.0, size=1.0, side="sell", seq=i) for i in range(30)]
    late = [TradePrint(ts=now - 20 + i, coin="BTC", price=100.0, size=1.0, side="buy", seq=100 + i) for i in range(10)]
    weak = counter_flow("long", prints + late, coin="BTC", now=now, mid=100.0, usdc=1000.0)
    assert weak.blocked and weak.adverse == pytest.approx(-30.0)
    strong = [TradePrint(ts=now - 80 + i * 2, coin="BTC", price=100.0, size=1.0, side="buy", seq=200 + i) for i in range(40)]
    flipped = counter_flow("long", prints + strong, coin="BTC", now=now, mid=100.0, usdc=1000.0)
    assert not flipped.blocked
    # Small flow (under the USDC band) never vetoes.
    small = counter_flow("long", prints + late, coin="BTC", now=now, mid=100.0, usdc=1e9)
    assert not small.blocked


# ------------------------------------------------- hard 2% loss cap (Chris)


def test_load_account_snapshot_network_path_parses_protective_orders(monkeypatch):
    """The real HTTP path (no injection): 23:32 testnet start hit a NameError here."""
    import hl_bot.exchange.info_client as ic

    def fake_post(_url, body, timeout=15.0):
        if body["type"] == "clearinghouseState":
            if body.get("dex"):
                return 200, {"assetPositions": [], "marginSummary": {"totalMarginUsed": "0"}}
            return 200, {
                "assetPositions": [
                    {"position": {"coin": "BTC", "szi": "0.05", "entryPx": "83106",
                                  "positionValue": "4155.5", "unrealizedPnl": "0.2",
                                  "marginUsed": "200"}}
                ],
                "marginSummary": {"totalMarginUsed": "200"},
            }
        if body["type"] == "frontendOpenOrders":
            if body.get("dex"):
                return 200, []
            return 200, [
                {"coin": "BTC", "oid": 7, "side": "A", "sz": "0.05", "origSz": "0.05",
                 "limitPx": "82900", "triggerPx": "82970", "isTrigger": True,
                 "reduceOnly": True, "orderType": "Stop Market", "isPositionTpsl": False},
            ]
        return 200, []

    monkeypatch.setattr(ic, "_post_info", fake_post)
    snap = InfoClient().load_account_snapshot("0xabc", ("", "xyz"))
    assert snap.ok and snap.positions[0].size == pytest.approx(0.05)
    assert [o.oid for o in snap.protection("BTC")] == [7]


def test_cap_size_to_loss_never_exceeds_two_percent():
    import random

    rnd = random.Random(7)
    for _ in range(5000):
        account = rnd.uniform(1, 100_000)
        entry = rnd.uniform(0.0001, 100_000)
        stop = entry * (1 + rnd.choice([-1, 1]) * rnd.uniform(1e-6, 0.3))
        size = rnd.uniform(0, 10) * account / entry * 50
        out = cap_size_to_loss(size, entry, stop, account, max_loss_pct=rnd.choice([0.02, 0.05, 1.0]))
        assert 0 <= out <= size
        assert out * abs(entry - stop) <= HARD_MAX_LOSS_PCT * account * (1 + 1e-9)
    # Rounding helper floors; a size that floors to nothing is refused.
    assert cap_size_to_loss(1.0, 100.0, 99.0, 10.0, round_down=lambda x: int(x * 100) / 100) == 0.2
    def boom(_x):
        raise ValueError("rounds to 0")
    assert cap_size_to_loss(1.0, 100.0, 99.0, 10.0, round_down=boom) == 0.0


def test_engine_tickets_never_risk_more_than_two_percent():
    import random

    rnd = random.Random(11)
    for _ in range(3000):
        equity = rnd.uniform(5, 50_000)
        entry = rnd.uniform(0.01, 100_000)
        stop = entry * (1 - rnd.uniform(1e-6, 0.2))
        size, _d = size_from_stop(
            equity, entry, stop, risk_pct=0.02, leverage=rnd.choice([3, 10, 20, 40, 50]),
            notional_leverage=rnd.choice([1, 20, 50]), min_stop_bps=rnd.choice([0, 15]),
        )
        size = cap_size_to_loss(size, entry, stop, equity)
        assert size * (entry - stop) <= 0.02 * equity * (1 + 1e-9)


def test_settings_refuse_a_loss_cap_above_two_percent(tmp_path):
    with pytest.raises(ValueError):
        _live_account_settings(tmp_path, "x.jsonl", model_b_max_loss_pct=0.021).validate()
    with pytest.raises(ValueError):
        _live_account_settings(tmp_path, "x.jsonl", model_b_max_loss_pct=0.0).validate()
    s = _live_account_settings(tmp_path, "x.jsonl")
    s.validate()
    assert s.model_b_max_loss_pct == 0.02
    # Startup assertion: the loop refuses to run past the cap even unvalidated.
    with pytest.raises((RuntimeError, ValueError), match="LOSS"):
        run_model_b(
            _live_account_settings(tmp_path, "y.jsonl", model_b_max_loss_pct=0.03),
            max_iterations=1,
            info=InfoClient(),
            feed=MemoryFeed(),
            exchange=FakeLive(),
            sleep_fn=lambda _s: None,
            connect_feed=False,
            coins=("BTC",),
        )


def test_guard_clamps_any_loss_setting_to_two_percent():
    g = _guard(FakeLive(), AccountSnapshot(ok=True), max_loss_pct=0.5)
    assert g.max_loss_pct == HARD_MAX_LOSS_PCT
    assert g.risk_pct <= HARD_MAX_LOSS_PCT


def test_loss_kill_at_two_percent_even_when_planned_risk_is_bigger(caplog):
    # Plan says $30 risk on a $293 account (10%): the hard cap ($5.86) wins.
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=82990.0, upnl=-5.9),))
    live = FakeLive()
    g = _guard(live, snap, spot=293.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82500.0, 83337.0, 0.05, 30.0)})
    with caplog.at_level(logging.WARNING):
        events = g.run_once()
    assert events[0]["kind"] == "LOSS_KILL"
    assert events[0]["basis"] == "hard_cap_2.00pct" and events[0]["limit"] == pytest.approx(5.86)
    assert live.reduces and any("MODEL_B ALERT kind=LOSS_KILL" in r.getMessage() for r in caplog.records)


def test_loss_kill_uses_planned_risk_when_it_is_smaller():
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=83040.0, upnl=-3.3),))
    live = FakeLive()
    g = _guard(live, snap, spot=1000.0)
    g.set_plans({"BTC": Plan("BTC", "long", 83040.0, 83337.0, 0.05, 3.3)})
    events = g.run_once()
    assert events[0]["kind"] == "LOSS_KILL" and events[0]["basis"] == "planned_risk"


def test_manual_position_with_a_wide_stop_gets_a_cap_stop():
    # Manual long, its own stop 5% away: loss there = 0.05 x 4155 = $207 on $293.
    snap = AccountSnapshot(
        ok=True,
        positions=(_pos(szi=0.05, mark=83110.0),),
        protective_orders=(ProtectiveOrder("BTC", 31, "sl", "sell", 78950.0, 0.05),),
    )
    live = FakeLive()
    _guard(live, snap, spot=293.0).run_once()
    assert len(live.stops) == 1
    _c, _b, size, trigger = live.stops[0]
    assert size == pytest.approx(0.05)
    assert (83106.0 - trigger) * 0.05 <= 0.02 * 293.0 + 1e-9
    assert all(oid != 31 for _c2, oid in live.cancels)  # the manual stop is not ours to pull


def test_cap_uses_account_value_seen_at_open():
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=83100.0, upnl=-0.3),))
    live = FakeLive()
    g = _guard(live, snap, spot=293.0)
    g.run_once()
    assert g.loss_cap(_pos(szi=0.05), None) == pytest.approx(5.86)
    g.info.spot = 250.0
    g.info.snapshot = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, mark=83000.0, upnl=-5.3),))
    events = g.run_once()
    assert not any(e["kind"] == "LOSS_KILL" for e in events)  # -5.30 < 2% of 293 at open
    g.info.snapshot = AccountSnapshot(ok=True)
    g.run_once()
    assert g._open_account == {}


def test_guard_property_no_position_survives_past_two_percent():
    import random

    rnd = random.Random(3)
    for _ in range(400):
        account = rnd.uniform(50, 5000)
        entry = rnd.uniform(10, 100_000)
        side = rnd.choice(["long", "short"])
        size = rnd.uniform(0.1, 30) * account / entry
        move = rnd.uniform(-0.05, 0.05)
        mark = entry * (1 + move)
        sign = 1 if side == "long" else -1
        upnl = (mark - entry) * size * sign
        pos = _pos(szi=size * sign, entry=entry, mark=mark, upnl=upnl)
        live = FakeLive()
        g = _guard(live, AccountSnapshot(ok=True, positions=(pos,)), spot=account, max_leverage=50)
        if rnd.random() < 0.5:
            stop = entry * (1 - sign * rnd.uniform(0.001, 0.2))
            g.set_plans({"BTC": Plan("BTC", side, stop, 0.0, size, size * abs(entry - stop))})
        events = g.run_once()
        killed = any(e["kind"] in ("LOSS_KILL", "NAKED_CLOSE") for e in events)
        if upnl <= -0.02 * account:
            assert killed, (side, upnl, account)
        for _c, _b, sz, trig in live.stops:
            assert abs(entry - trig) * sz <= 0.02 * account * (1 + 1e-6) + 1e-9


def test_loop_shrinks_an_oversized_ticket_before_placement(tmp_path, caplog, monkeypatch):
    """Even if sizing upstream misbehaves, the order sent risks <= 2%."""
    import hl_bot.strategy.model_b.engine as eng

    real = eng.size_from_stop
    monkeypatch.setattr(eng, "size_from_stop", lambda *a, **k: (real(*a, **k)[0] * 3, 300.0))
    monkeypatch.setattr(eng, "cap_size_to_loss", lambda size, *a, **k: size)
    now = _now()
    info, feed = _arm_world(now)
    live = FakeLive()
    with caplog.at_level(logging.INFO):
        run_model_b(
            _live_account_settings(tmp_path, "cap.jsonl"),
            max_iterations=1,
            info=info,
            feed=feed,
            exchange=live,
            sleep_fn=lambda _s: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC",),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("MODEL_B LOSS_CAP hard=2.00%") for m in msgs)
    assert any(m.startswith("MODEL_B LOSS_CAP BTC size") for m in msgs)
    lev = [m for m in msgs if m.startswith("MODEL_B LEVERAGE BTC")]
    assert lev and live.alos
    risk = float(lev[0].split(" risk=")[1].split()[0])
    assert risk <= 0.02 * 5000.0 + 1e-6

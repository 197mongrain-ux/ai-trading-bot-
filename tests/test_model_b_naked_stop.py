"""A naked position gets a 2% stop, not a market close (Chris, Oct 8 09:04 ET).

"dont close position that has no stop. place a stop at 2 percent."

The guard closes a naked position only when the 2% absolute rule leaves no
choice: price already past the 2% level, or the stop is rejected on two
guard passes in a row. Sizing, leverage, the 20x cap, LOSS_KILL and
OVERSIZE_CUT are untouched (covered in test_model_b_protection.py).
"""

from __future__ import annotations

import logging

import pytest

from hl_bot.exchange.account import AccountSnapshot, EntryOrder, PerpPosition, ProtectiveOrder
from hl_bot.execution.guard import Plan, PositionGuard
from hl_bot.journal import TradeJournal
from hl_bot.strategy.model_b.risk import (
    HARD_MAX_LOSS_PCT,
    cap_size_to_loss,
    loss_at_stop,
    size_from_stop,
)
from tests.test_model_b_protection import FakeInfo, FakeLive, _err, _ok


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
        include_fees=kw.pop("include_fees", True),  # the live default
        **kw,
    )


def _pos(coin="BTC", szi=0.05, entry=83000.0, mark=None, upnl=None):
    return PerpPosition(
        coin=coin, szi=szi, entry=entry, margin_used=0.0,
        mark=entry if mark is None else mark, unrealized_pnl=upnl,
    )


def _snap(*positions, stops=(), entries=()):
    return AccountSnapshot(ok=True, positions=tuple(positions),
                           protective_orders=tuple(stops), entry_orders=tuple(entries))


def _alerts(events, kind):
    return [e for e in events if e["kind"] == kind]


# ------------------------------------------------------------ 2% stop placed


@pytest.mark.parametrize("szi", [0.05, -0.05])
def test_naked_position_gets_full_size_two_percent_stop_long_and_short(szi, tmp_path, caplog):
    pos = _pos(szi=szi)
    journal = TradeJournal(tmp_path / "j.jsonl")
    live = FakeLive()
    g = _guard(live, _snap(pos), spot=250.0, journal=journal)
    with caplog.at_level(logging.INFO):
        events = g.run_once()
    assert live.reduces == []  # not closed
    assert len(live.stops) == 1
    coin, is_buy, size, trigger = live.stops[0]
    assert coin == "BTC" and size == pytest.approx(0.05)  # full live position
    assert is_buy is (szi < 0)  # reduce-only on the closing side
    if szi > 0:
        assert trigger < 83000.0
    else:
        assert trigger > 83000.0
    # Loss at the trigger, maker entry + taker exit fee included, is 2.00%.
    loss = loss_at_stop(0.05, 83000.0, trigger, include_fees=True)
    assert loss <= 0.02 * 250.0 + 1e-9
    assert loss == pytest.approx(0.02 * 250.0, rel=1e-6)
    alert = _alerts(events, "NAKED_STOP_PLACED")
    assert alert and alert[0]["loss_at_stop"] == "2.00%" and alert[0]["why"] == "no_plan"
    assert any(
        "MODEL_B ALERT NAKED_STOP_PLACED coin=BTC size=0.05 stop=" in r.message
        and "loss_at_stop=2.00%" in r.message
        for r in caplog.records
    )
    assert any(r.get("kind") == "NAKED_STOP_PLACED" for r in journal.read_all())
    assert not _alerts(events, "NAKED_CLOSE")


def test_two_percent_stop_without_fees_matches_price_only_cap():
    live = FakeLive()
    _guard(live, _snap(_pos(szi=0.05)), spot=250.0, include_fees=False).run_once()
    assert live.stops[0][3] == pytest.approx(83000.0 - 0.02 * 250.0 / 0.05)


def test_entries_stay_blocked_until_the_stop_is_seen_resting():
    pos = _pos(szi=0.05)
    live = FakeLive()
    g = _guard(live, _snap(pos), spot=250.0)
    g.run_once()
    assert "BTC" in g.unprotected  # placed, not yet confirmed
    oid, trigger = live._oid, live.stops[0][3]
    # Next pass: the stop is on the book -> confirmed, entries unblocked.
    g.info.snapshot = _snap(pos, stops=[ProtectiveOrder("BTC", oid, "sl", "sell", trigger, 0.05)])
    events = g.run_once()
    assert g.unprotected == set()
    assert len(live.stops) == 1 and live.reduces == [] and events == []


def test_stop_uses_account_value_at_open_not_the_current_balance():
    pos = _pos(szi=0.05)
    live = FakeLive()
    g = _guard(live, _snap(pos), spot=250.0)
    g.loss_cap(pos, 300.0)  # first seen when the account was 300
    g.run_once()
    loss = loss_at_stop(0.05, 83000.0, live.stops[0][3], include_fees=True)
    assert loss == pytest.approx(0.02 * 300.0, rel=1e-6)


# ------------------------------------------------------------ planned stop


def test_planned_stop_tighter_than_two_percent_is_used():
    pos = _pos(szi=0.05)
    live = FakeLive()
    g = _guard(live, _snap(pos), spot=250.0)
    # 2% of 250 = $5 -> ~100 points of room; the plan's stop is 40 points.
    g.set_plans({"BTC": Plan("BTC", "long", 82960.0, 83070.0, 0.05, 2.0)})
    events = g.run_once()
    assert live.stops == [("BTC", False, 0.05, 82960.0)]
    assert live.tps == [("BTC", False, 0.05, 83070.0)]  # planned TP placed when missing
    assert live.reduces == [] and not _alerts(events, "NAKED_CLOSE")


def test_no_tp_is_invented_without_a_plan():
    live = FakeLive()
    _guard(live, _snap(_pos(szi=0.05)), spot=250.0).run_once()
    assert live.tps == []


def test_price_past_planned_stop_but_inside_two_percent_gets_two_percent_stop(caplog):
    # Partial fill (0.02 of a 0.05 ticket) already past its 40-point stop:
    # before this PR the guard market-closed it ("close for nothing").
    pos = _pos(szi=0.02, mark=82950.0)
    live = FakeLive()
    g = _guard(live, _snap(pos), spot=250.0)
    g.set_plans({"BTC": Plan("BTC", "long", 82960.0, 83070.0, 0.05, 2.0)})
    with caplog.at_level(logging.INFO):
        events = g.run_once()
    assert live.reduces == []
    assert len(live.stops) == 1
    _, _, size, trigger = live.stops[0]
    assert size == pytest.approx(0.02) and trigger < 82950.0
    assert loss_at_stop(0.02, 83000.0, trigger, include_fees=True) == pytest.approx(5.0, rel=1e-6)
    alert = _alerts(events, "NAKED_STOP_PLACED")
    assert alert and alert[0]["why"] == "planned_stop_passed"
    # Next pass with that stop resting: nothing re-placed, nothing closed.
    g.info.snapshot = _snap(
        pos, stops=[ProtectiveOrder("BTC", live._oid - 1, "sl", "sell", trigger, 0.02)]
    )
    stops_before = len(live.stops)
    g.run_once()
    assert len(live.stops) == stops_before and live.reduces == []
    assert g.unprotected == set()


# ------------------------------------------------------------ close fallbacks


def test_price_already_past_two_percent_level_closes(caplog):
    # Mark is 300 points under entry: 0.05 x 300 = $15 > 2% of 250.
    # A stale uPnL keeps LOSS_KILL out of the way so the stop path decides.
    pos = _pos(szi=0.05, mark=82700.0, upnl=-0.1)
    live = FakeLive()
    g = _guard(live, _snap(pos, entries=[EntryOrder("BTC", 11, "long", 82990.0, 0.01)]),
               spot=250.0, loss_kill_r=100.0)
    with caplog.at_level(logging.INFO):
        events = g.run_once()
    assert live.stops == []
    assert live.reduces == [("BTC", False, 0.05, 82700.0)]  # full size, reduce-only
    assert ("BTC", 11) in live.cancels
    close = _alerts(events, "NAKED_CLOSE")
    assert close and close[0]["reason"] == "past_stop"
    assert not _alerts(events, "NAKED_STOP_PLACED")
    assert any("MODEL_B ALERT kind=NAKED_CLOSE coin=BTC" in r.message for r in caplog.records)


def test_placement_failure_closes_on_the_next_pass(caplog):
    pos = _pos(szi=0.05)
    live = FakeLive(stop_resp=lambda n: _err("Too many open orders"))
    g = _guard(live, _snap(pos), spot=250.0)
    with caplog.at_level(logging.INFO):
        first = g.run_once()
    assert live.reduces == [] and "BTC" in g.unprotected
    assert not _alerts(first, "NAKED_CLOSE")
    second = g.run_once()
    assert live.reduces == [("BTC", False, 0.05, 83000.0)]
    close = _alerts(second, "NAKED_CLOSE")
    assert close and close[0]["reason"] == "stop_reject"
    assert "BTC" in g.unprotected


def test_placement_failure_then_success_does_not_close():
    pos = _pos(szi=0.05)
    live = FakeLive(stop_resp=lambda n: _err() if n <= 2 else _ok(900 + n))
    g = _guard(live, _snap(pos), spot=250.0)
    g.run_once()
    events = g.run_once()
    assert live.reduces == []
    assert _alerts(events, "NAKED_STOP_PLACED")
    # A later reject on a fresh pass starts over (one retry again).
    g.info.snapshot = _snap(pos, stops=[ProtectiveOrder("BTC", 903, "sl", "sell", live.stops[-1][3], 0.05)])
    g.run_once()
    assert g.unprotected == set()


def test_stop_sized_to_the_full_live_position_not_the_ticket():
    # Exchange shows 0.0731 (drips beyond the bot's 0.05 view); the 2% stop
    # covers all of it.
    pos = _pos(coin="ETH", szi=0.0731, entry=2500.0)
    live = FakeLive()
    g = _guard(live, _snap(pos), spot=250.0, oversize_ratio=10.0)
    g.run_once()
    assert live.stops[0][0] == "ETH" and live.stops[0][2] == pytest.approx(0.0731)
    loss = loss_at_stop(0.0731, 2500.0, live.stops[0][3], include_fees=True)
    assert loss == pytest.approx(5.0, rel=1e-6)


def test_short_foreign_stop_is_topped_up_to_full_size_without_alert():
    # A manual 0.02 stop on a 0.05 position: the guard adds 0.03 at the 2%
    # level. Not naked, so no NAKED_STOP_PLACED.
    pos = _pos(szi=0.05)
    live = FakeLive()
    g = _guard(live, _snap(pos, stops=[ProtectiveOrder("BTC", 77, "sl", "sell", 82950.0, 0.02)]), spot=250.0)
    events = g.run_once()
    assert live.stops and live.stops[0][2] == pytest.approx(0.03)
    assert not _alerts(events, "NAKED_STOP_PLACED") and live.reduces == []


# ------------------------------------------------------------ sizing unchanged


def test_size_cap_still_holds_with_a_very_tight_stop():
    spot, entry, stop = 251.32, 83000.0, 82999.0  # 1-point (~0.12 bp) stop
    size, risk = size_from_stop(
        spot, entry, stop, risk_pct=0.02, leverage=40, notional_leverage=20,
        min_stop_bps=15, include_fees=True,
    )
    assert size * entry <= spot * 20 + 1e-6  # 20x notional cap
    assert risk <= spot * HARD_MAX_LOSS_PCT + 1e-9
    assert loss_at_stop(size, entry, stop, include_fees=True) <= spot * HARD_MAX_LOSS_PCT + 1e-9
    capped = cap_size_to_loss(size, entry, stop, spot, include_fees=True)
    assert capped == pytest.approx(size)  # already inside the cap

"""Adversarial audit of PR #9 (Oct 8 2026): bugs of the Oct 7 BTC class.

Each test pins one way exposure could be left open, oversized, or able to
lose more than 2% of the account:

- fees were outside the 2% cap (Oct 6 ETH stop-out lost 2.56% net);
- a crash in the hunt loop left resting entries live with the guard dead;
- the guard going blind (API down / 429) did not stop new entries;
- HL_NETWORK and HL_API_URL could point at different networks;
- a new ticket's plan reached the guard one pass late;
- a partial taker fill (stop drip, guard cut, manual trim) flattened the book;
- the oversize check used the live balance instead of the balance at open;
- one bad journal line crashed the loop;
- the SDK had no HTTP timeout, so one hung call froze the guard.
"""

from __future__ import annotations

import logging

import pytest

from hl_bot.config import Settings, load_settings
from hl_bot.exchange.account import AccountSnapshot, EntryOrder, PerpPosition, ProtectiveOrder
from hl_bot.exchange.hl_trades import MemoryFeed
from hl_bot.exchange.info_client import InfoClient
from hl_bot.execution import model_b_loop as loop_mod
from hl_bot.execution.guard import Plan, PositionGuard
from hl_bot.execution.model_b_loop import run_model_b
from hl_bot.journal import TradeJournal
from hl_bot.strategy.model_b.risk import (
    MAKER_FEE_RATE,
    TAKER_FEE_RATE,
    cap_size_to_loss,
    size_from_stop,
)
from hl_bot.strategy.model_b.thesis import CloseEvent, ThesisBook
from hl_bot.strategy.model_b.types import AloIntent, Pool
from tests.test_model_b import _live_account_settings, _now
from tests.test_model_b_protection import FakeLive, _arm_world, _guard, _pos


def _loss_with_fees(size, entry, stop):
    return size * abs(entry - stop) + size * entry * MAKER_FEE_RATE + size * stop * TAKER_FEE_RATE


# ------------------------------------------------------------ fees in the cap


def test_oct6_eth_stop_out_replay_fits_two_percent_with_fees():
    """ETH long Oct 6 23:58: $14.80 account, Alo 2610.8, stop 2605.5.

    Sized at 2% price risk it was 0.0558 ETH and lost $0.3795 net
    (-2.56%) at the stop once the maker entry and taker exit fees landed.
    """
    acct, entry, stop = 14.80, 2610.8, 2605.5
    old_size = 0.296 / (entry - stop)
    assert _loss_with_fees(old_size, entry, stop) / acct > 0.025
    size, _ = size_from_stop(
        acct, entry, stop, risk_pct=0.02, leverage=25, notional_leverage=20, include_fees=True
    )
    assert _loss_with_fees(size, entry, stop) <= 0.02 * acct * (1 + 1e-9)


def test_ticket_loss_at_stop_including_fees_never_exceeds_two_percent():
    import random

    rnd = random.Random(5)
    for _ in range(3000):
        acct = rnd.uniform(5, 50_000)
        entry = rnd.uniform(0.01, 100_000)
        side = rnd.choice([-1, 1])
        stop = entry * (1 - side * rnd.uniform(1e-5, 0.2))
        size, _ = size_from_stop(
            acct, entry, stop, risk_pct=0.02, leverage=rnd.choice([3, 20, 40, 50]),
            notional_leverage=20, min_stop_bps=rnd.choice([0, 15]), include_fees=True,
        )
        size = cap_size_to_loss(size, entry, stop, acct, include_fees=True)
        assert _loss_with_fees(size, entry, stop) <= 0.02 * acct * (1 + 1e-9)


def test_engine_sizes_with_fees_by_default():
    """The default engine ticket is fee-inclusive (MODEL_B_CAP_INCLUDES_FEES=1)."""
    s = Settings()
    assert s.model_b_cap_includes_fees is True


def test_guard_hard_cap_counts_entry_and_exit_fees(caplog):
    # $1000 account, 0.1 BTC (8.3x). Price loss $16 + ~$5 fees = $21 > $20.
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.1, entry=83106.0, mark=82946.0, upnl=-16.0),))
    live = FakeLive()
    events = _guard(live, snap, spot=1000.0, include_fees=True).run_once()
    assert events and events[0]["kind"] == "LOSS_KILL"
    assert live.reduces


def test_guard_cap_stop_leaves_room_for_fees():
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=0.05, entry=83106.0, mark=83100.0, upnl=-0.3),))
    live = FakeLive()
    _guard(live, snap, spot=293.0, include_fees=True).run_once()
    _c, _b, size, trigger = live.stops[0]
    assert _loss_with_fees(size, 83106.0, trigger) <= 0.02 * 293.0 + 1e-6


# ------------------------------------------------------------ crash = fail closed


def test_loop_crash_cancels_resting_entries_and_protects_positions(tmp_path):
    """Any exception in the hunt kills the process and the daemon guard with it.

    The resting Alo it leaves can fill later with nobody to place a stop.
    Fail closed: pull this process's entries, run one last guard pass.
    """
    now = _now()
    info, feed = _arm_world(now)
    live = FakeLive()

    def sleep_fn(_s):
        assert live.alos, "fixture must arm"
        raise RuntimeError("boom mid-loop")

    with pytest.raises(RuntimeError, match="boom"):
        run_model_b(
            _live_account_settings(tmp_path, "crash.jsonl"),
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
    assert ("BTC", 11) in live.cancels
    rows = TradeJournal(tmp_path / "crash.jsonl").read_all()
    assert any(r["event"] == "model_b_alert" and r.get("kind") == "LOOP_CRASH" for r in rows)


# ------------------------------------------------------------ blind guard


def test_no_new_entry_while_the_guard_cannot_read_the_account(tmp_path, caplog):
    """API down / 429: the guard cannot verify stops, so nothing new is sent."""
    now = _now()
    info, feed = _arm_world(now)
    info.inject_account_snapshot(AccountSnapshot(ok=False))
    live = FakeLive()
    with caplog.at_level(logging.INFO):
        run_model_b(
            _live_account_settings(tmp_path, "blind.jsonl"),
            max_iterations=2,
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
    assert live.alos == []
    assert any("GUARD_STALE" in r.getMessage() for r in caplog.records)


def test_guard_records_last_verified_pass():
    snap = AccountSnapshot(ok=True)
    g = _guard(FakeLive(), snap)
    g.clock = lambda: 1000.0
    assert g.stale(1000.0, 15.0)
    g.run_once()
    assert not g.stale(1010.0, 15.0)
    assert g.stale(1016.0, 15.0)
    g.info.snapshot = AccountSnapshot(ok=False)
    g.clock = lambda: 1020.0
    g.run_once()
    assert g.stale(1020.0, 15.0) is False or g.last_ok_at == 1000.0
    assert g.last_ok_at == 1000.0


def test_guard_kick_wakes_the_thread_now():
    import threading

    snap = AccountSnapshot(ok=True)
    g = _guard(FakeLive(), snap)
    hits = []
    done = threading.Event()
    real = g.run_once

    def counted(*a, **k):
        hits.append(1)
        if len(hits) >= 2:
            done.set()
        return real(*a, **k)

    g.run_once = counted
    g.start(60.0)
    try:
        import time

        time.sleep(0.2)
        g.kick()
        assert done.wait(2.0), "kick must wake the guard without waiting 60s"
    finally:
        g.stop()


# ------------------------------------------------------------ network mismatch


@pytest.mark.parametrize(
    "network,url",
    [("testnet", "https://api.hyperliquid.xyz"), ("mainnet", "https://api.hyperliquid-testnet.xyz")],
)
def test_network_and_api_url_must_agree(monkeypatch, network, url):
    monkeypatch.setenv("HL_NETWORK", network)
    monkeypatch.setenv("HL_API_URL", url)
    with pytest.raises(ValueError, match="HL_API_URL"):
        load_settings()


# ------------------------------------------------------------ plan race


def test_new_ticket_plan_reaches_the_guard_in_the_same_pass(tmp_path, monkeypatch):
    seen: list[set] = []

    class SpyGuard(PositionGuard):
        def set_plans(self, plans):
            seen.append(set(plans))
            super().set_plans(plans)

    monkeypatch.setattr(loop_mod, "PositionGuard", SpyGuard)
    now = _now()
    info, feed = _arm_world(now)
    live = FakeLive()
    state = {}

    def sleep_fn(_s):
        state["at_sleep"] = list(seen)

    run_model_b(
        _live_account_settings(tmp_path, "plan.jsonl"),
        max_iterations=1,
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
    assert live.alos
    assert state["at_sleep"] and "BTC" in state["at_sleep"][-1]


def test_own_fallback_stop_is_upgraded_to_the_plan_stop():
    # The guard put a fallback stop on before the plan arrived; now it has one.
    pos = _pos(szi=0.05, entry=83106.0, mark=83100.0, upnl=-0.3)
    snap = AccountSnapshot(ok=True, positions=(pos,))
    live = FakeLive()
    g = _guard(live, snap, spot=5000.0)
    g.run_once()
    first = live.stops[0]
    g.info.snapshot = AccountSnapshot(
        ok=True,
        positions=(pos,),
        protective_orders=(ProtectiveOrder("BTC", 501, "sl", "sell", first[3], 0.05),),
    )
    g.set_plans({"BTC": Plan("BTC", "long", 82980.0, 83337.0, 0.05, 0.05 * 126)})
    g.run_once()
    assert live.stops[-1][3] == pytest.approx(82980.0)
    assert ("BTC", 501) in live.cancels


# ------------------------------------------------------------ partial taker fills


def _filled_book(size=1.0):
    book = ThesisBook()
    intent = AloIntent(
        coin="BTC", side="long", limit_px=100.0, size=size, stop=99.0, take_profit=102.0,
        swing_id="s1", tif="Alo", market_fallback=False, leverage=20, work_sec=0.0,
        sweep_px=99.5, tick=0.01, pool_px=102.0, tp_r=1.67,
    )
    book.post(intent, 0.0, oid=11)
    book.apply_user_fill(coin="BTC", oid=11, price=100.0, ts=1.0, crossed=False, size=size)
    return book


def test_partial_taker_reduce_does_not_flatten_the_book():
    book = _filled_book()
    out = book.apply_user_fill(
        coin="BTC", oid=99, price=99.5, ts=2.0, crossed=True, size=0.3, direction="Close Long"
    )
    assert not isinstance(out, CloseEvent)
    pos = book.position("BTC")
    assert pos is not None and pos.size == pytest.approx(0.7) and pos.stop == 99.0
    out = book.apply_user_fill(
        coin="BTC", oid=99, price=99.0, ts=3.0, crossed=True, size=0.7, direction="Close Long"
    )
    assert isinstance(out, CloseEvent)
    assert out.size == pytest.approx(1.0)
    assert out.pnl == pytest.approx(-0.3 * 0.5 - 0.7 * 1.0)


def test_taker_add_is_not_a_close():
    book = _filled_book()
    out = book.apply_user_fill(
        coin="BTC", oid=77, price=100.5, ts=2.0, crossed=True, size=0.2, direction="Open Long"
    )
    assert out is None and book.position("BTC") is not None


# ------------------------------------------------------------ oversize base


def test_oversize_uses_account_at_open_not_a_shrinking_balance():
    # 19x at open on $1000; another coin's loss drops spot to $900.
    pos = _pos(szi=0.2286, entry=83106.0, mark=83100.0, upnl=-1.0)
    live = FakeLive()
    g = _guard(live, AccountSnapshot(ok=True, positions=(pos,)), spot=1000.0)
    g.run_once()
    g.info.spot = 900.0
    events = g.run_once()
    assert not any(e["kind"] == "OVERSIZE_CUT" for e in events)


# ------------------------------------------------------------ unknown account


def test_unknown_account_marks_the_coin_unprotected_and_does_not_cache():
    pos = _pos(szi=0.05, entry=83106.0, mark=83100.0, upnl=-0.3)
    live = FakeLive()
    g = _guard(live, AccountSnapshot(ok=True, positions=(pos,)), spot=None)
    g.run_once()
    assert "BTC" in g.unprotected
    assert g._fallback == {}


# ------------------------------------------------------------ journal / SDK


def test_one_bad_journal_line_does_not_crash_reads(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"event": "open", "ts": 1}\n{"event": "clo\n{"event": "close", "ts": 2}\n')
    rows = TradeJournal(path).read_all()
    assert [r["event"] for r in rows] == ["open", "close"]


def test_live_exchange_sets_an_http_timeout(monkeypatch):
    import hyperliquid.exchange as hx

    from hl_bot.exchange.live_exchange import LiveExchange

    seen = {}

    class FakeExchange:
        def __init__(self, wallet, base_url, **kw):
            seen.update(kw)

    monkeypatch.setattr(hx, "Exchange", FakeExchange)
    LiveExchange(private_key="0x" + "ab" * 32, base_url="https://api.hyperliquid.xyz")
    assert seen.get("timeout") and 0 < seen["timeout"] <= 15


# ------------------------------------------------------------ fee cap keeps the plan stop


def test_fee_sized_ticket_keeps_its_planned_stop_and_tp():
    """A ticket sized fee-inclusive fits the cap exactly: the guard must not move its stop."""
    from hl_bot.strategy.model_b.risk import size_from_stop

    entry, stop, tp = 83106.0, 82970.0, 83337.0
    size, _ = size_from_stop(1000.0, entry, stop, risk_pct=0.02, include_fees=True)
    snap = AccountSnapshot(ok=True, positions=(_pos(szi=size, entry=entry, mark=83110.0),))
    live = FakeLive()
    g = _guard(live, snap, spot=1000.0, include_fees=True)
    g.set_plans({"BTC": Plan("BTC", "long", stop, tp, size, size * (entry - stop))})
    g.run_once()
    assert [s[3] for s in live.stops] == [stop]
    assert [t[3] for t in live.tps] == [tp]


def test_live_ticket_is_fee_inclusive_by_default(tmp_path, monkeypatch):
    """End to end: the Alo sent is sized so price loss + fees at the stop <= 2% of spot."""
    plans: list[dict] = []

    class SpyGuard(PositionGuard):
        def set_plans(self, p):
            plans.append(dict(p))
            super().set_plans(p)

    monkeypatch.setattr(loop_mod, "PositionGuard", SpyGuard)
    now = _now()
    info, feed = _arm_world(now)
    live = FakeLive()
    run_model_b(
        _live_account_settings(tmp_path, "fees.jsonl", model_b_cap_includes_fees=True),
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
    assert live.alos
    _coin, _buy, size, limit, _lev = live.alos[0]
    stop = plans[-1]["BTC"].stop
    assert _loss_with_fees(size, limit, stop) <= 0.02 * 5000.0 + 1e-6
    # And it is the fee-inclusive size, not a needlessly small one.
    assert _loss_with_fees(size, limit, stop) >= 0.02 * 5000.0 * 0.99
    # The old price-only size breaks 2% once fees are paid.
    old = 0.02 * 5000.0 / abs(limit - stop)
    assert _loss_with_fees(old, limit, stop) > 0.02 * 5000.0

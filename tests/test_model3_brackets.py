"""Live bracket protection: partial fills, drips, halt races, tick rounding."""

from decimal import Decimal

import pytest

from hl_bot.config import Settings
from hl_bot.execution.brackets import (
    AccountSnapshot,
    BracketTicket,
    ExchangeOrder,
    ExchangePosition,
    quantize_size,
    quantize_trigger_px,
    plan_protection,
    wire_float,
)
from hl_bot.execution.loop import run_bot
from hl_bot.journal import TradeJournal
from hyperliquid.utils.signing import float_to_wire


def _plan(**kwargs):
    defaults = dict(
        positions=[],
        orders=[],
        tickets={},
        last_abs={},
        journaled_abs={},
        halt_requested=False,
        marks=None,
        stop_pct=Decimal("0.0015"),
        tp_r=Decimal("2"),
        sz_decimals={},
    )
    defaults.update(kwargs)
    return plan_protection(**defaults)


def test_partial_fill_attaches_brackets_at_full_position_size():
    """Alo partial of 30 (not the resting 100) must bracket sz == 30."""
    ticket = BracketTicket(
        "BLUR",
        "long",
        stop=Decimal("0.050"),
        take_profit=Decimal("0.090"),
        entry=Decimal("0.060"),
        sz_decimals=0,
        trade_id="blur1",
    )
    plan = _plan(
        positions=[ExchangePosition("BLUR", Decimal("30"), Decimal("0.060"))],
        tickets={"BLUR": ticket},
        sz_decimals={"BLUR": 0},
    )
    assert plan.journal_events[0]["event"] == "open"
    assert plan.journal_events[0]["size"] == "30"
    assert plan.journal_events[0]["stop"] == "0.050"
    assert all(ev["event"] != "stop" for ev in plan.journal_events)
    assert len(plan.resizes) == 1
    assert plan.resizes[0].size == Decimal("30")
    assert plan.resizes[0].side == "long"
    assert plan.block_new_entries
    assert not plan.allow_process_stop
    assert "BLUR" in plan.naked_coins


def test_drip_growth_resizes_undersized_brackets():
    """ATOM: position 240.68, TP/SL still 205.38 → cancel and replace at 240.68."""
    ticket = BracketTicket(
        "ATOM",
        "long",
        stop=Decimal("4.5"),
        take_profit=Decimal("6.5"),
        entry=Decimal("5"),
        sz_decimals=2,
    )
    stale = [
        ExchangeOrder(11, "ATOM", Decimal("205.38"), True, "sl", Decimal("4.5")),
        ExchangeOrder(12, "ATOM", Decimal("205.38"), True, "tp", Decimal("6.5")),
    ]
    plan = _plan(
        positions=[ExchangePosition("ATOM", Decimal("240.68"), Decimal("5"))],
        orders=stale,
        tickets={"ATOM": ticket},
        last_abs={"ATOM": Decimal("205.38")},
        journaled_abs={"ATOM": Decimal("205.38")},
        sz_decimals={"ATOM": 2},
    )
    assert plan.journal_events[0]["event"] == "open"
    assert plan.journal_events[0]["size"] == "240.68"
    assert plan.journal_events[0]["fill_sz"] == "35.30"
    assert plan.resizes[0].size == Decimal("240.68")
    assert plan.resizes[0].cancel_oids == (11, 12)
    assert plan.resizes[0].existing_legs == ((11, "sl"), (12, "tp"))
    assert not plan.allow_process_stop

    # Once brackets match the live size, do not churn and do not re-open.
    sized = [
        ExchangeOrder(21, "ATOM", Decimal("240.68"), True, "sl", plan.resizes[0].stop_px),
        ExchangeOrder(22, "ATOM", Decimal("240.68"), True, "tp", plan.resizes[0].take_profit_px),
    ]
    held = _plan(
        positions=[ExchangePosition("ATOM", Decimal("240.68"), Decimal("5"))],
        orders=sized,
        tickets={"ATOM": ticket},
        last_abs={"ATOM": Decimal("240.68")},
        journaled_abs={"ATOM": Decimal("240.68")},
        sz_decimals={"ATOM": 2},
    )
    assert held.resizes == ()
    assert held.journal_events == ()
    assert held.allow_process_stop
    assert not held.block_new_entries


def test_fill_plus_halt_journals_open_and_refuses_naked_stop():
    ticket = BracketTicket(
        "BLUR",
        "long",
        stop=Decimal("0.050"),
        take_profit=Decimal("0.090"),
        entry=Decimal("0.060"),
    )
    plan = _plan(
        positions=[ExchangePosition("BLUR", Decimal("100"), Decimal("0.060"))],
        tickets={"BLUR": ticket},
        halt_requested=True,
        sz_decimals={"BLUR": 0},
    )
    assert [ev["event"] for ev in plan.journal_events] == ["open"]
    assert plan.allow_process_stop is False
    assert plan.resizes[0].size == Decimal("100")

    protected = [
        ExchangeOrder(1, "BLUR", Decimal("100"), True, "sl", plan.resizes[0].stop_px),
        ExchangeOrder(2, "BLUR", Decimal("100"), True, "tp", plan.resizes[0].take_profit_px),
    ]
    after = _plan(
        positions=[ExchangePosition("BLUR", Decimal("100"), Decimal("0.060"))],
        orders=protected,
        tickets={"BLUR": ticket},
        last_abs=plan.next_last_abs,
        journaled_abs=plan.next_journaled_abs,
        halt_requested=True,
        sz_decimals={"BLUR": 0},
    )
    assert after.allow_process_stop
    assert after.resizes == ()
    assert after.journal_events == ()


def test_resting_alo_blocks_halt_until_cancelled():
    alo = ExchangeOrder(99, "BLUR", Decimal("50"), False, None, None)
    plan = _plan(orders=[alo], halt_requested=True)
    assert not plan.allow_process_stop
    assert plan.cancel_entry_orders == (("BLUR", 99),)
    idle = _plan(orders=[alo], halt_requested=False)
    assert idle.allow_process_stop
    assert idle.cancel_entry_orders == ()


def test_exchange_flat_journals_tp_close():
    """TP filled on the exchange with no paper close still emits close."""
    ticket = BracketTicket(
        "MORPHO",
        "long",
        stop=Decimal("1.50"),
        take_profit=Decimal("1.80"),
        entry=Decimal("1.60"),
        trade_id="morph1",
    )
    plan = _plan(
        tickets={"MORPHO": ticket},
        last_abs={"MORPHO": Decimal("12")},
        journaled_abs={"MORPHO": Decimal("12")},
        marks={"MORPHO": Decimal("1.81")},
        sz_decimals={"MORPHO": 1},
    )
    assert len(plan.journal_events) == 1
    close = plan.journal_events[0]
    assert close["event"] == "close"
    assert close["reason"] == "tp"
    assert close["symbol"] == "MORPHO"
    assert close["trade_id"] == "morph1"
    assert close["size"] == "12"


def test_trigger_prices_are_decimal_tick_rounded():
    dusty = Decimal("1.234567891234")
    rounded = quantize_trigger_px(dusty, sz_decimals=1)
    # 5 significant figures, and at most 6 - 1 decimal places.
    assert rounded == Decimal("1.2346")
    assert len(rounded.normalize().as_tuple().digits) <= 5
    exp = rounded.as_tuple().exponent
    assert isinstance(exp, int) and -exp <= 5
    # Wire must be exact — this is the MORPHO float-dust rejection.
    assert float_to_wire(wire_float(rounded)) == "1.2346"

    # Large px: sig-fig grid, not the raw float.
    big = quantize_trigger_px(Decimal("123456.789"), sz_decimals=2)
    assert big == Decimal("123460")
    float_to_wire(wire_float(big))

    ticket = BracketTicket(
        "MORPHO",
        "long",
        stop=dusty,
        take_profit=Decimal("1.987654321"),
        entry=Decimal("1.5"),
        sz_decimals=1,
    )
    plan = _plan(
        positions=[ExchangePosition("MORPHO", Decimal("10"), Decimal("1.5"))],
        tickets={"MORPHO": ticket},
        sz_decimals={"MORPHO": 1},
    )
    assert plan.resizes[0].stop_px == Decimal("1.2346")
    assert plan.resizes[0].take_profit_px == quantize_trigger_px(Decimal("1.987654321"), 1)
    assert quantize_size("10.009", 1) == Decimal("10.0")


def test_missing_ticket_recomputes_and_blocks_entries():
    plan = _plan(
        positions=[ExchangePosition("ATOM", Decimal("1.5"), Decimal("100"))],
        stop_pct=Decimal("0.0015"),
        tp_r=Decimal("3"),
        sz_decimals={"ATOM": 1},
    )
    assert plan.block_new_entries
    assert plan.resizes[0].side == "long"
    assert plan.resizes[0].size == Decimal("1.5")
    # Long stop below entry, TP at 3R. 100 * 0.0015 = 0.15.
    assert plan.resizes[0].stop_px < Decimal("100")
    assert plan.resizes[0].take_profit_px > Decimal("100")
    risk = Decimal("100") - plan.resizes[0].stop_px
    reward = plan.resizes[0].take_profit_px - Decimal("100")
    assert reward == pytest.approx(risk * 3)

    short = _plan(
        positions=[ExchangePosition("ATOM", Decimal("-1.5"), Decimal("100"))],
        stop_pct=Decimal("0.0015"),
        tp_r=Decimal("3"),
        sz_decimals={"ATOM": 1},
    )
    assert short.resizes[0].side == "short"
    assert short.resizes[0].stop_px > Decimal("100")
    assert short.resizes[0].take_profit_px < Decimal("100")


class _Info:
    def get_mark_price(self, coin: str = "BTC") -> float:
        return 0.06

    def get_candles(self, *args, **kwargs):
        return []


class _FakeLive:
    def __init__(self, szi: str, *, fail_resize: bool = False):
        self.szi = Decimal(szi)
        self.fail_resize = fail_resize
        self.orders: list[ExchangeOrder] = []
        self.resize_calls: list[Decimal] = []
        self.cancels: list[tuple[str, int]] = []

    def fetch_account(self) -> AccountSnapshot:
        positions = ()
        if self.szi != 0:
            positions = (ExchangePosition("BLUR", self.szi, Decimal("0.06")),)
        return AccountSnapshot(
            positions=positions,
            orders=tuple(self.orders),
            sz_decimals={"BLUR": 0},
        )

    def resize_model3_brackets(
        self, coin, side, size, stop_px, tp_px, sz_decimals, cancel_oids=None, existing_legs=None
    ):
        if self.fail_resize:
            raise RuntimeError("resize failed")
        self.resize_calls.append(Decimal(str(size)))
        self.orders = [
            ExchangeOrder(1, coin, size, True, "sl", stop_px),
            ExchangeOrder(2, coin, size, True, "tp", tp_px),
        ]

    def cancel_orders(self, orders):
        self.cancels.extend(orders)

    def market_open(self, *args, **kwargs):
        return None

    def market_close(self, *args, **kwargs):
        return None


def _live_settings(tmp_path, **overrides):
    params = dict(
        symbols=("BLUR",),
        symbol="BLUR",
        journal_path=str(tmp_path / "trades.jsonl"),
        kill_switch=True,
        loop_interval_sec=0.0,
        htf_confirm=False,
        trade_hours_utc="0-24",
    )
    params.update(overrides)
    return Settings(**params)


def test_run_loop_halt_during_fill_attaches_full_size_before_stop(tmp_path):
    live = _FakeLive("30")
    settings = _live_settings(tmp_path)
    summary = run_bot(
        settings,
        max_iterations=1,
        info=_Info(),
        live=live,
        sleep_fn=lambda _s: None,
    )
    rows = TradeJournal(settings.journal_path).read_all()
    events = [r["event"] for r in rows]
    assert "open" in events
    assert "stop" in events
    assert events.index("open") < events.index("stop")
    assert live.resize_calls
    assert all(sz == Decimal("30") for sz in live.resize_calls)
    assert summary.get("naked") is not True


def test_run_loop_refuses_stop_when_brackets_cannot_attach(tmp_path):
    live = _FakeLive("30", fail_resize=True)
    settings = _live_settings(tmp_path)
    summary = run_bot(
        settings,
        max_iterations=1,
        info=_Info(),
        live=live,
        sleep_fn=lambda _s: None,
    )
    rows = TradeJournal(settings.journal_path).read_all()
    events = [r["event"] for r in rows]
    assert "open" in events
    assert "stop" not in events
    assert "halt_deferred" in events
    assert summary["naked"] is True
    # Open is on the journal before any halt_deferred row.
    assert events.index("open") < events.index("halt_deferred")


def test_journal_refuses_stop_while_naked(tmp_path):
    journal = TradeJournal(tmp_path / "j.jsonl")
    assert journal.log_process_stop(naked_coins=["BLUR"], equity=1) is False
    assert journal.log_process_stop(naked_coins=[], equity=1) is True
    events = [r["event"] for r in journal.read_all()]
    assert events == ["halt_deferred", "stop"]

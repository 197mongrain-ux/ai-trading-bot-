"""Model 3: skip tight stops, refuse noise tightens, one thesis per coin."""

import pytest

from hl_bot.strategy.filters import cooldown_active
from hl_bot.strategy.model3 import (
    MODEL3_MIN_SCORE,
    MODEL3_STUB_STOP_PCT,
    MODEL3_STOP_LIQ_BUFFER_BPS,
    ThesisBook,
    alo_cancel_oids,
    apply_bracket_stop,
    build_ticket,
    fit_stop,
    heal_stop_tp,
    place_stop_beyond_liquidity,
    resolve_stop_liq_buffer_bps,
    size_for_real_stop,
    stop_beyond_liquidity_ok,
)


def test_min_score_constant_stays_at_7():
    assert MODEL3_MIN_SCORE == 7


def test_buffer_default_and_floor_are_2_bps(monkeypatch):
    monkeypatch.delenv("MODEL3_STOP_LIQ_BUFFER_BPS", raising=False)
    assert MODEL3_STOP_LIQ_BUFFER_BPS == 2
    assert resolve_stop_liq_buffer_bps(None) == 2
    assert resolve_stop_liq_buffer_bps(0) == 2
    assert resolve_stop_liq_buffer_bps(1) == 2
    assert resolve_stop_liq_buffer_bps(5) == 5


def test_buffer_env_cannot_undercut_floor(monkeypatch):
    monkeypatch.setenv("MODEL3_STOP_LIQ_BUFFER_BPS", "1")
    assert resolve_stop_liq_buffer_bps(None) == 2
    monkeypatch.setenv("MODEL3_STOP_LIQ_BUFFER_BPS", "8")
    assert resolve_stop_liq_buffer_bps(None) == 8


def test_skip_when_stop_not_beyond_liquidity_no_stub_fallback():
    """SSL on the wrong side of entry cannot arm, and 0.2% is not substituted."""
    entry = 100.0
    stub = entry * (1.0 - MODEL3_STUB_STOP_PCT)
    decision = build_ticket(
        symbol="AAVE",
        side="long",
        entry=entry,
        liquidity_px=entry + 0.50,  # SSL above entry — no protective stop
        score=9,
        volume_tag="HEAVY",
        window_end=10_000.0,
        current_stop=stub,
        dollar_risk=12.0,
    )
    assert decision.action == "PASS"
    assert decision.reason == "stop_not_beyond_liq"
    assert decision.send_signal is False
    assert decision.ticket is None
    assert decision.stop is None
    assert decision.stop != pytest.approx(stub)

    fit = fit_stop(side="long", entry=entry, liquidity_px=100.5, current_stop=stub)
    assert fit.action == "pass"
    assert fit.stop is None

    # A stop that sits between entry and the SSL does not clear the buffer.
    assert place_stop_beyond_liquidity("long", 100.0, 99.0) == pytest.approx(99.0 * (1 - 0.0002))
    assert not stop_beyond_liquidity_ok(
        "long", 100.0, stop=99.5, liquidity_px=99.0, stop_liq_buffer_bps=2
    )


def test_equal_liquidity_is_not_beyond():
    assert place_stop_beyond_liquidity("short", 3000.0, 3000.0) is None
    decision = build_ticket(
        symbol="ETH",
        side="short",
        entry=3000.0,
        liquidity_px=3000.0,
        score=7,
        window_end=5_000.0,
    )
    assert decision.action == "PASS"
    assert decision.reason == "stop_not_beyond_liq"
    assert decision.stop is None


def test_refuse_tighten_into_noise_aave_long():
    """AAVE re-entry: 0.070% structural must not replace the 0.2% stub."""
    entry = 180.0
    stub = entry * (1.0 - 0.002)
    ssl = entry * (1.0 - 0.00070)  # 0.070% under entry

    decision = build_ticket(
        symbol="AAVE",
        side="long",
        entry=entry,
        liquidity_px=ssl,
        score=8,
        volume_tag="VOL_OK",
        window_end=20_000.0,
        current_stop=stub,
        dollar_risk=12.0,
    )
    assert decision.action == "PASS"
    assert decision.reason == "structural_inside_noise"
    assert decision.ticket is None
    assert decision.stop is None

    update = apply_bracket_stop(
        side="long",
        entry=entry,
        liquidity_px=ssl,
        current_stop=stub,
    )
    assert update.action == "drop"
    assert update.drop_trade is True
    assert update.stop == pytest.approx(stub)
    assert abs(update.stop - entry) / entry == pytest.approx(0.002)
    tight = entry * (1.0 - 0.00070)
    assert update.stop < tight  # resting stop stays further from entry


def test_refuse_tighten_into_noise_eth_short():
    """ETH short: stub exactly 0.2%, structural ~0.076% must not be written."""
    entry = 3_500.0
    stub = entry * (1.0 + 0.002)
    bsl = entry * (1.0 + 0.00076)

    update = apply_bracket_stop(
        side="short",
        entry=entry,
        liquidity_px=bsl,
        current_stop=stub,
    )
    assert update.action == "drop"
    assert update.stop == pytest.approx(stub)
    assert abs(update.stop - entry) / entry == pytest.approx(0.002)

    decision = build_ticket(
        symbol="ETH",
        side="short",
        entry=entry,
        liquidity_px=bsl,
        score=7,
        window_end=50_000.0,
        current_stop=stub,
        dollar_risk=12.0,
    )
    assert decision.action == "PASS"
    assert decision.stop is None
    assert decision.ticket is None


def test_wider_structural_stop_may_replace_stub():
    """Widening is allowed. The armed stop is beyond SSL, not the 0.2% stub."""
    entry = 100.0
    stub = entry * (1.0 - 0.002)
    ssl = 99.0  # 1% below; buffer pushes the stop a bit further
    decision = build_ticket(
        symbol="AAVE",
        side="long",
        entry=entry,
        liquidity_px=ssl,
        score=7,
        volume_tag="HEAVY",
        window_end=9_000.0,
        current_stop=stub,
        dollar_risk=12.0,
    )
    assert decision.action == "SIGNAL"
    assert decision.ticket is not None
    assert decision.stop == pytest.approx(ssl * (1.0 - 0.0002))
    assert decision.stop < ssl
    assert abs(decision.stop - entry) / entry > 0.005
    assert decision.ticket.volume_tag == "HEAVY"
    assert decision.ticket.score == 7


def test_volume_tag_never_vetoes():
    kwargs = dict(
        symbol="ETH",
        side="short",
        entry=3_000.0,
        liquidity_px=3_060.0,
        score=7,
        window_end=8_000.0,
        dollar_risk=12.0,
    )
    heavy = build_ticket(**kwargs, volume_tag="HEAVY")
    ok = build_ticket(**kwargs, volume_tag="VOL_OK")
    assert heavy.action == "SIGNAL"
    assert ok.action == "SIGNAL"
    assert heavy.ticket is not None and ok.ticket is not None
    assert heavy.ticket.stop == pytest.approx(ok.ticket.stop)
    assert heavy.ticket.volume_tag == "HEAVY"
    assert ok.ticket.volume_tag == "VOL_OK"


def test_score_below_7_passes_even_with_heavy_volume():
    decision = build_ticket(
        symbol="ETH",
        side="long",
        entry=100.0,
        liquidity_px=99.0,
        score=6,
        volume_tag="HEAVY",
        window_end=8_000.0,
        dollar_risk=12.0,
    )
    assert decision.action == "PASS"
    assert decision.reason == "score_below_min"
    assert decision.ticket is None


def test_skip_sizing_when_stop_inside_half_percent():
    entry = 100.0
    stop = entry * (1.0 - 0.0007)  # 0.07% — the fee-bleed distance
    dollar_risk = 12.0
    naive_notional = (dollar_risk / abs(entry - stop)) * entry
    assert naive_notional > 10_000

    sized = size_for_real_stop(entry=entry, stop=stop, dollar_risk=dollar_risk)
    assert sized.allowed is False
    assert sized.size == 0.0
    assert sized.reason == "stop_inside_half_pct"

    # Structural stop between the 0.2% noise line and 0.5% still does not arm.
    entry = 100.0
    liq = entry * (1.0 - 0.003)  # ~0.3% plus 2 bps of buffer, still under 0.5%
    decision = build_ticket(
        symbol="ETH",
        side="long",
        entry=entry,
        liquidity_px=liq,
        score=9,
        window_end=4_000.0,
        dollar_risk=dollar_risk,
    )
    assert decision.action == "PASS"
    assert decision.reason == "stop_inside_half_pct"
    assert decision.ticket is None
    assert decision.stop is None


def test_size_uses_real_stop_distance():
    entry = 100.0
    stop = 99.0  # 1%
    sized = size_for_real_stop(entry=entry, stop=stop, dollar_risk=12.0, side="long")
    assert sized.allowed is True
    assert sized.size == pytest.approx(12.0)
    assert sized.notional == pytest.approx(1_200.0)

    decision = build_ticket(
        symbol="AAVE",
        side="long",
        entry=entry,
        liquidity_px=99.0,
        score=7,
        window_end=4_000.0,
        dollar_risk=12.0,
    )
    assert decision.action == "SIGNAL"
    ticket = decision.ticket
    assert ticket is not None
    dist = entry - ticket.stop
    assert ticket.size == pytest.approx(12.0 / dist)
    assert ticket.size * entry < 2_000


def test_one_thesis_blocks_reentry_for_the_ticket_window_not_120s():
    book = ThesisBook()
    window_end = 10_000.0
    stopped_at = 8_000.0
    book.note_open("ETH", "t1", 3_000.0, window_end, stop=3_060.0)
    book.note_stop("ETH", now=stopped_at)

    # 120s cooldown has already elapsed; the thesis window has not.
    now = stopped_at + 121.0
    assert now < window_end
    assert not cooldown_active(stopped_at, now_ts=now, cooldown_sec=120.0)

    allowed, reason = book.allow_entry("ETH", now=now, trade_id="t2", price=2_990.0)
    assert allowed is False
    assert reason == "one_thesis_window"

    decision = build_ticket(
        symbol="ETH",
        side="short",
        entry=3_000.0,
        liquidity_px=3_060.0,  # would otherwise be a valid wide stop
        score=9,
        volume_tag="HEAVY",
        window_end=window_end + 3_600.0,
        trade_id="t2",
        book=book,
        now=now,
        dollar_risk=12.0,
    )
    assert decision.action == "PASS"
    assert decision.reason == "one_thesis_window"
    assert decision.ticket is None

    # Still blocked a few minutes later (the 3–10 minute re-entry).
    later = stopped_at + 600.0
    assert later < window_end
    again = build_ticket(
        symbol="ETH",
        side="short",
        entry=3_000.0,
        liquidity_px=3_060.0,
        score=9,
        window_end=window_end,
        trade_id="t3",
        book=book,
        now=later,
        dollar_risk=12.0,
    )
    assert again.action == "PASS"
    assert again.reason == "one_thesis_window"

    # Window end releases the symbol.
    released = build_ticket(
        symbol="ETH",
        side="short",
        entry=3_000.0,
        liquidity_px=3_060.0,
        score=9,
        window_end=window_end + 3_600.0,
        trade_id="t4",
        book=book,
        now=window_end,
        dollar_risk=12.0,
    )
    assert released.action == "SIGNAL"
    assert released.ticket is not None


def test_same_trade_partial_allowed_averaging_down_blocked():
    book = ThesisBook()
    window_end = 5_000.0
    book.note_open("AAVE", "t1", 100.0, window_end, stop=99.0)

    partial = build_ticket(
        symbol="AAVE",
        side="long",
        entry=100.0,
        liquidity_px=99.95,  # tight structure must not replace the 1% stop
        score=8,
        window_end=window_end,
        trade_id="t1",
        book=book,
        now=100.0,
        current_stop=99.0,
        dollar_risk=12.0,
    )
    assert partial.action == "SIGNAL"
    assert partial.reason == "same_trade_partial"
    assert partial.ticket is not None
    assert partial.ticket.trade_id == "t1"
    assert partial.ticket.stop == pytest.approx(99.0)
    assert partial.ticket.size == pytest.approx(12.0)

    down = build_ticket(
        symbol="AAVE",
        side="long",
        entry=99.0,
        liquidity_px=98.0,
        score=8,
        window_end=window_end,
        trade_id="t1",
        book=book,
        now=100.0,
        dollar_risk=12.0,
    )
    assert down.action == "PASS"
    assert down.reason == "averaging_down"
    assert down.ticket is None


def test_partial_with_tight_stop_does_not_scale_notional():
    book = ThesisBook()
    book.note_open("ETH", "t1", 3_000.0, 9_000.0, stop=3_000.0 * (1.0 - 0.0007))
    decision = build_ticket(
        symbol="ETH",
        side="long",
        entry=3_000.0,
        liquidity_px=2_900.0,
        score=8,
        window_end=9_000.0,
        trade_id="t1",
        book=book,
        now=50.0,
        dollar_risk=12.0,
    )
    assert decision.action == "PASS"
    assert decision.reason == "stop_inside_half_pct"
    assert decision.ticket is None


def test_heal_does_not_overwrite_wider_stop_with_stop_pct():
    entry = 100.0
    existing_stop = 98.0  # 2% — wider than a 0.2% default
    existing_tp = 110.0
    stop, tp = heal_stop_tp(
        "long",
        entry,
        existing_stop,
        existing_tp,
        stop_pct=0.002,
        tp_r=2.0,
    )
    assert stop == pytest.approx(existing_stop)
    assert tp == pytest.approx(existing_tp)
    assert stop != pytest.approx(entry * (1.0 - 0.002))

    # Short mirror: keep the wider stop above entry and the further TP.
    stop_s, tp_s = heal_stop_tp(
        "short",
        3_000.0,
        3_090.0,
        2_700.0,
        stop_pct=0.0015,
        tp_r=2.0,
    )
    assert stop_s == pytest.approx(3_090.0)
    assert tp_s == pytest.approx(2_700.0)


def test_heal_fills_missing_stop_from_stop_pct():
    stop, tp = heal_stop_tp(
        "long",
        100.0,
        None,
        None,
        stop_pct=0.002,
        tp_r=3.0,
    )
    assert stop == pytest.approx(99.8)
    assert tp == pytest.approx(100.0 + 0.2 * 3.0)


def test_alo_cancel_does_not_wipe_reduce_only_brackets():
    orders = [
        {"oid": 10, "reduce_only": False, "orderType": "Limit"},
        {"oid": 11, "reduce_only": True, "tpsl": "tp"},
        {"oid": 12, "reduceOnly": True, "tpsl": "sl"},
        {"oid": 13, "reduce_only": True},  # unlabeled reduce-only still protected
        {"oid": 14, "reduce_only": False, "orderType": "Stop Market"},
    ]
    assert alo_cancel_oids(orders) == [10]

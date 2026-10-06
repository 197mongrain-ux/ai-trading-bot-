"""Model B: tape, bias, Alo, thesis, TP cap, and ENTRY_MODE wiring."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import math

import pytest

from hl_bot.config import Settings, load_settings
from hl_bot.exchange.hl_trades import (
    HyperliquidTradeFeed,
    MemoryFeed,
    app_ping,
    map_aggressor_side,
    normalize_coin,
    parse_bbo,
    parse_hl_trade,
    parse_l2_top,
    parse_user_fill,
    trades_subscribe,
    ws_url,
    UserFill,
)
from hl_bot.exchange.info_client import (
    CANDLE_BACKOFF_BASE_SEC,
    CANDLE_TTL_SEC,
    InfoClient,
    parse_spot_usdc_total,
)
from hl_bot.execution.loop import run_bot
from hl_bot.execution.model_b_loop import format_model_b_fail, run_model_b
from hl_bot.journal import TradeJournal
from hl_bot.strategy.model_b.alo import (
    alo_limit,
    distance_to_fill_bps,
    is_closer_to_fill,
)
from hl_bot.strategy.model_b.bias import resolve_bias
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import (
    FLOW_EXIT_ENABLED,
    LEVERAGE,
    RISK_PCT,
    SOFT_PROP_ENABLED,
    STRATEGY_KILL_ENABLED,
    flow_exit_reason,
    heal_stop,
    floor_distance,
    place_stop,
    size_from_stop,
    soft_prop_allows,
    stop_beyond_extreme,
    stop_clears_fees,
    take_profit,
)
from hl_bot.strategy.model_b.score import log_only_score, volume_tag
from hl_bot.strategy.model_b.swings import atr14, local_bar_extreme
from hl_bot.strategy.model_b.tape import (
    MAINNET_BTC_PRINTS_PER_MIN,
    MIN_PRINTS,
    MIN_PRINTS_FLOOR,
    TESTNET_BTC_PRINTS_PER_MIN,
    density_min_prints,
    window_prints,
)
from hl_bot.strategy.model_b.thesis import ThesisBook, WorkingOrder
from hl_bot.strategy.model_b.types import AloIntent, Pool, TradePrint
from hl_bot.strategy.model_b.universe import (
    AFTER_HOURS_COINS,
    NY_COINS,
    session_coins,
)
from hl_bot.strategy.vwap import VwapTrendScalp

NY = ZoneInfo("America/New_York")


def _now() -> float:
    return datetime(2026, 10, 5, 10, 0, tzinfo=NY).timestamp()


def _bars(now: float, low: float = 100.0) -> list[dict]:
    """Swing low on bar 2. The last bar wicks to 98, past the 0.15% floor.

    That wick is the liquidity the stop has to clear. It is not a fractal
    (no bar to its right), so the confirmed swing stays at ``low``.
    """
    t0 = now - 400
    return [
        {"t": t0, "o": 110, "h": 112, "l": 108, "c": 109, "v": 1},
        {"t": t0 + 60, "o": 109, "h": 110, "l": low, "c": 106, "v": 1},
        {"t": t0 + 120, "o": 106, "h": 111, "l": 105, "c": 110, "v": 1},
        {"t": t0 + 180, "o": 110, "h": 114, "l": 98.0, "c": 112, "v": 1},
    ]


def _long_prints(
    now: float,
    *,
    sweep_sz: float = 7.5,
    reclaim_sz: float = 1.0,
    late_sz: float = 0.5,
    last_price: float = 104.0,
    final_price: float | None = None,
    sweep_px: float = 99.0,
    n_prefix: int = 20,
    extra: list[tuple] | None = None,
) -> list[TradePrint]:
    prints: list[TradePrint] = []
    seq = 0

    def add(offset: float, price: float, size: float, side: str | None) -> None:
        nonlocal seq
        prints.append(
            TradePrint(
                ts=now + offset,
                coin="BTC",
                price=price,
                size=size,
                side=side,
                seq=seq,
            )
        )
        seq += 1

    # Keep the whole window inside the last ~55s so a 20s-later re-check
    # still contains the sweep (the 90s window has not dropped it).
    for i in range(n_prefix):
        add(-55 + i * 0.3, 104.0, 0.5, "buy")
    add(-40, sweep_px, sweep_sz, "sell")
    add(-38, sweep_px, sweep_sz, "sell")
    for i in range(8):
        add(-30 + i * 0.2, last_price, reclaim_sz, "buy")
    add(-10, last_price, late_sz, "buy")
    add(-1, final_price if final_price is not None else last_price, late_sz, "buy")
    for item in extra or []:
        add(*item)
    return prints


def _decide(prints, bars, pools, **kw):
    now = kw.pop("now", _now())
    risk_pct = kw.pop("risk_pct", 0.02)
    tp_r = kw.pop("tp_r", 1.5)
    engine = kw.pop("engine", None) or ModelBEngine(tp_r=tp_r, risk_pct=risk_pct)
    return engine.evaluate(
        kw.pop("coin", "BTC"),
        now=now,
        prints=prints,
        bars=bars,
        pools=pools,
        best_bid=kw.pop("bid", 103.0),
        best_ask=kw.pop("ask", 105.0),
        equity=kw.pop("equity", 5000.0),
        tick=kw.pop("tick", 0.01),
        score=kw.pop("score", None),
    )


def _short_prints(now: float) -> list[TradePrint]:
    prints: list[TradePrint] = []
    seq = 0

    def add(offset: float, price: float, size: float, side: str) -> None:
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="BTC", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, 96.0, 0.5, "sell")
    add(-40, 101.0, 7.5, "buy")
    add(-38, 101.0, 7.5, "buy")
    for i in range(8):
        add(-30 + i * 0.2, 96.0, 1.0, "sell")
    add(-10, 96.0, 0.5, "sell")
    add(-1, 96.0, 0.5, "sell")
    return prints


def _short_bars(now: float) -> list[dict]:
    """Swing high on bar 2. The last bar wicks to 102.2, past the 0.15% floor."""
    t0 = now - 400
    return [
        {"t": t0, "o": 90, "h": 90, "l": 80, "c": 88, "v": 1},
        {"t": t0 + 60, "o": 88, "h": 100, "l": 85, "c": 90, "v": 1},
        {"t": t0 + 120, "o": 90, "h": 96, "l": 84, "c": 86, "v": 1},
        {"t": t0 + 180, "o": 86, "h": 102.2, "l": 83, "c": 90, "v": 1},
    ]


def _eth_bars(now: float, *, wick: float, swing: float) -> list[dict]:
    """ETH-shaped tape. ``swing`` is the fractal low. ``wick`` is the last bar.

    The last bar is not a fractal, so a wick under the swing does not
    replace the swing the sweep has to trade through.
    """
    t0 = now - 400
    return [
        {"t": t0, "o": 2720, "h": 2722, "l": 2712, "c": 2714, "v": 1},
        {"t": t0 + 60, "o": 2714, "h": 2716, "l": swing, "c": 2712, "v": 1},
        {"t": t0 + 120, "o": 2712, "h": 2718, "l": 2710, "c": 2716, "v": 1},
        {"t": t0 + 180, "o": 2716, "h": 2719, "l": wick, "c": 2715, "v": 1},
    ]


def _short_case(pools: list[Pool]):
    now = _now()
    return _decide(
        _short_prints(now),
        _short_bars(now),
        pools,
        bid=95.0,
        ask=96.5,
    )


def _pass_case(**kw):
    now = kw.pop("now", _now())
    pools = kw.pop("pools", [Pool("PDH", 130.0, taken=False)])
    return _decide(_long_prints(now, **{k: kw.pop(k) for k in list(kw) if k in {
        "sweep_sz", "reclaim_sz", "late_sz", "last_price", "final_price",
        "sweep_px", "n_prefix", "extra",
    }}), _bars(now), pools, now=now, **kw)


# --- universe ---------------------------------------------------------------


def test_ny_session_universe_and_after_hours():
    assert session_coins(datetime(2026, 10, 5, 9, 0, tzinfo=NY)) == NY_COINS
    assert session_coins(datetime(2026, 10, 5, 15, 59, tzinfo=NY)) == NY_COINS
    assert "TAO" in session_coins(datetime(2026, 10, 5, 9, 0, tzinfo=NY))
    assert session_coins(datetime(2026, 10, 5, 16, 0, tzinfo=NY)) == AFTER_HOURS_COINS
    assert session_coins(datetime(2026, 10, 5, 8, 59, tzinfo=NY)) == AFTER_HOURS_COINS
    # Weekend uses the same clock. Saturday morning is still the NY list.
    assert "TAO" in session_coins(datetime(2026, 10, 10, 10, 0, tzinfo=NY))
    assert NY_COINS == (
        "BTC", "ETH", "NEAR", "PUMP", "SOL", "LIT", "AAVE", "ONDO", "WLD", "TAO",
    )


def test_tao_after_hours_is_out_of_session():
    now = datetime(2026, 10, 5, 16, 30, tzinfo=NY).timestamp()
    decision = _decide(
        _long_prints(now),
        _bars(now),
        [Pool("PDH", 130, False)],
        now=now,
        coin="TAO",
    )
    assert decision.fail_reason == "OUT_OF_SESSION"
    assert decision.armed is False


# --- bias -------------------------------------------------------------------


def test_bias_pool_above_drops_shorts_and_below_drops_longs():
    assert resolve_bias(100, [Pool("PDH", 110, False)]).side == "long"
    assert resolve_bias(100, [Pool("PDL", 90, False)]).side == "short"
    # Taken pool is ignored; the remaining pool sets the side.
    bias = resolve_bias(
        100,
        [Pool("PDH", 101, taken=True), Pool("PDL", 90, taken=False)],
    )
    assert bias.side == "short"
    assert bias.pool is not None and bias.pool.name == "PDL"


def test_bias_tie_is_none():
    tied = resolve_bias(100, [Pool("PDH", 110, False), Pool("PDL", 90, False)])
    assert tied.side == "NONE"
    assert tied.pool is None
    assert tied.pool_above is not None and tied.pool_above.name == "PDH"
    assert tied.pool_below is not None and tied.pool_below.name == "PDL"
    assert resolve_bias(100, []).side == "NONE"


def test_none_bias_allows_both_sides():
    # No pool: both sides allowed, and a valid long tape arms.
    bare = _pass_case(pools=[])
    assert bare.bias == "NONE"
    assert bare.armed is True
    assert bare.intent is not None and bare.intent.side == "long"
    assert bare.pool is None
    assert bare.intent.take_profit == pytest.approx(100.545)

    # Exact tie is also NONE, and the long is capped by the pool above.
    tie = _pass_case(pools=[Pool("PDH", 114.0, False), Pool("PDL", 94.0, False)])
    assert tie.bias == "NONE"
    assert tie.armed is True
    assert tie.intent is not None and tie.intent.side == "long"
    assert tie.pool == "PDH@114"
    assert tie.intent.take_profit <= 114

    # The same NONE filter arms a valid short.
    short = _short_case([])
    assert short.bias == "NONE"
    assert short.armed is True
    assert short.intent is not None and short.intent.side == "short"
    assert short.pool is None


def test_long_setup_does_not_arm_when_bias_is_short():
    decision = _pass_case(pools=[Pool("PDL", 90.0, taken=False)])
    assert decision.bias == "short"
    assert decision.armed is False
    assert decision.intent is None
    assert decision.fail_reason == "NO_SWING"


def test_short_setup_does_not_arm_when_bias_is_long():
    decision = _short_case([Pool("PDH", 130.0, taken=False)])
    assert decision.bias == "long"
    assert decision.armed is False
    assert decision.intent is None
    assert decision.fail_reason == "NO_SWING"


# --- tape fails -------------------------------------------------------------


def test_long_reclaim_absorb_and_deltas_arm():
    decision = _pass_case()
    assert decision.armed is True
    assert decision.fail_reason is None
    assert decision.bias == "long"
    assert decision.pool == "PDH@130"
    assert decision.swing == pytest.approx(100)
    assert decision.sweep_price == pytest.approx(99)
    assert decision.absorb == pytest.approx(15 / 9)
    assert decision.window_delta is not None and decision.window_delta > 0
    assert decision.last_15s_delta is not None and decision.last_15s_delta >= 0
    assert decision.volume_tag == "VOL_OK"
    assert decision.score == log_only_score(32)
    assert decision.score < 7
    intent = decision.intent
    assert intent is not None
    assert intent.tif == "Alo"
    assert intent.market_fallback is False
    assert intent.leverage == 20
    assert intent.limit_px == pytest.approx(99)
    # Last-bar wick is 98. Stop is that low minus the 3-tick buffer, not
    # the 0.15% floor (~98.85) and not one point under the sweep.
    assert intent.stop == pytest.approx(97.97)
    assert intent.stop < 99 * (1.0 - 0.0015)
    assert intent.take_profit == pytest.approx(100.545)
    assert intent.take_profit <= 130
    assert intent.size == pytest.approx(97.087378)  # 5000 * 2% / 1.03


def test_absorb_boundary():
    # 8*1.0 + 2*1.0 = 10 buy after reclaim, 2*7.5 = 15 sell → 1.5 exactly.
    exact = _pass_case(reclaim_sz=1.0, late_sz=1.0)
    assert exact.armed is True
    assert exact.absorb == pytest.approx(1.5)

    # 14.9 / 10 = 1.49
    under = _pass_case(sweep_sz=7.45, reclaim_sz=1.0, late_sz=1.0)
    assert under.armed is False
    assert under.fail_reason == "ABSORB"
    assert under.absorb == pytest.approx(1.49)


def test_no_reclaim():
    decision = _pass_case(final_price=96.0)
    assert decision.fail_reason == "NO_RECLAIM"
    assert decision.armed is False
    assert decision.sweep_price == pytest.approx(99)


def test_testnet_min_prints_follows_tape_density(monkeypatch):
    """30 on mainnet. Testnet auto floor is the same density, bounded at 3."""
    assert MAINNET_BTC_PRINTS_PER_MIN == 252
    assert TESTNET_BTC_PRINTS_PER_MIN == 7
    scaled = round(MIN_PRINTS * (TESTNET_BTC_PRINTS_PER_MIN / MAINNET_BTC_PRINTS_PER_MIN))
    assert density_min_prints() == max(MIN_PRINTS_FLOOR, scaled) == 3

    monkeypatch.delenv("MODEL_B_MIN_PRINTS", raising=False)
    monkeypatch.setenv("HL_NETWORK", "mainnet")
    monkeypatch.setenv("ENTRY_MODE", "both")
    assert load_settings().model_b_min_prints == 30

    monkeypatch.setenv("HL_NETWORK", "testnet")
    assert load_settings().model_b_min_prints == 3

    monkeypatch.setenv("MODEL_B_MIN_PRINTS", "12")
    assert load_settings().model_b_min_prints == 12

    now = _now()
    quiet = _long_prints(now)[:12]
    bars = _bars(now)
    pools = [Pool("PDH", 130, False)]
    mainnet = _decide(quiet, bars, pools, engine=ModelBEngine(min_prints=30))
    assert mainnet.fail_reason == "THIN_TAPE"
    assert mainnet.print_count == 12
    assert mainnet.min_prints == 30
    assert "prints=12/30" in format_model_b_fail(mainnet)

    testnet = _decide(quiet, bars, pools, engine=ModelBEngine(min_prints=3))
    assert testnet.fail_reason != "THIN_TAPE"
    assert testnet.min_prints == 3
    assert testnet.print_count == 12


def test_thin_tape_and_no_side_priority():
    now = _now()
    thin = _decide(_long_prints(now)[:29], _bars(now), [Pool("PDH", 130, False)])
    assert thin.fail_reason == "THIN_TAPE"
    assert thin.armed is False
    assert thin.print_count == 29
    assert thin.to_log()["print_count"] == 29
    assert "prints=29/30" in format_model_b_fail(thin)
    assert "prints=" not in format_model_b_fail(
        _decide(_long_prints(now), _bars(now), [Pool("PDL", 90, False)])
    )

    sideless = _long_prints(now)
    sideless[3] = replace(sideless[3], side=None)
    decision = _decide(sideless, _bars(now), [Pool("PDH", 130, False)])
    assert decision.fail_reason == "NO_SIDE"

    # Fewer than 30 prints, one without a side: fail closed, not THIN_TAPE.
    short_tape = _long_prints(now)[:10]
    short_tape[0] = replace(short_tape[0], side=None)
    assert _decide(short_tape, _bars(now), [Pool("PDH", 130, False)]).fail_reason == "NO_SIDE"


def test_each_tape_fail_reason():
    assert _pass_case(sweep_px=104.0).fail_reason == "NO_SWEEP"
    assert _pass_case(reclaim_sz=3.0).fail_reason == "ABSORB"
    assert _pass_case(sweep_sz=100.0).fail_reason == "DELTA"
    last15 = _pass_case(extra=[(-5.0, 104.0, 3.0, "sell")])
    assert last15.fail_reason == "LAST_15s"
    assert last15.window_delta is not None and last15.window_delta > 0
    assert last15.last_15s_delta is not None and last15.last_15s_delta < 0


def test_score_does_not_arm_or_block_and_volume_is_not_a_veto():
    low = _pass_case(score=1)
    assert low.armed is True
    assert low.score == 1
    assert low.volume_tag == "VOL_OK"

    zero = _pass_case(score=0)
    assert zero.armed is True

    high = _pass_case(sweep_px=104.0, score=9)
    assert high.armed is False
    assert high.score == 9
    assert high.fail_reason == "NO_SWEEP"

    # Default density score hits 9/9 on a thick tape and still does not arm.
    now = _now()
    thick = []
    for i in range(90):
        thick.append(
            TradePrint(ts=now - 80 + i * 0.5, coin="BTC", price=104.0, size=0.2, side="buy", seq=i)
        )
    dense = _decide(thick, _bars(now), [Pool("PDH", 130, False)])
    assert dense.score == 9
    assert dense.fail_reason == "NO_SWEEP"
    assert dense.armed is False

    # HEAVY tag with a failed absorb does not sneak an entry through.
    heavy_prints = _long_prints(now, reclaim_sz=3.0)
    base = len(heavy_prints)
    for i in range(40):
        heavy_prints.append(
            TradePrint(
                ts=now - 0.5,
                coin="BTC",
                price=104.0,
                size=1.0,
                side="buy",
                seq=base + i,
            )
        )
    heavy = _decide(heavy_prints, _bars(now), [Pool("PDH", 130, False)])
    assert heavy.volume_tag == "HEAVY"
    assert volume_tag(len(heavy_prints)) == "HEAVY"
    assert heavy.armed is False
    assert heavy.fail_reason == "ABSORB"


def test_swing_closer_than_3_ticks_is_ignored():
    now = _now()
    t0 = now - 500
    bars = [
        {"t": t0, "o": 110, "h": 112, "l": 110, "c": 110, "v": 1},
        {"t": t0 + 60, "o": 110, "h": 111, "l": 100, "c": 108, "v": 1},
        {"t": t0 + 120, "o": 108, "h": 112, "l": 108, "c": 109, "v": 1},
        {"t": t0 + 180, "o": 109, "h": 111, "l": 107, "c": 108, "v": 1},
        {"t": t0 + 240, "o": 108, "h": 110, "l": 102, "c": 106, "v": 1},
        {"t": t0 + 300, "o": 106, "h": 112, "l": 106, "c": 110, "v": 1},
        {"t": t0 + 360, "o": 110, "h": 114, "l": 109, "c": 112, "v": 1},
    ]
    # Last trade 104 is 2 ticks from the recent swing (102) and 4 from 100.
    # Tick 1 is coarser than the 1.5% cap, so the arm fails closed. The
    # swing itself was still selected.
    decision = _decide(_long_prints(now), bars, [Pool("PDH", 130, False)], tick=1.0)
    assert decision.swing == pytest.approx(100)
    assert decision.fail_reason == "BAD_STOP"

    # Exactly 3 ticks is kept.
    exact = _decide(
        _long_prints(now, last_price=103.0),
        _bars(now),
        [Pool("PDH", 130, False)],
        tick=1.0,
    )
    assert exact.swing == pytest.approx(100)
    assert exact.fail_reason != "NO_SWING"

    # Only a 2-tick swing → nothing to arm.
    near = _decide(
        _long_prints(now, last_price=102.0),
        _bars(now),
        [Pool("PDH", 130, False)],
        tick=1.0,
    )
    assert near.fail_reason == "NO_SWING"
    assert near.armed is False


def test_short_mirror_arms_and_caps_context():
    now = _now()
    t0 = now - 400
    bars = [
        {"t": t0, "o": 90, "h": 90, "l": 80, "c": 88, "v": 1},
        {"t": t0 + 60, "o": 88, "h": 100, "l": 85, "c": 90, "v": 1},
        {"t": t0 + 120, "o": 90, "h": 96, "l": 84, "c": 86, "v": 1},
        {"t": t0 + 180, "o": 86, "h": 102.2, "l": 83, "c": 90, "v": 1},
    ]
    prints: list[TradePrint] = []
    seq = 0

    def add(offset, price, size, side):
        nonlocal seq
        prints.append(TradePrint(ts=now + offset, coin="BTC", price=price, size=size, side=side, seq=seq))
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, 96.0, 0.5, "sell")
    add(-40, 101.0, 7.5, "buy")
    add(-38, 101.0, 7.5, "buy")
    for i in range(8):
        add(-30 + i * 0.2, 96.0, 1.0, "sell")
    add(-10, 96.0, 0.5, "sell")
    add(-1, 96.0, 0.5, "sell")

    decision = _decide(
        prints,
        bars,
        [Pool("PDL", 80.0, taken=False)],
        bid=95.0,
        ask=96.5,
    )
    assert decision.bias == "short"
    assert decision.armed is True
    assert decision.absorb == pytest.approx(15 / 9)
    assert decision.window_delta is not None and decision.window_delta < 0
    intent = decision.intent
    assert intent is not None
    assert intent.side == "short"
    assert intent.limit_px == pytest.approx(101)
    # Last-bar high 102.2 is the liquidity. Stop clears it by the buffer.
    assert intent.stop == pytest.approx(102.23)
    assert intent.take_profit == pytest.approx(99.155)
    assert intent.take_profit >= 80
    assert intent.tif == "Alo"


# --- alo / risk / thesis ----------------------------------------------------


def test_alo_anchor_never_crosses():
    assert alo_limit("long", 99, 103, 105, 1) == pytest.approx(99)
    # Swept low would lift the ask → join the bid.
    assert alo_limit("long", 106, 104, 105, 1) == pytest.approx(104)
    # Locked book: do not send.
    assert alo_limit("long", 106, 105, 105, 1) is None
    # No ask → cannot prove the buy rests.
    assert alo_limit("long", 99, 103, None, 1) is None

    assert alo_limit("short", 101, 95, 96, 1) == pytest.approx(101)
    assert alo_limit("short", 90, 95, 96, 1) == pytest.approx(96)
    assert alo_limit("short", 90, 95, 95, 1) is None
    assert alo_limit("short", 101, None, 96, 1) is None


def test_tp_capped_by_pool_and_r_band():
    # 1.5R inside the pool.
    assert take_profit("long", 99, 98, 130, tp_r=1.5) == pytest.approx(100.5)
    # 2.5R clamps to the 2R cap.
    assert take_profit("long", 99, 98, 130, tp_r=2.5) == pytest.approx(101.0)
    # 2R would print through the pool → cap at the pool, even inside 1R.
    assert take_profit("long", 99, 98, 100.2, tp_r=2.0) == pytest.approx(100.2)
    # Requested 4R clamps to 2R. A pool inside that 2R still wins.
    assert take_profit("long", 100, 90, 115, tp_r=4) == pytest.approx(115)
    assert take_profit("long", 100, 90, 140, tp_r=4) == pytest.approx(120)
    # Sub-1R request is lifted to 1R before the cap.
    assert take_profit("long", 100, 90, None, tp_r=0.2) == pytest.approx(110)
    # Short mirror: cap is the higher price (closer to entry).
    assert take_profit("short", 101, 102, 80, tp_r=1.5) == pytest.approx(99.5)
    assert take_profit("short", 101, 102, 100.2, tp_r=1.5) == pytest.approx(100.2)


def test_size_uses_risk_per_trade_and_rejects_40x():
    size, dollar = size_from_stop(5000, 99, 98, risk_pct=0.02)
    assert dollar == pytest.approx(100)
    assert size == pytest.approx(100)
    # The fraction is the argument, not a hidden constant.
    half, half_dollar = size_from_stop(5000, 99, 98, risk_pct=0.01)
    assert half_dollar == pytest.approx(50)
    assert half == pytest.approx(50)
    assert RISK_PCT == pytest.approx(0.02)
    assert LEVERAGE == 20
    with pytest.raises(ValueError, match="20x"):
        size_from_stop(5000, 99, 98, risk_pct=0.02, leverage=40)

    sized = _pass_case(risk_pct=0.02)
    assert sized.intent is not None
    assert sized.intent.size == pytest.approx(97.087378)
    other = _pass_case(risk_pct=0.01)
    assert other.intent is not None
    assert other.intent.size == pytest.approx(48.543689)


def test_heal_keeps_wider_stop_and_flow_does_not_exit():
    assert heal_stop("long", 98, 99) == pytest.approx(98)
    assert heal_stop("long", 99, 98) == pytest.approx(98)
    assert heal_stop("short", 102, 101) == pytest.approx(102)
    assert heal_stop("short", 101, 102) == pytest.approx(102)
    assert soft_prop_allows() is False
    assert SOFT_PROP_ENABLED is False
    assert STRATEGY_KILL_ENABLED is False
    assert FLOW_EXIT_ENABLED is False
    assert flow_exit_reason(window_delta=-50, last_15s_delta=-10) is None


def test_default_rests_alo_until_sweep_is_stale():
    now = _now()
    bars = _bars(now)
    prints = _long_prints(now)
    pools = [Pool("PDH", 130, False)]
    engine = ModelBEngine()
    first = _decide(prints, bars, pools, engine=engine)
    assert first.intent is not None
    assert first.intent.work_sec == 0
    assert first.intent.limit_px != first.intent.stop
    engine.thesis.post(first.intent, now)
    assert engine.thesis.block_reason("BTC", "other-swing") == "SECOND_ALO"
    # No clock cancel, including the old 20s mark.
    assert engine.thesis.expire(now + 20) == []
    assert engine.thesis.expire(now + 3600) == []
    # A print through the swept low that does not sell into the bid cancels.
    through = TradePrint(
        ts=now + 5,
        coin="BTC",
        price=first.intent.sweep_px - 1,
        size=1,
        side="buy",
        seq=1,
    )
    cancelled = engine.thesis.cancel_if_stale("BTC", [through])
    assert len(cancelled) == 1
    again = _decide(prints, bars, pools, now=now + 5, engine=engine)
    assert again.fail_reason == "THESIS_DONE"

    # The optional timer still cancels when MODEL_B_ALO_TIMEOUT_SEC is set.
    timed = ThesisBook(work_sec=20)
    timed.post(first.intent, now)
    assert timed.expire(now + 19.9) == []
    assert len(timed.expire(now + 20)) == 1


def test_lit_short_stop_is_not_the_alo_tick():
    """One tick past a LIT Alo collides with the fill. A floor-only stop is also rejected."""
    tick = 0.0001
    sweep = 3.9046
    limit = alo_limit("short", sweep, sweep, sweep + tick, tick)
    assert limit == pytest.approx(3.9047)
    naive = stop_beyond_extreme("short", sweep, tick)
    assert naive == pytest.approx(limit)
    assert stop_clears_fees(limit, limit + tick, tick=tick) is False
    # Sweep ≈ Alo and no wick past the 0.15% floor: do not arm on the cluster.
    assert place_stop("short", sweep, limit, tick) is None
    # A local high beyond that floor. Stop clears the high, not the fill tick.
    stop = place_stop("short", sweep, limit, tick, local_extreme=3.92)
    assert stop == pytest.approx(3.9208)
    assert stop - limit > tick * 2
    assert stop > limit + floor_distance(limit, tick) - tick

    now = _now()
    book = ThesisBook()
    intent = AloIntent(
        coin="LIT",
        side="short",
        limit_px=limit,
        size=10,
        stop=stop,
        take_profit=take_profit("short", limit, stop, None),
        swing_id="LIT:high:1",
        sweep_px=sweep,
        tick=tick,
        tp_r=1.5,
    )
    book.post(intent, now)
    # Fill lands on the old 1-tick level. The open stop stays past the high.
    opened = book.apply_user_fill(
        coin="LIT", oid=None, price=naive, ts=now + 1, crossed=False
    )
    assert opened is not None
    assert opened.entry == pytest.approx(naive)
    assert opened.stop == pytest.approx(3.9208)
    assert opened.stop - opened.entry > tick
    assert stop_clears_fees(opened.entry, opened.stop, tick=tick)


def test_eth_floor_only_stop_does_not_arm_and_a_deeper_wick_does():
    """Oct 5 ~21:46 ET ETH long: Alo/sweep 2705.8, swing 2706.9, stop 2701.7.

    2701.7 is the 0.15% floor (41 ticks). Price later traded ~2702.7, a hunt
    into that floor. With no wick past the floor the arm is BAD_STOP. A 1m
    low under the floor puts the stop beyond that low, and size is 2% of
    spot USDC over the new distance.
    """
    tick = 0.1
    entry = 2705.8
    floor_px = entry - floor_distance(entry, tick)
    assert math.floor(floor_px / tick + 1e-9) * tick == pytest.approx(2701.7)
    # The live stop. Sweep on the Alo, swing above the fill, no deeper wick.
    assert place_stop("long", entry, entry, tick, swing=2706.9) is None
    # A shallow wick still above the floor must not be lifted onto 2701.7.
    assert place_stop("long", entry, entry, tick, swing=2706.9, local_extreme=2704.0) is None
    # 0.5×ATR that does not clear the floor is the same hunt stop.
    assert place_stop("long", entry, entry, tick, atr=4.0) is None
    # 0.5×ATR = 6 clears the 4.06 floor. Stop is the sweep minus that buffer.
    atr_stop = place_stop("long", entry, entry, tick, swing=2706.9, atr=12.0)
    assert atr_stop == pytest.approx(2699.8)
    assert atr_stop < 2701.7
    # Wick at 2698 is under the floor. Stop stays beyond the wick; the floor
    # must not pull it back up to 2701.7.
    stop = place_stop("long", entry, entry, tick, swing=2706.9, local_extreme=2698.0)
    assert stop == pytest.approx(2697.4)
    assert stop < 2698.0
    assert stop < 2701.7
    dist = entry - stop
    assert dist / entry < 0.015
    size, dollar = size_from_stop(8000.0, entry, stop, risk_pct=0.02)
    assert dollar == pytest.approx(160.0)
    assert size == pytest.approx(19.047619)
    assert size == pytest.approx(math.floor((8000.0 * 0.02) / dist * 1_000_000) / 1_000_000)
    # Required room past a much deeper low exceeds 1.5%. Fail closed.
    assert place_stop("long", entry, entry, tick, local_extreme=2660.0) is None

    now = _now()
    prints = _long_prints(now, sweep_px=entry, last_price=2709.0, final_price=2709.0)
    prints = [replace(p, price=2709.0) if p.price == 104.0 else p for p in prints]
    floor_bars = _eth_bars(now, wick=2708.0, swing=2706.9)
    refused = _decide(
        prints,
        floor_bars,
        [Pool("PDH", 2800.0, False)],
        bid=2708.9,
        ask=2709.1,
        tick=tick,
        equity=8000.0,
    )
    assert refused.armed is False
    assert refused.fail_reason == "BAD_STOP"
    assert refused.sweep_price == pytest.approx(entry)
    assert local_bar_extreme(floor_bars, side="long", now=now) == pytest.approx(2706.9)

    wick_bars = _eth_bars(now, wick=2698.0, swing=2706.9)
    armed = _decide(
        prints,
        wick_bars,
        [Pool("PDH", 2800.0, False)],
        bid=2708.9,
        ask=2709.1,
        tick=tick,
        equity=8000.0,
    )
    assert armed.armed is True
    intent = armed.intent
    assert intent is not None
    assert intent.limit_px == pytest.approx(entry)
    assert intent.stop == pytest.approx(2697.4)
    assert intent.stop < 2701.7
    assert intent.take_profit == pytest.approx(2718.4)
    assert intent.size == pytest.approx(19.047619)
    sized, _dollar = size_from_stop(8000.0, intent.limit_px, intent.stop, risk_pct=0.02)
    assert intent.size == pytest.approx(sized)

    wide_bars = _eth_bars(now, wick=2660.0, swing=2706.9)
    capped = _decide(
        prints,
        wide_bars,
        [Pool("PDH", 2800.0, False)],
        bid=2708.9,
        ask=2709.1,
        tick=tick,
        equity=8000.0,
    )
    assert capped.armed is False
    assert capped.fail_reason == "BAD_STOP"


def test_brackets_use_filled_size_when_margin_trimmed():
    """A partial fill brackets the filled size. The remainder stays working."""
    now = _now()
    entry = 2701.6
    stop = place_stop("long", entry, entry, 0.1, local_extreme=2694.0)
    assert stop is not None
    assert stop < entry * (1.0 - 0.0015)
    requested, _dollar = size_from_stop(8000.0, entry, stop, risk_pct=0.02)
    assert requested * entry <= 8000.0 * 20 + 1e-6
    book = ThesisBook()
    intent = AloIntent(
        coin="ETH",
        side="long",
        limit_px=entry,
        size=requested,
        stop=stop,
        take_profit=take_profit("long", entry, stop, None),
        swing_id="ETH:low:1",
        sweep_px=entry,
        tick=0.1,
        tp_r=1.5,
    )
    book.post(intent, now, oid=4)
    filled = requested / 4
    opened = book.apply_user_fill(
        coin="ETH", oid=4, price=entry, ts=now + 1, crossed=False, size=filled
    )
    assert opened is not None
    assert opened.size == pytest.approx(filled)
    assert opened.size < requested
    assert opened.stop == pytest.approx(stop)
    assert stop_clears_fees(opened.entry, opened.stop, tick=0.1)
    assert opened.remainder_kept is True
    resting = book.working("ETH")
    assert resting is not None
    assert resting.size == pytest.approx(requested - filled)
    assert resting.oid == 4


def test_no_second_alo_no_average_down_no_market_and_stop_consumes_thesis():
    now = _now()
    book = ThesisBook()
    decision = _decide(_long_prints(now), _bars(now), [Pool("PDH", 130, False)])
    intent = decision.intent
    assert intent is not None
    book.post(intent, now)
    with pytest.raises(ValueError, match="market fallback"):
        book.post(replace(intent, market_fallback=True, tif="Ioc"), now + 1)
    with pytest.raises(ValueError, match="SECOND_ALO"):
        book.post(replace(intent, swing_id="other"), now + 1)

    sell = TradePrint(
        ts=now + 1,
        coin="BTC",
        price=intent.limit_px,
        size=1,
        side="sell",
        seq=0,
    )
    pos = book.try_fill_from_prints([sell])
    assert pos is not None
    assert book.block_reason("BTC", "brand-new-swing") == "AVERAGE_DOWN"
    # A sell-heavy print that does not reach the stop is not an exit.
    assert flow_exit_reason() is None
    assert book.try_exit("BTC", intent.limit_px) is None
    # Heal cannot tighten the stop that was placed past the sweep.
    assert book.propose_stop("BTC", intent.stop + 0.5) == pytest.approx(intent.stop)
    closed = book.try_exit("BTC", intent.stop)
    assert closed is not None and closed.reason == "stop"
    assert book.block_reason("BTC", intent.swing_id) == "THESIS_DONE"


def test_taker_user_fill_does_not_open_a_position():
    now = _now()
    book = ThesisBook()
    decision = _decide(_long_prints(now), _bars(now), [Pool("PDH", 130, False)])
    assert decision.intent is not None
    book.post(decision.intent, now, oid=7)
    assert book.apply_user_fill(
        coin="BTC", oid=7, price=decision.intent.limit_px, ts=now + 1, crossed=True
    ) is None
    assert book.position("BTC") is None
    opened = book.apply_user_fill(
        coin="BTC", oid=7, price=decision.intent.limit_px, ts=now + 1, crossed=False
    )
    assert opened is not None
    assert book.working("BTC") is None


# --- feed -------------------------------------------------------------------


def test_trade_feed_aggressor_side_fail_closed():
    assert map_aggressor_side("B") == "buy"
    assert map_aggressor_side("A") == "sell"
    assert map_aggressor_side(None) is None
    assert map_aggressor_side("Z") is None
    assert ws_url("testnet") == "wss://api.hyperliquid-testnet.xyz/ws"
    assert ws_url("mainnet") == "wss://api.hyperliquid.xyz/ws"
    assert trades_subscribe("BTC")["subscription"]["type"] == "trades"

    buy = parse_hl_trade(
        {
            "coin": "btc",
            "side": "B",
            "px": "100",
            "sz": "0.5",
            "time": 1_700_000_000_000,
            "users": ["0xbuyer", "0xseller"],
        }
    )
    assert buy is not None
    assert buy.side == "buy"
    assert buy.coin == "BTC"
    assert buy.ts == pytest.approx(1_700_000_000)

    # Addresses are not a side. Missing side stays None.
    missing = parse_hl_trade(
        {
            "coin": "ETH",
            "px": "10",
            "sz": "1",
            "time": 1_700_000_000_100,
            "users": ["0xbuyer", "0xseller"],
        }
    )
    assert missing is not None and missing.side is None

    feed = HyperliquidTradeFeed(network="testnet", coins=("BTC", "ETH"))
    stored = feed.ingest(
        {
            "channel": "trades",
            "data": [
                {"coin": "BTC", "side": "A", "px": "99", "sz": "0.2", "time": 1_700_000_000_200},
                {"coin": "BTC", "px": "99", "sz": "0.1", "time": 1_700_000_000_300},
            ],
        }
    )
    assert [p.side for p in stored] == ["sell", None]
    assert feed.prints("BTC")[1].side is None

    parsed = parse_bbo(
        {"coin": "BTC", "bbo": [{"px": "100", "sz": "1", "n": 2}, {"px": "101", "sz": "3", "n": 1}]}
    )
    assert parsed == ("BTC", 100.0, 101.0)
    # Only the top of an l2 book is kept.
    top = parse_l2_top(
        {
            "coin": "SOL",
            "levels": [
                [{"px": "10", "sz": "1"}, {"px": "9", "sz": "4"}],
                [{"px": "11", "sz": "2"}, {"px": "12", "sz": "8"}],
            ],
        }
    )
    assert top == ("SOL", 10.0, 11.0)

    maker = parse_user_fill(
        {"coin": "BTC", "px": "99", "sz": "1", "time": 1_700_000_000_000, "oid": 5, "crossed": False}
    )
    assert maker is not None and maker.crossed is False
    unknown = parse_user_fill(
        {"coin": "BTC", "px": "99", "sz": "1", "time": 1_700_000_000_000, "oid": 5}
    )
    assert unknown is not None and unknown.crossed is None


def test_trade_feed_reconnect_keeps_buffer_and_resubscribes():
    """Expired reconnect must resubscribe without wiping or double-counting prints.

    The trades channel only snapshots the last few prints. A fresh socket
    stays under 30 until live prints arrive. Replaying that snapshot on
    reconnect must not fake a thick tape, and the prints already stored
    must still be there.
    """
    assert normalize_coin("btc-perp") == "BTC"
    assert app_ping() == {"method": "ping"}

    feed = HyperliquidTradeFeed(network="testnet", coins=("BTC", "ETH"), user="0xabc")
    payloads = feed.resubscribe_payloads()
    assert [p["subscription"]["coin"] for p in payloads if p.get("subscription", {}).get("type") == "trades"] == [
        "BTC",
        "ETH",
    ]
    assert payloads[-1] == {"method": "ping"}
    assert any(p.get("subscription", {}).get("type") == "userFills" for p in payloads)

    sent: list[dict] = []

    class _WS:
        def send(self, raw: str) -> None:
            sent.append(__import__("json").loads(raw))

    now_ms = 1_700_000_000_000
    now = now_ms / 1000
    feed._send_subscriptions(_WS())
    assert sent[0]["subscription"]["type"] == "trades"
    assert sent[-1] == {"method": "ping"}

    snapshot = {
        "channel": "trades",
        "data": [
            {
                "coin": "BTC-PERP",
                "side": "B" if i % 2 == 0 else "A",
                "px": "100",
                "sz": "0.01",
                "time": now_ms - (19 - i) * 1000,
                "tid": i + 1,
            }
            for i in range(20)
        ],
    }
    assert len(feed.ingest(snapshot)) == 20
    assert feed.prints("btc")[0].coin == "BTC"
    thin = window_prints(feed.prints("BTC"), coin="BTC", now=now)
    assert len(thin) == 20
    assert len(thin) < MIN_PRINTS

    # Same snapshot again (reconnect ack). Buffer stays, count does not jump.
    assert feed.ingest(snapshot) == []
    assert len(feed.prints("BTC")) == 20

    # Socket open after Expired sends the same subscriptions. Prints remain.
    sent.clear()
    feed._send_subscriptions(_WS())
    assert sent[-1] == {"method": "ping"}
    assert len(feed.prints("BTC")) == 20

    live = {
        "channel": "trades",
        "data": {
            "coin": "BTC",
            "side": "A",
            "px": "101",
            "sz": "0.02",
            "time": now_ms,
            "tid": 500,
        },
    }
    # One dict (not only a list) plus enough new ids to clear THIN_TAPE.
    assert len(feed.ingest(live)) == 1
    more = {
        "channel": "trades",
        "data": [
            {
                "coin": "BTC",
                "side": "B",
                "px": "101",
                "sz": "0.02",
                "time": now_ms,
                "tid": 600 + i,
            }
            for i in range(MIN_PRINTS)
        ],
    }
    assert len(feed.ingest(more)) == MIN_PRINTS
    filled = window_prints(feed.prints("BTC"), coin="BTC", now=now)
    assert len(filled) >= MIN_PRINTS
    # Replaying the live batch does not inflate the window.
    assert feed.ingest(more) == []
    assert len(window_prints(feed.prints("BTC"), coin="BTC", now=now)) == len(filled)


def test_candle_snapshot_caches_and_backs_off_on_429(monkeypatch):
    calls: list[str] = []
    mode = {"status": 200}

    def fake_post(base_url, coin, interval, start_ms, end_ms, timeout=15.0):
        calls.append(coin)
        if mode["status"] != 200:
            return mode["status"], None
        return 200, [
            {"t": start_ms + 1, "o": 1, "h": 3, "l": 0.5, "c": 2, "v": 4, "T": start_ms + 60_000}
        ]

    monkeypatch.setattr("hl_bot.exchange.info_client._post_candle_snapshot", fake_post)
    client = InfoClient(base_url="https://example.invalid")
    clock = {"t": 1_000_000.0}
    client._now = lambda: clock["t"]

    first = client.get_candles("BTC", "1m", start_ms=1, end_ms=2_000_000_000_000)
    second = client.get_candles("btc", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert calls == ["BTC"]
    assert second == first
    assert first[0]["h"] == 3

    # Ten-coin scan while the snapshot is fresh must not hit the network.
    for coin in ("ETH", "SOL", "BTC", "NEAR"):
        if coin == "BTC":
            client.get_candles(coin, "1m", start_ms=1, end_ms=2_000_000_000_000)
        else:
            mode["status"] = 200
            client.get_candles(coin, "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert calls == ["BTC", "ETH", "SOL", "NEAR"]

    clock["t"] = 1_000_000.0 + CANDLE_TTL_SEC
    mode["status"] = 429
    stale = client.get_candles("BTC", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert stale == first  # last-good, not an empty pool wipe
    assert calls[-1] == "BTC"
    # Same pass, other coins must not each fire their own 429.
    before = len(calls)
    client.get_candles("ETH", "1m", start_ms=1, end_ms=2_000_000_000_000)
    client.get_candles("SOL", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert len(calls) == before

    # No tight retry: just inside the backoff window is still one attempt.
    clock["t"] = 1_000_000.0 + CANDLE_TTL_SEC + CANDLE_BACKOFF_BASE_SEC - 1
    client.get_candles("BTC", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert len(calls) == before

    # Backoff elapsed → exactly one more try, then a doubled wait.
    clock["t"] = 1_000_000.0 + CANDLE_TTL_SEC + CANDLE_BACKOFF_BASE_SEC
    mode["status"] = 429
    client.get_candles("BTC", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert len(calls) == before + 1
    doubled = CANDLE_BACKOFF_BASE_SEC * 2
    clock["t"] = 1_000_000.0 + CANDLE_TTL_SEC + CANDLE_BACKOFF_BASE_SEC + doubled - 1
    client.get_candles("ETH", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert len(calls) == before + 1

    # A success clears the backoff so the next 429 starts at the base delay.
    clock["t"] = 1_000_000.0 + CANDLE_TTL_SEC + CANDLE_BACKOFF_BASE_SEC + doubled
    mode["status"] = 200
    healed = client.get_candles("ETH", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert healed[0]["c"] == 2
    mode["status"] = 429
    client.get_candles("SOL", "1m", start_ms=1, end_ms=2_000_000_000_000)
    after_reset = len(calls)
    reset_at = clock["t"]
    clock["t"] = reset_at + CANDLE_BACKOFF_BASE_SEC - 1
    client.get_candles("SOL", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert len(calls) == after_reset
    clock["t"] = reset_at + CANDLE_BACKOFF_BASE_SEC
    client.get_candles("SOL", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert len(calls) == after_reset + 1


def test_atr14_is_absent_until_fourteen_ranges_exist():
    now = _now()
    assert atr14(_bars(now), now=now) is None
    t0 = now - 20 * 60
    bars = [
        {"t": t0 + i * 60, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1}
        for i in range(16)
    ]
    assert atr14(bars, now=now) == pytest.approx(2.0)
    assert local_bar_extreme(_bars(now), side="long", now=now) == pytest.approx(98.0)


def test_pools_from_bars_mark_taken_levels():
    now = _now()
    today = datetime.fromtimestamp(now, tz=NY).date()
    yesterday = datetime(today.year, today.month, today.day, 12, 0, tzinfo=NY).timestamp() - 86400
    bars = [
        {"t": yesterday, "o": 100, "h": 120, "l": 80, "c": 110, "v": 1},
        {"t": now - 120, "o": 100, "h": 121, "l": 90, "c": 100, "v": 1},
    ]
    pools = pools_from_bars(bars, now, last_price=100)
    by_name = {p.name: p for p in pools}
    assert by_name["PDH"].price == pytest.approx(120)
    assert by_name["PDH"].taken is True  # today's high 121
    assert by_name["PDL"].price == pytest.approx(80)
    assert by_name["PDL"].taken is False


# --- wiring -----------------------------------------------------------------


def test_entry_mode_model_b_is_selectable_and_rejects_40x(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("HL_NETWORK", "testnet")
    monkeypatch.setenv("I_UNDERSTAND_LIVE_TRADING", "true")
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("RISK_PER_TRADE", "0.02")
    monkeypatch.setenv("ENTRY_MODE", "model_b")
    monkeypatch.setenv("LEVERAGE", "20")
    monkeypatch.setenv("MODEL_B_TP_R", "1.5")
    settings = load_settings()
    assert settings.entry_mode == "model_b"
    assert settings.trading_mode == "live"
    assert settings.network == "testnet"
    assert settings.is_live
    assert settings.risk_per_trade == pytest.approx(0.02)
    assert settings.model_b_tp_r == pytest.approx(1.5)
    assert settings.leverage == 20
    assert settings.model_b_min_prints == density_min_prints() == 3
    assert settings.model_b_alo_timeout_sec == 0
    monkeypatch.setenv("MODEL_B_ALO_TIMEOUT_SEC", "15")
    assert load_settings().model_b_alo_timeout_sec == pytest.approx(15)
    monkeypatch.delenv("MODEL_B_ALO_TIMEOUT_SEC")

    monkeypatch.delenv("RISK_PER_TRADE")
    assert load_settings().risk_per_trade == pytest.approx(0.02)

    with pytest.raises(ValueError, match="20x"):
        Settings(entry_mode="model_b", leverage=40, risk_per_trade=0.02).validate()
    with pytest.raises(ValueError, match="20x"):
        Settings(entry_mode="model_b", leverage=10, risk_per_trade=0.02).validate()
    with pytest.raises(ValueError, match="RISK_PER_TRADE"):
        Settings(entry_mode="model_b", risk_per_trade=0.005).validate()
    with pytest.raises(ValueError, match="MODEL_B_TP_R"):
        Settings(entry_mode="model_b", risk_per_trade=0.02, model_b_tp_r=2.5).validate()
    # Breakout/OTE mode keeps the scalp risk band.
    Settings(entry_mode="both", leverage=20).validate()
    with pytest.raises(ValueError, match="RISK_PER_TRADE"):
        Settings(entry_mode="both", risk_per_trade=0.02).validate()


def test_vwap_does_not_absorb_model_b():
    strat = VwapTrendScalp(entry_mode="model_b", htf_confirm=False)
    assert strat.entry_mode == "model_b"
    sig = strat._pick_entry("long", 100.0, [], 90.0, None)
    assert sig.side == "flat"
    assert sig.reason == "model_b_separate"


def test_run_bot_routes_to_model_b(monkeypatch):
    called = {}

    def fake(settings, **kwargs):
        called["mode"] = settings.entry_mode
        called["kwargs"] = kwargs
        return {"entry_mode": "model_b", "arms": 0}

    monkeypatch.setattr("hl_bot.execution.model_b_loop.run_model_b", fake)
    summary = run_bot(Settings(entry_mode="model_b"), max_iterations=1, sleep_fn=lambda *_: None)
    assert called["mode"] == "model_b"
    assert summary["entry_mode"] == "model_b"


def test_paper_loop_rests_alo_until_sweep_prints_through(tmp_path):
    now = _now()
    clock = {"t": now}
    prints = _long_prints(now)
    bars = _bars(now)
    info = InfoClient()
    info.inject_bars(bars, coin="BTC")
    feed = MemoryFeed(prints, bbo={"BTC": (103.0, 105.0)})
    journal = tmp_path / "trades.jsonl"
    sleeps = {"n": 0}

    def sleep_fn(_sec):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            # Well past the old 20s timeout. The Alo must still be working.
            clock["t"] = now + 60
        elif sleeps["n"] == 2:
            feed._prints.append(
                TradePrint(ts=clock["t"] + 1, coin="BTC", price=90.0, size=1, side="buy", seq=999)
            )
            clock["t"] = clock["t"] + 1

    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            journal_path=str(journal),
            loop_interval_sec=0,
        ),
        max_iterations=3,
        info=info,
        feed=feed,
        sleep_fn=sleep_fn,
        now_fn=lambda: clock["t"],
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda coin, now_, bars_, last: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["arms"] == 1
    assert summary["cancels"] == 1
    rows = TradeJournal(journal).read_all()
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert len(arms) == 1
    arm = arms[0]
    for key in (
        "coin",
        "bias",
        "pool",
        "swing",
        "sweep_price",
        "absorb",
        "window_delta",
        "last_15s_delta",
        "score",
        "volume_tag",
    ):
        assert key in arm
    assert arm["fail_reason"] is None
    assert arm["volume_tag"] == "VOL_OK"
    cancels = [r for r in rows if r["event"] == "model_b_cancel"]
    assert len(cancels) == 1
    assert cancels[0]["reason"] == "thesis_stale"
    assert not any(r.get("reason") == "unfilled_20s" for r in rows)
    assert not any(r["event"] == "open" and r.get("reason") != "alo_fill" for r in rows)


def test_live_path_places_alo_not_market(tmp_path):
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})

    class FakeLive:
        def __init__(self):
            self.alos = []
            self.markets = []

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            assert leverage == 20
            self.alos.append((coin, is_buy, size, limit_px))
            return {"response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}}

        def market_open(self, *args, **kwargs):
            raise AssertionError("market fallback")

        def cancel_order(self, coin, oid):
            self.markets.append(("cancel", coin, oid))

        def market_close(self, *args, **kwargs):
            raise AssertionError("market close on entry")

    fake = FakeLive()
    settings = Settings(
        entry_mode="model_b",
        risk_per_trade=0.02,
        trading_mode="live",
        i_understand_live_trading=True,
        private_key="0x" + "ab" * 32,
        network="testnet",
        journal_path=str(tmp_path / "live.jsonl"),
        loop_interval_sec=0,
    )
    summary = run_model_b(
        settings,
        max_iterations=1,
        info=info,
        feed=feed,
        exchange=fake,
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["mode"] == "LIVE"
    assert summary["arms"] == 1
    assert len(fake.alos) == 1
    assert fake.alos[0][0] == "BTC"
    assert fake.alos[0][1] is True
    assert fake.markets == []


def test_spot_usdc_total_ignores_perp_account_value():
    assert parse_spot_usdc_total(
        {
            "balances": [
                {"coin": "USDC", "token": 0, "hold": "10", "total": "8000.5"},
                {"coin": "HYPE", "total": "3"},
            ]
        }
    ) == pytest.approx(8000.5)
    # A present zero is an empty wallet, not a missing read.
    assert parse_spot_usdc_total({"balances": [{"coin": "USDC", "total": "0"}]}) == 0
    # Perp clearinghouse account value is not a sizing base.
    assert parse_spot_usdc_total({"marginSummary": {"accountValue": "12.5"}}) is None
    assert parse_spot_usdc_total({"balances": [{"coin": "HYPE", "total": "3"}]}) is None


def test_live_size_is_two_percent_of_spot_usdc_not_starting_equity(tmp_path):
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(2500.0)
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})

    class FakeLive:
        def __init__(self):
            self.alos = []

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            self.alos.append(size)
            return {"response": {"data": {"statuses": [{"resting": {"oid": 3}}]}}}

    fake = FakeLive()
    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            starting_equity=9000.0,
            trading_mode="live",
            i_understand_live_trading=True,
            private_key="0x" + "ab" * 32,
            network="testnet",
            journal_path=str(tmp_path / "spot.jsonl"),
            loop_interval_sec=0,
        ),
        max_iterations=1,
        info=info,
        feed=feed,
        exchange=fake,
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["arms"] == 1
    # 2% of spot 2500 / 1.03 stop. 2% of STARTING_EQUITY 9000 would be ~174.
    assert fake.alos == [pytest.approx(48.543689)]


def test_live_without_spot_usdc_does_not_arm(tmp_path):
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(None)
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})

    class FakeLive:
        def __init__(self):
            self.alos = []

        def place_alo(self, *args, **kwargs):
            self.alos.append(args)
            raise AssertionError("sized without spot USDC")

    fake = FakeLive()
    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            starting_equity=5000.0,
            trading_mode="live",
            i_understand_live_trading=True,
            private_key="0x" + "cd" * 32,
            network="testnet",
            journal_path=str(tmp_path / "nosspot.jsonl"),
            loop_interval_sec=0,
        ),
        max_iterations=1,
        info=info,
        feed=feed,
        exchange=fake,
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["arms"] == 0
    assert fake.alos == []
    assert summary["fails"] >= 1
    rows = TradeJournal(tmp_path / "nosspot.jsonl").read_all()
    assert any(r.get("fail_reason") == "NO_SPOT_USDC" for r in rows)


def test_live_brackets_use_filled_alo_size(tmp_path):
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})

    class FakeLive:
        def __init__(self):
            self.stops = []
            self.tps = []

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            return {"response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}}

        def set_stop_loss(self, coin, is_buy, size, trigger_px):
            self.stops.append((size, trigger_px))

        def set_take_profit(self, coin, is_buy, size, trigger_px):
            self.tps.append((size, trigger_px))

    fake = FakeLive()

    def sleep_fn(_sec):
        feed._fills.append(UserFill("BTC", 11, 99.0, 40.0, now + 1, False))

    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            trading_mode="live",
            i_understand_live_trading=True,
            private_key="0x" + "ef" * 32,
            network="testnet",
            journal_path=str(tmp_path / "fill.jsonl"),
            loop_interval_sec=0,
        ),
        max_iterations=2,
        info=info,
        feed=feed,
        exchange=fake,
        sleep_fn=sleep_fn,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["arms"] == 1
    assert summary["opens"] == 1
    assert fake.stops and fake.stops[0][0] == pytest.approx(40.0)
    assert fake.tps and fake.tps[0][0] == pytest.approx(40.0)
    assert fake.stops[0][1] == pytest.approx(97.97)
    rows = TradeJournal(tmp_path / "fill.jsonl").read_all()
    opened = [r for r in rows if r["event"] == "open"]
    assert opened and opened[0]["size"] == pytest.approx(40.0)


def test_distance_to_fill_is_bps_and_tie_keeps_the_resting_order():
    # Long 21 points under a 120 mid is much farther than 4 points under a 103 mid.
    far = distance_to_fill_bps("long", 99.0, 120.0)
    near = distance_to_fill_bps("long", 99.0, 103.0)
    assert far == pytest.approx(21 / 120 * 10_000)
    assert near == pytest.approx(4 / 103 * 10_000)
    assert is_closer_to_fill(near, far)
    assert is_closer_to_fill(far, near) is False
    assert is_closer_to_fill(near, near) is False
    # A short already through the market is as close as a maker gets.
    assert distance_to_fill_bps("short", 85876.0, 86000.0) == 0.0


def _retag(prints, coin: str):
    return [replace(p, coin=coin) for p in prints]


def _two_coin_hunt(tmp_path, *, btc_last: float, eth_last: float, iterations: int = 1):
    now = _now()
    btc = _long_prints(now, last_price=btc_last, final_price=btc_last, sweep_px=99.0)
    eth = _retag(
        _long_prints(now, last_price=eth_last, final_price=eth_last, sweep_px=99.0),
        "ETH",
    )
    info = InfoClient()
    bars = _bars(now)
    info.inject_bars(bars, coin="BTC")
    info.inject_bars(bars, coin="ETH")
    feed = MemoryFeed(
        btc + eth,
        bbo={
            "BTC": (btc_last - 1.0, btc_last + 1.0),
            "ETH": (eth_last - 1.0, eth_last + 1.0),
        },
    )
    journal = tmp_path / "closer.jsonl"
    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            journal_path=str(journal),
            loop_interval_sec=0,
        ),
        max_iterations=iterations,
        info=info,
        feed=feed,
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC", "ETH"),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    return summary, TradeJournal(journal).read_all()


def test_closer_ticker_cancels_the_far_alo_and_places_the_near_one(tmp_path):
    """BTC rests far under the market. ETH's limit is closer, so BTC is cancelled."""
    summary, rows = _two_coin_hunt(tmp_path, btc_last=120.0, eth_last=103.0, iterations=2)
    assert summary["arms"] == 2
    assert summary["cancels"] == 1
    cancels = [r for r in rows if r["event"] == "model_b_cancel"]
    assert len(cancels) == 1
    assert cancels[0]["coin"] == "BTC"
    assert cancels[0]["reason"] == "closer_ticker"
    assert cancels[0]["winner"] == "ETH"
    assert cancels[0]["margin_for_better"] is True
    assert not any(r.get("reason") == "unfilled_20s" for r in rows)
    # The cancelled swing stays done, so the next pass does not repost BTC.
    assert any(r.get("coin") == "BTC" and r.get("fail_reason") == "THESIS_DONE" for r in rows)
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert [r["coin"] for r in arms] == ["BTC", "ETH"]


def test_farther_ticker_does_not_cancel_the_closer_alo(tmp_path):
    summary, rows = _two_coin_hunt(tmp_path, btc_last=103.0, eth_last=120.0)
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    fails = [r for r in rows if r.get("fail_reason") == "NOT_CLOSER"]
    assert len(fails) == 1
    assert fails[0]["coin"] == "ETH"
    assert fails[0]["held_by"] == "BTC"
    assert not any(r["event"] == "model_b_cancel" for r in rows)


def test_filled_position_is_not_cancelled_for_a_closer_ticker(tmp_path):
    """A fill keeps its brackets. The new coin may place because no Alo is resting."""
    now = _now()
    btc = _long_prints(now, last_price=120.0, final_price=120.0, sweep_px=99.0)
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_bars(_bars(now), coin="ETH")
    feed = MemoryFeed(btc, bbo={"BTC": (119.0, 121.0)})
    added = {"done": False}

    def sleep_fn(_sec):
        if added["done"]:
            return
        added["done"] = True
        feed._prints.extend(
            _retag(
                _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
                "ETH",
            )
        )
        feed._bbo["ETH"] = (102.0, 104.0)
        feed._prints.append(
            TradePrint(ts=now + 1, coin="BTC", price=99.0, size=1, side="sell", seq=800)
        )

    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            journal_path=str(tmp_path / "filled.jsonl"),
            loop_interval_sec=0,
        ),
        max_iterations=2,
        info=info,
        feed=feed,
        sleep_fn=sleep_fn,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC", "ETH"),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["cancels"] == 0
    assert summary["opens"] == 1
    assert summary["arms"] == 2
    rows = TradeJournal(tmp_path / "filled.jsonl").read_all()
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    assert not any(r["event"] == "close" for r in rows)
    assert any(r["event"] == "model_b_arm" and r["coin"] == "ETH" for r in rows)


def test_release_for_closer_refuses_an_open_position():
    now = _now()
    book = ThesisBook()
    decision = _decide(_long_prints(now), _bars(now), [Pool("PDH", 130, False)])
    intent = decision.intent
    assert intent is not None
    order = book.post(intent, now, oid=9)
    sell = TradePrint(ts=now + 1, coin="BTC", price=intent.limit_px, size=1, side="sell", seq=0)
    pos = book.try_fill_from_prints([sell])
    assert pos is not None
    assert book.release_for_closer("BTC") is None
    assert book.position("BTC") is pos
    book._state("BTC").working = WorkingOrder(
        coin="BTC",
        side="long",
        limit_px=intent.limit_px,
        size=intent.size,
        stop=intent.stop,
        take_profit=intent.take_profit,
        swing_id=intent.swing_id,
        posted_at=now,
        oid=9,
    )
    assert book.release_for_closer("BTC") is None
    assert book.position("BTC") is pos
    assert book.working("BTC") is not None


def test_partial_drip_keeps_remainder_until_thesis_stale():
    """BTC 0.0885 drip-filled 0.00036 must not drop the rest of the maker.

    Brackets follow the filled size. A later drip grows that size. The
    remainder stays working until a print through the sweep, and a closer
    coin cannot cancel it once any fill exists.
    """
    now = _now()
    book = ThesisBook()
    order_size = 0.0885
    intent = AloIntent(
        coin="BTC",
        side="long",
        limit_px=85876.0,
        size=order_size,
        stop=85000.0,
        take_profit=87000.0,
        swing_id="BTC:low:1",
        sweep_px=85876.0,
        tick=1.0,
        tp_r=2.5,
    )
    book.post(intent, now, oid=42)
    first = book.apply_user_fill(
        coin="BTC", oid=42, price=85876.0, ts=now + 1, crossed=False, size=0.00036
    )
    assert first is not None
    assert first.just_opened is True
    assert first.remainder_kept is True
    assert first.size == pytest.approx(0.00036)
    assert first.remainder_size == pytest.approx(order_size - 0.00036)
    resting = book.working("BTC")
    assert resting is not None and resting.oid == 42
    assert resting.size == pytest.approx(order_size - 0.00036)
    assert book.position("BTC") is first

    second = book.apply_user_fill(
        coin="BTC", oid=42, price=85876.0, ts=now + 2, crossed=False, size=0.001
    )
    assert second is first
    assert second.just_opened is False
    assert second.size == pytest.approx(0.00136)
    assert second.remainder_kept is True
    assert book.working("BTC") is not None
    assert book.working("BTC").size == pytest.approx(order_size - 0.00136)
    with pytest.raises(ValueError, match="AVERAGE_DOWN"):
        book.post(replace(intent, swing_id="other"), now + 3)
    assert book.release_for_closer("BTC") is None
    assert book.working("BTC") is not None

    through = TradePrint(
        ts=now + 4, coin="BTC", price=85870.0, size=1, side="buy", seq=1
    )
    cancelled = book.cancel_if_stale("BTC", [through])
    assert len(cancelled) == 1
    assert cancelled[0].oid == 42
    assert cancelled[0].size == pytest.approx(order_size - 0.00136)
    assert book.working("BTC") is None
    assert book.position("BTC") is not None
    assert book.position("BTC").size == pytest.approx(0.00136)


def test_live_partial_keeps_alo_and_stale_cancels_remainder(tmp_path):
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})
    sleeps = {"n": 0}
    before_stale: dict = {}

    class FakeLive:
        def __init__(self):
            self.cancels = []
            self.stops = []
            self.closes = []
            self._oid = 300

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            return {"response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}}

        def cancel_order(self, coin, oid):
            self.cancels.append((coin, oid))

        def set_stop_loss(self, coin, is_buy, size, trigger_px):
            self._oid += 1
            self.stops.append(size)
            return {"response": {"data": {"statuses": [{"resting": {"oid": self._oid}}]}}}

        def set_take_profit(self, coin, is_buy, size, trigger_px):
            self._oid += 1
            return {"response": {"data": {"statuses": [{"resting": {"oid": self._oid}}]}}}

        def market_close(self, *args, **kwargs):
            self.closes.append(args)

    fake = FakeLive()

    def sleep_fn(_sec):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            feed._fills.append(UserFill("BTC", 11, 99.0, 0.00036, now + 1, False))
        elif sleeps["n"] == 2:
            before_stale["cancels"] = list(fake.cancels)
            before_stale["stops"] = list(fake.stops)
            feed._fills.append(UserFill("BTC", 11, 99.0, 0.001, now + 2, False))
            feed._prints.append(
                TradePrint(ts=now + 3, coin="BTC", price=90.0, size=1, side="buy", seq=900)
            )

    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            trading_mode="live",
            i_understand_live_trading=True,
            private_key="0x" + "11" * 32,
            network="testnet",
            journal_path=str(tmp_path / "partial.jsonl"),
            loop_interval_sec=0,
        ),
        max_iterations=3,
        info=info,
        feed=feed,
        exchange=fake,
        sleep_fn=sleep_fn,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["opens"] == 1
    assert summary["closes"] == 0
    assert summary["arms"] == 1
    assert fake.closes == []
    # First drip places a bracket and does not cancel the Alo.
    assert before_stale["stops"] == [pytest.approx(0.00036)]
    assert all(oid != 11 for _coin, oid in before_stale["cancels"])
    # Second drip resizes the bracket. Thesis-stale then cancels the Alo only.
    assert fake.stops[-1] == pytest.approx(0.00136)
    assert ("BTC", 11) in fake.cancels
    rows = TradeJournal(tmp_path / "partial.jsonl").read_all()
    kept = [r for r in rows if r.get("event") == "model_b_partial" and r.get("remainder") == "kept"]
    assert len(kept) == 2
    assert kept[0]["remainder_size"] == pytest.approx(97.087378 - 0.00036)
    cancelled = [
        r for r in rows
        if r.get("event") == "model_b_cancel" and r.get("reason") == "thesis_stale"
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["remainder"] == "cancelled"
    assert cancelled[0]["position_kept"] is True
    assert cancelled[0].get("size") == pytest.approx(97.087378 - 0.00136)

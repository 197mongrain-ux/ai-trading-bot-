"""Model B: tape, bias, Alo, thesis, liquidity TP, and ENTRY_MODE wiring."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
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
from hl_bot.exchange.account import (
    AccountSnapshot,
    EntryOrder,
    PerpPosition,
    parse_clearinghouse,
    parse_entry_orders,
)
from hl_bot.execution.model_b_loop import (
    ClosePreference,
    _margin_in_use,
    close_distance_bps,
    format_model_b_fail,
    is_close_setup,
    model_b_hunt_coins,
    preferred_close,
    run_model_b,
    stick_preferred,
)
from hl_bot.journal import TradeJournal
from hl_bot.strategy.model_b.alo import (
    alo_limit,
    distance_to_fill_bps,
    is_closer_to_fill,
    resting_score_blocks_closer_cancel,
)
from hl_bot.strategy.model_b.bias import resolve_bias
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.risk import (
    FLOW_EXIT_ENABLED,
    LEVERAGE,
    RISK_PCT,
    SIZE_ADJUST_WIDE_STOP,
    SOFT_PROP_ENABLED,
    STRATEGY_KILL_ENABLED,
    arm_take_profit,
    collides_with_fill,
    flow_exit_reason,
    heal_stop,
    floor_distance,
    CLOSE_MARGIN_RESERVE,
    initial_margin,
    leaves_reserve_headroom,
    locked_take_profit,
    min_tp_distance,
    next_liquidity,
    other_margin_cap,
    place_stop,
    size_adjust_tag,
    size_from_stop,
    ticket_fits,
    soft_prop_allows,
    stop_beyond_extreme,
    stop_clears_fees,
    take_profit,
    tp_fail_detail,
    tp_is_valid,
)
from hl_bot.strategy.model_b.score import log_only_score, volume_tag
from hl_bot.strategy.model_b.swings import atr14, local_bar_extreme
from hl_bot.strategy.model_b.tape import (
    ABSORB_MIN,
    DELTA_FLAT_EPS,
    DELTA_FLAT_USDC,
    MAINNET_BTC_PRINTS_PER_MIN,
    MIN_PRINTS,
    MIN_PRINTS_FLOOR,
    TESTNET_BTC_PRINTS_PER_MIN,
    _delta_aligned,
    density_min_prints,
    flat_eps_coins,
    window_prints,
)
from hl_bot.strategy.model_b.thesis import ThesisBook, WorkingOrder
from hl_bot.strategy.model_b.types import AloIntent, Pool, TradePrint
from hl_bot.strategy.model_b.universe import (
    AFTER_HOURS_COINS,
    DEFAULT_HUNT_COINS,
    NY_COINS,
    perp_dexs_for,
    session_coins,
)
from hl_bot.strategy.vwap import VwapTrendScalp

NY = ZoneInfo("America/New_York")


def _now() -> float:
    return datetime(2026, 10, 5, 10, 0, tzinfo=NY).timestamp()


def _tight_bars(now: float, low: float = 100.0) -> list[dict]:
    """Same swing as ``_bars``, without the wick that widens the stop.

    The anchor stays on the sweep, so the stop clears wick room instead of
    resting a few ticks off the fill. Two of those tickets still do not
    fit in a 5000 balance.
    """
    bars = _bars(now, low=low)
    bars[-1]["l"] = low
    return bars


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
    prefix_step: float = 0.3,
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
    # Stay before the sweep at -40s. A tighter step packs a score-9 tape
    # (90 prints) without dumping buys into the reclaim leg.
    for i in range(n_prefix):
        add(-55 + i * prefix_step, 104.0, 0.5, "buy")
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


_EXPANDED = (
    "BTC",
    "ETH",
    "SOL",
    "NEAR",
    "PUMP",
    "LIT",
    "AAVE",
    "ONDO",
    "WLD",
    "TAO",
    "xyz:GOLD",
    "xyz:SP500",
    "xyz:XYZ100",
)


def test_ny_session_universe_and_after_hours():
    morning = datetime(2026, 10, 5, 9, 0, tzinfo=NY)
    late = datetime(2026, 10, 5, 15, 59, tzinfo=NY)
    close = datetime(2026, 10, 5, 16, 0, tzinfo=NY)
    pre = datetime(2026, 10, 5, 8, 59, tzinfo=NY)
    weekend = datetime(2026, 10, 10, 10, 0, tzinfo=NY)
    assert session_coins(morning) == NY_COINS == AFTER_HOURS_COINS == DEFAULT_HUNT_COINS
    assert session_coins(late) == _EXPANDED
    assert session_coins(close) == _EXPANDED
    assert session_coins(pre) == _EXPANDED
    # Weekend uses the same list. Saturday morning still includes TAO and xyz.
    assert session_coins(weekend) == _EXPANDED
    assert _EXPANDED[:3] == ("BTC", "ETH", "SOL")
    assert "xyz:XYZ100" in _EXPANDED
    assert "xyz:SP500" in _EXPANDED


def test_session_coins_follows_explicit_symbols():
    now = datetime(2026, 10, 5, 16, 30, tzinfo=NY)
    assert session_coins(now, symbols=("eth", "XYZ:gold", "btc")) == ("ETH", "xyz:GOLD", "BTC")
    assert session_coins(now, symbols=("xyz:sp500",)) == ("xyz:SP500",)


def test_tao_after_hours_stays_in_the_hunt():
    now = datetime(2026, 10, 5, 16, 30, tzinfo=NY).timestamp()
    decision = _decide(
        _long_prints(now),
        _bars(now),
        [Pool("PDH", 130, False)],
        now=now,
        coin="TAO",
    )
    assert decision.coin == "TAO"
    assert decision.fail_reason != "OUT_OF_SESSION"
    assert "TAO" in session_coins(now)


def test_coin_outside_universe_is_out_of_session():
    for stamp in (
        datetime(2026, 10, 5, 10, 0, tzinfo=NY),
        datetime(2026, 10, 5, 16, 30, tzinfo=NY),
    ):
        decision = _decide(
            _long_prints(stamp.timestamp()),
            _bars(stamp.timestamp()),
            [Pool("PDH", 130, False)],
            now=stamp.timestamp(),
            coin="XRP",
        )
        assert decision.fail_reason == "OUT_OF_SESSION"
        assert decision.armed is False


def test_xyz_name_is_in_session_with_lowercase_dex():
    now = datetime(2026, 10, 5, 20, 0, tzinfo=NY).timestamp()
    decision = _decide(
        _long_prints(now),
        _bars(now),
        [Pool("PDH", 130, False)],
        now=now,
        coin="XYZ:gold",
    )
    assert decision.coin == "xyz:GOLD"
    assert decision.fail_reason != "OUT_OF_SESSION"


def test_perp_dexs_include_xyz_and_keep_the_original_dex():
    assert perp_dexs_for(("BTC", "ETH", "SOL")) is None
    assert perp_dexs_for(("BTC", "XYZ:GOLD", "xyz:SP500")) == ["", "xyz"]


def test_model_b_hunt_coins_uses_env_symbols(monkeypatch):
    monkeypatch.delenv("SYMBOLS", raising=False)
    monkeypatch.delenv("SYMBOL", raising=False)
    bare = Settings(
        entry_mode="model_b",
        risk_per_trade=0.02,
        symbols=("BTC", "SOL", "XRP"),
    )
    assert model_b_hunt_coins(bare) == DEFAULT_HUNT_COINS
    monkeypatch.setenv("SYMBOLS", "btc,XYZ:gold")
    configured = Settings(
        entry_mode="model_b",
        risk_per_trade=0.02,
        symbols=("BTC", "XYZ:GOLD"),
    )
    assert model_b_hunt_coins(configured) == ("BTC", "xyz:GOLD")
    assert model_b_hunt_coins(configured, coins=("ETH",)) == ("ETH",)
    monkeypatch.delenv("SYMBOLS", raising=False)
    monkeypatch.setenv("SYMBOL", "xyz:xyz100")
    single = Settings(entry_mode="model_b", risk_per_trade=0.02, symbols=("xyz:XYZ100",))
    assert model_b_hunt_coins(single) == ("xyz:XYZ100",)


def test_trade_feed_subscribes_xyz_with_lowercase_dex():
    feed = HyperliquidTradeFeed(network="mainnet", coins=("BTC", "XYZ:gold", "xyz:SP500"))
    assert feed.coins == ("BTC", "xyz:GOLD", "xyz:SP500")
    names = [
        p["subscription"]["coin"]
        for p in feed.resubscribe_payloads()
        if p.get("subscription", {}).get("type") == "trades"
    ]
    assert names == ["BTC", "xyz:GOLD", "xyz:SP500"]
    stored = feed.ingest(
        {
            "channel": "trades",
            "data": [
                {
                    "coin": "xyz:GOLD",
                    "px": "4100",
                    "sz": "0.1",
                    "side": "B",
                    "time": 1_700_000_000_000,
                    "tid": 1,
                }
            ],
        }
    )
    assert stored and stored[0].coin == "xyz:GOLD"
    assert len(feed.prints("XYZ:gold")) == 1


def test_model_b_feed_subscribes_default_or_env_symbols(monkeypatch, tmp_path):
    captured: dict = {}

    class CaptureFeed:
        def __init__(self, network="mainnet", coins=(), user=None, maxlen=5000):
            captured["coins"] = tuple(coins)

        def prints(self, coin):
            return []

        def bbo(self, coin):
            return (None, None)

        def take_user_fills(self):
            return []

    monkeypatch.setattr("hl_bot.execution.model_b_loop.HyperliquidTradeFeed", CaptureFeed)

    def _no_network(*_a, **_k):
        raise AssertionError("candle network")

    monkeypatch.setattr("hl_bot.exchange.info_client._post_candle_snapshot", _no_network)
    monkeypatch.delenv("SYMBOLS", raising=False)
    monkeypatch.delenv("SYMBOL", raising=False)
    info = InfoClient()
    info.inject_bars([], coin=None)

    def _run(journal: str, symbols=("BTC", "SOL", "XRP"), coins=None):
        run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(tmp_path / journal),
                loop_interval_sec=0,
                symbols=symbols,
            ),
            max_iterations=1,
            info=info,
            sleep_fn=lambda *_: None,
            now_fn=_now,
            connect_feed=False,
            coins=coins,
        )

    _run("default.jsonl")
    assert captured["coins"] == _EXPANDED

    monkeypatch.setenv("SYMBOLS", "BTC,XYZ:gold,xyz:SP500")
    _run("env.jsonl", symbols=("BTC", "XYZ:GOLD", "xyz:SP500"))
    assert captured["coins"] == ("BTC", "xyz:GOLD", "xyz:SP500")

    _run("override.jsonl", coins=("ETH",))
    assert captured["coins"] == ("ETH",)


def test_candle_snapshot_keeps_xyz_dex_lowercase(monkeypatch):
    calls: list[str] = []

    def fake_post(base_url, coin, interval, start_ms, end_ms, timeout=15.0):
        calls.append(coin)
        return 200, []

    monkeypatch.setattr("hl_bot.exchange.info_client._post_candle_snapshot", fake_post)
    client = InfoClient(base_url="https://example.invalid")
    client.get_candles("XYZ:gold", "1m", start_ms=1, end_ms=2_000_000_000_000)
    client.get_candles("xyz:GOLD", "1m", start_ms=1, end_ms=2_000_000_000_000)
    assert calls == ["xyz:GOLD"]


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
    assert decision.delta_flat is None
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
    # PDH 130 is the next liquidity. It is the target, not 2R of the wick.
    assert intent.take_profit == pytest.approx(130)
    assert intent.pool_px == pytest.approx(130)
    assert intent.size == pytest.approx(97.087378)  # 5000 * 2% / 1.03
    # Inside 1.5% of entry. Not the wide-stop reduction.
    assert decision.size_adjust is None
    assert decision.to_log()["size_adjust"] is None


def test_absorb_boundary():
    assert ABSORB_MIN == pytest.approx(1.3)
    # 8*1.0 + 2*1.0 = 10 buy after reclaim, 2*6.5 = 13 sell → 1.3 exactly.
    exact = _pass_case(sweep_sz=6.5, reclaim_sz=1.0, late_sz=1.0)
    assert exact.armed is True
    assert exact.absorb == pytest.approx(1.3)

    # 13.4 / 10 = 1.34, the Oct 6 ETH short that peaked under the old 1.5 floor.
    near = _pass_case(sweep_sz=6.7, reclaim_sz=1.0, late_sz=1.0)
    assert near.armed is True
    assert near.absorb == pytest.approx(1.34)

    # 12.9 / 10 = 1.29
    under = _pass_case(sweep_sz=6.45, reclaim_sz=1.0, late_sz=1.0)
    assert under.armed is False
    assert under.fail_reason == "ABSORB"
    assert under.absorb == pytest.approx(1.29)


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


def test_flat_delta_band_passes_and_adverse_still_fails():
    """The band is the larger of $100 / mid and 0.05 coins.

    Delta is buy size minus sell size. On SOL the $100 term wins. On BTC
    the 0.05-coin floor wins. Either term at 0 drops that term. Both at 0
    is the strict sign check.
    """
    assert DELTA_FLAT_USDC == pytest.approx(100)
    assert DELTA_FLAT_EPS == pytest.approx(0.05)
    assert flat_eps_coins(100, 150) == pytest.approx(100 / 150)
    assert flat_eps_coins(100, 86000) == pytest.approx(0.05)
    assert flat_eps_coins(100, 150) > 0.05
    assert flat_eps_coins(100, 86000, 0) == pytest.approx(100 / 86000)
    sol_eps = flat_eps_coins(100, 150)
    btc_eps = flat_eps_coins(100, 86000)
    assert _delta_aligned("short", 0.19, sol_eps)
    assert not _delta_aligned("short", 1.1, sol_eps)
    assert _delta_aligned("long", -0.05, btc_eps)
    assert not _delta_aligned("long", -0.06, btc_eps)
    assert _delta_aligned("short", 0.006, btc_eps)
    assert flat_eps_coins(0, 150, 0.05) == pytest.approx(0.05)
    assert flat_eps_coins(0, 150, 0) == 0
    assert _delta_aligned("long", -0.05, DELTA_FLAT_EPS)
    assert not _delta_aligned("long", -0.06, DELTA_FLAT_EPS)
    assert _delta_aligned("long", -1e-9, 0.0)
    assert not _delta_aligned("long", -0.01, 0.0)

    # Base window delta is +4. A 4.04 sell outside the last 15s leaves dW −0.04.
    flat = _pass_case(extra=[(-20.0, 104.0, 4.04, "sell")])
    assert flat.armed is True
    assert flat.fail_reason is None
    assert flat.window_delta == pytest.approx(-0.04)
    assert flat.last_15s_delta == pytest.approx(1.0)
    assert flat.delta_flat == "window"
    assert flat.to_log()["delta_flat"] == "window"
    assert flat.absorb == pytest.approx(15 / 9)

    last = _pass_case(extra=[(-5.0, 104.0, 1.04, "sell")])
    assert last.armed is True
    assert last.delta_flat == "last_15s"
    assert last.window_delta == pytest.approx(2.96)
    assert last.last_15s_delta == pytest.approx(-0.04)

    both = _pass_case(
        extra=[(-20.0, 104.0, 3.0, "sell"), (-5.0, 104.0, 1.04, "sell")]
    )
    assert both.armed is True
    assert both.delta_flat == "both"
    assert both.window_delta == pytest.approx(-0.04)
    assert both.last_15s_delta == pytest.approx(-0.04)

    # −0.06 coins at a ~104 mid is about $6, inside the $100 band.
    small = _pass_case(extra=[(-20.0, 104.0, 4.06, "sell")])
    assert small.armed is True
    assert small.delta_flat == "window"
    assert small.window_delta == pytest.approx(-0.06)
    assert small.delta_flat_eps == pytest.approx(100 / 104)
    assert small.delta_flat_usdc_eps == pytest.approx(100 / 104)
    assert small.delta_flat_coin_eps == pytest.approx(0.05)
    assert small.delta_flat_px == pytest.approx(104)

    # −1.20 coins at that mid is about $125, still DELTA.
    outside = _pass_case(extra=[(-20.0, 104.0, 5.20, "sell")])
    assert outside.armed is False
    assert outside.fail_reason == "DELTA"
    assert outside.delta_flat is None
    assert outside.window_delta == pytest.approx(-1.20)

    coin_band = _pass_case(
        extra=[(-20.0, 104.0, 4.06, "sell")],
        engine=ModelBEngine(delta_flat_usdc=0, delta_flat_eps=0.05),
    )
    assert coin_band.armed is False
    assert coin_band.fail_reason == "DELTA"
    assert coin_band.delta_flat_eps == pytest.approx(0.05)

    assert _pass_case(sweep_sz=100.0).fail_reason == "DELTA"

    strict = _pass_case(
        extra=[(-20.0, 104.0, 4.04, "sell")],
        engine=ModelBEngine(delta_flat_usdc=0, delta_flat_eps=0.0),
    )
    assert strict.armed is False
    assert strict.fail_reason == "DELTA"
    assert strict.delta_flat is None

    now = _now()
    short_flat_prints = _short_prints(now)
    short_flat_prints.append(
        TradePrint(ts=now - 20, coin="BTC", price=96.0, size=4.04, side="buy", seq=1000)
    )
    short_flat = _decide(short_flat_prints, _short_bars(now), [], bid=95.0, ask=96.5)
    assert short_flat.armed is True
    assert short_flat.intent is not None and short_flat.intent.side == "short"
    assert short_flat.window_delta == pytest.approx(0.04)
    assert short_flat.delta_flat == "window"

    short_last = _short_prints(now)
    short_last.append(
        TradePrint(ts=now - 5, coin="BTC", price=96.0, size=1.04, side="buy", seq=1000)
    )
    short_last_decision = _decide(short_last, _short_bars(now), [], bid=95.0, ask=96.5)
    assert short_last_decision.armed is True
    assert short_last_decision.delta_flat == "last_15s"
    assert short_last_decision.last_15s_delta == pytest.approx(0.04)

    short_adverse = _short_prints(now)
    short_adverse.append(
        TradePrint(ts=now - 20, coin="BTC", price=96.0, size=4.19, side="buy", seq=1000)
    )
    blocked = _decide(short_adverse, _short_bars(now), [], bid=95.0, ask=96.5)
    assert blocked.armed is True
    assert blocked.delta_flat == "window"
    assert blocked.window_delta == pytest.approx(0.19)

    short_wide = _short_prints(now)
    short_wide.append(
        TradePrint(ts=now - 20, coin="BTC", price=96.0, size=5.20, side="buy", seq=1001)
    )
    wide_block = _decide(short_wide, _short_bars(now), [], bid=95.0, ask=96.5)
    assert wide_block.armed is False
    assert wide_block.fail_reason == "DELTA"
    assert wide_block.window_delta == pytest.approx(1.20)
    assert wide_block.delta_flat is None

    short_strict_prints = _short_prints(now)
    short_strict_prints.append(
        TradePrint(ts=now - 20, coin="BTC", price=96.0, size=4.04, side="buy", seq=1000)
    )
    short_strict = _decide(
        short_strict_prints,
        _short_bars(now),
        [],
        bid=95.0,
        ask=96.5,
        engine=ModelBEngine(delta_flat_usdc=0, delta_flat_eps=0.0),
    )
    assert short_strict.armed is False
    assert short_strict.fail_reason == "DELTA"


def test_btc_flat_band_keeps_the_coin_floor():
    """$100 / mid is ~0.0012 BTC. The 0.05-coin floor is the band there.

    A short with dW +0.006 and last-15s +0.0018 failed when the band was
    only the USDC term. It passes again. A print past 0.05 coins still
    fails. Setting the coin floor to 0 restores the tight USDC band.
    """
    shift = 85900.0
    now = _now()
    mid = 85996.0

    def btc_short(extra: list[TradePrint]) -> list[TradePrint]:
        prints = []
        for print_ in _short_prints(now):
            prints.append(
                TradePrint(
                    ts=print_.ts,
                    coin=print_.coin,
                    price=print_.price + shift,
                    size=print_.size,
                    side=print_.side,
                    seq=print_.seq,
                )
            )
        prints.extend(extra)
        return prints

    bars = []
    for bar in _short_bars(now):
        row = dict(bar)
        for key in ("o", "h", "l", "c"):
            row[key] = bar[key] + shift
        bars.append(row)

    # Base window is −4 and last-15s is −1. These two buys land on the
    # observed wrong-way tape: dW +0.006, last 15s +0.0018.
    saved = _decide(
        btc_short(
            [
                TradePrint(ts=now - 20, coin="BTC", price=mid, size=3.0042, side="buy", seq=1000),
                TradePrint(ts=now - 5, coin="BTC", price=mid, size=1.0018, side="buy", seq=1001),
            ]
        ),
        bars,
        [Pool("PDL", 80000.0, False)],
        bid=85995.0,
        ask=85997.0,
        tick=1.0,
        equity=10_000.0,
    )
    assert saved.armed is True
    assert saved.intent is not None and saved.intent.side == "short"
    assert saved.window_delta == pytest.approx(0.006)
    assert saved.last_15s_delta == pytest.approx(0.0018)
    assert saved.delta_flat == "both"
    assert saved.delta_flat_px == pytest.approx(mid)
    assert saved.delta_flat_usdc_eps == pytest.approx(100 / mid)
    assert saved.delta_flat_coin_eps == pytest.approx(0.05)
    assert saved.delta_flat_eps == pytest.approx(0.05)
    assert saved.delta_flat_eps == pytest.approx(
        max(saved.delta_flat_usdc_eps, saved.delta_flat_coin_eps)
    )

    past_coin = _decide(
        btc_short(
            [TradePrint(ts=now - 20, coin="BTC", price=mid, size=4.06, side="buy", seq=1000)]
        ),
        bars,
        [Pool("PDL", 80000.0, False)],
        bid=85995.0,
        ask=85997.0,
        tick=1.0,
        equity=10_000.0,
    )
    assert past_coin.armed is False
    assert past_coin.fail_reason == "DELTA"
    assert past_coin.window_delta == pytest.approx(0.06)
    assert past_coin.delta_flat_eps == pytest.approx(0.05)

    usdc_only = _decide(
        btc_short(
            [
                TradePrint(ts=now - 20, coin="BTC", price=mid, size=3.0042, side="buy", seq=1000),
                TradePrint(ts=now - 5, coin="BTC", price=mid, size=1.0018, side="buy", seq=1001),
            ]
        ),
        bars,
        [Pool("PDL", 80000.0, False)],
        bid=85995.0,
        ask=85997.0,
        tick=1.0,
        equity=10_000.0,
        engine=ModelBEngine(delta_flat_usdc=100, delta_flat_eps=0.0),
    )
    assert usdc_only.armed is False
    assert usdc_only.fail_reason == "DELTA"
    assert usdc_only.delta_flat_eps == pytest.approx(100 / mid)


def test_flat_delta_save_is_logged(tmp_path, caplog):
    now = _now()
    prints = _long_prints(now, extra=[(-20.0, 104.0, 4.04, "sell")])
    journal = tmp_path / "flat.jsonl"
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(journal),
                loop_interval_sec=0,
            ),
            max_iterations=1,
            info=info,
            feed=MemoryFeed(prints, bbo={"BTC": (103.0, 105.0)}),
            sleep_fn=lambda *_: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC",),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    assert summary["arms"] == 1
    assert any(
        "MODEL_B DELTA_FLAT" in record.message
        and "saved=window" in record.message
        and "chosen=" in record.message
        and f"usdc_eps={100 / 104}" in record.message
        and "coin_eps=0.05" in record.message
        and "usdc=100.0" in record.message
        and "px=104.0" in record.message
        for record in caplog.records
    )
    arm = next(row for row in TradeJournal(journal).read_all() if row["event"] == "model_b_arm")
    assert arm["delta_flat"] == "window"
    assert arm["window_delta"] == pytest.approx(-0.04)
    assert arm["delta_flat_eps"] == pytest.approx(100 / 104)
    assert arm["delta_flat_usdc_eps"] == pytest.approx(100 / 104)
    assert arm["delta_flat_coin_eps"] == pytest.approx(0.05)
    assert arm["delta_flat_px"] == pytest.approx(104)


def test_bad_tp_logs_r_to_pool_and_does_not_change_the_band(monkeypatch):
    """Fail labels stay put. The raw helper still returns a caller-supplied pool.

    The hunt itself skips a pool inside 1R or inside the fee. That filter
    is ``next_liquidity``, not this label helper.
    """
    close = tp_fail_detail("long", 100.0, 99.0, 100.4, 100.0)
    assert close["why"] == "pool_too_close"
    assert close["r_distance"] == pytest.approx(1.0)
    assert close["pool_distance"] == pytest.approx(0.4)
    assert close["pool_r"] == pytest.approx(0.4)

    # The pool is 3R away, but the rejected target never left the entry.
    # That is not an over-2R scrap.
    far = tp_fail_detail("long", 100.0, 99.0, 103.0, 100.0)
    assert far["why"] == "under_1r"
    assert far["pool_r"] == pytest.approx(3.0)
    past_band = tp_fail_detail("long", 100.0, 99.0, 110.0, 104.0)
    assert past_band["why"] == "over_2r"
    assert past_band["pool_r"] == pytest.approx(10.0)

    under = tp_fail_detail("long", 100.0, 99.0, None, 100.0)
    assert under["why"] == "under_1r"
    assert under["pool_distance"] is None

    fees = tp_fail_detail("long", 100.0, 99.99, None, 100.0)
    assert fees["why"] == "fees"

    short = tp_fail_detail("short", 100.0, 101.0, 99.6, 100.0)
    assert short["why"] == "pool_too_close"
    assert short["pool_distance"] == pytest.approx(0.4)

    # Pool cap inside 1R stays a legal TP.
    capped = take_profit("long", 99.0, 97.97, 99.4, tp_r=1.5)
    assert capped == pytest.approx(99.4)
    assert tp_is_valid("long", 99.0, capped)
    assert arm_take_profit("long", 99.0, 97.97, 99.4, tp_r=1.5) == pytest.approx(99.4)

    # Zero stop distance cannot be placed beyond the entry.
    assert arm_take_profit("long", 100.0, 100.0, 130.0) is None
    assert tp_is_valid("long", 100.0, 100.0) is False
    assert tp_is_valid("short", 100.0, 101.0) is False

    armed = _pass_case()
    assert armed.armed is True
    assert armed.bad_tp_why is None
    assert armed.intent is not None and armed.intent.take_profit > armed.intent.limit_px

    def no_target(side, entry, stop, pool_price, tp_r=1.5):
        del side, entry, stop, pool_price, tp_r
        return None

    monkeypatch.setattr("hl_bot.strategy.model_b.engine.arm_take_profit", no_target)
    failed = _pass_case()
    assert failed.armed is False
    assert failed.intent is None
    assert failed.fail_reason == "BAD_TP"
    assert failed.r_distance == pytest.approx(1.03)
    assert failed.pool_distance == pytest.approx(31.0)
    assert failed.pool_r == pytest.approx(31.0 / 1.03)
    # The pool is far. The rejected target is the entry, so the label is
    # not over_2r.
    assert failed.bad_tp_why == "under_1r"
    assert failed.to_log()["bad_tp_why"] == "under_1r"
    text = format_model_b_fail(failed)
    assert "reason=BAD_TP" in text
    assert "why=under_1r" in text
    assert "pool_r=" in text


def test_far_pool_short_targets_the_pool_and_clears_wick_room():
    """ETH/SOL shape: no wick past the fill, pool many R away, flow already clear.

    The few-tick buffer is inside wick room, so the stop clears the room.
    The pool is the target. It is not pulled back to 2R, and the idea arms.
    """
    now = _now()
    entry = 3000.0
    tick = 0.01
    pool = entry - 17.8
    last = 2986.0
    swing = 2990.0
    t0 = now - 400
    bars = [
        {"t": t0, "o": 2970, "h": 2980, "l": 2960, "c": 2975, "v": 1},
        {"t": t0 + 60, "o": 2975, "h": swing, "l": 2970, "c": 2982, "v": 1},
        {"t": t0 + 120, "o": 2982, "h": 2985, "l": 2974, "c": 2980, "v": 1},
        {"t": t0 + 180, "o": 2980, "h": 2988, "l": 2976, "c": 2984, "v": 1},
    ]
    prints: list[TradePrint] = []
    seq = 0

    def add(offset, price, size, side):
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="ETH", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, last, 0.5, "sell")
    add(-40, entry, 7.5, "buy")
    add(-38, entry, 7.5, "buy")
    for i in range(8):
        add(-30 + i * 0.2, last, 1.0, "sell")
    add(-10, last, 0.5, "sell")
    add(-1, last, 0.5, "sell")

    decision = _decide(
        prints,
        bars,
        [Pool("PDL", pool, taken=False)],
        coin="ETH",
        bid=last - 1.0,
        ask=last + 0.5,
        tick=tick,
    )
    assert decision.armed is True
    assert decision.fail_reason is None
    assert decision.bad_tp_why is None
    intent = decision.intent
    assert intent is not None
    assert intent.side == "short"
    assert intent.limit_px == pytest.approx(entry)
    # No high past the fill. Room is 4.5; the pad is 2 bps (0.6).
    assert intent.stop == pytest.approx(3005.1)
    assert intent.stop != pytest.approx(entry + 0.6)
    dist = intent.stop - intent.limit_px
    assert dist == pytest.approx(5.1)
    assert intent.take_profit == pytest.approx(pool)
    assert intent.pool_px == pytest.approx(pool)
    assert intent.take_profit != pytest.approx(entry - 2.0 * 0.6)
    sized, _dollar = size_from_stop(5000.0, intent.limit_px, intent.stop, risk_pct=0.02)
    assert intent.size == pytest.approx(sized)


def test_tp_r_uses_the_widened_stop_not_a_tight_placeholder():
    """A local high past the fill is the R. A 2 bps placeholder is not."""
    now = _now()
    entry = 3000.0
    tick = 0.01
    pool = entry - 17.8
    last = 2986.0
    swing = 2990.0
    wick = 3010.0
    t0 = now - 400
    bars = [
        {"t": t0, "o": 2970, "h": 2980, "l": 2960, "c": 2975, "v": 1},
        {"t": t0 + 60, "o": 2975, "h": swing, "l": 2970, "c": 2982, "v": 1},
        {"t": t0 + 120, "o": 2982, "h": 2985, "l": 2974, "c": 2980, "v": 1},
        {"t": t0 + 180, "o": 2980, "h": wick, "l": 2976, "c": 2984, "v": 1},
    ]
    prints: list[TradePrint] = []
    seq = 0

    def add(offset, price, size, side):
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="ETH", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, last, 0.5, "sell")
    add(-40, entry, 7.5, "buy")
    add(-38, entry, 7.5, "buy")
    for i in range(8):
        add(-30 + i * 0.2, last, 1.0, "sell")
    add(-10, last, 0.5, "sell")
    add(-1, last, 0.5, "sell")

    decision = _decide(
        prints,
        bars,
        [Pool("PDL", pool, taken=False)],
        coin="ETH",
        bid=last - 1.0,
        ask=last + 0.5,
        tick=tick,
    )
    assert decision.armed is True
    intent = decision.intent
    assert intent is not None
    assert intent.stop == pytest.approx(wick + 0.6)
    dist = intent.stop - intent.limit_px
    assert dist == pytest.approx(10.6)
    # The local high is the stop. The pool is the target, not 1.5R of
    # that stop and not 2R of a 0.6 placeholder.
    assert intent.take_profit == pytest.approx(pool)
    assert intent.take_profit != pytest.approx(entry - 1.2)
    assert intent.take_profit != pytest.approx(entry - 1.5 * dist)


def test_liquidity_tp_is_kept_past_2r():
    """A pool many R away is the target. It is not pulled back to 2R."""
    tp = arm_take_profit("short", 3000.0, 3000.6, 2982.2, tp_r=1.5)
    assert tp == pytest.approx(2982.2)
    assert tp != pytest.approx(3000.0 - 1.2)
    assert tp_is_valid("short", 3000.0, tp)


def test_infinite_absorb_journals_null_and_still_passes():
    """No reclaim-side size is an infinite ratio. The gate stays. The log does not."""
    assert math.inf >= ABSORB_MIN
    now = _now()
    prints: list[TradePrint] = []
    seq = 0

    def add(offset, price, size, side):
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="BTC", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(26):
        add(-55 + i * 0.2, 104.0, 0.5, "buy")
    add(-40, 99.0, 0.4, "sell")
    add(-38, 99.0, 0.4, "sell")
    # Price is back above the swing, but these are sells, so buy size
    # after the reclaim is zero.
    add(-10, 104.0, 0.2, "sell")
    add(-1, 104.0, 0.2, "sell")

    decision = _decide(prints, _bars(now), [Pool("PDH", 130.0, False)], now=now)
    assert decision.absorb == math.inf
    assert decision.fail_reason is None
    assert decision.armed is True
    assert decision.to_log()["absorb"] is None
    assert 1e6 not in decision.to_log().values()


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
    # No low past the fill. Tick 1 puts the 3-tick buffer inside the
    # 10-tick wick room, so the stop clears that room (99 − 13 = 86).
    decision = _decide(_long_prints(now), bars, [Pool("PDH", 130, False)], tick=1.0)
    assert decision.swing == pytest.approx(100)
    assert decision.armed is True
    assert decision.fail_reason is None
    intent = decision.intent
    assert intent is not None
    assert intent.limit_px == pytest.approx(99)
    assert intent.stop == pytest.approx(86)
    assert intent.stop != pytest.approx(96)
    assert intent.size == pytest.approx(7.692307)  # 5000 * 2% / 13
    assert decision.size_adjust == SIZE_ADJUST_WIDE_STOP

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
    # PDL 80 is the next liquidity, past 2R of the wick stop.
    assert intent.take_profit == pytest.approx(80)
    assert intent.pool_px == pytest.approx(80)
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


def test_tp_is_the_liquidity_level_and_r_is_only_the_fallback():
    # A pool past 2R is the target. Raising the requested R does not move it.
    assert take_profit("long", 99, 98, 130, tp_r=1.5) == pytest.approx(130)
    assert take_profit("long", 99, 98, 130, tp_r=2.5) == pytest.approx(130)
    # A pool inside 1R is still the target.
    assert take_profit("long", 99, 98, 100.2, tp_r=2.0) == pytest.approx(100.2)
    assert take_profit("long", 100, 90, 115, tp_r=4) == pytest.approx(115)
    assert take_profit("long", 100, 90, 140, tp_r=4) == pytest.approx(140)
    # No pool: sub-1R request is lifted to 1R. It is not forced to 2R.
    assert take_profit("long", 100, 90, None, tp_r=0.2) == pytest.approx(110)
    assert take_profit("long", 100, 90, None, tp_r=1.5) == pytest.approx(115)
    # Short mirror: a far pool is the pool. A close pool is the pool.
    assert take_profit("short", 101, 102, 80, tp_r=1.5) == pytest.approx(80)
    assert take_profit("short", 101, 102, 100.2, tp_r=1.5) == pytest.approx(100.2)
    # Nearest level wins. A far pool does not outrank a closer swing.
    assert next_liquidity("long", 100.0, 0.01, [130.0, 110.0]) == pytest.approx(110)
    assert next_liquidity("short", 85658.0, 1.0, [85500.0, 85640.0]) == pytest.approx(85640)
    # One tick off the fill is noise.
    assert next_liquidity("long", 100.0, 0.01, [100.01, 110.0]) == pytest.approx(110)
    # 1R of a 180-point stop skips both of those prints.
    assert next_liquidity(
        "short", 85658.0, 1.0, [85500.0, 85640.0], stop=85838.0
    ) is None
    # Oct 6: 85814 is ~0.03R of a 147-point stop. PDL 85273 clears 1R.
    assert min_tp_distance(85818.0, 85965.0) == pytest.approx(147.0)
    assert next_liquidity(
        "short", 85818.0, 1.0, [85814.0, 85273.0], stop=85965.0
    ) == pytest.approx(85273.0)
    # Fee can be the floor when 1R is shorter than the round trip.
    assert min_tp_distance(100.0, 100.05) == pytest.approx(100.0 * 0.0006)
    assert next_liquidity(
        "long", 100.0, 0.01, [100.055, 100.2], stop=100.05
    ) == pytest.approx(100.2)


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
    """One tick past a LIT Alo collides with the fill. That stop still fails closed.

    A sweep sitting on the Alo, with no wick past the old 0.15% floor, is
    sized from the structural buffer. It is not parked on the fill and it
    is not scrapped.
    """
    tick = 0.0001
    sweep = 3.9046
    limit = alo_limit("short", sweep, sweep, sweep + tick, tick)
    assert limit == pytest.approx(3.9047)
    naive = stop_beyond_extreme("short", sweep, tick)
    assert naive == pytest.approx(limit)
    assert collides_with_fill(limit, naive, tick)
    assert collides_with_fill(limit, limit, tick)
    assert stop_clears_fees(limit, limit + tick, tick=tick) is False
    # No high past the fill. The few-tick buffer is inside wick room.
    tight = place_stop("short", sweep, limit, tick)
    assert tight == pytest.approx(3.9114)
    assert tight != pytest.approx(3.9055)
    assert tight != pytest.approx(limit)
    assert not collides_with_fill(limit, tight, tick)
    # A fill-only recompute must not invent that room.
    assert place_stop("short", sweep, limit, tick, clear_wick_room=False) == pytest.approx(3.9055)
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


def test_eth_floor_only_stop_sizes_from_structure_and_a_deeper_wick_does():
    """Oct 5 ~21:46 ET ETH long: Alo/sweep 2705.8, swing 2706.9.

    2701.7 is the 0.15% floor (41 ticks). Price later traded ~2702.7, a hunt
    into that floor. A real wick inside the room still owns the stop and is
    not lifted onto 2701.7. With no wick past the sweep the few-tick buffer
    is inside that room, so the stop clears the room (near 2701.2), past
    the old floor snap, and is not parked on it. A 1m low under the floor
    puts the stop beyond that low. A wick past 1.5% of price still arms,
    at a smaller size.
    """
    tick = 0.1
    entry = 2705.8
    floor_px = entry - floor_distance(entry, tick)
    assert math.floor(floor_px / tick + 1e-9) * tick == pytest.approx(2701.7)
    # Sweep on the Alo, swing above the fill, no deeper wick. Clear the room.
    tight = place_stop("long", entry, entry, tick, swing=2706.9)
    assert tight == pytest.approx(2701.2)
    assert tight != pytest.approx(2705.2)
    assert tight != pytest.approx(2701.7)
    assert tight < 2701.7
    assert tight < entry
    # A shallow wick still above the floor must not be lifted onto 2701.7.
    shallow = place_stop("long", entry, entry, tick, swing=2706.9, local_extreme=2704.0)
    assert shallow == pytest.approx(2703.4)
    assert shallow < 2704.0
    assert shallow != pytest.approx(2701.7)
    # 0.5×ATR inside the room is still wick room. The stop clears the room.
    small_atr = place_stop("long", entry, entry, tick, atr=4.0)
    assert small_atr == pytest.approx(2701.2)
    assert small_atr != pytest.approx(2703.8)
    # A low outside the room is the opposing print. One inside it is not.
    past_low = place_stop("long", entry, entry, tick, further=[2690.0])
    assert past_low == pytest.approx(2689.4)
    assert past_low < 2690.0
    inside = place_stop("long", entry, entry, tick, further=[2704.0])
    assert inside == pytest.approx(2701.2)
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
    assert size_adjust_tag(entry, stop) is None
    size, dollar = size_from_stop(8000.0, entry, stop, risk_pct=0.02)
    assert dollar == pytest.approx(160.0)
    assert size == pytest.approx(19.047619)
    assert size == pytest.approx(math.floor((8000.0 * 0.02) / dist * 1_000_000) / 1_000_000)
    # Room past a much deeper low exceeds 1.5%. Keep the wick. Size down.
    wide = place_stop("long", entry, entry, tick, local_extreme=2660.0)
    assert wide == pytest.approx(2659.4)
    assert wide < entry * (1.0 - 0.015)
    assert size_adjust_tag(entry, wide) == SIZE_ADJUST_WIDE_STOP
    wide_size, wide_dollar = size_from_stop(8000.0, entry, wide, risk_pct=0.02)
    assert wide_dollar == pytest.approx(160.0)
    assert wide_size == pytest.approx(3.448275)
    assert wide_size < size

    now = _now()
    prints = _long_prints(now, sweep_px=entry, last_price=2709.0, final_price=2709.0)
    prints = [replace(p, price=2709.0) if p.price == 104.0 else p for p in prints]
    floor_bars = _eth_bars(now, wick=2708.0, swing=2706.9)
    tight_arm = _decide(
        prints,
        floor_bars,
        [Pool("PDH", 2800.0, False)],
        bid=2708.9,
        ask=2709.1,
        tick=tick,
        equity=8000.0,
    )
    assert tight_arm.armed is True
    assert tight_arm.fail_reason is None
    assert tight_arm.sweep_price == pytest.approx(entry)
    assert tight_arm.size_adjust is None
    assert local_bar_extreme(floor_bars, side="long", now=now) == pytest.approx(2706.9)
    tight_intent = tight_arm.intent
    assert tight_intent is not None
    assert tight_intent.limit_px == pytest.approx(entry)
    assert tight_intent.stop == pytest.approx(2701.2)
    assert tight_intent.stop != pytest.approx(2705.2)
    assert tight_intent.stop < 2701.7
    sized_tight, _dollar = size_from_stop(
        8000.0, tight_intent.limit_px, tight_intent.stop, risk_pct=0.02
    )
    assert tight_intent.size == pytest.approx(sized_tight)
    assert tight_intent.size == pytest.approx(34.782608)
    # PDH 2800 is the next liquidity, past 2R of this stop.
    assert tight_intent.take_profit == pytest.approx(2800.0)

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
    assert intent.take_profit == pytest.approx(2800.0)
    assert intent.size == pytest.approx(19.047619)
    assert armed.size_adjust is None
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
    assert capped.armed is True
    assert capped.fail_reason is None
    assert capped.size_adjust == SIZE_ADJUST_WIDE_STOP
    assert capped.to_log()["size_adjust"] == "wide_stop"
    wide_intent = capped.intent
    assert wide_intent is not None
    assert wide_intent.stop == pytest.approx(2659.4)
    assert wide_intent.size == pytest.approx(3.448275)
    assert wide_intent.size < intent.size
    assert wide_intent.take_profit == pytest.approx(2800.0)


def _btc_1026_prints(now: float, *, sweep_px: float = 86054.0) -> list[TradePrint]:
    """Oct 6 10:26 ET shape: BTC long swept 86054 and reclaimed, absorb well above 1.3."""
    prints: list[TradePrint] = []
    seq = 0

    def add(offset: float, price: float, size: float, side: str) -> None:
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="BTC", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, 86120.0, 20.0, "buy")
    # Swept sell size 318. Reclaim buys sum to 1, so absorb is 318.
    # Prefix buys keep the window delta non-negative.
    add(-40, sweep_px, 159.0, "sell")
    add(-38, sweep_px, 159.0, "sell")
    for i in range(8):
        add(-30 + i * 0.2, 86120.0, 0.1, "buy")
    add(-10, 86120.0, 0.1, "buy")
    add(-1, 86120.0, 0.1, "buy")
    return prints


def _btc_1026_bars(now: float, *, wick: float, swing: float = 86080.0) -> list[dict]:
    """Swing low above the sweep. ``wick`` is the last bar and is not a fractal."""
    t0 = now - 400
    return [
        {"t": t0, "o": 86200, "h": 86220, "l": 86150, "c": 86180, "v": 1},
        {"t": t0 + 60, "o": 86180, "h": 86190, "l": swing, "c": 86140, "v": 1},
        {"t": t0 + 120, "o": 86140, "h": 86170, "l": 86110, "c": 86150, "v": 1},
        {"t": t0 + 180, "o": 86150, "h": 86180, "l": wick, "c": 86160, "v": 1},
    ]


def test_btc_1026_sweep_arms_from_structural_stop_instead_of_bad_stop():
    """Oct 6 10:26 ET BTC long: sweep 86054, absorb passed, stop past the wick.

    The structural stop is inside the old 0.15% floor (~$129), which used
    to log BAD_STOP after the flow gates. It now arms. Size is 2% of spot
    USDC over that distance. A wick that pushes the stop past 1.5% also
    arms, smaller, with size_adjust=wide_stop.
    """
    now = _now()
    tick = 1.0
    entry = 86054.0
    equity = 10_000.0
    prints = _btc_1026_prints(now)
    # Wick under the sweep by less than the old 0.15% floor (~$129) and
    # by more than 0.1%, so 2% / distance fits inside the 20× notional cap.
    tight = _decide(
        prints,
        _btc_1026_bars(now, wick=85972.0),
        [Pool("PDH", 90000.0, False)],
        bid=86119.0,
        ask=86121.0,
        tick=tick,
        equity=equity,
    )
    assert tight.armed is True
    assert tight.fail_reason is None
    assert tight.sweep_price == pytest.approx(entry)
    assert tight.absorb == pytest.approx(318)
    assert tight.size_adjust is None
    intent = tight.intent
    assert intent is not None
    assert intent.side == "long"
    assert intent.tif == "Alo"
    assert intent.market_fallback is False
    assert intent.limit_px == pytest.approx(entry)
    assert intent.stop == pytest.approx(85954)
    assert intent.stop < 85972.0
    dist = entry - intent.stop
    assert dist == pytest.approx(100)
    assert dist < floor_distance(entry, tick)
    assert intent.size == pytest.approx(math.floor((equity * 0.02) / dist * 1_000_000) / 1_000_000)
    assert intent.size == pytest.approx(2.0)
    # PDH 90000 is the next liquidity, past 2R of this wick.
    assert intent.take_profit == pytest.approx(90000.0)
    assert intent.pool_px == pytest.approx(90000.0)
    sized, dollar = size_from_stop(equity, intent.limit_px, intent.stop, risk_pct=0.02)
    assert intent.size == pytest.approx(sized)
    assert dollar == pytest.approx(200.0)

    wide = _decide(
        prints,
        _btc_1026_bars(now, wick=84700.0),
        [Pool("PDH", 90000.0, False)],
        bid=86119.0,
        ask=86121.0,
        tick=tick,
        equity=equity,
    )
    assert wide.armed is True
    assert wide.fail_reason is None
    assert wide.size_adjust == "wide_stop"
    assert wide.to_log()["size_adjust"] == "wide_stop"
    wide_intent = wide.intent
    assert wide_intent is not None
    assert wide_intent.limit_px == pytest.approx(entry)
    assert wide_intent.stop == pytest.approx(84682)
    assert (entry - wide_intent.stop) / entry > 0.015
    assert wide_intent.size < intent.size
    assert wide_intent.size == pytest.approx(0.145772)
    assert wide_intent.take_profit == pytest.approx(90000.0)
    # Not pulled up to the 1.5% level to fit the old cap.
    assert wide_intent.stop < entry * (1.0 - 0.015)


def test_btc_short_stops_past_opposing_liquidity_and_targets_the_pool():
    """Oct 6 BTC short: Alo 85658, stop past 85820, not the old 18-point buffer.

    PDL 85500 is inside 1R of the 180-point stop, so it is skipped. The
    next pool that clears 1R (85273) is the target, not 2R of the old
    18-point stop. Size is 2% of spot USDC over the new distance.
    """
    now = _now()
    entry = 85658.0
    tick = 1.0
    last = 85620.0
    t0 = now - 500
    bars = [
        {"t": t0, "o": 85700, "h": 85700, "l": 85680, "c": 85690, "v": 1},
        {"t": t0 + 60, "o": 85690, "h": 85820, "l": 85660, "c": 85700, "v": 1},
        {"t": t0 + 120, "o": 85700, "h": 85700, "l": 85640, "c": 85680, "v": 1},
        {"t": t0 + 180, "o": 85680, "h": 85610, "l": 85620, "c": 85630, "v": 1},
        {"t": t0 + 240, "o": 85630, "h": 85630, "l": 85600, "c": 85610, "v": 1},
        {"t": t0 + 300, "o": 85610, "h": 85600, "l": 85580, "c": 85590, "v": 1},
    ]
    prints: list[TradePrint] = []
    seq = 0

    def add(offset, price, size, side):
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="BTC", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, last, 0.5, "sell")
    add(-40, entry, 7.5, "buy")
    add(-38, entry, 7.5, "buy")
    for i in range(8):
        add(-30 + i * 0.2, last, 1.0, "sell")
    add(-10, last, 0.5, "sell")
    add(-1, last, 0.5, "sell")

    equity = 5000.0
    decision = _decide(
        prints,
        bars,
        [Pool("PDL", 85500.0, taken=False), Pool("PDL", 85273.0, taken=False)],
        coin="BTC",
        bid=last - 1.0,
        ask=last + 1.0,
        tick=tick,
        equity=equity,
    )
    assert decision.armed is True
    assert decision.fail_reason is None
    assert decision.bias == "short"
    intent = decision.intent
    assert intent is not None
    assert intent.limit_px == pytest.approx(entry)
    assert intent.stop == pytest.approx(85838)
    assert intent.stop > 85820
    assert intent.stop != pytest.approx(85676)
    assert intent.take_profit == pytest.approx(85273)
    assert intent.take_profit != pytest.approx(85500)
    assert intent.take_profit != pytest.approx(85622)
    assert intent.pool_px == pytest.approx(85273)
    dist = intent.stop - intent.limit_px
    assert intent.size == pytest.approx(
        math.floor((equity * 0.02) / dist * 1_000_000) / 1_000_000
    )
    assert intent.size == pytest.approx(0.555555)
    sized, dollar = size_from_stop(equity, intent.limit_px, intent.stop, risk_pct=0.02)
    assert intent.size == pytest.approx(sized)
    assert dollar == pytest.approx(100.0)

    # 85500 alone is inside 1R, so the arm falls back to 1.5R of the stop.
    near_only = _decide(
        prints,
        bars,
        [Pool("PDL", 85500.0, taken=False)],
        coin="BTC",
        bid=last - 1.0,
        ask=last + 1.0,
        tick=tick,
        equity=equity,
    )
    assert near_only.armed is True
    near = near_only.intent
    assert near is not None
    assert near.pool_px is None
    near_dist = near.stop - near.limit_px
    assert near.take_profit == pytest.approx(near.limit_px - 1.5 * near_dist)
    assert near.take_profit != pytest.approx(85500)


def test_fallback_tp_fails_closed_when_it_cannot_clear_the_band(monkeypatch):
    """1.5R that still sits inside the fee / min-R floor is BAD_TP."""

    monkeypatch.setattr(
        "hl_bot.strategy.model_b.engine.min_tp_distance",
        lambda entry, stop, min_r=1.0: 1e9,
    )
    failed = _pass_case()
    assert failed.armed is False
    assert failed.intent is None
    assert failed.fail_reason == "BAD_TP"


def test_drip_keeps_liquidity_tp_instead_of_writing_2r_back():
    """A 2R price on the order must not replace the pool, on the open or a drip.

    Live did this: the pool was 85500, the stored target was 2R at 85622,
    and every partial fill cancelled the pool trigger and put 85622 back.
    """
    entry = 85658.0
    two_r = 85622.0
    pool = 85500.0
    stop = 85838.0
    assert locked_take_profit("short", entry, two_r, pool, stop) == pytest.approx(pool)
    assert locked_take_profit("short", entry, two_r, pool, stop) != pytest.approx(two_r)

    now = _now()
    book = ThesisBook()
    intent = AloIntent(
        coin="BTC",
        side="short",
        limit_px=entry,
        size=1.0,
        stop=stop,
        take_profit=two_r,
        swing_id="BTC:high:1",
        sweep_px=entry,
        tick=1.0,
        pool_px=pool,
        tp_r=1.5,
    )
    book.post(intent, now, oid=7)
    opened = book.apply_user_fill(
        coin="BTC", oid=7, price=entry, ts=now + 1, crossed=False, size=0.2
    )
    assert opened is not None
    assert opened.take_profit == pytest.approx(pool)
    assert opened.stop == pytest.approx(stop)
    again = book.apply_user_fill(
        coin="BTC", oid=7, price=entry, ts=now + 2, crossed=False, size=0.3
    )
    assert again is opened
    assert again.just_opened is False
    assert again.take_profit == pytest.approx(pool)
    assert again.take_profit != pytest.approx(two_r)
    assert again.size == pytest.approx(0.5)
    assert book.position("BTC").take_profit == pytest.approx(pool)


def test_drip_bracket_keeps_pool_and_an_amend_error_does_not_stop_the_hunt(tmp_path):
    """Two partials both place the pool. A TP amend error does not end the loop."""
    now = _now()
    entry = 85658.0
    last = 85620.0
    t0 = now - 500
    bars = [
        {"t": t0, "o": 85700, "h": 85700, "l": 85680, "c": 85690, "v": 1},
        {"t": t0 + 60, "o": 85690, "h": 85820, "l": 85660, "c": 85700, "v": 1},
        {"t": t0 + 120, "o": 85700, "h": 85700, "l": 85640, "c": 85680, "v": 1},
        {"t": t0 + 180, "o": 85680, "h": 85610, "l": 85620, "c": 85630, "v": 1},
        {"t": t0 + 240, "o": 85630, "h": 85630, "l": 85600, "c": 85610, "v": 1},
        {"t": t0 + 300, "o": 85610, "h": 85600, "l": 85580, "c": 85590, "v": 1},
    ]
    prints: list[TradePrint] = []
    seq = 0

    def add(offset, price, size, side):
        nonlocal seq
        prints.append(
            TradePrint(ts=now + offset, coin="BTC", price=price, size=size, side=side, seq=seq)
        )
        seq += 1

    for i in range(20):
        add(-55 + i * 0.3, last, 0.5, "sell")
    add(-40, entry, 7.5, "buy")
    add(-38, entry, 7.5, "buy")
    for i in range(8):
        add(-30 + i * 0.2, last, 1.0, "sell")
    add(-10, last, 0.5, "sell")
    add(-1, last, 0.5, "sell")

    info = InfoClient()
    info.inject_bars(bars, coin="BTC")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(prints, bbo={"BTC": (last - 1.0, last + 1.0)})

    class FakeLive:
        def __init__(self):
            self.tps = []
            self.stops = []
            self._oid = 400

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            return {"response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}}

        def cancel_order(self, coin, oid):
            return None

        def set_stop_loss(self, coin, is_buy, size, trigger_px):
            self._oid += 1
            self.stops.append(trigger_px)
            return {"response": {"data": {"statuses": [{"resting": {"oid": self._oid}}]}}}

        def set_take_profit(self, coin, is_buy, size, trigger_px):
            self.tps.append(trigger_px)
            if len(self.tps) > 1:
                raise RuntimeError("tp amend failed")
            self._oid += 1
            return {"response": {"data": {"statuses": [{"resting": {"oid": self._oid}}]}}}

        def market_close(self, *args, **kwargs):
            raise AssertionError("market close")

    fake = FakeLive()
    sleeps = {"n": 0}

    def sleep_fn(_sec):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            feed._fills.append(UserFill("BTC", 11, entry, 0.1, now + 1, False))
        elif sleeps["n"] == 2:
            feed._fills.append(UserFill("BTC", 11, entry, 0.1, now + 2, False))

    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            trading_mode="live",
            i_understand_live_trading=True,
            private_key="0x" + "22" * 32,
            network="testnet",
            journal_path=str(tmp_path / "drip-tp.jsonl"),
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
        pools_for=lambda *a, **k: [Pool("PDL", 85273.0, False)],
        tick_for=lambda coin: 1.0,
    )
    assert summary["halted"] is False
    assert summary["arms"] == 1
    assert summary["opens"] == 1
    assert fake.tps == [pytest.approx(85273.0), pytest.approx(85273.0)]
    assert 85622.0 not in fake.tps
    assert 85500.0 not in fake.tps
    rows = TradeJournal(tmp_path / "drip-tp.jsonl").read_all()
    opened = [r for r in rows if r["event"] == "open"]
    assert opened and opened[0]["tp"] == pytest.approx(85273.0)


def test_stop_equal_to_entry_fails_closed(monkeypatch):
    """Flow can pass and the idea still dies when the stop is not past the fill."""

    def on_fill(side, extreme, entry, tick, tp_r=1.5, **kwargs):
        del side, extreme, tick, tp_r, kwargs
        return entry

    monkeypatch.setattr("hl_bot.strategy.model_b.engine.place_stop", on_fill)
    same = _pass_case()
    assert same.armed is False
    assert same.intent is None
    assert same.fail_reason == "BAD_STOP"
    assert same.absorb is not None and same.absorb >= ABSORB_MIN

    def one_tick(side, extreme, entry, tick, tp_r=1.5, **kwargs):
        del side, extreme, tp_r, kwargs
        return entry - tick

    monkeypatch.setattr("hl_bot.strategy.model_b.engine.place_stop", one_tick)
    collided = _pass_case()
    assert collided.armed is False
    assert collided.fail_reason == "BAD_STOP"

    def wrong_side(side, extreme, entry, tick, tp_r=1.5, **kwargs):
        del side, extreme, tick, tp_r, kwargs
        return entry + 5

    monkeypatch.setattr("hl_bot.strategy.model_b.engine.place_stop", wrong_side)
    flipped = _pass_case()
    assert flipped.armed is False
    assert flipped.fail_reason == "BAD_STOP"


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


def test_close_releases_pending_margin_without_waiting_for_thesis_stale():
    """A flat, a stop, and a TP each drop the position and the resting remainder.

    The remainder used to stay until thesis_stale. Margin for the next
    coin has to be free on the close itself.
    """
    now = _now()
    decision = _decide(_long_prints(now), _bars(now), [Pool("PDH", 130, False)])
    intent = decision.intent
    assert intent is not None
    assert intent.stop < intent.limit_px < intent.take_profit

    book = ThesisBook()
    book.post(intent, now, oid=7)
    opened = book.apply_user_fill(
        coin="BTC",
        oid=7,
        price=intent.limit_px,
        ts=now + 1,
        crossed=False,
        size=intent.size * 0.4,
    )
    assert opened is not None
    assert book.working("BTC") is not None
    assert _margin_in_use(book) > 0
    # Between stop and TP. try_exit does not see this as a close.
    assert book.try_exit("BTC", intent.limit_px) is None
    closed = book.apply_user_fill(
        coin="BTC",
        oid=99,
        price=intent.limit_px,
        ts=now + 2,
        crossed=True,
        size=opened.size,
    )
    assert closed is not None
    assert closed.reason == "flat"
    assert closed.remainder is not None
    assert book.position("BTC") is None
    assert book.working("BTC") is None
    assert book.cancel_if_stale("BTC", _long_prints(now)) == []
    assert _margin_in_use(book) == 0
    assert ticket_fits(5000.0, _margin_in_use(book), intent.size, intent.limit_px)

    for price, reason in ((intent.stop, "stop"), (intent.take_profit, "tp")):
        fresh = ThesisBook()
        fresh.post(intent, now, oid=7)
        fresh.apply_user_fill(
            coin="BTC",
            oid=7,
            price=intent.limit_px,
            ts=now + 1,
            crossed=False,
            size=intent.size * 0.4,
        )
        hit = fresh.try_exit("BTC", price)
        assert hit is not None and hit.reason == reason
        assert hit.remainder is not None
        assert fresh.position("BTC") is None
        assert fresh.working("BTC") is None
        assert _margin_in_use(fresh) == 0


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
    assert normalize_coin("XYZ:gold") == "xyz:GOLD"
    assert normalize_coin("xyz:XYZ100") == "xyz:XYZ100"
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
    assert settings.model_b_closer_score_guard is True
    assert settings.model_b_close_margin_reserve == pytest.approx(0.60)
    assert settings.model_b_delta_flat_usdc == pytest.approx(100)
    assert settings.model_b_delta_flat_eps == pytest.approx(0.05)
    monkeypatch.setenv("MODEL_B_DELTA_FLAT_USDC", "50")
    assert load_settings().model_b_delta_flat_usdc == pytest.approx(50)
    monkeypatch.setenv("MODEL_B_DELTA_FLAT_USDC", "-1")
    with pytest.raises(ValueError, match="MODEL_B_DELTA_FLAT_USDC"):
        load_settings()
    monkeypatch.setenv("MODEL_B_DELTA_FLAT_USDC", "100")
    monkeypatch.setenv("MODEL_B_DELTA_FLAT_EPS", "0")
    assert load_settings().model_b_delta_flat_eps == pytest.approx(0.0)
    monkeypatch.setenv("MODEL_B_DELTA_FLAT_EPS", "-0.01")
    with pytest.raises(ValueError, match="MODEL_B_DELTA_FLAT_EPS"):
        load_settings()
    monkeypatch.delenv("MODEL_B_DELTA_FLAT_EPS")
    monkeypatch.setenv("MODEL_B_ALO_TIMEOUT_SEC", "15")
    assert load_settings().model_b_alo_timeout_sec == pytest.approx(15)
    monkeypatch.delenv("MODEL_B_ALO_TIMEOUT_SEC")
    monkeypatch.setenv("MODEL_B_CLOSER_SCORE_GUARD", "0")
    assert load_settings().model_b_closer_score_guard is False
    monkeypatch.delenv("MODEL_B_CLOSER_SCORE_GUARD")
    assert load_settings().model_b_closer_score_guard is True
    monkeypatch.setenv("MODEL_B_CLOSE_MARGIN_RESERVE", "0")
    assert load_settings().model_b_close_margin_reserve == pytest.approx(0.0)
    monkeypatch.setenv("MODEL_B_CLOSE_MARGIN_RESERVE", "0.25")
    assert load_settings().model_b_close_margin_reserve == pytest.approx(0.25)
    monkeypatch.setenv("MODEL_B_CLOSE_MARGIN_RESERVE", "1.5")
    with pytest.raises(ValueError, match="MODEL_B_CLOSE_MARGIN_RESERVE"):
        load_settings()
    monkeypatch.setenv("MODEL_B_CLOSE_MARGIN_RESERVE", "-0.1")
    with pytest.raises(ValueError, match="MODEL_B_CLOSE_MARGIN_RESERVE"):
        load_settings()
    monkeypatch.delenv("MODEL_B_CLOSE_MARGIN_RESERVE")
    assert load_settings().model_b_close_margin_reserve == pytest.approx(0.60)

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


def _live_settings(tmp_path, name: str) -> Settings:
    return Settings(
        entry_mode="model_b",
        risk_per_trade=0.02,
        trading_mode="live",
        i_understand_live_trading=True,
        private_key="0x" + "ab" * 32,
        network="testnet",
        journal_path=str(tmp_path / name),
        loop_interval_sec=0,
    )


def test_margin_reject_drops_the_ticket_and_does_not_block_the_next_coin(tmp_path):
    """An insufficient-margin Alo is not a resting ticket.

    BTC's reject must not sit in the book until thesis_stale. ETH in the
    same pass still arms, and the next pass is another reject, not SECOND_ALO.
    """
    now = _now()
    btc = _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0)
    eth = _retag(
        _long_prints(now, last_price=120.0, final_price=120.0, sweep_px=99.0),
        "ETH",
    )
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_bars(_bars(now), coin="ETH")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(
        btc + eth,
        bbo={"BTC": (102.0, 104.0), "ETH": (119.0, 121.0)},
    )

    class FakeLive:
        def __init__(self):
            self.alos = []

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            self.alos.append(coin)
            if coin == "BTC":
                return {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {
                            "statuses": [
                                {"error": "Insufficient margin to place order. asset=0"}
                            ]
                        },
                    },
                }
            return {"response": {"data": {"statuses": [{"resting": {"oid": 21}}]}}}

        def cancel_order(self, coin, oid):
            return None

    summary = run_model_b(
        _live_settings(tmp_path, "reject.jsonl"),
        max_iterations=2,
        info=info,
        feed=feed,
        exchange=FakeLive(),
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC", "ETH"),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    rows = TradeJournal(tmp_path / "reject.jsonl").read_all()
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert [r["coin"] for r in arms] == ["ETH"]
    assert summary["arms"] == 1
    rejects = [
        r
        for r in rows
        if r["event"] == "model_b_fail" and r.get("fail_reason") == "MARGIN_REJECT"
    ]
    assert len(rejects) == 2
    assert {r["coin"] for r in rejects} == {"BTC"}
    assert all(r.get("armed") is False for r in rejects)
    drops = [
        r
        for r in rows
        if r["event"] == "model_b_margin" and r.get("action") == "drop"
    ]
    assert len(drops) == 2
    assert all(r["reason"] == "MARGIN_REJECT" for r in drops)
    assert not any(r.get("fail_reason") == "SECOND_ALO" and r.get("coin") == "BTC" for r in rows)
    assert not any(r.get("reason") == "thesis_stale" for r in rows)


def test_send_fail_without_an_oid_does_not_post(tmp_path):
    """A response that never rests is SEND_FAIL, not a phantom working order."""
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)})

    class FakeLive:
        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            return {"status": "ok", "response": {"data": {"statuses": [{}]}}}

    summary = run_model_b(
        _live_settings(tmp_path, "send-fail.jsonl"),
        max_iterations=1,
        info=info,
        feed=feed,
        exchange=FakeLive(),
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["arms"] == 0
    rows = TradeJournal(tmp_path / "send-fail.jsonl").read_all()
    fails = [r for r in rows if r["event"] == "model_b_fail"]
    assert len(fails) == 1
    assert fails[0]["fail_reason"] == "SEND_FAIL"
    assert fails[0]["coin"] == "BTC"
    assert not any(r["event"] == "model_b_arm" for r in rows)


def test_flat_close_frees_margin_for_the_other_coin(tmp_path):
    """BTC's position and remainder drop on a between-band fill, then ETH arms.

    Tight stops use more than half of 5000, so ETH cannot rest beside BTC.
    The flat must free that margin in the same pass. thesis_stale is not
    the release.
    """
    now = _now()
    btc = _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0)
    eth = _retag(
        _long_prints(now, last_price=120.0, final_price=120.0, sweep_px=99.0),
        "ETH",
    )
    info = InfoClient()
    bars = _tight_bars(now)
    info.inject_bars(bars, coin="BTC")
    info.inject_bars(bars, coin="ETH")
    info.inject_spot_usdc(5000.0)
    feed = MemoryFeed(
        btc + eth,
        bbo={"BTC": (102.0, 104.0), "ETH": (119.0, 121.0)},
    )

    class FakeLive:
        def __init__(self):
            self.alos = []
            self.cancels = []

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            self.alos.append((coin, limit_px, size))
            return {
                "response": {
                    "data": {"statuses": [{"resting": {"oid": 11 if coin == "BTC" else 22}}]}
                }
            }

        def cancel_order(self, coin, oid):
            self.cancels.append((coin, oid))

        def set_stop_loss(self, coin, is_buy, size, trigger_px):
            return {"response": {"data": {"statuses": [{"resting": {"oid": 31}}]}}}

        def set_take_profit(self, coin, is_buy, size, trigger_px):
            return {"response": {"data": {"statuses": [{"resting": {"oid": 32}}]}}}

    fake = FakeLive()
    sleeps = {"n": 0}

    def sleep_fn(_sec):
        sleeps["n"] += 1
        if sleeps["n"] == 1 and fake.alos:
            px = fake.alos[0][1]
            feed._fills.append(UserFill("BTC", 11, px, 0.01, now + 1, False))
        elif sleeps["n"] == 2 and fake.alos:
            px = fake.alos[0][1]
            feed._fills.append(UserFill("BTC", 77, px, 0.01, now + 2, True))

    summary = run_model_b(
        _live_settings(tmp_path, "flat.jsonl"),
        max_iterations=3,
        info=info,
        feed=feed,
        exchange=fake,
        sleep_fn=sleep_fn,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC", "ETH"),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    rows = TradeJournal(tmp_path / "flat.jsonl").read_all()
    closes = [r for r in rows if r["event"] == "close"]
    assert len(closes) == 1
    assert closes[0]["symbol"] == "BTC"
    assert closes[0]["reason"] == "flat"
    releases = [
        r
        for r in rows
        if r["event"] == "model_b_margin" and r.get("action") == "release"
    ]
    assert len(releases) == 1
    assert releases[0]["coin"] == "BTC"
    assert releases[0]["reason"] == "flat"
    cancels = [r for r in rows if r["event"] == "model_b_cancel" and r["coin"] == "BTC"]
    assert len(cancels) == 1
    assert cancels[0]["reason"] == "flat"
    assert cancels[0]["remainder"] == "cancelled"
    assert not any(r.get("reason") == "thesis_stale" for r in rows)
    arms = [r["coin"] for r in rows if r["event"] == "model_b_arm"]
    assert arms == ["BTC", "ETH"]
    assert summary["arms"] == 2
    assert summary["closes"] == 1
    assert ("BTC", 11) in fake.cancels


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


def test_resting_score_blocks_only_a_strictly_higher_ticket():
    """Equal scores do not block. Closer-bps still decides that swap."""
    assert resting_score_blocks_closer_cancel(9, 4) is True
    assert resting_score_blocks_closer_cancel(5, 9) is False
    assert resting_score_blocks_closer_cancel(4, 4) is False
    assert resting_score_blocks_closer_cancel(None, 9) is False
    assert resting_score_blocks_closer_cancel(9, None) is False


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


def _two_coin_hunt(
    tmp_path,
    *,
    btc_last: float,
    eth_last: float,
    iterations: int = 1,
    tight_stop: bool = False,
    btc_prefix: int = 20,
    eth_prefix: int = 20,
    btc_step: float = 0.3,
    eth_step: float = 0.3,
    closer_score_guard: bool | None = None,
):
    now = _now()
    btc = _long_prints(
        now,
        last_price=btc_last,
        final_price=btc_last,
        sweep_px=99.0,
        n_prefix=btc_prefix,
        prefix_step=btc_step,
    )
    eth = _retag(
        _long_prints(
            now,
            last_price=eth_last,
            final_price=eth_last,
            sweep_px=99.0,
            n_prefix=eth_prefix,
            prefix_step=eth_step,
        ),
        "ETH",
    )
    info = InfoClient()
    bars = _tight_bars(now) if tight_stop else _bars(now)
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
    settings_kw: dict = dict(
        entry_mode="model_b",
        risk_per_trade=0.02,
        journal_path=str(journal),
        loop_interval_sec=0,
    )
    if closer_score_guard is not None:
        settings_kw["model_b_closer_score_guard"] = closer_score_guard
    summary = run_model_b(
        Settings(**settings_kw),
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


def test_closer_ticker_cancels_the_far_alo_and_places_the_near_one(tmp_path, caplog):
    """Room-cleared stops still use more than half the balance, so ETH cancels BTC."""
    with caplog.at_level(logging.INFO):
        summary, rows = _two_coin_hunt(
            tmp_path, btc_last=120.0, eth_last=103.0, iterations=2, tight_stop=True
        )
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
    assert any("MODEL_B MARGIN" in rec.message and "closer_cancel" in rec.message for rec in caplog.records)
    assert not any("dual_rest" in rec.message for rec in caplog.records)


def test_closer_ticker_keeps_a_higher_score_alo(tmp_path, caplog):
    """The Oct 7 case: score-9 SOL must not die for a closer score-4 XYZ100.

    BTC stands in for the resting ticket (far, score 9). ETH is closer in
    bps and scores 4. Free margin cannot dual-rest. The guard keeps BTC.
    """
    # 78 prefix prints + 12 tape prints = 90 → score 9. Step 0.1 stays
    # before the sweep. 28 + 12 = 40 → score 4.
    with caplog.at_level(logging.INFO):
        summary, rows = _two_coin_hunt(
            tmp_path,
            btc_last=120.0,
            eth_last=103.0,
            tight_stop=True,
            btc_prefix=78,
            btc_step=0.1,
            eth_prefix=28,
        )
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert [r["coin"] for r in arms] == ["BTC"]
    assert arms[0]["score"] == 9
    skips = [r for r in rows if r.get("fail_reason") == "CLOSER_SKIP_LOWER_SCORE"]
    assert len(skips) == 1
    assert skips[0]["coin"] == "ETH"
    assert skips[0]["held_by"] == "BTC"
    assert skips[0]["resting_score"] == 9
    assert skips[0]["candidate_score"] == 4
    assert skips[0]["score"] == 4
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    assert any(
        "CLOSER_SKIP_LOWER_SCORE" in rec.message
        and "held_by=BTC" in rec.message
        and "resting_score=9" in rec.message
        and "candidate_score=4" in rec.message
        for rec in caplog.records
    )


def test_closer_ticker_cancels_when_the_candidate_scores_higher(tmp_path, caplog):
    """Resting 5 vs a closer 9 still swaps. The guard only protects a higher score."""
    # 38 + 12 = 50 → score 5. 78 + 12 = 90 → score 9.
    with caplog.at_level(logging.INFO):
        summary, rows = _two_coin_hunt(
            tmp_path,
            btc_last=120.0,
            eth_last=103.0,
            tight_stop=True,
            btc_prefix=38,
            eth_prefix=78,
            eth_step=0.1,
        )
    assert summary["arms"] == 2
    assert summary["cancels"] == 1
    cancels = [r for r in rows if r["event"] == "model_b_cancel"]
    assert len(cancels) == 1
    assert cancels[0]["coin"] == "BTC"
    assert cancels[0]["reason"] == "closer_ticker"
    assert cancels[0]["winner"] == "ETH"
    assert cancels[0]["resting_score"] == 5
    assert cancels[0]["candidate_score"] == 9
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert [r["coin"] for r in arms] == ["BTC", "ETH"]
    assert arms[0]["score"] == 5
    assert arms[1]["score"] == 9
    assert not any(r.get("fail_reason") == "CLOSER_SKIP_LOWER_SCORE" for r in rows)
    assert any("closer_cancel" in rec.message for rec in caplog.records)


def test_equal_scores_still_cancel_the_farther_alo(tmp_path):
    """Equal scores keep today's closer-bps swap. A tie does not prefer the resting ticket.

    Both tapes are 40 prints (score 4). ETH is closer, so BTC is cancelled.
    """
    summary, rows = _two_coin_hunt(
        tmp_path,
        btc_last=120.0,
        eth_last=103.0,
        tight_stop=True,
        btc_prefix=28,
        eth_prefix=28,
    )
    assert summary["cancels"] == 1
    cancels = [r for r in rows if r["event"] == "model_b_cancel"]
    assert cancels[0]["reason"] == "closer_ticker"
    assert cancels[0]["resting_score"] == 4
    assert cancels[0]["candidate_score"] == 4
    assert not any(r.get("fail_reason") == "CLOSER_SKIP_LOWER_SCORE" for r in rows)


def test_closer_score_guard_off_cancels_the_higher_score(tmp_path):
    """MODEL_B_CLOSER_SCORE_GUARD=0 restores the bps-only cancel."""
    summary, rows = _two_coin_hunt(
        tmp_path,
        btc_last=120.0,
        eth_last=103.0,
        tight_stop=True,
        btc_prefix=78,
        btc_step=0.1,
        eth_prefix=28,
        closer_score_guard=False,
    )
    assert summary["cancels"] == 1
    cancels = [r for r in rows if r["event"] == "model_b_cancel"]
    assert cancels[0]["coin"] == "BTC"
    assert cancels[0]["reason"] == "closer_ticker"
    assert cancels[0]["resting_score"] == 9
    assert cancels[0]["candidate_score"] == 4


def test_farther_ticker_does_not_cancel_the_closer_alo(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        summary, rows = _two_coin_hunt(
            tmp_path, btc_last=103.0, eth_last=120.0, tight_stop=True
        )
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    fails = [r for r in rows if r.get("fail_reason") == "NOT_CLOSER"]
    assert len(fails) == 1
    assert fails[0]["coin"] == "ETH"
    assert fails[0]["held_by"] == "BTC"
    assert fails[0]["margin_need"] > fails[0]["free_margin"]
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    assert any("insufficient" in rec.message for rec in caplog.records)
    assert not any("dual_rest" in rec.message for rec in caplog.records)


def test_free_margin_places_the_farther_alo_without_cancel(tmp_path, caplog):
    """A wide stop leaves margin. ETH rests beside BTC even though it is farther."""
    with caplog.at_level(logging.INFO):
        summary, rows = _two_coin_hunt(tmp_path, btc_last=103.0, eth_last=120.0)
    assert summary["arms"] == 2
    assert summary["cancels"] == 0
    assert not any(r.get("fail_reason") == "NOT_CLOSER" for r in rows)
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert [r["coin"] for r in arms] == ["BTC", "ETH"]
    margin = [r for r in rows if r["event"] == "model_b_margin"]
    assert len(margin) == 1
    assert margin[0]["coin"] == "ETH"
    assert margin[0]["action"] == "dual_rest"
    assert margin[0]["held_by"] == "BTC"
    assert margin[0]["free_margin"] >= margin[0]["margin_need"]
    assert any(
        "MODEL_B MARGIN" in rec.message and "dual_rest" in rec.message
        for rec in caplog.records
    )


def test_same_coin_stays_blocked_when_margin_is_free(tmp_path):
    """A second pass does not average down BTC just because ETH was allowed to rest."""
    summary, rows = _two_coin_hunt(
        tmp_path, btc_last=103.0, eth_last=120.0, iterations=2
    )
    assert summary["arms"] == 2
    assert summary["cancels"] == 0
    arms = [r for r in rows if r["event"] == "model_b_arm"]
    assert [r["coin"] for r in arms] == ["BTC", "ETH"]
    assert not any(r.get("fail_reason") in ("SECOND_ALO", "AVERAGE_DOWN") for r in rows)


def _setup(coin, swing, fail, armed=False):
    return SimpleNamespace(coin=coin, swing=swing, fail_reason=fail, armed=armed)


def _waiting_prints(now: float, coin: str, *, n: int = 40, price: float = 104.0, seq0: int = 0):
    """Thick tape that never trades through the swing. That is NO_SWEEP."""
    return [
        TradePrint(
            ts=now - 50 + i * 0.4,
            coin=coin,
            price=price,
            size=0.2,
            side="buy",
            seq=seq0 + i,
        )
        for i in range(n)
    ]


def _hunt(
    coin,
    swing,
    fail,
    *,
    score=0,
    armed=False,
    bias="long",
    bid=103.0,
    ask=105.0,
    last=104.0,
):
    return {
        "decision": SimpleNamespace(
            coin=coin,
            swing=swing,
            fail_reason=fail,
            armed=armed,
            score=score,
            bias=bias,
        ),
        "bid": bid,
        "ask": ask,
        "last": last,
    }


def test_close_reserve_picks_highest_score_then_closer_bps():
    """Preferred close coin: highest score, then closer swing bps, then hunt order.

    The 0.05 coin floor is unchanged. A one-coin pass does not reserve.
    """
    assert DELTA_FLAT_EPS == pytest.approx(0.05)
    assert DELTA_FLAT_USDC == pytest.approx(100)
    assert CLOSE_MARGIN_RESERVE == pytest.approx(0.60)
    assert other_margin_cap(5000) == pytest.approx(2000)
    assert other_margin_cap(5000, 0) == pytest.approx(5000)
    assert leaves_reserve_headroom(5000, 0, 480)
    assert leaves_reserve_headroom(5000, 0, 5000) is False
    assert leaves_reserve_headroom(5000, 0, 5000, 0)
    btc = _setup("BTC", 85922.0, "NO_SWEEP")
    eth_thin = _setup("ETH", None, "THIN_TAPE")
    eth_wait = _setup("ETH", 2700.0, "NO_SWEEP")
    assert is_close_setup(btc)
    assert is_close_setup(_setup("SOL", 150.0, "NO_RECLAIM"))
    assert is_close_setup(_setup("ETH", 2700.0, "ABSORB"))
    assert not is_close_setup(eth_thin)
    assert not is_close_setup(_setup("ETH", None, "NO_SWEEP"))
    assert not is_close_setup(_setup("ETH", 2700.0, None, armed=True))
    assert not is_close_setup(_setup("BTC", 85922.0, "THESIS_DONE"))
    assert not is_close_setup(_setup("ETH", 2700.0, "SECOND_ALO"))

    btc_close = _hunt("BTC", 100.0, "NO_SWEEP", score=4)
    thin = _hunt("ETH", None, "THIN_TAPE")
    no_swing = _hunt("SOL", None, "NO_SWING")
    only = preferred_close([btc_close, thin, no_swing])
    assert only is not None and only.coin == "BTC"
    # Nobody else was judged, so the reserve stays off.
    assert preferred_close([btc_close]) is None
    assert preferred_close([
        _hunt("BTC", 85922.0, None, armed=True, score=9),
        thin,
    ]) is None
    # Two close coins still pick one. The reserve does not turn off.
    assert preferred_close([btc_close, _hunt("ETH", 2700.0, "NO_SWEEP", score=4)]) is not None

    # Score 9 on xyz:XYZ100 beats a closer-or-not BTC score 4.
    xyz = _hunt("xyz:xyz100", 100.0, "NO_RECLAIM", score=9, bid=100.5, ask=101.5)
    btc_low = _hunt("BTC", 103.5, "NO_SWEEP", score=4)
    picked = preferred_close([btc_low, xyz, thin])
    assert picked is not None
    assert picked.coin == "xyz:XYZ100"
    assert picked.score == 9

    # Equal scores: the closer swing in bps wins, even when it is listed second.
    # BTC mid 104, swing 103.5 → ~48 bps. ETH mid 104, swing 90 → ~1346 bps.
    btc_near = _hunt("BTC", 103.5, "NO_SWEEP", score=4)
    eth_far = _hunt("ETH", 90.0, "ABSORB", score=4)
    assert close_distance_bps(btc_near["decision"], 104.0) == pytest.approx(
        (104.0 - 103.5) / 104.0 * 10_000
    )
    assert close_distance_bps(eth_far["decision"], 104.0) == pytest.approx(
        (104.0 - 90.0) / 104.0 * 10_000
    )
    closer = preferred_close([eth_far, btc_near])
    assert closer is not None and closer.coin == "BTC"
    assert closer.distance_bps == pytest.approx((104.0 - 103.5) / 104.0 * 10_000)
    # Short gap is swing − ref. A nearer short loses to the 48 bp long.
    short = _hunt("SOL", 110.0, "NO_SWEEP", score=4, bias="short")
    assert close_distance_bps(short["decision"], 104.0) == pytest.approx(
        (110.0 - 104.0) / 104.0 * 10_000
    )
    assert preferred_close([short, btc_near]).coin == "BTC"

    # Equal score and equal bps: earlier hunt order. Not a BTC preference.
    same_btc = _hunt("BTC", 100.0, "NO_SWEEP", score=4)
    same_eth = _hunt("ETH", 100.0, "NO_SWEEP", score=4)
    assert preferred_close([same_btc, same_eth]).coin == "BTC"
    assert preferred_close([same_eth, same_btc]).coin == "ETH"
    # A known distance beats a missing book.
    blind = _hunt("ETH", 100.0, "NO_SWEEP", score=4, bid=None, ask=None, last=None)
    assert close_distance_bps(blind["decision"], None) is None
    assert preferred_close([blind, same_btc]).coin == "BTC"


def test_close_reserve_holds_a_full_size_other_and_allows_a_small_one(tmp_path, caplog):
    """An unswept BTC does not block a ready ETH ticket.

    The reserve used to hold 60% for BTC's NO_SWEEP and veto ETH. A setup
    that cleared every gate is allowed through. The wide ticket still arms.
    """
    now = _now()
    btc = _waiting_prints(now, "BTC")
    eth = _retag(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "ETH",
    )
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_bars(_tight_bars(now), coin="ETH")
    feed = MemoryFeed(
        btc + eth,
        bbo={"BTC": (103.0, 105.0), "ETH": (102.0, 104.0)},
    )
    journal = tmp_path / "reserve-hold.jsonl"
    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(journal),
                loop_interval_sec=0,
            ),
            max_iterations=1,
            info=info,
            feed=feed,
            sleep_fn=lambda *_: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC", "ETH"),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    rows = TradeJournal(journal).read_all()
    assert any(r.get("coin") == "BTC" and r.get("fail_reason") == "NO_SWEEP" for r in rows)
    assert any(r["event"] == "model_b_arm" and r["coin"] == "ETH" for r in rows)
    assert not any(r.get("fail_reason") == "CLOSE_MARGIN_RESERVE" for r in rows)
    assert not any(r.get("action") == "close_reserve_engage" for r in rows)
    assert not any("CLOSE_MARGIN_RESERVE" in rec.message for rec in caplog.records)

    wide = InfoClient()
    wide.inject_bars(_bars(now), coin="BTC")
    wide.inject_bars(_bars(now), coin="ETH")
    wide_journal = tmp_path / "reserve-wide.jsonl"
    wide_summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            journal_path=str(wide_journal),
            loop_interval_sec=0,
        ),
        max_iterations=1,
        info=wide,
        feed=MemoryFeed(
            btc + eth,
            bbo={"BTC": (103.0, 105.0), "ETH": (102.0, 104.0)},
        ),
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC", "ETH"),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert wide_summary["arms"] == 1
    assert wide_summary["cancels"] == 0
    wide_rows = TradeJournal(wide_journal).read_all()
    assert any(r["event"] == "model_b_arm" and r["coin"] == "ETH" for r in wide_rows)
    assert not any(r.get("fail_reason") == "CLOSE_MARGIN_RESERVE" for r in wide_rows)
    assert not any(r.get("action") == "close_reserve_engage" for r in wide_rows)


def test_close_reserve_cancels_a_resting_other_then_releases(tmp_path, caplog):
    """A resting ETH Alo stays when BTC has not armed.

    BTC waiting on the sweep used to become the preferred close coin and
    cancel ETH. The reserve now protects the resting ticket instead.
    """
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_bars(_tight_bars(now), coin="ETH")
    eth = _retag(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "ETH",
    )
    feed = MemoryFeed(
        _long_prints(now)[:5] + eth,
        bbo={"BTC": (103.0, 105.0), "ETH": (102.0, 104.0)},
    )
    step = {"n": 0}

    def sleep_fn(_sec):
        step["n"] += 1
        feed._prints.clear()
        if step["n"] == 1:
            feed._prints.extend(_waiting_prints(now, "BTC"))
            feed._prints.extend(_retag(_long_prints(now)[:2], "ETH"))
        elif step["n"] == 2:
            feed._prints.extend(_long_prints(now)[:4])
            feed._prints.extend(_retag(_long_prints(now)[:4], "ETH"))

    journal = tmp_path / "reserve-cancel.jsonl"
    with caplog.at_level(logging.INFO):
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
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC", "ETH"),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    rows = TradeJournal(journal).read_all()
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    engage = [r for r in rows if r.get("action") == "close_reserve_engage"]
    assert [r["coin"] for r in engage] == ["ETH"]
    assert not any(r.get("action") == "close_reserve_release" for r in rows)
    assert any(
        "CLOSE_MARGIN_RESERVE engage" in rec.message and "protected=resting" in rec.message
        for rec in caplog.records
    )
    assert not any("CLOSE_MARGIN_RESERVE release" in rec.message for rec in caplog.records)
    assert any(r["event"] == "model_b_arm" and r["coin"] == "ETH" for r in rows)


def test_equal_scores_reserve_the_closer_swing_and_keep_its_alo(tmp_path, caplog):
    """Both coins are close at score 4. ETH's swing is closer, so ETH keeps the Alo.

    BTC mid 104 vs swing 100 is ~385 bps. ETH mid 103 vs the same swing is
    ~291 bps. The reserve engages for ETH and does not cancel ETH.
    """
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_bars(_tight_bars(now), coin="ETH")
    eth = _retag(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "ETH",
    )
    feed = MemoryFeed(
        _long_prints(now)[:5] + eth,
        bbo={"BTC": (103.0, 105.0), "ETH": (102.0, 104.0)},
    )

    def sleep_fn(_sec):
        feed._prints.clear()
        feed._prints.extend(_waiting_prints(now, "BTC"))
        feed._prints.extend(_waiting_prints(now, "ETH", seq0=100))

    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(tmp_path / "reserve-both.jsonl"),
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
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    rows = TradeJournal(tmp_path / "reserve-both.jsonl").read_all()
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    engage = [r for r in rows if r.get("action") == "close_reserve_engage"]
    assert len(engage) == 1
    assert engage[0]["coin"] == "ETH"
    # Arm score from the resting Alo (32 prints → 3), not the later NO_SWEEP tape.
    assert engage[0]["score"] == 3
    assert any(
        "CLOSE_MARGIN_RESERVE engage" in rec.message and "coin=ETH" in rec.message
        for rec in caplog.records
    )


def test_close_reserve_tie_follows_hunt_order(tmp_path):
    """A resting ETH Alo is not cancelled for an unswept BTC.

    Hunt order used to hand the reserve to whichever close setup was
    listed first and cancel the other Alo. ETH is the coin that actually
    rested, so it keeps the reserve in both hunt orders.
    """
    now = _now()

    def _run(coins, journal):
        info = InfoClient()
        info.inject_bars(_bars(now), coin="BTC")
        info.inject_bars(_tight_bars(now), coin="ETH")
        eth = _retag(
            _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
            "ETH",
        )
        feed = MemoryFeed(
            _long_prints(now)[:5] + eth,
            bbo={"BTC": (103.0, 105.0), "ETH": (103.0, 105.0)},
        )

        def sleep_fn(_sec):
            feed._prints.clear()
            feed._prints.extend(_waiting_prints(now, "BTC"))
            feed._prints.extend(_waiting_prints(now, "ETH", price=104.0, seq0=100))

        run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(journal),
                loop_interval_sec=0,
            ),
            max_iterations=2,
            info=info,
            feed=feed,
            sleep_fn=sleep_fn,
            now_fn=lambda: now,
            connect_feed=False,
            coins=coins,
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
        return TradeJournal(journal).read_all()

    btc_first = _run(("BTC", "ETH"), tmp_path / "tie-btc.jsonl")
    engage = [r for r in btc_first if r.get("action") == "close_reserve_engage"]
    assert [r["coin"] for r in engage] == ["ETH"]
    assert not any(r["event"] == "model_b_cancel" for r in btc_first)
    assert not any(r.get("fail_reason") == "CLOSE_MARGIN_RESERVE" for r in btc_first)

    eth_first = _run(("ETH", "BTC"), tmp_path / "tie-eth.jsonl")
    engage = [r for r in eth_first if r.get("action") == "close_reserve_engage"]
    assert [r["coin"] for r in engage] == ["ETH"]
    assert not any(r["event"] == "model_b_cancel" for r in eth_first)


def test_close_reserve_follows_xyz100_not_btc(tmp_path, caplog):
    """A ready BTC is not blocked to save margin for an unswept xyz:XYZ100."""
    now = _now()
    xyz = _waiting_prints(now, "xyz:XYZ100", n=90)
    btc = _retag(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "BTC",
    )
    info = InfoClient()
    info.inject_bars(_bars(now), coin="xyz:XYZ100")
    info.inject_bars(_tight_bars(now), coin="BTC")
    feed = MemoryFeed(
        xyz + btc,
        bbo={"xyz:XYZ100": (103.0, 105.0), "BTC": (102.0, 104.0)},
    )
    journal = tmp_path / "reserve-xyz.jsonl"
    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(journal),
                loop_interval_sec=0,
            ),
            max_iterations=1,
            info=info,
            feed=feed,
            sleep_fn=lambda *_: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("xyz:XYZ100", "BTC"),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    rows = TradeJournal(journal).read_all()
    assert any(r["event"] == "model_b_arm" and r["coin"] == "BTC" for r in rows)
    assert any(r.get("coin") == "xyz:XYZ100" and r.get("fail_reason") == "NO_SWEEP" for r in rows)
    assert not any(r.get("fail_reason") == "CLOSE_MARGIN_RESERVE" for r in rows)
    assert not any(r.get("action") == "close_reserve_engage" for r in rows)
    assert not any("CLOSE_MARGIN_RESERVE" in rec.message for rec in caplog.records)


def test_close_reserve_cancels_btc_when_xyz100_scores_higher(tmp_path, caplog):
    """An unswept score-9 xyz:XYZ100 does not cancel a resting BTC Alo."""
    now = _now()
    info = InfoClient()
    info.inject_bars(_tight_bars(now), coin="BTC")
    info.inject_bars(_bars(now), coin="xyz:XYZ100")
    feed = MemoryFeed(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0)
        + _retag(_long_prints(now)[:5], "xyz:XYZ100"),
        bbo={"BTC": (102.0, 104.0), "xyz:XYZ100": (103.0, 105.0)},
    )
    step = {"n": 0}

    def sleep_fn(_sec):
        step["n"] += 1
        feed._prints.clear()
        if step["n"] == 1:
            feed._prints.extend(_waiting_prints(now, "BTC", n=40))
            feed._prints.extend(_waiting_prints(now, "xyz:XYZ100", n=90, seq0=1000))
        elif step["n"] == 2:
            feed._prints.extend(_long_prints(now)[:4])
            feed._prints.extend(_retag(_long_prints(now)[:4], "xyz:XYZ100"))

    journal = tmp_path / "reserve-xyz-cancel.jsonl"
    with caplog.at_level(logging.INFO):
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
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC", "xyz:XYZ100"),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    rows = TradeJournal(journal).read_all()
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    assert not any(r["event"] == "model_b_cancel" for r in rows)
    assert any(r["event"] == "model_b_arm" and r["coin"] == "BTC" for r in rows)
    assert any(r.get("coin") == "xyz:XYZ100" and r.get("fail_reason") == "NO_SWEEP" for r in rows)
    engage = [r for r in rows if r.get("action") == "close_reserve_engage"]
    assert [r["coin"] for r in engage] == ["BTC"]
    assert not any(r.get("fail_reason") == "CLOSE_MARGIN_RESERVE" for r in rows)
    assert any("protected=resting" in rec.message for rec in caplog.records)


def test_stick_preferred_ignores_a_bps_flip_until_score_is_higher():
    """Equal-score distance noise does not move the reserve. A higher score does."""
    btc = ClosePreference("BTC", 4, 10.0)
    eth = ClosePreference("ETH", 4, 1.0)
    assert stick_preferred([btc, eth], "BTC").coin == "BTC"
    assert stick_preferred([btc, eth], None).coin == "ETH"
    higher = ClosePreference("ETH", 9, 80.0)
    assert stick_preferred([btc, higher], "BTC").coin == "ETH"
    assert stick_preferred([eth], "BTC").coin == "ETH"


def test_resting_reserve_holds_a_lower_score_ticket(tmp_path, caplog):
    """A resting score-9 Alo still blocks a lower score that would spend the reserve."""
    now = _now()
    info = InfoClient()
    info.inject_bars(_bars(now), coin="ETH")
    info.inject_bars(_tight_bars(now), coin="BTC")
    eth = _retag(
        _long_prints(now, n_prefix=90, prefix_step=0.2, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "ETH",
    )
    feed = MemoryFeed(
        eth + _long_prints(now)[:5],
        bbo={"ETH": (102.0, 104.0), "BTC": (102.0, 104.0)},
    )

    def sleep_fn(_sec):
        feed._prints.clear()
        feed._prints.extend(_waiting_prints(now, "ETH", n=40))
        feed._prints.extend(
            _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0)
        )

    journal = tmp_path / "reserve-resting-hold.jsonl"
    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                journal_path=str(journal),
                loop_interval_sec=0,
            ),
            max_iterations=2,
            info=info,
            feed=feed,
            sleep_fn=sleep_fn,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("ETH", "BTC"),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    rows = TradeJournal(journal).read_all()
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    held = [r for r in rows if r.get("fail_reason") == "CLOSE_MARGIN_RESERVE"]
    assert len(held) == 1
    assert held[0]["coin"] == "BTC"
    assert held[0]["preferred"] == "ETH"
    assert held[0]["score"] < 9
    assert any(r["event"] == "model_b_arm" and r["coin"] == "ETH" for r in rows)
    assert any("CLOSE_MARGIN_RESERVE hold" in rec.message for rec in caplog.records)


def test_clearinghouse_margin_includes_xyz_and_skips_reduce_only():
    """xyz marginUsed is real held margin. A reduce-only TP is not an entry."""
    positions, reported = parse_clearinghouse(
        {
            "marginSummary": {
                "accountValue": "69.75",
                "totalNtlPos": "174.01",
                "totalRawUsd": "69.75",
                "totalMarginUsed": "8.72",
            },
            "assetPositions": [
                {
                    "type": "oneWay",
                    "position": {
                        "coin": "xyz:XYZ100",
                        "szi": "0.0056",
                        "entryPx": "31074",
                        "marginUsed": "8.72",
                    },
                }
            ],
        }
    )
    assert reported == pytest.approx(8.72)
    assert positions[0].coin == "xyz:XYZ100"
    assert positions[0].side == "long"
    assert positions[0].held_margin() == pytest.approx(8.72)
    snapshot = AccountSnapshot(
        ok=True,
        positions=tuple(positions),
        reported_margin=reported,
    )
    assert snapshot.margin_held() == pytest.approx(8.72)
    orders = parse_entry_orders(
        [
            {
                "coin": "XYZ:XYZ100",
                "side": "B",
                "limitPx": "31000",
                "sz": "0.01",
                "oid": 1,
                "reduceOnly": False,
            },
            {
                "coin": "xyz:XYZ100",
                "side": "A",
                "limitPx": "31165",
                "sz": "0.0056",
                "oid": 2,
                "reduceOnly": True,
                "orderType": "Take Profit Market",
            },
        ]
    )
    assert len(orders) == 1
    assert orders[0].coin == "xyz:XYZ100"
    assert orders[0].side == "long"
    assert isinstance(orders[0], EntryOrder)


def _live_account_settings(tmp_path, name: str) -> Settings:
    return Settings(
        entry_mode="model_b",
        risk_per_trade=0.02,
        trading_mode="live",
        i_understand_live_trading=True,
        private_key="0x" + "ab" * 32,
        account_address="0x" + "11" * 20,
        network="mainnet",
        journal_path=str(tmp_path / name),
        loop_interval_sec=0,
    )


def test_free_margin_subtracts_held_xyz_position(tmp_path, caplog):
    """Spot USDC total stays the 2% base. Free margin subtracts xyz marginUsed.

    A tight BTC ticket sized off $69.75 does not fit once $40 is already
    held. The old book, empty after a restart, treated $69.75 as free.
    """
    now = _now()
    info = InfoClient()
    info.inject_bars(_tight_bars(now), coin="BTC")
    info.inject_spot_usdc(69.75)
    info.inject_account_snapshot(
        AccountSnapshot(
            ok=True,
            positions=(
                PerpPosition(
                    coin="xyz:XYZ100",
                    szi=0.0056,
                    entry=31074.0,
                    margin_used=40.0,
                ),
            ),
            reported_margin=40.0,
        )
    )
    feed = MemoryFeed(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        bbo={"BTC": (102.0, 104.0)},
    )

    class FakeLive:
        def __init__(self):
            self.alos = []

        def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
            self.alos.append(coin)
            return {"response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}

    fake = FakeLive()
    with caplog.at_level(logging.INFO):
        run_model_b(
            _live_account_settings(tmp_path, "free-xyz.jsonl"),
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
    assert fake.alos == []
    rows = TradeJournal(tmp_path / "free-xyz.jsonl").read_all()
    failed = [r for r in rows if r.get("fail_reason") == "INSUFFICIENT_MARGIN"]
    assert len(failed) == 1
    assert failed[0]["coin"] == "BTC"
    assert failed[0]["spot"] == pytest.approx(69.75)
    assert failed[0]["held"] == pytest.approx(40.0)
    assert failed[0]["free_margin"] == pytest.approx(29.75)
    # Sized off the full spot balance, not off the smaller free number.
    assert failed[0]["margin_need"] > failed[0]["free_margin"]
    assert failed[0]["margin_need"] > 35
    assert any(r.get("action") == "free_margin" and r.get("positions") == "xyz:XYZ100" for r in rows)
    assert any(r.get("action") == "adopt_position" and r.get("coin") == "xyz:XYZ100" for r in rows)
    assert any("MODEL_B MARGIN free" in rec.message and "held=40.0000" in rec.message for rec in caplog.records)
    assert any("MODEL_B ADOPT xyz:XYZ100" in rec.message for rec in caplog.records)


def test_reconcile_journals_xyz_close_from_fills(tmp_path, caplog):
    """A TP fill missed by the websocket still gets a journal close.

    The open is already in the journal (the 13:01 xyz:XYZ100). The book is
    empty, as after the 14:34 restart, and the exchange is flat. The fill
    at 31162 is 3 points under the 31165 trigger.
    """
    now = _now()
    journal = tmp_path / "reconcile.jsonl"
    opened_at = now - 10_000
    journal.write_text(
        json.dumps(
            {
                "ts": opened_at,
                "event": "open",
                "symbol": "xyz:XYZ100",
                "side": "long",
                "size": 0.0056,
                "price": 31074.0,
                "stop": 31020.0,
                "tp": 31165.0,
                "entry_mode": "model_b",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_spot_usdc(69.75)
    info.inject_account_snapshot(
        AccountSnapshot(
            ok=True,
            positions=(),
            fills=(
                UserFill(
                    coin="xyz:XYZ100",
                    oid=77,
                    price=31162.0,
                    size=0.0056,
                    ts=opened_at + 100,
                    crossed=True,
                    side="sell",
                    direction="Close Long",
                    closed_pnl=(31162.0 - 31074.0) * 0.0056,
                    start_position=0.0056,
                    tid=9001,
                ),
            ),
            reported_margin=0.0,
        )
    )
    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            _live_account_settings(tmp_path, "reconcile.jsonl"),
            max_iterations=1,
            info=info,
            feed=MemoryFeed([]),
            exchange=object(),
            sleep_fn=lambda *_: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC",),
        )
    assert summary["closes"] == 1
    rows = TradeJournal(journal).read_all()
    closes = [r for r in rows if r["event"] == "close" and r.get("symbol") == "xyz:XYZ100"]
    assert len(closes) == 1
    assert closes[0]["price"] == pytest.approx(31162.0)
    assert closes[0]["reason"] == "tp"
    assert closes[0]["source"] == "reconcile"
    assert closes[0]["side"] == "long"
    assert closes[0]["pnl"] == pytest.approx((31162.0 - 31074.0) * 0.0056)
    assert any("MODEL_B RECONCILE close xyz:XYZ100" in rec.message for rec in caplog.records)


def test_same_coin_xyz_blocked_after_restart_and_resting_entry(tmp_path, caplog):
    """A restart with an open xyz position, or a resting entry, never sends."""
    now = _now()
    prints = _retag(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "xyz:XYZ100",
    )

    def _run(snapshot, name):
        info = InfoClient()
        info.inject_bars(_tight_bars(now), coin="xyz:XYZ100")
        info.inject_spot_usdc(69.75)
        info.inject_account_snapshot(snapshot)
        sent = []

        class FakeLive:
            def place_alo(self, coin, is_buy, size, limit_px, leverage=20):
                sent.append(coin)
                return {"response": {"data": {"statuses": [{"resting": {"oid": 3}}]}}}

        run_model_b(
            _live_account_settings(tmp_path, name),
            max_iterations=1,
            info=info,
            feed=MemoryFeed(prints, bbo={"xyz:XYZ100": (102.0, 104.0)}),
            exchange=FakeLive(),
            sleep_fn=lambda *_: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("xyz:XYZ100",),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
        return sent, TradeJournal(tmp_path / name).read_all()

    with caplog.at_level(logging.INFO):
        sent, rows = _run(
            AccountSnapshot(
                ok=True,
                positions=(
                    PerpPosition(
                        coin="xyz:XYZ100",
                        szi=0.0056,
                        entry=31074.0,
                        margin_used=8.72,
                    ),
                ),
                reported_margin=8.72,
            ),
            "block-position.jsonl",
        )
    assert sent == []
    blocked = [r for r in rows if r.get("fail_reason") == "OPEN_POSITION"]
    assert len(blocked) == 1
    assert blocked[0]["coin"] == "xyz:XYZ100"
    assert blocked[0]["armed"] is False
    assert any("MODEL_B BLOCK xyz:XYZ100 reason=OPEN_POSITION" in rec.message for rec in caplog.records)

    caplog.clear()
    sent, rows = _run(
        AccountSnapshot(
            ok=True,
            entry_orders=(
                EntryOrder(
                    coin="xyz:XYZ100",
                    oid=44,
                    side="long",
                    limit_px=31000.0,
                    size=0.01,
                ),
            ),
        ),
        "block-entry.jsonl",
    )
    assert sent == []
    blocked = [r for r in rows if r.get("fail_reason") == "RESTING_ENTRY"]
    assert len(blocked) == 1
    assert blocked[0]["coin"] == "xyz:XYZ100"
    assert any("MODEL_B BLOCK xyz:XYZ100 reason=RESTING_ENTRY" in rec.message for rec in caplog.records)


def test_close_reserve_off_does_not_hold_the_other_coin(tmp_path, caplog):
    """MODEL_B_CLOSE_MARGIN_RESERVE=0 leaves the full-size ETH ticket free to arm."""
    now = _now()
    btc = _waiting_prints(now, "BTC")
    eth = _retag(
        _long_prints(now, last_price=103.0, final_price=103.0, sweep_px=99.0),
        "ETH",
    )
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    info.inject_bars(_tight_bars(now), coin="ETH")
    feed = MemoryFeed(
        btc + eth,
        bbo={"BTC": (103.0, 105.0), "ETH": (102.0, 104.0)},
    )
    journal = tmp_path / "reserve-off.jsonl"
    with caplog.at_level(logging.INFO):
        summary = run_model_b(
            Settings(
                entry_mode="model_b",
                risk_per_trade=0.02,
                model_b_close_margin_reserve=0.0,
                journal_path=str(journal),
                loop_interval_sec=0,
            ),
            max_iterations=1,
            info=info,
            feed=feed,
            sleep_fn=lambda *_: None,
            now_fn=lambda: now,
            connect_feed=False,
            coins=("BTC", "ETH"),
            pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
            tick_for=lambda coin: 0.01,
        )
    assert summary["arms"] == 1
    assert summary["cancels"] == 0
    rows = TradeJournal(journal).read_all()
    assert any(r["event"] == "model_b_arm" and r["coin"] == "ETH" for r in rows)
    assert not any(r.get("fail_reason") == "CLOSE_MARGIN_RESERVE" for r in rows)
    assert not any(r.get("action") == "close_reserve_engage" for r in rows)
    assert not any("CLOSE_MARGIN_RESERVE" in rec.message for rec in caplog.records)


def test_ticket_fits_uses_notional_over_leverage_on_the_sizing_balance():
    wide = initial_margin(97.087378, 99.0)
    assert wide == pytest.approx(97.087378 * 99.0 / 20.0)
    assert ticket_fits(5000.0, 0.0, 97.087378, 99.0)
    assert ticket_fits(5000.0, wide, 97.087378, 99.0)
    size, _dollar = size_from_stop(5000.0, 99.0, 98.97, risk_pct=0.02)
    full = initial_margin(size, 99.0)
    assert full == pytest.approx(5000.0, abs=1e-3)
    assert ticket_fits(5000.0, 0.0, size, 99.0)
    assert ticket_fits(5000.0, full, size, 99.0) is False


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


_VP_KEYS = (
    "vp_poc",
    "vp_vah",
    "vp_val",
    "nearest_lvn_on_side",
    "sweep_to_val_bps",
    "sweep_to_lvn_bps",
    "vp_tag",
    "catalyst_flag",
)


def test_vp_tags_are_logged_and_do_not_change_the_arm(monkeypatch):
    """Tags ride on the decision. They do not move the Alo, stop, size, or TP."""
    from hl_bot.strategy.volume_profile import (
        VP_AS_FILTER,
        VP_ENABLED,
        VP_ENTRIES,
        VolumeProfile,
    )

    assert VP_AS_FILTER is False and VP_ENTRIES is False and VP_ENABLED is False
    now = _now()
    prints = _long_prints(now)
    bars = _bars(now)
    pools = [Pool("PDH", 130.0, False)]
    base = _decide(prints, bars, pools)
    assert base.armed is True
    intent = base.intent
    assert intent is not None
    assert base.vp_tag == "none"
    assert base.vp_poc is None
    assert base.catalyst_flag is False
    logged = base.to_log()
    for key in _VP_KEYS:
        assert key in logged

    def rich(*_args, **_kwargs):
        return VolumeProfile(
            poc=110.0,
            vah=111.0,
            val=99.0,
            lvns=(98.0, 112.0),
            total_volume=1.0,
            bar_count=40,
            ok=True,
            error=None,
        )

    monkeypatch.setattr("hl_bot.strategy.model_b.vp_log.compute_profile", rich)
    tagged = _decide(prints, bars, pools)
    assert tagged.armed is True
    assert tagged.fail_reason is None
    tagged_intent = tagged.intent
    assert tagged_intent is not None
    assert tagged_intent.limit_px == pytest.approx(intent.limit_px)
    assert tagged_intent.stop == pytest.approx(intent.stop)
    assert tagged_intent.size == pytest.approx(intent.size)
    assert tagged_intent.take_profit == pytest.approx(intent.take_profit)
    assert tagged.vp_tag == "val"
    assert tagged.vp_val == pytest.approx(99.0)
    assert tagged.nearest_lvn_on_side == pytest.approx(98.0)
    assert tagged.catalyst_flag is False

    def boom(*_args, **_kwargs):
        raise RuntimeError("profile down")

    monkeypatch.setattr("hl_bot.strategy.model_b.vp_log.compute_profile", boom)
    broken = _decide(prints, bars, pools)
    assert broken.armed is True
    assert broken.intent is not None
    assert broken.intent.stop == pytest.approx(intent.stop)
    assert broken.intent.size == pytest.approx(intent.size)
    assert broken.intent.limit_px == pytest.approx(intent.limit_px)
    assert broken.intent.take_profit == pytest.approx(intent.take_profit)
    assert broken.vp_tag == "vp_error"
    assert broken.vp_poc is None
    assert broken.sweep_to_val_bps is None
    assert broken.catalyst_flag is False

    thin = _decide(_long_prints(now)[:5], bars, pools)
    assert thin.armed is False
    assert thin.fail_reason == "THIN_TAPE"
    assert thin.vp_tag == "vp_error"
    assert thin.intent is None


def test_vp_fields_land_on_arm_and_fail_journal_rows(tmp_path):
    now = _now()
    journal = tmp_path / "vp.jsonl"
    info = InfoClient()
    info.inject_bars(_bars(now), coin="BTC")
    summary = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            journal_path=str(journal),
            loop_interval_sec=0,
        ),
        max_iterations=1,
        info=info,
        feed=MemoryFeed(_long_prints(now), bbo={"BTC": (103.0, 105.0)}),
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert summary["arms"] == 1
    arm = next(r for r in TradeJournal(journal).read_all() if r["event"] == "model_b_arm")
    for key in _VP_KEYS:
        assert key in arm
    assert arm["vp_tag"] == "none"
    assert arm["catalyst_flag"] is False
    assert arm["vp_poc"] is None

    fail_journal = tmp_path / "vp-fail.jsonl"
    failed = run_model_b(
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            journal_path=str(fail_journal),
            loop_interval_sec=0,
        ),
        max_iterations=1,
        info=info,
        feed=MemoryFeed(_long_prints(now)[:5], bbo={"BTC": (103.0, 105.0)}),
        sleep_fn=lambda *_: None,
        now_fn=lambda: now,
        connect_feed=False,
        coins=("BTC",),
        pools_for=lambda *a, **k: [Pool("PDH", 130.0, False)],
        tick_for=lambda coin: 0.01,
    )
    assert failed["arms"] == 0
    row = next(r for r in TradeJournal(fail_journal).read_all() if r["event"] == "model_b_fail")
    assert row["fail_reason"] == "THIN_TAPE"
    for key in _VP_KEYS:
        assert key in row
    assert row["vp_tag"] == "none"
    assert row["catalyst_flag"] is False

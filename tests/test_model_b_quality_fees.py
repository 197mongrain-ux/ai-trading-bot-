"""PR #16: setup quality, per-coin fees, candle weight, risk-cap log, isolated margin."""

from __future__ import annotations

import pytest

from hl_bot.exchange.info_client import (
    InfoClient,
    candle_request_weight,
)
from hl_bot.execution.guard import PositionGuard
from hl_bot.execution.model_b_loop import (
    ClosePreference,
    rank_armed_hunts,
    resting_quality_blocks_closer_cancel,
    stick_preferred,
)
from hl_bot.strategy.model_b.alo import resting_quality_blocks_closer_cancel as quality_blocks
from hl_bot.strategy.model_b.fees import (
    conservative_fees,
    parse_asset_flags,
    parse_user_fees,
    resolve_fees,
)
from hl_bot.strategy.model_b.groups import assess_risk_cap, correlation_group
from hl_bot.strategy.model_b.quality import rank_armed_hunts as rank_hunts
from hl_bot.strategy.model_b.quality import setup_quality
from hl_bot.strategy.model_b.risk import (
    MAKER_FEE_RATE,
    TAKER_FEE_RATE,
    cap_size_to_loss,
    loss_at_stop,
    min_tp_distance,
    size_from_stop,
)


def _sized(entry, stop, equity, maker, taker):
    size, _dollar = size_from_stop(
        equity,
        entry,
        stop,
        risk_pct=0.02,
        leverage=20,
        notional_leverage=20,
        min_stop_bps=15,
        include_fees=True,
        maker_fee=maker,
        taker_fee=taker,
    )
    size = cap_size_to_loss(
        size,
        entry,
        stop,
        equity,
        include_fees=True,
        maker_fee=maker,
        taker_fee=taker,
    )
    loss = loss_at_stop(
        size, entry, stop, include_fees=True, maker_fee=maker, taker_fee=taker
    )
    return size, loss


def test_quality_ranks_the_better_setup_and_shadow_keeps_order():
    weak = setup_quality(
        side="long",
        entry=100.0,
        stop=99.85,
        take_profit=100.20,
        absorb=0.4,
        swing=100.0,
        sweep=99.97,
        macro="range",
        macro_adx=10,
        atr=0.8,
        maker_fee=MAKER_FEE_RATE * 2,
        taker_fee=TAKER_FEE_RATE * 2,
    )
    strong = setup_quality(
        side="short",
        entry=100.0,
        stop=100.40,
        take_profit=98.8,
        absorb=2.4,
        swing=100.0,
        sweep=100.20,
        macro="down",
        macro_adx=32,
        atr=0.50,
        maker_fee=MAKER_FEE_RATE,
        taker_fee=TAKER_FEE_RATE,
    )
    assert strong.quality > weak.quality
    assert strong.tp1_r > weak.tp1_r
    assert strong.fee_drag < weak.fee_drag
    assert strong.macro_align == 1.0
    assert weak.macro_align == 0.0
    # Same book, one knob at a time, still moves the score the right way.
    deeper = setup_quality(
        side="long", entry=100, stop=99, take_profit=102, sweep=98, swing=100, absorb=1
    )
    shallow = setup_quality(
        side="long", entry=100, stop=99, take_profit=102, sweep=99.9, swing=100, absorb=1
    )
    assert deeper.sweep_bps > shallow.sweep_bps
    assert deeper.quality > shallow.quality

    def hunt(coin, quality, armed=True):
        decision = type("D", (), {})()
        decision.armed = armed
        decision.intent = object() if armed else None
        decision.quality = quality
        decision.coin = coin
        return {"post": True, "decision": decision}

    hunts = [hunt("BTC", 1.0), hunt("ETH", 4.0), hunt("SOL", None, armed=False), hunt("xyz:GOLD", 3.0)]
    ranked = rank_hunts(hunts, "on")
    assert [item["decision"].coin for item in ranked] == ["ETH", "xyz:GOLD", "SOL", "BTC"]
    assert [item["decision"].coin for item in rank_hunts(hunts, "shadow")] == [
        "BTC",
        "ETH",
        "SOL",
        "xyz:GOLD",
    ]
    assert [item["decision"].coin for item in rank_hunts(hunts, "off")] == [
        "BTC",
        "ETH",
        "SOL",
        "xyz:GOLD",
    ]
    assert rank_armed_hunts(hunts, "off")[0]["decision"].coin == "BTC"


def test_quality_closer_guard_and_reserve_prefer_the_higher_quality():
    assert quality_blocks(8.2, 4.1) is True
    assert quality_blocks(4.1, 8.2) is False
    assert quality_blocks(3.0, 3.0) is False
    assert quality_blocks(None, 9.0) is False
    assert resting_quality_blocks_closer_cancel(5.0, 5.0) is False
    high = ClosePreference("ETH", 9, 30.0, quality=6.5)
    low = ClosePreference("BTC", 9, 1.0, quality=2.0)
    # Closer bps does not beat a higher quality. Equal quality keeps the incumbent.
    assert stick_preferred([low, high], "BTC").coin == "ETH"
    tied = ClosePreference("SOL", 9, 0.1, quality=6.5)
    assert stick_preferred([high, tied], "ETH").coin == "ETH"


def test_btc_and_gold_tight_stops_stay_inside_20x_and_2pct():
    equity = 10_000.0
    btc_entry = 80_000.0
    btc_stop = btc_entry * (1.0 - 0.0004)
    btc_size, btc_loss = _sized(
        btc_entry, btc_stop, equity, MAKER_FEE_RATE, TAKER_FEE_RATE
    )
    assert btc_size * btc_entry <= equity * 20.0 + 1e-6
    assert btc_loss <= 0.02 * equity + 1e-6

    gold = conservative_fees("xyz:GOLD")
    assert gold.maker == MAKER_FEE_RATE * 2
    assert gold.taker == TAKER_FEE_RATE * 2
    entry = 4_100.0
    stop = entry * (1.0 - 15.0 / 10_000.0)
    size, loss = _sized(entry, stop, equity, gold.maker, gold.taker)
    assert size * entry <= equity * 20.0 + 1e-6
    assert loss / equity <= 0.0200001

    # Observed xyz schedule (2x a 1.44/4.32 account), sized at those rates.
    observed_m, observed_t = 0.000288, 0.000864
    size_obs, loss_obs = _sized(entry, stop, equity, observed_m, observed_t)
    assert size_obs * entry <= equity * 20.0 + 1e-6
    assert loss_obs / equity <= 0.0200001

    # The bug: size as if GOLD paid main-dex fees, then pay the observed xyz fee.
    wrong, _ = size_from_stop(
        equity, entry, stop, risk_pct=0.02, leverage=20, notional_leverage=20,
        min_stop_bps=15, include_fees=True,
    )
    wrong = cap_size_to_loss(wrong, entry, stop, equity, include_fees=True)
    paid = loss_at_stop(
        wrong, entry, stop, include_fees=True, maker_fee=observed_m, taker_fee=observed_t
    )
    assert paid / equity > 0.02


def test_fee_table_user_rates_and_growth_never_discount_an_unknown_xyz():
    user = parse_user_fees({"userAddRate": "0.000144", "userCrossRate": "0.000432"})
    assert user == (0.000144, 0.000432)
    main = resolve_fees("BTC", user_add=user[0], user_cross=user[1])
    assert main.source == "user"
    assert main.maker == 0.000144
    gold = resolve_fees("xyz:GOLD", user_add=user[0], user_cross=user[1], growth=None)
    assert gold.source == "user-xyz"
    assert gold.maker == 0.000144 * 2
    assert gold.taker == 0.000432 * 2
    growth = resolve_fees("xyz:NVDA", user_add=user[0], user_cross=user[1], growth=True)
    assert growth.source == "user-growth"
    assert abs(growth.maker - 0.000144 * 0.2) < 1e-12
    assert abs(growth.taker - 0.0000864) < 1e-12
    flags = parse_asset_flags(
        {
            "universe": [
                {"name": "GOLD", "maxLeverage": 20},
                {"name": "NVDA", "growthMode": "enabled", "onlyIsolated": False},
                {"name": "SMSN", "onlyIsolated": True, "maxLeverage": 5},
            ]
        },
        dex="xyz",
    )
    assert flags["xyz:GOLD"]["growth"] is None
    assert flags["xyz:NVDA"]["growth"] is True
    assert flags["xyz:SMSN"]["only_isolated"] is True
    # Unknown growth stays on the high rate. A missing flag is not a discount.
    assert resolve_fees("xyz:GOLD", growth=flags["xyz:GOLD"]["growth"]).source == "table-xyz"
    # 8 bps stop: 1R is 8 bps of price. xyz round-trip (~11.5 bps) clears it;
    # the base tier (~6 bps) does not, so the floor stays at 1R.
    tight = 100.0 * (1.0 - 8.0 / 10_000.0)
    floor_main = min_tp_distance(100.0, tight)
    floor_xyz = min_tp_distance(100.0, tight, maker_fee=gold.maker, taker_fee=gold.taker)
    assert floor_xyz > floor_main
    assert floor_xyz == pytest.approx(100.0 * (gold.maker + gold.taker))


def test_incremental_candles_merge_stagger_and_do_not_block_the_guard(monkeypatch):
    clock = {"t": 1_700_000_000.0}
    calls: list[tuple] = []

    def post(base, coin, interval, start, end, timeout=15.0):
        calls.append((coin, int(start), int(end)))
        return 200, [
            {"t": int(end) - 60_000, "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 1},
            {"t": int(end) - 120_000, "o": 1, "h": 1, "l": 0.5, "c": 1, "v": 1},
        ]

    monkeypatch.setattr("hl_bot.exchange.info_client._post_candle_snapshot", post)
    client = InfoClient(base_url="https://example.invalid")
    client._now = lambda: clock["t"]
    client.candle_mode = "incremental"
    start = int(clock["t"] * 1000) - 14 * 24 * 3600 * 1000
    end = int(clock["t"] * 1000)
    first = client.get_candles("BTC", "1m", start_ms=start, end_ms=end)
    assert first
    assert calls[0][1] == start
    cold = client.weight_report(clock["t"])
    assert cold["candle"] > 100
    assert client.get_candles("ETH", "1m", start_ms=start, end_ms=end) == []
    assert len(calls) == 1
    clock["t"] += 61
    end2 = int(clock["t"] * 1000)
    merged = client.get_candles("BTC", "1m", start_ms=start, end_ms=end2)
    assert len(calls) == 2
    assert calls[1][1] > start
    assert end2 - calls[1][1] <= 10 * 60_000
    opens = {int(bar["t"]) for bar in merged}
    assert (end - 60_000) in opens
    assert (end2 - 60_000) in opens
    tail = candle_request_weight(calls[1][1], calls[1][2], "1m")
    assert tail < 25

    def post_info(base, payload, timeout=15.0):
        if payload.get("type") == "clearinghouseState":
            return 200, {"assetPositions": [], "marginSummary": {"totalMarginUsed": "0"}}
        return 200, []

    monkeypatch.setattr("hl_bot.exchange.info_client._post_info", post_info)
    client._candles.penalize(clock["t"])
    assert client._candles.in_backoff(clock["t"])
    snap = client.load_account_snapshot("0xabc", ("",))
    assert snap.ok is True

    full = 18 * candle_request_weight(0, 14 * 24 * 3600 * 1000, "1m")
    tail_book = 18 * candle_request_weight(0, 8 * 60_000, "1m")
    assert full > 1200
    assert tail_book < 400


def test_risk_cap_logs_only_unless_turned_on():
    assert correlation_group("xyz:GOLD") == "metals"
    assert correlation_group("xyz:NVDA") == "us_tech"
    assert correlation_group("BTC") == "crypto"
    assert correlation_group("xyz:SMSN") is None
    rows = [("xyz:GOLD", 100.0), ("BTC", 50.0)]
    shadow = assess_risk_cap(
        coin="xyz:SILVER", new_risk=80.0, equity=1000.0, open_rows=rows, max_pct=6, mode="shadow"
    )
    assert shadow.block is False
    assert "group=metals" in shadow.reason
    assert "total=" in shadow.reason
    live = assess_risk_cap(
        coin="xyz:SILVER", new_risk=80.0, equity=1000.0, open_rows=rows, max_pct=6, mode="on"
    )
    assert live.block is True
    off = assess_risk_cap(
        coin="xyz:SILVER", new_risk=80.0, equity=1000.0, open_rows=rows, max_pct=6, mode="off"
    )
    assert off.block is False
    assert off.reason == ""
    clear = assess_risk_cap(
        coin="xyz:CL", new_risk=10.0, equity=10_000.0, open_rows=[("BTC", 20.0)], max_pct=6, mode="shadow"
    )
    assert clear.reason == ""


def test_isolated_only_sets_isolated_margin_and_cross_still_fails_closed():
    from hl_bot.exchange.live_exchange import LiveExchange

    calls: list = []

    class Exchange:
        def update_leverage(self, lev, coin, is_cross=True):
            calls.append((lev, coin, is_cross))
            if is_cross and coin == "xyz:SMSN":
                raise RuntimeError("only isolated margin allowed")
            return {"status": "ok"}

        def order(self, *args, **kwargs):
            calls.append(("order", args[0]))
            return {"status": "ok"}

    live = LiveExchange.__new__(LiveExchange)
    live._exchange = Exchange()
    live.place_alo("xyz:SMSN", True, 1.0, 100.0, leverage=5, is_cross=True)
    assert ("order", "xyz:SMSN") in calls
    assert (5, "xyz:SMSN", False) in calls
    calls.clear()
    live.place_alo("BTC", True, 0.01, 100000.0, leverage=40, is_cross=True)
    assert calls[0] == (40, "BTC", True)
    assert calls[1] == ("order", "BTC")

    class Rejected:
        def update_leverage(self, lev, coin, is_cross=True):
            return {"status": "err", "response": "nope"}

        def order(self, *args, **kwargs):
            raise AssertionError("order must not be sent")

    live._exchange = Rejected()
    try:
        live.place_alo("ETH", True, 0.1, 3000.0, leverage=20)
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_guard_fee_uses_the_coin_rate():
    class Live:
        def round_price_toward(self, coin, px, up):
            return px

    guard = PositionGuard(
        Live(),
        InfoClient(),
        user="0xabc",
        dexs=("",),
        fee_for=lambda coin: (0.000288, 0.000864) if coin == "xyz:GOLD" else (MAKER_FEE_RATE, TAKER_FEE_RATE),
    )

    class Pos:
        coin = "xyz:GOLD"
        side = "long"
        size = 1.0
        entry = 4100.0

    fees = guard._fees(Pos(), 4090.0)
    assert fees == 4100.0 * 0.000288 + 4090.0 * 0.000864
    Pos.coin = "BTC"
    btc = guard._fees(Pos(), 4090.0)
    assert btc == 4100.0 * MAKER_FEE_RATE + 4090.0 * TAKER_FEE_RATE


def test_rank_demo_three_armed_setups(capsys):
    """Synthetic stand-in for today's arms. No live journal is in this tree."""
    book = [
        ("BTC", setup_quality(
            side="short", entry=80000, stop=80120, take_profit=79640,
            absorb=1.6, swing=80000, sweep=80040, macro="down", macro_adx=28, atr=150,
        )),
        ("xyz:GOLD", setup_quality(
            side="long", entry=4100, stop=4093.85, take_profit=4112,
            absorb=0.5, swing=4100, sweep=4098, macro="range", macro_adx=12, atr=8,
            maker_fee=MAKER_FEE_RATE * 2, taker_fee=TAKER_FEE_RATE * 2,
        )),
        ("xyz:NVDA", setup_quality(
            side="long", entry=180, stop=178.5, take_profit=184.5,
            absorb=2.2, swing=180, sweep=179.4, macro="up", macro_adx=34, atr=2.0,
            maker_fee=MAKER_FEE_RATE * 2, taker_fee=TAKER_FEE_RATE * 2,
        )),
    ]
    ordered = sorted(book, key=lambda item: -item[1].quality)
    lines = [
        f"{name} quality={q.quality:.3f} tp1_r={q.tp1_r:.2f} absorb={q.absorb:.2f} "
        f"macro_adx={q.macro_adx:.1f} align={q.macro_align:+.0f} stop_atr={q.stop_atr:.2f} "
        f"sweep_bps={q.sweep_bps:.1f} fee_drag={q.fee_drag:.3f}"
        for name, q in ordered
    ]
    print("RANK DEMO\n" + "\n".join(lines))
    assert [name for name, _q in ordered][0] == "xyz:NVDA"
    assert ordered[0][1].quality > ordered[1][1].quality > ordered[2][1].quality
    assert "xyz:GOLD" in {name for name, _q in ordered}

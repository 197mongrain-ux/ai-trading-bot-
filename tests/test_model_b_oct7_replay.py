"""Oct 7 setups under tomorrow's (data-collection day) config.

Real Hyperliquid 1m candles before each arm; the 90s tape is rebuilt from
the journal's own numbers for that arm (swing, sweep, absorb, window delta,
last-15s delta) plus the most one-sided 90s delta the bot logged in the
5 minutes before it (counter-flow input).

Tomorrow's config: STRUCTURE shadow (log only), COUNTER_FLOW on, min stop
15 bps, SHALLOW_SWEEP 0.3 bps, no HTF requirement, 20x notional cap, 2%
hard loss cap. Every Oct 7 setup that armed must still arm, with the same
stop; the 22:56 BTC arm must be rejected (or capped to <= 2% loss).
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest

from hl_bot.config import load_settings
from hl_bot.execution.model_b_loop import build_model_b_engine
from hl_bot.strategy.model_b.pools import pools_from_bars
from hl_bot.strategy.model_b.types import Pool, TradePrint

FIX = pathlib.Path(__file__).parent / "fixtures"

TOMORROW_ENV = """
TRADING_MODE=paper
ENTRY_MODE=model_b
HL_NETWORK=mainnet
SYMBOLS=BTC,ETH,SOL,PUMP,xyz:XYZ100
RISK_PER_TRADE=0.02
LEVERAGE=20
MODEL_B_TP_R=1.67
MODEL_B_DELTA_FLAT_EPS=0.05
MODEL_B_DELTA_FLAT_USDC=100
MODEL_B_STRUCTURE_FILTER=shadow
MODEL_B_COUNTER_FLOW=1
MODEL_B_MIN_STOP_BPS=15
MODEL_B_MIN_SWEEP_BPS=0.3
MODEL_B_SWEEP_REQUIRE_HTF=0
MODEL_B_MAX_LEVERAGE=20
MODEL_B_MAX_LOSS_PCT=0.02
"""

# label, coin, now, side, swing, sweep, absorb, dW, d15, adverse 90s delta
# (signed coin units, 5 min before), tick, leverage, logged stop
SETUPS = [
    ("ETH L 10-06 23:47", "ETH", 1791344876.02, "long", 2611.0, 2610.8, 4.4968, 1.3034, 0.763, -22.0838, 0.1, 25, 2605.5),
    ("SOL S 10-07 00:26", "SOL", 1791347171.01, "short", 118.22, 118.28, 15.9296, -30.8, -1.57, 431.88, 0.01, 20, 118.53),
    ("PUMP S 10-07 08:04", "PUMP", 1791374649.78, "short", 0.006391, 0.006396, 2.3911, -3638942.0, -515631.0, 6076876.0, 0.000001, 10, 0.00641),
    ("XYZ100 L 10-07 08:16", "xyz:XYZ100", 1791375393.28, "long", 31009.0, 31008.0, 1.7056, 1.2129, 0.179, 0.9982, 1.0, 20, 30954.0),
    ("XYZ100 L 10-07 10:05", "xyz:XYZ100", 1791381941.25, "long", 30951.0, 30950.0, 2.4470, 1.4052, 0.9193, -4.0713, 1.0, 20, 30896.0),
    ("XYZ100 L 10-07 12:38", "xyz:XYZ100", 1791391116.54, "long", 31078.0, 31074.0, 3.2619, 0.0411, 0.2955, -0.3669, 1.0, 20, 31020.0),
]
EQUITY = 293.06


@pytest.fixture
def tomorrow(tmp_path):
    env = tmp_path / "tomorrow.env"
    env.write_text(TOMORROW_ENV)
    settings = load_settings(env_file=str(env))
    assert settings.model_b_structure_mode == "shadow"
    assert settings.model_b_structure_filter is False
    assert settings.model_b_counter_flow is True
    assert settings.model_b_min_stop_bps == 15.0
    assert settings.model_b_max_loss_pct == 0.02
    return settings


def _tape(coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick):
    s = 1 if side == "long" else -1
    buy, sell = ("buy", "sell") if side == "long" else ("sell", "buy")
    depth = max(abs(swing - sweep), tick)
    off = max(depth, 3 * tick)  # last trade >= 3 ticks off the swing
    reclaim_sz = max(abs(d15), 1e-9)
    sweep_sz = absorb * reclaim_sz
    lead_sz = max(abs(dw) - reclaim_sz + sweep_sz, 1e-9)
    burst = abs(adverse) if adverse * s < 0 else 0.0
    out = []

    def add(ts, px, sz, sd):
        out.append(
            TradePrint(ts=now + ts, coin=coin, price=round(px / tick) * tick, size=sz, side=sd, seq=len(out))
        )

    for i in range(64 if burst else 0):  # one-sided flow, outside the 90s window
        add(-175 + i, swing + s * 3 * depth, burst / 64, sell)
    for i in range(38):
        add(-90 + i * 1.7, swing + s * (off + tick), lead_sz / 38, buy)
    for i in range(8):
        add(-20 + i * 0.5, swing - s * min(depth, max(depth * (i + 1) / 8, tick)), sweep_sz / 8, sell)
    for i in range(6):
        add(-13 + i * 2, swing + s * off, reclaim_sz / 6, buy)
    return out, swing + s * off


def _bars(coin, now):
    raw = json.loads((FIX / "oct7_winners_1m.json").read_text())["bars"][coin]
    return [{"t": t, "o": o, "h": h, "l": l, "c": c} for t, o, h, l, c in raw if t + 60 <= now]


@pytest.mark.parametrize("setup", SETUPS, ids=[s[0] for s in SETUPS])
def test_oct7_setup_still_arms_under_tomorrow_config(setup, tomorrow, caplog):
    label, coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick, lev, stop = setup
    prints, last = _tape(coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick)
    bars = _bars(coin, now)
    engine = build_model_b_engine(tomorrow, (coin,))
    with caplog.at_level(logging.INFO):
        d = engine.evaluate(
            coin,
            now=now,
            prints=prints,
            bars=bars,
            pools=pools_from_bars(bars, now, last_price=last),
            best_bid=last - tick,
            best_ask=last + tick,
            equity=EQUITY,
            tick=tick,
            leverage=lev,
        )
    assert d.armed, (label, d.fail_reason, d.structure, d.counter_flow)
    i = d.intent
    assert d.swing == swing and d.sweep_price == pytest.approx(sweep)
    assert i.limit_px == pytest.approx(sweep) and i.stop == pytest.approx(stop)
    assert abs(i.limit_px - i.stop) / i.limit_px * 10_000 >= 15.0
    assert i.size * abs(i.limit_px - i.stop) <= 0.02 * EQUITY + 1e-9
    assert i.size * i.limit_px <= 20 * EQUITY + 1e-6
    assert d.counter_flow is not None  # logged on every arm
    if d.structure_shadow == "would_block":
        assert any(
            "MODEL_B SHADOW" in r.getMessage() and "reason=STRUCTURE would_block=1" in r.getMessage()
            for r in caplog.records
        )


def test_structure_shadow_marks_the_trades_it_would_have_blocked(tomorrow):
    marked = []
    for label, coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick, lev, _stop in SETUPS:
        prints, last = _tape(coin, now, side, swing, sweep, absorb, dw, d15, adverse, tick)
        bars = _bars(coin, now)
        d = build_model_b_engine(tomorrow, (coin,)).evaluate(
            coin, now=now, prints=prints, bars=bars,
            pools=pools_from_bars(bars, now, last_price=last),
            best_bid=last - tick, best_ask=last + tick, equity=EQUITY, tick=tick, leverage=lev,
        )
        if d.structure_shadow == "would_block":
            marked.append(label)
    # As "on" it would have blocked the ETH loser and all three XYZ100 longs.
    assert marked == [
        "ETH L 10-06 23:47",
        "XYZ100 L 10-07 08:16",
        "XYZ100 L 10-07 10:05",
        "XYZ100 L 10-07 12:38",
    ]


def test_2256_btc_is_rejected_or_capped_under_tomorrow_config(tomorrow):
    from tests.test_model_b_protection import _replay_world

    now, bars, prints = _replay_world()
    d = build_model_b_engine(tomorrow, ("BTC",)).evaluate(
        "BTC", now=now, prints=prints, bars=bars, pools=[Pool("PDH", 86670.0, False)],
        best_bid=83139.0, best_ask=83141.0, equity=EQUITY, tick=1.0, leverage=40,
    )
    if d.armed:
        i = d.intent
        assert i.size * abs(i.limit_px - i.stop) <= 0.02 * EQUITY + 1e-9
    else:
        assert d.fail_reason == "COUNTER_FLOW"


@pytest.mark.parametrize(
    "raw,mode", [(None, "on"), ("1", "on"), ("on", "on"), ("shadow", "shadow"), ("SHADOW", "shadow"), ("0", "off")]
)
def test_structure_filter_env_modes(raw, mode, monkeypatch):
    from hl_bot.config import _structure_mode

    if raw is None:
        monkeypatch.delenv("MODEL_B_STRUCTURE_FILTER", raising=False)
    else:
        monkeypatch.setenv("MODEL_B_STRUCTURE_FILTER", raw)
    assert _structure_mode() == mode

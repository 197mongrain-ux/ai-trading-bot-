"""xyz 40 bp stop floor, and Model B risk at 1% / 1.5% / 2%.

The Oct 7-8 replay (1m candles, TP walk at 1.5R, path through the last
bar) ranked a 40 bp xyz floor at 2% risk first on expectancy. Shadow is
the default: the edge was +0.05R on 14 trades and it gave back one
earlier winner, which is not enough to turn the flag on by itself.
"""

from __future__ import annotations

import pytest

from hl_bot.config import Settings, load_settings
from hl_bot.execution.model_b_loop import build_model_b_engine
from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.risk import cap_size_to_loss, loss_at_stop, size_from_stop
from hl_bot.strategy.model_b.xyz_min_stop import RECOMMENDED_XYZ_MIN_STOP_BPS, xyz_floor_stop

EQUITY = 244.0


def test_recommended_floor_is_40_bps_and_shadow_by_default(monkeypatch):
    monkeypatch.delenv("MODEL_B_XYZ_MIN_STOP", raising=False)
    monkeypatch.delenv("MODEL_B_XYZ_MIN_STOP_BPS", raising=False)
    settings = load_settings()
    assert settings.model_b_xyz_min_stop == "shadow"
    assert settings.model_b_xyz_min_stop_bps == pytest.approx(40.0)
    assert RECOMMENDED_XYZ_MIN_STOP_BPS == pytest.approx(40.0)
    engine = build_model_b_engine(settings, ("BTC", "xyz:XYZ100"))
    assert engine.xyz_min_stop == "shadow"
    assert engine.xyz_min_stop_bps == pytest.approx(40.0)


def test_xyz100_spike_needs_30_bps_and_40_has_room():
    """12:17 short, entry 30971, entry-bar high 31061. Tick 1."""
    entry, spike, tick = 30971.0, 31061.0, 1.0
    live = 31027.0
    at_25 = xyz_floor_stop("short", entry, live, tick, "xyz:XYZ100", 25)
    at_30 = xyz_floor_stop("short", entry, live, tick, "xyz:XYZ100", 30)
    at_40 = xyz_floor_stop("short", entry, live, tick, "xyz:XYZ100", 40)
    assert at_25 is not None and at_25 < spike  # still inside the spike
    assert at_30 == pytest.approx(31064.0)
    assert at_30 > spike  # 3 pts, no cushion
    assert at_40 == pytest.approx(31095.0)
    assert at_40 - spike == pytest.approx(34.0)


def test_skhx_40_bps_does_not_clear_the_rally():
    """Stop 1198.8, price traded 1209. 40 bp is 1201.5. TP 1193 was not the save."""
    widened = xyz_floor_stop("short", 1196.7, 1198.8, 0.1, "xyz:SKHX", 40)
    assert widened == pytest.approx(1201.5)
    assert widened < 1209.0


def test_floor_only_loosens_and_skips_main_and_a_wider_stop():
    assert xyz_floor_stop("long", 82618.0, 82476.0, 1.0, "BTC", 40) is None
    # 19.6 bp GOLD stop is inside 40 bp, so it loosens. A 50 bp stop does not.
    assert xyz_floor_stop("short", 4138.5, 4146.6, 0.1, "xyz:GOLD", 40) == pytest.approx(4155.1)
    assert xyz_floor_stop("short", 4138.5, 4159.0, 0.1, "xyz:GOLD", 40) is None
    assert xyz_floor_stop("short", 30971.0, 31027.0, 1.0, "xyz:XYZ100", 0) is None


def test_model_b_accepts_one_and_one_point_five_percent_and_rejects_half_percent():
    Settings(entry_mode="model_b", risk_per_trade=0.02, leverage=20).validate()
    Settings(entry_mode="model_b", risk_per_trade=0.015, leverage=20).validate()
    Settings(entry_mode="model_b", risk_per_trade=0.01, leverage=20).validate()
    with pytest.raises(ValueError, match="RISK_PER_TRADE"):
        Settings(entry_mode="model_b", risk_per_trade=0.005, leverage=20).validate()
    with pytest.raises(ValueError, match="RISK_PER_TRADE"):
        Settings(entry_mode="model_b", risk_per_trade=0.025, leverage=20).validate()
    with pytest.raises(ValueError, match="MODEL_B_XYZ_MIN_STOP_BPS"):
        Settings(
            entry_mode="model_b",
            risk_per_trade=0.02,
            leverage=20,
            model_b_xyz_min_stop_bps=250,
        ).validate()


def _sized(coin, entry, stop, risk, slip):
    fees = conservative_fees(coin)
    size, _ = size_from_stop(
        EQUITY,
        entry,
        stop,
        risk_pct=risk,
        leverage=20,
        notional_leverage=20,
        min_stop_bps=15.0,
        include_fees=True,
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip_bps=slip,
    )
    size = cap_size_to_loss(
        size,
        entry,
        stop,
        EQUITY,
        include_fees=True,
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip_bps=slip,
    )
    return size


def test_size_table_on_244_equity_keeps_the_20x_cap():
    """A 4 bp BTC stop with the 15 bp brake stays under 20x. xyz 40 bp scales with risk."""
    btc_entry = 80_000.0
    btc_stop = btc_entry * (1.0 - 0.0004)
    for risk in (0.02, 0.015, 0.01):
        size = _sized("BTC", btc_entry, btc_stop, risk, 5.0)
        # min_stop_bps 15 sizes the 4 bp stop as 15 bp + 5 bp slip, under 20x.
        assert size * btc_entry < EQUITY * 20.0
        assert loss_at_stop(
            size, btc_entry, btc_stop,
            maker_fee=conservative_fees("BTC").maker,
            taker_fee=conservative_fees("BTC").taker,
            slip_bps=5.0,
        ) <= 0.02 * EQUITY + 1e-6

    # Fees turn a 4 bp stop into ~10 bp of sizing distance. 2% is the 20x
    # cap. 1.5% and 1% sit under it, so on this one case a smaller risk
    # does shrink the ticket. A 4 bp stop with no fee in the distance is
    # 25x at 1% and would still be the 20x cap — see the 10_000 test.
    raw = {}
    for risk in (0.02, 0.015, 0.01):
        size, _ = size_from_stop(
            EQUITY, btc_entry, btc_stop, risk_pct=risk, leverage=20, notional_leverage=20,
            include_fees=True,
        )
        size = cap_size_to_loss(size, btc_entry, btc_stop, EQUITY, include_fees=True)
        raw[risk] = size
        assert size * btc_entry <= EQUITY * 20.0 + 1e-6
        assert loss_at_stop(size, btc_entry, btc_stop, include_fees=True) <= 0.02 * EQUITY + 1e-6
    assert raw[0.02] * btc_entry == pytest.approx(EQUITY * 20.0, rel=1e-4)
    assert raw[0.015] < raw[0.02]
    assert raw[0.01] < raw[0.015]

    gold_live = _sized("xyz:GOLD", 4138.5, 4146.6, 0.02, 20.0)
    gold_40 = _sized("xyz:GOLD", 4138.5, 4155.1, 0.02, 20.0)
    assert gold_40 < gold_live
    assert gold_40 * 4138.5 < EQUITY * 20.0

    skhx_40 = _sized("xyz:SKHX", 1196.7, 1201.5, 0.02, 20.0)
    skhx_15 = _sized("xyz:SKHX", 1196.7, 1201.5, 0.015, 20.0)
    skhx_10 = _sized("xyz:SKHX", 1196.7, 1201.5, 0.01, 20.0)
    assert skhx_10 < skhx_15 < skhx_40
    # 40 bp + 20 bp slip is about 5x, so the dollars scale with risk.
    assert skhx_15 / skhx_40 == pytest.approx(0.015 / 0.02, rel=1e-3)
    assert skhx_10 / skhx_40 == pytest.approx(0.01 / 0.02, rel=1e-3)

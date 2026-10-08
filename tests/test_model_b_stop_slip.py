"""Stop-market slip in the 2% size, and the 15m swing the stop sat under.

xyz:SKHX short, Oct 8 2026, PR #15 runner on. Trigger 1198.8 filled at
1200.6 (1.8 pts, 15.0 bps of the 1196.7 entry). Closed PnL -6.72 plus
about $0.24 of fees was about -2.8% of a ~$251 account. The planned
risk was 2.1 points.

Default MODEL_B_STOP_SLIP=shadow does not change the posted size. With
the flag on, loss at the trigger plus the allowance plus fees is <= 2%,
and notional stays <= 20x. MODEL_B_HTF_STOP=shadow only reports when the
stop is not beyond the nearest 15m swing plus the usual buffer.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.htf_stop import check_htf_stop
from hl_bot.strategy.model_b.risk import (
    cap_size_to_loss,
    loss_at_stop,
    size_from_stop,
    stop_buffer,
)
from hl_bot.strategy.model_b.stop_slip import (
    MAIN_STOP_SLIP_BPS,
    XYZ_STOP_SLIP_BPS,
    resolve_stop_slip_bps,
)
from hl_bot.strategy.model_b.swings import atr14

# Reconstructed from the fill: 1.723 * (1200.6 - entry) = 6.72, and
# 1198.8 - entry = 2.1.
SKHX_ACCOUNT = 251.0
SKHX_ENTRY = 1196.7
SKHX_STOP = 1198.8
SKHX_FILL = 1200.6
SKHX_SIZE = 1.723
SKHX_TICK = 0.1
FIX = pathlib.Path(__file__).parent / "fixtures"


def _size(entry, stop, equity, *, slip_bps, maker, taker, min_stop_bps=0.0):
    size, _dollar = size_from_stop(
        equity,
        entry,
        stop,
        risk_pct=0.02,
        leverage=20,
        notional_leverage=20,
        min_stop_bps=min_stop_bps,
        include_fees=True,
        maker_fee=maker,
        taker_fee=taker,
        slip_bps=slip_bps,
    )
    return cap_size_to_loss(
        size,
        entry,
        stop,
        equity,
        include_fees=True,
        maker_fee=maker,
        taker_fee=taker,
        slip_bps=slip_bps,
    )


def test_slip_and_htf_stop_default_to_shadow():
    from hl_bot.config import load_settings

    settings = load_settings()
    assert settings.model_b_stop_slip == "shadow"
    assert settings.model_b_htf_stop == "shadow"
    assert settings.model_b_stop_slip_bps == ""


def test_xyz_slip_floor_is_above_the_skhx_print_and_a_tight_book_does_not_cut_it():
    observed = (SKHX_FILL - SKHX_STOP) / SKHX_ENTRY * 10_000.0
    assert observed == pytest.approx(15.04, abs=0.05)
    bps, source = resolve_stop_slip_bps("xyz:SKHX")
    assert bps == XYZ_STOP_SLIP_BPS == 20.0
    assert source == "xyz-default"
    assert bps > observed
    # One tick on SKHX is under 1 bp. It must not replace the 20 bp floor.
    tight, src = resolve_stop_slip_bps(
        "xyz:SKHX",
        best_bid=SKHX_ENTRY,
        best_ask=SKHX_ENTRY + SKHX_TICK,
        entry=SKHX_ENTRY,
    )
    assert tight == 20.0 and src == "xyz-default"
    wide, src = resolve_stop_slip_bps(
        "xyz:SKHX",
        best_bid=SKHX_ENTRY,
        best_ask=SKHX_ENTRY * (1 + 30 / 10_000.0),
        entry=SKHX_ENTRY,
    )
    assert wide == pytest.approx(30.0) and src == "book"
    main, src = resolve_stop_slip_bps("BTC")
    assert main == MAIN_STOP_SLIP_BPS == 5.0 and src == "main-default"
    named, src = resolve_stop_slip_bps("xyz:SKHX", "xyz:SKHX=15,xyz=20,default=5")
    assert named == 15.0 and src == "coin"


def test_skhx_slip_on_keeps_trigger_plus_slip_plus_fees_inside_2pct():
    """The size SKHX would have had with the xyz fee table and 20 bp slip.

    Shadow posts the no-slip size. ``on`` posts the slipped size. Both
    stay inside 20x. The live 1.723, marked to the 1200.6 fill, does not.
    """
    fees = conservative_fees("xyz:SKHX")
    maker, taker = fees.maker, fees.taker
    base = _size(SKHX_ENTRY, SKHX_STOP, SKHX_ACCOUNT, slip_bps=0.0, maker=maker, taker=taker)
    slipped = _size(
        SKHX_ENTRY, SKHX_STOP, SKHX_ACCOUNT, slip_bps=20.0, maker=maker, taker=taker
    )
    assert slipped == pytest.approx(0.846353)
    assert base == pytest.approx(1.418908)
    assert slipped < base < SKHX_SIZE
    for size in (base, slipped):
        assert size * SKHX_ENTRY <= 20.0 * SKHX_ACCOUNT + 1e-6
    assert loss_at_stop(
        base, SKHX_ENTRY, SKHX_STOP, maker_fee=maker, taker_fee=taker
    ) <= 0.02 * SKHX_ACCOUNT + 1e-6
    assert loss_at_stop(
        slipped, SKHX_ENTRY, SKHX_STOP, maker_fee=maker, taker_fee=taker, slip_bps=20.0
    ) <= 0.02 * SKHX_ACCOUNT + 1e-6
    # The actual fill is 15 bp of slip, inside the 20 bp allowance.
    assert loss_at_stop(
        slipped, SKHX_ENTRY, SKHX_FILL, maker_fee=maker, taker_fee=taker
    ) <= 0.02 * SKHX_ACCOUNT + 1e-6
    live_loss = SKHX_SIZE * (SKHX_FILL - SKHX_ENTRY) + 0.24
    assert live_loss > 0.02 * SKHX_ACCOUNT


def test_slip_does_not_push_a_4bp_btc_or_15bp_gold_through_20x_or_2pct():
    """Same 2% and 20x brakes as PR #16. Slip only shrinks.

    Without slip, the 4 bp BTC stop is still the 20x cap. With the 5 bp
    main allowance on, that stop is sized off 4 bp + 5 bp + fees, so
    notional falls to about 13.3x and the loss at the trigger is under
    2% (the rest of the 2% is the slip allowance). GOLD's 15 bp stop
    plus 20 bp of xyz slip stays under 20x and inside 2% including slip,
    and is smaller than the no-slip coin-fee size.
    """
    equity = 10_000.0
    btc_entry = 80_000.0
    btc_stop = btc_entry * (1.0 - 0.0004)
    btc_fees = conservative_fees("BTC")
    btc_plain = _size(
        btc_entry,
        btc_stop,
        equity,
        slip_bps=0.0,
        maker=btc_fees.maker,
        taker=btc_fees.taker,
    )
    assert btc_plain * btc_entry == pytest.approx(equity * 20.0, rel=1e-6)
    btc = _size(
        btc_entry,
        btc_stop,
        equity,
        slip_bps=MAIN_STOP_SLIP_BPS,
        maker=btc_fees.maker,
        taker=btc_fees.taker,
    )
    assert btc * btc_entry == pytest.approx(133_349.28, abs=1.0)
    assert btc * btc_entry <= equity * 20.0 + 1e-6
    assert loss_at_stop(
        btc,
        btc_entry,
        btc_stop,
        maker_fee=btc_fees.maker,
        taker_fee=btc_fees.taker,
        slip_bps=MAIN_STOP_SLIP_BPS,
    ) <= 0.02 * equity + 1e-6

    gold_entry = 4_000.0
    gold_stop = gold_entry * (1.0 + 0.0015)  # 15 bps short
    gold_fees = conservative_fees("xyz:GOLD")
    plain = _size(
        gold_entry, gold_stop, equity, slip_bps=0.0, maker=gold_fees.maker, taker=gold_fees.taker
    )
    gold = _size(
        gold_entry,
        gold_stop,
        equity,
        slip_bps=XYZ_STOP_SLIP_BPS,
        maker=gold_fees.maker,
        taker=gold_fees.taker,
    )
    assert gold < plain
    assert gold * gold_entry <= equity * 20.0 + 1e-6
    assert loss_at_stop(
        gold,
        gold_entry,
        gold_stop,
        maker_fee=gold_fees.maker,
        taker_fee=gold_fees.taker,
        slip_bps=XYZ_STOP_SLIP_BPS,
    ) <= 0.02 * equity + 1e-6


def _skhx_bars():
    """1m bars whose 15m resample has a swing high at 1198.9."""
    bars = []
    for bucket in range(7):
        for minute in range(15):
            t = bucket * 900 + minute * 60
            # Other highs sit under the 1196.7 entry so the last closed 15m
            # bar is not an opposing extreme. The 1198.9 print stays the anchor.
            high = 1198.9 if bucket == 2 and minute == 7 else 1196.5
            bars.append({"t": t, "o": 1196.8, "h": high, "l": high - 0.2, "c": 1196.8})
    return bars, 7 * 900


def test_skhx_stop_sat_one_tick_under_the_15m_swing():
    bars, now = _skhx_bars()
    check = check_htf_stop(
        "short", SKHX_ENTRY, SKHX_STOP, bars, now, SKHX_TICK, atr14(bars, now)
    )
    assert check.swing == pytest.approx(1198.9)
    assert check.under_swing
    assert check.inside
    assert SKHX_STOP < check.swing
    assert check.swing - SKHX_STOP == pytest.approx(SKHX_TICK)
    buf = stop_buffer(SKHX_ENTRY, SKHX_TICK, atr14(bars, now))
    assert check.beyond == pytest.approx(1198.9 + buf)
    assert check.beyond > check.swing


def test_nearer_15m_bar_beats_a_far_fractal():
    """SKHX 13:45 high is not a width-2 fractal. The fractal was ~1234.

    The stop check uses the closer of the two, so a 3% fractal does not
    become the stop when the last closed 15m high is 1198.9.
    """
    bars = []
    for bucket in range(8):
        for minute in range(15):
            t = bucket * 900 + minute * 60
            if bucket == 2:
                high = 1234.3
            elif bucket == 7:
                high = 1198.9
            else:
                high = 1190.0
            bars.append({"t": t, "o": 1196.0, "h": high, "l": high - 0.4, "c": 1196.0})
    now = 8 * 900
    check = check_htf_stop(
        "short", SKHX_ENTRY, SKHX_STOP, bars, now, SKHX_TICK, atr14(bars, now)
    )
    assert check.swing == pytest.approx(1198.9)
    assert check.under_swing
    assert check.inside
    # beyond is the swing plus the buffer, snapped out to the next tick.
    assert check.beyond + 1e-9 >= 1198.9 + check.buffer
    assert check.beyond < 1198.9 + check.buffer + SKHX_TICK + 1e-9


def test_oct7_replay_htf_stop_frequency():
    """How often the armed stop was not beyond the nearest 15m swing + buffer.

    The Oct 7 replay book is the six armed winners in the fixture. The
    count is locked so a later change to the swing rule shows up here.
    """
    from tests.test_model_b_oct7_replay import SETUPS

    raw = json.loads((FIX / "oct7_winners_1m.json").read_text())["bars"]
    inside = []
    under = []
    checked = 0
    for label, coin, now, side, _swing, sweep, *_rest, tick, _lev, stop in (
        (*setup[:10], setup[10], setup[11], setup[12]) for setup in SETUPS
    ):
        bars = [
            {"t": t, "o": o, "h": h, "l": l, "c": c}
            for t, o, h, l, c in raw[coin]
            if t + 60 <= now
        ]
        check = check_htf_stop(side, sweep, stop, bars, now, tick, atr14(bars, now))
        checked += 1
        if check.inside:
            inside.append((label, check.swing, stop, check.beyond, check.under_swing))
        if check.under_swing:
            under.append(label)
    assert checked == 6
    # Measured on tests/fixtures/oct7_winners_1m.json. "Inside" is the
    # nearer of the 15m fractal and the last closed 15m extreme, plus
    # the buffer. The old fractal-only read marked ETH, SOL, and PUMP
    # inside because a far fractal sat past a stop that had already
    # cleared the last 15m bar. Those three are not inside anymore.
    # The three XYZ100 stops were already beyond either reading. The
    # SKHX one-tick miss is the synthetic test above; it is not in this book.
    assert inside == []
    assert under == []

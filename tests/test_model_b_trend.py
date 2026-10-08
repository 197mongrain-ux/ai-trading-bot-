"""Trend read for the TP too-far rule (trend.py)."""

from __future__ import annotations

import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from hl_bot.strategy.model_b.trend import TfTrend, TrendRead, read_trend, tf_trend

FIX = pathlib.Path(__file__).parent / "fixtures" / "oct8_trend_1m.json"
ET = ZoneInfo("America/Toronto")


def _bars(coin, now):
    raw = json.loads(FIX.read_text())["bars"][coin]
    return [{"t": t, "o": o, "h": h, "l": l, "c": c} for t, o, h, l, c in raw if t / 1000 + 60 <= now]


def _et(hh, mm, day=8):
    return datetime(2026, 10, day, hh, mm, tzinfo=ET).timestamp()


def _zigzag(points, start=1_790_000_000.0, step=900):
    """1m bars that trace straight lines between 15m pivot prices."""
    bars, t = [], start
    for a, b in zip(points, points[1:]):
        for i in range(15 * 4):  # 4 x 15m candles per leg
            px = a + (b - a) * (i + 1) / 60
            bars.append({"t": t * 1000, "o": px, "h": px + 0.05, "l": px - 0.05, "c": px})
            t += 60
    return bars, t


def test_btc_morning_lower_highs_read_down_on_15m():
    # Oct 8: 15m highs 83259 -> 83147 -> 82621, lows 82267 -> 82191 -> 81875.
    for hh, mm in ((7, 45), (8, 45), (9, 20), (10, 0), (10, 20)):
        now = _et(hh, mm)
        assert tf_trend(_bars("BTC", now), "15m", now).state == "down", (hh, mm)
    # 10:15 candle closed 82707, above the 82621 swing high: structure broke.
    # It stays broken (no flip back to "down" on the next lower close).
    for hh, mm in ((10, 31), (10, 44)):
        now = _et(hh, mm)
        r = tf_trend(_bars("BTC", now), "15m", now)
        assert r.state == "range" and r.broke == "closed_above_last_high", (hh, mm)


def test_gold_short_at_10_30_reads_15m_up():
    now = _et(10, 30)
    read = read_trend(_bars("xyz:GOLD", now), now)
    assert read.state("15m") == "up"
    assert read.with_side() in ("long", "none")
    assert not read.is_with("short")


def test_synthetic_up_down_range():
    up, t = _zigzag([100, 104, 102, 106, 104, 108, 106, 110, 108, 109])
    assert tf_trend(up, "15m", t).state == "up"
    down, t = _zigzag([110, 106, 108, 104, 106, 102, 104, 100, 102, 101])
    assert tf_trend(down, "15m", t).state == "down"
    # Expanding: higher highs but lower lows -> range.
    rng, t = _zigzag([100, 104, 101, 105, 100, 106, 99, 107, 98, 101])
    assert tf_trend(rng, "15m", t).state == "range"


def test_close_through_last_swing_is_a_break_not_a_wick():
    bars, t = _zigzag([100, 104, 102, 106, 104, 108, 106, 110, 108, 109])
    base = tf_trend(bars, "15m", t)
    assert base.state == "up"
    last_low = base.lows[-1]
    # A wick under the last swing low (the sweep) keeps the read.
    wick = dict(bars[-1])
    wick["l"] = last_low - 3
    assert tf_trend(bars[:-1] + [wick], "15m", t).state == "up"
    # A full 15m candle closing under it downgrades to range.
    more = []
    for i in range(15):
        px = last_low - 1
        more.append({"t": t * 1000 + i * 60_000, "o": px, "h": px + 0.05, "l": px - 0.05, "c": px})
    broken = tf_trend(bars + more, "15m", t + 900)
    assert broken.state == "range" and broken.broke == "closed_below_last_low"


def test_with_side_rules():
    def read(a, b):
        return TrendRead((TfTrend("15m", a), TfTrend("1h", b)))

    assert read("up", "range").with_side() == "long"
    assert read("range", "down").with_side() == "short"
    assert read("up", "down").with_side() == "none"
    assert read("range", "range").with_side() == "none"
    assert read("unknown", "unknown").with_side() == "none"
    assert read("down", "down").is_with("short") and not read("down", "down").is_with("long")
    assert read("up", "range").label().startswith("15m=up 1h=range ")


def test_not_enough_history_is_unknown():
    bars, t = _zigzag([100, 101])
    assert tf_trend(bars, "1h", t).state == "unknown"

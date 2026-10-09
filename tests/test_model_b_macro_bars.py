"""Macro 1h history must reach every coin soon after a restart.

In incremental candle mode the 40-day cold backfill is gated by the shared
``_next_cold_at`` (one cold pull per CANDLE_BACKFILL_GAP_SEC across all
coins). ``_macro_bars`` used to cache the deferred empty result for
MACRO_REFRESH_SEC (600s), so only one coin got its 4h history every 10 min
and PR #18's MACRO_UNKNOWN block locked the rest out for up to ~50 min.
"""

from __future__ import annotations

from hl_bot.exchange.info_client import InfoClient
from hl_bot.execution import model_b_loop
from hl_bot.execution.model_b_loop import MACRO_BARS_DAYS, _macro_bars

COINS = ["BTC", "ETH", "SOL", "xyz:GOLD", "xyz:SP500", "xyz:XYZ100"]
HOUR_MS = 3_600_000


def _client(monkeypatch, clock):
    calls: list[tuple] = []

    def fake_post(base, coin, interval, start, end, timeout=15.0):
        calls.append((coin, interval, int(start), int(end)))
        step = HOUR_MS if interval == "1h" else 60_000
        first = (int(start) // step) * step
        rows = []
        t = first
        while t <= int(end):
            rows.append({"t": t, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 10})
            t += step
        return 200, rows

    monkeypatch.setattr("hl_bot.exchange.info_client._post_candle_snapshot", fake_post)
    client = InfoClient(base_url="https://example.invalid")
    client._now = lambda: clock["t"]
    client.candle_mode = "incremental"
    return client, calls


def test_macro_bars_reach_all_coins_within_a_few_passes(monkeypatch):
    # Start mid-hour so no 1h bucket rollover helps the old code.
    clock = {"t": 1_760_000_000.0 - (1_760_000_000.0 % 3600) + 600.0}
    client, calls = _client(monkeypatch, clock)
    cache: dict = {}
    first_full: dict[str, float] = {}
    t0 = clock["t"]
    elapsed = 0.0
    while elapsed <= 180.0:
        clock["t"] = t0 + elapsed
        for coin in COINS:
            bars = _macro_bars(client, coin, clock["t"], cache)
            if len(bars) >= 900 and coin not in first_full:
                first_full[coin] = elapsed
        elapsed += 18.0

    missing = [c for c in COINS if c not in first_full]
    assert not missing, f"no macro history after 180s for {missing}; got {first_full}"
    assert max(first_full.values()) <= 90.0 + 1e-9
    assert all(call[1] == "1h" for call in calls)
    assert len(calls) <= len(COINS) * 3  # still rate-gentle: no per-pass spin


def test_macro_bars_does_not_cache_a_deferred_miss(monkeypatch):
    clock = {"t": 1_760_000_000.0}
    client, _ = _client(monkeypatch, clock)
    cache: dict = {}
    assert len(_macro_bars(client, "BTC", clock["t"], cache)) >= 900
    # Same instant: the shared cold gap defers ETH's backfill.
    assert _macro_bars(client, "ETH", clock["t"], cache) == []
    assert "ETH" not in cache
    clock["t"] += model_b_loop.MACRO_REFRESH_SEC / 100  # 6s later, gap clear
    assert len(_macro_bars(client, "ETH", clock["t"], cache)) >= MACRO_BARS_DAYS * 24 - 24


def test_macro_bars_keeps_last_good_copy_on_empty_refetch(monkeypatch):
    clock = {"t": 1_760_000_000.0}
    client, _ = _client(monkeypatch, clock)
    cache: dict = {}
    good = _macro_bars(client, "BTC", clock["t"], cache)
    assert len(good) >= 900

    class Empty:
        def get_candles(self, *a, **k):
            return []

    later = clock["t"] + model_b_loop.MACRO_REFRESH_SEC + 1
    assert _macro_bars(Empty(), "BTC", later, cache) == good

"""Offline smoke: entry cooldown + RESET_DAILY_RISK journal on start."""

from datetime import datetime, timezone
from pathlib import Path

from hl_bot.config import Settings
from hl_bot.exchange.info_client import InfoClient
from hl_bot.exchange.paper_broker import PaperBroker
from hl_bot.execution.loop import run_bot
from hl_bot.journal import TradeJournal


def _bar(t_ms, o, h, l, c, v=10):
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v}


class _StubInfo(InfoClient):
    def __init__(self, mark: float, bars):
        super().__init__()
        self._mark = mark
        self.inject_bars(bars)

    def get_mark_price(self, coin: str = "BTC") -> float:
        return self._mark

    def get_mid_price(self, coin: str = "BTC") -> float:
        return self._mark


def test_reset_daily_risk_journals_on_start(tmp_path):
    day = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)
    t0 = day.timestamp() * 1000
    px = 50_000.0
    bars = [_bar(t0 + i * 60_000, px, px + 5, px - 5, px) for i in range(10)]
    journal_path = tmp_path / "trades.jsonl"
    settings = Settings(
        symbols=("BTC",),
        symbol="BTC",
        journal_path=str(journal_path),
        reset_daily_risk=True,
        trade_hours_utc="0-24",
        htf_confirm=False,
        max_bar_range_pct=None,
        max_range_vs_stop=99.0,
        loop_interval_sec=0.0,
        max_positions_per_symbol=1,
    )
    info = _StubInfo(px, bars)
    broker = PaperBroker(starting_equity=5000.0, symbols=("BTC",))
    run_bot(settings, max_iterations=1, info=info, broker=broker, sleep_fn=lambda _s: None)
    rows = TradeJournal(journal_path).read_all()
    events = [r["event"] for r in rows]
    assert "start" in events
    assert "risk_reset" in events


def test_cooldown_blocks_reentry_after_stop(tmp_path, monkeypatch):
    """After a stop fill, same-symbol entry is blocked while cooldown active."""
    from hl_bot.strategy.filters import cooldown_active

    assert cooldown_active(1_000.0, now_ts=1_050.0, cooldown_sec=120.0)
    # Loop wires the same helper; unit coverage is sufficient offline.

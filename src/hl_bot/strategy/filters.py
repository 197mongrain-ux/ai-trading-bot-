"""Entry accuracy filters for the VWAP micro-breakout scalp.

Pure functions so strategy and unit tests can inject bars / clocks offline.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_trade_hours(raw: str | None) -> tuple[int, int] | None:
    """Parse ``TRADE_HOURS_UTC`` as START-END (start inclusive, end exclusive).

    Returns ``None`` when the filter is disabled (empty, ``0-24``, or ``0-0``
    with empty meaning). Examples:
      - ``12-23`` → (12, 23)  # 12:00–22:59 UTC
      - ``0-24`` / ``""`` / ``None`` → disabled (always allow)
      - ``22-6`` → (22, 6) wrap across midnight
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if "-" not in text:
        raise ValueError(f"TRADE_HOURS_UTC must be START-END, got {raw!r}")
    start_s, end_s = text.split("-", 1)
    start, end = int(start_s.strip()), int(end_s.strip())
    if start == 0 and end == 24:
        return None  # disabled — all hours
    if not (0 <= start <= 23) or not (0 <= end <= 24):
        raise ValueError(f"TRADE_HOURS_UTC hours out of range: {raw!r}")
    if start == end:
        return None  # treat empty window as disabled
    return start, end


def in_trade_hours(
    now: datetime | None = None,
    *,
    hours: tuple[int, int] | None = None,
    hours_raw: str | None = None,
) -> bool:
    """True if UTC hour is inside the configured session window."""
    window = hours if hours is not None else parse_trade_hours(hours_raw)
    if window is None:
        return True
    start, end = window
    n = now or datetime.now(timezone.utc)
    if n.tzinfo is None:
        n = n.replace(tzinfo=timezone.utc)
    else:
        n = n.astimezone(timezone.utc)
    hour = n.hour
    if start < end:
        return start <= hour < end
    # wrap e.g. 22-6 → 22,23,0,1,2,3,4,5
    return hour >= start or hour < end


def bar_range_pct(bar: dict[str, float]) -> float:
    """(high - low) / close for one bar; 0 if close <= 0."""
    h = float(bar.get("h", bar.get("c", 0)))
    l = float(bar.get("l", bar.get("c", 0)))
    c = float(bar.get("c", 0))
    if c <= 0:
        return 0.0
    return max(0.0, (h - l) / c)


def avg_range_pct(bars: list[dict[str, float]], n: int = 5) -> float | None:
    """Average (high-low)/close over the last ``n`` bars (includes latest)."""
    if n < 1 or len(bars) < 1:
        return None
    window = bars[-n:]
    if not window:
        return None
    return sum(bar_range_pct(b) for b in window) / len(window)


def volatility_block(
    bars: list[dict[str, float]],
    *,
    stop_pct: float,
    max_range_vs_stop: float = 1.0,
    lookback: int = 5,
    max_bar_range_pct: float | None = 0.003,
) -> str | None:
    """Return flat reason if noise is too high vs stop, else None.

    - ``vol_too_high``: avg range over lookback >= max_range_vs_stop * stop_pct
    - ``bar_too_wide``: last bar range > max_bar_range_pct (if set > 0)
    """
    if not bars or stop_pct <= 0:
        return None

    last = bar_range_pct(bars[-1])
    if max_bar_range_pct is not None and max_bar_range_pct > 0 and last > max_bar_range_pct:
        return "bar_too_wide"

    avg = avg_range_pct(bars, lookback)
    if avg is not None and avg >= max_range_vs_stop * stop_pct:
        return "vol_too_high"

    return None


def aggregate_bars(
    bars: list[dict[str, float]],
    interval_minutes: int = 5,
) -> list[dict[str, float]]:
    """Aggregate 1m (or finer) OHLCV bars into higher-timeframe candles."""
    if interval_minutes < 1 or not bars:
        return list(bars)
    bucket_ms = interval_minutes * 60_000
    buckets: dict[int, dict[str, float]] = {}
    order: list[int] = []
    for b in bars:
        t = float(b.get("t", 0))
        key = int(t // bucket_ms) * bucket_ms
        o = float(b.get("o", b.get("c", 0)))
        h = float(b.get("h", b.get("c", 0)))
        l = float(b.get("l", b.get("c", 0)))
        c = float(b.get("c", 0))
        v = float(b.get("v", 0))
        if key not in buckets:
            buckets[key] = {"t": float(key), "o": o, "h": h, "l": l, "c": c, "v": v}
            order.append(key)
        else:
            agg = buckets[key]
            agg["h"] = max(agg["h"], h)
            agg["l"] = min(agg["l"], l)
            agg["c"] = c
            agg["v"] = agg["v"] + v
    return [buckets[k] for k in order]


def parse_interval_minutes(interval: str) -> int:
    """Parse ``1m`` / ``5m`` / ``15m`` / ``1h`` into minutes."""
    text = (interval or "5m").strip().lower()
    if text.endswith("h"):
        return int(text[:-1] or "1") * 60
    if text.endswith("m"):
        return int(text[:-1] or "5")
    return int(text)


def htf_vwap_blocks(
    side: str,
    mark: float,
    htf_bars: list[dict[str, float]],
    *,
    reset_utc_hour: int = 0,
    now_ms: float | None = None,
) -> bool:
    """True if HTF session VWAP bias disagrees with ``side`` (long/short)."""
    from hl_bot.strategy.vwap import session_vwap

    if side not in ("long", "short") or mark <= 0 or not htf_bars:
        return False
    vwap = session_vwap(htf_bars, reset_utc_hour=reset_utc_hour, now_ms=now_ms)
    if vwap is None:
        return False
    if side == "long" and mark <= vwap:
        return True
    if side == "short" and mark >= vwap:
        return True
    return False


def cooldown_active(
    last_stop_ts: float | None,
    *,
    now_ts: float | None = None,
    cooldown_sec: float = 120.0,
) -> bool:
    """True if still inside post-stop-out cooldown for a symbol."""
    if cooldown_sec <= 0 or last_stop_ts is None:
        return False
    import time

    now = time.time() if now_ts is None else now_ts
    return (now - last_stop_ts) < cooldown_sec

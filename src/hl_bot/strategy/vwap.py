"""Session VWAP trend scalp strategy."""

from __future__ import annotations

from datetime import datetime, timezone

from hl_bot.strategy.base import Signal


def session_vwap(
    bars: list[dict[str, float]],
    *,
    reset_utc_hour: int = 0,
    now_ms: float | None = None,
) -> float | None:
    """Compute session VWAP from bars since last UTC reset hour.

    Typical bar keys: t (ms), o, h, l, c, v.
    Uses typical price (h+l+c)/3 * volume when volume > 0; else equal-weight close.
    """
    if not bars:
        return None

    if now_ms is None:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000

    now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    reset = now_dt.replace(hour=reset_utc_hour, minute=0, second=0, microsecond=0)
    if now_dt < reset:
        # before today's reset hour → use previous day's reset
        from datetime import timedelta

        reset = reset - timedelta(days=1)
    reset_ms = reset.timestamp() * 1000

    session = [b for b in bars if float(b.get("t", 0)) >= reset_ms]
    if not session:
        session = bars  # fallback: use all provided bars

    cum_pv = 0.0
    cum_v = 0.0
    closes: list[float] = []
    for b in session:
        h = float(b.get("h", b.get("c", 0)))
        l = float(b.get("l", b.get("c", 0)))
        c = float(b.get("c", 0))
        v = float(b.get("v", 0))
        typical = (h + l + c) / 3.0 if (h or l) else c
        closes.append(c)
        if v > 0:
            cum_pv += typical * v
            cum_v += v

    if cum_v > 0:
        return cum_pv / cum_v
    if closes:
        return sum(closes) / len(closes)
    return None


class VwapTrendScalp:
    """Long when mark > VWAP + buffer; short when mark < VWAP - buffer.

    Stop is placed just beyond VWAP (on the other side of VWAP from entry),
    with an optional extra buffer in bps. TP at R-multiple of risk.
    """

    def __init__(
        self,
        buffer_bps: float = 5.0,
        stop_buffer_bps: float = 2.0,
        tp_r_multiple: float = 2.0,
        reset_utc_hour: int = 0,
    ):
        self.buffer_bps = buffer_bps
        self.stop_buffer_bps = stop_buffer_bps
        self.tp_r_multiple = tp_r_multiple
        self.reset_utc_hour = reset_utc_hour

    def on_bar(
        self,
        mark: float,
        bars: list[dict[str, float]],
        *,
        has_position: bool = False,
    ) -> Signal:
        vwap = session_vwap(bars, reset_utc_hour=self.reset_utc_hour)
        if vwap is None or mark <= 0:
            return Signal("flat", mark, 0.0, 0.0, vwap or 0.0, reason="no_vwap")

        if has_position:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="already_in")

        buf = vwap * (self.buffer_bps / 10_000.0)
        stop_buf = vwap * (self.stop_buffer_bps / 10_000.0)

        if mark > vwap + buf:
            # Long: stop just below VWAP
            stop = vwap - stop_buf
            risk = mark - stop
            if risk <= 0:
                return Signal("flat", mark, 0.0, 0.0, vwap, reason="bad_risk")
            tp = mark + risk * self.tp_r_multiple
            return Signal("long", mark, stop, tp, vwap, reason="above_vwap")

        if mark < vwap - buf:
            # Short: stop just above VWAP
            stop = vwap + stop_buf
            risk = stop - mark
            if risk <= 0:
                return Signal("flat", mark, 0.0, 0.0, vwap, reason="bad_risk")
            tp = mark - risk * self.tp_r_multiple
            return Signal("short", mark, stop, tp, vwap, reason="below_vwap")

        return Signal("flat", mark, 0.0, 0.0, vwap, reason="inside_buffer")

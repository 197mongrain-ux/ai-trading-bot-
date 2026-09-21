"""Session VWAP bias + micro breakout scalp strategy.

VWAP is used for directional bias only. Stops are a fixed percent from entry
(not placed at VWAP). Entries require a break of the prior N-bar high/low.

Accuracy filters (all optional / configurable) gate new entries before open:
  1. Volatility / noise vs stop
  2. Session / time-of-day (UTC)
  3. Higher-timeframe VWAP confirmation
"""

from __future__ import annotations

from datetime import datetime, timezone

from hl_bot.strategy.base import Signal
from hl_bot.strategy.filters import (
    aggregate_bars,
    htf_vwap_blocks,
    in_trade_hours,
    parse_interval_minutes,
    parse_trade_hours,
    volatility_block,
)


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


def prior_n_bar_high_low(
    bars: list[dict[str, float]], n: int
) -> tuple[float | None, float | None]:
    """High/low of the prior N completed bars (excludes the latest bar)."""
    if n < 1 or len(bars) < n + 1:
        return None, None
    window = bars[-(n + 1) : -1]
    highs = [float(b.get("h", b.get("c", 0))) for b in window]
    lows = [float(b.get("l", b.get("c", 0))) for b in window]
    if not highs or not lows:
        return None, None
    return max(highs), min(lows)


class VwapTrendScalp:
    """VWAP-bias + micro breakout scalp with fixed-percent stop.

    Bias (VWAP only):
      - long only if mark > session VWAP + buffer
      - short only if mark < session VWAP - buffer
      Buffer defaults to 0 bps (optional small bias buffer).

    Entry (1m bars): break of prior N-bar high (long) / low (short). Default N=3.

    Stop: FIXED percent from entry (default 0.15%). NOT at VWAP.
    TP: TP_R_MULTIPLE * stop distance (default 2.0R).

    Accuracy filters (before open):
      - Volatility: skip if avg bar range >= MAX_RANGE_VS_STOP * STOP_PCT
        or last bar > MAX_BAR_RANGE_PCT
      - Session: only enter when UTC hour in TRADE_HOURS_UTC
      - HTF: long/short must align with HTF session VWAP (default 5m)
    """

    def __init__(
        self,
        buffer_bps: float = 0.0,
        stop_pct: float = 0.0015,
        tp_r_multiple: float = 2.0,
        reset_utc_hour: int = 0,
        breakout_bars: int = 3,
        min_stop_pct: float = 0.0,
        max_stop_pct: float | None = None,
        # Deprecated: ignored — stop is no longer placed at VWAP
        stop_buffer_bps: float = 0.0,
        # --- accuracy filters ---
        max_range_vs_stop: float = 1.0,
        vol_lookback_bars: int = 5,
        max_bar_range_pct: float | None = 0.003,
        trade_hours_utc: str = "12-23",
        htf_confirm: bool = True,
        htf_interval: str = "5m",
    ):
        self.buffer_bps = buffer_bps
        self.stop_pct = stop_pct
        self.tp_r_multiple = tp_r_multiple
        self.reset_utc_hour = reset_utc_hour
        self.breakout_bars = breakout_bars
        self.min_stop_pct = min_stop_pct
        self.max_stop_pct = max_stop_pct
        self.stop_buffer_bps = stop_buffer_bps  # retained for backward compat; unused
        self.max_range_vs_stop = max_range_vs_stop
        self.vol_lookback_bars = vol_lookback_bars
        self.max_bar_range_pct = max_bar_range_pct
        self.trade_hours_utc = trade_hours_utc
        self._trade_hours = parse_trade_hours(trade_hours_utc)
        self.htf_confirm = htf_confirm
        self.htf_interval = htf_interval

    def _stop_distance(self, entry: float) -> float:
        dist = entry * self.stop_pct
        if self.min_stop_pct > 0:
            dist = max(dist, entry * self.min_stop_pct)
        return dist

    def _resolve_htf_bars(
        self,
        bars: list[dict[str, float]],
        htf_bars: list[dict[str, float]] | None,
    ) -> list[dict[str, float]]:
        if htf_bars is not None:
            return htf_bars
        minutes = parse_interval_minutes(self.htf_interval)
        if minutes <= 1:
            return bars
        return aggregate_bars(bars, minutes)

    def on_bar(
        self,
        mark: float,
        bars: list[dict[str, float]],
        *,
        has_position: bool = False,
        now: datetime | None = None,
        htf_bars: list[dict[str, float]] | None = None,
        now_ms: float | None = None,
    ) -> Signal:
        # Resolve clock: explicit now/now_ms, else last bar time (offline tests),
        # else wall clock. Keeps TRADE_HOURS_UTC deterministic with injected bars.
        now_dt = now
        if now_dt is None and now_ms is not None:
            now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
        if now_dt is None and bars:
            last_t = float(bars[-1].get("t", 0) or 0)
            if last_t > 0:
                now_dt = datetime.fromtimestamp(last_t / 1000.0, tz=timezone.utc)
                now_ms = last_t
        if now_ms is None and now_dt is not None:
            now_ms = now_dt.timestamp() * 1000

        vwap = session_vwap(
            bars, reset_utc_hour=self.reset_utc_hour, now_ms=now_ms
        )
        if vwap is None or mark <= 0:
            return Signal("flat", mark, 0.0, 0.0, vwap or 0.0, reason="no_vwap")

        if has_position:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="already_in")

        # --- Session / time-of-day filter (UTC) ---
        if not in_trade_hours(now_dt, hours=self._trade_hours):
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="outside_session")

        # --- Volatility / noise filter ---
        vol_reason = volatility_block(
            bars,
            stop_pct=self.stop_pct,
            max_range_vs_stop=self.max_range_vs_stop,
            lookback=self.vol_lookback_bars,
            max_bar_range_pct=self.max_bar_range_pct,
        )
        if vol_reason:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason=vol_reason)

        buf = vwap * (self.buffer_bps / 10_000.0)
        prior_high, prior_low = prior_n_bar_high_low(bars, self.breakout_bars)

        # Effective stop pct for max-skip check
        eff_stop_pct = self.stop_pct
        if self.min_stop_pct > 0:
            eff_stop_pct = max(eff_stop_pct, self.min_stop_pct)
        if self.max_stop_pct is not None and eff_stop_pct > self.max_stop_pct:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="stop_exceeds_max")

        side: str | None = None
        stop = 0.0
        tp = 0.0
        reason = ""

        # Long: above VWAP bias + break of prior N-bar high
        if mark > vwap + buf:
            if prior_high is None or mark <= prior_high:
                return Signal(
                    "flat", mark, 0.0, 0.0, vwap, reason="no_breakout_long"
                )
            risk = self._stop_distance(mark)
            if risk <= 0:
                return Signal("flat", mark, 0.0, 0.0, vwap, reason="bad_risk")
            side = "long"
            stop = mark - risk
            tp = mark + risk * self.tp_r_multiple
            reason = "vwap_bias_breakout_long"

        # Short: below VWAP bias + break of prior N-bar low
        elif mark < vwap - buf:
            if prior_low is None or mark >= prior_low:
                return Signal(
                    "flat", mark, 0.0, 0.0, vwap, reason="no_breakout_short"
                )
            risk = self._stop_distance(mark)
            if risk <= 0:
                return Signal("flat", mark, 0.0, 0.0, vwap, reason="bad_risk")
            side = "short"
            stop = mark + risk
            tp = mark - risk * self.tp_r_multiple
            reason = "vwap_bias_breakout_short"
        else:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="inside_buffer")

        # --- Higher-timeframe VWAP confirmation ---
        if self.htf_confirm and side in ("long", "short"):
            htf = self._resolve_htf_bars(bars, htf_bars)
            if htf_vwap_blocks(
                side,
                mark,
                htf,
                reset_utc_hour=self.reset_utc_hour,
                now_ms=now_ms,
            ):
                return Signal(
                    "flat", mark, 0.0, 0.0, vwap, reason="htf_vwap_block"
                )

        return Signal(side, mark, stop, tp, vwap, reason=reason)  # type: ignore[arg-type]

"""Session VWAP bias + micro breakout scalp strategy (+ optional OTE add-on).

VWAP is used for directional bias only. Stops are a fixed percent from entry
(not placed at VWAP), except OTE which may tighten toward the zone edge while
still clamping max risk to STOP_PCT.

Entries:
  - breakout: break of the prior N-bar high/low (default)
  - ote: Optimal Trade Entry pullback into 62–79% Fib of recent impulse
  - both: prefer OTE when mark is inside the zone; otherwise try breakout

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
from hl_bot.strategy.ote import evaluate_ote_long, evaluate_ote_short


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
    """VWAP-bias + micro breakout scalp (optional OTE pullback add-on).

    Bias (VWAP only):
      - long only if mark > session VWAP + buffer
      - short only if mark < session VWAP - buffer
      Buffer defaults to 0 bps (optional small bias buffer).

    Entry modes (``ENTRY_MODE``):
      - ``breakout``: break of prior N-bar high (long) / low (short). Default N=3.
      - ``ote``: mark inside 62–79% Fib retracement of recent impulse (with bias).
      - ``both`` (default): prefer OTE when mark is in the zone; else breakout.

    Stop (breakout): FIXED percent from entry (default 0.15%). NOT at VWAP.
    Stop (OTE): zone edge ± buffer, but **clamped** so max risk distance is
      STOP_PCT (if swing/zone stop is wider, use STOP_PCT from entry).
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
        # --- entry mode / OTE add-on ---
        entry_mode: str = "both",
        ote_lookback_bars: int = 45,
        ote_fib_shallow: float = 0.62,
        ote_fib_deep: float = 0.79,
        ote_stop_buffer_bps: float = 0.0,
        ote_require_close: bool = False,
        ote_use_htf_swings: bool = False,
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
        mode = (entry_mode or "both").strip().lower()
        if mode not in {"breakout", "ote", "both"}:
            mode = "both"
        self.entry_mode = mode
        self.ote_lookback_bars = ote_lookback_bars
        self.ote_fib_shallow = ote_fib_shallow
        self.ote_fib_deep = ote_fib_deep
        self.ote_stop_buffer_bps = ote_stop_buffer_bps
        self.ote_require_close = ote_require_close
        self.ote_use_htf_swings = ote_use_htf_swings

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

    def _breakout_long(
        self, mark: float, bars: list[dict[str, float]], vwap: float
    ) -> Signal | None:
        prior_high, _ = prior_n_bar_high_low(bars, self.breakout_bars)
        if prior_high is None or mark <= prior_high:
            return Signal(
                "flat",
                mark,
                0.0,
                0.0,
                vwap,
                reason="no_breakout_long",
                entry_mode="breakout",
            )
        risk = self._stop_distance(mark)
        if risk <= 0:
            return Signal(
                "flat", mark, 0.0, 0.0, vwap, reason="bad_risk", entry_mode="breakout"
            )
        stop = mark - risk
        tp = mark + risk * self.tp_r_multiple
        return Signal(
            "long",
            mark,
            stop,
            tp,
            vwap,
            reason="vwap_bias_breakout_long",
            entry_mode="breakout",
        )

    def _breakout_short(
        self, mark: float, bars: list[dict[str, float]], vwap: float
    ) -> Signal | None:
        _, prior_low = prior_n_bar_high_low(bars, self.breakout_bars)
        if prior_low is None or mark >= prior_low:
            return Signal(
                "flat",
                mark,
                0.0,
                0.0,
                vwap,
                reason="no_breakout_short",
                entry_mode="breakout",
            )
        risk = self._stop_distance(mark)
        if risk <= 0:
            return Signal(
                "flat", mark, 0.0, 0.0, vwap, reason="bad_risk", entry_mode="breakout"
            )
        stop = mark + risk
        tp = mark - risk * self.tp_r_multiple
        return Signal(
            "short",
            mark,
            stop,
            tp,
            vwap,
            reason="vwap_bias_breakout_short",
            entry_mode="breakout",
        )

    def _ote_long(
        self,
        mark: float,
        bars: list[dict[str, float]],
        vwap: float,
        swing_bars: list[dict[str, float]] | None,
    ) -> Signal:
        reason, stop, _, _zone = evaluate_ote_long(
            mark,
            bars,
            lookback=self.ote_lookback_bars,
            fib_shallow=self.ote_fib_shallow,
            fib_deep=self.ote_fib_deep,
            stop_pct=self.stop_pct,
            stop_buffer_bps=self.ote_stop_buffer_bps,
            require_close=self.ote_require_close,
            swing_bars=swing_bars,
        )
        if reason != "ote_long" or stop <= 0:
            return Signal(
                "flat", mark, 0.0, 0.0, vwap, reason=reason, entry_mode="ote"
            )
        risk = mark - stop
        tp = mark + risk * self.tp_r_multiple
        return Signal(
            "long", mark, stop, tp, vwap, reason="ote_long", entry_mode="ote"
        )

    def _ote_short(
        self,
        mark: float,
        bars: list[dict[str, float]],
        vwap: float,
        swing_bars: list[dict[str, float]] | None,
    ) -> Signal:
        reason, stop, _, _zone = evaluate_ote_short(
            mark,
            bars,
            lookback=self.ote_lookback_bars,
            fib_shallow=self.ote_fib_shallow,
            fib_deep=self.ote_fib_deep,
            stop_pct=self.stop_pct,
            stop_buffer_bps=self.ote_stop_buffer_bps,
            require_close=self.ote_require_close,
            swing_bars=swing_bars,
        )
        if reason != "ote_short" or stop <= 0:
            return Signal(
                "flat", mark, 0.0, 0.0, vwap, reason=reason, entry_mode="ote"
            )
        risk = stop - mark
        tp = mark - risk * self.tp_r_multiple
        return Signal(
            "short", mark, stop, tp, vwap, reason="ote_short", entry_mode="ote"
        )

    def _pick_entry(
        self,
        bias: str,
        mark: float,
        bars: list[dict[str, float]],
        vwap: float,
        swing_bars: list[dict[str, float]] | None,
    ) -> Signal:
        """Select OTE and/or breakout per ENTRY_MODE.

        ``both``: prefer OTE when mark is inside the zone (live long/short
        signal); otherwise fall through to breakout. If OTE is outside the
        zone / no swing, breakout still runs. If breakout also flat, the
        last reason (breakout's) is returned — except when mode is ``ote``
        only, then OTE reasons surface.
        """
        mode = self.entry_mode
        ote_fn = self._ote_long if bias == "long" else self._ote_short
        brk_fn = self._breakout_long if bias == "long" else self._breakout_short

        ote_sig: Signal | None = None
        if mode in ("ote", "both"):
            ote_sig = ote_fn(mark, bars, vwap, swing_bars)
            if ote_sig.side in ("long", "short"):
                return ote_sig
            if mode == "ote":
                return ote_sig

        # breakout or both (OTE did not fire)
        if mode in ("breakout", "both"):
            brk = brk_fn(mark, bars, vwap)
            assert brk is not None
            if brk.side in ("long", "short"):
                return brk
            # both + OTE flat: if OTE was outside_zone / no_swing, keep that
            # reason only when breakout also flat AND OTE actually had a zone
            # miss worth reporting — prefer breakout reason for "no breakout"
            # so existing tests keep seeing no_breakout_*.
            return brk

        # unreachable — mode validated in __init__
        return Signal("flat", mark, 0.0, 0.0, vwap, reason="bad_entry_mode")

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

        # Effective stop pct for max-skip check
        eff_stop_pct = self.stop_pct
        if self.min_stop_pct > 0:
            eff_stop_pct = max(eff_stop_pct, self.min_stop_pct)
        if self.max_stop_pct is not None and eff_stop_pct > self.max_stop_pct:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="stop_exceeds_max")

        # VWAP bias
        if mark > vwap + buf:
            bias = "long"
        elif mark < vwap - buf:
            bias = "short"
        else:
            return Signal("flat", mark, 0.0, 0.0, vwap, reason="inside_buffer")

        swing_bars = None
        if self.ote_use_htf_swings and self.entry_mode in ("ote", "both"):
            swing_bars = self._resolve_htf_bars(bars, htf_bars)

        sig = self._pick_entry(bias, mark, bars, vwap, swing_bars)
        if sig.side not in ("long", "short"):
            return sig

        # --- Higher-timeframe VWAP confirmation ---
        if self.htf_confirm:
            htf = self._resolve_htf_bars(bars, htf_bars)
            if htf_vwap_blocks(
                sig.side,
                mark,
                htf,
                reset_utc_hour=self.reset_utc_hour,
                now_ms=now_ms,
            ):
                return Signal(
                    "flat",
                    mark,
                    0.0,
                    0.0,
                    vwap,
                    reason="htf_vwap_block",
                    entry_mode=sig.entry_mode,
                )

        return sig

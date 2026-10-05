"""Model B decision. Score is logged. It is not a gate.

Arm only when the hunt coin, the bias filter, a far-enough confirmed
swing, and the full tape (sweep, reclaim, absorb, window delta, last 15s)
all pass, and the coin has no live thesis on that swing. The order is a
post-only Alo anchored at the sweep, sized at 2% to a stop one tick past
the extreme, with TP1 at ~2.5R and never beyond the untaken pool.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.alo import alo_limit
from hl_bot.strategy.model_b.bias import format_pool, resolve_bias
from hl_bot.strategy.model_b.risk import (
    assert_policy,
    size_from_stop,
    stop_beyond_extreme,
    stop_is_valid,
    take_profit,
    tp_is_valid,
)
from hl_bot.strategy.model_b.score import log_only_score, volume_tag
from hl_bot.strategy.model_b.swings import confirmed_swings, select_swing, swing_id
from hl_bot.strategy.model_b.tape import (
    MIN_PRINTS,
    NO_SIDE,
    THIN_TAPE,
    analyze_tape,
    missing_side,
    window_prints,
)
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.types import AloIntent, Decision, Pool, TradePrint
from hl_bot.strategy.model_b.universe import session_coins

OUT_OF_SESSION = "OUT_OF_SESSION"
NO_BIAS = "NO_BIAS"
NO_SWING = "NO_SWING"
NO_ALO = "NO_ALO"
BAD_STOP = "BAD_STOP"
BAD_TP = "BAD_TP"


class ModelBEngine:
    def __init__(self, thesis: ThesisBook | None = None, tp_r: float = 2.5):
        assert_policy()
        self.thesis = thesis or ThesisBook()
        self.tp_r = float(tp_r)

    def evaluate(
        self,
        coin: str,
        *,
        now: float,
        prints: list[TradePrint],
        bars: list[dict],
        pools: list[Pool],
        best_bid: float | None,
        best_ask: float | None,
        equity: float,
        tick: float,
        score: int | None = None,
        mark: float | None = None,
    ) -> Decision:
        """Decide an arm or a single fail reason.

        ``score``, when passed, is written on the log line unchanged
        (clamped to 0–9). It is not compared to 7/9 and it is not combined
        with any flow flag. The default score is tape density only.
        """
        coin_u = coin.upper()
        window = window_prints(prints, coin=coin_u, now=now)
        logged_score = (
            log_only_score(len(window))
            if score is None
            else max(0, min(9, int(score)))
        )
        tag = volume_tag(len(window))
        last_px = window[-1].price if window else (mark if mark and mark > 0 else None)
        bias = resolve_bias(last_px, pools) if last_px is not None else resolve_bias(0, [])

        def _done(
            reason: str | None,
            *,
            armed: bool = False,
            swing: float | None = None,
            sweep: float | None = None,
            absorb: float | None = None,
            window_delta: float | None = None,
            last_15: float | None = None,
            intent: AloIntent | None = None,
        ) -> Decision:
            return Decision(
                coin=coin_u,
                bias=bias.side,
                pool=format_pool(bias.pool),
                swing=swing,
                sweep_price=sweep,
                absorb=absorb,
                window_delta=window_delta,
                last_15s_delta=last_15,
                score=logged_score,
                volume_tag=tag,
                armed=armed,
                fail_reason=reason,
                intent=intent,
            )

        if coin_u not in session_coins(now):
            return _done(OUT_OF_SESSION)

        # Fail closed before any delta / absorb math that would skip a print.
        if window and missing_side(window):
            return _done(NO_SIDE)

        if len(window) < MIN_PRINTS:
            return _done(THIN_TAPE)

        if bias.side not in ("long", "short") or bias.pool is None:
            return _done(NO_BIAS)

        assert last_px is not None
        kind = "low" if bias.side == "long" else "high"
        swing = select_swing(
            confirmed_swings(bars, kind=kind, now=now),
            last_px,
            tick,
        )
        if swing is None:
            return _done(NO_SWING)

        metrics = analyze_tape(
            window,
            side=bias.side,
            swing=swing.price,
            tick=tick,
            now=now,
        )
        if metrics.fail_reason:
            return _done(
                metrics.fail_reason,
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
            )

        sid = swing_id(coin_u, swing)
        blocked = self.thesis.block_reason(coin_u, sid)
        if blocked:
            return _done(
                blocked,
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
            )

        assert metrics.sweep_price is not None
        limit = alo_limit(
            bias.side,
            metrics.sweep_price,
            best_bid,
            best_ask,
            tick,
        )
        if limit is None:
            return _done(
                NO_ALO,
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
            )

        stop = stop_beyond_extreme(bias.side, metrics.sweep_price, tick)
        if not stop_is_valid(bias.side, limit, stop):
            return _done(
                BAD_STOP,
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
            )

        try:
            size, _dollar = size_from_stop(equity, limit, stop, leverage=20)
        except ValueError:
            return _done(
                BAD_STOP,
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
            )

        tp = take_profit(
            bias.side,
            limit,
            stop,
            bias.pool.price,
            tp_r=self.tp_r,
        )
        if not tp_is_valid(bias.side, limit, tp):
            return _done(
                BAD_TP,
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
            )

        intent = AloIntent(
            coin=coin_u,
            side=bias.side,
            limit_px=limit,
            size=size,
            stop=stop,
            take_profit=tp,
            swing_id=sid,
            tif="Alo",
            market_fallback=False,
            leverage=20,
            work_sec=20.0,
        )
        return _done(
            None,
            armed=True,
            swing=swing.price,
            sweep=metrics.sweep_price,
            absorb=metrics.absorb,
            window_delta=metrics.window_delta,
            last_15=metrics.last_15s_delta,
            intent=intent,
        )

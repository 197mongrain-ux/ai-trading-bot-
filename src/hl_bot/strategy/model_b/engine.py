"""Model B decision. Score is logged. It is not a gate.

Arm when the hunt coin, the allowed side, a far-enough confirmed swing,
and the full tape (sweep, reclaim, absorb, window delta, last 15s) all
pass, and the coin has no live thesis on that swing. Directional bias
drops the other side. Bias NONE allows both. The order is a post-only Alo
anchored at the sweep, sized from RISK_PER_TRADE of unified equity to a
stop one tick past the extreme, with TP1 at ~2.5R and never beyond the
untaken pool on that side.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.alo import alo_limit
from hl_bot.strategy.model_b.bias import format_pool, resolve_bias
from hl_bot.strategy.model_b.risk import (
    MODEL_B_RISK_PCT,
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
NO_SWING = "NO_SWING"
NO_ALO = "NO_ALO"
BAD_STOP = "BAD_STOP"
BAD_TP = "BAD_TP"

# How close a failed side got to an arm. Used only when NONE tries both
# sides and neither arms, so the log still carries one reason.
_FAIL_RANK = {
    NO_SWING: 1,
    "NO_SWEEP": 2,
    "NO_RECLAIM": 3,
    "ABSORB": 4,
    "DELTA": 5,
    "LAST_15s": 6,
    NO_ALO: 7,
    BAD_STOP: 8,
    BAD_TP: 8,
    "THESIS_DONE": 9,
    "SECOND_ALO": 9,
    "AVERAGE_DOWN": 9,
}


class ModelBEngine:
    def __init__(
        self,
        thesis: ThesisBook | None = None,
        tp_r: float = 2.5,
        risk_pct: float = MODEL_B_RISK_PCT,
        min_prints: int = MIN_PRINTS,
    ):
        assert_policy()
        if risk_pct <= 0:
            raise ValueError("risk_pct must be > 0")
        if int(min_prints) < 1:
            raise ValueError("min_prints must be >= 1")
        self.thesis = thesis or ThesisBook()
        self.tp_r = float(tp_r)
        # From RISK_PER_TRADE. Model B settings validation requires 0.02.
        self.risk_pct = float(risk_pct)
        # Mainnet default is 30. Testnet settings pass the density-scaled floor.
        self.min_prints = int(min_prints)

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
            pool_label: str | None = None,
        ) -> Decision:
            return Decision(
                coin=coin_u,
                bias=bias.side,
                pool=pool_label if pool_label is not None else format_pool(bias.pool),
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
                print_count=len(window),
                min_prints=self.min_prints,
            )

        def attempt(side: str, pool: Pool | None) -> Decision:
            label = format_pool(pool)
            kind = "low" if side == "long" else "high"
            swing = select_swing(
                confirmed_swings(bars, kind=kind, now=now),
                last_px,
                tick,
            )
            if swing is None:
                return _done(NO_SWING, pool_label=label)

            metrics = analyze_tape(
                window,
                side=side,
                swing=swing.price,
                tick=tick,
                now=now,
            )
            fields = dict(
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
                pool_label=label,
            )
            if metrics.fail_reason:
                return _done(metrics.fail_reason, **fields)

            sid = swing_id(coin_u, swing)
            blocked = self.thesis.block_reason(coin_u, sid)
            if blocked:
                return _done(blocked, **fields)

            assert metrics.sweep_price is not None
            limit = alo_limit(
                side,
                metrics.sweep_price,
                best_bid,
                best_ask,
                tick,
            )
            if limit is None:
                return _done(NO_ALO, **fields)

            stop = stop_beyond_extreme(side, metrics.sweep_price, tick)
            if not stop_is_valid(side, limit, stop):
                return _done(BAD_STOP, **fields)

            try:
                size, _dollar = size_from_stop(
                    equity,
                    limit,
                    stop,
                    risk_pct=self.risk_pct,
                    leverage=20,
                )
            except ValueError:
                return _done(BAD_STOP, **fields)

            tp = take_profit(
                side,
                limit,
                stop,
                None if pool is None else pool.price,
                tp_r=self.tp_r,
            )
            if not tp_is_valid(side, limit, tp):
                return _done(BAD_TP, **fields)

            intent = AloIntent(
                coin=coin_u,
                side=side,
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
            return _done(None, armed=True, intent=intent, **fields)

        if coin_u not in session_coins(now):
            return _done(OUT_OF_SESSION)

        # Fail closed before any delta / absorb math that would skip a print.
        if window and missing_side(window):
            return _done(NO_SIDE)

        if len(window) < self.min_prints:
            return _done(THIN_TAPE)

        if last_px is None:
            return _done(THIN_TAPE)

        if bias.side == "long":
            sides = [("long", bias.pool)]
        elif bias.side == "short":
            sides = [("short", bias.pool)]
        else:
            # NONE: both directions are allowed. Each side is capped by the
            # untaken pool on that side when one exists.
            sides = [("long", bias.pool_above), ("short", bias.pool_below)]

        results = [attempt(side, pool) for side, pool in sides]
        armed = [item for item in results if item.armed and item.intent is not None]
        if len(armed) == 1:
            return armed[0]
        if len(armed) > 1:
            # One thesis per coin. Higher absorb wins; a tie keeps the long.
            return max(
                armed,
                key=lambda item: (
                    item.absorb or 0.0,
                    1 if item.intent is not None and item.intent.side == "long" else 0,
                ),
            )
        return max(results, key=lambda item: _FAIL_RANK.get(item.fail_reason or "", 0))

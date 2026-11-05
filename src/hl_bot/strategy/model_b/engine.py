"""Model B decision. Score is logged. It is not a gate.

Arm when the hunt coin, the allowed side, a far-enough confirmed swing,
and the full tape (sweep, reclaim, absorb, window delta, last 15s) all
pass, and the coin has no live thesis on that swing. Directional bias
drops the other side. Bias NONE allows both. The order is a post-only Alo
anchored at the sweep, sized from RISK_PER_TRADE of the spot USDC
balance (not perp account value). The stop is past opposing liquidity.
A real wick more than one tick past the fill keeps its buffer. A stop
that would otherwise be a few ticks off the fill clears the next
opposing print outside wick room, or the room itself. A stop wider
than 1.5% of price is still armed; size is 2% of spot USDC over that
distance. ``size_adjust=wide_stop`` is journaled when the distance is
past 1.5%. ``BAD_STOP`` is only impossible geometry (wrong side,
stop == fill, one-tick collision). Window and last-15s delta may sit
inside a flat band; a clearly adverse delta still fails. The band is
the larger of ``DELTA_FLAT_USDC`` / mid (default $100) and
``DELTA_FLAT_EPS`` (default 0.05 coins). TP is the nearest confirmed
swing or untaken pool in the trade direction that clears fees and at
least 1R of the stop, even past 2R. A closer level is skipped. With
no level past that floor the target is 1.5R of the stop just placed.
``BAD_TP`` is that fallback when it still cannot clear the band, or a
target that is not strictly beyond the entry. The fail logs the R
distance and the pool R-multiple. A session volume profile
(POC / VAH / VAL / LVN) is written on the decision for the journal.
Those tags do not arm, block, or move the stop.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.alo import alo_limit, market_ref
from hl_bot.strategy.model_b.bias import format_pool, resolve_bias
from hl_bot.strategy.model_b.risk import (
    DEFAULT_TP_R,
    MODEL_B_RISK_PCT,
    arm_take_profit,
    assert_policy,
    clamp_tp_r,
    collides_with_fill,
    min_tp_distance,
    next_liquidity,
    place_stop,
    size_adjust_tag,
    size_from_stop,
    stop_is_valid,
    tp_fail_detail,
)
from hl_bot.strategy.model_b.score import log_only_score, volume_tag
from hl_bot.strategy.model_b.swings import (
    atr14,
    closed_prices,
    confirmed_swings,
    local_bar_extreme,
    select_swing,
    swing_id,
)
from hl_bot.strategy.model_b.tape import (
    DELTA_FLAT_EPS,
    DELTA_FLAT_USDC,
    MIN_PRINTS,
    NO_SIDE,
    THIN_TAPE,
    analyze_tape,
    flat_eps_parts,
    missing_side,
    window_prints,
)
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.types import AloIntent, Decision, Pool, TradePrint
from hl_bot.strategy.model_b.universe import session_coins
from hl_bot.strategy.model_b.vp_log import vp_error_fields, vp_log_fields
from hl_bot.strategy.volume_profile import VP_AS_FILTER, VP_ENABLED, VP_ENTRIES

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
        tp_r: float = DEFAULT_TP_R,
        risk_pct: float = MODEL_B_RISK_PCT,
        min_prints: int = MIN_PRINTS,
        alo_timeout_sec: float = 0.0,
        delta_flat_eps: float = DELTA_FLAT_EPS,
        delta_flat_usdc: float = DELTA_FLAT_USDC,
    ):
        assert_policy()
        # Profile tags are journal-only. These switches must not become a gate.
        if VP_AS_FILTER or VP_ENTRIES or VP_ENABLED:
            raise RuntimeError("Model B volume profile is log-only")
        if risk_pct <= 0:
            raise ValueError("risk_pct must be > 0")
        if int(min_prints) < 1:
            raise ValueError("min_prints must be >= 1")
        if float(alo_timeout_sec) < 0:
            raise ValueError("alo_timeout_sec must be >= 0")
        if float(delta_flat_eps) < 0:
            raise ValueError("delta_flat_eps must be >= 0")
        if float(delta_flat_usdc) < 0:
            raise ValueError("delta_flat_usdc must be >= 0")
        self.thesis = thesis or ThesisBook(work_sec=float(alo_timeout_sec))
        self.tp_r = clamp_tp_r(tp_r)
        # From RISK_PER_TRADE. Model B settings validation requires 0.02.
        self.risk_pct = float(risk_pct)
        # Mainnet default is 30. Testnet settings pass the density-scaled floor.
        self.min_prints = int(min_prints)
        # 0 rests the maker until the thesis is stale. No default 20s cancel.
        self.alo_timeout_sec = float(alo_timeout_sec)
        # Coin-size floor. The band is max(usdc / price, this).
        self.delta_flat_eps = float(delta_flat_eps)
        # USDC notional. Divided by the mid, then compared with the coin floor.
        self.delta_flat_usdc = float(delta_flat_usdc)

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

        ``equity`` is the spot USDC balance the 2% risk is taken from.
        Paper tests pass that balance in. Live passes the spot read, not
        perp account value.
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
        # Filled by attempt() for the log line only. The arm math below
        # does not read it.
        vp_ctx: dict[str, object] = {"side": None, "swing_ts": None}

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
            size_adjust: str | None = None,
            delta_flat: str | None = None,
            delta_flat_eps: float | None = None,
            delta_flat_usdc_eps: float | None = None,
            delta_flat_coin_eps: float | None = None,
            delta_flat_px: float | None = None,
            r_distance: float | None = None,
            pool_distance: float | None = None,
            pool_r: float | None = None,
            bad_tp_why: str | None = None,
        ) -> Decision:
            ctx_side = vp_ctx.get("side")
            log_side = ctx_side if ctx_side in ("long", "short") else (
                bias.side if bias.side in ("long", "short") else None
            )
            swing_ts = vp_ctx.get("swing_ts")
            try:
                tags = vp_log_fields(
                    bars,
                    now=now,
                    prints=prints,
                    side=log_side if isinstance(log_side, str) else None,
                    sweep=sweep,
                    swing_ts=float(swing_ts) if isinstance(swing_ts, (int, float)) else None,
                    ref_price=last_px,
                )
            except Exception:
                tags = vp_error_fields()
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
                vp_poc=tags["vp_poc"],
                vp_vah=tags["vp_vah"],
                vp_val=tags["vp_val"],
                nearest_lvn_on_side=tags["nearest_lvn_on_side"],
                sweep_to_val_bps=tags["sweep_to_val_bps"],
                sweep_to_lvn_bps=tags["sweep_to_lvn_bps"],
                vp_tag=tags["vp_tag"],
                catalyst_flag=tags["catalyst_flag"],
                size_adjust=size_adjust,
                delta_flat=delta_flat,
                delta_flat_eps=delta_flat_eps,
                delta_flat_usdc_eps=delta_flat_usdc_eps,
                delta_flat_coin_eps=delta_flat_coin_eps,
                delta_flat_px=delta_flat_px,
                r_distance=r_distance,
                pool_distance=pool_distance,
                pool_r=pool_r,
                bad_tp_why=bad_tp_why,
            )

        def attempt(side: str, pool: Pool | None) -> Decision:
            vp_ctx["side"] = side
            vp_ctx["swing_ts"] = None
            label = format_pool(pool)
            kind = "low" if side == "long" else "high"
            swing = select_swing(
                confirmed_swings(bars, kind=kind, now=now),
                last_px,
                tick,
            )
            ref_px = market_ref(
                best_bid,
                best_ask,
                last_px if last_px and last_px > 0 else (mark if mark and mark > 0 else None),
            )
            eps_coin, usdc_eps, coin_eps = flat_eps_parts(
                self.delta_flat_usdc, ref_px, self.delta_flat_eps
            )
            scale = dict(
                delta_flat_eps=eps_coin,
                delta_flat_usdc_eps=usdc_eps,
                delta_flat_coin_eps=coin_eps,
                delta_flat_px=ref_px,
            )
            if swing is None:
                return _done(NO_SWING, pool_label=label, **scale)
            vp_ctx["swing_ts"] = swing.ts

            metrics = analyze_tape(
                window,
                side=side,
                swing=swing.price,
                tick=tick,
                now=now,
                delta_flat_eps=eps_coin,
            )
            fields = dict(
                swing=swing.price,
                sweep=metrics.sweep_price,
                absorb=metrics.absorb,
                window_delta=metrics.window_delta,
                last_15=metrics.last_15s_delta,
                pool_label=label,
                **scale,
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

            # Stop side: every closed extreme, confirmed swings, and
            # untaken pools beyond the fill. TP side: confirmed swings
            # and untaken pools only. A bar wick is not a target.
            if side == "long":
                stop_field, stop_kind, tp_kind = "l", "low", "high"
            else:
                stop_field, stop_kind, tp_kind = "h", "high", "low"
            further = closed_prices(bars, field=stop_field, now=now)
            further.extend(
                item.price for item in confirmed_swings(bars, kind=stop_kind, now=now)
            )
            tp_levels = [
                item.price for item in confirmed_swings(bars, kind=tp_kind, now=now)
            ]
            for item in pools:
                if item.taken or item.price <= 0:
                    continue
                if side == "long":
                    if item.price < limit:
                        further.append(item.price)
                    elif item.price > limit:
                        tp_levels.append(item.price)
                elif item.price > limit:
                    further.append(item.price)
                elif item.price < limit:
                    tp_levels.append(item.price)
            stop = place_stop(
                side,
                metrics.sweep_price,
                limit,
                tick,
                tp_r=self.tp_r,
                swing=swing.price,
                local_extreme=local_bar_extreme(bars, side=side, now=now),
                atr=atr14(bars, now=now),
                further=further,
            )
            # Flow already passed. A structural stop past the wick is armed.
            # Tight room and a distance past 1.5% change the size. They do
            # not scrap the thesis. Wrong side, stop == fill, and a one-tick
            # collision still fail closed.
            if (
                stop is None
                or not stop_is_valid(side, limit, stop)
                or collides_with_fill(limit, stop, tick)
            ):
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
            adjust = size_adjust_tag(limit, stop)

            # Nearest liquidity that clears fees and 1R. A closer pool is
            # skipped so the next real level can win, including past 2R.
            # No level past that floor: 1.5R of the stop the size just used.
            # That fallback fails closed when it still cannot clear the band.
            floor = min_tp_distance(limit, stop)
            target = next_liquidity(side, limit, tick, tp_levels, min_dist=floor)
            nearest = next_liquidity(side, limit, tick, tp_levels)
            logged_pool = target if target is not None else nearest
            if logged_pool is None and pool is not None:
                logged_pool = pool.price
            tp = arm_take_profit(
                side,
                limit,
                stop,
                target,
                tp_r=self.tp_r,
            )
            if tp is not None and target is None:
                gap = (tp - limit) if side == "long" else (limit - tp)
                if gap + 1e-12 < floor:
                    tp = None
            if tp is None:
                detail = tp_fail_detail(
                    side,
                    limit,
                    stop,
                    logged_pool,
                    limit,
                )
                return _done(
                    BAD_TP,
                    r_distance=detail["r_distance"],
                    pool_distance=detail["pool_distance"],
                    pool_r=detail["pool_r"],
                    bad_tp_why=detail["why"],
                    **fields,
                )

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
                work_sec=self.alo_timeout_sec,
                sweep_px=metrics.sweep_price,
                tick=tick,
                pool_px=target,
                tp_r=self.tp_r,
            )
            return _done(
                None,
                armed=True,
                intent=intent,
                size_adjust=adjust,
                delta_flat=metrics.delta_flat,
                **fields,
            )

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
            # NONE: both directions are allowed. Each side targets the
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

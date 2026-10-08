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
least 1R of the stop, even past 2R. A closer level is skipped. When that
nearest level is under ``MODEL_B_TP_MIN_POOL_R`` (default 1.5R, same
fee/R floor math), the target walks out to the first real level at or
past it; with none, the nearest level is kept and ``MODEL_B
TP_UNDER_1_5R`` is logged (0 = off). A walked-to pool past
``MODEL_B_TP_MAX_POOL_R`` (3R), or no pool at 1.5R at all, is taken only
when the side is with the 15m/1h trend (trend.py); otherwise the side is
skipped as ``TP_TOO_FAR_COUNTERTREND`` / ``TP_UNDER_1_5R_COUNTERTREND``
(``MODEL_B_TP_FAR_SKIP_COUNTERTREND=0`` keeps the pool instead). A moved
(min-stop widened) stop keeps its own tp_r floor. With no level past the
1R floor the target is 1.5R of the stop just placed.
``BAD_TP`` is that fallback when it still cannot clear the band, or a
target that is not strictly beyond the entry. The fail logs the R
distance and the pool R-multiple. A session volume profile
(POC / VAH / VAL / LVN) is written on the decision for the journal.
Those tags do not arm, block, or move the stop.
"""

from __future__ import annotations

import logging

import math
from dataclasses import replace

from hl_bot.strategy.model_b.alo import alo_limit, market_ref
from hl_bot.strategy.model_b.bias import format_pool, resolve_bias
from hl_bot.strategy.model_b.htf_stop import check_htf_stop
from hl_bot.strategy.model_b.stop_slip import resolve_stop_slip_bps
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
    cap_size_to_loss,
    stop_buffer,
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
from hl_bot.strategy.model_b.flow import (
    COUNTER_FLOW,
    COUNTER_FLOW_EPS,
    COUNTER_FLOW_USDC,
    FLIP_RATIO,
    HOLD_SEC,
    LOOKBACK_SEC,
    counter_flow,
)
from hl_bot.strategy.model_b.structure import (
    DEFAULT_TIMEFRAMES,
    STRUCTURE,
    structure_blocks,
    structure_label,
    structure_states,
)
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.tp_select import (
    filter_spent_swing_prices,
    runner_target,
    significant_levels,
    split_runner_size,
    walk_liquidity_tp,
    TpWalk,
)
from hl_bot.strategy.model_b.trend import AdxTrend, MacroRead, TfTrend, TrendRead, read_macro, read_trend
from hl_bot.strategy.model_b.types import AloIntent, Decision, Pool, TradePrint
from hl_bot.strategy.model_b.xyz_min_stop import (
    RECOMMENDED_XYZ_MIN_STOP_BPS,
    xyz_floor_stop,
)


def _bound_distance(maker_fee: float | None, taker_fee: float | None):
    """``min_tp_distance`` closed over this coin's fee rates.

    Looking up ``min_tp_distance`` at call time keeps a test patch on
    ``hl_bot.strategy.model_b.engine.min_tp_distance`` in force.
    """

    def _fn(entry, stop, *, min_r=1.0):
        # Omit the fee kwargs when this coin is on the base tier so a test
        # patch of min_tp_distance(entry, stop, min_r=) still matches.
        if maker_fee is None and taker_fee is None:
            return min_tp_distance(entry, stop, min_r=min_r)
        return min_tp_distance(
            entry, stop, min_r=min_r, maker_fee=maker_fee, taker_fee=taker_fee
        )

    return _fn
from hl_bot.strategy.model_b.universe import canon_coin, resolve_hunt_coins
from hl_bot.strategy.model_b.vp_log import vp_error_fields, vp_log_fields
from hl_bot.strategy.volume_profile import VP_AS_FILTER, VP_ENABLED, VP_ENTRIES

OUT_OF_SESSION = "OUT_OF_SESSION"
NO_SWING = "NO_SWING"
NO_ALO = "NO_ALO"
BAD_STOP = "BAD_STOP"
BAD_TP = "BAD_TP"
STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
SHALLOW_SWEEP = "SHALLOW_SWEEP"
TP_TOO_FAR_COUNTERTREND = "TP_TOO_FAR_COUNTERTREND"
TP_UNDER_1_5R_COUNTERTREND = "TP_UNDER_1_5R_COUNTERTREND"
MACRO_SIDE = "MACRO_SIDE"
MACRO_MODES_ALLOWED = ("off", "on", "shadow")


def min_sweep_bps_for(coin: str, spec: float | str | dict | None) -> float:
    """``MODEL_B_MIN_SWEEP_BPS``: a number, or ``BTC:5,ETH:3,default:0.3``."""
    if spec is None:
        return 0.0
    if isinstance(spec, (int, float)):
        return max(0.0, float(spec))
    table: dict[str, float] = {}
    if isinstance(spec, dict):
        table = {canon_coin(k) if k != "default" else "default": float(v) for k, v in spec.items()}
    else:
        text = str(spec).strip()
        if not text:
            return 0.0
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        for part in text.split(","):
            if ":" not in part:
                continue
            key, _, val = part.rpartition(":")
            key = key.strip()
            try:
                table["default" if key.lower() == "default" else canon_coin(key)] = float(val)
            except ValueError:
                continue
    return max(0.0, table.get(canon_coin(coin), table.get("default", 0.0)))

# How close a failed side got to an arm. Used only when NONE tries both
# sides and neither arms, so the log still carries one reason.
_FAIL_RANK = {
    MACRO_SIDE: 0.5,
    NO_SWING: 1,
    "NO_SWEEP": 2,
    "NO_RECLAIM": 3,
    "ABSORB": 4,
    "DELTA": 5,
    "LAST_15s": 6,
    SHALLOW_SWEEP: 6.4,
    COUNTER_FLOW: 6.5,
    STRUCTURE: 6.6,
    NO_ALO: 7,
    BAD_STOP: 8,
    BAD_TP: 8,
    TP_TOO_FAR_COUNTERTREND: 8,
    TP_UNDER_1_5R_COUNTERTREND: 8,
    STOP_TOO_TIGHT: 8,
    "THESIS_DONE": 9,
    "SECOND_ALO": 9,
    "AVERAGE_DOWN": 9,
}


logger = logging.getLogger(__name__)

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
        coins: tuple[str, ...] | list[str] | None = None,
        max_notional_leverage: int = 20,
        min_stop_bps: float = 15.0,
        structure_filter: bool = True,
        structure_shadow: bool = False,
        structure_timeframes: tuple[str, ...] | list[str] = DEFAULT_TIMEFRAMES,
        counter_flow_filter: bool = True,
        counter_flow_sec: float = LOOKBACK_SEC,
        counter_flow_usdc: float = COUNTER_FLOW_USDC,
        counter_flow_eps: float = COUNTER_FLOW_EPS,
        counter_flow_flip: float = FLIP_RATIO,
        counter_flow_hold_sec: float = HOLD_SEC,
        min_sweep_bps: float | str | dict | None = 0.3,
        sweep_require_htf: bool = False,
        cap_includes_fees: bool = True,
        two_sided: bool = False,
        tp_min_pool_r: float = 0.0,
        tp_max_pool_r: float = 3.0,
        tp_far_skip_countertrend: bool = True,
        macro_side_only: str = "off",
        macro_range_policy: str = "both",
        macro_adx_min: float = 20.0,
        macro_mode: str = "4h_lead",
        tp_untaken_only: str = "on",
        tp_runner: str = "shadow",
        tp_runner_frac: float = 0.5,
        tp_runner_max_r: float = 5.0,
        stop_slip: str = "off",
        stop_slip_bps: str = "",
        htf_stop: str = "off",
        xyz_min_stop: str = "off",
        xyz_min_stop_bps: float = RECOMMENDED_XYZ_MIN_STOP_BPS,
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
        # From RISK_PER_TRADE. Model B settings allow 1% to 2%. Default 0.02.
        self.risk_pct = float(risk_pct)
        # Mainnet default is 30. Testnet settings pass the density-scaled floor.
        self.min_prints = int(min_prints)
        # 0 rests the maker until the thesis is stale. No default 20s cancel.
        self.alo_timeout_sec = float(alo_timeout_sec)
        # Coin-size floor. The band is max(usdc / price, this).
        self.delta_flat_eps = float(delta_flat_eps)
        # USDC notional. Divided by the mid, then compared with the coin floor.
        self.delta_flat_usdc = float(delta_flat_usdc)
        # Same list in NY hours and after hours. ``None`` is the default universe.
        self.coins = resolve_hunt_coins(coins)
        # Size brakes (MODEL_B_MAX_LEVERAGE, MODEL_B_MIN_STOP_BPS). They only
        # shrink size; the stop and target prices are untouched.
        self.max_notional_leverage = int(max_notional_leverage)
        self.min_stop_bps = float(min_stop_bps)
        self.structure_filter = bool(structure_filter)
        # Shadow: compute and log, never block (data-collection mode).
        self.structure_shadow = bool(structure_shadow) and not self.structure_filter
        self.structure_timeframes = tuple(structure_timeframes)
        self.counter_flow_filter = bool(counter_flow_filter)
        self.counter_flow_sec = float(counter_flow_sec)
        self.counter_flow_usdc = float(counter_flow_usdc)
        self.counter_flow_eps = float(counter_flow_eps)
        self.counter_flow_flip = float(counter_flow_flip)
        self.counter_flow_hold_sec = float(counter_flow_hold_sec)
        self.min_sweep_bps = min_sweep_bps
        self.sweep_require_htf = bool(sweep_require_htf)
        # 2% cap counts the maker entry + taker exit fee (stop and TP unchanged).
        self.cap_includes_fees = bool(cap_includes_fees)
        # MODEL_B_TWO_SIDED: evaluate long AND short every cycle; the draw
        # pool is the TP target only. Off = the nearest pool picks the side.
        self.two_sided = bool(two_sided)
        # MODEL_B_TP_MIN_POOL_R: a nearest TP pool under this R walks out to
        # the next real pool at >= this R (same fee/R floor math). 0 = off.
        # The bare engine default is off; settings default it to 1.5.
        if float(tp_min_pool_r) < 0:
            raise ValueError("tp_min_pool_r must be >= 0")
        self.tp_min_pool_r = float(tp_min_pool_r)
        # MODEL_B_TP_MAX_POOL_R: a walked-to pool past this R is "too far".
        # MODEL_B_TP_FAR_SKIP_COUNTERTREND: too far (or no pool at the min R)
        # and not with the 15m/1h trend -> skip. Off -> keep the pool.
        if float(tp_max_pool_r) < 0:
            raise ValueError("tp_max_pool_r must be >= 0")
        self.tp_max_pool_r = float(tp_max_pool_r)
        self.tp_far_skip_countertrend = bool(tp_far_skip_countertrend)
        # MODEL_B_MACRO_SIDE_ONLY: off | on (only the macro side is evaluated)
        # | shadow (both evaluated; an arm the macro would block is logged
        # "MODEL_B SHADOW reason=MACRO_SIDE would_block=1"). Macro = ADX(14)
        # +DI/-DI on 1h and 4h (trend.read_macro). Range -> MACRO_RANGE_POLICY.
        mode = str(macro_side_only).strip().lower()
        if mode not in MACRO_MODES_ALLOWED:
            raise ValueError("macro_side_only must be off|on|shadow")
        if macro_range_policy not in ("both", "none"):
            raise ValueError("macro_range_policy must be both|none")
        self.macro_side_only = mode
        self.macro_range_policy = macro_range_policy
        self.macro_adx_min = float(macro_adx_min)
        self.macro_mode = macro_mode
        untaken = str(tp_untaken_only).strip().lower()
        if untaken not in ("off", "on", "shadow"):
            raise ValueError("tp_untaken_only must be off|on|shadow")
        runner = str(tp_runner).strip().lower()
        if runner not in ("off", "on", "shadow"):
            raise ValueError("tp_runner must be off|on|shadow")
        if not (0.0 < float(tp_runner_frac) < 1.0):
            raise ValueError("tp_runner_frac must be in (0, 1)")
        if float(tp_runner_max_r) <= 0:
            raise ValueError("tp_runner_max_r must be > 0")
        self.tp_untaken_only = untaken
        self.tp_runner = runner
        self.tp_runner_frac = float(tp_runner_frac)
        self.tp_runner_max_r = float(tp_runner_max_r)
        # Bare engine stays off so existing arm tests keep today's size and
        # stop. load_settings defaults both to shadow (log only).
        slip_mode = str(stop_slip).strip().lower()
        htf_mode = str(htf_stop).strip().lower()
        if slip_mode not in ("off", "on", "shadow"):
            raise ValueError("stop_slip must be off|on|shadow")
        if htf_mode not in ("off", "on", "shadow"):
            raise ValueError("htf_stop must be off|on|shadow")
        xyz_mode = str(xyz_min_stop).strip().lower()
        if xyz_mode not in ("off", "on", "shadow"):
            raise ValueError("xyz_min_stop must be off|on|shadow")
        if float(xyz_min_stop_bps) < 0:
            raise ValueError("xyz_min_stop_bps must be >= 0")
        self.stop_slip = slip_mode
        self.stop_slip_bps = str(stop_slip_bps or "")
        self.htf_stop = htf_mode
        # Bare engine stays off. load_settings defaults the mode to shadow
        # and the floor to 40 bps (the Oct 7-8 expectancy pick).
        self.xyz_min_stop = xyz_mode
        self.xyz_min_stop_bps = float(xyz_min_stop_bps)
        # coin -> (1h bucket, MacroRead): one read + one MACRO line per 1h bar.
        self._macro_cache: dict[str, tuple[int, MacroRead]] = {}

    def pick_target(
        self,
        side: str,
        entry: float,
        stop: float,
        tick: float,
        bars: list[dict],
        pools: list[Pool],
        now: float,
        last: float | None,
        *,
        coin: str = "",
        maker_fee: float | None = None,
        taker_fee: float | None = None,
    ) -> TpWalk:
        """Same TP walk the arm uses, from a fill price and the planned stop.

        The stop is not an output. Callers that re-pick at the fill keep
        the planned stop even when this returns no target.
        """
        kind = "high" if side == "long" else "low"
        swings = confirmed_swings(bars, kind=kind, now=now)
        levels = [item.price for item in swings]
        for item in pools:
            if item.taken or item.price <= 0:
                continue
            if side == "long" and item.price > entry:
                levels.append(item.price)
            elif side == "short" and item.price < entry:
                levels.append(item.price)
        if self.tp_untaken_only == "on":
            spent = filter_spent_swing_prices(side, swings, bars, now, last)
            levels = [px for px in levels if px not in spent]

        def _trend():
            try:
                return read_trend(bars, now)
            except Exception:
                return TrendRead((TfTrend("15m", "unknown"), TfTrend("1h", "unknown")))

        return walk_liquidity_tp(
            side,
            entry,
            stop,
            tick,
            levels,
            coin=coin or side,
            tp_r=self.tp_r,
            min_pool_r=self.tp_min_pool_r,
            max_pool_r=self.tp_max_pool_r,
            far_skip=self.tp_far_skip_countertrend,
            moved_floor=None,
            trend_fn=_trend,
            distance_fn=_bound_distance(maker_fee, taker_fee),
        )

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
        leverage: int = 20,
        htf_bars: list[dict] | None = None,
        maker_fee: float | None = None,
        taker_fee: float | None = None,
    ) -> Decision:
        """Decide an arm or a single fail reason.

        ``score``, when passed, is written on the log line unchanged
        (clamped to 0–9). It is not compared to 7/9 and it is not combined
        with any flow flag. The default score is tape density only.

        ``equity`` is the spot USDC balance the 2% risk is taken from.
        Paper tests pass that balance in. Live passes the spot read, not
        perp account value.

        ``leverage`` is that coin's max, used only for the notional cap
        and the margin on the intent. It does not move the stop or the
        target. Unknown meta passes 20.
        """
        coin_u = canon_coin(coin)
        _dist = _bound_distance(maker_fee, taker_fee)
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
            structure: str | None = None,
            counter_flow: str | None = None,
            structure_shadow: str | None = None,
            sizing_dist: float | None = None,
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
                structure=structure,
                counter_flow=counter_flow,
                structure_shadow=structure_shadow,
                sizing_dist=sizing_dist,
            )

        structure_cache: dict[str, object] = {}

        trend_cache: dict[str, TrendRead] = {}

        def _trend() -> TrendRead:
            # TP decision only (MODEL_B_TP_MIN_POOL_R > 0). Not a gate on its own.
            if "read" not in trend_cache:
                try:
                    trend_cache["read"] = read_trend(bars, now)
                except Exception:
                    # Unknown is "not with trend": a far TP is not taken blind.
                    trend_cache["read"] = TrendRead(
                        (TfTrend("15m", "unknown"), TfTrend("1h", "unknown"))
                    )
            return trend_cache["read"]

        def _structure():
            if "states" not in structure_cache:
                try:
                    structure_cache["states"] = structure_states(
                        bars, now, last_px, self.structure_timeframes
                    )
                except Exception:
                    structure_cache["states"] = []
            return structure_cache["states"]

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

            # Sweep depth past the swing, in bps (per coin), and optionally
            # the swept level must be a 15m swing or an untaken pool rather
            # than a micro 1m low/high.
            assert metrics.sweep_price is not None
            depth_bps = abs(swing.price - metrics.sweep_price) / swing.price * 10_000.0
            need_bps = min_sweep_bps_for(coin_u, self.min_sweep_bps)
            if need_bps > 0 and depth_bps + 1e-9 < need_bps:
                return _done(SHALLOW_SWEEP, **fields)
            if self.sweep_require_htf and not _meaningful_level(
                bars, now, side, swing.price, pools, tick
            ):
                return _done(SHALLOW_SWEEP, **fields)

            # Market structure (15m + 1h) must not fight the side, and the
            # sweep window must not be heavy one-sided flow against it.
            # Both run only on a setup the tape already cleared.
            states = _structure()
            fields["structure"] = structure_label(states)
            if self.structure_filter and structure_blocks(side, states):
                return _done(STRUCTURE, **fields)
            if self.structure_shadow and structure_blocks(side, states):
                fields["structure_shadow"] = "would_block"
                logger.info(
                    "MODEL_B SHADOW %s reason=STRUCTURE would_block=1 side=%s structure=%s",
                    coin_u,
                    side,
                    fields["structure"],
                )
            if self.counter_flow_filter:
                flow = counter_flow(
                    side,
                    prints,
                    coin=coin_u,
                    now=now,
                    mid=ref_px,
                    lookback_sec=self.counter_flow_sec,
                    usdc=self.counter_flow_usdc,
                    eps=self.counter_flow_eps,
                    flip_ratio=self.counter_flow_flip,
                    hold_sec=self.counter_flow_hold_sec,
                )
                fields["counter_flow"] = flow.label()
                if flow.blocked:
                    return _done(COUNTER_FLOW, **fields)

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
            tp_swings = confirmed_swings(bars, kind=tp_kind, now=now)
            tp_levels = [item.price for item in tp_swings]
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
            # The min-stop walk below may widen ``stop`` again. The HTF
            # check runs after that walk, once the stop price is final.

            # Min stop distance (MODEL_B_MIN_STOP_BPS). A liquidity stop
            # tighter than that (BTC 22:56 Oct 7: 23 pts / 2.8 bps) is moved
            # out to the next real opposing liquidity -- a confirmed swing
            # or an untaken pool -- past the min distance, with the usual
            # buffer beyond it, the way the Oct 7 winners (17-22 bps) sat.
            # No such level, or no pool target at tp_r (1.67R) of the moved
            # stop: skip as STOP_TOO_TIGHT. A stop already past the min is
            # untouched.
            moved_tp_floor: float | None = None
            min_dist = limit * self.min_stop_bps / 10_000.0 if self.min_stop_bps > 0 else 0.0
            if min_dist > 0 and abs(limit - stop) + 1e-12 < min_dist:
                structural = [
                    item.price
                    for item in confirmed_swings(bars, kind=stop_kind, now=now)
                ]
                for item in pools:
                    if item.taken or item.price <= 0:
                        continue
                    structural.append(item.price)
                buf = stop_buffer(limit, tick, atr14(bars, now=now))
                moved: float | None = None
                for level in structural:
                    if side == "long":
                        cand = level - buf
                        if level >= limit or limit - cand + 1e-12 < min_dist:
                            continue
                        if moved is None or cand > moved:
                            moved = cand
                    else:
                        cand = level + buf
                        if level <= limit or cand - limit + 1e-12 < min_dist:
                            continue
                        if moved is None or cand < moved:
                            moved = cand
                if moved is not None and tick > 0:
                    steps = moved / tick
                    moved = (
                        math.floor(steps + 1e-9) * tick
                        if side == "long"
                        else math.ceil(steps - 1e-9) * tick
                    )
                    moved = float(f"{moved:.10g}")
                if (
                    moved is None
                    or moved <= 0
                    or not stop_is_valid(side, limit, moved)
                    or collides_with_fill(limit, moved, tick)
                ):
                    return _done(STOP_TOO_TIGHT, **fields)
                stop = moved
                moved_tp_floor = max(
                    _dist(limit, stop), self.tp_r * abs(limit - stop)
                )

            if self.htf_stop != "off":
                htf = check_htf_stop(
                    side,
                    limit,
                    stop,
                    bars,
                    now,
                    tick,
                    atr14(bars, now=now),
                )
                if htf.inside and htf.beyond is not None and htf.beyond > 0:
                    if (
                        self.htf_stop == "on"
                        and stop_is_valid(side, limit, htf.beyond)
                        and not collides_with_fill(limit, htf.beyond, tick)
                    ):
                        logger.info(
                            "MODEL_B HTF_STOP %s %s mode=on stop=%s was=%s "
                            "swing=%s tf=%s beyond=%s under_swing=%s",
                            coin_u,
                            side,
                            htf.beyond,
                            stop,
                            htf.swing,
                            htf.tf,
                            htf.beyond,
                            int(htf.under_swing),
                        )
                        stop = htf.beyond
                        if moved_tp_floor is not None:
                            moved_tp_floor = max(
                                _dist(limit, stop), self.tp_r * abs(limit - stop)
                            )
                    elif self.htf_stop == "shadow":
                        logger.info(
                            "MODEL_B HTF_STOP %s %s mode=shadow inside=1 "
                            "stop=%s swing=%s tf=%s beyond=%s under_swing=%s",
                            coin_u,
                            side,
                            stop,
                            htf.swing,
                            htf.tf,
                            htf.beyond,
                            int(htf.under_swing),
                        )
            # xyz min-stop floor. Only loosens. The TP walk below uses the
            # stop this leaves, so a wider stop that cannot pay 1.5R does
            # not arm. Shadow logs and keeps the liquidity stop.
            if self.xyz_min_stop != "off":
                floored = xyz_floor_stop(
                    side,
                    limit,
                    stop,
                    tick,
                    coin_u,
                    self.xyz_min_stop_bps,
                )
                if floored is not None and floored > 0:
                    if (
                        self.xyz_min_stop == "on"
                        and stop_is_valid(side, limit, floored)
                        and not collides_with_fill(limit, floored, tick)
                    ):
                        logger.info(
                            "MODEL_B XYZ_MIN_STOP %s %s mode=on bps=%.1f "
                            "stop=%s was=%s",
                            coin_u,
                            side,
                            self.xyz_min_stop_bps,
                            floored,
                            stop,
                        )
                        stop = floored
                        if moved_tp_floor is not None:
                            moved_tp_floor = max(
                                _dist(limit, stop), self.tp_r * abs(limit - stop)
                            )
                    elif self.xyz_min_stop == "shadow":
                        logger.info(
                            "MODEL_B XYZ_MIN_STOP %s %s mode=shadow bps=%.1f "
                            "stop=%s would=%s",
                            coin_u,
                            side,
                            self.xyz_min_stop_bps,
                            stop,
                            floored,
                        )
            try:
                lev = int(leverage)
            except (TypeError, ValueError):
                lev = 20
            if lev < 1:
                lev = 20
            try:
                size, _dollar = size_from_stop(
                    equity,
                    limit,
                    stop,
                    risk_pct=self.risk_pct,
                    leverage=lev,
                    notional_leverage=self.max_notional_leverage,
                    min_stop_bps=self.min_stop_bps,
                    include_fees=self.cap_includes_fees,
                    maker_fee=maker_fee,
                    taker_fee=taker_fee,
                )
            except ValueError:
                return _done(BAD_STOP, **fields)
            # Hard 2% cap on the loss at the stop (Chris's absolute rule).
            size = cap_size_to_loss(
                size,
                limit,
                stop,
                equity,
                include_fees=self.cap_includes_fees,
                maker_fee=maker_fee,
                taker_fee=taker_fee,
            )
            if self.stop_slip != "off":
                slip_bps, slip_src = resolve_stop_slip_bps(
                    coin_u,
                    self.stop_slip_bps,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    entry=limit,
                )
                would = 0.0
                try:
                    slipped, _slipped_dollar = size_from_stop(
                        equity,
                        limit,
                        stop,
                        risk_pct=self.risk_pct,
                        leverage=lev,
                        notional_leverage=self.max_notional_leverage,
                        min_stop_bps=self.min_stop_bps,
                        include_fees=self.cap_includes_fees,
                        maker_fee=maker_fee,
                        taker_fee=taker_fee,
                        slip_bps=slip_bps,
                    )
                    would = cap_size_to_loss(
                        slipped,
                        limit,
                        stop,
                        equity,
                        include_fees=self.cap_includes_fees,
                        maker_fee=maker_fee,
                        taker_fee=taker_fee,
                        slip_bps=slip_bps,
                    )
                except ValueError:
                    would = 0.0
                if self.stop_slip == "on":
                    if would <= 0:
                        return _done(BAD_STOP, **fields)
                    logger.info(
                        "MODEL_B STOP_SLIP %s %s mode=on bps=%.2f source=%s "
                        "size=%s was=%s",
                        coin_u,
                        side,
                        slip_bps,
                        slip_src,
                        would,
                        size,
                    )
                    size = would
                else:
                    logger.info(
                        "MODEL_B STOP_SLIP %s %s mode=shadow bps=%.2f source=%s "
                        "size=%s would_size=%s",
                        coin_u,
                        side,
                        slip_bps,
                        slip_src,
                        size,
                        would,
                    )
            if size <= 0:
                return _done(BAD_STOP, **fields)
            adjust = size_adjust_tag(limit, stop)

            # Nearest liquidity that clears fees and 1R, then the 1.5R
            # next-pool walk. Untaken-only drops 1m swings price has already
            # traded through. Shadow logs that pick and keeps today's level.
            # The stop and the size above are not touched.
            levels_all = list(tp_levels)
            if self.tp_untaken_only == "off":
                levels = levels_all
                levels_untaken = levels_all
            else:
                spent = filter_spent_swing_prices(side, tp_swings, bars, now, last_px)
                levels_untaken = [px for px in levels_all if px not in spent]
                levels = levels_untaken if self.tp_untaken_only == "on" else levels_all
            floor = moved_tp_floor if moved_tp_floor is not None else _dist(limit, stop)
            walked = walk_liquidity_tp(
                side,
                limit,
                stop,
                tick,
                levels,
                coin=coin_u,
                tp_r=self.tp_r,
                min_pool_r=self.tp_min_pool_r,
                max_pool_r=self.tp_max_pool_r,
                far_skip=self.tp_far_skip_countertrend,
                moved_floor=moved_tp_floor,
                trend_fn=_trend,
                distance_fn=_dist,
            )
            for fmt, args in walked.logs:
                logger.info(fmt, *args)
            trend_label = walked.trend_label
            if walked.fail:
                return replace(
                    _done(walked.fail, pool_r=walked.pool_r, **fields),
                    trend=trend_label,
                )
            target = walked.target
            if self.tp_untaken_only == "shadow":
                alt = walk_liquidity_tp(
                    side,
                    limit,
                    stop,
                    tick,
                    levels_untaken,
                    coin=coin_u,
                    tp_r=self.tp_r,
                    min_pool_r=self.tp_min_pool_r,
                    max_pool_r=self.tp_max_pool_r,
                    far_skip=self.tp_far_skip_countertrend,
                    moved_floor=moved_tp_floor,
                    trend_fn=_trend,
                    distance_fn=_dist,
                )
                logger.info(
                    "MODEL_B TP_UNTAKEN shadow %s %s kept=%s would=%s",
                    coin_u,
                    side,
                    target,
                    alt.fail or alt.target,
                )
            elif self.tp_untaken_only == "on" and levels is not levels_all:
                plain = walk_liquidity_tp(
                    side,
                    limit,
                    stop,
                    tick,
                    levels_all,
                    coin=coin_u,
                    tp_r=self.tp_r,
                    min_pool_r=self.tp_min_pool_r,
                    max_pool_r=self.tp_max_pool_r,
                    far_skip=self.tp_far_skip_countertrend,
                    moved_floor=moved_tp_floor,
                    trend_fn=_trend,
                    distance_fn=_dist,
                )
                if plain.target != target:
                    logger.info(
                        "MODEL_B TP_UNTAKEN %s %s tp=%s was=%s",
                        coin_u,
                        side,
                        target,
                        plain.fail or plain.target,
                    )
            nearest = next_liquidity(side, limit, tick, levels)
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

            runner_px = None
            if self.tp_runner != "off":
                sig = significant_levels(side, limit, bars, now, last_px, tick)
                runner_px = runner_target(
                    side, limit, stop, tp, sig, max_r=self.tp_runner_max_r
                )
                tp1_sz, run_sz = split_runner_size(size, self.tp_runner_frac)
                logger.info(
                    "MODEL_B TP_RUNNER %s %s mode=%s tp1=%s tp1_size=%s tp2=%s "
                    "runner_size=%s frac=%s max_r=%s trail=1m_after_be",
                    coin_u,
                    side,
                    self.tp_runner,
                    tp,
                    tp1_sz,
                    runner_px,
                    run_sz,
                    self.tp_runner_frac,
                    self.tp_runner_max_r,
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
                leverage=lev,
                work_sec=self.alo_timeout_sec,
                sweep_px=metrics.sweep_price,
                tick=tick,
                pool_px=target,
                tp_r=self.tp_r,
                runner_px=runner_px,
                runner_mode=self.tp_runner if runner_px is not None else "off",
            )
            armed_decision = _done(
                None,
                armed=True,
                intent=intent,
                size_adjust=adjust,
                delta_flat=metrics.delta_flat,
                sizing_dist=max(abs(limit - stop), limit * self.min_stop_bps / 10_000.0),
                **fields,
            )
            if self.tp_min_pool_r > 0:
                if trend_label is None:
                    trend_label = _trend().label()
                    logger.info("MODEL_B TREND %s %s side=%s", coin_u, trend_label, side)
                armed_decision = replace(armed_decision, trend=trend_label)
            return armed_decision

        if coin_u not in self.coins:
            return _done(OUT_OF_SESSION)

        # Fail closed before any delta / absorb math that would skip a print.
        if window and missing_side(window):
            return _done(NO_SIDE)

        if len(window) < self.min_prints:
            return _done(THIN_TAPE)

        if last_px is None:
            return _done(THIN_TAPE)

        macro: MacroRead | None = None
        macro_label: str | None = None
        if self.macro_side_only != "off":
            macro = self._macro_read(coin_u, htf_bars if htf_bars else bars, now)
            macro_label = macro.label(self.macro_range_policy)

        def _macro_blocked(side: str) -> bool:
            return macro is not None and not macro.allows(side, self.macro_range_policy)

        def _side_try(side: str, pool) -> Decision:
            if self.macro_side_only == "on" and _macro_blocked(side):
                return replace(_done(MACRO_SIDE), side=side, macro=macro_label)
            d = replace(attempt(side, pool), side=side)
            if macro is not None and d.armed:
                if self.macro_side_only == "shadow" and _macro_blocked(side):
                    logger.info(
                        "MODEL_B SHADOW %s reason=MACRO_SIDE would_block=1 side=%s %s",
                        coin_u, side, macro_label,
                    )
                    d = replace(d, macro=f"would_block {macro_label}")
                else:
                    d = replace(d, macro=macro_label)
            return d

        if self.two_sided:
            return self._pick_two_sided(
                [
                    _side_try("long", bias.pool_above),
                    _side_try("short", bias.pool_below),
                ],
                _structure,
            )

        if bias.side == "long":
            sides = [("long", bias.pool)]
        elif bias.side == "short":
            sides = [("short", bias.pool)]
        else:
            # NONE: both directions are allowed. Each side targets the
            # untaken pool on that side when one exists.
            sides = [("long", bias.pool_above), ("short", bias.pool_below)]

        if self.macro_side_only == "off":
            results = [attempt(side, pool) for side, pool in sides]
        else:
            # side= is set by _side_try; one-sided mode keeps its old log shape.
            results = [replace(_side_try(side, pool), side=None) for side, pool in sides]
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

    def _macro_read(self, coin: str, bars: list[dict], now: float) -> MacroRead:
        """1h/4h ADX macro, recomputed (and logged) once per coin per 1h bar."""
        bucket = int(float(now) // 3600)
        hit = self._macro_cache.get(coin)
        if hit is not None and hit[0] == bucket:
            return hit[1]
        try:
            read = read_macro(bars, now, adx_min=self.macro_adx_min, mode=self.macro_mode)
        except Exception:
            logger.exception("MODEL_B MACRO read failed for %s", coin)
            read = MacroRead(
                AdxTrend("1h", "unknown"), AdxTrend("4h", "unknown"), "range", self.macro_mode
            )
        self._macro_cache[coin] = (bucket, read)
        logger.info(
            "MODEL_B MACRO %s %s mode=%s adx_min=%s policy=%s filter=%s",
            coin, read.label(self.macro_range_policy), self.macro_mode,
            self.macro_adx_min, self.macro_range_policy, self.macro_side_only,
        )
        return read

    @staticmethod
    def _pick_two_sided(results: list[Decision], structure) -> Decision:
        """One decision per coin from the long and the short attempt.

        Only one side armed -> that side. Both armed -> the side the 15m/1h
        structure agrees with (bull for long, bear for short; one ticket
        per coin), then higher absorb, then long. Score is per coin, the
        same for both sides, so it cannot break the tie. Neither armed ->
        the side that got furthest. The other side rides along in
        ``other_sides`` so the loop logs one FAIL line per side.
        """
        armed = [item for item in results if item.armed and item.intent is not None]
        if len(armed) == 1:
            chosen = armed[0]
        elif len(armed) > 1:
            try:
                states = structure() or []
            except Exception:
                states = []

            def _agrees(item: Decision) -> int:
                want = "bull" if item.side == "long" else "bear"
                against = "bear" if item.side == "long" else "bull"
                return sum(1 for st in states if st.state == want) - sum(
                    1 for st in states if st.state == against
                )

            chosen = max(
                armed,
                key=lambda item: (
                    _agrees(item),
                    item.score,
                    item.absorb or 0.0,
                    1 if item.side == "long" else 0,
                ),
            )
        else:
            chosen = max(
                results,
                key=lambda item: (
                    _FAIL_RANK.get(item.fail_reason or "", 0),
                    1 if item.side == "long" else 0,
                ),
            )
        others = tuple(item for item in results if item is not chosen)
        return replace(chosen, other_sides=others)


def _meaningful_level(bars, now, side, level, pools, tick) -> bool:
    """True when ``level`` sits on a 15m swing (3-bar pivot) or an untaken pool."""
    from hl_bot.strategy.model_b.structure import _pivots, resample

    tol = max(2.0 * float(tick), abs(float(level)) * 2.0 / 10_000.0)
    for item in pools or []:
        if not item.taken and item.price > 0 and abs(item.price - level) <= tol:
            return True
    candles = resample(bars, 900, now)[-48:]
    field = "l" if side == "long" else "h"
    return any(abs(px - level) <= tol for px in _pivots(candles, field, 1)[-8:])


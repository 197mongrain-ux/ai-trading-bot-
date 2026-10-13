"""PDL/PDH retest entry (paper only) and its 90-day replay.

Prior day is the UTC day (Hyperliquid daily candles roll at 00:00 UTC,
20:00 Toronto in summer). Short side, mirrored for longs at PDH:

1. Two or more consecutive closed 1h bars close below PDL (same UTC day).
2. Macro from the existing swing read (1h+4h, 4h_lead, ADX>20) is down or range.
3. A later bar (retest bar) trades up to PDL (within ``touch_bps`` under it,
   at most ``max_above_bps`` over it) and closes back below PDL: the stall.
4. Loose flow check: bar delta < 0 (tape when we have it, else close in the
   lower half of the bar), or buyers absorbed (volume >= 20-bar average and
   upper wick >= 40% of the range). Looser than the swing absorption gate.
5. Entry at the retest-bar close. Stop above the retest-bar high plus
   0.1 x ATR(14, 1h). Target is the nearest 4h support below entry; skip if
   room < 2R. Size to 2% of book (fees + slip reserve included), <=20x.
   One position at a time across the book. 72h time stop.

Nothing here touches live trading.
"""
from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from hl_bot.strategy.model_b.fees import conservative_fees
from hl_bot.strategy.model_b.swing import (
    SwingParams,
    aggregate,
    atr14_of,
    build_levels,
    read_swing_macro,
    size_swing,
)

DAY = 86400
H1 = 3600


@dataclass(frozen=True)
class RetestParams:
    min_closes: int = 2
    touch_bps: float = 5.0
    max_above_bps: float = 40.0
    stop_atr: float = 0.1
    min_room_r: float = 2.0
    risk_pct: float = 0.02
    max_lev: int = 20
    max_hold_h: float = 72.0
    entry_slip_bps: float = 2.0
    stop_slip_bps: float = 5.0
    retest_tf: int = H1  # 3600 or 900
    # wick: retest-bar extreme + stop_atr x ATR. h1: last closed 1h bar extreme.
    # h2: extreme of the last two closed 1h bars. lvl_atr: PDL/PDH + stop_atr x ATR.
    stop_mode: str = "wick"


LEVEL_PARAMS = SwingParams(
    level_set="4h", top_n=12, min_score=1.0, macro_tfs=("1h", "4h"), macro_mode="4h_lead", adx_min=20.0,
    max_lev=20,
)


def norm(raw: list[dict]) -> list[dict]:
    out = []
    for b in raw:
        t = float(b["t"])
        if t > 1e11:
            t /= 1000.0
        out.append({"t": t, "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                    "c": float(b["c"]), "v": float(b.get("v") or 0)})
    out.sort(key=lambda x: x["t"])
    return out


def tape_delta(coin_dir: Path, tf: int) -> dict[float, float]:
    """Signed taker volume per bar from recorded HL prints (side B = buyer lifted)."""
    out: dict[float, float] = {}
    if not coin_dir.exists():
        return out
    for f in sorted(coin_dir.glob("*.trades.jsonl.gz")):
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = float(int(r["time"] / 1000 // tf) * tf)
                sz = float(r["sz"])
                out[k] = out.get(k, 0.0) + (sz if r["side"] == "B" else -sz)
    return out


def flow_ok(side: str, bar: dict, recent: list[dict], delta: float | None) -> tuple[bool, str]:
    rng = bar["h"] - bar["l"]
    if delta is not None:
        if (side == "short" and delta < 0) or (side == "long" and delta > 0):
            return True, f"tape_delta={delta:+.2f}"
    elif rng > 0:
        clv = (bar["c"] - bar["l"]) / rng
        if (side == "short" and clv < 0.5) or (side == "long" and clv > 0.5):
            return True, f"clv={clv:.2f}"
    if rng > 0 and recent:
        avg_v = sum(b["v"] for b in recent) / len(recent)
        wick = (bar["h"] - max(bar["o"], bar["c"])) if side == "short" else (min(bar["o"], bar["c"]) - bar["l"])
        if bar["v"] >= avg_v and wick / rng >= 0.4:
            return True, "absorbed"
    return False, "flow_against"


@dataclass
class Trade:
    coin: str
    side: str
    entry_t: float
    entry: float
    stop: float
    target: float
    size: float
    risk_usd: float
    equity: float
    level: float
    flow: str
    macro: str
    exit_t: float = 0.0
    exit: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    r: float = 0.0

    def row(self) -> dict:
        d = asdict(self)
        d["entry_utc"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.entry_t))
        d["exit_utc"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.exit_t)) if self.exit_t else ""
        return d


def sanity(t: Trade, p: RetestParams) -> list[str]:
    bad = []
    if t.side == "short" and not (t.stop > t.entry > t.target):
        bad.append("short stop/target order")
    if t.side == "long" and not (t.stop < t.entry < t.target):
        bad.append("long stop/target order")
    if t.risk_usd > p.risk_pct * t.equity * 1.0001:
        bad.append(f"risk {t.risk_usd:.2f} > {p.risk_pct:.0%} of {t.equity:.2f}")
    if t.size * t.entry > p.max_lev * t.equity * 1.0001:
        bad.append(f"lev {t.size * t.entry / t.equity:.1f}x > {p.max_lev}x")
    if t.size <= 0:
        bad.append("size 0")
    return bad


def find_setup(coin: str, hourly: list[dict], bars: list[dict], i: int, p: RetestParams,
               deltas: dict[float, float], used: set) -> dict | None:
    """Check the bar ``bars[i]`` (retest_tf wide, just closed) for a retest entry."""
    bar = bars[i]
    now = bar["t"] + p.retest_tf
    day0 = int(bar["t"] // DAY) * DAY
    daily = aggregate(hourly, H1, DAY, day0)
    if not daily or daily[-1]["t"] != day0 - DAY:
        return None
    pdh, pdl = daily[-1]["h"], daily[-1]["l"]
    h1 = [b for b in aggregate(hourly, H1, H1, now) if b["t"] >= day0]
    # Confirmation must finish before the retest bar opens.
    h1_before = [b for b in h1 if b["t"] + H1 <= bar["t"] + 1e-9]
    for side, lvl in (("short", pdl), ("long", pdh)):
        if (coin, day0, side) in used:
            continue
        run = best = 0
        for b in h1_before:
            hit = b["c"] < lvl if side == "short" else b["c"] > lvl
            run = run + 1 if hit else 0
            best = max(best, run)
        if best < p.min_closes:
            continue
        if side == "short":
            reach = bar["h"] >= lvl * (1 - p.touch_bps / 1e4) and bar["h"] <= lvl * (1 + p.max_above_bps / 1e4)
            stall = bar["c"] < lvl
        else:
            reach = bar["l"] <= lvl * (1 + p.touch_bps / 1e4) and bar["l"] >= lvl * (1 - p.max_above_bps / 1e4)
            stall = bar["c"] > lvl
        if not (reach and stall):
            continue
        macro = read_swing_macro(hourly, now, LEVEL_PARAMS).macro
        if macro not in ("range", "down" if side == "short" else "up"):
            continue
        ok, why = flow_ok(side, bar, bars[max(0, i - 20):i], deltas.get(bar["t"]))
        if not ok:
            continue
        atr = atr14_of(aggregate(hourly, H1, H1, now)) or 0.0
        entry = bar["c"]
        last = [b for b in aggregate(hourly, H1, H1, now)][-2:]
        if p.stop_mode == "h1":
            stop = last[-1]["h"] if side == "short" else last[-1]["l"]
        elif p.stop_mode == "h2":
            stop = max(b["h"] for b in last) if side == "short" else min(b["l"] for b in last)
        elif p.stop_mode == "lvl_atr":
            stop = lvl + p.stop_atr * atr if side == "short" else lvl - p.stop_atr * atr
        else:
            stop = bar["h"] + p.stop_atr * atr if side == "short" else bar["l"] - p.stop_atr * atr
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        levels = build_levels(hourly, now, LEVEL_PARAMS)
        if side == "short":
            cands = sorted((lv.price for lv in levels if lv.kind == "support" and lv.price < entry), reverse=True)
        else:
            cands = sorted(lv.price for lv in levels if lv.kind == "resistance" and lv.price > entry)
        # Ignore 4h prints hugging the entry (<0.25R); those are not the next floor.
        cands = [c for c in cands if abs(c - entry) >= 0.25 * risk]
        if not cands:
            return {"skip": "no 4h level", "side": side, "t": now, "level": lvl, "day": day0}
        target = cands[0]
        room = abs(target - entry) / risk
        if room < p.min_room_r:
            return {"skip": f"room {room:.2f}R < {p.min_room_r}R", "side": side, "t": now, "level": lvl,
                    "entry": entry, "stop": stop, "target": target, "day": day0}
        return {"side": side, "t": now, "level": lvl, "entry": entry, "stop": stop, "target": target,
                "flow": why, "macro": macro, "room": room, "day": day0}
    return None


def replay(data: dict[str, dict], p: RetestParams, start: float, end: float, equity0: float = 5000.0,
           tape_root: Path | None = None) -> dict:
    tf = p.retest_tf
    key = "1h" if tf == H1 else "15m"
    deltas = {c: (tape_delta(tape_root / c, tf) if tape_root else {}) for c in data}
    # Exit resolution: finest bars we have (15m) else 1h.
    events = []
    for coin, d in data.items():
        for i, b in enumerate(d[key]):
            if start <= b["t"] < end:
                events.append((b["t"] + tf, coin, i))
    events.sort()
    eq = peak = equity0
    max_dd = 0.0
    trades: list[Trade] = []
    skips: list[dict] = []
    used: set = set()
    pos: Trade | None = None
    issues = []

    def path(coin, t0, t1):
        fine = [b for b in data[coin].get("15m", []) if t0 <= b["t"] and b["t"] + 900 <= t1]
        if fine and fine[0]["t"] <= t0 + 900:
            return fine, 900
        return [b for b in data[coin]["1h"] if t0 <= b["t"] and b["t"] + H1 <= t1], H1

    for now, coin, i in events:
        if pos is not None and now >= pos.exit_t:
            eq += pos.pnl
            peak = max(peak, eq)
            pos = None
        if pos is not None:
            continue
        s = find_setup(coin, data[coin]["1h"], data[coin][key], i, p, deltas[coin], used)
        if not s:
            continue
        used.add((coin, s["day"], s["side"]))
        if "skip" in s:
            skips.append({"coin": coin, **s, "utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["t"]))})
            continue
        fees = conservative_fees(coin)
        side = s["side"]
        sgn = 1 if side == "long" else -1
        entry = s["entry"] * (1 + sgn * p.entry_slip_bps / 1e4)
        size, loss = size_swing(coin, entry, s["stop"], eq, LEVEL_PARAMS, risk_pct=p.risk_pct,
                                max_leverage=p.max_lev, maker_fee=fees.taker, taker_fee=fees.taker)
        t = Trade(coin, side, now, entry, s["stop"], s["target"], size, loss, eq, s["level"], s["flow"], s["macro"])
        bad = sanity(t, p)
        if bad:
            issues.append({"coin": coin, "t": now, "bad": bad})
            continue
        bars, btf = path(coin, now, now + p.max_hold_h * H1 + btf_pad())
        exit_px, exit_t, reason = None, None, ""
        worst = 0.0
        for b in bars:
            adverse = (b["h"] - entry) if side == "short" else (entry - b["l"])
            worst = max(worst, adverse * size)
            stop_hit = b["h"] >= t.stop if side == "short" else b["l"] <= t.stop
            tp_hit = b["l"] <= t.target if side == "short" else b["h"] >= t.target
            if stop_hit:  # stop wins a tie
                exit_px, exit_t, reason = t.stop * (1 - sgn * p.stop_slip_bps / 1e4), b["t"] + btf, "stop"
                break
            if tp_hit:
                exit_px, exit_t, reason = t.target, b["t"] + btf, "target"
                break
            if b["t"] + btf >= now + p.max_hold_h * H1:
                exit_px, exit_t, reason = b["c"], b["t"] + btf, "time"
                break
        if exit_px is None:
            if bars:
                exit_px, exit_t, reason = bars[-1]["c"], bars[-1]["t"] + btf, "open_at_end"
            else:
                exit_px, exit_t, reason = entry, now, "open_at_end"
        gross = sgn * (exit_px - entry) * size
        fee = size * entry * fees.taker + size * exit_px * (fees.maker if reason == "target" else fees.taker)
        t.exit, t.exit_t, t.reason = exit_px, exit_t, reason
        t.pnl = gross - fee
        t.r = t.pnl / loss if loss > 0 else 0.0
        max_dd = max(max_dd, (peak - (eq - min(worst, loss * 1.5))) / peak)
        trades.append(t)
        pos = t
    if pos is not None:
        eq += pos.pnl
        peak = max(peak, eq)
    # Closed-trade drawdown (equity curve), plus intratrade worst already folded in.
    curve = equity0
    pk = equity0
    for tr in trades:
        curve += tr.pnl
        pk = max(pk, curve)
        max_dd = max(max_dd, (pk - curve) / pk)
    wins = [x for x in trades if x.pnl > 0]
    return {
        "params": asdict(p),
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_r": sum(x.r for x in trades) / len(trades) if trades else 0.0,
        "total_r": sum(x.r for x in trades),
        "equity_end": eq,
        "net_pct": (eq / equity0 - 1) * 100,
        "max_dd_pct": max_dd * 100,
        "sanity_failures": issues,
        "rows": [x.row() for x in trades],
        "skips": skips,
    }


def btf_pad() -> float:
    return H1


def load(cache: Path, coins=("BTC", "ETH", "SOL"), tape: Path | None = None) -> dict[str, dict]:
    out = {}
    for c in coins:
        d = {"1h": norm(json.loads((cache / f"{c}_1h.json").read_text()))}
        f15 = cache / f"{c}_15m.json"
        d["15m"] = norm(json.loads(f15.read_text())) if f15.exists() else []
        out[c] = d
    return out


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="/workspace/pdl_cache")
    ap.add_argument("--tape", default="/workspace/hl_tape")
    ap.add_argument("--days", type=float, default=90)
    ap.add_argument("--end", type=float, default=0)
    ap.add_argument("--out", default="docs/pnl/pdl_retest_replay.json")
    a = ap.parse_args(argv)
    data = load(Path(a.cache))
    end = a.end or max(d["1h"][-1]["t"] + H1 for d in data.values())
    res = {}
    res["1h_90d"] = replay(data, RetestParams(retest_tf=H1), end - a.days * DAY, end, tape_root=Path(a.tape))
    s15 = max(d["15m"][0]["t"] for d in data.values()) + 2 * DAY
    res["15m_since_" + time.strftime("%m%d", time.gmtime(s15))] = replay(
        data, RetestParams(retest_tf=900), s15, end, tape_root=Path(a.tape))
    res["1h_90d_stop0.5atr"] = replay(data, RetestParams(retest_tf=H1, stop_atr=0.5), end - a.days * DAY, end,
                                      tape_root=Path(a.tape))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    for k, r in res.items():
        print(f"{k}: trades={r['trades']} wr={r['win_rate']:.0%} avgR={r['avg_r']:+.2f} totR={r['total_r']:+.2f} "
              f"net={r['net_pct']:+.1f}% maxDD={r['max_dd_pct']:.1f}% sanity_fail={len(r['sanity_failures'])} skips={len(r['skips'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


STOP_VARIANTS = {
    "wick+0.1atr (baseline)": dict(stop_mode="wick", stop_atr=0.1),
    "wick+0.25atr": dict(stop_mode="wick", stop_atr=0.25),
    "wick+0.5atr": dict(stop_mode="wick", stop_atr=0.5),
    "wick+0.75atr": dict(stop_mode="wick", stop_atr=0.75),
    "last 1h high": dict(stop_mode="h1", stop_atr=0.0),
    "last 2x1h high": dict(stop_mode="h2", stop_atr=0.0),
    "PDL+1atr": dict(stop_mode="lvl_atr", stop_atr=1.0),
}


def _one(args):
    name, kw, start, end, cache, tape = args
    data = load(Path(cache))
    for d in data.values():
        d["15m"] = []  # 1h exits in both periods so they compare
    r = replay(data, RetestParams(retest_tf=H1, **kw), start, end, tape_root=Path(tape))
    r.pop("skips", None)
    return name, r


def stop_study(cache="/workspace/pdl_cache", tape="/workspace/hl_tape", split_days=90.0, warmup_days=31.0,
               out="docs/pnl/pdl_retest_stops.json") -> dict:
    from multiprocessing import Pool

    data = load(Path(cache))
    first = max(d["1h"][0]["t"] for d in data.values())
    end = min(d["1h"][-1]["t"] + H1 for d in data.values())
    split = end - split_days * DAY
    ins = (first + warmup_days * DAY, split)
    held = (split, end)
    jobs = []
    for name, kw in STOP_VARIANTS.items():
        jobs.append(((name, "in"), kw, *ins, cache, tape))
        jobs.append(((name, "held"), kw, *held, cache, tape))
    with Pool(min(8, len(jobs))) as pool:
        got = pool.map(_one, jobs)
    res = {"in_sample": {}, "held_out": {},
           "periods_utc": {k: [time.strftime("%Y-%m-%d", time.gmtime(a)), time.strftime("%Y-%m-%d", time.gmtime(b))]
                           for k, (a, b) in (("in_sample", ins), ("held_out", held))}}
    for (name, which), r in got:
        res["in_sample" if which == "in" else "held_out"][name] = r
    # Pick on in-sample only: total R, ties to lower drawdown.
    best = max(res["in_sample"], key=lambda n: (res["in_sample"][n]["total_r"], -res["in_sample"][n]["max_dd_pct"]))
    res["pick_in_sample"] = best
    Path(out).write_text(json.dumps(res, indent=1))
    return res

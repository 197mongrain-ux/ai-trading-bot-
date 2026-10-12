"""Similar-setup search for PDL/PDH breakdown-retest setups (BTC/ETH/SOL). Research only.

Every retest candidate from Hyperliquid 1h candles (UTC prior day) gets a feature
vector and an outcome. Candidates here ignore the macro, flow and 2R-room filters
(those become features) so the sample is as wide as possible.

Outcome per setup: stop = retest-bar extreme + 0.25 ATR (the best held-out stop);
target = next 4h level beyond 0.25R. ``mfe_r`` = best R reached before the stop
(72h cap, stop wins same-bar ties). ``win`` = mfe_r >= 2.

  python -m hl_bot.research.setup_search scan   [--top 25]
  python -m hl_bot.research.setup_search near   --coin BTC --at "2026-10-12 16:00" [--k 10]
  python -m hl_bot.research.setup_search now    [--k 8]       # live HL candles
  python -m hl_bot.research.setup_search stats
Output: markdown on stdout and docs/pnl/setup_search_*.md / .html.
"""
from __future__ import annotations

import json
import math
import statistics as st
import time
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hl_bot.research.pdl_retest import LEVEL_PARAMS, load, tape_delta
from hl_bot.strategy.model_b.swing import aggregate, build_levels
from hl_bot.strategy.model_b.trend import adx_series

H1, H4, DAY = 3600, 14400, 86400
ET = ZoneInfo("America/New_York")
COINS = ("BTC", "ETH", "SOL")
STOP_ATR = 0.25
TOUCH_BPS, MAX_ABOVE_BPS = 5.0, 40.0

FEATURES = [
    "trend_with",      # +1 4h DI agrees with trade side, -1 against
    "adx_1h", "adx_4h",
    "closes_beyond",   # 1h closes beyond the level today before the retest
    "depth_pct",       # how far the retest wick went past the level, % of price
    "depth_atr",
    "swept",           # 1 = wick pierced the level by >5bp and closed back
    "clv_with",        # retest-bar close location in trade direction (1 = best)
    "prior_clv_with",  # same, mean of the 3 bars before
    "rel_vol",         # retest-bar volume / 20-bar mean
    "room_r",          # distance to next 4h level in R
    "atr_pct",         # ATR(14,1h) / price, %
    "atr_regime",      # ATR now / 30-day median ATR
    "et_hour",
    "dow",
    "vwap_dist_atr",   # (close - UTC-day VWAP) / ATR, signed in trade direction
    "pd_range_atr",    # prior-day range / ATR
]


@dataclass
class Setup:
    coin: str
    side: str
    t: float              # retest-bar close, unix sec
    level: float
    entry: float
    stop: float
    target: float | None
    f: dict = field(default_factory=dict)
    mfe_r: float | None = None
    r: float | None = None
    win: int | None = None
    note: str = ""

    @property
    def utc(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.t))

    @property
    def et(self) -> str:
        return datetime.fromtimestamp(self.t, ET).strftime("%Y-%m-%d %H:%M ET")


def _clv(b: dict, side: str) -> float:
    rng = b["h"] - b["l"]
    if rng <= 0:
        return 0.5
    x = (b["c"] - b["l"]) / rng
    return 1 - x if side == "short" else x


def _atr(h1: list[dict], i: int) -> float:
    lo = max(1, i - 13)
    trs = [max(h1[j]["h"] - h1[j]["l"], abs(h1[j]["h"] - h1[j - 1]["c"]), abs(h1[j]["l"] - h1[j - 1]["c"]))
           for j in range(lo, i + 1)]
    return sum(trs) / len(trs) if trs else 0.0


class CoinCtx:
    def __init__(self, coin: str, hourly: list[dict], delta: dict | None = None):
        self.coin = coin
        self.h1 = hourly
        self.idx = {b["t"]: i for i, b in enumerate(hourly)}
        end = hourly[-1]["t"] + H1
        self.daily = {b["t"]: b for b in aggregate(hourly, H1, DAY, end)}
        self.h4 = aggregate(hourly, H1, H4, end)
        a1 = adx_series(hourly, 14)
        a4 = adx_series(self.h4, 14)
        self.adx1 = {b["t"]: a for b, a in zip(hourly, a1)}
        self.adx4 = [(b["t"] + H4, a) for b, a in zip(self.h4, a4)]
        self.atrs = [_atr(hourly, i) for i in range(len(hourly))]
        self.delta = delta or {}

    def adx4_at(self, now: float):
        best = None
        for close_t, a in self.adx4:
            if close_t <= now:
                best = a
            else:
                break
        return best

    def setups(self, i_from: int = 40) -> list[Setup]:
        out = []
        seen_t: dict[str, float] = {}
        for i in range(i_from, len(self.h1)):
            s = self.setup_at(i, require_retest=True)
            if s is None:
                continue
            last = seen_t.get(s.side, -1e18)
            if s.t - last < 2 * H1:  # skip the bar right after a setup (no back-to-back duplicates)
                continue
            seen_t[s.side] = s.t
            out.append(s)
        return out

    def setup_at(self, i: int, require_retest: bool = True, side_hint: str | None = None) -> Setup | None:
        h1 = self.h1
        bar = h1[i]
        day0 = int(bar["t"] // DAY) * DAY
        prev = self.daily.get(day0 - DAY)
        if prev is None:
            return None
        today = [b for b in h1[max(0, i - 24): i] if b["t"] >= day0]
        found = []
        for side, lvl in (("short", prev["l"]), ("long", prev["h"])):
            if side_hint and side != side_hint:
                continue
            run = best = 0
            for b in today:
                hit = b["c"] < lvl if side == "short" else b["c"] > lvl
                run = run + 1 if hit else 0
                best = max(best, run)
            if side == "short":
                reach = bar["h"] >= lvl * (1 - TOUCH_BPS / 1e4) and bar["h"] <= lvl * (1 + MAX_ABOVE_BPS / 1e4)
                stall = bar["c"] < lvl
            else:
                reach = bar["l"] <= lvl * (1 + TOUCH_BPS / 1e4) and bar["l"] >= lvl * (1 - MAX_ABOVE_BPS / 1e4)
                stall = bar["c"] > lvl
            if require_retest and not (best >= 2 and reach and stall):
                continue
            found.append((side, lvl, best, prev))
        if not found:
            return None
        if side_hint is None and len(found) > 1:
            # 'now' mode with no retest: keep the side whose level is closer.
            found.sort(key=lambda x: abs(bar["c"] - x[1]))
        side, lvl, closes, prev = found[0]
        return self._build(i, side, lvl, closes, prev)

    def _build(self, i, side, lvl, closes, prev) -> Setup:
        h1, bar = self.h1, self.h1[i]
        now = bar["t"] + H1
        atr = self.atrs[i] or 1e-9
        sgn = -1 if side == "short" else 1
        entry = bar["c"]
        stop = bar["h"] + STOP_ATR * atr if side == "short" else bar["l"] - STOP_ATR * atr
        risk = abs(entry - stop) or 1e-9
        levels = build_levels(h1, now, LEVEL_PARAMS)
        if side == "short":
            c = sorted((lv.price for lv in levels if lv.kind == "support" and lv.price < entry - 0.25 * risk), reverse=True)
        else:
            c = sorted(lv.price for lv in levels if lv.kind == "resistance" and lv.price > entry + 0.25 * risk)
        target = c[0] if c else None
        room = abs(target - entry) / risk if target else 10.0
        a1 = self.adx1.get(bar["t"])
        a4 = self.adx4_at(now)
        di = 0
        if a4:
            di = 1 if (a4[1] > a4[2]) == (side == "long") else -1
        depth = (bar["h"] - lvl) if side == "short" else (lvl - bar["l"])
        day0 = int(bar["t"] // DAY) * DAY
        dbars = [b for b in h1[max(0, i - 24): i + 1] if b["t"] >= day0]
        pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in dbars)
        vv = sum(b["v"] for b in dbars) or 1e-9
        vwap = pv / vv
        win20 = h1[max(0, i - 20): i]
        rel_vol = bar["v"] / (sum(b["v"] for b in win20) / len(win20)) if win20 and sum(b["v"] for b in win20) else 1.0
        med_atr = st.median(self.atrs[max(14, i - 720): i + 1]) if i > 20 else atr
        clv = _clv(bar, side)
        d = self.delta.get(bar["t"])
        if d is not None:
            clv = 1.0 if (d < 0) == (side == "short") else 0.0
        prior = h1[max(0, i - 3): i]
        et = datetime.fromtimestamp(now, ET)
        f = {
            "trend_with": di,
            "adx_1h": a1[0] if a1 else 0.0,
            "adx_4h": a4[0] if a4 else 0.0,
            "closes_beyond": closes,
            "depth_pct": depth / lvl * 100,
            "depth_atr": depth / atr,
            "swept": 1 if depth > lvl * 5 / 1e4 else 0,
            "clv_with": clv,
            "prior_clv_with": sum(_clv(b, side) for b in prior) / len(prior) if prior else 0.5,
            "rel_vol": rel_vol,
            "room_r": min(room, 15.0),
            "atr_pct": atr / entry * 100,
            "atr_regime": atr / med_atr if med_atr else 1.0,
            "et_hour": et.hour,
            "dow": et.weekday(),
            "vwap_dist_atr": sgn * (vwap - entry) / atr * -1,
            "pd_range_atr": (prev["h"] - prev["l"]) / atr,
        }
        return Setup(self.coin, side, now, lvl, entry, stop, target, f)

    def outcome(self, s: Setup, max_h: int = 72) -> None:
        i0 = self.idx.get(s.t)  # first bar after the retest closes
        if i0 is None:
            s.note = "open"
            return
        risk = abs(s.entry - s.stop)
        mfe = 0.0
        r = None
        for b in self.h1[i0: i0 + max_h]:
            if s.side == "short":
                if b["h"] >= s.stop:
                    r = -1.0
                    break
                mfe = max(mfe, (s.entry - b["l"]) / risk)
                if s.target and b["l"] <= s.target:
                    r = (s.entry - s.target) / risk
                    break
            else:
                if b["l"] <= s.stop:
                    r = -1.0
                    break
                mfe = max(mfe, (b["h"] - s.entry) / risk)
                if s.target and b["h"] >= s.target:
                    r = (s.target - s.entry) / risk
                    break
        n = len(self.h1[i0: i0 + max_h])
        if r is None:
            if n < max_h:
                s.note = "open"
                s.mfe_r = mfe
                return
            last = self.h1[min(len(self.h1) - 1, i0 + max_h - 1)]["c"]
            r = (s.entry - last) / risk if s.side == "short" else (last - s.entry) / risk
        s.mfe_r, s.r = mfe, r
        s.win = 1 if mfe >= 2.0 else 0


# ---------- library ----------

def build_library(cache="/workspace/pdl_cache", tape="/workspace/hl_tape", hourly: dict | None = None) -> list[Setup]:
    data = hourly or {c: d["1h"] for c, d in load(Path(cache)).items()}
    lib = []
    for coin, h1 in data.items():
        ctx = CoinCtx(coin, h1, tape_delta(Path(tape) / coin, H1) if tape else None)
        start = next(i for i, b in enumerate(h1) if b["t"] >= h1[0]["t"] + 31 * DAY)
        for s in ctx.setups(start):
            ctx.outcome(s)
            lib.append(s)
    lib.sort(key=lambda s: s.t)
    return lib


def _scales(rows: list[dict]) -> dict[str, tuple[float, float]]:
    out = {}
    for k in FEATURES:
        vals = [r[k] for r in rows]
        mu = st.mean(vals)
        sd = st.pstdev(vals) or 1.0
        out[k] = (mu, sd)
    return out


def _z(f: dict, sc) -> list[float]:
    return [(f[k] - sc[k][0]) / sc[k][1] for k in FEATURES]


def distance(a: dict, b: dict, sc) -> float:
    za, zb = _z(a, sc), _z(b, sc)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(za, zb)) / len(FEATURES))


def nearest(query: dict, lib: list[Setup], k: int = 10, exclude_t: float | None = None, side: str | None = None):
    pool = [s for s in lib if s.t != exclude_t and (side is None or s.side == side)]
    sc = _scales([s.f for s in lib])
    ranked = sorted(pool, key=lambda s: distance(query, s.f, sc))
    return [(distance(query, s.f, sc), s) for s in ranked[:k]], sc


def winner_profile(lib: list[Setup]) -> dict:
    wins = [s.f for s in lib if s.win == 1]
    return {k: st.mean(w[k] for w in wins) for k in FEATURES} if wins else {}


def feature_stats(lib: list[Setup]) -> list[dict]:
    done = [s for s in lib if s.win is not None]
    w = [s.f for s in done if s.win]
    l = [s.f for s in done if not s.win]
    rows = []
    for k in FEATURES:
        a, b = [x[k] for x in w], [x[k] for x in l]
        if len(a) < 2 or len(b) < 2:
            continue
        sp = math.sqrt(((len(a) - 1) * st.variance(a) + (len(b) - 1) * st.variance(b)) / (len(a) + len(b) - 2)) or 1e-9
        d = (st.mean(a) - st.mean(b)) / sp
        # AUC: P(random winner > random loser)
        gt = sum((x > y) + 0.5 * (x == y) for x in a for y in b)
        rows.append({"feature": k, "win_mean": st.mean(a), "loss_mean": st.mean(b), "cohen_d": d,
                     "auc": gt / (len(a) * len(b))})
    rows.sort(key=lambda r: abs(r["cohen_d"]), reverse=True)
    return rows


def loo_knn(lib: list[Setup], k: int = 7) -> dict:
    """Leave-one-out: predict win from the k nearest other setups. Compare to base rate."""
    done = [s for s in lib if s.win is not None]
    sc = _scales([s.f for s in done])
    base = sum(s.win for s in done) / len(done)
    hits_top, hits_all = [], []
    for s in done:
        others = sorted((x for x in done if x is not s and x.side == s.side), key=lambda x: distance(s.f, x.f, sc))[:k]
        p = sum(x.win for x in others) / len(others)
        hits_all.append((p, s.win))
    # Of the setups the kNN liked most (top third by predicted p), how many won?
    hits_all.sort(key=lambda x: -x[0])
    third = max(1, len(hits_all) // 3)
    top = hits_all[:third]
    bot = hits_all[-third:]
    return {"n": len(done), "base_win": base, "top_third_win": sum(w for _, w in top) / len(top),
            "bottom_third_win": sum(w for _, w in bot) / len(bot), "k": k}


# ---------- live ----------

def fetch_hourly(coin: str, days: int = 210) -> list[dict]:
    now = int(time.time() * 1000)
    out, start = {}, now - days * DAY * 1000
    while start < now:
        body = json.dumps({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start,
                                                                "endTime": now}}).encode()
        req = urllib.request.Request("https://api.hyperliquid.xyz/info", body, {"Content-Type": "application/json"})
        rows = json.load(urllib.request.urlopen(req, timeout=20))
        if not rows:
            break
        for r in rows:
            out[r["t"]] = r
        nxt = rows[-1]["t"] + 1
        if nxt <= start or len(rows) < 10:
            break
        start = nxt
    from hl_bot.research.pdl_retest import norm
    bars = norm([out[k] for k in sorted(out)])
    closed = [b for b in bars if b["t"] + H1 <= time.time()]
    return closed


# ---------- output ----------

def _px(x: float) -> str:
    return f"{x:,.0f}" if x >= 1000 else f"{x:,.2f}"


def _row(s: Setup, dist: float | None = None) -> str:
    res = "open" if s.win is None else ("WIN" if s.win else "loss")
    r = f"{s.r:+.1f}" if s.r is not None else "-"
    mfe = f"{s.mfe_r:.1f}" if s.mfe_r is not None else "-"
    d = f"{dist:.2f} | " if dist is not None else ""
    f = s.f
    return (f"| {d}{s.coin} | {s.side} | {s.et} | {_px(s.level)} | {f['closes_beyond']} | {f['trend_with']:+d} | "
            f"{f['adx_4h']:.0f} | {f['depth_atr']:.2f} | {f['swept']} | {f['clv_with']:.2f} | {f['rel_vol']:.1f} | "
            f"{f['room_r']:.1f} | {f['atr_regime']:.2f} | {res} | {r} | {mfe} |")


HDR = ("| {d}coin | side | retest close | level | closes | 4h trend with | ADX4h | depth ATR | swept | CLV with | relVol | "
       "room R | ATR regime | reached +2R | R at stop/target | MFE R |")


def table(rows, with_dist=False) -> str:
    h = HDR.format(d="dist | " if with_dist else "")
    sep = "|" + "---|" * (h.count("|") - 1)
    body = [_row(s, d) if with_dist else _row(s) for d, s in rows] if with_dist else [_row(s) for s in rows]
    return "\n".join([h, sep, *body])


def write(name: str, md: str) -> None:
    out = Path("docs/pnl")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"setup_search_{name}.md").write_text(md)
    try:
        import markdown  # type: ignore
        html = markdown.markdown(md, extensions=["tables"])
    except Exception:
        html = "<pre>" + md.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    (out / f"setup_search_{name}.html").write_text(f"<html><body style='font-family:sans-serif'>{html}</body></html>")


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "near", "now", "stats"])
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--at", default="", help="UTC 'YYYY-MM-DD HH:MM' of the retest bar OPEN")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--live", action="store_true", help="fetch fresh HL candles for the library too")
    a = ap.parse_args(argv)
    hourly = {c: fetch_hourly(c) for c in COINS} if (a.live or a.mode == "now") else None
    lib = build_library(hourly=hourly)
    done = [s for s in lib if s.win is not None]
    md = [f"Library: {len(lib)} PDL/PDH retest setups ({len(done)} resolved, "
          f"{sum(s.win for s in done)} reached +2R). Stop = retest wick + {STOP_ATR} ATR.\n"]
    if a.mode == "stats":
        md.append("| feature | winners mean | losers mean | Cohen d | AUC |\n|---|---|---|---|---|")
        for r in feature_stats(lib):
            md.append(f"| {r['feature']} | {r['win_mean']:.2f} | {r['loss_mean']:.2f} | {r['cohen_d']:+.2f} | {r['auc']:.2f} |")
        lo = loo_knn(lib)
        md.append(f"\nLeave-one-out kNN (k={lo['k']}): base win {lo['base_win']:.0%}, top-third predicted "
                  f"{lo['top_third_win']:.0%}, bottom third {lo['bottom_third_win']:.0%} (n={lo['n']}).")
        md.append("\nAll setups:\n")
        md.append(table(lib))
    elif a.mode == "scan":
        prof = winner_profile(lib)
        sc = _scales([s.f for s in lib])
        ranked = sorted(lib, key=lambda s: distance(prof, s.f, sc))[: a.top]
        md.append("Ranked by similarity to the average winner:\n")
        md.append(table([(distance(prof, s.f, sc), s) for s in ranked], with_dist=True))
    elif a.mode == "near":
        t = datetime.strptime(a.at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
        h1 = hourly[a.coin] if hourly else {c: d["1h"] for c, d in load(Path("/workspace/pdl_cache")).items()}[a.coin]
        ctx = CoinCtx(a.coin, h1)
        i = ctx.idx[t]
        q = ctx.setup_at(i, require_retest=False)
        md.append(f"Query {a.coin} {q.side} {q.et} level {_px(q.level)}\n")
        md.append(table([(0.0, q)], with_dist=True))
        nn, _ = nearest(q.f, lib, a.k, exclude_t=q.t, side=q.side)
        md.append("\nNearest historical setups:\n")
        md.append(table(nn, with_dist=True))
    else:
        for coin in COINS:
            ctx = CoinCtx(coin, hourly[coin])
            i = len(ctx.h1) - 1
            live = ctx.setup_at(i, require_retest=True)
            q = live or ctx.setup_at(i, require_retest=False)
            if q is None:
                md.append(f"\n### {coin}: no prior-day level yet\n")
                continue
            status = "LIVE RETEST SETUP" if live else (
                f"no retest on the last bar; {q.f['closes_beyond']} 1h closes beyond {('PDL' if q.side == 'short' else 'PDH')}")
            md.append(f"\n### {coin} {q.side} vs {'PDL' if q.side == 'short' else 'PDH'} {_px(q.level)} "
                      f"(last close {_px(q.entry)}, {q.et}) - {status}\n")
            md.append(table([(0.0, q)], with_dist=True))
            nn, _ = nearest(q.f, lib, a.k, side=q.side)
            wins = sum(1 for _, s in nn if s.win)
            res = sum(1 for _, s in nn if s.win is not None)
            md.append(f"\n{a.k} nearest: {wins}/{res} resolved reached +2R.\n")
            md.append(table(nn, with_dist=True))
    text = "\n".join(md)
    write(a.mode, text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

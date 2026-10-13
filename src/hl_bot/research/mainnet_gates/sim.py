"""Model B @190ca7b (mainnet flip config) replayed on 1m candles with synthetic prints."""
import sys, csv, json, glob, math, time
sys.path.insert(0, "/workspace/mb_190ca7b/src")
from hl_bot.strategy.model_b import engine as E, risk as R
from hl_bot.strategy.model_b.engine import ModelBEngine
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.types import TradePrint
from hl_bot.strategy.model_b.pools import pools_from_bars

MAKER, TAKER, SLIP = R.MAKER_FEE_RATE, R.TAKER_FEE_RATE, 5e-4
TICK = {"BTC": 1.0, "ETH": 0.1, "SOL": 0.01}
K = 12  # synthetic prints per bar

def load_bn(coin):
    rows = []
    for f in sorted(glob.glob(f"/workspace/bn1m/{coin}USDT-1m-*.csv")):
        with open(f) as fh:
            for r in csv.reader(fh):
                if not r[0].isdigit(): continue
                rows.append(dict(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]),
                                 v=float(r[5]), n=int(r[8]), tb=float(r[9])))
    rows.sort(key=lambda b: b["t"]); out = []; last = -1
    for b in rows:
        if b["t"] != last: out.append(b); last = b["t"]
    return out

def load_hl(coin):
    out = []
    for line in open(f"/workspace/hl_tape/{coin}/candles_1m.jsonl"):
        r = json.loads(line)
        out.append(dict(t=int(r["t"]), o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
                        v=float(r["v"]), n=int(r["n"]), tb=None))
    d = {b["t"]: b for b in out}
    return [d[k] for k in sorted(d)]

def prints_for(coin, b, vscale):
    t0 = b["t"] / 1000.0
    up = b["c"] >= b["o"]
    path = [b["o"], b["l"], b["h"], b["c"]] if up else [b["o"], b["h"], b["l"], b["c"]]
    pts = []
    for j in range(K):
        x = j / (K - 1) * 3; k = min(int(x), 2); fr = x - k
        pts.append(path[k] + (path[k + 1] - path[k]) * fr)
    v = b["v"] * vscale
    tb = (b["tb"] * vscale) if b["tb"] is not None else None
    moves = [pts[0] - (pts[0])] + [pts[j] - pts[j - 1] for j in range(1, K)]
    sides = ["buy" if (m > 0 or (m == 0 and up)) else "sell" for m in moves]
    nb = sum(s == "buy" for s in sides) or 1; ns = K - nb or 1
    if tb is None:  # candle proxy: buy share from close location
        rng = b["h"] - b["l"]; share = 0.5 if rng <= 0 else 0.25 + 0.5 * (b["c"] - b["l"]) / rng
        tb = v * share
    out = []
    for j in range(K):
        sz = (tb / nb) if sides[j] == "buy" else ((v - tb) / ns)
        out.append(TradePrint(ts=t0 + j * 60.0 / K + 0.5, coin=coin, price=pts[j], size=max(sz, 1e-9), side=sides[j], seq=j))
    return out

def prefilter(bars):
    h=[b["h"] for b in bars]; l=[b["l"] for b in bars]; c=[b["c"] for b in bars]; n=len(bars); ok=[False]*n
    hs=[]; ls=[]
    for i in range(3, n):
        k=i-2
        if h[k]>h[k-1] and h[k]>h[k+1]: hs.append(h[k])
        if l[k]<l[k-1] and l[k]<l[k+1]: ls.append(l[k])
        hs=hs[-40:]; ls=ls[-40:]
        lo2=min(l[i],l[i-1]); hi2=max(h[i],h[i-1])
        if any(lo2 < s_ < c[i] for s_ in ls) or any(c[i] < s_ < hi2 for s_ in hs): ok[i]=True
    return ok

def run(coin, bars, start_ms, end_ms, vscale, nscale, eq0=5000.0, min_r=1.0, tp_r=1.5, equity_fn=None):
    if min_r != 1.0:
        E.min_tp_distance = lambda e, s, min_r_=min_r: R.min_tp_distance(e, s, min_r=min_r_)
    else:
        E.min_tp_distance = R.min_tp_distance
    eng = ModelBEngine(thesis=ThesisBook(work_sec=0), tp_r=tp_r, risk_pct=0.02, min_prints=1,
                       coins=(coin,), delta_flat_eps=0.05, delta_flat_usdc=100.0)
    ok = prefilter(bars); tick = TICK[coin]
    sigs = []; used = set(); busy_until = 0
    for i in range(300, len(bars)):
        b = bars[i]
        if not (start_ms <= b["t"] < end_ms) or not ok[i] or b["t"] < busy_until: continue
        if (b["n"] + bars[i - 1]["n"] / 2) * nscale < 30: continue  # THIN_TAPE, mainnet floor 30
        now = b["t"] / 1000.0 + 60
        win = bars[max(0, i - 4999): i + 1]
        pr = prints_for(coin, bars[i - 1], vscale) + prints_for(coin, b, vscale)
        last = b["c"]
        try:
            d = eng.evaluate(coin, now=now, prints=pr, bars=win, pools=pools_from_bars(win, now, last_price=last),
                             best_bid=last - tick, best_ask=last + tick, equity=eq0, tick=tick)
        except Exception as ex:
            continue
        if not d.armed or d.intent is None: continue
        it = d.intent
        if it.swing_id in used: continue
        used.add(it.swing_id)
        # rest the Alo; fill on touch; cancel if TP or stop trades first, or after 240 min
        sg = 1 if it.side == "long" else -1
        fill_j = None
        for j in range(i + 1, min(len(bars), i + 241)):
            x = bars[j]
            if (x["l"] <= it.limit_px) if sg == 1 else (x["h"] >= it.limit_px): fill_j = j; break
            if (x["h"] >= it.take_profit or x["l"] <= it.stop) if sg == 1 else (x["l"] <= it.take_profit or x["h"] >= it.stop): break
        if fill_j is None: continue
        ex = None
        for j in range(fill_j, len(bars)):
            x = bars[j]
            if (x["l"] <= it.stop) if sg == 1 else (x["h"] >= it.stop):
                ex = (it.stop * (1 - sg * SLIP), j, "stop"); break
            if j > fill_j or True:
                if (x["h"] >= it.take_profit) if sg == 1 else (x["l"] <= it.take_profit):
                    ex = (it.take_profit, j, "target"); break
        if ex is None: ex = (bars[-1]["c"], len(bars) - 1, "open_at_end")
        sigs.append(dict(coin=coin, side=it.side, t=bars[fill_j]["t"] / 1000, arm_t=now, entry=it.limit_px, stop=it.stop,
                         tp=it.take_profit, exit=ex[0], exit_t=bars[ex[1]]["t"] / 1000 + 60, why=ex[2],
                         stop_pct=abs(it.limit_px - it.stop) / it.limit_px * 100,
                         rr=abs(it.take_profit - it.limit_px) / abs(it.limit_px - it.stop)))
        busy_until = bars[ex[1]]["t"] + 60000  # one ticket per coin
    return sigs

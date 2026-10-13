"""Wide-stop PDL/PDH retest (a,c,e,f) on the top-15 HL perps by 24h volume. HL 1h (5000 bars) + 15m."""
import json, time
from pathlib import Path
from hl_bot.research import fixed_width as fw, wide_stops as ws
from hl_bot.research.disc_filters import candidates
from hl_bot.research.pdl_retest import load, DAY

TOP15 = "BTC ETH HYPE NEAR ZEC SOL PUMP XRP STRK ENA W ZRO ONDO PONS WLD".split()
SKIP = {"PONS": "listed on HL 2026-08-31, under 90 days of history"}
COINS = [c for c in TOP15 if c not in SKIP]
CELLS = [("sweep+0.25atr", "2R"), ("sweep+0.25atr", "3R"), ("sweep+0.1%", "2R"), ("sweep+0.1%", "3R")]

def run(cands, smode, tmode, per_coin):
    busy, eq, used, tr = {}, 5000.0, set(), []
    for c in cands:
        key = c["coin"] if per_coin else "_"
        if c["t"] < busy.get(key, 0) or (c["coin"], c["day"], c["side"]) in used: continue
        if not all(c[k] for k in ws.NEED): continue
        p, _ = ws.plan(c, smode, tmode)
        if p is None: continue
        used.add((c["coin"], c["day"], c["side"]))
        x = ws.sim(c["coin"], c["side"], c["t"], c["entry"], p[0], p[1], eq); tr.append(x); busy[key] = x["exit_t"]
        if not per_coin: eq += x["pnl"]
    return tr

def main():
    fw.data = load(Path("/workspace/pdl_cache15"), coins=tuple(COINS))
    end = min(d["1h"][-1]["t"] for d in fw.data.values()) + 3600
    fw.END, fw.START, fw.SPLIT = end, end - 90 * DAY, end - 30 * DAY
    cands = candidates(fw.data, fw.START, fw.END)
    res = {"coins": COINS, "skipped": SKIP, "window": [time.strftime("%F %H:%M", time.gmtime(x)) for x in (fw.START, fw.SPLIT, fw.END)]}
    print(res["window"])
    for s, t in CELLS:
        for pc in (False, True):
            tr = sorted(run(cands, s, t, pc), key=lambda x: x["exit_t"])
            name = f"{s} | {t} | {'per-coin' if pc else 'one-at-a-time'}"
            d = {"all": fw.stats(tr), "first60": fw.stats([x for x in tr if x["t"] < fw.SPLIT]),
                 "last30": fw.stats([x for x in tr if x["t"] >= fw.SPLIT]),
                 "coins": {c: fw.stats([x for x in tr if x["coin"] == c]) for c in COINS},
                 "long": fw.stats([x for x in tr if x["side"] == "long"]), "short": fw.stats([x for x in tr if x["side"] == "short"]),
                 "max_lev": max((x["lev"] for x in tr), default=0), "capped": sum(x["capped"] for x in tr)}
            res[name] = d
            g = lambda z, q: round(z.get(q, 0), 2)
            a, f, l = d["all"], d["first60"], d["last30"]
            print(f"{name:40} n={a.get('n',0)} wr={g(a,'wr')} avgR={g(a,'avg_r')} totR={g(a,'tot_r')} ret={g(a,'ret')} dd={g(a,'dd')} ls={a.get('streak',0)} f60 {f.get('n',0)}/{g(f,'tot_r')} l30 {l.get('n',0)}/{g(l,'tot_r')} lev={d['max_lev']} L {g(d['long'],'tot_r')} S {g(d['short'],'tot_r')}")
            print("   ", {c: (v.get("n", 0), g(v, "tot_r")) for c, v in d["coins"].items()})
    Path("docs/pnl/pdl_retest_wide_top15.json").write_text(json.dumps(res, indent=1, default=str))

if __name__ == "__main__":
    main()

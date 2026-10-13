"""PDL/PDH retest (filters a,c,e,f; no g) with wide stops and liquidity / fixed R targets, 2% risk, 72h."""
import json
from pathlib import Path
from hl_bot.research import fixed_width as fw
from hl_bot.research.disc_filters import candidates
from hl_bot.research.pdl_retest import LEVEL_PARAMS, H1
P = fw.P
from hl_bot.strategy.model_b.swing import aggregate, atr14_of, build_levels
from hl_bot.strategy.model_b.fees import conservative_fees

NEED = ["a_2closes", "c_macro", "e_flow", "f_room"]
STOPS = ["sweep+0.1%", "sweep+0.25atr", "level+1atr", "level+1.5atr", "4h_swing"]
TGTS = ["liq_1to5R", "2R", "3R", "5R"]

def swing4h(h1, now, side):
    h4 = aggregate(h1, H1, 4 * H1, now)[-30:]
    for k in range(len(h4) - 2, 0, -1):
        a, b, c = h4[k - 1], h4[k], h4[k + 1]
        if side == "short" and b["h"] > a["h"] and b["h"] > c["h"]: return b["h"]
        if side == "long" and b["l"] < a["l"] and b["l"] < c["l"]: return b["l"]
    return None

def sim(coin, side, t0, entry_raw, stop, tgt, eq):
    fees = conservative_fees(coin); sg = 1 if side == "long" else -1
    entry = entry_raw * (1 + sg * P.entry_slip_bps / 1e4)
    sf = stop * (1 - sg * P.stop_slip_bps / 1e4)
    per = abs(entry - sf) + entry * fees.taker + sf * fees.taker
    size = 0.02 * eq / per; lev = size * entry / eq; capped = lev > 20
    if capped: size = 20 * eq / entry
    bars, btf = fw.bars_for(coin, t0); ex = None
    for b in bars:
        if (b["l"] <= stop) if sg == 1 else (b["h"] >= stop): ex = (sf, b["t"] + btf, "stop"); break
        if (b["h"] >= tgt) if sg == 1 else (b["l"] <= tgt): ex = (tgt, b["t"] + btf, "target"); break
        if b["t"] + btf >= t0 + 72 * H1: ex = (b["c"], b["t"] + btf, "time"); break
    if ex is None: ex = (bars[-1]["c"], bars[-1]["t"] + btf, "open_at_end") if bars else (entry, t0, "open_at_end")
    px, xt, why = ex
    pnl = sg * (px - entry) * size - size * entry * fees.taker - size * px * (fees.maker if why == "target" else fees.taker)
    return dict(coin=coin, side=side, t=t0, exit_t=xt, why=why, lev=round(lev, 2), capped=capped, pnl=pnl,
                r=pnl / (0.02 * eq), stop_pct=abs(entry - stop) / entry * 100)

def plan(c, smode, tmode):
    h1 = fw.data[c["coin"]]["1h"]; now = c["t"]; sh = c["side"] == "short"; sg = -1 if sh else 1
    atr = atr14_of(aggregate(h1, H1, H1, now)) or 0.0
    e, lvl, sw = c["entry"], c["level"], c["sweep"]
    if smode == "sweep+0.1%": stop = sw * (1 - sg * 0.001)
    elif smode == "sweep+0.25atr": stop = sw - sg * 0.25 * atr
    elif smode == "level+1atr": stop = lvl - sg * atr
    elif smode == "level+1.5atr": stop = lvl - sg * 1.5 * atr
    else:
        s4 = swing4h(h1, now, c["side"]); stop = s4 if s4 else None
    if stop is None or (sh and stop <= e) or (not sh and stop >= e): return None, "bad_stop"
    risk = abs(e - stop)
    if tmode == "liq_1to5R":
        lv = build_levels(h1, now, LEVEL_PARAMS)
        cs = sorted([l.price for l in lv if l.kind == ("support" if sh else "resistance") and (l.price < e if sh else l.price > e)],
                    key=lambda p: abs(p - e))
        cs = [p for p in cs if 1.0 <= abs(p - e) / risk <= 5.0]
        if not cs: return None, "no_pool_1to5R"
        return (stop, cs[0]), None
    k = float(tmode[0]); return (stop, e + sg * k * risk), None

def run(cands, smode, tmode):
    busy, eq, used, tr, skips = 0, 5000.0, set(), [], {}
    for c in cands:
        if c["t"] < busy or (c["coin"], c["day"], c["side"]) in used: continue
        if not all(c[k] for k in NEED): continue
        p, why = plan(c, smode, tmode)
        if p is None: skips[why] = skips.get(why, 0) + 1; continue
        used.add((c["coin"], c["day"], c["side"]))
        x = sim(c["coin"], c["side"], c["t"], c["entry"], p[0], p[1], eq); tr.append(x); busy = x["exit_t"]; eq += x["pnl"]
    return tr, skips

def main():
    cands = candidates(fw.data, fw.START, fw.END)
    funnel = {"base": len(cands)}; k_ = cands
    for k in NEED: k_ = [c for c in k_ if c[k]]; funnel["+" + k] = len(k_)
    res = {"funnel": funnel}
    for s in STOPS:
        for t in TGTS:
            tr, sk = run(cands, s, t)
            d = {"all": fw.stats(tr), "first60": fw.stats([x for x in tr if x["t"] < fw.SPLIT]),
                 "last30": fw.stats([x for x in tr if x["t"] >= fw.SPLIT]), "skips": sk}
            for c in ("BTC", "ETH", "SOL"): d[c] = fw.stats([x for x in tr if x["coin"] == c])
            for sd in ("long", "short"): d[sd] = fw.stats([x for x in tr if x["side"] == sd])
            d["avg_stop_pct"] = sum(x["stop_pct"] for x in tr) / len(tr) if tr else 0
            d["max_lev"] = max((x["lev"] for x in tr), default=0); d["rows"] = tr
            res[f"{s} | {t}"] = d
    Path("docs/pnl/pdl_retest_wide_stops.json").write_text(json.dumps(res, indent=1, default=str))
    print(funnel)
    for k, d in res.items():
        if k == "funnel": continue
        a, f, l = d["all"], d["first60"], d["last30"]
        g = lambda z, q: round(z.get(q, 0), 2)
        print(f"{k:28} n={a.get('n',0)} wr={g(a,'wr')} avgR={g(a,'avg_r')} totR={g(a,'tot_r')} ret={g(a,'ret')} dd={g(a,'dd')} ls={a.get('streak',0)} "
              f"| f60 n={f.get('n',0)} R={g(f,'tot_r')} | l30 n={l.get('n',0)} R={g(l,'tot_r')} | stop%={d['avg_stop_pct']:.2f} lev={d['max_lev']} "
              f"| BTC {g(d['BTC'],'tot_r')} ETH {g(d['ETH'],'tot_r')} SOL {g(d['SOL'],'tot_r')} L {g(d['long'],'tot_r')}({d['long'].get('n',0)}) S {g(d['short'],'tot_r')}({d['short'].get('n',0)}) skips={d['skips']}")

if __name__ == "__main__":
    main()

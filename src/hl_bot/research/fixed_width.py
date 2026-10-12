import json, time, sys
from pathlib import Path
sys.path.insert(0, "src")
from hl_bot.research.pdl_retest import load, find_setup, RetestParams, tape_delta, H1, DAY
from hl_bot.research.setup_search import build_library
from hl_bot.strategy.model_b.fees import conservative_fees
SP, TP = 0.00421, 0.02549
P = RetestParams(retest_tf=H1)
data = load(Path("/workspace/pdl_cache"))
END = max(d["1h"][-1]["t"] + H1 for d in data.values())
START = END - 90 * DAY
SPLIT = END - 30 * DAY

def bars_for(coin, t0):
    f = [b for b in data[coin]["15m"] if b["t"] >= t0]
    if f and f[0]["t"] <= t0 + 900: return f, 900
    return [b for b in data[coin]["1h"] if b["t"] >= t0], H1

def sim(coin, side, t0, entry_raw, eq, max_h):
    fees = conservative_fees(coin); sg = 1 if side == "long" else -1
    entry = entry_raw * (1 + sg * P.entry_slip_bps / 1e4)
    stop = entry * (1 - sg * SP); tgt = entry * (1 + sg * TP)
    stop_fill = stop * (1 - sg * P.stop_slip_bps / 1e4)
    loss_per = abs(entry - stop_fill) + entry * fees.taker + stop_fill * fees.taker
    size = 0.02 * eq / loss_per; lev = size * entry / eq; capped = lev > 20
    if capped: size = 20 * eq / entry
    bars, btf = bars_for(coin, t0); ex = None
    for b in bars:
        if (b["h"] >= stop) if side == "short" else (b["l"] <= stop):
            ex = (stop_fill, b["t"] + btf, "stop"); break
        if (b["l"] <= tgt) if side == "short" else (b["h"] >= tgt):
            ex = (tgt, b["t"] + btf, "target"); break
        if max_h and b["t"] + btf >= t0 + max_h * H1:
            ex = (b["c"], b["t"] + btf, "time"); break
    if ex is None:
        ex = (bars[-1]["c"], bars[-1]["t"] + btf, "open_at_end") if bars else (entry, t0, "open_at_end")
    px, xt, why = ex
    pnl = sg * (px - entry) * size - size * entry * fees.taker - size * px * (fees.maker if why == "target" else fees.taker)
    return dict(coin=coin, side=side, t=t0, utc=time.strftime("%Y-%m-%d %H:%M", time.gmtime(t0)), entry=entry,
                exit=px, exit_t=xt, why=why, lev=round(lev, 2), capped=capped, pnl=pnl, r=pnl / (0.02 * eq), eq0=eq,
                hold_h=(xt - t0) / H1)

def stats(tr, eq0=5000.0):
    if not tr: return dict(n=0)
    eq = pk = eq0; dd = 0; ls = cur = 0
    for x in tr:
        eq *= (1 + 0.02 * x["r"]); pk = max(pk, eq); dd = max(dd, (pk - eq) / pk)
        cur = cur + 1 if x["r"] <= 0 else 0; ls = max(ls, cur)
    w = sum(x["r"] > 0 for x in tr)
    return dict(n=len(tr), wins=w, wr=w / len(tr), avg_r=sum(x["r"] for x in tr) / len(tr),
                tot_r=sum(x["r"] for x in tr), ret=(eq / eq0 - 1) * 100, dd=dd * 100, streak=ls,
                stops=sum(x["why"] == "stop" for x in tr), tps=sum(x["why"] == "target" for x in tr),
                time=sum(x["why"] == "time" for x in tr), open=sum(x["why"] == "open_at_end" for x in tr),
                capped=sum(x["capped"] for x in tr), max_lev=max(x["lev"] for x in tr))

def pdl(max_h):
    deltas = {c: tape_delta(Path("/workspace/hl_tape") / c, H1) for c in data}
    ev = sorted((b["t"] + H1, c, i) for c, d in data.items() for i, b in enumerate(d["1h"]) if START <= b["t"] < END)
    used, busy, eq, out = set(), 0, 5000.0, []
    for now, c, i in ev:
        if now < busy: continue
        s = find_setup(c, data[c]["1h"], data[c]["1h"], i, P, deltas[c], used)
        if not s: continue
        used.add((c, s["day"], s["side"]))
        if "skip" in s: continue
        x = sim(c, s["side"], now, s["entry"], eq, max_h); out.append(x); busy = x["exit_t"]; eq += x["pnl"]
    return out

def lib(max_h, seq):
    L = [s for s in build_library() if START <= s.t < END]
    out, busy, eq = [], 0, 5000.0
    for s in L:
        if seq and s.t < busy: continue
        x = sim(s.coin, s.side, s.t, s.entry, eq, max_h); out.append(x)
        if seq: busy = x["exit_t"]; eq += x["pnl"]
    return out, len(L)

def report(name, tr):
    r = {"all": stats(tr), "first60": stats([x for x in tr if x["t"] < SPLIT]), "last30": stats([x for x in tr if x["t"] >= SPLIT])}
    for k in ("BTC", "ETH", "SOL"): r[k] = stats([x for x in tr if x["coin"] == k])
    for k in ("long", "short"): r[k] = stats([x for x in tr if x["side"] == k])
    return r

res = {}
for mh, lab in ((0, "no_limit"), (72, "72h")):
    res[f"pdl_{lab}"] = report("pdl", t := pdl(mh)); res[f"pdl_{lab}"]["rows"] = t
    t, n = lib(mh, False); res[f"lib_all_{lab}"] = report("lib", t); res[f"lib_all_{lab}"]["n_lib"] = n
    t, _ = lib(mh, True); res[f"lib_seq_{lab}"] = report("lib", t)
res["window"] = [time.strftime("%Y-%m-%d %H:%M", time.gmtime(x)) for x in (START, SPLIT, END)]
Path("docs/pnl/pdl_retest_fixed_width.json").write_text(json.dumps(res, indent=1, default=str))
for k, v in res.items():
    if k == "window": print(v); continue
    print("==", k, v.get("n_lib", ""))
    for s in ("all", "first60", "last30", "BTC", "ETH", "SOL", "long", "short"):
        a = v[s]; print(f" {s:7}", {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in a.items()})

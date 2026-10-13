import json, glob, sys, time
MAKER, TAKER = 0.00015, 0.00045
def r_of(x):
    e, s = x["entry"], x["stop"]; sg = 1 if x["side"] == "long" else -1
    dist = abs(e - s); size = 1.0 / dist  # per $1 risk
    lev_cap = 20 / e  # max size per $ equity / risk 0.02 -> size per $risk = 20/(0.02*e)
    capped = size > 20 / (0.02 * e)
    if capped: size = 20 / (0.02 * e)
    pnl = sg * (x["exit"] - e) * size - size * e * MAKER - size * x["exit"] * (MAKER if x["why"] == "target" else TAKER)
    return pnl, capped, size * e * 0.02
def stats(tr):
    if not tr: return dict(n=0)
    tr = sorted(tr, key=lambda x: x["exit_t"]); eq = pk = 5000.0; dd = 0; cur = ls = 0; rs = []
    for x in tr:
        r = x["R"]; rs.append(r); eq *= 1 + 0.02 * r; pk = max(pk, eq); dd = max(dd, (pk - eq) / pk)
        cur = cur + 1 if r <= 0 else 0; ls = max(ls, cur)
    w = [x for x in tr if x["R"] > 0]; l = [x for x in tr if x["R"] <= 0]
    mv = lambda x: (x["exit"] / x["entry"] - 1) * 100 * (1 if x["side"] == "long" else -1)
    return dict(n=len(tr), wr=len(w) / len(tr), avg_win_pct=sum(map(mv, w)) / len(w) if w else 0,
                avg_loss_pct=sum(map(mv, l)) / len(l) if l else 0, avg_R=sum(rs) / len(rs), tot_R=sum(rs),
                ret=(eq / 5000 - 1) * 100, dd=dd * 100, streak=ls, capped=sum(x["capped"] for x in tr),
                med_stop_pct=sorted(x["stop_pct"] for x in tr)[len(tr) // 2], avg_rr=sum(x["rr"] for x in tr) / len(tr),
                open=sum(x["why"] == "open_at_end" for x in tr))
res = {}
for src in ("bn", "hl"):
    for var in ("flip", "r167"):
        tr = []
        for f in glob.glob(f"/workspace/mbsim/out_{src}_{var}_*.json"):
            for x in json.load(open(f)):
                x["R"], x["capped"], x["lev"] = r_of(x); tr.append(x)
        if not tr: continue
        end = max(x["t"] for x in tr); split = 1791763200 - 30 * 86400 if src == "bn" else None
        d = {"all": stats(tr)}
        for c in ("BTC", "ETH", "SOL"): d[c] = stats([x for x in tr if x["coin"] == c])
        for s in ("long", "short"): d[s] = stats([x for x in tr if x["side"] == s])
        if split:
            d["first60"] = stats([x for x in tr if x["t"] < split]); d["last30"] = stats([x for x in tr if x["t"] >= split])
        d["rows"] = sorted(tr, key=lambda x: x["t"]); res[f"{src}_{var}"] = d
json.dump(res, open("/workspace/pr25_wt/docs/pnl/model_b_mainnet_gates_90d.json", "w"), indent=1)
for k, v in res.items():
    print("==", k)
    for s, a in v.items():
        if s != "rows": print(f" {s:8}", {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in a.items()})

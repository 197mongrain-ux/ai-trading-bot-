"""Chris's discretionary PDL/PDH retest read as hard filters, fixed widths (0.421% / 2.549%), 2% risk."""
import json, sys, time
from pathlib import Path
from hl_bot.research import fixed_width as fw
from hl_bot.research.pdl_retest import tape_delta, LEVEL_PARAMS, H1, DAY
from hl_bot.strategy.model_b.swing import aggregate, build_levels, read_swing_macro

FILTERS = ["a_2closes", "c_macro", "d_lower_low", "e_flow", "f_room", "g_sweep_in_stop"]

def candidates(data, start, end, extra_hourly=None):
    out = []
    for coin, d in data.items():
        h1 = d["1h"]
        tape = tape_delta(Path("/workspace/hl_tape") / coin, H1)
        daily = {b["t"]: b for b in aggregate(h1, H1, DAY, h1[-1]["t"] + H1)}
        for i, bar in enumerate(h1):
            if not (start <= bar["t"] < end): continue
            day0 = int(bar["t"] // DAY) * DAY; prev = daily.get(day0 - DAY)
            if not prev: continue
            today = [b for b in h1[max(0, i - 24):i] if b["t"] >= day0]
            now = bar["t"] + H1
            for side, lvl in (("short", prev["l"]), ("long", prev["h"])):
                sh = side == "short"
                run = best = 0
                for b in today:
                    run = run + 1 if ((b["c"] < lvl) if sh else (b["c"] > lvl)) else 0; best = max(best, run)
                # base: breakdown closed beyond level (>=1 close), retest wick reaches level, close back beyond
                if best < 1: continue
                reach = bar["h"] >= lvl * (1 - 5e-4) if sh else bar["l"] <= lvl * (1 + 5e-4)
                back = bar["c"] < lvl if sh else bar["c"] > lvl
                if not (reach and back): continue
                entry = bar["c"]
                f = {}
                f["a_2closes"] = best >= 2
                macro = read_swing_macro(h1, now, LEVEL_PARAMS).macro
                f["c_macro"] = macro == ("down" if sh else "up")
                tlow = min([b["l"] for b in today] + [bar["l"]]); thigh = max([b["h"] for b in today] + [bar["h"]])
                f["d_lower_low"] = tlow < prev["l"] if sh else thigh > prev["h"]
                dlt = tape.get(bar["t"])
                if dlt is not None:
                    f["e_flow"] = dlt < 0 if sh else dlt > 0; src = "tape"
                else:
                    rng = bar["h"] - bar["l"]; clv = (bar["c"] - bar["l"]) / rng if rng else 0.5
                    f["e_flow"] = (clv < 0.5 and bar["c"] < bar["o"]) if sh else (clv > 0.5 and bar["c"] > bar["o"]); src = "candle"
                tp = entry * (1 - fw.TP) if sh else entry * (1 + fw.TP)
                lv = build_levels(h1, now, LEVEL_PARAMS)
                block = [l.price for l in lv if l.kind == ("support" if sh else "resistance")
                         and ((tp < l.price < entry * (1 - 0.001)) if sh else (entry * 1.001 < l.price < tp))]
                f["f_room"] = not block
                stop = entry * (1 + fw.SP) if sh else entry * (1 - fw.SP)
                f["g_sweep_in_stop"] = bar["h"] < stop if sh else bar["l"] > stop
                out.append(dict(coin=coin, side=side, t=now, day=day0, entry=entry, level=lvl, macro=macro,
                                flow_src=src, blockers=block, closes=best, sweep=bar["h"] if sh else bar["l"], **f))
    out.sort(key=lambda x: x["t"]); return out

def run(cands, need):
    busy, eq, used, tr = 0, 5000.0, set(), []
    for c in cands:
        if c["t"] < busy or (c["coin"], c["day"], c["side"]) in used: continue
        if not all(c[k] for k in need): continue
        used.add((c["coin"], c["day"], c["side"]))
        x = fw.sim(c["coin"], c["side"], c["t"], c["entry"], eq, 72); tr.append(x); busy = x["exit_t"]; eq += x["pnl"]
    return tr

def main():
    cands = candidates(fw.data, fw.START, fw.END)
    funnel = {"base (close beyond + rejected retest)": len(cands)}
    keep = cands
    for k in FILTERS:
        keep = [c for c in keep if c[k]]; funnel["+" + k] = len(keep)
    solo = {k: sum(c[k] for c in cands) for k in FILTERS}
    res = {"funnel": funnel, "solo_pass": solo, "flow_src": {s: sum(c["flow_src"] == s for c in cands) for s in ("tape", "candle")}}
    cfgs = {"ALL": FILTERS, "none (base only)": []}
    for k in FILTERS: cfgs["drop " + k] = [f for f in FILTERS if f != k]
    for n, need in cfgs.items():
        t = run(cands, need)
        res[n] = {"all": fw.stats(t), "first60": fw.stats([x for x in t if x["t"] < fw.SPLIT]),
                  "last30": fw.stats([x for x in t if x["t"] >= fw.SPLIT]), "rows": t}
    res["today"] = [c for c in cands if c["t"] >= fw.END - DAY]
    Path("docs/pnl/pdl_retest_discretionary_filters.json").write_text(json.dumps(res, indent=1, default=str))
    print(funnel, solo, res["flow_src"])
    for n in cfgs:
        for s in ("all", "first60", "last30"):
            a = res[n][s]; print(n, s, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in a.items() if k in ("n","wr","avg_r","tot_r","ret","dd","streak")})
    for c in res["today"]: print({k: c[k] for k in c if k != "blockers"}, c["blockers"][:5])

if __name__ == "__main__":
    main()

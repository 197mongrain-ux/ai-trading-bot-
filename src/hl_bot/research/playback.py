"""15m playback of Oct 12 2026 BTC, 12:30 PM - 9 PM ET, with real HL tape."""
import gzip, json, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from hl_bot.strategy.model_b.trend import adx_series
from hl_bot.strategy.model_b.swing import read_swing_macro, aggregate
from hl_bot.research.pdl_retest import LEVEL_PARAMS, norm
ET = ZoneInfo("America/Toronto"); M15 = 900
T0 = datetime(2026, 10, 12, 12, 30, tzinfo=ET).timestamp(); T1 = datetime(2026, 10, 12, 21, 0, tzinfo=ET).timestamp()
PDL = 82746.0

def hl(body):
    r = urllib.request.Request("https://api.hyperliquid.xyz/info", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r))

def adx_at(series, bars, tf, now):
    best = None
    for b, a in zip(bars, series):
        if b["t"] + tf <= now: best = a
    return best

def main():
    end = int(T1 * 1000) + 3600_000
    h1 = norm(hl({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": end - 120 * 86400_000, "endTime": end}}))
    m15 = norm(hl({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "15m", "startTime": end - 10 * 86400_000, "endTime": end}}))
    fund = hl({"type": "fundingHistory", "coin": "BTC", "startTime": int(T0 * 1000) - 3600_000, "endTime": end})
    day = datetime(2026, 10, 11, tzinfo=ZoneInfo("UTC")).timestamp()
    pdh = max(b["h"] for b in h1 if day <= b["t"] < day + 86400)
    buy, sell, cnt = {}, {}, {}
    for f in ("2026-10-12", "2026-10-13"):
        for line in gzip.open(f"/workspace/hl_tape/BTC/{f}.trades.jsonl.gz", "rt"):
            r = json.loads(line); t = r["time"] / 1000
            if not (T0 - 3600 <= t < T1): continue
            k = int(t // M15) * M15; sz = float(r["sz"]); dct = buy if r["side"] == "B" else sell
            dct[k] = dct.get(k, 0) + sz; cnt[k] = cnt.get(k, 0) + 1
    a1 = adx_series(h1); h4 = aggregate(h1, 3600, 14400, T1 + 3600); a4 = adx_series(h4)
    rows = []; cvd = 0.0; vols = [b["v"] for b in m15]
    for i, b in enumerate(m15):
        if not (T0 <= b["t"] < T1): continue
        now = b["t"] + M15; k = b["t"]
        bu, se = buy.get(k, 0), sell.get(k, 0); has = cnt.get(k, 0) > 0; dl = bu - se; cvd += dl
        avgv = sum(vols[i - 20:i]) / 20
        x1, x4 = adx_at(a1, h1, 3600, now), adx_at(a4, h4, 14400, now)
        macro = read_swing_macro(h1, now, LEVEL_PARAMS).macro
        piv_l, piv_h = [], []
        for j in range(i - 5, 5, -1):
            w = m15[j - 5: j + 6]
            if len(piv_l) < 3 and m15[j]["l"] == min(x["l"] for x in w): piv_l.append(m15[j]["l"])
            if len(piv_h) < 3 and m15[j]["h"] == max(x["h"] for x in w): piv_h.append(m15[j]["h"])
            if len(piv_l) == 3 and len(piv_h) == 3: break
        c = b["c"]
        ll = sorted({round(p * (1 - 1 / v)) for p in piv_l for v in (25, 50, 100) if abs(p * (1 - 1 / v) - c) / c < 0.04}, reverse=True)
        sl = sorted({round(p * (1 + 1 / v)) for p in piv_h for v in (25, 50, 100) if abs(p * (1 + 1 / v) - c) / c < 0.04})
        absorb = round(max(bu, se) / min(bu, se), 2) if has and min(bu, se) > 0 else None
        tag = "absorbed" if has and ((dl < 0 and c > b["o"]) or (dl > 0 and c < b["o"])) else ""
        fr = [f for f in fund if f["time"] / 1000 <= now]
        rows.append(dict(et=datetime.fromtimestamp(b["t"], ET).strftime("%H:%M"), t=b["t"], o=b["o"], h=b["h"], l=b["l"], c=c,
                         pdl_bps=round((c / PDL - 1) * 1e4), pdh_bps=round((c / pdh - 1) * 1e4),
                         adx1=round(x1[0], 1) if x1 else None, di1=("dn" if x1 and x1[2] > x1[1] else "up"),
                         adx4=round(x4[0], 1) if x4 else None, di4=("dn" if x4 and x4[2] > x4[1] else "up"), macro=macro,
                         delta=round(dl, 2) if has else None, absorb=absorb, absorb_tag=tag, cvd=round(cvd, 2),
                         prints=cnt.get(k, 0), vol_x=round(b["v"] / avgv, 2) if avgv else None,
                         funding=float(fr[-1]["fundingRate"]) if fr else None, longliq=ll[:3], shortliq=sl[:3]))
    return rows, pdh

if __name__ == "__main__":
    rows, pdh = main()
    json.dump({"pdh": pdh, "rows": rows}, open("docs/reference_trades/2026-10-12_btc_playback.json", "w"), indent=1)
    for r in rows:
        print(r["et"], r["o"], r["h"], r["l"], r["c"], r["pdl_bps"], r["adx1"], r["di1"], r["adx4"], r["di4"], r["macro"],
              r["delta"], r["absorb"], r["absorb_tag"], r["cvd"], r["vol_x"], r["prints"], r["funding"], r["longliq"], r["shortliq"])
    print("PDH", pdh)

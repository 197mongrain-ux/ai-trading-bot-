import sys, json; sys.path.insert(0, '/workspace/mbsim')
import sim
coin, var, src = sys.argv[1], sys.argv[2], sys.argv[3]
VS = {"BTC": (0.209, 0.1), "ETH": (0.155, 0.047), "SOL": (0.115, 0.044)}
if src == "bn":
    bars = sim.load_bn(coin); end = bars[-1]["t"] + 60000; start = end - 90 * 86400000; vs, ns = VS[coin]
else:  # HL real 1m candles, live window Oct 7 00:00 ET - Oct 9 24:00 ET; no taker split -> candle proxy
    bars = sim.load_hl(coin); start = 1791345600000; end = 1791604800000; vs, ns = 1.0, 1.0
kw = dict(min_r=1.0, tp_r=1.5) if var == "flip" else dict(min_r=1.67, tp_r=1.67)
s = sim.run(coin, bars, start, end, vs, ns, **kw)
json.dump(s, open(f"/workspace/mbsim/out_{src}_{var}_{coin}.json", "w"))
print(coin, var, src, len(s))

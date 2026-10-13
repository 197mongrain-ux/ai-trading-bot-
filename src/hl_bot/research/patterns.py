"""Pre-defined chart-pattern detectors for the PDL/PDH retest search. Research only.

Definitions were fixed before any outcome was looked at.

Pivots: fractal highs/lows with ``n`` bars each side on 1h. Only pivots confirmed
before the retest bar opens count (pivot index + n < retest index). Window: the
``window`` 1h bars before the retest bar.

Short side (long side mirrors with lows/highs swapped):
- Double top: the last two pivot highs P1, P2, >= 3 bars apart, |P1 - P2| <= tol x ATR,
  and the lowest low between them is >= 1 ATR below the lower peak. Neckline = that low.
- Head and shoulders: the last three pivot highs L, H, R with H >= max(L, R) + tol x ATR
  and |L - R| <= tol x ATR. Neckline = the line through the lowest lows between L-H and H-R,
  evaluated at the retest bar.
- at_level: the second top (DT) or the right shoulder (H&S) is within 1 ATR of the level.
- neck_broken: some close after the last peak and up to the retest bar is beyond the neckline.

Settings: A = (n=2, tol=0.3 ATR), B = (n=3, tol=0.5 ATR). A feeds the kNN features.
"""
from __future__ import annotations

SETTINGS = {"A": (2, 0.3), "B": (3, 0.5)}
WINDOW = 48


def pivots(bars: list[dict], lo: int, hi: int, n: int, kind: str) -> list[int]:
    """Indices j in [lo, hi) confirmed by bar hi-1 (j + n < hi)."""
    out = []
    key = "h" if kind == "high" else "l"
    for j in range(max(lo, n), hi - n):
        v = bars[j][key]
        left = bars[j - n: j]
        right = bars[j + 1: j + 1 + n]
        if kind == "high":
            ok = all(v > b["h"] for b in left) and all(v >= b["h"] for b in right)
        else:
            ok = all(v < b["l"] for b in left) and all(v <= b["l"] for b in right)
        if ok:
            out.append(j)
    return out


def detect(bars: list[dict], i: int, side: str, level: float, atr: float, n: int, tol: float,
           window: int = WINDOW) -> dict:
    """Pattern flags for a retest bar at index ``i``. ``side`` is the trade side."""
    res = {"dt": 0, "hs": 0, "dt_at": 0, "hs_at": 0, "dt_neck": 0, "hs_neck": 0}
    if atr <= 0 or i < n + 3:
        return res
    lo = max(0, i - window)
    short = side == "short"
    # Mirror longs by flipping prices so one code path handles both.
    def hi_(b):
        return b["h"] if short else -b["l"]

    def lo_(b):
        return b["l"] if short else -b["h"]

    def cl_(b):
        return b["c"] if short else -b["c"]

    lvl = level if short else -level
    view = [{"h": hi_(b), "l": lo_(b), "c": cl_(b)} for b in bars[lo:i]]
    piv = [j for j in pivots(view, 0, len(view), n, "high")]
    tol_px = tol * atr
    if len(piv) >= 2:
        a, b = piv[-2], piv[-1]
        pa, pb = view[a]["h"], view[b]["h"]
        if b - a >= 3 and abs(pa - pb) <= tol_px:
            neck = min(x["l"] for x in view[a + 1: b])
            if min(pa, pb) - neck >= atr:
                res["dt"] = 1
                res["dt_at"] = int(abs(pb - lvl) <= atr)
                res["dt_neck"] = int(any(x["c"] < neck for x in view[b + 1:]))
    if len(piv) >= 3:
        l_, h_, r_ = piv[-3], piv[-2], piv[-1]
        pl, ph, pr = view[l_]["h"], view[h_]["h"], view[r_]["h"]
        if ph >= max(pl, pr) + tol_px and abs(pl - pr) <= tol_px:
            t1 = min(range(l_ + 1, h_), key=lambda k: view[k]["l"])
            t2 = min(range(h_ + 1, r_), key=lambda k: view[k]["l"])
            slope = (view[t2]["l"] - view[t1]["l"]) / (t2 - t1)
            res["hs"] = 1
            res["hs_at"] = int(abs(pr - lvl) <= atr)
            res["hs_neck"] = int(any(view[k]["c"] < view[t1]["l"] + slope * (k - t1)
                                     for k in range(r_ + 1, len(view))))
    return res

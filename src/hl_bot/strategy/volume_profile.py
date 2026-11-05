"""Session volume profile (POC, value area, low-volume nodes).

This module does not place or block orders. ``vp_as_filter``, ``vp_entries``,
and ``vp_enabled`` stay off: Model B may log the profile and must not read
it as a gate. A touch is 12 bps (``VP_TOUCH_BPS``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# Trading switches. Model B does not consult these. They stay false so a
# profile level cannot become an entry, a filter, or a stop.
VP_AS_FILTER = False
VP_ENTRIES = False
VP_ENABLED = False

VP_TOUCH_BPS = 12.0
VALUE_AREA_FRACTION = 0.70
# Fewer closed-or-started session bars than this is "no profile", not an error.
MIN_SESSION_BARS = 30
# A traded bin this small versus the POC, and thinner than both neighboring
# traded bins, is a low-volume node.
LVN_MAX_FRAC_OF_POC = 0.30
_BINS = 32
_MS_CUTOFF = 1e11
_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class VolumeProfile:
    poc: float | None
    vah: float | None
    val: float | None
    lvns: tuple[float, ...]
    total_volume: float
    bar_count: int
    ok: bool
    error: str | None = None


def _bar_open_sec(t: float) -> float:
    t = float(t)
    if t >= _MS_CUTOFF:
        return t / 1000.0
    return t


def vp_touch_level(price: float, level: float, bps: float = VP_TOUCH_BPS) -> bool:
    """True when ``price`` is within ``bps`` of ``level`` (basis points of price).

    Exactly ``bps`` counts as a touch. A non-positive price or level does not.
    """
    if price <= 0 or level <= 0 or bps < 0:
        return False
    return abs(price - level) / price * 10_000.0 <= bps + 1e-9


def distance_bps(origin: float | None, level: float | None) -> float | None:
    """Signed bps from ``origin`` to ``level``. Positive means the level is above."""
    if origin is None or level is None:
        return None
    if origin <= 0 or level <= 0:
        return None
    return (level - origin) / origin * 10_000.0


def compute_profile(bars: list[dict] | None, *, now: float) -> VolumeProfile:
    """Today's NY session profile, or ``ok=False`` when there is not enough data.

    Bar volume is spread evenly from low to high. POC is the fullest bin.
    The value area is 70% of volume expanded out from the POC (equal next
    bins expand both ways). An LVN is a traded bin at or under 30% of POC
    volume and thinner than the traded bins on either side of it.
    Any internal failure returns ``ok=False`` with ``error`` set. Callers
    must not raise that into an order decision.
    """
    try:
        return _compute_profile(list(bars or []), now=float(now))
    except Exception as exc:
        return VolumeProfile(None, None, None, (), 0.0, 0, False, error=type(exc).__name__)


def _compute_profile(bars: list[dict], *, now: float) -> VolumeProfile:
    today = datetime.fromtimestamp(now, tz=_NY).date()
    session: list[tuple[float, float, float]] = []
    for bar in bars:
        if "t" not in bar:
            continue
        open_sec = _bar_open_sec(float(bar["t"]))
        if open_sec > now + 1e-9:
            continue
        if datetime.fromtimestamp(open_sec, tz=_NY).date() != today:
            continue
        low = float(bar.get("l") or 0)
        high = float(bar.get("h") or 0)
        vol = float(bar.get("v") or 0)
        if low <= 0 or high <= 0 or high < low or vol <= 0:
            continue
        session.append((low, high, vol))
    if len(session) < MIN_SESSION_BARS:
        return VolumeProfile(None, None, None, (), 0.0, len(session), False, error=None)

    lo = min(item[0] for item in session)
    hi = max(item[1] for item in session)
    span = hi - lo
    n = _BINS
    if span <= 0:
        width = max(lo * 1e-6, 1e-9)
        origin = lo - width / 2.0
    else:
        width = span / n
        origin = lo
    hist = [0.0] * n
    for low, high, vol in session:
        if high == low or span <= 0:
            hist[_bin_index(low, origin, width, n)] += vol
            continue
        for i in range(n):
            b0 = origin + i * width
            b1 = b0 + width
            overlap = min(high, b1) - max(low, b0)
            if overlap > 0:
                hist[i] += vol * (overlap / (high - low))

    total = sum(hist)
    if total <= 0:
        return VolumeProfile(None, None, None, (), 0.0, len(session), False, error=None)

    poc_i = max(range(n), key=lambda i: (hist[i], -i))
    poc = origin + (poc_i + 0.5) * width
    area_lo, area_hi = _value_area(hist, poc_i, total)
    val = origin + area_lo * width
    vah = origin + (area_hi + 1) * width
    lvns = _lvns(hist, poc_i, origin, width)
    return VolumeProfile(
        poc=poc,
        vah=vah,
        val=val,
        lvns=lvns,
        total_volume=total,
        bar_count=len(session),
        ok=True,
        error=None,
    )


def _bin_index(price: float, origin: float, width: float, n: int) -> int:
    if width <= 0:
        return 0
    idx = int(math.floor((price - origin) / width + 1e-9))
    return min(n - 1, max(0, idx))


def _value_area(hist: list[float], poc_i: int, total: float) -> tuple[int, int]:
    target = VALUE_AREA_FRACTION * total
    lo = hi = poc_i
    acc = hist[poc_i]
    n = len(hist)
    while acc + 1e-9 < target and (lo > 0 or hi < n - 1):
        can_left = lo > 0
        can_right = hi < n - 1
        left = hist[lo - 1] if can_left else -1.0
        right = hist[hi + 1] if can_right else -1.0
        if can_left and can_right and left == right:
            lo -= 1
            hi += 1
            acc += hist[lo] + hist[hi]
        elif right > left and can_right:
            hi += 1
            acc += hist[hi]
        elif can_left:
            lo -= 1
            acc += hist[lo]
        else:
            hi += 1
            acc += hist[hi]
    return lo, hi


def _lvns(hist: list[float], poc_i: int, origin: float, width: float) -> tuple[float, ...]:
    traded = [i for i, vol in enumerate(hist) if vol > 0]
    if len(traded) < 3:
        return ()
    threshold = LVN_MAX_FRAC_OF_POC * hist[poc_i]
    found: list[float] = []
    for k in range(1, len(traded) - 1):
        i = traded[k]
        if i == poc_i:
            continue
        vol = hist[i]
        if vol < hist[traded[k - 1]] and vol < hist[traded[k + 1]] and vol <= threshold:
            found.append(origin + (i + 0.5) * width)
    return tuple(found)


def nearest_lvn_on_side(
    lvns: tuple[float, ...] | list[float],
    poc: float | None,
    side: str | None,
    ref: float | None,
) -> float | None:
    """LVN on the trade's side of the POC, nearest the reference price.

    Long keeps nodes at or below the POC. Short keeps nodes at or above it.
    Without a reference price, the node closest to the POC is used.
    """
    if poc is None or poc <= 0 or side not in ("long", "short"):
        return None
    if side == "long":
        cands = [px for px in lvns if px <= poc + 1e-9]
    else:
        cands = [px for px in lvns if px >= poc - 1e-9]
    if not cands:
        return None
    if ref is None or ref <= 0:
        return min(cands, key=lambda px: (abs(px - poc), px))
    return min(cands, key=lambda px: (abs(px - ref), abs(px - poc), px))

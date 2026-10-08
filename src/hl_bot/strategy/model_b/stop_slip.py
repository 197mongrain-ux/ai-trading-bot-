"""Expected stop-market slippage, used only to shrink size.

Oct 8 2026, xyz:SKHX short: the stop trigger was 1198.8 and the stop market
filled at 1200.6. That is 1.8 points of slip on a 2.1-point planned risk
(~15 bps of the 1196.7 entry). Price loss plus fees was about 2.8% of a
~$251 account. The 2% cap had been computed at the trigger.

``MODEL_B_STOP_SLIP`` (default ``shadow``) sizes so the loss at the
trigger, plus this allowance, plus the maker entry and taker exit, stays
inside 2%. The stop price is not moved. ``shadow`` logs the size that
would have been sent and posts today's size. ``1`` posts the smaller
size. ``0`` does not look.

The allowance is the larger of a per-coin bps (configurable, with a
conservative xyz floor above that SKHX print) and the current bid/ask
spread. A one-tick book does not undercut the floor. A wide book raises
it. The 20x notional cap is unchanged.
"""

from __future__ import annotations

from hl_bot.strategy.model_b.universe import canon_coin

# Above the SKHX print (1.8 / 1196.7 = 15.0 bps). Main books are tighter;
# 5 bps still covers a several-tick stop-market sweep on BTC/ETH.
XYZ_STOP_SLIP_BPS = 20.0
MAIN_STOP_SLIP_BPS = 5.0


def _dex(coin: str) -> str:
    name = canon_coin(coin)
    if ":" not in name:
        return ""
    return name.split(":", 1)[0]


def default_stop_slip_bps(coin: str) -> tuple[float, str]:
    """Conservative floor when no per-coin override is set."""
    if _dex(coin) == "xyz":
        return XYZ_STOP_SLIP_BPS, "xyz-default"
    return MAIN_STOP_SLIP_BPS, "main-default"


def _parse_spec(coin: str, spec: str) -> tuple[float, str] | None:
    text = spec.strip()
    if not text:
        return None
    if "," not in text and "=" not in text:
        return float(text), "config"
    name = canon_coin(coin)
    dex = _dex(name)
    exact: float | None = None
    dex_hit: float | None = None
    default: float | None = None
    for part in text.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip()
        bps = float(raw.strip())
        if bps < 0:
            raise ValueError(f"stop slip bps must be >= 0 ({part})")
        token = key.lower()
        if token in ("*", "default"):
            default = bps
            continue
        if canon_coin(key) == name:
            exact = bps
            continue
        if token in (dex.lower(), f"{dex.lower()}:*", f"{dex.lower()}:"):
            dex_hit = bps
    if exact is not None:
        return exact, "coin"
    if dex_hit is not None:
        return dex_hit, "dex"
    if default is not None:
        return default, "default"
    return None


def resolve_stop_slip_bps(
    coin: str,
    spec: str | None = None,
    *,
    best_bid: float | None = None,
    best_ask: float | None = None,
    entry: float | None = None,
) -> tuple[float, str]:
    """Bps of entry to add to the sizing distance, and where it came from.

    Configured bps (spec, else the xyz/main floor) and the live spread
    are combined with ``max``. The stop price is not an output.
    """
    try:
        parsed = _parse_spec(coin, spec or "")
    except ValueError:
        parsed = None
    if parsed is None:
        bps, source = default_stop_slip_bps(coin)
    else:
        bps, source = parsed
    if (
        entry is not None
        and entry > 0
        and best_bid is not None
        and best_ask is not None
        and best_ask > best_bid > 0
    ):
        spread = (float(best_ask) - float(best_bid)) / float(entry) * 10_000.0
        if spread > bps:
            return spread, "book"
    return float(bps), source

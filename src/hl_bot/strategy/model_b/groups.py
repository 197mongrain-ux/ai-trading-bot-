"""Log-only total-risk cap and one-name-per correlated group.

Chris wants a gate-cleared setup to use whatever margin is left, so
``MODEL_B_RISK_CAP`` defaults to ``shadow``: the hunt logs
``would_block`` and still posts. ``1`` refuses the new ticket. ``0``
does not look. Nothing here cancels a resting order or a position.

Groups (the 18-coin book, October 2026):

- crypto: BTC, ETH, SOL, XRP, HYPE, ZEC
- us_tech: xyz:NVDA, xyz:MU, xyz:SNDK, xyz:SKHX, xyz:DRAM, xyz:SPCX,
  xyz:SP500, xyz:XYZ100
- oil: xyz:CL, xyz:BRENTOIL
- metals: xyz:GOLD, xyz:SILVER

A coin in none of those has no group. Open risk is the fee-inclusive
loss at the planned stop for every open position and every resting
ticket, plus the candidate. The cap is a percent of the sizing balance
(``MODEL_B_MAX_TOTAL_RISK_PCT``, default 6).
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.strategy.model_b.universe import canon_coin

CORRELATION_GROUPS: dict[str, frozenset[str]] = {
    "crypto": frozenset({"BTC", "ETH", "SOL", "XRP", "HYPE", "ZEC"}),
    "us_tech": frozenset(
        {
            "xyz:NVDA",
            "xyz:MU",
            "xyz:SNDK",
            "xyz:SKHX",
            "xyz:DRAM",
            "xyz:SPCX",
            "xyz:SP500",
            "xyz:XYZ100",
        }
    ),
    "oil": frozenset({"xyz:CL", "xyz:BRENTOIL"}),
    "metals": frozenset({"xyz:GOLD", "xyz:SILVER"}),
}


def correlation_group(coin: str) -> str | None:
    name = canon_coin(coin)
    for group, members in CORRELATION_GROUPS.items():
        if name in members:
            return group
    return None


@dataclass(frozen=True)
class RiskCap:
    """``block`` is true only in ``on`` mode. ``reason`` empty means clear."""

    block: bool
    reason: str
    total_pct: float
    group: str | None


def assess_risk_cap(
    *,
    coin: str,
    new_risk: float,
    equity: float,
    open_rows: list[tuple[str, float]],
    max_pct: float,
    mode: str,
) -> RiskCap:
    """Whether this ticket would break the total cap or its group.

    ``open_rows`` is ``(coin, dollar risk)`` for positions and resting
    tickets already on the book. The same coin is not counted twice.
    """
    if mode == "off" or float(max_pct) <= 0 or float(equity) <= 0:
        return RiskCap(False, "", 0.0, correlation_group(coin))
    name = canon_coin(coin)
    group = correlation_group(name)
    held = None
    if group is not None:
        for other, _risk in open_rows:
            if canon_coin(other) == name:
                continue
            if correlation_group(other) == group:
                held = canon_coin(other)
                break
    total = float(new_risk)
    for other, risk in open_rows:
        if canon_coin(other) == name:
            continue
        total += max(0.0, float(risk))
    total_pct = total / float(equity) * 100.0
    reasons: list[str] = []
    if held is not None:
        reasons.append(f"group={group} held={held}")
    if total_pct > float(max_pct) + 1e-9:
        reasons.append(f"total={total_pct:.2f}%>{float(max_pct):g}%")
    if not reasons:
        return RiskCap(False, "", total_pct, group)
    return RiskCap(mode == "on", ";".join(reasons), total_pct, group)

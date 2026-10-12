# Reference trade: BTC PDL breakdown-retest short (Chris, manual) — 2026-10-12

Status: OPEN (outcome TBD)

## Context
- 4h/1h trend: down (ADX>20, 4h-led).
- PDL (HL Oct 11 UTC low): 82,746. 1h closes below PDL: 10:00 (82,555), 11:00 (82,686) ET -> breakdown confirmed 12:00 ET (2-3 closes below on the day).
- Noon bar retested PDL from below (high 82,924, close 82,466), real tape delta negative.

## Trade
- Short 0.0404 BTC @ 82,836 (retest just over PDL), 22x cross, $177 account.
- Stop 83,176 (above retest high; 340 pts, ~$13.70, ~7.7% risk — manual high-conviction, above bot 2% cap).
- TP 80,716 (~6.2R), just above 4h support zone 80,150–80,350.
- 4h supports below: 82,228 / 80,150–80,350 / 79,200–79,400 / 78,600 / 77,700–77,950.

## Follow-through
- 14:45 ET low 82,262 held near 82,228 minor support.
- 15:20–15:30 ET rally 82,315 -> 82,778 (above PDL, top volume) then back under (~82,713): likely PDL buy-stop sweep; confirmation = 16:00 ET 1h close < 82,746.

## Lessons for bot
- Same idea as PR #26 pdl_retest, but rule version had no edge (90d + held-out); discretionary context (trend, sweep of PDL, room to 4h support) mattered.
- Outcome: TBD

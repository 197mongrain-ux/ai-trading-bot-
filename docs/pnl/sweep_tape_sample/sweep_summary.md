# Sweep entry on the recorded tape

Pipeline check on the tape that was passed in. A couple of hours of prints cannot say whether the sweep has an edge. The config is locked to the least-bad study frame: 15m confirm, macro 1h and 4h with the 4h leading, flow off. Monday paper stays on 15m confirm with 4h and daily, both required. Nothing here is deployed.

Window `2026-10-09` → `2026-10-09`. Equity 5000, risk 1.00%. R is net dollars divided by the risk budget reserved at the arm. A `tape_end` row is a mark. Closed expectancy leaves it out. The with-marks total includes it. Funding is not charged. The stop loss stays inside the 2% cap.

## Tape read

| coin | prints | 15m bars | 1h bars | span |
| --- | ---: | ---: | ---: | --- |
| BTC | 12673 | 11 | 3 | 2026-10-09 20:50:56 → 2026-10-09 23:28:58 (2.63h) |
| xyz:SP500 | 2582 | 11 | 3 | 2026-10-09 20:49:41 → 2026-10-09 23:28:34 (2.65h) |

## Scan

Closed 15m bars walked: 20. Signals 0, fills 0.

Every walked bar returned `MACRO_UNKNOWN` (20). 4h ADX(14) needs about 29 closed 4h bars, which this window does not have. No sweep armed. That is the pipeline result for a short tape.

## Result

| config | closed | marks | E closed | E marks | E with marks | avg budget $ | net | max DD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| sweep-15m-1h4h-4h_lead | 0 | 0 | — | — | — | — | 0.00 | 0.00% |

The same cell would need both calendar halves, at least 4 of 6 coins, and at least 20 trades before it could move the Monday paper book. This file does not meet that bar.

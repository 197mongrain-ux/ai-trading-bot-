# Trapped sellers / trapped buyers

Pipeline check on the tape that was passed in. A couple of hours of prints cannot say whether this entry has an edge. The grid was fixed before the run (bar 1m/3m/5m, imbalance 2.5/3/4, stacked 2/3/4, zone 10/20/40 bps). The default cell is 1m, 3:1, stacked 3, zone 20 bps. `MODEL_B_SWING_ENTRY` stays `sweep` unless it is set to `trapped`. Nothing here is deployed.

Window `2026-10-09` → `2026-10-09`. Equity 5000, risk 1.00%. Fees and the 25/30 bp slip are on. Funding is not in these files, so it is not charged. A `tape_end` row is a mark of a position still open when the file ends, not a finished trade.

## Tape read

| coin | prints | 1h bars | span |
| --- | ---: | ---: | --- |
| BTC | 12673 | 3 | 2026-10-09 20:50:56 → 2026-10-09 23:28:58 (2.63h) |
| xyz:SP500 | 2582 | 3 | 2026-10-09 20:49:41 → 2026-10-09 23:28:34 (2.65h) |

Footprint bars and how many of them had a session profile (VAL/VAH/POC) from prints before the bar:

| bar | BTC | xyz:SP500 |
| --- | ---: | ---: |
| 1m | 159 bars, 157 profiles | 160 bars, 157 profiles |
| 3m | 54 bars, 52 profiles | 54 bars, 52 profiles |
| 5m | 32 bars, 31 profiles | 33 bars, 31 profiles |

## Grid

The same fill can show up in more than one cell. That is one event counted again, not a new trade.

| config | signals | fills | trades | tape_end | E (all exits) | E (closed) | net | max DD | under 20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1m-imb2.5-stack2-z10 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb2.5-stack2-z20 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb2.5-stack2-z40 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb2.5-stack3-z10 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack3-z20 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack3-z40 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack4-z10 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack4-z20 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack4-z40 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack2-z10 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb3-stack2-z20 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb3-stack2-z40 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb3-stack3-z10 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack3-z20 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack3-z40 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack4-z10 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack4-z20 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack4-z40 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack2-z10 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb4-stack2-z20 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb4-stack2-z40 | 2 | 2 | 0 | 2 | -5.269 | — | -81.73 | 1.63% | yes |
| 1m-imb4-stack3-z10 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack3-z20 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack3-z40 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack4-z10 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack4-z20 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack4-z40 | 0 | 0 | 0 | 0 | — | — | 0.00 | 0.00% | yes |
| 3m-imb2.5-stack2-z10 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack2-z20 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack2-z40 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack3-z10 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack3-z20 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack3-z40 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack4-z10 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb2.5-stack4-z20 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb2.5-stack4-z40 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb3-stack2-z10 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb3-stack2-z20 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb3-stack2-z40 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb3-stack3-z10 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb3-stack3-z20 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb3-stack3-z40 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb3-stack4-z10 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb3-stack4-z20 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb3-stack4-z40 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb4-stack2-z10 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb4-stack2-z20 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb4-stack2-z40 | 3 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb4-stack3-z10 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb4-stack3-z20 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb4-stack3-z40 | 2 | 2 | 1 | 1 | -3.761 | 0.581 | -41.69 | 0.94% | yes |
| 3m-imb4-stack4-z10 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb4-stack4-z20 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 3m-imb4-stack4-z40 | 1 | 1 | 1 | 0 | 0.581 | 0.581 | 5.31 | 0.00% | yes |
| 5m-imb2.5-stack2-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack2-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack2-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack3-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack3-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack3-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack4-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack4-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack4-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack2-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack2-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack2-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack3-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack3-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack3-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack4-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack4-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb3-stack4-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack2-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack2-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack2-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack3-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack3-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack3-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack4-z10 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack4-z20 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |
| 5m-imb4-stack4-z40 | 2 | 2 | 1 | 1 | -3.507 | 0.581 | -41.45 | 0.93% | yes |

## Default cell

`1m-imb3-stack3-z20`. Pairs that did not become a signal: NO_DELTA 2, NO_FAILURE 15, NO_TARGET 1, NO_TRAP 291, NO_ZONE 5, TP_UNDER_MIN 3.
Cancels: target before fill 0, stop before fill 0, expired 0, busy 0.
No fill on this cell.

## Read this as a pipeline check

The same cell would have to be judged on both the 15m and the 1h books, in both halves, on at least 4 of 6 coins, with at least 20 trades, before the paper flag would turn on. This file does not meet that bar. Order-flow thresholds chosen on a few hours of one session would be fit to that session. Monday paper stays on the sweep entry.

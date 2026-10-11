# Trapped sellers / trapped buyers

Pipeline check on the tape that was passed in. A couple of hours of prints cannot say whether this entry has an edge. The entry grid was fixed before the run (bar 1m/3m/5m, imbalance 2.5/3/4, stacked 2/3/4, zone 10/20/40 bps). The default cell is 1m, 3:1, stacked 3, zone 20 bps. The stop and slip grid below is also fixed, and it runs only on that default cell. `MODEL_B_SWING_ENTRY` stays `sweep` unless it is set to `trapped`. Nothing here is deployed.

Window `2026-10-09` → `2026-10-09`. Equity 5000, risk 1.00%. R is net dollars divided by the risk budget reserved at the arm (stop distance, the slip that sizing used, and fees). It is not the raw stop distance. A `tape_end` row is a mark of a position still open when the file ends. Closed expectancy leaves those marks out. The with-marks total includes them. Funding is not in these files, so it is not charged. A stop's dollar loss stays inside the 2% cap.

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

The same fill can show up in more than one cell. That is one event counted again, not a new trade. The entry grid uses the wick stop and the coin slip (25 bps main, 30 bps xyz).

| config | signals | fills | closed | marks | E closed | E marks | E with marks | avg budget $ | net | max DD | under 20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1m-imb2.5-stack2-z10 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb2.5-stack2-z20 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb2.5-stack2-z40 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb2.5-stack3-z10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack3-z20 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack3-z40 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack4-z10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack4-z20 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb2.5-stack4-z40 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack2-z10 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb3-stack2-z20 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb3-stack2-z40 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb3-stack3-z10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack3-z20 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack3-z40 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack4-z10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack4-z20 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb3-stack4-z40 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack2-z10 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb4-stack2-z20 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb4-stack2-z40 | 2 | 2 | 0 | 2 | — | -0.817 | -0.817 | 50.00 | -81.72 | 1.63% | yes |
| 1m-imb4-stack3-z10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack3-z20 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack3-z40 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack4-z10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack4-z20 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 1m-imb4-stack4-z40 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| 3m-imb2.5-stack2-z10 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack2-z20 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack2-z40 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack3-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack3-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack3-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb2.5-stack4-z10 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb2.5-stack4-z20 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb2.5-stack4-z40 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb3-stack2-z10 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb3-stack2-z20 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb3-stack2-z40 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb3-stack3-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb3-stack3-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb3-stack3-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb3-stack4-z10 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb3-stack4-z20 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb3-stack4-z40 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb4-stack2-z10 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb4-stack2-z20 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb4-stack2-z40 | 3 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb4-stack3-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb4-stack3-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb4-stack3-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.940 | -0.417 | 50.00 | -41.69 | 0.94% | yes |
| 3m-imb4-stack4-z10 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb4-stack4-z20 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 3m-imb4-stack4-z40 | 1 | 1 | 1 | 0 | 0.106 | — | 0.106 | 50.00 | 5.31 | 0.00% | yes |
| 5m-imb2.5-stack2-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack2-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack2-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack3-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack3-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack3-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack4-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack4-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb2.5-stack4-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack2-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack2-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack2-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack3-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack3-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack3-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack4-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack4-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb3-stack4-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack2-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack2-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack2-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack3-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack3-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack3-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack4-z10 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack4-z20 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |
| 5m-imb4-stack4-z40 | 2 | 2 | 1 | 1 | 0.106 | -0.935 | -0.414 | 50.00 | -41.45 | 0.93% | yes |

## Default cell

`1m-imb3-stack3-z20`. Pairs that did not become a signal: NO_DELTA 2, NO_FAILURE 15, NO_TARGET 1, NO_TRAP 291, NO_ZONE 5, TP_UNDER_MIN 3.
Cancels: target before fill 0, stop before fill 0, expired 0, busy 0.
No fill on this cell.

## Stop and slip

Fixed before the run, and only on the default entry cell (`1m-imb3-stack3-z20`). Anchor `trap` is beyond the trap-bar extreme. Anchor `zone` is beyond the support or resistance. The min distance is max(X bps, k×ATR(14) of the footprint bars up to the trap bar), with X 20/30/40 and k 0.5/1.0. Those twelve cells keep the coin slip. The slip rows keep the wick stop and no min floor: flat 10 bps, flat 15 bps, and proportional, which reserves min(the coin 25/30 allowance, the stop distance in bps). `ref` is the default cell above. Avg budget is the mean dollars reserved per fill. A wider stop or a smaller slip changes how much of that budget is the stop versus unused slip, and therefore the R of the same price path. This is not crossed with the 81 entry cells. Seeing the table does not add a grid point.

| config | signals | fills | closed | marks | E closed | E marks | E with marks | avg budget $ | net | max DD | under 20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ref-trap-slip25/30 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-trap-x20-k0.5 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-trap-x20-k1 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-trap-x30-k0.5 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-trap-x30-k1 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-trap-x40-k0.5 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-trap-x40-k1 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-zone-x20-k0.5 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-zone-x20-k1 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-zone-x30-k0.5 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-zone-x30-k1 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-zone-x40-k0.5 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| stop-zone-x40-k1 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| slip-flat10 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| slip-flat15 | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |
| slip-proportional | 0 | 0 | 0 | 0 | — | — | — | — | 0.00 | 0.00% | yes |

Wick-stop fills from the entry grid, one row per event. A cell that repeats the fill is not a new trade. Stop distance is a few bps and the reserved slip is 25 or 30, so the budget is the full risk percent and a target that is 1 price-R of that stop is a small fraction of it. A mark pays the taker fee plus that same slip, so an open trade near the entry is charged most of the budget even though it has not stopped.

| coin | side | reason | stop bps | slip bps | budget $ | net $ | R |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| xyz:SP500 | long | tape_end | 5.77 | 30.0 | 50.00 | -44.74 | -0.895 |
| BTC | short | tape_end | 9.60 | 25.0 | 50.00 | -36.98 | -0.740 |
| BTC | long | tp | 6.94 | 25.0 | 50.00 | 5.31 | 0.106 |
| xyz:SP500 | long | tape_end | 5.51 | 30.0 | 50.00 | -47.00 | -0.940 |
| xyz:SP500 | long | tape_end | 5.90 | 30.0 | 50.00 | -46.76 | -0.935 |

## Read this as a pipeline check

The same cell would have to be judged on both the 15m and the 1h books, in both halves, on at least 4 of 6 coins, with at least 20 trades, before the paper flag would turn on. This file does not meet that bar. A stop or slip chosen on a few hours of one session would be fit to that session. Monday paper stays on the sweep entry, with the wick stop and the 25/30 bp allowance.

# Winner profile and first-half filters

Filters were chosen from the first half of the 130-day 1h book only. The second half was not shown to that choice. A filter that looks better on the first half was selected because it looked better there, so that half is not evidence. The 15m books did not set any threshold.

First-half 1h trades 66, winners 13. Kept: none. Order-flow gates are off in this replay. They are not in the winner profile.

## Winners vs losers

Cells are winner median / loser median, or winner rate / loser rate. First half is the only sample that was allowed to choose a filter.

### 15m-best

| feature | first half win/loss | second half win/loss |
| --- | --- | --- |
| trades (win, loss) | 2, 2 | 4, 6 |
| with macro | 50% / 50% | 25% / 33% |
| range macro | 50% / 50% | 75% / 67% |
| ADX 4h | 32.95 / 22.15 | 18.30 / 17.65 |
| ADX daily | — / — | — / — |
| ADX 1h | 28.10 / 26.00 | 28.95 / 23.35 |
| daily/session only | 0% / 0% | 0% / 0% |
| cluster includes daily/session | 100% / 100% | 75% / 100% |
| touches | 5.50 / 8.00 | 8.00 / 7.00 |
| US session | 50% / 50% | 75% / 50% |
| hour UTC | 16.00 / 12.50 | 15.50 / 18.50 |
| stop bps | 89.36 / 23.09 | 48.86 / 36.17 |
| stop in ATR | 0.84 / 0.85 | 1.08 / 1.10 |
| target R | 4.88 / 4.67 | 3.21 / 3.01 |
| sweep bps | 41.47 / 9.41 | 26.64 / 20.61 |
| reclaim bar bps | 96.61 / 13.05 | 44.60 / 27.86 |
| MFE R | 3.73 / 2.03 | 3.26 / 0.38 |
| MAE R | 0.43 / 3.15 | 0.77 / 1.10 |
| hold hours | 42.12 / 10.00 | 7.00 / 4.38 |
| hours to TP (winners) | 12.25 / — | 7.00 / — |

Level families (win, loss) first half: 4h+daily 1/2, 4h+daily+session 1/0

Coins (win, loss) first half: BTC 0/1, ETH 1/0, SOL 0/0, xyz:GOLD 1/1, xyz:SP500 0/0, xyz:XYZ100 0/0

### 15m-baseline

| feature | first half win/loss | second half win/loss |
| --- | --- | --- |
| trades (win, loss) | 2, 3 | 4, 7 |
| with macro | 50% / 33% | 25% / 14% |
| range macro | 50% / 67% | 75% / 86% |
| ADX 4h | 32.95 / 33.10 | 18.30 / 17.90 |
| ADX daily | 41.00 / 21.00 | 25.60 / 24.40 |
| ADX 1h | — / — | — / — |
| daily/session only | 0% / 0% | 0% / 0% |
| cluster includes daily/session | 100% / 100% | 75% / 86% |
| touches | 5.50 / 7.00 | 8.00 / 7.00 |
| US session | 50% / 33% | 75% / 57% |
| hour UTC | 16.00 / 4.00 | 15.50 / 18.00 |
| stop bps | 89.36 / 31.46 | 48.86 / 43.46 |
| stop in ATR | 0.84 / 0.76 | 1.08 / 1.19 |
| target R | 4.88 / 3.98 | 3.21 / 3.00 |
| sweep bps | 41.47 / 8.55 | 26.64 / 21.86 |
| reclaim bar bps | 96.61 / 10.76 | 44.60 / 32.47 |
| MFE R | 3.73 / 1.05 | 3.26 / 0.45 |
| MAE R | 0.43 / 1.27 | 0.77 / 1.10 |
| hold hours | 42.12 / 4.25 | 7.00 / 2.50 |
| hours to TP (winners) | 12.25 / — | 7.00 / — |

Level families (win, loss) first half: 4h+daily 1/2, 4h+daily+session 1/1

Coins (win, loss) first half: BTC 0/1, ETH 1/0, SOL 0/0, xyz:GOLD 1/1, xyz:SP500 0/0, xyz:XYZ100 0/1

### 1h-130d

| feature | first half win/loss | second half win/loss |
| --- | --- | --- |
| trades (win, loss) | 13, 53 | 6, 13 |
| with macro | 46% / 49% | 33% / 46% |
| range macro | 54% / 51% | 67% / 54% |
| ADX 4h | 21.80 / 22.70 | 18.30 / 23.00 |
| ADX daily | — / — | — / — |
| ADX 1h | 22.90 / 20.50 | 28.25 / 23.50 |
| daily/session only | 0% / 2% | 0% / 0% |
| cluster includes daily/session | 100% / 100% | 83% / 100% |
| touches | 6.00 / 4.00 | 6.00 / 7.00 |
| US session | 38% / 47% | 50% / 46% |
| hour UTC | 16.00 / 14.00 | 14.50 / 15.00 |
| stop bps | 39.78 / 42.35 | 56.71 / 28.98 |
| stop in ATR | 1.17 / 1.06 | 1.47 / 1.07 |
| target R | 2.28 / 2.74 | 3.03 / 4.15 |
| sweep bps | 22.67 / 20.84 | 30.67 / 15.14 |
| reclaim bar bps | 33.90 / 61.95 | 70.44 / 42.13 |
| MFE R | 2.76 / 1.06 | 3.61 / 1.19 |
| MAE R | 0.41 / 1.24 | 0.46 / 1.52 |
| hold hours | 10.00 / 2.00 | 11.00 / 2.00 |
| hours to TP (winners) | 8.50 / — | 10.00 / — |

Level families (win, loss) first half: 4h+daily 4/11, 4h+daily+session 3/8, 4h+session 6/33, session 0/1

Coins (win, loss) first half: BTC 3/13, ETH 0/3, SOL 0/10, xyz:GOLD 2/5, xyz:SP500 4/14, xyz:XYZ100 4/8

## What the first half was allowed to keep

- trend-only: not kept. winners were not more aligned than losers. Not applied as a chosen filter. The separation rule failed, so this row was not used to set a threshold.
- adx-4h: not kept. 4h ADX did not separate winners from losers. Not applied as a chosen filter. The separation rule failed, so this row was not used to set a threshold.
- daily-or-session-3: not kept. Exclusive daily/session rate was 0% of winners and 0% of losers. A mixed 4h cluster that also tagged PDH does not count. Not applied as a chosen filter. The separation rule failed, so this row was not used to set a threshold.
- session-us: not kept. winners were not more often in the US window. Not applied as a chosen filter. The separation rule failed, so this row was not used to set a threshold.
- sweep-band: not kept. loser median was inside the winner sweep band, or the band was empty. Not applied as a chosen filter. The separation rule failed, so this row was not used to set a threshold.

## Filters

A kept rule is replayed on the full books. The first half was the fitting sample, so only the second half is out of sample. A row named check: is the owner's example replayed for transparency. It was not chosen from winners, and it is not the combined config.

| book | filter | trades | win | avg W R | avg L R | E | net | DD | H1 n/E | H2 n/E | coins | <20 | improves | profitable |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 15m-baseline | ref | 16 | 37.5% | 2.85 | -2.16 | -0.278 | -1.76% | 4.88% | 5/-0.427 | 11/-0.210 | 0 | True | False | False |
| 15m-baseline | check:daily-or-session-3 | 3 | 0.0% | 0.00 | -2.30 | -2.298 | -2.98% | 3.45% | 2/-2.337 | 1/-2.220 | 1 | True | False | False |
| 15m-best | ref | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| 15m-best | check:daily-or-session-3 | 1 | 0.0% | 0.00 | -2.22 | -2.220 | -0.99% | 0.99% | 0/0.000 | 1/-2.220 | 1 | True | False | False |
| 1h-130d | ref | 85 | 22.4% | 2.30 | -2.08 | -1.101 | -35.18% | 37.40% | 66/-1.194 | 19/-0.776 | 0 | False | False | False |
| 1h-130d | check:daily-or-session-3 | 3 | 0.0% | 0.00 | -1.63 | -1.634 | -2.94% | 3.04% | 2/-1.609 | 1/-1.682 | 0 | True | False | False |

## Verdict

No winner-based filter separated on the first half of the 130-day 1h book under the pre-registered rules (winners had to differ from losers, and the first half had to keep at least 8 trades at a higher expectancy). The daily-or-session level with at least 3 touches was still replayed as a check (rows named check:daily-or-session-3). It was not a chosen filter, and it does not count toward the robustness bar. Combined config: none. Monday paper stays on the spec defaults. Live scalp notes (reports/model_b_trade_notes.json) are not in this repo, so they were not used. Overfitting: five hypotheses were scored on the first half. That half is not evidence, and a second-half pass would still be one look after those screens. MFE, MAE, and time in the trade separate winners from losers only after the trade is open. They were not used as entry filters.

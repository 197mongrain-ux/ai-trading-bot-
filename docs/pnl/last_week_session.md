# Last week, US cash session

Paper research only. One week is not a verdict. This does not change Monday's paper book, and it does not place an order.

## Window

Monday 5 Oct 2026 09:30 through Friday 9 Oct 2026 16:00, America/Toronto. Entries only while that cash session is open (start inclusive, 16:00 exclusive, weekdays). Overnight and +1R swing variants may exit after 16:00. The 3-day max hold still applies. The candle file ends Sunday night UTC (2026-10-12 01:00), so a Friday fill does not get its full three days. A position still open on the last bar is `data_end`.

## Sample

Five sessions. 2592 swing cells were tried. That is the whole locked `swing_search` grid (2 entries × 3 level sources × 2 confirm timeframes × 3 macro settings × 3 stop multiples × 4 targets × 3 session rules × 2 coin books = 2592). The 90-day search left 15m unrun because those bars do not cover 90 days. This week they do, so 15m is included. Ranking the top 5 by expectancy after seeing all 2592 cells is a multiple comparison.

The 90-day penalty formula on 2592 cells is 0.198 R. It is not applied as a pass mark. This week has no holdout, and it cannot clear the 30-trade rule. A cell with fewer than 5 trades is not in the top 5. The single best cell with no trade floor is an anecdote, written separately.

Cells with no trades: 684. Cells with 1 to 4 trades: 1419. Cells with at least 5 trades: 489. Cells with expectancy above zero (any trade count): 678, and the largest of those has 7 trades.

## Costs

Fees are on. Funding is not. Swing slip is the booked 25 bp on main coins and 30 bp on xyz, inside the size and on a stop, a 16:00 flatten, a 3-day max hold, and a data-end flatten. A target is a maker fill with no slip.

Scalp size is the live shadow size: 2% of a $10,000 research book, 20x, fees inside the 2% cap, and the 5 bp / 20 bp stop-slip allowance not taken out of the posted size. Stops, max-hold marks, and data-end flattens then pay that 5 bp (main) or 20 bp (xyz) plus the taker fee. Targets pay the maker fee and no slip. The $10,000 book is not the live account.

A second book at slip measured from live fills was not run. `reports/model_b_trade_notes.json` is not in this repository, and there is no per-trade fill file to measure. The only stop-market print written down is xyz:SKHX on 8 Oct 2026: trigger 1198.8, fill 1200.6, 1.8 points, 15.0 bp of the 1196.7 entry (`stop_slip.py`). SKHX is not in this week's coin list. The xyz floor used above is already 20 bp, which sits above that print. The same week, an xyz:XYZ100 take-profit triggered at 31165 and filled at 31162 (`account.py`). That is three points toward the target, not a stop-slip sample. It is not applied to the other coins.

## Candles

1h and 15m come from the local cache, extended from Hyperliquid `candleSnapshot` when Friday's 16:00 ET close was missing. Both cover Monday's open through Friday's close. 1m was not in the cache and was fetched the same way. The public 1m snapshot only keeps about 5,000 bars, which from this fetch reaches back to Thursday 8 Oct and no further. Requests for Monday through Wednesday returned no bars. Nothing fetched is committed.

1h last close, earliest coin: 2026-10-12 02:00 UTC (covers Friday 16:00 ET). 15m last close, earliest coin: 2026-10-12 01:15 UTC (covers Friday 16:00 ET). 1m coins loaded: 13 of 13. 1m first opens: TAO 2026-10-08 06:31 ET through ONDO 2026-10-08 10:06 ET. This is after Monday's open, so earlier cash sessions are absent. 1m last close, earliest loaded coin: 2026-10-12 01:01 UTC (covers Friday 16:00 ET).

## Swing

Flow is off. There is no aggressor tape, so absorption, print count, and delta are not in any cell. Confirm is the bar that must sweep or touch. 1h cells are managed on 1h bars. 15m cells are managed on 15m bars. Levels, the 4h/daily macro, and the ATR used in the stop are 1h in both, which is how `plan_trade` already reads them. One position. A working order is not replaced, and it is cancelled at 16:00, so a signal whose next bar closes at 16:00 does not fill.

Signals: 259 on 1h, 370 on 15m.

Top 5 by expectancy, among cells with at least 5 trades. This is not a pick for Monday.

Each of these cells has 5 trades, the ranking floor. That is what the best of 2592 cells looks like on five sessions.

Rows 1, 2, 4 share entries (`touch/4h/15m/none/k1.5/all`). Only the target or the 16:00 rule changes. They are one sample counted more than once, not separate setups.

### 1. `touch/4h/15m/none/k1.5/tp3/be/all`

5 trades, win 60.0%, avg winner 2.966 R, avg loser -1.986 R, expectancy 0.985 R, net 5.37%, max DD 1.24%

Exits: stop 1, tp 3, SESSION_CLOSE 1. Coins: ETH 3/2.966R, xyz:GOLD 1/-0.577R, xyz:SP500 1/-3.396R.

| Day | Trades | Win% | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| 2026-10-05 | 1 | 0.0% | -0.577 | -31.49 |
| 2026-10-06 | 1 | 100.0% | 2.954 | 199.16 |
| 2026-10-07 | 1 | 0.0% | -3.396 | -101.72 |
| 2026-10-08 | 2 | 100.0% | 2.972 | 471.31 |
| 2026-10-09 | 0 | 0.0% | — | 0.00 |

### 2. `touch/4h/15m/none/k1.5/tp3/overnight/all`

5 trades, win 60.0%, avg winner 2.966 R, avg loser -2.613 R, expectancy 0.734 R, net 4.65%, max DD 1.36%

Exits: stop 2, tp 3. Coins: ETH 3/2.966R, xyz:GOLD 1/-1.831R, xyz:SP500 1/-3.396R.

| Day | Trades | Win% | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| 2026-10-05 | 1 | 0.0% | -1.831 | -99.97 |
| 2026-10-06 | 1 | 100.0% | 2.954 | 197.79 |
| 2026-10-07 | 1 | 0.0% | -3.396 | -101.02 |
| 2026-10-08 | 2 | 100.0% | 2.972 | 468.07 |
| 2026-10-09 | 0 | 0.0% | — | 0.00 |

### 3. `touch/session/1h/4h+1d/k1.5/tp3/overnight/all`

5 trades, win 60.0%, avg winner 2.460 R, avg loser -2.111 R, expectancy 0.631 R, net 2.17%, max DD 1.58%

Exits: stop 2, tp 2, data_end 1. Coins: BTC 1/1.634R, xyz:GOLD 1/2.889R, xyz:XYZ100 3/-0.455R.

| Day | Trades | Win% | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| 2026-10-05 | 1 | 0.0% | -2.062 | -100.03 |
| 2026-10-06 | 1 | 100.0% | 2.889 | 160.21 |
| 2026-10-07 | 0 | 0.0% | — | 0.00 |
| 2026-10-08 | 2 | 50.0% | 0.348 | 40.65 |
| 2026-10-09 | 1 | 100.0% | 1.634 | 116.25 |

### 4. `touch/4h/15m/none/k1.5/tp2/be/all`

5 trades, win 60.0%, avg winner 1.966 R, avg loser -1.986 R, expectancy 0.385 R, net 3.09%, max DD 1.24%

Exits: stop 1, tp 3, SESSION_CLOSE 1. Coins: ETH 3/1.966R, xyz:GOLD 1/-0.577R, xyz:SP500 1/-3.396R.

| Day | Trades | Win% | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| 2026-10-05 | 1 | 0.0% | -0.577 | -31.49 |
| 2026-10-06 | 1 | 100.0% | 1.954 | 131.73 |
| 2026-10-07 | 1 | 0.0% | -3.396 | -101.05 |
| 2026-10-08 | 2 | 100.0% | 1.972 | 309.48 |
| 2026-10-09 | 0 | 0.0% | — | 0.00 |

### 5. `sweep/session/1h/none/k1/tp1.5/overnight/crypto`

5 trades, win 60.0%, avg winner 1.446 R, avg loser -1.451 R, expectancy 0.287 R, net 0.78%, max DD 1.99%

Exits: stop 2, tp 3. Coins: BTC 3/0.467R, SOL 2/0.019R.

| Day | Trades | Win% | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| 2026-10-05 | 1 | 100.0% | 1.456 | 100.31 |
| 2026-10-06 | 0 | 0.0% | — | 0.00 |
| 2026-10-07 | 1 | 0.0% | -1.419 | -100.99 |
| 2026-10-08 | 1 | 0.0% | -1.483 | -99.98 |
| 2026-10-09 | 2 | 100.0% | 1.441 | 178.63 |

## Unrestricted best cell

`sweep/4h/1h/4h+1d/k1.5/tp3/overnight/crypto` has the highest expectancy with no trade floor: 1 trades, win 100.0%, avg winner 2.981 R, avg loser — R, expectancy 2.981 R, net 2.50%, max DD 0.63%. That is one cell out of 2592. It is an anecdote, not a result.

## Scalp, structural, tape off

Reference only, against the live rules, not a reconstruction of the live tickets. Live hunts the same coins after 16:00. This replay does not take those entries. Fills have to print on a later bar that is still inside the cash session. An open position is not flattened at 16:00. It runs to the stop, the target, or the last 1m bar.

Monday, Tuesday, and Wednesday have no 1m candles, so a zero on those days is a missing file, not a day without a setup. Thursday starts when each coin's file starts (late morning for BTC). Friday is covered.

What is on, from the current live defaults: latest confirmed 1m swing at least 3 ticks from the close; wick at least 1 tick and 0.3 bp through that swing with the close back through it; 15m and 1h structure filter (a long is blocked when either is bearish, a short when either is bullish); stop past the wick by `max(3 ticks, 2 bp, 0.5×ATR14)`, then the 15 bp minimum-stop walk; take-profit is the next untaken confirmed swing that clears the fee and 1R, walked out to 1.5R when the nearest is closer, skipped when that walk is countertrend and too far, otherwise the 1.5R fallback inside [1, 2]. One thesis per swing. One open position for the headline book, majors first when two coins signal on the same minute.

What is off, because the candles have no aggressor prints: absorption at 1.3, the 30-print minimum, window delta, the last 15 seconds, and counter-flow. No bid or ask, so the post-only join cannot be proven and the limit is the swept wick. No untaken-pool tape, so a pool that was not also a 1m swing is invisible. Do not read this as the live blotter.

19 trades, win 36.8%, avg winner 2.023 R, avg loser -1.634 R, expectancy -0.287 R, net -9.19%, max DD 13.74%

Signals 509. Signals skipped because a ticket was already working or open: 477. Exits: stop 12, tp 7.

Coins with no 1m candles: none.

Per day, one-position book (the day is the fill, in Toronto):

| Day | Trades | Win% | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| 2026-10-05 | 0 | 0.0% | — | 0.00 |
| 2026-10-06 | 0 | 0.0% | — | 0.00 |
| 2026-10-07 | 0 | 0.0% | — | 0.00 |
| 2026-10-08 | 13 | 38.5% | -0.143 | -449.53 |
| 2026-10-09 | 6 | 33.3% | -0.597 | -469.87 |

Structural signals per day, before the one-position cut:

| Day | Signals |
|---|---:|
| 2026-10-05 | 0 |
| 2026-10-06 | 0 |
| 2026-10-07 | 0 |
| 2026-10-08 | 342 |
| 2026-10-09 | 167 |

Per coin, each coin given its own $10,000 book. These nets do not add. Adding them would pretend every coin had a separate account.

| Coin | Signals | Trades | Expectancy | Net $ |
|---|---:|---:|---:|---:|
| BTC | 43 | 14 | -0.597 | -1219.37 |
| ETH | 43 | 15 | -0.776 | -1646.94 |
| SOL | 37 | 13 | -0.268 | -603.14 |
| NEAR | 45 | 21 | -0.615 | -2000.04 |
| PUMP | 51 | 13 | -0.757 | -1548.59 |
| LIT | 50 | 22 | -0.868 | -2605.87 |
| AAVE | 42 | 21 | -1.284 | -3390.30 |
| ONDO | 51 | 23 | -1.031 | -3031.24 |
| WLD | 42 | 24 | -0.878 | -2749.80 |
| TAO | 40 | 19 | -1.002 | -2527.23 |
| xyz:GOLD | 18 | 5 | -0.267 | -183.52 |
| xyz:SP500 | 25 | 7 | -1.680 | -1325.14 |
| xyz:XYZ100 | 22 | 8 | -1.150 | -1095.29 |

### Live sample already written down

PR #17 records 14 closed mainnet round trips from 6 Oct 2026 23:58 ET through 8 Oct 2026 15:31 ET. On that path, today's stops were −2.31R / −$11.26 on a $244 book. The same fills are not stored in this repo, so this replay is not joined to those 14 tickets. That window also includes after-hours entries this study does not take. `reports/model_b_trade_notes.json` is still absent.

## What this does not say

One week, 2592 swing cells, tape off on both books. No setup is promoted. No default moves.

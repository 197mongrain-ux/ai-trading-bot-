# Chris's read as hard filters (fixed widths 0.421% / 2.549%, 2% risk)

Window: Jul 14 to Oct 12 2026 (90 days), BTC, ETH and SOL. One position at a time, at most one trade per coin per day per side, and a 72h time limit.
Fees, slippage, sizing and stop-first handling are the same as `pdl_retest_fixed_width.md`. Leverage stayed about 3.6x and the 20x cap was never hit.
Code: `src/hl_bot/research/disc_filters.py`. Raw data: `pdl_retest_discretionary_filters.json`.

## Rules (short side; longs mirror against PDH)

**Base setup:** at least one 1h close below PDL today. Then a later 1h bar wicks into PDL (within 5 bps or above it) and closes back below. Entry is at that close.

| Filter | Rule |
|---|---|
| a | 2 or more consecutive 1h closes below PDL before the retest |
| c | the existing 4h-led macro read (ADX>20) is "down" ("range" does not count) |
| d | today's low is below yesterday's low. This is always true once (a) holds, so it never blocks anything |
| e | sellers dominant on the rejection bar. Uses tape delta < 0 where the HL tape covers (since Oct 9, 12 of 383 bars). Otherwise a candle proxy: red bar closing in the lower half. 371 of 383 bars used the proxy |
| f | no 4h support from `build_levels` between entry (minus 0.1%) and the TP |
| g | sweep high is below the fixed 0.421% stop |

## Funnel (candidate bars)

| step | bars left | pass on its own |
|---|---|---|
| base | 383 | |
| a | 281 | 281 |
| c | 108 | 144 |
| d | 108 | 383 |
| e | 55 | 211 |
| f | 39 | 206 |
| g | 11 | 231 |

## Results

| config | trades | WR | avg R | total R | $5k | max DD | streak | first 60d (n / R) | last 30d (n / R) |
|---|---|---|---|---|---|---|---|---|---|
| ALL filters | 7 | 14% | -0.22 | -1.6 | -3.6% | 7.8% | 4 | 4 / -4.0 | 3 / +2.4 |
| base only | 97 | 10% | -0.46 | -44.2 | -60.8% | 62.5% | 25 | 65 / -40.3 | 32 / -3.9 |
| drop a | 10 | 20% | -0.19 | -1.9 | -4.3% | 9.6% | 5 | 4 / -4.0 | 6 / +2.1 |
| drop c | 17 | 18% | -0.04 | -0.7 | -2.7% | 11.4% | 6 | 11 / -5.6 | 6 / +4.9 |
| drop d | 7 | identical to ALL | | | | | | | |
| drop e | 19 | 16% | -0.14 | -2.7 | -6.6% | 19.6% | 10 | 9 / -9.0 | 10 / +6.3 |
| drop f | 14 | 7% | -0.61 | -8.6 | -16.3% | 19.9% | 11 | 11 / -11.0 | 3 / +2.4 |
| drop g | 18 | 11% | -0.35 | -6.4 | -12.9% | 18.3% | 10 | 9 / -9.0 | 9 / +2.6 |

**WARNING: every filtered sample is under 20 trades, and ALL has only 7.** These numbers cannot tell skill from luck.
The filters cut losses a lot compared with base only, but no filtered version is positive over the 90 days.
Every config loses in the first 60 days and wins in the last 30. That points to the recent market regime, not the filters.

## Today (Oct 12) BTC short vs PDL 82,746. Times are ET bar closes.

| close | entry | sweep high | a | c | e (tape) | f | g | result |
|---|---|---|---|---|---|---|---|---|
| 11 AM | 82,555 | 83,084 | no (1 close) | no (range) | yes | yes | no | fail |
| 12 PM | 82,686 | 82,781 | no | yes | no | yes | yes | fail |
| 1 PM | 82,466 | 82,924 | yes | yes | yes | yes | no (stop would be 82,813) | fail |

The bot would NOT have taken today's trade.
Chris's fill at 82,827 was above the 1h close, so his 0.421% stop (83,176) sat above the 83,084 sweep.
The bot enters at the 1h close, which is lower. With the same width, its stop lands inside the wick, so rule (g) blocks it.
Matching his read would need a limit entry into the retest wick. That is the pdl_retest idea we already tested and dropped.
Verdict: no edge. Do not deploy.

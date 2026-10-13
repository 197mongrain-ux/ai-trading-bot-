# Fixed-width replay: Chris's BTC short widths, 2% risk

Window: 2026-07-14 19:00 to 2026-10-12 19:00 UTC (90 days). Out-of-sample split at 2026-09-12 (first 60 days / last 30 days).
Stop is 0.421% from entry and target is 2.549%, mirrored for longs. Each trade is sized so a stop-out, including fees, 2 bps entry slip and 5 bps stop slip, loses exactly 2% of equity.
Fees use `conservative_fees` (taker on entry and stop, maker on target), the same as `pdl_retest.replay`. If a bar touches both stop and target, the stop counts first.
Exits are checked on 15m bars where we have them, otherwise 1h. Leverage needed was 3.6x on every trade, so the 20x cap was never hit.
A win nets about +4.5R after fees and slip, so the break-even win rate is about 18%.
Code: `src/hl_bot/research/fixed_width.py`. Raw data: `pdl_retest_fixed_width.json`.

| run | trades | WR | avg R | total R | $5k compounded | max DD | worst streak |
|---|---|---|---|---|---|---|---|
| (1) pdl_retest bot mode (one position at a time) | 34 | 12% | -0.34 | -11.5 | -22.2% | 31.6% | 16 |
| ...first 60d | 23 | 4% | -0.76 | -17.6 | -30.2% | | 15 |
| ...last 30d | 11 | 27% | +0.55 | +6.1 | +11.5% | 5.9% | 3 |
| (2) setup_search, every setup in the 90d (196) | 196 | 12% | -0.33 | -63.9 | -75.4% | 80.6% | 28 |
| ...first 60d | 141 | 10% | -0.46 | -64.9 | -74.7% | | 26 |
| ...last 30d | 55 | 18% | +0.02 | +1.0 | -2.5% | 28.8% | 16 |
| (2b) setup_search, one position at a time | 85 | 15% | -0.16 | -13.6 | -28.4% | 44.1% | 19 |
| ...first 60d | 60 | 12% | -0.37 | -21.9 | -37.8% | | 19 |
| ...last 30d | 25 | 24% | +0.33 | +8.4 | +15.2% | 14.9% | 8 |

Per coin / side, total R:
- (1) BTC 9 trades, 0 wins, -8.3R. ETH 15 trades, 13% WR, -4.1R. SOL 10 trades, 20% WR, +0.9R. Longs 14 trades, -3.1R. Shorts 20 trades, 10% WR, -8.4R.
- (2) BTC 74 trades, 11% WR, -29.8R. ETH 53 trades, 9% WR, -25.8R. SOL 69 trades, 16% WR, -8.3R. Longs 118 trades, -36.4R. Shorts 78 trades, -27.5R.

How trades ended: almost all hit the stop or the target. With no time limit, 1 trade in (1) and 2 in (2) were still open at the end of the data.
A 72h limit changes nothing in (1). In (2) it closes 2 trades on time and moves the total from -63.9R to -62.9R, which is basically the same.
The 0.42% stop is about the size of normal 1h noise, so most entries get stopped before the move can develop.

The library holds 340 setups over about 180 days, and 196 of them fall inside this 90-day window.
The last 30 days look better than the first 60, but the sample is small: 11 trades in (1) and 25 in (2b). The first 60 days lose heavily. Verdict: no edge. Do not deploy.

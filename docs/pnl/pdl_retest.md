# PDL/PDH retest book (paper only, replay)

Prior day = UTC day. Rules in `src/hl_bot/research/pdl_retest.py`. HL candles (1h 150d, 15m ~52d);
real tape delta only for Oct 9-12 (hl_tape), candle close-location proxy elsewhere. Stop wins
same-bar ties. Fees + 2 bp entry slip + 5 bp stop slip. Size via `size_swing` (2%, <=20x, 25 bp reserve).

| run | trades | win rate | avg R | total R | net | max DD |
|---|---|---|---|---|---|---|
| 1h retest, 90d, stop = bar high + 0.1 ATR | 29 | 10% | -0.02 | -0.6 | -3.8% | 19.8% |
| 15m retest, Aug 23-Oct 12 | 27 | 15% | -0.09 | -2.3 | -5.9% | 24.3% |
| 1h retest, 90d, stop + 0.5 ATR (one tweak, curve-fit risk) | 24 | 21% | +0.19 | +4.6 | +7.6% | 8.1% |

All simulated trades passed the size/risk check (max risk 2.00%, max 4.8x). Not ready for a paper loop.

## Stop-placement study (candidates fixed before running)

Hourly retest, 1h bars for exits in both periods (stop wins ties), same 2% / 20x / 2R-room / fees.
In-sample 2026-04-18 to 07-14 (HL 1h history starts 03-18; first 31 days are warmup). Held-out 07-14 to 10-12.
The pick is the best in-sample total R. Caveat: the held-out window is the same 90 days the first replay used.

| stop | IS trades | IS win | IS avg R | IS total R | IS max DD | HO trades | HO win | HO avg R | HO total R | HO max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| wick+0.1 ATR (baseline) | 30 | 10% | -0.54 | -16.2 | 28.3% | 33 | 9% | -0.10 | -3.4 | 23.1% |
| wick+0.25 ATR | 26 | 12% | -0.50 | -12.9 | 23.3% | 30 | 13% | +0.00 | +0.0 | 17.6% |
| wick+0.5 ATR | 22 | 9% | -0.62 | -13.7 | 24.2% | 28 | 14% | -0.07 | -2.0 | 17.4% |
| **wick+0.75 ATR (in-sample pick)** | 18 | 11% | -0.63 | -11.4 | 20.6% | 26 | 15% | -0.11 | -2.8 | 16.3% |
| last 1h high | 31 | 13% | -0.46 | -14.2 | 25.6% | 34 | 9% | -0.06 | -2.1 | 23.2% |
| last 2 x 1h high | 29 | 14% | -0.46 | -13.2 | 23.6% | 33 | 9% | -0.10 | -3.4 | 23.1% |
| PDL+1 ATR | 17 | 6% | -0.74 | -12.6 | 22.5% | 24 | 12% | -0.30 | -7.2 | 22.2% |

All 371 simulated trades passed size/risk (<=2.00%, <=20x). Verdict: no stop choice gives this entry an edge.
Every variant loses in-sample; the pick loses held-out (-2.8R, -6.8%). The earlier +7.6% for wick+0.5 ATR
came from 15m exit resolution on one window and does not hold up. Do not paper-trade or deploy.

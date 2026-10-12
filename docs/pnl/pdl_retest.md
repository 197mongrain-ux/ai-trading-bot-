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

# Model B with the mainnet-flip gates: 90-day replay

Research only. Nothing here changes the live bot.

## Config live at the mainnet flip

The first mainnet fill was Oct 6 23:58 ET (PR #17 ledger). The newest commit before it is **190ca7b** ("Hunt the expanded Model B universe in both sessions", Oct 6 22:14 ET).

Engine at that commit:
- 1m confirmed-swing sweep and reclaim on the 90s tape. The print floor is 30.
- Absorb at least 1.3.
- Window and last-15s delta inside a flat band of max($100/mid, 0.05 coin).
- Post-only Alo at the sweep, with no timeout. It is cancelled when price trades through the sweep.
- Stop past opposing liquidity (`place_stop`).
- TP at the nearest swing or untaken pool that is at least 1R and clears fees. With no pool past that floor, TP is a fixed 1.5R.
- Size is 2% of spot USDC over the stop distance, capped at 20x. Fees are **not** inside the 2%; the fee-inclusive cap only arrived in a709138 on Oct 7 23:44 ET.
- Universe: the expanded 13-coin list (BTC, ETH, SOL, NEAR, PUMP, LIT, AAVE, ONDO, WLD, TAO, xyz:GOLD/SP500/XYZ100).
- Margin reserve: 60% free margin for the sole close BTC (decdd9b).
- Not included: PR #13–#15 TP walk, the ADX macro side filter (773b898), two-sided hunt, structure and counter-flow filters.

The **1.67R floor came later**, at the Oct 8 open. That was the MODEL_B_TP_R=1.67 setting plus the pool floor. Both are run below:
- **flip**: exactly 190ca7b.
- **r167**: pool floor and fallback both 1.67R.

## Method and proxies (read before the numbers)

- **Engine:** the real 190ca7b `ModelBEngine.evaluate` and `pools_from_bars` run every closed minute on BTC, ETH and SOL only. Alts and xyz have no 90-day 1m history here.
- **Candles:** HL keeps about 3.5 days of 1m, so the 90 days use **Binance USDT-M 1m klines**. Window: Jul 14 to Oct 12 2026 00:00 UTC. Split at Sep 12: first 60 days vs last 30.
- **Synthetic tape:** 12 prints per bar along O→L→H→C (or O→H→L→C), timed 5s apart. Buy and sell size come from Binance taker-buy volume.
- **Scaling to HL:** volume is scaled to HL size using the overlap ratio (BTC 0.21, ETH 0.155, SOL 0.115). Print count is scaled the same way (BTC 0.10, ETH 0.047, SOL 0.044) for the 30-print floor.
- **Real HL tape:** only covers Oct 9 onward, so it is **not** used for delta or absorb.
- **What the proxy cannot see:** absorb and delta come from candle shape plus taker split, not real sub-minute prints. Treat arm counts as approximate.
- **Fills:** the Alo fills when a later 1m bar touches the limit. It is cancelled if the stop or TP trades first, or after 240 min.
- **Exits:** stop first when one bar touches both. Stop slip is 5 bps. Fees: maker 1.5 bps on entry and TP, taker 4.5 bps on the stop.
- **Positions:** one ticket per coin, and coins can overlap. Compounding on $5k is in exit order.
- **Not simulated:** the margin reserve rule.

## Results (Binance 1m proxy, 90 days)

| | trades | WR | avg win % | avg loss % | avg R | total R | $5k | max DD | streak | 20x-capped |
|---|---|---|---|---|---|---|---|---|---|---|
| flip | 122 | 52% | +0.15% | −0.18% | −0.38 | −46.0 | −61.6% | 65.4% | 5 | 40 |
| flip first 60d | 84 | 54% | | | −0.32 | −26.8 | −42.9% | | | |
| flip last 30d | 38 | 47% | | | −0.51 | −19.2 | −32.7% | | | |
| r167 | 152 | 38% | +0.24% | −0.19% | −0.54 | −82.0 | −81.9% | 83.3% | 8 | 47 |
| r167 first 60d | 104 | 37% | | | −0.57 | −59.7 | | | | |
| r167 last 30d | 48 | 40% | | | −0.47 | −22.3 | | | | |

Per coin, flip: BTC 60 trades, 57% WR, −16.9R. ETH 32 trades, 44% WR, −18.5R. SOL 30 trades, 50% WR, −10.6R.
By side, flip: longs 63 trades, −23.9R. Shorts 59 trades, −22.1R.
r167: BTC −29.1R, ETH −27.2R, SOL −25.7R. Longs −42.3R, shorts −39.7R.

Why it loses even at a 52% win rate:
- Stops are tiny. The median is 0.17%, and BTC's median is 0.05%.
- With fees outside the 2%, a stop costs about −1.6R to −1.9R. A win is only about +0.3R to +1.6R.
- The 20x cap trims about a third of the BTC trades.

## Live comparison, Oct 7–9 (HL real 1m candles, candle-proxy tape)

| | trades | WR | total R |
|---|---|---|---|
| replay flip | 8 | 75% | +0.79 |
| replay r167 | 11 | 64% | +6.0 |
| actual live (PR #17 ledger, 14 trades, all coins incl. xyz) | 14 | 43% | −2.31 |
| actual live, BTC/ETH/SOL only | 7 | 29% | −2.87 |

The replay does not reproduce live. It arms different minutes, and the candle tape is not the real tape. Three days is also too short to mean anything.
The 90-day result is the read: these gates lose about 0.4–0.5R per trade over 120–150 trades.

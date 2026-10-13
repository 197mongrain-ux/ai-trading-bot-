# Wide-stop PDL/PDH retest on the top-15 HL perps

## Universe

Top 15 perps by 24h notional volume, pulled live from `metaAndAssetCtxs` on Oct 12 2026 at about 8:20 PM ET. Main dex only; xyz is not included.

BTC, ETH, HYPE, NEAR, ZEC, SOL, PUMP, XRP, STRK, ENA, W, ZRO, ONDO, PONS, WLD.

**Skipped:** PONS. It listed on HL on Aug 31, so it has under 90 days of history.

## Data and rules

- **Candles:** HL `candleSnapshot` for everything. 1h has 5000 bars back to March, so the full 90 days plus warmup are covered and Binance was not needed. 15m covers about 52 days; exits use 15m where it covers and 1h before that.
- **Order flow:** the candle proxy. Tape exists only for BTC, ETH and SOL from Oct 9.
- **Window:** Jul 14 to Oct 12 2026, split at Sep 12 into first 60 days and last 30.
- **Filters:** a, c, e and f, with g off.
- **Sizing and exits:** 2% of equity lost at the stop, including fees and slip. 20x cap, though leverage never went above 5.2x. Stop first when a bar touches both. 72h time limit.
- **Variants:**
  - One position at a time across all coins.
  - One position per coin, with coins allowed to overlap. Its compounding is the R sequence in exit order.

Code: `src/hl_bot/research/wide_top15.py`. Raw data: `pdl_retest_wide_top15.json`.

## Results

| stop / target / mode | n | WR | avg R | total R | $5k | max DD | streak | first 60d (n / R) | last 30d (n / R) |
|---|---|---|---|---|---|---|---|---|---|
| sweep+0.25atr / 2R / one-at-a-time | 88 | 30% | −0.18 | −15.6 | −28.9% | 30.5% | 10 | 55 / −13.3 | 33 / −2.4 |
| sweep+0.25atr / 2R / per-coin | 139 | 27% | −0.25 | −34.9 | −52.3% | 53.4% | 10 | 77 / −16.9 | 62 / −18.0 |
| sweep+0.25atr / 3R / one-at-a-time | 82 | 21% | −0.25 | −20.8 | −36.2% | 41.7% | 11 | 50 / −13.0 | 32 / −7.8 |
| sweep+0.25atr / 3R / per-coin | 135 | 21% | −0.27 | −36.1 | −53.9% | 58.8% | 12 | 76 / −21.2 | 59 / −14.9 |
| sweep+0.1% / 2R / one-at-a-time | 105 | 30% | −0.20 | −20.8 | −36.2% | 38.9% | 10 | 64 / −18.2 | 41 / −2.7 |
| sweep+0.1% / 2R / per-coin | 141 | 26% | −0.29 | −41.6 | −58.2% | 61.4% | 11 | 79 / −26.0 | 62 / −15.5 |
| sweep+0.1% / 3R / one-at-a-time | 97 | 22% | −0.22 | −21.1 | −37.1% | 43.0% | 10 | 59 / −15.6 | 38 / −5.5 |
| sweep+0.1% / 3R / per-coin | 139 | 19% | −0.32 | −44.0 | −60.7% | 65.5% | 13 | 78 / −28.7 | 61 / −15.3 |

## Per coin: total R, per-coin mode (n in brackets)

| coin | 0.25atr / 2R | 0.25atr / 3R | 0.1% / 2R | 0.1% / 3R |
|---|---|---|---|---|
| BTC | +4.3 (6) | +3.8 | +4.4 | +3.9 |
| ETH | −1.1 (6) | +0.5 | −1.2 | +0.3 |
| HYPE | −8.2 (11) | −7.3 | −9.2 | −12.0 |
| NEAR | +0.5 (13) | −0.3 | −2.6 | −5.3 |
| ZEC | −2.9 (10) | −1.1 | −3.0 | −1.2 |
| SOL | 0.0 (11) | +3.2 | −0.2 | +3.0 |
| PUMP | −5.5 (17) | −4.9 | −5.7 | −5.1 |
| XRP | −3.0 (3) | −3.0 | −3.0 | −3.0 |
| STRK | −9.0 (9) | −9.0 | −6.1 | −9.0 |
| ENA | +4.0 (13) | +0.7 | +3.8 | +1.9 |
| W | −9.0 (9) | −9.0 | −9.0 | −9.0 |
| ZRO | −5.6 (14) | −11.1 | −9.5 | −10.1 |
| ONDO | +2.9 (9) | +2.8 | +1.9 | +1.8 |
| WLD | −2.2 (8) | −1.3 | −2.2 | −0.3 |

**Helps** in every cell: BTC, ONDO, ENA. SOL helps with 3R targets.
**Hurts** in every cell: HYPE, STRK, W, ZRO, PUMP and XRP. W and STRK went 0 for 9.

Picking only the winning coins after seeing these results would be overfitting. Each coin has 6–13 trades.

## Verdict

Widening to 14 coins gives enough trades (82–141 per cell), and every cell loses in both the first 60 days and the last 30.
The small positive result on BTC, ETH and SOL in the earlier run does not hold up on the wider universe. No edge. Do not deploy.

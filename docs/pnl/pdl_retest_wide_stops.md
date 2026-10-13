# PDL/PDH retest with wide stops and 1–5R targets

Filters a, c, e and f from `pdl_retest_discretionary_filters.md` are on. Filter g is dropped.
Window: BTC, ETH and SOL, Jul 14 to Oct 12 2026. One position at a time and a 72h time limit.
Sizing: 2% of equity lost at the stop, including fees and slip, with a 20x cap. Max leverage used was 5.2x, so the cap never bound.
When one bar touches both stop and target, the stop counts first.
Code: `src/hl_bot/research/wide_stops.py`. Raw data: `pdl_retest_wide_stops.json`.
Nothing was picked on the last 30 days. Every cell is shown.

**Funnel:** 383 base → a 281 → c 108 → e 55 → f 39 bars. That is about 15–18 trades after the one-at-a-time and one-per-day rules.

**WARNING: every cell is under 20 trades.** None of this separates skill from luck.

| stop | target | n | WR | avg R | total R | $5k | max DD | streak | first 60d R | last 30d R |
|---|---|---|---|---|---|---|---|---|---|---|
| sweep+0.1% (avg 0.76%) | 2R | 18 | 44% | +0.12 | +2.2 | +4.0% | 7.2% | 2 | −3.2 (8) | +5.4 (10) |
| sweep+0.1% | 3R | 17 | 35% | +0.18 | +3.1 | +5.6% | 9.9% | 5 | −5.1 (8) | +8.2 (9) |
| sweep+0.1% | 5R | 16 | 12% | −0.31 | −5.0 | −10.3% | 15.2% | 8 | −8.0 | +3.0 |
| sweep+0.25atr (0.85%) | 2R | 18 | 44% | +0.13 | +2.4 | +4.3% | 6.9% | 2 | −3.1 | +5.4 |
| sweep+0.25atr | 3R | 17 | 35% | +0.21 | +3.6 | +6.5% | 9.7% | 5 | −5.0 | +8.5 |
| sweep+0.25atr | 5R | 16 | 12% | −0.30 | −4.8 | −9.9% | 15.0% | 8 | −8.0 | +3.2 |
| level+1 ATR (1.15%) | 2R | 17 | 41% | +0.14 | +2.3 | +4.2% | 6.2% | 2 | −2.8 | +5.1 |
| level+1 ATR | 3R | 16 | 12% | −0.50 | −8.0 | −15.2% | 17.4% | 8 | −8.0 | +0.0 |
| level+1 ATR | 5R | 16 | 12% | −0.53 | −8.5 | −16.1% | 20.8% | 8 | −8.0 | −0.5 |
| level+1.5 ATR (1.55%) | 2R | 16 | 25% | −0.26 | −4.2 | −8.4% | 12.2% | 4 | −5.2 | +1.0 |
| level+1.5 ATR | 3R | 16 | 19% | −0.40 | −6.4 | −12.5% | 14.9% | 4 | −4.3 | −2.1 |
| level+1.5 ATR | 5R | 16 | 19% | −0.17 | −2.8 | −6.3% | 12.6% | 4 | −2.4 | −0.4 |
| last 4h swing (3.6%) | 2R | 15 | 33% | −0.35 | −5.3 | −10.3% | 11.5% | 3 | −3.7 | −1.6 |
| last 4h swing | 3R | 15 | 33% | −0.29 | −4.4 | −8.7% | 11.5% | 3 | −3.7 | −0.7 |
| last 4h swing | 5R | 15 | 33% | −0.30 | −4.5 | −8.8% | 11.5% | 3 | −3.7 | −0.8 |
| any stop | next 4h pool 1–5R | 2–7 | 0% | | −2.0 to −4.4 | | | | | |

The pool target almost never fires. Filter f already removes setups with 4h support between entry and a 2.55% TP, so 19–34 of about 36 setups have no pool 1–5R away and are skipped.

Per coin and side, for the two best cells:
- sweep+0.1% with 2R: BTC +2.6R, ETH −0.2R, SOL −0.1R. Longs 13 trades +1.3R, shorts 5 trades +0.9R.
- sweep+0.25atr with 3R: BTC −1.2R, ETH +1.5R, SOL +3.2R. Longs 12 trades +0.8R, shorts 5 trades +2.7R.

**Read:** stops tied to the sweep with 2–3R targets are the only cells above zero, at about +2 to +3.6R on 17–18 trades.
Every cell loses in the first 60 days and only makes money in the last 30. That is the same regime pattern as before, not proof.
Nothing qualifies for paper or live on this evidence.

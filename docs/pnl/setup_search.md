# Similar-setup search (PDL/PDH retest, BTC/ETH/SOL), research only

`python -m hl_bot.research.setup_search {stats|scan|near|now}`. See the module docstring.

- **Library:** 340 resolved retest setups from Apr 18 to Oct 12 (HL 1h; the first 31 days are warmup). The macro, flow and room filters are recorded as features, not used to drop setups.
- **Outcome:** stop = retest wick + 0.25 ATR, target = next 4h level. 93 of 340 setups (27%) reached +2R before the stop. Average realized R was -0.06, and only 75 hit the target.

## What separates the +2R setups (17 features)
The strongest effects are small (|Cohen d| <= 0.33, AUC <= 0.62):
- More room to the next 4h level: 8.0R vs 6.3R.
- Entry closer to the day VWAP: 0.31 vs 0.54 ATR past it.
- Higher 1h ADX: 30 vs 27.
- Lower relative volume on the retest bar: 0.97 vs 1.22.

The sweep, the close location on the retest bar, the number of closes beyond the level, retest depth and agreement with the 4h trend show essentially no separation. With 17 features and about 90 winners, some of these effects are expected by chance alone.

## Leave-one-out check
- **kNN (k=7):** the setups it rated best won 35% of the time, against a 27% base rate; its worst third won 17%. That's a weak signal.
- **Scan:** the top 15 setups most similar to the average winner had 4 winners (27%), the same as the base rate.

Treat this as a lookup tool for context, not a signal. Don't trade it.

## Chart patterns (definitions fixed before outcomes; see `patterns.py`)
Run with `python -m hl_bot.research.pattern_study`; the full table is in `setup_search_patterns.md`.
- **Patterns tested:** double top and H&S for shorts; double bottom and inverse H&S for longs.
- **Pivots:** 1h fractal pivots, 48h window.
- **Two settings:** A (n=2, 0.3 ATR) and B (n=3, 0.5 ATR).

Results across the 340 setups:
- **Double top/bottom:** they did *not* help. The +2R rate was 15% with vs 30% without (A, Fisher p=0.02) and 20% vs 30% (B, p=0.11). Avg R differences have 95% CIs that include 0. With 18 comparisons, p=0.02 is not convincing; at most it suggests these setups are worse.
- **H&S / inverse H&S:** too rare to judge (15-19 setups; at the level, 1-2).
- **"At the level":** too rare (8-10 setups).
- **Neckline broken:** no reliable effect, and the sign flips between settings for avg R.

Verdict: no evidence that these patterns improve the PDL/PDH retest. Setting A flags (`pat_any`, `pat_at_level`, `pat_neck_broken`) are now kNN features.

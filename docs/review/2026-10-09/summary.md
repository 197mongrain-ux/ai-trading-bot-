# Nightly review 2026-10-09

Paper and research only. This command reads the journal and the tape. It does not place an order and it does not change the running book.

Hard limits stay fixed: a stop is always present, max loss is 2%, leverage is 20, and risk per trade stays at or under 2%. Those knobs are not tunable.

## Day

- Closed labelled trades on 2026-10-09: 0 (0 winners, 0 losers).
- Labelled closes used for walk-forward (journals plus history, all dates): 0.
- Tune trades: 0. Holdout trades: 0.

Budget R is net dollars divided by the risk budget (stop plus reserved slip plus fees). A row with only pnl and no budget is unlabelled and stays out of the table.

## Winners versus losers

Means skip a missing feature. Marks and blocks are not in this table.

| feature | winners | losers |
| --- | --- | --- |
| absorb | — | — |
| window_delta | — | — |
| last_15s_delta | — | — |
| imbalance_ratio | — | — |
| stacked | — | — |
| zone_dist_bps | — | — |
| stop_bps | — | — |
| target_r | — | — |
| pool_r | — | — |
| print_count | — | — |

- Coin, winners: —
- Coin, losers: —
- Macro, winners: —
- Macro, losers: —
- Hour UTC, winners: —
- Hour UTC, losers: —

## Blocks

- No arm-block rows in the journals.

## Walk-forward

Closed labelled setups are ordered by time. The first 70% tunes. The rest is the holdout. Every tune timestamp is strictly earlier than every holdout timestamp.
On the tune set, each filter knob contributes at most one value: the tighter-than-default setting with the best tune expectancy and at least 8 trades. Looser gates are marked LOOSER_THAN_TRADED and are not scored. A missing feature fails the tighter gate.
A candidate survives only when the holdout has at least 20 trades, holdout expectancy (mean budget R) beats the unfiltered holdout by more than the penalty, and holdout max drawdown is not worse.
The penalty is 0 when one candidate reaches the holdout, otherwise 0.05 * sqrt(2 * log N). N counts only candidates whose holdout was computed. Placement knobs that need a replay do not inflate N.
Placement knobs (stop floor, ATR multiple, slip mode, slip allowance, delta flat epsilon) change economics. They are NEEDS_REPLAY unless a replay function is injected. This command does not invent a replay.

| env | value | status | tune n | tune E | hold n | hold E | hold DD | ref E | ref DD | penalty |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ABSORB_MIN | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_MIN_PRINTS | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_IMBALANCE | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_STACKED | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_ZONE_BPS | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_MIN_R | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_TP_MIN_POOL_R | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_MIN_STOP_BPS | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_MIN_STOP_ATR | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_ATR_FRAC | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_SLIP_MODE | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_SWING_TRAP_SLIP_BPS | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |
| MODEL_B_DELTA_FLAT_EPS | — | NO_SPLIT | 0 | — | 0 | — | — | — | — | 0.0000 |

## Survivors

No candidate cleared the held-out bar. Do not change the paper book.

Far too little data to treat a result as an edge.

## Env diff

```
# Nightly review env diff for 2026-10-09
# Paper and research only. One change. Hard limits are not in this file.
# Stop stays on, max loss stays 2%, leverage stays 20, risk per trade stays at or under 2%.
# Do not apply more than the uncommented line. Each other survivor was tested alone.
# Filter results drop taken trades. They are not a fresh path replay.
# No candidate cleared the held-out bar. Do not change the paper book.
```

## ML scorer

ML not trained: 0 labelled setups, need 300. Log only. No parameter change.

The ML scorer is log-only. It does not change a parameter and it does not edit the env diff.

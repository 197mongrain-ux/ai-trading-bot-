# Swing search

Paper research only. This does not change Monday's paper book.

## Limit

Candles only. Flow is off. There is no historical aggressor tape, so absorption, print count, and delta are not in any cell. Costs are maker/taker fees and the 25 bp / 30 bp slip. Funding is not applied. A target is a maker fill. A stop, a 3-day max hold, a 16:00 flatten, and a position still open on the last bar pay taker plus slip.

The 1h cache covers the last 90 days. The 15m cache spans 52.1 days, which cannot fill a 60-day tune and a 30-day holdout. Those cells were registered and not run.

## Rules, fixed before the run

Grid: entry sweep or same-bar touch-reclaim; level source 4h pivots with at least two touches, prior-day/week high or low, or a daily fractal; confirm 15m or 1h; macro 4h+daily both, 4h only, or none; stop beyond the level by 0.5, 1.0, or 1.5 times the 1h ATR(14); target a fixed 1.5R, 2R, or 3R, or the next opposite level inside 1R to 3R; session hold overnight, hold only if the 16:00 close is at least +1R and then stop at entry, or flatten at 16:00; coins all six or BTC/ETH/SOL.

Touch-reclaim is a same-bar low through support and a close back above it, or the short mirror. It has no 5 bp minimum. Sweep is the existing rule: at least 5 bp through the level, same bar or the next bar's close. Entries are only 09:30–16:00 America/Toronto, Monday to Friday. The fill is on a later bar. One position, the higher level score. Unfilled orders cancel at 16:00. Fixed risk 1%, swing notional cap 10x, loss cap 2%, equity 10,000. The 3-day max hold stays. A 4h level uses the paper score floor of 3. A prior-day or prior-week print scores 3 with its bonus. A daily fractal is kept from score 1 so a single daily swing is eligible. Top two levels per side.

A trade belongs to the window that contains its fill. Tune is the first 60 days. Holdout is the last 30. The two halves split the 90 days at 45. The walk-forward pick is the highest tune expectancy among cells with at least 30 trades overall and at least 15 trades in the tune window. The top 5 below are ranked by holdout expectancy among cells with at least 30 trades and at least one holdout trade. That ranking looks at the test window. The walk-forward pick is the cell the tune window would have chosen.

A cell passes the raw bar when it has at least 30 trades, holdout expectancy above zero, and both halves above zero. The penalty is 0.05 * sqrt(2 * ln N), where N is the number of cells run. It is 0.189 R. A cell survives the penalty only when it passes the raw bar and the holdout expectancy is above that number.

## Result

- Cells registered: 2592
- Cells run: 1296
- Cells not run (15m history 52.1 days): 1296
- Signals found: 3383
- Cells passing the raw bar: 0
- Cells surviving the penalty: 0

No setup survives. None of the cells that were run cleared the raw bar and the multiple-comparison penalty.

18 cells were positive in both halves and in the holdout. The largest of those has 19 trades, under the 30-trade floor.

93 cells have a positive full-window expectancy. The largest of those has 22 trades.

## Top 5 by holdout expectancy

| setup | trades | win | avg win R | avg loss R | E | net | max DD | tune E | holdout n/E | H1 n/E | H2 n/E | raw | penalty |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| sweep/4h/1h/none/k1/tp1.5/overnight/all | 34 | 44.1% | 1.307 | -2.185 | -0.645 | -9.44% | 13.16% | -1.075 | 9/0.552 | 20/-0.850 | 14/-0.352 | no | no |
| touch/4h/1h/none/k1.5/tp2/overnight/all | 32 | 43.8% | 1.898 | -1.838 | -0.203 | -2.08% | 8.80% | -0.513 | 10/0.478 | 17/-0.257 | 15/-0.143 | no | no |
| sweep/4h/1h/none/k1/tp1.5/flat/all | 34 | 38.2% | 1.314 | -1.667 | -0.527 | -7.14% | 10.42% | -0.875 | 9/0.439 | 20/-0.694 | 14/-0.289 | no | no |
| sweep/4h/1h/none/k1/tp1.5/be/all | 34 | 38.2% | 1.314 | -1.667 | -0.527 | -7.14% | 10.42% | -0.875 | 9/0.439 | 20/-0.694 | 14/-0.289 | no | no |
| sweep/4h/1h/none/k1.5/tp2/overnight/all | 32 | 37.5% | 1.899 | -1.794 | -0.409 | -5.99% | 12.49% | -0.783 | 10/0.414 | 17/-0.606 | 15/-0.186 | no | no |

- `sweep/4h/1h/none/k1/tp1.5/overnight/all` exits: stop 19, tp 15. Per coin: BTC 9/-0.316R, ETH 3/-0.630R, SOL 1/-1.629R, xyz:GOLD 6/-0.351R, xyz:SP500 9/-0.679R, xyz:XYZ100 6/-1.222R.
- `touch/4h/1h/none/k1.5/tp2/overnight/all` exits: stop 18, tp 14. Per coin: BTC 7/0.476R, ETH 4/0.280R, SOL 1/-1.419R, xyz:GOLD 9/-0.145R, xyz:SP500 4/-1.464R, xyz:XYZ100 7/-0.340R.
- `sweep/4h/1h/none/k1/tp1.5/flat/all` exits: stop 10, tp 13, SESSION_CLOSE 11. Per coin: BTC 9/0.003R, ETH 3/-0.630R, SOL 1/-1.629R, xyz:GOLD 6/-0.521R, xyz:SP500 9/-0.800R, xyz:XYZ100 6/-0.684R.
- `sweep/4h/1h/none/k1/tp1.5/be/all` exits: stop 10, tp 13, SESSION_CLOSE 11. Per coin: BTC 9/0.003R, ETH 3/-0.630R, SOL 1/-1.629R, xyz:GOLD 6/-0.521R, xyz:SP500 9/-0.800R, xyz:XYZ100 6/-0.684R.
- `sweep/4h/1h/none/k1.5/tp2/overnight/all` exits: stop 20, tp 12. Per coin: BTC 9/0.065R, ETH 5/-0.052R, SOL 1/-1.419R, xyz:GOLD 6/0.095R, xyz:SP500 5/-1.669R, xyz:XYZ100 6/-0.703R.

## Walk-forward pick

`touch/4h/1h/none/k1.5/tp1.5/overnight/all` had tune expectancy -0.130 R on 24 trades. Holdout: 10 trades, 0.178 R. Full window: 34 trades, win 55.9%, avg win 1.392 R, avg loss -1.853 R, expectancy -0.040 R, net 0.54%, max DD 9.30%. Halves: 19/0.226 R and 15/-0.376 R. Raw bar: no. Penalty: no.

Exits: stop 15, tp 19. Per coin: BTC 8/0.723R, ETH 4/0.030R, SOL 1/-1.419R, xyz:GOLD 8/0.224R, xyz:SP500 6/-0.625R, xyz:XYZ100 7/-0.555R.

R is net dollars divided by size times the original stop distance. The stop is beyond the level, not beyond the sweep wick.

# Swing overnight replay

Paper research only. This is not the Monday default. Both books keep the 10x swing cap, one position, major 4h or prior-day/week levels, 1% risk, the 2% loss cap, and targets of 1R to 3R. Confirm is 1h. Macro is 4h and daily, both. Flow is off.

Window: last 90 days of the cached 1h book. R is net dollars divided by size times the original price-stop distance.

## (a) Hold overnight

No flatten at 16:00. Entries are still only 09:30–16:00 America/Toronto, Monday to Friday. The 3-day max hold still applies.

- Trades: 16
- Win rate: 31.2%
- Average winner R: 1.875
- Average loser R: -1.834
- Expectancy R: -0.675
- Net: -6.49%
- Max drawdown: 6.52%
- Average hold: 17.8h
- Exits: stop 11, tp 4, max_hold 1
- Span walked: 90.0 days

## (b) Hold only if +1R at 16:00

At 16:00, a close of at least +1R against the original stop moves the stop to entry and holds. Anything less flattens as SESSION_CLOSE. The next session close is checked again.

- Trades: 16
- Win rate: 43.8%
- Average winner R: 0.928
- Average loser R: -1.012
- Expectancy R: -0.163
- Net: -1.68%
- Max drawdown: 3.93%
- Average hold: 2.9h
- Exits: stop 4, tp 2, SESSION_CLOSE 10
- Span walked: 90.0 days

Neither book turns a flag on. Monday's paper stays on a 3-day hold, targets of 1R to 5R, and no session flatten.

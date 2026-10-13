# BTC playback, Oct 12 2026, 12:30 PM to 9 PM ET (15m bars)

Chart: `playback_2026-10-12.png` (also saved at /workspace/playback_2026-10-12.png). Full per-bar data: `2026-10-12_btc_playback.json`. Code: `src/hl_bot/research/playback.py`.

## Sources

- **Candles and levels:** HL 15m and 1h candles. PDL is 82,746 and PDH is 84,181, from the Oct 11 UTC day.
- **ADX:** 14-period on 1h and 4h. The macro read is the same 4h-led `read_swing_macro` call the MB Swing indicator uses.
- **Delta and CVD:** from the real HL trade tape (`/workspace/hl_tape`). CVD starts counting at 12:30 PM.
- **Tape gaps:** the 4:45, 6:45 and 8:30 PM bars have no tape (prints = 0). Their delta shows as "gap" and CVD is held flat across them.
- **Absorption:** the larger of buy and sell size over the smaller. A bar is tagged "absorbed" when delta points the opposite way from the candle.
- **Funding:** from HL `fundingHistory`, hourly.
- **Liquidation bands:** the indicator's method. Last 3 15m pivots (5 bars each side) × 25/50/100x leverage, shown only within 4% of price.
- **Open interest:** HL has no historical OI endpoint. Only the current value is available, so OI is not in the playback.

## Key bars

| ET bar | close | vs PDL | 1h ADX | 4h ADX | macro | delta (BTC) | CVD | vol vs avg | note |
|---|---|---|---|---|---|---|---|---|---|
| 12:30 | 82,759 | +2 bp | 25.9 down | 20.0 down | down | −23 | −23 | 0.34x | entry 12:36 at about 82,836 |
| 12:45 | 82,466 | −34 bp | 26.1 | 20.0 | down | −207 | −231 | 0.70x | sellers push off PDL |
| 15:00 | 82,365 | −46 bp | 27.1 | 20.0 | down | +132 | −143 | 1.13x | buyers start the squeeze |
| 15:45 | 82,833 | +11 bp | 26.0 | 19.4 | **range** | +48 | −71 | 1.28x | back above PDL. 4h ADX drops under 20 and the macro flips to range |
| 16:00 | 82,943 | +24 bp | 26.0 | 19.4 | range | **+222** | +151 | 2.13x | chase into the sweep |
| 16:15–16:30 | 83,053–83,096 | +37–42 bp | | | range | −13 / −8 | about +130 | about 1x | up bars with negative delta: buying stalls |
| 16:45 | 83,068 | +39 bp | | | | gap | | 2.79x | sweep high: 83,116 on HL 15m, about 83,153 on Chris's fill chart |
| **17:00** | 82,850 | +13 bp | 24.2 | | | **−675** | −545 | **4.99x** | bull trap confirmed. Clearest single bar of the day |
| 17:45 | 82,536 | −25 bp | 23.8 | | | −81 | −715 | 2.38x | the 5–6 PM 1h bar closes back under PDL |
| 18:45 | 82,120 | −76 bp | 24.1 | | | gap | | 2.20x | |
| 19:00 | 82,010 | −89 bp | | | | −18 | −928 | 4.28x | funding turns negative (−0.0002%) |
| 19:45→20:00 | 81,812 → 81,530 | −113 → −147 bp | 25.0 | 19.6 | range | +195 then **−468** | −1,261 | 0.86x then **3.33x** | 82k break, then the long liquidation flush to 81,253 (HL 15m low) through the 81,439–81,599 bands |
| 20:45 | 81,395 | −163 bp | 26.8 | | | −104 | −1,185 | 2.77x | low 81,000. TP fills at 81,006, 8:52 PM |

## Which readings helped and which were noise

**Helped early or clearly**

- **PDL itself.** Price respected it as resistance, and the final rejection came off a sweep only about 0.45% above it.
- **The size of a single bar's delta together with volume.** The 4 PM bar had +222 on 2.1x volume: late buyers chasing.
  The 5 PM bar then had −675 on 5x volume with price back near PDL. That was the clearest "trap is done" signal. It came about 1 hour before the 1h close confirmed it.
- **CVD direction after the trap.** It fell from +151 to −1,261 without one real reversal and never made a higher high. It confirmed the move the whole way down.
- **Volume spikes (above 2x).** Every real move (17:00, 19:00, 20:00, 20:45) came on 2–5x volume. The squeeze up came on about 1x volume.
- **Estimated long liquidation bands at 81,439–81,599.** The flush ran straight through them and bottomed just below. They worked as a magnet and target zone, and agreed with the 81,865 Coinglass cluster.

**Noise or late**

- **The 4h ADX and macro read.** 4h ADX sat right at 19.4–20.0 all session. The macro flipped from down to range at 3:45 PM, right as the short was about to work.
  A hard "macro must be down" rule (filter c) would have blocked a re-entry at the best moment.
- **1h ADX.** It stayed at 24–27 and down the whole time, so it never told us anything new.
- **The absorbed tag and the absorption ratio.** They fired on about a third of bars in both directions. The ratio jumped to 19–24 on thin bars (17:15, 17:45). Not usable on its own.
- **Funding.** Tiny and positive most of the day, and only turned negative at 7 PM. That is after the move, so it lags.
- **Delta on low-volume bars.** The ±20–80 swings during the 1:00–2:30 PM grind meant nothing.

## For the bot

- The two useful triggers would be:
  - A **bar delta and volume spike against the sweep** (for a short: delta at or below −3× the recent average absolute delta, volume at least 3x, close back toward PDL).
  - **Liquidation bands as targets.**
- Neither has been tested over 90 days. This is one trade, and it is a hypothesis to test, not a rule.

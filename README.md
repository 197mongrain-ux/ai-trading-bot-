# hl-bot — Hyperliquid Multi-Symbol SCALP Bot

Paper-first bot for **BTC, SOL, and XRP perpetuals** on [Hyperliquid](https://hyperliquid.xyz). Default mode is **PAPER** (simulated fills at mark/mid). **LIVE** trading is gated and disabled unless you explicitly opt in.

> **No profit guarantee.** This is educational / experimental software. Perpetual futures are high risk. You can lose more than you deposit. Past or backtested behavior does not predict future results.

## Features

- **Multi-symbol + stacking** — trade `BTC`, `SOL`, and `XRP` with independent paper/live positions; multiple opens per ticker allowed (each with its own stop/TP/`trade_id`)
- **Strategy — VWAP bias + micro breakout scalp + optional OTE pullback** (same rules on every symbol)
  - Session VWAP resets at UTC 00:00 (configurable via `VWAP_RESET_UTC_HOUR`) — **bias only**
  - Long only if mark > VWAP (+ optional `VWAP_BUFFER_BPS`); short only if mark < VWAP (− buffer)
  - Entry on **1m bars**: break of prior **N**-bar high (long) / low (short). Default `BREAKOUT_BARS=3`
  - **Fixed percent stop from entry** — default **0.15%** (`STOP_PCT=0.0015`). **Not** placed at VWAP
  - Take-profit at an R-multiple of that tight stop (default **2.0**, configurable **1–3**)
  - **Accuracy filters** (env-configurable; gate entries in `strategy.on_bar` before open):
    1. **Volatility / noise** — skip if avg 1m range ≥ `MAX_RANGE_VS_STOP` × `STOP_PCT` (`vol_too_high`) or last bar > `MAX_BAR_RANGE_PCT` (`bar_too_wide`)
    2. **Session (UTC)** — only enter when hour ∈ `TRADE_HOURS_UTC` (default `12-23` = 12:00–22:59 UTC; `0-24` / empty disables). Outside → `outside_session`
    3. **HTF VWAP confirm** — long only if mark > 5m session VWAP; short only if below (`HTF_CONFIRM`, `HTF_INTERVAL`). Fail → `htf_vwap_block`
    - Bonus: `ENTRY_COOLDOWN_SEC` (default 120) after a stop-out on the same symbol
- Stub `strategy/ai_signal.py` (unused by default)
- Hard risk limits shared across symbols (daily loss, drawdown kill switch, consecutive-loss pause)
- Position size from **dollar risk ÷ stop distance** (leverage is exchange margin / notional ceiling only)
- **Sell into strength (scale-out)** — at `SCALE_OUT_R` (default 1.0R) close `SCALE_OUT_PCT` (default 50%) of each leg, move stop to breakeven ± `BE_BUFFER_BPS`, leave runner to original TP (or optional `RUNNER_TP_R`). Shorts mirror. Paper-first; LIVE is best-effort reduce-only under exchange netting.
- JSONL trade journal
- **Live local dashboard** — TradingView + tape + positions (`python -m hl_bot dashboard`)

### Scalp vs old wide VWAP stops

Older versions placed the stop just beyond session VWAP. When price was far from VWAP that made stops **very wide**, so position size shrank and R-multiples were unrealistic for scalps.

This version keeps VWAP for **direction bias only**. The stop is a **tight fixed %** from entry (default 0.15%). That is the intended scalp behavior for liquid BTC/SOL/XRP perps.

**Warning on `LEVERAGE=20`:** High leverage is for **margin efficiency** with tiny stops — it does **not** mean “risk 20× equity.” Dollar risk per trade is still `RISK_PER_TRADE` (default 0.5%). 20× only lets the exchange hold the notional that risk-based sizing already chose without over-margining.


### OTE add-on (Optimal Trade Entry pullback)

ICT-lite pullback entry alongside the 1m breakout scalp. **OTE ≈ the 62%–79% Fibonacci retracement** of the most recent impulse swing, taken **with** VWAP bias (never counter-trend).

**Long** (only when VWAP long bias already passes):
1. Impulse = swing low → swing high over `OTE_LOOKBACK_BARS` (default 45 of 1m; set `OTE_USE_HTF_SWINGS=true` to use `HTF_INTERVAL` bars for swings).
2. Zone from high: `zone_high = high − 0.62×(high−low)`, `zone_low = high − 0.79×(high−low)`.
3. Enter when mark is **inside** the zone (optional: `OTE_REQUIRE_CLOSE=true` also needs a bullish 1m close).
4. Stop: prefer `zone_low − buffer`, but **clamp max risk to `STOP_PCT`**. If the swing/zone stop is wider than `STOP_PCT` from entry, use `STOP_PCT` so R stays scalp-sized. (`stop = max(entry×(1−STOP_PCT), zone_low − buffer)` for longs.)
5. TP: `TP_R_MULTIPLE` × that stop distance (unchanged).

**Short:** mirror (VWAP short bias, swing high→low impulse, OTE zone on the retrace up).

**`ENTRY_MODE`** = `breakout` | `ote` | `both` | `model_b` (default **`both`**):
- `breakout` — existing N-bar micro breakout only.
- `ote` — OTE pullback only (reasons: `ote_long` / `ote_short` / `ote_no_swing` / `ote_outside_zone`).
- `both` — **prefer OTE when mark is in the zone**; otherwise try breakout. Session / vol / HTF / cooldown filters still apply before either path.

**Example zone math:** impulse low 100 → high 110 (range 10).  
Long OTE zone = `[110 − 0.79×10, 110 − 0.62×10]` = **`[102.1, 103.8]`**.  
Mark 103.0 inside → `ote_long`. With `STOP_PCT=0.0015`, pct stop ≈ 102.845; zone stop at 102.1 is wider → use pct stop 102.845 so risk stays 0.15%.

Env knobs: `ENTRY_MODE`, `OTE_LOOKBACK_BARS`, `OTE_FIB_SHALLOW`, `OTE_FIB_DEEP`, `OTE_STOP_BUFFER_BPS`, `OTE_REQUIRE_CLOSE`, `OTE_USE_HTF_SWINGS`. Journal `open` events log `entry_mode` + `reason`.

### Model B (testnet hunt this week)

`ENTRY_MODE=model_b` is a **separate** entry mode. It does not run inside the VWAP breakout/OTE path, and it does not sit on top of a Model 3 score door. A 7/9 or 9/9 score is written to the journal and then ignored. A low score does not block. `HEAVY` / `VOL_OK` is a tag only. There is no `FLOW_OK` pre-place gate.

```bash
# Desk start — testnet live. Paper is for unit tests only.
TRADING_MODE=live
HL_NETWORK=testnet
ENTRY_MODE=model_b
LEVERAGE=20
RISK_PER_TRADE=0.02   # max 2% of spot USDC (not perp account value)
I_UNDERSTAND_LIVE_TRADING=true
# HL_PRIVATE_KEY=0x...   # API wallet
# MODEL_B_TP_R=1.5       # clamped to [1, 2], then capped at the untaken pool
```

```bash
python -m hl_bot run
```

Swing is a separate style of that same hunt. `MODEL_B_STYLE=scalp` (the default) does not change the book above. Swing stays in paper until `MODEL_B_PAPER=0`.

```bash
# Laptop paper. Live mainnet prices and trades, simulated fills, own journal.
# No real orders. Going live later is MODEL_B_PAPER=0 plus the live gate.
TRADING_MODE=paper
HL_NETWORK=mainnet
ENTRY_MODE=model_b
MODEL_B_STYLE=swing
MODEL_B_PAPER=1
LEVERAGE=20
RISK_PER_TRADE=0.01
python -m hl_bot run
```

The swing journal is `logs/model_b_swing_paper.jsonl` (not `logs/trades.jsonl`). Coins default to BTC, ETH, SOL, xyz:GOLD, xyz:SP500, xyz:XYZ100.

NY hours and after hours hunt the same list. With `ENTRY_MODE=model_b`, a set `SYMBOLS` (or `SYMBOL`) is that list, dex prefix left lowercase (`xyz:GOLD`, not `XYZ:GOLD`). When `SYMBOLS` is unset, the hunt is the built-in mainnet universe — not the breakout default `BTC, SOL, XRP`:

BTC, ETH, SOL, NEAR, PUMP, LIT, AAVE, ONDO, WLD, TAO, xyz:GOLD, xyz:SP500, xyz:XYZ100

Majors stay first. `xyz:XYZ100` is the Nasdaq proxy. `xyz:SP500` is the S&P proxy. The clock does not drop names overnight or on weekends.

What it does:

- **Bias** is the nearest untaken pool (PDH / PDL / WKH / WKL). Pool above price drops shorts; pool below drops longs. No pool, or a tie, is **NONE and both sides are allowed**. Bias never arms. The pool on the trade's side is a liquidity target for the take-profit, not the entry.
- **Data** is aggressor trade prints `{ts, coin, price, size, side}` from the Hyperliquid trades websocket (`B` = buy aggressor, `A` = sell aggressor). A print with no side fails closed as `NO_SIDE`. Book depth is not an entry input. Best bid/ask is used only to keep the Alo from crossing. The socket sends `{"method":"ping"}` every 20s and resubscribes after a disconnect (`Expired` included). The print buffer is kept across that reconnect. `BTC-PERP` is stored as `BTC`.
- **Candles** for bias pools are cached per coin for 60s. HTTP 429 backs off (5s, then double, cap 60s) and reuses the last good snapshot. The hunt does not refetch `candleSnapshot` for every name on every pass.
- **Arm** on the latest confirmed 1-minute swing opposite the bias (long = swing low, short = swing high), ignoring a swing closer than 3 ticks to the last trade. The 90s window needs at least `MODEL_B_MIN_PRINTS` prints (`THIN_TAPE` otherwise). Mainnet stays at **30**. On testnet the unset default is **3**, the same density as 30 prints against a much thicker mainnet tape: a 2026-10-05 NY sample was **252 BTC prints/min** on mainnet (84 prints in 20s) and the desk probe was **~7 BTC prints/min** on testnet (~10.5 in 90s). `30 * (7/252)` rounds to 1, and the auto floor is `max(3, that)`. Set `MODEL_B_MIN_PRINTS` to override. The print floor does not change absorb or sweep. Long: a print at least 1 tick through the swing low, last trade back above it, absorb (sell size sweep→reclaim / buy size reclaim→now) ≥ 1.3 (`ABSORB_MIN`). Window delta and last-15s delta are coin size (buy size minus sell size, the logged `dW` / `d15`). The flat band in coins is the larger of `MODEL_B_DELTA_FLAT_USDC` / mid (default **$100**; the last trade when the book is one-sided) and `MODEL_B_DELTA_FLAT_EPS` (default **0.05** coins). Long passes when each delta is at or above minus that coin size. Short passes at or below plus it. At $150 the $100 term is about 0.67 SOL and wins. At $86,000 the 0.05-coin floor is about $4,300 and wins, so a BTC short with `dW` about +0.006 still passes. A short SOL `dW` of +1.1 (~$165 at $150) still fails. Set either value to 0 to drop that term. Both at 0 restore the strict sign check.
- **Order** is a post-only Alo immediately, never a market. A live Alo the exchange does not rest is not kept as a ticket: insufficient margin is `MARGIN_REJECT`, and any other never-rested response or send exception is `SEND_FAIL`. Nothing is posted into the book, so that coin does not reserve margin or block another ticker. The swing is not consumed, so a later pass can arm once the margin is actually free. Long rests at the swept low, or the best bid if that low would cross. Short mirrors. It rests until the thesis is stale: a later print through the sweep extreme that does not fill the order cancels it and ends that swing. There is no 20s maker timeout. `MODEL_B_ALO_TIMEOUT_SEC=0` (default) keeps the timer off; a positive value is an optional clock cancel. One thesis per coin: no average-down, no second Alo, no re-entry after a stale cancel or stop on that swing. Another coin can rest at the same time when the sizing balance still covers that ticket's initial margin. The balance is the same one the sizer uses: live spot USDC `total`, or paper equity. Margin is notional divided by that coin's max leverage (20× when the exchange max is unknown). The Alo limit is used for a resting order and the fill for an open position. The stop, the target, and the 2% size do not change with that leverage. The new ticket keeps its full 2% size; it is not cut down to fit. `MODEL_B MARGIN … dual_rest` is that place, and the journal event is `model_b_margin` with `action=dual_rest`. When the remainder cannot fund that full size, a new coin takes the slot only by cancelling a strictly farther unfilled Alo (closer to the mid, else the last trade, in bps) whose arm score is not strictly higher. `MODEL_B_CLOSER_SCORE_GUARD` defaults on. Resting score 9 is not cancelled for a closer score 4: the fail is `CLOSER_SKIP_LOWER_SCORE` and the line names both coins and both scores. Equal scores still use that closer-bps rule, so a strictly closer limit still swaps. Set the flag to `0` to cancel on bps alone. The log is `MODEL_B MARGIN … closer_cancel`, the cancel reason is `closer_ticker`, and the log names the coin that won (`margin_for_better`) plus both scores. A bps tie keeps the resting order. The coin that loses the slot is not posted (`NOT_CLOSER`). A coin that already has a fill is not cancelled; its stop and TP stay. `MODEL_B_CLOSE_MARGIN_RESERVE` (default **0.60**, `0` off) keeps that fraction for a resting unfilled Alo, not for a swing that has not armed. The preferred resting coin is the highest arm score, then the closer limit in bps. An equal or lower score does not take it over when only the distance moves, so the reserve does not flip between BTC, ETH, and SOL every few seconds. A strictly higher score takes it immediately. A setup that cleared every gate is never failed `CLOSE_MARGIN_RESERVE` to save margin for a coin that is not resting. A lower-or-equal score that would spend the resting ticket's reserve still fails `CLOSE_MARGIN_RESERVE`. The higher-score closer guard is unchanged. Free margin is spot USDC minus margin already held by open positions and resting entries, including `xyz` builder-dex positions. The 2% ticket is still sized off spot USDC `total`. The desk logs `MODEL_B MARGIN CLOSE_MARGIN_RESERVE engage` with `protected=resting`, plus `release` and `hold`. `MODEL_B MARGIN free` names spot, held, and free when held margin changes. `MODEL_B ADOPT` records a position or resting entry found on the exchange after a restart. `MODEL_B BLOCK` is `OPEN_POSITION` or `RESTING_ENTRY` and nothing is sent. `MODEL_B RECONCILE close` is a journal close built from exchange fills when the websocket missed it. This does not change the flat-delta floor.
- **Risk** is `RISK_PER_TRADE` (Model B requires **0.02**) times the **spot USDC balance**, divided by the stop distance. That is a max of 2% of spot USDC. Live reads `spotClearinghouseState` (USDC `total`). It does not size off perp `accountValue`, which can be a thin remainder while the USDC sits in spot. Whether a new ticket fits uses that same total minus perp margin already held (`clearinghouseState` on the default dex and on each builder dex, so an open `xyz:XYZ100` is not invisible). A missing or zero spot balance fails closed (`NO_SPOT_USDC`) and does not fall back to `STARTING_EQUITY`. Paper tests pass the balance in directly. Notional is capped at that same leverage times the balance, which can only shrink a 2% ticket. Set `HL_ACCOUNT_ADDRESS` to the master account that holds the spot USDC.
- **Stop** is placed past opposing liquidity, and only then is the Alo sized. A wick, swing, or last-3-bar extreme more than one tick past the fill owns the stop. The buffer past that print is `max(3 ticks, 2 bps, 0.5×ATR14)`. ATR14 is used only when 15 closed bars exist. A print inside the old 0.15% / 10-tick / fee room is kept and is not lifted onto that floor: the Oct 5 ETH long at **2705.8** with a 1m low at **2704** rests near **2703.4**, not at **2701.7**. When the anchor is the fill itself, that few-tick buffer is still inside wick room (the Oct 6 BTC short at **85658** was stopped **18** points away at **85676**). The stop then goes past the nearest opposing print that already sits outside the room — every closed bar extreme on the stop side, a confirmed swing, or an untaken pool — or, with no such print, past the room by the 3-tick / 2 bp pad. The no-print ETH case clears the room near **2701.2**, beyond the old **2701.7** floor snap, and is not parked on it. A 0.5×ATR buffer that already clears the room is kept. A deeper wick is never pulled back. Distance over **1.5%** of price arms at a smaller size (`size_adjust=wide_stop`). The stop is not tightened into the wick to fit 1.5%. `BAD_STOP` remains only when the stop is on the wrong side of the fill, equal to the fill, or one tick off it (the LIT collision). A size that rounds to zero fails closed the same way. Size is still 2% of spot USDC divided by the distance that results.
- **Volume profile is a log tag only.** `vp_as_filter`, `vp_entries`, and `vp_enabled` stay off. Each evaluation writes `vp_poc`, `vp_vah`, `vp_val`, `nearest_lvn_on_side`, `sweep_to_val_bps`, `sweep_to_lvn_bps`, `vp_tag` (`val`, `lvn`, `val+lvn`, `none`, or `vp_error`), and `catalyst_flag` on the arm/fail journal row. A touch is 12 bps. Too few session bars leave the numbers null and `vp_tag=none`. The tags do not arm, block, move the Alo, or widen the stop. The live laptop still needs this port; it is not applied there yet.
- **TP1** is the next liquidity in the trade direction that clears the round-trip fee and at least 1R of the stop just placed: the nearest confirmed swing (long: swing high, short: swing low) or untaken pool, more than one tick past the entry, and at least `max(1 × stop distance, maker+taker fee)` away. A closer pool is skipped. The Oct 6 short at **85818** with a ~147 point stop does not take the **85814** print (about 0.03R); it advances to the next real pool, such as PDL **85273**. A level past 2R is still that pool and is not pulled back to 2R. PDL **85500** on the **85658** short is inside 1R of the 180-point stop, so it is skipped too. If no level clears the band, the target falls back to 1.5R of that stop (clamped to **[1, 2]** via `MODEL_B_TP_R`). `BAD_TP` is that fallback when it still cannot clear the band, or a target that is not strictly beyond the entry. A later drip resizes the stop and TP to the filled size and does not move a chosen pool back to 2R. The hunt owns both brackets in this process. An amend that fails is logged and does not exit the loop. The fail logs `r` (entry→stop), `pool_dist`, `pool_r`, and `why` (`pool_too_close`, `under_1r`, `over_2r` only if the rejected target itself is past 2R, or `fees`). An absorb ratio with no reclaim-side size is infinite and still clears 1.3; the journal writes null, not `1000000`. A heal cannot replace a wider stop with a tighter one. On a live fill the arm stop is kept unless a recompute is wider. A fill-only buffer is not substituted when it would tighten the stop or sit on the fill. TP is recomputed from that stop, and the stop/TP size is the **filled** size (a margin trim or partial fill does not bracket the requested size). A partial fill keeps the unfilled maker resting. Further drips grow the position and the brackets are resized to the filled size. The remainder's margin is released when the position closes or goes flat (`MODEL_B MARGIN … release`), including a bracket fill that prints between the stop and the target. It is not left reserved until the thesis is stale. While the position is still open, the remainder is cancelled when that thesis is stale (`MODEL_B PARTIAL … remainder cancelled reason=thesis_stale`, position kept) or another existing cancel fires. A closer coin does not take the slot once any size on that coin has filled. Soft-prop, strategy kill, and flow exits are off.

Every arm and fail is journaled (`model_b_arm` / `model_b_fail`) with coin, bias, pool, swing, sweep price, absorb, window delta, last-15s delta, score, volume tag, `size_adjust` (`wide_stop` when the armed stop is wider than 1.5% of entry, otherwise null), `delta_flat` (`window`, `last_15s`, or `both` when the flat band passed a delta the strict sign check would have failed, otherwise null), `delta_flat_eps` (the coin size compared: the larger of the USDC term and the coin floor), `delta_flat_usdc_eps`, `delta_flat_coin_eps`, and `delta_flat_px` (the price that scaled the USDC term), and on `BAD_TP` also `r_distance`, `pool_distance`, `pool_r`, and `bad_tp_why`. One fail reason: `NO_SIDE`, `THIN_TAPE`, `NO_SWEEP`, `NO_RECLAIM`, `ABSORB`, `DELTA`, `LAST_15s` (plus `NO_SWING`, `OUT_OF_SESSION`, `THESIS_DONE`, `SECOND_ALO`, `AVERAGE_DOWN` when the hunt never reaches the tape). A `THIN_TAPE` line also shows `prints=N/M` for the 90s window and the floor in force (`M` is 30 on mainnet, 3 on testnet unless overridden).

The operator kill switch still flattens. That is account safety, not a score kill. Unit tests stay offline; they do not need a testnet session.


## Watching on TradingView

Useful Hyperliquid USDC perp symbols:

| Coin | TradingView |
|------|-------------|
| BTC  | `HYPERLIQUID:BTCUSDC.P` |
| SOL  | `HYPERLIQUID:SOLUSDC.P` |
| XRP  | `HYPERLIQUID:XRPUSDC.P` |

## Safety checklist (read before LIVE)

1. Start in **PAPER** and verify journal / sizing with your intended equity.
2. Use an **API wallet (agent)**, not your main cold wallet. Create one at  
   https://app.hyperliquid.xyz/API
3. Set tight risk: default **0.5%** risk per trade (documented range **0.25–0.5%**).
4. Confirm `KILL_SWITCH=1` or a `.killswitch` file flattens and blocks new entries.
5. Never commit `.env` or private keys.
6. Set both `TRADING_MODE=live` **and** `I_UNDERSTAND_LIVE_TRADING=true`.
7. Understand liquidation / funding on perps before sizing up.

## Install

Requires **Python 3.11+**.

```bash
cd ai-trading-bot
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

Copy env template:

```bash
cp .env.example .env
# edit .env — leave TRADING_MODE=paper
```

## Symbols (`.env`)

```bash
# Preferred: comma-separated Hyperliquid coin names
SYMBOLS=BTC,SOL,XRP

# Fallback (single symbol) if SYMBOLS is unset:
# SYMBOL=BTC

# Stacking: max independent opens per ticker (default 3; 0 = unlimited)
MAX_POSITIONS_PER_SYMBOL=3

# Optional global cap across all trade_ids (default 0 = unlimited)
MAX_OPEN_POSITIONS=0
```

- If `SYMBOLS` is set → that list is used.
- Else if `SYMBOL` is set → single-symbol list `[SYMBOL]` (backward compatible).
- Else → default `BTC,SOL,XRP`.
- **Stacking:** the bot may open multiple positions on the same ticker when new entry signals fire, up to `MAX_POSITIONS_PER_SYMBOL`. Each open keeps its own `trade_id`, stop, TP, and size. `MAX_OPEN_POSITIONS` (0 = unlimited) optionally caps total opens across all symbols.

Hyperliquid Info `allMids` keys are bare names (`BTC`, `SOL`, `XRP`) for these perps.

## Paper mode (default)

```bash
python -m hl_bot run
# or limited smoke loop:
python -m hl_bot run --max-iterations 3
```

PAPER mode **never** calls `Exchange.order`. It uses `PaperBroker` fills at mark/mid from the Info API (or injected bars in tests). Each loop iteration fetches mark + candles and runs the scalp independently per configured symbol. **Multiple positions per symbol are allowed** (stacking) up to `MAX_POSITIONS_PER_SYMBOL`; the entry loop does not skip a ticker merely because it already has an open — only when that ticker is at the per-symbol cap. Each position has its own stop/TP and is closed independently.

### Example defaults (~$5k equity)

| Setting | Default | Notes |
|--------|---------|--------|
| `SYMBOLS` | `BTC,SOL,XRP` | Hyperliquid perp coins |
| `MAX_POSITIONS_PER_SYMBOL` | `3` | Max stacked opens per ticker (`0` = unlimited) |
| `MAX_OPEN_POSITIONS` | `0` | Optional global cap (`0` = unlimited) |
| `STARTING_EQUITY` | `5000` | Paper account equity |
| `RISK_PER_TRADE` | `0.005` | 0.5% → $25 risk on $5k |
| `STOP_PCT` | `0.0015` | 0.15% stop from entry |
| `BREAKOUT_BARS` | `3` | Prior N 1m bars for breakout |
| `VWAP_BUFFER_BPS` | `0` | Bias-only (optional e.g. 2) |
| `LEVERAGE` | `20` | Margin / notional ceiling — not sizing |
| `TP_R_MULTIPLE` | `2.0` | TP at 2R from stop distance |
| `MAX_DAILY_LOSS_PCT` | `0.03` | Flatten + halt until next UTC day |
| `MAX_DRAWDOWN_PCT` | `0.08` | Kill switch from high-water mark |
| `MAX_TRADES_PER_DAY` | `0` | **0 = unlimited** count; set e.g. 500 to cap |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Pause new entries |
| `MAX_RANGE_VS_STOP` | `1.0` | Skip if avg bar range ≥ this × `STOP_PCT` |
| `VOL_LOOKBACK_BARS` | `5` | Bars for avg range |
| `MAX_BAR_RANGE_PCT` | `0.003` | Skip if last bar wider than 0.3% (empty = off) |
| `TRADE_HOURS_UTC` | `12-23` | UTC entry window, start incl. / end excl. (`0-24` = off) |
| `HTF_CONFIRM` | `true` | Require 5m VWAP bias alignment |
| `HTF_INTERVAL` | `5m` | HTF candle interval |
| `ENTRY_COOLDOWN_SEC` | `120` | Block re-entry after stop-out (same symbol) |
| `ENTRY_MODE` | `both` | `breakout` \| `ote` \| `both` \| `model_b` |
| `OTE_LOOKBACK_BARS` | `45` | Impulse swing lookback (1m or HTF) |
| `SCALE_OUT_ENABLED` | `true` | Sell into strength at `SCALE_OUT_R` |
| `SCALE_OUT_R` | `1.0` | Unrealized R to scale out |
| `SCALE_OUT_PCT` | `0.5` | Fraction of size to close (50%) |
| `BE_BUFFER_BPS` | `2` | BE stop buffer (favorable direction) |
| `RUNNER_TP_R` | _(unset)_ | Optional retarget of remainder TP |

**SL/TP example (BTC @ 86 000):**  
stop = `86000 × (1 − 0.0015)` = **85 871** (−0.15%).  
TP at 2R = `86000 + 2 × 129` = **86 258** (+0.30%).

**Sizing example:** equity $5 000, risk 0.5% → $25. Entry 86 000, stop 85 871 → distance $129 → size ≈ **0.194 BTC**. Leverage does not increase that size; it only caps max notional at `equity × leverage` and sets exchange margin.

## LIVE mode (gated)

```bash
# .env
TRADING_MODE=live
I_UNDERSTAND_LIVE_TRADING=true
HL_PRIVATE_KEY=0x...          # API wallet key
HL_ACCOUNT_ADDRESS=0x...      # optional master address if using agent
HL_NETWORK=mainnet            # or testnet
SYMBOLS=BTC,SOL,XRP
LEVERAGE=20
```

Without `I_UNDERSTAND_LIVE_TRADING=true`, LIVE will refuse to start. Open/close/stop orders are placed **per symbol** the same way as paper. `update_leverage(20, coin)` is called before market open.

**LIVE stacking limitation:** Hyperliquid typically uses one-way / netted positions per coin. Paper keeps fully independent legs (separate stops/trade_ids). On LIVE the bot still `market_open`s additional size and places per-fill stops / size-reduced closes as **best-effort** — the exchange may merge same-side exposure. Prefer paper for true multi-leg simulation.

### Hyperliquid API wallet notes

- Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- API wallet UI: https://app.hyperliquid.xyz/API
- Prefer an **agent / API wallet** with limited authority over exporting your main key.
- Test on **testnet** (`HL_NETWORK=testnet`) before mainnet.

## Risk rules (hard enforced)

- Shared across all symbols (one RiskManager)
- Max daily loss **3%** of day-start equity → flatten all + no new entries until next UTC day
- Max drawdown **8%** from high-water mark → kill switch
- Risk per trade default **0.5%** (range **0.25–0.5%**)
- **Daily trade count:** `MAX_TRADES_PER_DAY=0` means **no count limit** (default). Activity is still limited by entry rules, daily loss, DD, open-position caps, and consecutive-loss pause. Set a positive number (e.g. `500`) if you want a hard cap.
- Pause after **3** consecutive losses
- **Per-symbol stack cap** = `MAX_POSITIONS_PER_SYMBOL` (default **3**; `0` = unlimited per ticker)
- **Global open cap** = `MAX_OPEN_POSITIONS` (default **0** = unlimited across all symbols)
- Each stacked entry still passes `allow_entry` with dollar risk; `open_position_count` is total opens across all
- Stop **required** (fixed % from entry)
- Kill switch: env `KILL_SWITCH=1` or file `.killswitch`
- **Daily loss halt is in-memory:** restarting `python -m hl_bot run` clears it (new `RiskManager`). Optional `RESET_DAILY_RISK=1` journals a `risk_reset` event at start. UTC day boundary also clears via `maybe_roll_day`.

### Stop placement (scalp)

Stop = entry × (1 ± `STOP_PCT`). VWAP is **not** used for stop placement.  
TP = entry ± `TP_R_MULTIPLE ×` stop distance.

## Live dashboard (local command center)

Axiom-style dark UI: TradingView chart (BTC / SOL / XRP Hyperliquid perps), live trade tape from the JSONL journal, open positions with optional mark uPnL, day/session stats, and a bot status strip.

```bash
# Terminal A — paper bot (writes logs/trades.jsonl)
python -m hl_bot run

# Terminal B — dashboard on http://127.0.0.1:8787/
python -m hl_bot dashboard
# equivalent:
python -m dashboard
```

- URL: **http://127.0.0.1:8787/**
- API: `GET /api/state` (polled every 2s by the page)
- Journal: `JOURNAL_PATH` env (default `logs/trades.jsonl`, relative to the process cwd / project root)
- Clear **PAPER** badge unless the latest journal `start` event has `mode=LIVE`
- No secrets in the frontend; tape works offline even if TradingView needs network
- Optional mark prices via Hyperliquid `allMids` (server-side `requests`; fails soft if offline)

## Tests

```bash
pytest -q
```

All unit tests are offline (no network). Inject bars for breakout cases in strategy tests.

## Project layout

```
src/hl_bot/
  config.py              # env-based settings (SYMBOLS / SYMBOL / STOP_PCT / …)
  journal.py             # JSONL trade log
  __main__.py            # python -m hl_bot run | dashboard
  exchange/
    info_client.py       # mark/mid + candles wrapper
    paper_broker.py      # multi-symbol simulated fills at mark
    live_exchange.py     # LIVE only (update_leverage + orders)
  risk/manager.py        # sizing + hard limits (shared)
  strategy/
    base.py              # Strategy protocol
    vwap.py              # VWAP bias + micro breakout / OTE + filters
    ote.py               # OTE Fib zone helpers (impulse / zone / stop)
    filters.py           # vol / session / HTF / cooldown helpers
    model_b/             # ENTRY_MODE=model_b sweep/reclaim Alo (score is log-only)
    ai_signal.py         # stub (unused)
  exchange/hl_trades.py  # aggressor trade-print feed (fail closed without side)
  execution/loop.py      # main loop (per-symbol); model_b dispatches out
  execution/model_b_loop.py
src/dashboard/
  state.py               # journal → open positions / stats / status
  marks.py               # optional Hyperliquid allMids
  app.py                 # FastAPI: GET / + GET /api/state
  static/                # dark single-page UI
  __main__.py            # python -m dashboard
tests/
```

## Caveats

- **Mark/mid language:** prices come from Hyperliquid Info (`allMids` / `markPx`), not spot.
- **Candle / VWAP data:** session VWAP uses Info `candleSnapshot` when available. The SDK/API surface can vary; if candles are empty, VWAP cannot signal until bars exist. Paper/tests can `inject_bars` on `InfoClient` (optionally per coin).
- LIVE order helpers depend on `hyperliquid-python-sdk` versions; always verify on testnet.
- Equity (paper) = cash + sum of mark-to-market on **all** open positions (every `trade_id`).
- Paper positions are a dict keyed by `trade_id`; the same symbol may appear multiple times with independent stops/TP.
- Scale-out is paper-authoritative; LIVE reduce-only partials are best-effort under exchange netting.

## License / disclaimer

Provided as-is with no warranty. Use at your own risk. Not financial advice.

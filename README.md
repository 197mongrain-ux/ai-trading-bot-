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

**`ENTRY_MODE`** = `breakout` | `ote` | `both` (default **`both`**):
- `breakout` — existing N-bar micro breakout only.
- `ote` — OTE pullback only (reasons: `ote_long` / `ote_short` / `ote_no_swing` / `ote_outside_zone`).
- `both` — **prefer OTE when mark is in the zone**; otherwise try breakout. Session / vol / HTF / cooldown filters still apply before either path.

**Example zone math:** impulse low 100 → high 110 (range 10).  
Long OTE zone = `[110 − 0.79×10, 110 − 0.62×10]` = **`[102.1, 103.8]`**.  
Mark 103.0 inside → `ote_long`. With `STOP_PCT=0.0015`, pct stop ≈ 102.845; zone stop at 102.1 is wider → use pct stop 102.845 so risk stays 0.15%.

Env knobs: `ENTRY_MODE`, `OTE_LOOKBACK_BARS`, `OTE_FIB_SHALLOW`, `OTE_FIB_DEEP`, `OTE_STOP_BUFFER_BPS`, `OTE_REQUIRE_CLOSE`, `OTE_USE_HTF_SWINGS`. Journal `open` events log `entry_mode` + `reason`.


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
| `ENTRY_MODE` | `both` | `breakout` \| `ote` \| `both` (prefer OTE in zone) |
| `OTE_LOOKBACK_BARS` | `45` | Impulse swing lookback (1m or HTF) |

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
    ai_signal.py         # stub (unused)
  execution/loop.py      # main loop (per-symbol)
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

## License / disclaimer

Provided as-is with no warranty. Use at your own risk. Not financial advice.

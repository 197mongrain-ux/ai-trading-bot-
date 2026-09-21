# hl-bot — Hyperliquid Multi-Symbol Perpetual Trading Bot

Paper-first bot for **BTC, SOL, and XRP perpetuals** on [Hyperliquid](https://hyperliquid.xyz). Default mode is **PAPER** (simulated fills at mark/mid). **LIVE** trading is gated and disabled unless you explicitly opt in.

> **No profit guarantee.** This is educational / experimental software. Perpetual futures are high risk. You can lose more than you deposit. Past or backtested behavior does not predict future results.

## Features

- **Multi-symbol** — trade `BTC`, `SOL`, and `XRP` (Hyperliquid bare coin names) with independent paper/live positions
- **Strategy v1 — VWAP trend scalp** (same rules on every symbol)
  - Session VWAP resets at UTC 00:00 (configurable via `VWAP_RESET_UTC_HOUR`)
  - Long when mark/mid is above VWAP + small buffer; short when below
  - **Stop just beyond VWAP** (slightly on the far side of VWAP from entry, plus `STOP_BUFFER_BPS`)
  - Take-profit at an R-multiple of risk (default **2.0**, configurable **1–3**)
- Stub `strategy/ai_signal.py` (unused by default)
- Hard risk limits shared across symbols (daily loss, drawdown kill switch, trade caps, consecutive-loss pause)
- Position size from **dollar risk ÷ stop distance** (leverage is a notional ceiling only)
- JSONL trade journal

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

# Cap concurrent opens across all symbols (default = number of symbols)
MAX_OPEN_POSITIONS=3
```

- If `SYMBOLS` is set → that list is used.
- Else if `SYMBOL` is set → single-symbol list `[SYMBOL]` (backward compatible).
- Else → default `BTC,SOL,XRP`.

Hyperliquid Info `allMids` keys are bare names (`BTC`, `SOL`, `XRP`) for these perps.

## Paper mode (default)

```bash
python -m hl_bot run
# or limited smoke loop:
python -m hl_bot run --max-iterations 3
```

PAPER mode **never** calls `Exchange.order`. It uses `PaperBroker` fills at mark/mid from the Info API (or injected bars in tests). Each loop iteration fetches mark + candles and runs VWAP independently per configured symbol.

### Example defaults (~$5k equity)

| Setting | Default | Notes |
|--------|---------|--------|
| `SYMBOLS` | `BTC,SOL,XRP` | Hyperliquid perp coins |
| `MAX_OPEN_POSITIONS` | `3` | One per symbol by default |
| `STARTING_EQUITY` | `5000` | Paper account equity |
| `RISK_PER_TRADE` | `0.005` | 0.5% → $25 risk on $5k |
| `LEVERAGE` | `5` | Cap only; size from stop distance |
| `TP_R_MULTIPLE` | `2.0` | TP at 2R |
| `MAX_DAILY_LOSS_PCT` | `0.03` | Flatten + halt until next UTC day |
| `MAX_DRAWDOWN_PCT` | `0.08` | Kill switch from high-water mark |
| `MAX_TRADES_PER_DAY` | `20` | Hard cap (total across symbols) |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Pause new entries |

**Sizing example:** equity $5 000, risk 0.5% → $25. Entry 50 000, stop 49 900 → distance $100 → size **0.25 BTC**. Leverage does not increase that size; it only caps max notional at `equity × leverage`.

## LIVE mode (gated)

```bash
# .env
TRADING_MODE=live
I_UNDERSTAND_LIVE_TRADING=true
HL_PRIVATE_KEY=0x...          # API wallet key
HL_ACCOUNT_ADDRESS=0x...      # optional master address if using agent
HL_NETWORK=mainnet            # or testnet
SYMBOLS=BTC,SOL,XRP
```

Without `I_UNDERSTAND_LIVE_TRADING=true`, LIVE will refuse to start. Open/close/stop orders are placed **per symbol** the same way as paper.

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
- Max **20** trades/day **total** (all symbols)
- Pause after **3** consecutive losses
- Max open positions = `MAX_OPEN_POSITIONS` (default = number of symbols)
- **One position max per symbol** — no averaging down
- Stop **required**
- Kill switch: env `KILL_SWITCH=1` or file `.killswitch`

### Stop placement (VWAP scalp)

For a **long**, stop sits just **below** session VWAP (VWAP − `STOP_BUFFER_BPS`).  
For a **short**, stop sits just **above** VWAP (VWAP + buffer).  
That way a reclaim/reject of VWAP invalidates the scalp. TP is entry ± `TP_R_MULTIPLE × R`.

## Tests

```bash
pytest -q
```

All unit tests are offline (no network).

## Project layout

```
src/hl_bot/
  config.py              # env-based settings (SYMBOLS / SYMBOL)
  journal.py             # JSONL trade log
  __main__.py            # python -m hl_bot run
  exchange/
    info_client.py       # mark/mid + candles wrapper
    paper_broker.py      # multi-symbol simulated fills at mark
    live_exchange.py     # LIVE only
  risk/manager.py        # sizing + hard limits (shared)
  strategy/
    base.py              # Strategy protocol
    vwap.py              # VWAP trend scalp
    ai_signal.py         # stub (unused)
  execution/loop.py      # main loop (per-symbol)
tests/
```

## Caveats

- **Mark/mid language:** prices come from Hyperliquid Info (`allMids` / `markPx`), not spot.
- **Candle / VWAP data:** session VWAP uses Info `candleSnapshot` when available. The SDK/API surface can vary; if candles are empty, VWAP cannot signal until bars exist. Paper/tests can `inject_bars` on `InfoClient` (optionally per coin).
- LIVE order helpers depend on `hyperliquid-python-sdk` versions; always verify on testnet.
- Equity (paper) = cash + sum of mark-to-market on all open positions.

## License / disclaimer

Provided as-is with no warranty. Use at your own risk. Not financial advice.

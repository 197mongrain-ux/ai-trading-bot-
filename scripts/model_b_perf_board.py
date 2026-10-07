"""Model B mainnet performance board.

Mainnet-only closed trades since the Model B flip (~2026-10-06 21:34 ET).
Writes reports/model_b_perf_board.{html,json,md} with:
  - starting fund + deposits/withdrawals (cash flows) + total funds
  - overall WR / net / % on capital (deposits never count as PnL)
  - per-day rows: daily % PnL (time-weighted across same-day deposits) + win rate

Cash flows come from Hyperliquid's userNonFundingLedgerUpdates (deposit,
withdraw, send / spotTransfer / internalTransfer / subAccountTransfer in or out
of the account). reports/model_b_deposits.json is a fallback / manual override
file: {"flows": [{"time_ms": ..., "amount": 54.12, "kind": "deposit", "note": ""}]}
(negative amount = withdrawal). Entries are merged with the API by hash/time.

Refresh:
  .venv\\Scripts\\python.exe scripts\\model_b_perf_board.py
  .venv\\Scripts\\python.exe scripts\\model_b_perf_board.py --publish
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

try:
    from zoneinfo import ZoneInfo

    try:
        ET = ZoneInfo("America/Toronto")
    except Exception:  # pragma: no cover
        ET = timezone(timedelta(hours=-4), name="ET")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-4), name="ET")

# First mainnet flip / agent live window (desk log: ~21:34 flip, live after 22:02).
DEFAULT_SINCE = datetime(2026, 10, 6, 21, 34, 0, tzinfo=ET)
DEFAULT_STARTING = 14.80
DEFAULT_OUT_MD = ROOT / "reports" / "model_b_perf_board.md"
DEFAULT_OUT_JSON = ROOT / "reports" / "model_b_perf_board.json"
DEFAULT_OUT_HTML = ROOT / "reports" / "model_b_perf_board.html"
DEFAULT_FLOWS_FILE = ROOT / "reports" / "model_b_deposits.json"
PUBLIC_PAGES_REPO = "197mongrain-ux/ai-trading-bot-"
PUBLIC_PAGES_HTML_PATH = "docs/model-b/index.html"
PUBLIC_PAGES_JSON_PATH = "docs/model-b/board.json"
PUBLIC_BOARD_URL = "https://197mongrain-ux.github.io/ai-trading-bot-/model-b/"


def load_dotenv(path: Path | None = None) -> None:
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_et(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%Y-%m-%d %H:%M:%S ET")


def _redact_addr(addr: str) -> str:
    a = (addr or "").strip()
    if len(a) < 12:
        return "(missing)"
    return f"{a[:6]}…{a[-4:]}"


def _is_testnet() -> bool:
    """Respect HL_NETWORK=mainnet (desk .env). Legacy HL_TESTNET/TESTNET still work."""
    net = (os.environ.get("HL_NETWORK") or "").strip().lower()
    if net in ("mainnet", "main"):
        return False
    if net in ("testnet", "test"):
        return True
    raw = (os.environ.get("HL_TESTNET") or os.environ.get("TESTNET") or "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    # Default mainnet for this Model B board (never mix testnet book).
    return False


_TRANSFER_TYPES = ("send", "spotTransfer", "internalTransfer", "subAccountTransfer")


def classify_ledger_update(upd: dict[str, Any], addr: str) -> dict[str, Any] | None:
    """Map one userNonFundingLedgerUpdates row to an external cash flow (or None).

    + = money in (deposit), - = money out (withdrawal). Moves inside the same
    account (spot<->perp accountClassTransfer, self sends between dexes) are not
    cash flows and return None.
    """
    me = (addr or "").strip().lower()
    d = upd.get("delta") or {}
    typ = str(d.get("type") or "")
    t_ms = int(upd.get("time") or 0)
    h = str(upd.get("hash") or "")
    amount: float | None = None
    if typ == "deposit":
        amount = _f(d.get("usdc"))
    elif typ == "withdraw":
        amount = -(_f(d.get("usdc")) + _f(d.get("fee")))
    elif typ in _TRANSFER_TYPES:
        src = str(d.get("user") or "").lower()
        dst = str(d.get("destination") or "").lower()
        val = d.get("usdcValue")
        if val is None:
            val = d.get("usdc") if d.get("usdc") is not None else d.get("amount")
        v = _f(val)
        if dst == me and src != me:
            amount = v
        elif src == me and dst != me:
            amount = -(v + _f(d.get("fee")))
        else:
            return None
    else:
        return None
    if amount is None or abs(amount) < 1e-12:
        return None
    return {
        "time_ms": t_ms,
        "amount": round(amount, 6),
        "kind": "deposit" if amount > 0 else "withdrawal",
        "ledger_type": typ,
        "hash": h,
        "source": "hl_ledger",
    }


def load_flows_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    rows = raw.get("flows") if isinstance(raw, dict) else raw
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t_ms = int(_f(r.get("time_ms")))
        if not t_ms and r.get("time_et"):
            try:
                dt = datetime.fromisoformat(str(r["time_et"]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ET)
                t_ms = int(dt.timestamp() * 1000)
            except Exception:
                t_ms = 0
        amt = _f(r.get("amount"))
        if not t_ms or abs(amt) < 1e-12:
            continue
        out.append(
            {
                "time_ms": t_ms,
                "amount": round(amt, 6),
                "kind": r.get("kind") or ("deposit" if amt > 0 else "withdrawal"),
                "ledger_type": r.get("ledger_type") or "manual",
                "hash": str(r.get("hash") or ""),
                "note": r.get("note") or "",
                "source": "file",
            }
        )
    return out


def merge_flows(api: list[dict[str, Any]], file_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """API rows win; file rows are added unless they match an API row by hash or time (+/-60s)."""
    out = list(api)
    for r in file_rows:
        dup = False
        for a in api:
            if r.get("hash") and r["hash"] == a.get("hash"):
                dup = True
                break
            if abs(int(r["time_ms"]) - int(a["time_ms"])) <= 60_000 and abs(r["amount"] - a["amount"]) < 0.01:
                dup = True
                break
        if not dup:
            out.append(r)
    out.sort(key=lambda x: int(x["time_ms"]))
    return out


def save_flows_file(path: Path, flows: list[dict[str, Any]]) -> None:
    """Cache detected flows so the board still works if the ledger API is down."""
    rows = []
    for f in flows:
        rows.append(
            {
                "time_ms": int(f["time_ms"]),
                "time_et": datetime.fromtimestamp(int(f["time_ms"]) / 1000.0, tz=ET).isoformat(),
                "amount": f["amount"],
                "kind": f.get("kind"),
                "ledger_type": f.get("ledger_type"),
                "hash": f.get("hash") or "",
                "note": f.get("note") or "",
            }
        )
    payload = {
        "_doc": "Model B board cash flows (deposits +, withdrawals -). Auto-cached from HL "
        "userNonFundingLedgerUpdates; add manual rows if the API misses one.",
        "flows": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def fetch_hl_mainnet(since_ts: float) -> dict[str, Any]:
    addr = (
        os.environ.get("HL_ACCOUNT_ADDRESS")
        or os.environ.get("HL_ADDRESS")
        or ""
    ).strip()
    if not addr:
        return {"ok": False, "reason": "no HL_ACCOUNT_ADDRESS"}

    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "reason": f"hyperliquid import failed: {exc}"}

    testnet = _is_testnet()
    url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    if testnet:
        return {
            "ok": False,
            "reason": "HL_NETWORK is testnet — Model B perf board is mainnet-only",
            "testnet": True,
            "api": url,
        }

    info = Info(url, skip_ws=True)
    since_ms = int(since_ts * 1000)
    fills_all = info.user_fills(addr) or []
    fills = [f for f in fills_all if int(f.get("time") or 0) >= since_ms]
    fills.sort(key=lambda f: int(f.get("time") or 0))

    # Cluster Close fills in the same second / coin as one round-trip for WR.
    clusters: dict[tuple[str, int], dict[str, Any]] = {}
    order: list[tuple[str, int]] = []
    for f in fills:
        direc = str(f.get("dir") or "")
        if "Close" not in direc:
            continue
        coin = str(f.get("coin") or "?")
        t_ms = int(f.get("time") or 0)
        key = (coin, t_ms // 1000)
        if key not in clusters:
            clusters[key] = {
                "coin": coin,
                "dir": direc,
                "pnl": 0.0,
                "fees": 0.0,
                "fills": 0,
                "time_ms": t_ms,
                "sz": 0.0,
            }
            order.append(key)
        c = clusters[key]
        c["pnl"] += _f(f.get("closedPnl"))
        c["fees"] += _f(f.get("fee"))
        c["fills"] += 1
        c["sz"] += _f(f.get("sz"))

    # Attribute open fees to the nearest later close of the same coin (for trade net display).
    open_fee_events: list[tuple[str, float, float]] = []  # coin, ts, fee
    for f in fills:
        direc = str(f.get("dir") or "")
        if "Open" not in direc:
            continue
        open_fee_events.append(
            (str(f.get("coin") or "?"), int(f.get("time") or 0) / 1000.0, _f(f.get("fee")))
        )

    closed_trades: list[dict[str, Any]] = []
    wins = losses = be = 0
    used_opens: set[int] = set()
    for key in order:
        c = clusters[key]
        pnl = float(c["pnl"])
        close_fees = float(c["fees"])
        close_ts = c["time_ms"] / 1000.0
        open_fees = 0.0
        # Sum open fees for this coin in the 2h before close that aren't already used.
        for i, (ocoin, ots, ofee) in enumerate(open_fee_events):
            if i in used_opens:
                continue
            if ocoin != c["coin"]:
                continue
            if ots <= close_ts and (close_ts - ots) <= 7200.0:
                open_fees += ofee
                used_opens.add(i)
        trade_fees = open_fees + close_fees
        trade_net = pnl - trade_fees
        if pnl > 1e-9:
            result = "WIN"
            wins += 1
        elif pnl < -1e-9:
            result = "LOSS"
            losses += 1
        else:
            result = "BE"
            be += 1
        closed_trades.append(
            {
                "symbol": c["coin"],
                "dir": c["dir"],
                "pnl": round(pnl, 6),
                "fees": round(trade_fees, 6),
                "net": round(trade_net, 6),
                "fills": c["fills"],
                "sz": round(float(c["sz"]), 8),
                "closed_ts": close_ts,
                "result": result,
                "source": "hl_fills",
            }
        )

    all_fees = sum(_f(f.get("fee")) for f in fills)
    gross = sum(_f(t["pnl"]) for t in closed_trades)

    positions: list[dict[str, Any]] = []
    upnl_total = 0.0
    margin_used = 0.0
    for dex in (None, "xyz"):
        try:
            st = info.user_state(addr) if dex is None else info.user_state(addr, dex=dex)
            st = st or {}
            for ap in st.get("assetPositions") or []:
                pos = ap.get("position") or {}
                szi = _f(pos.get("szi"))
                if abs(szi) < 1e-12:
                    continue
                upnl = _f(pos.get("unrealizedPnl"))
                mu = _f(pos.get("marginUsed"))
                upnl_total += upnl
                margin_used += mu
                positions.append(
                    {
                        "coin": pos.get("coin"),
                        "szi": szi,
                        "side": "long" if szi > 0 else "short",
                        "entry": _f(pos.get("entryPx")),
                        "uPnl": upnl,
                        "marginUsed": mu,
                        "dex": dex or "main",
                    }
                )
        except Exception as exc:
            positions.append({"error": f"{dex or 'main'}: {exc}"})

    # External cash flows (deposits / withdrawals) since the flip.
    ledger_ok = False
    ledger_err = None
    api_flows: list[dict[str, Any]] = []
    try:
        rows = info.post(
            "/info",
            {"type": "userNonFundingLedgerUpdates", "user": addr, "startTime": since_ms},
        ) or []
        for upd in rows:
            fl = classify_ledger_update(upd, addr)
            if fl and int(fl["time_ms"]) >= since_ms:
                api_flows.append(fl)
        ledger_ok = True
    except Exception as exc:
        ledger_err = str(exc)

    funding_total = 0.0
    try:
        frows = info.post(
            "/info", {"type": "userFunding", "user": addr, "startTime": since_ms}
        ) or []
        for fr in frows:
            funding_total += _f((fr.get("delta") or {}).get("usdc"))
    except Exception:
        funding_total = 0.0

    spot_usdc = None
    spot_hold = None
    try:
        spot = info.spot_user_state(addr) or {}
        for b in spot.get("balances") or []:
            if str(b.get("coin") or "").upper() == "USDC":
                spot_usdc = _f(b.get("total"))
                spot_hold = _f(b.get("hold"))
                break
    except Exception:
        spot_usdc = None
        spot_hold = None

    decided = wins + losses
    wr = round(wins / decided * 100.0, 1) if decided else None

    return {
        "ok": True,
        "testnet": False,
        "api": url,
        "address_redacted": _redact_addr(addr),
        "fills_n": len(fills),
        "fills": fills,
        "gross_pnl": round(gross, 6),
        "fees": round(all_fees, 6),
        "closed_trades": closed_trades,
        "trades": len(closed_trades),
        "wins": wins,
        "losses": losses,
        "breakeven": be,
        "win_rate_pct": wr,
        "open_positions": positions,
        "spot_usdc": spot_usdc,
        "spot_hold": spot_hold,
        "unrealized_pnl": round(upnl_total, 6),
        "margin_used": round(margin_used, 6),
        "ledger_ok": ledger_ok,
        "ledger_err": ledger_err,
        "api_flows": api_flows,
        "funding": round(funding_total, 6),
    }


def build_daily(
    closed_trades: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    starting_fund: float,
    flows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Per-ET-day: W/L/BE, WR, net $, deposits, daily % PnL.

    Deposits/withdrawals are cash flows, never PnL. Daily % is time-weighted:
    the day is split at each cash flow, each slice's return = realized net in
    the slice / equity at slice start, and the slices are chained. So a
    deposit neither shows as gain nor dilutes trades closed before it.
    daily_pct_on_capital = net / (equity at day start + that day's deposits).
    Days with a close or a cash flow get a row.
    """
    flows = list(flows or [])
    events: list[tuple[float, int, str, dict[str, Any]]] = []
    for t in closed_trades:
        events.append((float(t["closed_ts"]), 1, "trade", t))
    for f in flows:
        events.append((int(f["time_ms"]) / 1000.0, 0, "flow", f))
    events.sort(key=lambda e: (e[0], e[1]))
    by_day: dict[str, list[tuple[float, int, str, dict[str, Any]]]] = defaultdict(list)
    for ev in events:
        day = datetime.fromtimestamp(ev[0], tz=ET).strftime("%Y-%m-%d")
        by_day[day].append(ev)

    equity = float(starting_fund)
    rows: list[dict[str, Any]] = []
    for day in sorted(by_day):
        w = l = be = n = 0
        gross = fees = net = 0.0
        dep = wd = 0.0
        sod = equity
        slice_start = equity
        slice_net = 0.0
        growth = 1.0
        for _ts, _o, kind, obj in by_day[day]:
            if kind == "flow":
                if slice_start > 0:
                    growth *= 1.0 + slice_net / slice_start
                amt = _f(obj.get("amount"))
                if amt > 0:
                    dep += amt
                else:
                    wd += -amt
                equity += amt
                slice_start = equity
                slice_net = 0.0
                continue
            pnl = _f(obj.get("pnl"))
            fee = _f(obj.get("fees"))
            tnet = _f(obj.get("net"), pnl - fee)
            n += 1
            gross += pnl
            fees += fee
            net += tnet
            slice_net += tnet
            equity += tnet
            if pnl > 1e-9:
                w += 1
            elif pnl < -1e-9:
                l += 1
            else:
                be += 1
        if slice_start > 0:
            growth *= 1.0 + slice_net / slice_start
        decided = w + l
        wr = round(w / decided * 100.0, 1) if decided else None
        cap = sod + dep
        rows.append(
            {
                "date": day,
                "trades": n,
                "wins": w,
                "losses": l,
                "breakeven": be,
                "win_rate_pct": wr,
                "gross_pnl": round(gross, 6),
                "fees": round(fees, 6),
                "net_pnl": round(net, 6),
                "deposits": round(dep, 6),
                "withdrawals": round(wd, 6),
                "net_flows": round(dep - wd, 6),
                "equity_start": round(sod, 6),
                "capital": round(cap, 6),
                "equity_end": round(equity, 6),
                "daily_pct": round((growth - 1.0) * 100.0, 3) if n else 0.0,
                "daily_pct_on_capital": round(net / cap * 100.0, 3) if cap else None,
            }
        )
    return rows


def build_board(
    *, since: datetime, starting_fund: float, flows_file: Path | None = DEFAULT_FLOWS_FILE
) -> dict[str, Any]:
    since_ts = since.timestamp()
    hl = fetch_hl_mainnet(since_ts)
    generated = datetime.now(tz=ET)

    if not hl.get("ok"):
        return {
            "title": "Model B · Mainnet performance",
            "since_et": since.isoformat(),
            "generated_et": generated.isoformat(),
            "network": "mainnet",
            "starting_fund": starting_fund,
            "total_funds": None,
            "money_source": "hl_fills",
            "hl": {
                "ok": False,
                "reason": hl.get("reason"),
                "address_redacted": hl.get("address_redacted"),
                "api": hl.get("api"),
            },
            "summary": {},
            "daily": [],
            "closed_trades": [],
            "open_positions": [],
            "notes": [f"HL fetch failed: {hl.get('reason')}"],
        }

    gross = float(hl["gross_pnl"])
    fees = float(hl["fees"])
    net = round(gross - fees, 6)
    funding = float(hl.get("funding") or 0.0)
    spot = hl.get("spot_usdc")
    hold = hl.get("spot_hold")
    upnl = float(hl.get("unrealized_pnl") or 0.0)

    # Cash flows: ledger API first, fallback/manual file merged in.
    file_rows = load_flows_file(flows_file) if flows_file else []
    file_rows = [r for r in file_rows if int(r["time_ms"]) >= int(since_ts * 1000)]
    api_flows = list(hl.get("api_flows") or [])
    flows = merge_flows(api_flows, file_rows) if hl.get("ledger_ok") else sorted(
        file_rows, key=lambda x: int(x["time_ms"])
    )
    if flows_file and hl.get("ledger_ok") and flows:
        try:
            save_flows_file(flows_file, flows)
        except Exception:
            pass
    flows_source = (
        "hl_ledger" if hl.get("ledger_ok") else ("file" if flows else "none")
    )
    deposits = round(sum(f["amount"] for f in flows if f["amount"] > 0), 6)
    withdrawals = round(-sum(f["amount"] for f in flows if f["amount"] < 0), 6)
    net_flows = round(deposits - withdrawals, 6)
    total_deposited = round(starting_fund + net_flows, 6)

    # Unified account: spot USDC total already includes perp margin + unrealized
    # PnL (verified: spot = start + flows + realized net + funding + uPnL).
    total_funds = round(float(spot), 6) if spot is not None else None
    expected_equity = round(total_deposited + net + funding + upnl, 6)
    recon_residual = (
        round(total_funds - expected_equity, 6) if total_funds is not None else None
    )
    total_pnl = round(total_funds - total_deposited, 6) if total_funds is not None else None
    total_pnl_pct = (
        round(total_pnl / total_deposited * 100.0, 3)
        if total_pnl is not None and total_deposited
        else None
    )
    net_on_capital_pct = round(net / total_deposited * 100.0, 3) if total_deposited else None
    vs_pct = round(net / starting_fund * 100.0, 3) if starting_fund else None

    daily = build_daily(
        list(hl["closed_trades"]), list(hl.get("fills") or []), starting_fund, flows
    )
    growth = 1.0
    for d in daily:
        growth *= 1.0 + float(d.get("daily_pct") or 0.0) / 100.0
    twr_pct = round((growth - 1.0) * 100.0, 3)
    flows_pub = [
        {
            "time_ms": int(f["time_ms"]),
            "time_et": _fmt_et(int(f["time_ms"]) / 1000.0),
            "amount": f["amount"],
            "kind": f.get("kind"),
            "ledger_type": f.get("ledger_type"),
            "source": f.get("source"),
        }
        for f in flows
    ]

    board = {
        "title": "Model B · Mainnet performance",
        "model": "model_b",
        "since_et": since.isoformat(),
        "generated_et": generated.isoformat(),
        "network": "mainnet",
        "framing": "Hyperliquid MAINNET — Model B closed trades since flip",
        "starting_fund": starting_fund,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "net_flows": net_flows,
        "total_deposited": total_deposited,
        "total_funds": total_funds,
        "cash_flows": flows_pub,
        "cash_flows_source": flows_source,
        "money_source": "hl_fills",
        "hl": {
            "ok": True,
            "reason": None,
            "address_redacted": hl.get("address_redacted"),
            "fills_n": hl.get("fills_n"),
            "api": hl.get("api"),
        },
        "summary": {
            "trades": hl["trades"],
            "wins": hl["wins"],
            "losses": hl["losses"],
            "breakeven": hl["breakeven"],
            "win_rate_pct": hl["win_rate_pct"],
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "vs_starting_usd": net,
            "vs_starting_pct": vs_pct,
            "net_on_capital_pct": net_on_capital_pct,
            "twr_pct": twr_pct,
            "funding": round(funding, 6),
            "starting_fund": starting_fund,
            "deposits": deposits,
            "withdrawals": withdrawals,
            "net_flows": net_flows,
            "total_deposited": total_deposited,
            "spot_usdc": spot,
            "spot_hold": hold,
            "unrealized_pnl": upnl,
            "margin_used": hl.get("margin_used"),
            "total_funds": total_funds,
            "total_pnl_usd": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "total_vs_starting_usd": total_pnl,
            "total_vs_starting_pct": total_pnl_pct,
            "expected_equity": expected_equity,
            "recon_residual": recon_residual,
        },
        "daily": daily,
        "closed_trades": hl["closed_trades"],
        "open_positions": hl["open_positions"],
        "notes": [
            "Mainnet-only. Window starts at Model B flip (~2026-10-06 21:34 ET). Testnet book is excluded.",
            "Win rate = W / (W+L) on closed round-trips (HL Close-fill clusters). BE excluded from WR.",
            "Trade PnL column = HL closedPnl; Fees = open+close fees attributed to that close; Net = PnL − fees.",
            "Overall net = sum(closedPnl) − all fill fees in window (realized trades only).",
            "Deposits/withdrawals come from HL userNonFundingLedgerUpdates (fallback: reports/model_b_deposits.json). They are capital, never PnL.",
            "Daily % PnL is time-weighted: the day is split at each deposit/withdrawal and each slice's realized net is divided by equity at the slice start, then chained. % on capital = net / (start-of-day equity + that day's deposits).",
            "Starting fund = $14.80 at the mainnet flip. Total deposited = starting fund + net deposits. Total funds = spot USDC (unified account: already includes perp margin + unrealized PnL). Total PnL = total funds − total deposited (includes funding + unrealized).",
        ],
    }
    return board


def public_board(board: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(board, default=str))
    return out


def render_md(board: dict[str, Any]) -> str:
    s = board.get("summary") or {}
    wr = s.get("win_rate_pct")
    wr_s = f"{wr:.1f}%" if wr is not None else "n/a"
    lines = [
        f"# {board['title']}",
        "",
        f"- **Since (flip):** {board['since_et']}",
        f"- **Generated:** {board['generated_et']}",
        f"- **Network:** {board.get('network')} — {board.get('framing')}",
        f"- **Starting fund:** ${float(board.get('starting_fund') or 0):.2f}",
        f"- **Deposits:** ${float(board.get('net_flows') or 0):+.2f} (source `{board.get('cash_flows_source')}`)",
        f"- **Total deposited:** ${float(board.get('total_deposited') or board.get('starting_fund') or 0):.2f}",
        (
            f"- **Total funds:** ${float(board['total_funds']):.4f}"
            if board.get("total_funds") is not None
            else "- **Total funds:** n/a"
        ),
        f"- **Money source:** `{board.get('money_source')}`",
        f"- **HL:** ok={board['hl'].get('ok')} addr={board['hl'].get('address_redacted')} fills={board['hl'].get('fills_n')}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Starting fund | ${float(board.get('starting_fund') or 0):.2f} |",
        f"| Deposits | ${float(s.get('deposits') or 0):.4f} |",
        f"| Withdrawals | ${float(s.get('withdrawals') or 0):.4f} |",
        f"| Total deposited (capital) | ${float(s.get('total_deposited') or 0):.4f} |",
    ]
    if s.get("total_funds") is not None:
        lines.append(f"| **Total funds** | **${float(s['total_funds']):.4f}** |")
    if s.get("total_pnl_usd") is not None:
        lines.append(
            f"| Total PnL (funds − deposited) | ${float(s['total_pnl_usd']):+.4f} "
            f"({float(s.get('total_pnl_pct') or 0):+.3f}%) |"
        )
    if s.get("spot_usdc") is not None:
        lines.append(f"| Spot USDC | ${float(s['spot_usdc']):.4f} |")
    if s.get("spot_hold") is not None:
        lines.append(f"| Spot hold (margin) | ${float(s['spot_hold']):.4f} |")
    if s.get("unrealized_pnl") is not None:
        lines.append(f"| Unrealized PnL | ${float(s['unrealized_pnl']):+.4f} |")
    lines += [
        f"| Trades (closed) | {s.get('trades', 0)} |",
        f"| Wins | {s.get('wins', 0)} |",
        f"| Losses | {s.get('losses', 0)} |",
        f"| Breakeven | {s.get('breakeven', 0)} |",
        f"| **Win rate** | **{wr_s}** |",
        f"| Gross PnL | ${float(s.get('gross_pnl') or 0):+.4f} |",
        f"| Fees | ${float(s.get('fees') or 0):.4f} |",
        f"| **Net PnL** | **${float(s.get('net_pnl') or 0):+.4f}** |",
    ]
    if s.get("twr_pct") is not None:
        lines.append(f"| Net PnL % (time-weighted) | {float(s['twr_pct']):+.3f}% |")
    if s.get("net_on_capital_pct") is not None:
        lines.append(f"| Net PnL % on capital | {float(s['net_on_capital_pct']):+.3f}% |")
    if s.get("funding") is not None:
        lines.append(f"| Funding | ${float(s['funding']):+.4f} |")

    lines += ["", "## Cash flows (deposits / withdrawals)", ""]
    cf = board.get("cash_flows") or []
    if not cf:
        lines.append("_None since flip._")
    else:
        lines.append("| When (ET) | Kind | Amount | Source |")
        lines.append("|---|---|---:|---|")
        for f in cf:
            lines.append(
                f"| {f.get('time_et')} | {f.get('kind')} ({f.get('ledger_type')}) | "
                f"${_f(f.get('amount')):+.4f} | {f.get('source')} |"
            )

    lines += ["", "## Daily", ""]
    daily = board.get("daily") or []
    if not daily:
        lines.append("_No closed days yet._")
    else:
        lines.append(
            "| Date (ET) | Trades | W/L/BE | Win rate | Net $ | Daily % | % on capital | Deposits | Equity EOD |"
        )
        lines.append("|---|---:|---|---:|---:|---:|---:|---:|---:|")
        for d in daily:
            wr_d = d.get("win_rate_pct")
            wr_ds = f"{wr_d:.1f}%" if wr_d is not None else "—"
            pct = d.get("daily_pct")
            pct_s = f"{pct:+.3f}%" if pct is not None else "—"
            pc = d.get("daily_pct_on_capital")
            pc_s = f"{pc:+.3f}%" if pc is not None else "—"
            lines.append(
                f"| {d['date']} | {d['trades']} | {d['wins']}/{d['losses']}/{d['breakeven']} | "
                f"{wr_ds} | ${float(d['net_pnl']):+.4f} | {pct_s} | {pc_s} | "
                f"${float(d.get('net_flows') or 0):+.2f} | ${float(d['equity_end']):.4f} |"
            )

    lines += ["", "## Closed trades", ""]
    if not board.get("closed_trades"):
        lines.append("_None in window._")
    else:
        lines.append("| When (ET) | Symbol | Result | PnL | Fees | Net |")
        lines.append("|---|---|---|---:|---:|---:|")
        for t in board["closed_trades"]:
            lines.append(
                f"| {_fmt_et(_f(t.get('closed_ts')) or None)} | {t.get('symbol')} | {t.get('result')} | "
                f"${_f(t.get('pnl')):+.4f} | ${_f(t.get('fees')):.4f} | ${_f(t.get('net')):+.4f} |"
            )

    lines += ["", "## Open positions", ""]
    opens = board.get("open_positions") or []
    if not opens:
        lines.append("_Flat — no open HL perps._")
    else:
        lines.append("| Coin | Side | Size | Entry | uPnL | Margin | Dex |")
        lines.append("|---|---|---:|---:|---:|---:|---|")
        for p in opens:
            if p.get("error"):
                lines.append(f"| error | | | | | | {p['error']} |")
                continue
            lines.append(
                f"| {p.get('coin')} | {p.get('side')} | {p.get('szi')} | {p.get('entry')} | "
                f"{p.get('uPnl')} | {p.get('marginUsed')} | {p.get('dex')} |"
            )

    lines += ["", "## Notes", ""]
    for n in board.get("notes") or []:
        lines.append(f"- {n}")
    lines += [
        "",
        "## Refresh",
        "",
        "```bat",
        r".venv\Scripts\python.exe scripts\model_b_perf_board.py --publish",
        "```",
        "",
        f"Public URL: {PUBLIC_BOARD_URL}",
        "",
    ]
    return "\n".join(lines)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Model B · Mainnet performance</title>
<style>
:root {
  --bg:#000; --bg2:#0b0b0b; --panel:#0e0e0e; --border:#1a1a1a;
  --text:#e8e8e8; --muted:#8b8b8b; --dim:#5c5c5c;
  --green:#00c805; --red:#ff4d4f; --amber:#e6c07b;
  --mono:"SF Mono","JetBrains Mono","IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  --sans:"Inter","IBM Plex Sans","Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.pos{color:var(--green)}.neg{color:var(--red)}.muted{color:var(--muted)}
.topbar{display:flex;align-items:center;gap:1rem;padding:.7rem 1.1rem;border-bottom:1px solid var(--border);background:var(--bg2);position:sticky;top:0;z-index:20;flex-wrap:wrap}
.brand-title{font-weight:650;font-size:14px}
.brand-sub{color:var(--muted);font-size:10px}
.badge{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.08em;padding:.2rem .5rem;border-radius:3px;border:1px solid #1a5c2a;color:var(--green);background:#06140a}
.spacer{flex:1}
.meta{color:var(--muted);font-size:11px}
.wrap{max-width:1100px;margin:0 auto;padding:1rem 1.1rem 2.5rem;display:flex;flex-direction:column;gap:1rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:.75rem .85rem}
.card .lbl{color:var(--muted);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:.25rem}
.card .val{font-family:var(--mono);font-size:1.15rem;font-weight:650}
.card .sub{color:var(--muted);font-size:10px;margin-top:.2rem}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.panel h2{margin:0;padding:.65rem .85rem;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
th,td{padding:.45rem .7rem;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.04em}
td.num,th.num{text-align:right}
.notes{color:var(--muted);font-size:11px;line-height:1.5;padding:0 .2rem}
.notes li{margin:.25rem 0}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <div class="brand-title">Model B · Mainnet performance</div>
    <div class="brand-sub" id="sub"></div>
  </div>
  <span class="badge" id="netBadge">MAINNET</span>
  <div class="spacer"></div>
  <div class="meta mono" id="gen"></div>
</div>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="panel">
    <h2>Daily · % PnL &amp; win rate</h2>
    <div style="overflow-x:auto"><table id="daily"></table></div>
  </div>
  <div class="panel">
    <h2>Deposits &amp; withdrawals</h2>
    <div style="overflow-x:auto"><table id="flows"></table></div>
  </div>
  <div class="panel">
    <h2>Closed trades</h2>
    <div style="overflow-x:auto"><table id="trades"></table></div>
  </div>
  <div class="panel">
    <h2>Open positions</h2>
    <div style="overflow-x:auto"><table id="opens"></table></div>
  </div>
  <ul class="notes" id="notes"></ul>
</div>
<script>
const BOARD = __BOARD_JSON__;
const s = BOARD.summary || {};
const fmtUsd = (v, signed=false) => {
  if (v==null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const body = Math.abs(n).toFixed(4);
  if (!signed) return "$"+body;
  return (n>0?"+$":n<0?"-$":"$")+body;
};
const cls = (v) => (v==null?"":(Number(v)>0?"pos":Number(v)<0?"neg":""));
document.getElementById("sub").textContent =
  `since flip ${BOARD.since_et||"—"} · ${BOARD.money_source||""} · starting $${Number(BOARD.starting_fund||0).toFixed(2)} · deposits $${Number(BOARD.net_flows||0).toFixed(2)}`;
document.getElementById("gen").textContent = `generated ${BOARD.generated_et||""}`;
const wr = s.win_rate_pct;
const pctS = (v) => v==null ? "—" : `${Number(v)>0?"+":""}${v}%`;
const nFlows = (BOARD.cash_flows||[]).length;
const cards = [
  {lbl:"Starting fund", val:fmtUsd(BOARD.starting_fund).replace(/(\.\d{2})\d+/,"$1"), c:"", sub:"at mainnet flip"},
  {lbl:"Deposits", val:(()=>{const v=Number(s.net_flows||0);return (v>0?"+$":v<0?"-$":"$")+Math.abs(v).toFixed(2);})(), c:"",
   sub: nFlows ? `${nFlows} cash flow${nFlows>1?"s":""} · capital $${Number(s.total_deposited||0).toFixed(2)}` : "none since flip"},
  {lbl:"Total funds", val:fmtUsd(s.total_funds), c:cls(s.total_pnl_usd),
   sub: s.total_pnl_usd!=null ? `${fmtUsd(s.total_pnl_usd,true)} (${pctS(s.total_pnl_pct)}) vs deposited` : "spot USDC"},
  {lbl:"Win rate", val: wr!=null ? `${wr}%` : "—", c:"",
   sub: `${s.wins||0}W / ${s.losses||0}L / ${s.breakeven||0}BE · n=${s.trades||0}`},
  {lbl:"Net PnL", val:fmtUsd(s.net_pnl,true), c:cls(s.net_pnl),
   sub: `${pctS(s.twr_pct)} time-weighted · ${pctS(s.net_on_capital_pct)} on capital`},
  {lbl:"Unrealized", val:fmtUsd(s.unrealized_pnl,true), c:cls(s.unrealized_pnl),
   sub: s.spot_hold!=null ? `open perps · hold $${Number(s.spot_hold).toFixed(2)}` : "open perps"},
];
document.getElementById("cards").innerHTML = cards.map(c =>
  `<div class="card"><div class="lbl">${c.lbl}</div><div class="val mono ${c.c}">${c.val}</div><div class="sub">${c.sub}</div></div>`
).join("");

const daily = BOARD.daily || [];
const dHead = `<tr><th>Date (ET)</th><th class="num">Trades</th><th>W/L/BE</th><th class="num">Win rate</th><th class="num">Net $</th><th class="num">Daily %</th><th class="num">% on capital</th><th class="num">Deposits</th><th class="num">Equity EOD</th></tr>`;
const dBody = daily.length ? daily.map(d => {
  const wr = d.win_rate_pct!=null ? `${d.win_rate_pct}%` : "—";
  const pct = d.daily_pct!=null ? `${d.daily_pct>0?"+":""}${d.daily_pct}%` : "—";
  return `<tr>
    <td>${d.date}</td>
    <td class="num">${d.trades}</td>
    <td>${d.wins}/${d.losses}/${d.breakeven}</td>
    <td class="num">${wr}</td>
    <td class="num ${cls(d.net_pnl)}">${fmtUsd(d.net_pnl,true)}</td>
    <td class="num ${cls(d.daily_pct)}">${pct}</td>
    <td class="num ${cls(d.daily_pct_on_capital)}">${pctS(d.daily_pct_on_capital)}</td>
    <td class="num">${d.net_flows ? fmtUsd(d.net_flows,true) : "—"}</td>
    <td class="num">${fmtUsd(d.equity_end)}</td>
  </tr>`;
}).join("") : `<tr><td colspan="9" class="muted">No closed days yet.</td></tr>`;
document.getElementById("daily").innerHTML = dHead + dBody;

const flows = BOARD.cash_flows || [];
const fHead = `<tr><th>When (ET)</th><th>Kind</th><th class="num">Amount</th><th>Source</th></tr>`;
const fBody = flows.length ? flows.map(f => {
  const when = f.time_ms ? new Date(f.time_ms).toLocaleString("en-CA",{timeZone:"America/Toronto"}) : "—";
  return `<tr><td>${when}</td><td>${f.kind||""}</td><td class="num">${fmtUsd(f.amount,true)}</td><td>${f.source||""}</td></tr>`;
}).join("") : `<tr><td colspan="4" class="muted">No deposits or withdrawals since flip.</td></tr>`;
document.getElementById("flows").innerHTML = fHead + fBody;

const trades = BOARD.closed_trades || [];
const tHead = `<tr><th>When (ET)</th><th>Symbol</th><th>Result</th><th class="num">PnL</th><th class="num">Fees</th><th class="num">Net</th></tr>`;
const tBody = trades.length ? trades.map(t => {
  const when = t.closed_ts ? new Date(t.closed_ts*1000).toLocaleString("en-CA",{timeZone:"America/Toronto"}) : "—";
  return `<tr>
    <td>${when}</td><td>${t.symbol||""}</td><td class="${t.result==="WIN"?"pos":t.result==="LOSS"?"neg":""}">${t.result||""}</td>
    <td class="num ${cls(t.pnl)}">${fmtUsd(t.pnl,true)}</td>
    <td class="num">${fmtUsd(t.fees)}</td>
    <td class="num ${cls(t.net)}">${fmtUsd(t.net,true)}</td>
  </tr>`;
}).join("") : `<tr><td colspan="6" class="muted">None in window.</td></tr>`;
document.getElementById("trades").innerHTML = tHead + tBody;

const opens = BOARD.open_positions || [];
const oHead = `<tr><th>Coin</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">uPnL</th><th class="num">Margin</th><th>Dex</th></tr>`;
const oBody = opens.length ? opens.map(p => {
  if (p.error) return `<tr><td colspan="7" class="neg">${p.error}</td></tr>`;
  return `<tr>
    <td>${p.coin||""}</td><td>${p.side||""}</td>
    <td class="num">${p.szi}</td><td class="num">${p.entry}</td>
    <td class="num ${cls(p.uPnl)}">${fmtUsd(p.uPnl,true)}</td>
    <td class="num">${fmtUsd(p.marginUsed)}</td><td>${p.dex||""}</td>
  </tr>`;
}).join("") : `<tr><td colspan="7" class="muted">Flat — no open HL perps.</td></tr>`;
document.getElementById("opens").innerHTML = oHead + oBody;

document.getElementById("notes").innerHTML = (BOARD.notes||[]).map(n => `<li>${n}</li>`).join("");
</script>
</body>
</html>
"""


def render_html(board: dict[str, Any]) -> str:
    pub = public_board(board)
    # Drop raw fills from HTML payload (keep closed_trades / daily).
    pub.pop("fills", None)
    payload = json.dumps(pub, separators=(",", ":"), default=str)
    return HTML_TEMPLATE.replace("__BOARD_JSON__", payload)


def _gh_put_file(repo: str, path: str, content: str, message: str) -> str:
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    meta = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}"],
        capture_output=True,
        text=True,
    )
    sha = None
    if meta.returncode == 0:
        try:
            sha = json.loads(meta.stdout).get("sha")
        except Exception:
            sha = None
    payload: dict[str, Any] = {
        "message": message,
        "content": b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    r = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gh put {path} failed: {r.stderr or r.stdout}")
    try:
        return str(json.loads(r.stdout).get("content", {}).get("html_url") or "ok")
    except Exception:
        return "ok"


def publish_board(board: dict[str, Any], html: str) -> str:
    pub = public_board(board)
    pub.pop("fills", None)
    msg = f"Refresh Model B mainnet perf board {pub.get('generated_et', '')}".strip()
    try:
        probe = subprocess.run(
            ["gh", "api", f"repos/{PUBLIC_PAGES_REPO}/contents/docs/.nojekyll"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            _gh_put_file(PUBLIC_PAGES_REPO, "docs/.nojekyll", "", msg + " (.nojekyll)")
    except Exception:
        pass
    _gh_put_file(PUBLIC_PAGES_REPO, PUBLIC_PAGES_HTML_PATH, html, msg)
    _gh_put_file(
        PUBLIC_PAGES_REPO,
        PUBLIC_PAGES_JSON_PATH,
        json.dumps(pub, indent=2, default=str) + "\n",
        msg + " (json)",
    )
    return PUBLIC_BOARD_URL


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # Windows cp1252 console vs "−"/"—"
        except Exception:
            pass
    load_dotenv()
    ap = argparse.ArgumentParser(description="Model B mainnet performance board")
    ap.add_argument("--since", default=DEFAULT_SINCE.isoformat())
    ap.add_argument(
        "--starting-fund",
        type=float,
        default=_f(os.getenv("STARTING_EQUITY"), DEFAULT_STARTING),
    )
    ap.add_argument("--flows-file", type=Path, default=DEFAULT_FLOWS_FILE)
    ap.add_argument("--out-md", type=Path, default=DEFAULT_OUT_MD)
    ap.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    ap.add_argument("--out-html", type=Path, default=DEFAULT_OUT_HTML)
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=ET)

    board = build_board(
        since=since, starting_fund=float(args.starting_fund), flows_file=args.flows_file
    )
    # Don't persist raw fills in local JSON (size / noise).
    board_out = dict(board)
    md = render_md(board)
    html = render_html(board)
    for p in (args.out_md, args.out_json, args.out_html):
        p.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(md, encoding="utf-8")
    args.out_json.write_text(json.dumps(board_out, indent=2, default=str), encoding="utf-8")
    args.out_html.write_text(html, encoding="utf-8")

    # Also mirror into docs/model-b locally (Pages source tree on laptop).
    docs_dir = ROOT / "docs" / "model-b"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "index.html").write_text(html, encoding="utf-8")
    pub = public_board(board_out)
    (docs_dir / "board.json").write_text(json.dumps(pub, indent=2, default=str) + "\n", encoding="utf-8")

    s = board.get("summary") or {}
    print(md)
    print("---")
    print(f"wrote {args.out_md}")
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_html}")
    print(f"wrote {docs_dir / 'index.html'}")
    print(
        f"BOARD trades={s.get('trades')} W={s.get('wins')} L={s.get('losses')} "
        f"WR={s.get('win_rate_pct')} net={s.get('net_pnl')} "
        f"start={board.get('starting_fund')} deposits={s.get('deposits')} "
        f"capital={s.get('total_deposited')} total={s.get('total_funds')} "
        f"total_pnl={s.get('total_pnl_usd')} ({s.get('total_pnl_pct')}%) "
        f"twr%={s.get('twr_pct')} on_capital%={s.get('net_on_capital_pct')} "
        f"recon_residual={s.get('recon_residual')}"
    )

    do_publish = args.publish or (
        (os.environ.get("PUBLISH_MODEL_B_BOARD") or "").strip().lower() in ("1", "true", "yes")
        and not args.no_publish
    )
    if do_publish:
        try:
            url = publish_board(board, html)
            print(f"published {url}")
        except FileNotFoundError:
            print("publish skipped: gh CLI not found")
        except Exception as exc:
            print(f"publish failed: {exc}")
            return 1
    else:
        print(f"public board (run with --publish to refresh): {PUBLIC_BOARD_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

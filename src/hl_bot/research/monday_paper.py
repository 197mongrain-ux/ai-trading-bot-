"""Two paper books for Monday. No live orders and no private key.

Book ``default`` is the unset swing paper spec (PR #20). Book ``touch4h``
is a touch/reclaim at major 4h levels, 15m confirm, no macro filter, a
stop 1.5×ATR beyond the level, a fixed 3R target, and a 16:00 flatten
unless the trade is at least +1R. Both keep the tape gates on and write
a separate journal.

Start both against Hyperliquid mainnet public data::

    python -m hl_bot.research.monday_paper start

Stop both::

    python -m hl_bot.research.monday_paper stop

Per-book summary, plus the Book B size table::

    python -m hl_bot.research.monday_paper board

Journals land in ``logs/monday/default.jsonl`` and
``logs/monday/touch4h.jsonl``. The process does not read ``HL_PRIVATE_KEY``
and does not load a ``.env``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

BOOKS = ("default", "touch4h")
RUNTIME = Path("logs/monday")

# Inherited from the shell only so the child can find Python. Trading
# settings below replace anything the parent process had set.
_KEEP = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONNOUSERSITE",
    "LD_LIBRARY_PATH",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "TMPDIR",
)

COMMON = {
    "TRADING_MODE": "paper",
    "I_UNDERSTAND_LIVE_TRADING": "false",
    "HL_NETWORK": "mainnet",
    "ENTRY_MODE": "model_b",
    "MODEL_B_STYLE": "swing",
    "MODEL_B_PAPER": "1",
    "LEVERAGE": "20",
    "RISK_PER_TRADE": "0.01",
    "MODEL_B_SWING_FLOW": "tape",
    "STARTING_EQUITY": "5000",
    "HL_BOT_SKIP_DOTENV": "1",
    "PYTHONUNBUFFERED": "1",
}

# Book B only. Unset on Book A, so that process stays on the spec defaults.
TOUCH4H = {
    "MODEL_B_SWING_ENTRY": "touch",
    "MODEL_B_SWING_MAJOR": "1",
    "MODEL_B_SWING_LEVEL_SET": "4h",
    "MODEL_B_SWING_MIN_TOUCHES": "2",
    "MODEL_B_SWING_CONFIRM": "15m",
    "MODEL_B_SWING_MACRO": "none",
    "MODEL_B_SWING_STOP_ANCHOR": "level",
    "MODEL_B_SWING_ATR_FRAC": "1.5",
    "MODEL_B_SWING_TARGET": "fixed",
    "MODEL_B_SWING_MIN_R": "1",
    "MODEL_B_SWING_RR_MAX": "3",
    "MODEL_B_SWING_HOLD_OVERNIGHT": "be",
    "MODEL_B_SWING_MAX_LEV": "10",
    "MODEL_B_SWING_MAX_POSITIONS": "1",
    "MODEL_B_SWING_SESSION": "09:30-16:00",
}

_GATE_KEYS = ("absorb", "print_count", "window_delta", "last_15s_delta")


def runtime_dir(path: Path | None = None) -> Path:
    return Path(path) if path is not None else RUNTIME


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def book_env(name: str, *, runtime: Path | None = None) -> dict[str, str]:
    """Environment for one paper book. No private key, no inherited swing flags."""
    if name not in BOOKS:
        raise ValueError(f"unknown book {name!r}")
    env = {key: os.environ[key] for key in _KEEP if os.environ.get(key)}
    env.update(COMMON)
    if name == "touch4h":
        env.update(TOUCH4H)
    folder = runtime_dir(runtime)
    env["MODEL_B_JOURNAL_PATH"] = str(folder / f"{name}.jsonl")
    env.pop("HL_PRIVATE_KEY", None)
    env.pop("HL_ACCOUNT_ADDRESS", None)
    return env


def book_b_size_rows() -> list[dict]:
    """BTC size at Book B rules. Entry is the level. Stop is 1.5× the 1h ATR beyond it.

    Two example ATRs at a $100,000 price. 40 bp ATR is a $600 stop (60 bp).
    80 bp ATR is a $1,200 stop (120 bp). Slip reserved is the main-book 25 bp.
    Loss includes that slip and fees, and is capped at 1% (the 2% hard cap
    is wider than the risk, so it does not bind first).
    """
    from hl_bot.strategy.model_b.swing import SwingParams, size_swing

    params = SwingParams(
        max_lev=10,
        entry="touch",
        stop_anchor="level",
        target="fixed",
        atr_frac=1.5,
        max_r=3.0,
    )
    price = 100_000.0
    rows: list[dict] = []
    for atr_bp, atr in ((40.0, price * 0.0040), (80.0, price * 0.0080)):
        stop = price - 1.5 * atr
        stop_bp = (price - stop) / price * 10_000.0
        for equity in (250.0, 5000.0):
            size, loss = size_swing(
                "BTC",
                price,
                stop,
                equity,
                params,
                risk_pct=0.01,
                leverage=20,
                max_leverage=20,
            )
            notional = float(size) * price
            rows.append(
                {
                    "book": "touch4h",
                    "coin": "BTC",
                    "price": price,
                    "atr_bp": atr_bp,
                    "stop_bp": stop_bp,
                    "equity": equity,
                    "size": float(size),
                    "notional": notional,
                    "leverage": (notional / equity) if equity else 0.0,
                    "risk_dollars": float(loss),
                    "risk_pct": (float(loss) / equity) if equity else 0.0,
                }
            )
    return rows


def render_size_table(rows: list[dict] | None = None) -> str:
    body = rows if rows is not None else book_b_size_rows()
    lines = [
        "Book B size, BTC at 100000, stop = level + 1.5×ATR, 25 bp slip, fees, 10x cap, 1% risk, 2% hard cap",
        f"{'equity':>8} {'atr_bp':>8} {'stop_bp':>8} {'size':>12} {'notional':>12} {'lev':>8} {'risk$':>10} {'risk%':>8}",
    ]
    for row in body:
        lines.append(
            f"{row['equity']:8.0f} {row['atr_bp']:8.0f} {row['stop_bp']:8.1f} "
            f"{row['size']:12.6f} {row['notional']:12.2f} {row['leverage']:8.2f} "
            f"{row['risk_dollars']:10.2f} {row['risk_pct'] * 100:7.2f}%"
        )
    return "\n".join(lines)


def _pid_path(folder: Path, name: str) -> Path:
    return folder / f"{name}.pid"


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _book_summary(name: str, folder: Path) -> str:
    pid = _read_pid(_pid_path(folder, name))
    state = f"running pid {pid}" if _alive(pid) else "stopped"
    journal = folder / f"{name}.jsonl"
    rows = _read_jsonl(journal)
    counts = {"model_b_arm": 0, "model_b_fail": 0, "model_b_cancel": 0, "open": 0, "close": 0}
    open_coins: dict[str, int] = {}
    realized = 0.0
    slips: list[float] = []
    gate_rows = 0
    gate_hit = 0
    for row in rows:
        event = str(row.get("event") or "")
        if event in counts:
            counts[event] += 1
        if event in {"model_b_arm", "model_b_fail", "model_b_cancel"}:
            gate_rows += 1
            if all(key in row for key in _GATE_KEYS):
                gate_hit += 1
        coin = str(row.get("coin") or row.get("symbol") or "")
        if event == "open" and coin:
            open_coins[coin] = open_coins.get(coin, 0) + 1
            slip = row.get("entry_slip_bps")
            if isinstance(slip, (int, float)):
                slips.append(float(slip))
        elif event == "close" and coin:
            open_coins[coin] = open_coins.get(coin, 0) - 1
            pnl = row.get("pnl")
            if isinstance(pnl, (int, float)):
                realized += float(pnl)
    held = [coin for coin, count in sorted(open_coins.items()) if count > 0]
    equity = 5000.0
    meta = folder / f"{name}.env.json"
    if meta.exists():
        try:
            saved = json.loads(meta.read_text(encoding="utf-8"))
            equity = float(saved.get("STARTING_EQUITY") or equity)
        except (ValueError, TypeError):
            pass
    equity += realized
    slip_txt = "n=0" if not slips else f"n={len(slips)} mean={sum(slips) / len(slips):.2f} bp"
    lines = [
        f"{name}: {state}",
        f"  journal {journal}",
        (
            f"  arms {counts['model_b_arm']}  fails {counts['model_b_fail']}  "
            f"cancels {counts['model_b_cancel']}  opens {counts['open']}  closes {counts['close']}"
        ),
        f"  open {', '.join(held) if held else 'flat'}",
        f"  realized {realized:.2f}  equity {equity:.2f}",
        f"  gates {gate_hit}/{gate_rows} rows carry absorb, print_count, window_delta, last_15s_delta",
        f"  entry slip (print vs signal, + = worse) {slip_txt}",
    ]
    return "\n".join(lines)


def board(runtime: Path | None = None) -> str:
    folder = runtime_dir(runtime)
    parts = [_book_summary(name, folder) for name in BOOKS]
    parts.append(render_size_table())
    return "\n".join(parts) + "\n"


def start(runtime: Path | None = None) -> list[int]:
    """Start both books. Refuses when either pid is already alive."""
    folder = runtime_dir(runtime)
    folder.mkdir(parents=True, exist_ok=True)
    alive = [name for name in BOOKS if _alive(_read_pid(_pid_path(folder, name)))]
    if alive:
        raise SystemExit(f"already running: {', '.join(alive)}. Stop them first.")
    pids: list[int] = []
    root = str(_repo_root())
    for name in BOOKS:
        env = book_env(name, runtime=folder)
        (folder / f"{name}.env.json").write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
        log_path = folder / f"{name}.log"
        with log_path.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "hl_bot", "run"],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _pid_path(folder, name).write_text(str(proc.pid), encoding="utf-8")
        pids.append(proc.pid)
    return pids


def _signal_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
        return
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def stop(runtime: Path | None = None) -> list[str]:
    folder = runtime_dir(runtime)
    notes: list[str] = []
    for name in BOOKS:
        path = _pid_path(folder, name)
        pid = _read_pid(path)
        if pid is None:
            notes.append(f"{name}: not running")
        elif not _alive(pid):
            notes.append(f"{name}: already stopped")
        else:
            _signal_pid(pid)
            notes.append(f"{name}: stopped pid {pid}")
        path.unlink(missing_ok=True)
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monday dual paper books. No live orders.")
    parser.add_argument("command", choices=("start", "stop", "board"))
    parser.add_argument("--runtime", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "start":
        pids = start(args.runtime)
        print("started " + " ".join(f"{name}={pid}" for name, pid in zip(BOOKS, pids)))
    elif args.command == "stop":
        print("\n".join(stop(args.runtime)))
    else:
        print(board(args.runtime), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

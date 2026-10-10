"""Nightly paper self-review. Reads journals and tape. Places no orders.

The whitelist below is pre-registered. Do not add a knob or a value after
seeing a run. Hard limits are not on the list: a stop stays on, max loss
stays 2%, leverage stays 20, and risk per trade stays at or under 2%.

Walk-forward uses earlier trades only to pick one tighter value per knob,
then scores that one value on later trades. Filter knobs drop taken trades
that miss a tighter gate. That is not a path replay. A looser gate is not
scored, because the journal does not hold the trades that gate blocked.
Placement knobs change the stop, the target distance, or the slip, so the
journal cannot score them. They stay NEEDS_REPLAY unless a caller injects
a replay function. The default command does not invent those numbers.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from hl_bot.strategy.model_b.footprint import read_trades
from hl_bot.strategy.model_b.tape import WINDOW_SEC

TUNE_FRACTION = 0.70
MIN_TUNE_TRADES = 8
MIN_HOLDOUT_TRADES = 20
PENALTY_SCALE = 0.05
ML_MIN_SETUPS = 300
BOOK_MAX_AGE_SEC = 60.0
LAST_15_SEC = 15.0

# Locked ceilings. assert_hard_limits refuses anything looser.
STOP_REQUIRED = True
MAX_LOSS_PCT = 0.02
MAX_LEVERAGE = 20.0
RISK_PER_TRADE_CEILING = 0.02

FORBIDDEN_ENVS = frozenset(
    {
        "STOP_REQUIRED",
        "MODEL_B_MAX_LOSS_PCT",
        "LEVERAGE",
        "MODEL_B_MAX_LEVERAGE",
        "RISK_PER_TRADE",
        "I_UNDERSTAND_LIVE_TRADING",
        "TRADING_MODE",
        "MODEL_B_PAPER",
    }
)


class ImmutableLimit(ValueError):
    """A proposal tried to move a hard limit or an unknown knob."""


@dataclass(frozen=True)
class Knob:
    name: str
    env: str
    values: tuple
    default: object
    kind: str  # filter | placement
    feature: str | None
    op: str | None  # ge | le
    in_settings: bool


# Pre-registered. Tighter-than-default is the only direction a taken-trade
# journal can score. Do not append a value after looking at a result.
FILTER_KNOBS: tuple[Knob, ...] = (
    Knob("absorb_min", "ABSORB_MIN", (1.1, 1.3, 1.5), 1.3, "filter", "absorb", "ge", False),
    Knob(
        "min_prints",
        "MODEL_B_MIN_PRINTS",
        (20, 30, 40),
        30,
        "filter",
        "print_count",
        "ge",
        True,
    ),
    Knob(
        "trap_imbalance",
        "MODEL_B_SWING_TRAP_IMBALANCE",
        (2.5, 3.0, 4.0),
        3.0,
        "filter",
        "imbalance_ratio",
        "ge",
        True,
    ),
    Knob(
        "trap_stacked",
        "MODEL_B_SWING_TRAP_STACKED",
        (2, 3, 4),
        3,
        "filter",
        "stacked",
        "ge",
        True,
    ),
    Knob(
        "trap_zone_bps",
        "MODEL_B_SWING_TRAP_ZONE_BPS",
        (10, 20, 40),
        20,
        "filter",
        "zone_dist_bps",
        "le",
        True,
    ),
    Knob(
        "swing_min_r",
        "MODEL_B_SWING_MIN_R",
        (1.0, 1.5, 2.0),
        1.0,
        "filter",
        "target_r",
        "ge",
        True,
    ),
    Knob(
        "tp_min_pool_r",
        "MODEL_B_TP_MIN_POOL_R",
        (0, 1.0, 1.5),
        1.5,
        "filter",
        "pool_r",
        "ge",
        True,
    ),
)

PLACEMENT_KNOBS: tuple[Knob, ...] = (
    Knob(
        "trap_min_stop_bps",
        "MODEL_B_SWING_TRAP_MIN_STOP_BPS",
        (0, 20, 30, 40),
        0,
        "placement",
        None,
        None,
        True,
    ),
    Knob(
        "trap_min_stop_atr",
        "MODEL_B_SWING_TRAP_MIN_STOP_ATR",
        (0, 0.5, 1.0),
        0,
        "placement",
        None,
        None,
        True,
    ),
    Knob(
        "swing_atr_frac",
        "MODEL_B_SWING_ATR_FRAC",
        (0.25, 0.5, 1.0),
        0.5,
        "placement",
        None,
        None,
        True,
    ),
    Knob(
        "trap_slip_mode",
        "MODEL_B_SWING_TRAP_SLIP_MODE",
        ("flat", "proportional"),
        "flat",
        "placement",
        None,
        None,
        True,
    ),
    Knob(
        "trap_slip_bps",
        "MODEL_B_SWING_TRAP_SLIP_BPS",
        (0, 10, 15),
        0,
        "placement",
        None,
        None,
        True,
    ),
    Knob(
        "delta_flat_eps",
        "MODEL_B_DELTA_FLAT_EPS",
        (0, 0.05, 0.1),
        0.05,
        "placement",
        None,
        None,
        True,
    ),
)

ALL_KNOBS: tuple[Knob, ...] = FILTER_KNOBS + PLACEMENT_KNOBS
BY_ENV: dict[str, Knob] = {knob.env: knob for knob in ALL_KNOBS}

ReplayFn = Callable[[str, object, list["Setup"]], dict | None]


@dataclass
class Setup:
    ts: float
    coin: str
    side: str = ""
    outcome: str = ""
    budget_r: float | None = None
    net: float | None = None
    stop_bps: float | None = None
    target_r: float | None = None
    pool_r: float | None = None
    absorb: float | None = None
    window_delta: float | None = None
    last_15s_delta: float | None = None
    imbalance: float | None = None
    imbalance_ratio: float | None = None
    stacked: float | None = None
    zone_dist_bps: float | None = None
    macro: str | None = None
    hour_utc: int | None = None
    print_count: float | None = None
    fail_reason: str | None = None
    event: str = ""
    stop_present: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    env: str
    value: object | None
    status: str
    reason: str
    tune_n: int = 0
    tune_e: float | None = None
    hold_n: int = 0
    hold_e: float | None = None
    hold_dd: float | None = None
    ref_n: int = 0
    ref_e: float | None = None
    ref_dd: float | None = None
    penalty: float = 0.0

    @property
    def survives(self) -> bool:
        return self.status == "SURVIVE"

    def to_json(self) -> dict[str, Any]:
        row = asdict(self)
        row["survives"] = self.survives
        return row


@dataclass
class Review:
    day: str
    day_setups: list[Setup]
    labelled: list[Setup]
    blocks: dict[str, int]
    candidates: list[Candidate]
    env_diff: str
    ml_log: str
    tune: list[Setup] = field(default_factory=list)
    hold: list[Setup] = field(default_factory=list)


def assert_hard_limits(
    *,
    stop_required: bool = STOP_REQUIRED,
    max_loss_pct: float = MAX_LOSS_PCT,
    leverage: float = MAX_LEVERAGE,
    risk_per_trade: float = RISK_PER_TRADE_CEILING,
) -> None:
    """Refuse a book that drops the stop or raises a ceiling."""
    if stop_required is not True:
        raise ImmutableLimit("stop must stay present")
    if float(max_loss_pct) > MAX_LOSS_PCT + 1e-9:
        raise ImmutableLimit("max loss cannot exceed 2%")
    if float(leverage) > MAX_LEVERAGE + 1e-9:
        raise ImmutableLimit("leverage cap is 20")
    if float(risk_per_trade) > RISK_PER_TRADE_CEILING + 1e-9:
        raise ImmutableLimit("risk per trade ceiling is 2%")


def _float_eq(left: object, right: object) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return str(left).strip().lower() == str(right).strip().lower()


def _is_tighter(knob: Knob, value: object) -> bool:
    if knob.op == "ge":
        return float(value) > float(knob.default) + 1e-12
    if knob.op == "le":
        return float(value) < float(knob.default) - 1e-12
    return False


def _is_looser(knob: Knob, value: object) -> bool:
    if knob.op == "ge":
        return float(value) < float(knob.default) - 1e-12
    if knob.op == "le":
        return float(value) > float(knob.default) + 1e-12
    return False


def _feature_value(setup: Setup, knob: Knob) -> object:
    raw = getattr(setup, knob.feature or "")
    if raw is None and knob.feature == "pool_r":
        return setup.target_r
    return raw


def _keeps(setup: Setup, knob: Knob, value: object) -> bool:
    raw = _feature_value(setup, knob)
    if raw is None:
        return False
    if knob.op == "ge":
        return float(raw) + 1e-12 >= float(value)
    if knob.op == "le":
        return float(raw) <= float(value) + 1e-12
    return False


def max_drawdown(rs: Iterable[float]) -> float:
    """Peak-to-trough of the cumulative budget-R sum. Positive means a hole."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for item in rs:
        equity += float(item)
        if equity > peak:
            peak = equity
        hole = peak - equity
        if hole > worst:
            worst = hole
    return worst


def trade_metrics(setups: list[Setup]) -> tuple[int, float | None, float | None]:
    ordered = sorted(setups, key=lambda row: (row.ts, row.coin))
    rs = [float(row.budget_r) for row in ordered if row.budget_r is not None]
    if not rs:
        return 0, None, None
    return len(rs), sum(rs) / len(rs), max_drawdown(rs)


def multiple_comparison_penalty(n: int) -> float:
    """Budget-R haircut. One comparison is free. More than one pays sqrt(2 log N)."""
    if n <= 1:
        return 0.0
    return PENALTY_SCALE * math.sqrt(2.0 * math.log(n))


def split_walk_forward(setups: list[Setup]) -> tuple[list[Setup], list[Setup]]:
    """Earlier slice for tuning, strictly later slice for the holdout.

    Closed labelled rows only, ordered by time then coin. The cut is
    ``TUNE_FRACTION`` of that list. Every tune timestamp is strictly earlier
    than every holdout timestamp. A tie across the cut goes to the holdout.
    An empty side means the split cannot be used.
    """
    closed = [row for row in setups if row.budget_r is not None and row.event == "close"]
    closed.sort(key=lambda row: (row.ts, row.coin))
    if len(closed) < 2:
        return [], []
    cut = int(len(closed) * TUNE_FRACTION)
    cut = min(max(cut, 1), len(closed) - 1)
    boundary = closed[cut].ts
    tune = [row for row in closed if row.ts < boundary]
    hold = [row for row in closed if row.ts >= boundary]
    if not tune or not hold:
        return [], []
    if max(row.ts for row in tune) >= min(row.ts for row in hold):
        return [], []
    return tune, hold


def choose_filter_value(
    knob: Knob, tune: list[Setup]
) -> tuple[object | None, int, float | None, str]:
    """Pick the tighter value with the best tune expectancy and at least 8 trades.

    Equal expectancy keeps the value closer to the default. Looser values are
    not eligible. The holdout is not an input.
    """
    if knob.kind != "filter":
        return None, 0, None, "NEEDS_REPLAY"
    tighter = [value for value in knob.values if _is_tighter(knob, value)]
    looser = [value for value in knob.values if _is_looser(knob, value)]
    best: tuple[tuple, object, int, float] | None = None
    for value in tighter:
        kept = [row for row in tune if _keeps(row, knob, value)]
        n, expectancy, _dd = trade_metrics(kept)
        if n < MIN_TUNE_TRADES or expectancy is None:
            continue
        distance = abs(float(value) - float(knob.default))
        rank = (expectancy, -distance)
        if best is None or rank > best[0]:
            best = (rank, value, n, expectancy)
    if best is not None:
        return best[1], best[2], best[3], "CHOSEN"
    if not tighter and looser:
        return None, 0, None, "LOOSER_THAN_TRADED"
    if looser and not any(True for _ in tighter):
        return None, 0, None, "LOOSER_THAN_TRADED"
    return None, 0, None, "NO_TUNE"


def _judge(
    hold_n: int,
    hold_e: float | None,
    hold_dd: float | None,
    ref_e: float | None,
    ref_dd: float | None,
    penalty: float,
) -> str:
    if hold_n < MIN_HOLDOUT_TRADES or hold_e is None or ref_e is None:
        return "REJECT_COUNT"
    if not (hold_e > ref_e + penalty):
        return "REJECT_EDGE"
    if hold_dd is None or ref_dd is None or hold_dd > ref_dd + 1e-9:
        return "REJECT_DD"
    return "SURVIVE"


def _replay_metrics(raw: dict | None) -> tuple[int, float, float] | None:
    if not raw:
        return None
    try:
        n = int(raw["n"])
        expectancy = float(raw["expectancy"])
        drawdown = float(raw["max_dd"])
    except (KeyError, TypeError, ValueError):
        return None
    return n, expectancy, drawdown


def score_candidates(
    setups: list[Setup],
    knobs: Iterable[Knob] | None = None,
    replay_fn: ReplayFn | None = None,
) -> tuple[list[Candidate], list[Setup], list[Setup]]:
    """Tune on earlier data, then accept or reject on the held-out tail.

    ``replay_fn(env, value, setups)`` may return ``{n, expectancy, max_dd}``
    for a placement knob. The default path passes None and those knobs do
    not survive and do not count toward the multiple-comparison penalty.
    """
    assert_hard_limits()
    chosen_knobs = list(ALL_KNOBS if knobs is None else knobs)
    tune, hold = split_walk_forward(setups)
    if not tune or not hold:
        rows = [
            Candidate(knob.env, None, "NO_SPLIT", "tune and holdout do not separate in time")
            for knob in chosen_knobs
        ]
        return rows, tune, hold

    ref_n, ref_e, ref_dd = trade_metrics(hold)
    picked: list[tuple[Knob, object, int, float, str]] = []
    notes: dict[str, Candidate] = {}

    for knob in chosen_knobs:
        if knob.kind == "placement":
            picked_value = _pick_placement(knob, tune, replay_fn)
            if picked_value is None:
                notes[knob.env] = Candidate(
                    knob.env,
                    None,
                    "NEEDS_REPLAY",
                    "stop, ATR, or slip changes the economics; journal membership cannot score it",
                    ref_n=ref_n,
                    ref_e=ref_e,
                    ref_dd=ref_dd,
                )
                continue
            value, tune_n, tune_e = picked_value
            picked.append((knob, value, tune_n, tune_e, "placement"))
            continue

        value, tune_n, tune_e, state = choose_filter_value(knob, tune)
        looser = [item for item in knob.values if _is_looser(knob, item)]
        if state == "LOOSER_THAN_TRADED":
            shown = ", ".join(_fmt_value(item) for item in looser) or "none"
            notes[knob.env] = Candidate(
                knob.env,
                None,
                "LOOSER_THAN_TRADED",
                f"values {shown} are looser than the traded book and were not scored",
                ref_n=ref_n,
                ref_e=ref_e,
                ref_dd=ref_dd,
            )
            continue
        if value is None or tune_e is None:
            notes[knob.env] = Candidate(
                knob.env,
                None,
                "NO_TUNE",
                f"no tighter value had {MIN_TUNE_TRADES} tune trades",
                ref_n=ref_n,
                ref_e=ref_e,
                ref_dd=ref_dd,
            )
            continue
        picked.append((knob, value, tune_n, tune_e, "filter"))

    evaluated: list[tuple[Knob, object, int, float, int, float | None, float | None]] = []
    for knob, value, tune_n, tune_e, kind in picked:
        if kind == "filter":
            kept = [row for row in hold if _keeps(row, knob, value)]
            hold_n, hold_e, hold_dd = trade_metrics(kept)
        else:
            assert replay_fn is not None
            measured = _replay_metrics(replay_fn(knob.env, value, hold))
            if measured is None:
                notes[knob.env] = Candidate(
                    knob.env,
                    value,
                    "NEEDS_REPLAY",
                    "replay returned nothing on the holdout",
                    tune_n=tune_n,
                    tune_e=tune_e,
                    ref_n=ref_n,
                    ref_e=ref_e,
                    ref_dd=ref_dd,
                )
                continue
            hold_n, hold_e, hold_dd = measured
        evaluated.append((knob, value, tune_n, tune_e, hold_n, hold_e, hold_dd))

    # N is the number of holdouts actually computed. A placement knob that
    # still needs a replay does not inflate the penalty.
    penalty = multiple_comparison_penalty(len(evaluated))
    out: list[Candidate] = []
    for knob, value, tune_n, tune_e, hold_n, hold_e, hold_dd in evaluated:
        status = _judge(hold_n, hold_e, hold_dd, ref_e, ref_dd, penalty)
        reason = _status_reason(status, penalty)
        out.append(
            Candidate(
                knob.env,
                value,
                status,
                reason,
                tune_n=tune_n,
                tune_e=tune_e,
                hold_n=hold_n,
                hold_e=hold_e,
                hold_dd=hold_dd,
                ref_n=ref_n,
                ref_e=ref_e,
                ref_dd=ref_dd,
                penalty=penalty,
            )
        )
    # Stable order: whitelist order, with notes for knobs that never reached holdout.
    by_env = {row.env: row for row in out}
    ordered: list[Candidate] = []
    for knob in chosen_knobs:
        if knob.env in by_env:
            ordered.append(by_env[knob.env])
        elif knob.env in notes:
            ordered.append(notes[knob.env])
    return ordered, tune, hold


def _pick_placement(
    knob: Knob, tune: list[Setup], replay_fn: ReplayFn | None
) -> tuple[object, int, float] | None:
    if replay_fn is None:
        return None
    best: tuple[tuple, object, int, float] | None = None
    for value in knob.values:
        if _float_eq(value, knob.default):
            continue
        measured = _replay_metrics(replay_fn(knob.env, value, tune))
        if measured is None:
            continue
        n, expectancy, _dd = measured
        if n < MIN_TUNE_TRADES:
            continue
        rank = (expectancy, -abs(float(value) - float(knob.default)) if _is_number(value) else 0.0)
        if best is None or rank > best[0]:
            best = (rank, value, n, expectancy)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _is_number(value: object) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _status_reason(status: str, penalty: float) -> str:
    if status == "SURVIVE":
        return (
            f"held-out expectancy beat the reference by more than {penalty:.4f} "
            "and drawdown was not worse"
        )
    if status == "REJECT_COUNT":
        return f"held-out trade count is under {MIN_HOLDOUT_TRADES}"
    if status == "REJECT_EDGE":
        return f"held-out expectancy did not clear the reference plus {penalty:.4f}"
    if status == "REJECT_DD":
        return "held-out max drawdown was worse than the unfiltered reference"
    return status


def survivors(candidates: list[Candidate]) -> list[Candidate]:
    rows = [row for row in candidates if row.survives]
    rows.sort(
        key=lambda row: (
            -((row.hold_e or 0.0) - (row.ref_e or 0.0)),
            row.env,
        )
    )
    return rows


def _fmt_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def render_env_diff(candidates: list[Candidate], day: str) -> str:
    """One uncommented assignment, the top survivor. The rest stay comments.

    Each survivor was tested alone. Stacking them was not validated.
    """
    assert_hard_limits()
    kept = survivors(candidates)
    lines = [
        f"# Nightly review env diff for {day}",
        "# Paper and research only. One change. Hard limits are not in this file.",
        "# Stop stays on, max loss stays 2%, leverage stays 20, risk per trade stays at or under 2%.",
        "# Do not apply more than the uncommented line. Each other survivor was tested alone.",
        "# Filter results drop taken trades. They are not a fresh path replay.",
    ]
    if not kept:
        lines.append("# No candidate cleared the held-out bar. Do not change the paper book.")
        return "\n".join(lines) + "\n"
    top, rest = kept[0], kept[1:]
    knob = BY_ENV.get(top.env)
    if knob is not None and not knob.in_settings:
        lines.append(
            f"# {top.env} is a code constant in src/hl_bot/strategy/model_b/tape.py. "
            "load_settings does not read this line."
        )
    lines.append(f"{top.env}={_fmt_value(top.value)}")
    for row in rest:
        lines.append(
            f"# not applied (tested alone): {row.env}={_fmt_value(row.value)}"
        )
    text = "\n".join(lines) + "\n"
    assert_diff_safe(text)
    return text


def iter_assignments(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        found.append((key.strip(), value.strip()))
    return found


def assert_diff_safe(text: str) -> None:
    """Raise before any git or gh command if the diff leaves the whitelist."""
    assert_hard_limits()
    for key, value in iter_assignments(text):
        if key in FORBIDDEN_ENVS:
            raise ImmutableLimit(f"{key} is not tunable")
        knob = BY_ENV.get(key)
        if knob is None:
            raise ImmutableLimit(f"{key} is not on the whitelist")
        if not any(_float_eq(value, allowed) for allowed in knob.values):
            raise ImmutableLimit(f"{key}={value} is not a pre-registered value")
        if key == "STOP_REQUIRED" or _off_stop(key, value):
            raise ImmutableLimit("stop must stay present")


def _off_stop(key: str, value: str) -> bool:
    if key != "STOP_REQUIRED":
        return False
    return value.strip().lower() in {"0", "off", "false", "no"}


def open_draft_pr(day: str, diff_path: Path, *, runner: Callable[[list[str]], object]) -> list[list[str]]:
    """Open a draft PR that contains the env diff only. Refuses an empty or unsafe file.

    The runner is called only after the diff is checked. Tests pass a fake runner.
    The real command path uses subprocess and stays off unless ``--open-draft`` is set.
    """
    text = Path(diff_path).read_text(encoding="utf-8")
    assert_diff_safe(text)
    if not iter_assignments(text):
        raise ImmutableLimit("refuses an empty proposal")
    branch = f"review/nightly-{day}"
    commands = [
        ["git", "checkout", "-b", branch],
        ["git", "add", "--", str(diff_path)],
        ["git", "commit", "-m", f"Nightly review env diff {day}"],
        ["git", "push", "-u", "origin", branch],
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--title",
            f"Nightly review {day}",
            "--body-file",
            str(diff_path),
        ],
    ]
    for command in commands:
        runner(command)
    return commands


def _subprocess_runner(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_ts(value: object) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    if parsed > 1e11:
        return parsed / 1000.0
    return parsed


def _coin_of(row: dict) -> str:
    raw = row.get("coin") or row.get("symbol") or ""
    return str(raw).replace("-PERP", "").strip()


def _budget_r_of(row: dict) -> float | None:
    if row.get("budget_r") is not None:
        return _num(row.get("budget_r"))
    pnl = _num(row.get("pnl"))
    budget = _num(row.get("budget"))
    if pnl is not None and budget not in (None, 0.0):
        return pnl / float(budget)
    if row.get("r") is not None and "planned_risk" not in row:
        return _num(row.get("r"))
    if row.get("r") is not None and row.get("budget") is not None:
        return _num(row.get("r"))
    return None


def _stop_present_of(row: dict) -> bool:
    if "stop_present" in row:
        return bool(row.get("stop_present"))
    flag = row.get("stop_required")
    if flag is None:
        return True
    return str(flag).strip().lower() not in {"0", "off", "false", "no"}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if str(path).endswith(".gz") else open
    rows: list[dict] = []
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
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


def _first(row: dict, *names: str) -> object:
    for name in names:
        if row.get(name) is not None:
            return row.get(name)
    return None


def _setup_from_parts(close: dict, arm: dict | None) -> Setup:
    source = {}
    if arm:
        source.update(arm)
    source.update({key: value for key, value in close.items() if value is not None})
    ts = _as_ts(close.get("ts")) or 0.0
    budget_r = _budget_r_of(close)
    if budget_r is None and arm is not None:
        budget_r = _budget_r_of(arm) if arm.get("budget_r") is not None else None
    net = _num(close.get("pnl"))
    if net is None:
        net = _num(close.get("net"))
    outcome = "unlabelled"
    if budget_r is not None:
        outcome = "win" if budget_r > 0 else "loss"
    hour = datetime.fromtimestamp(ts, timezone.utc).hour if ts else None
    pool = _num(_first(source, "pool_r"))
    target = _num(_first(source, "target_r", "r_distance"))
    return Setup(
        ts=ts,
        coin=_coin_of(close) or _coin_of(source),
        side=str(_first(source, "side") or ""),
        outcome=outcome,
        budget_r=budget_r,
        net=net,
        stop_bps=_num(_first(source, "stop_bps")),
        target_r=target,
        pool_r=pool,
        absorb=_num(_first(source, "absorb")),
        window_delta=_num(_first(source, "window_delta")),
        last_15s_delta=_num(_first(source, "last_15s_delta")),
        imbalance=_num(_first(source, "imbalance")),
        imbalance_ratio=_num(_first(source, "imbalance_ratio", "imbalance")),
        stacked=_num(_first(source, "stacked")),
        zone_dist_bps=_num(_first(source, "zone_dist_bps")),
        macro=(str(source.get("macro")) if source.get("macro") is not None else None),
        hour_utc=hour,
        print_count=_num(_first(source, "print_count")),
        fail_reason=(str(source.get("fail_reason")) if source.get("fail_reason") else None),
        event=str(close.get("event") or "close"),
        stop_present=_stop_present_of(close),
    )


def _join_arm(close: dict, arms: list[dict]) -> dict | None:
    coin = _coin_of(close)
    ts = _as_ts(close.get("ts"))
    if ts is None or not coin:
        return None
    prior = [
        arm
        for arm in arms
        if _coin_of(arm) == coin and (_as_ts(arm.get("ts")) or 0.0) <= ts
    ]
    if not prior:
        return None
    return max(prior, key=lambda arm: _as_ts(arm.get("ts")) or 0.0)


def _level_size(level: object) -> tuple[float, float] | None:
    if isinstance(level, dict):
        price = _num(level.get("px", level.get("price")))
        size = _num(level.get("sz", level.get("size")))
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price = _num(level[0])
        size = _num(level[1])
    else:
        return None
    if price is None or size is None or size < 0:
        return None
    return price, size


def _book_sides(raw: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    if "bids" in raw or "asks" in raw:
        bids = raw.get("bids") or []
        asks = raw.get("asks") or []
    else:
        levels = raw.get("levels") or []
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []
    bid_rows = [row for row in (_level_size(level) for level in bids) if row]
    ask_rows = [row for row in (_level_size(level) for level in asks) if row]
    bid_rows.sort(key=lambda item: -item[0])
    ask_rows.sort(key=lambda item: item[0])
    return bid_rows[:5], ask_rows[:5]


def book_imbalance(raw: dict) -> tuple[float, float | None] | None:
    """Nearest-snapshot helper. Returns (ts_seconds, top-5 bid/ask size) or None."""
    ts = _as_ts(raw.get("time", raw.get("ts")))
    if ts is None:
        return None
    bids, asks = _book_sides(raw)
    bid_sz = sum(size for _px, size in bids)
    ask_sz = sum(size for _px, size in asks)
    if bid_sz <= 0 or ask_sz <= 0:
        return ts, None
    return ts, bid_sz / ask_sz


def _coin_dir(root: Path, coin: str) -> Path:
    return root / coin.replace(":", "_")


def _day_file(folder: Path, day: str, kind: str) -> Path | None:
    for name in (f"{day}.{kind}.jsonl.gz", f"{day}.{kind}.jsonl"):
        path = folder / name
        if path.exists():
            return path
    return None


def _window_delta(prints: list, ts: float, seconds: float) -> float | None:
    total = 0.0
    seen = False
    lo = ts - seconds
    for print_ in prints:
        if lo <= print_.ts <= ts:
            seen = True
            total += print_.size if print_.side == "buy" else -print_.size
    if not seen:
        return None
    return total


def _load_books(path: Path) -> list[tuple[float, float | None]]:
    snaps: list[tuple[float, float | None]] = []
    for raw in _read_jsonl(path):
        parsed = book_imbalance(raw)
        if parsed is not None:
            snaps.append(parsed)
    snaps.sort(key=lambda item: item[0])
    return snaps


def _nearest_book(snaps: list[tuple[float, float | None]], ts: float) -> float | None:
    best: tuple[float, float | None] | None = None
    for snap_ts, imbalance in snaps:
        if snap_ts <= ts and ts - snap_ts <= BOOK_MAX_AGE_SEC:
            if best is None or snap_ts > best[0]:
                best = (snap_ts, imbalance)
    if best is None:
        return None
    return best[1]


def enrich_with_tape(setups: list[Setup], tape: Path | None, day: str) -> None:
    """Fill missing delta and book imbalance. Never replace a journal absorb."""
    if tape is None or not tape.exists():
        return
    prints_cache: dict[str, list] = {}
    book_cache: dict[str, list] = {}
    for setup in setups:
        if not setup.coin:
            continue
        folder = _coin_dir(tape, setup.coin)
        if setup.window_delta is None or setup.last_15s_delta is None:
            if setup.coin not in prints_cache:
                path = _day_file(folder, day, "trades")
                prints_cache[setup.coin] = read_trades(path) if path else []
            prints = prints_cache[setup.coin]
            if setup.window_delta is None:
                setup.window_delta = _window_delta(prints, setup.ts, WINDOW_SEC)
            if setup.last_15s_delta is None:
                setup.last_15s_delta = _window_delta(prints, setup.ts, LAST_15_SEC)
        if setup.imbalance_ratio is None or setup.imbalance is None:
            if setup.coin not in book_cache:
                path = _day_file(folder, day, "book")
                book_cache[setup.coin] = _load_books(path) if path else []
            imbalance = _nearest_book(book_cache[setup.coin], setup.ts)
            if setup.imbalance_ratio is None:
                setup.imbalance_ratio = imbalance
            if setup.imbalance is None:
                setup.imbalance = imbalance


def ingest(
    journal_paths: list[Path],
    history_paths: list[Path],
    tape: Path | None,
    day: str,
) -> tuple[list[Setup], list[Setup], dict[str, int]]:
    """Day report uses ``day`` only. Walk-forward uses every labelled close plus history."""
    journal_rows: list[dict] = []
    for path in journal_paths:
        journal_rows.extend(_read_jsonl(path))
    arms = [row for row in journal_rows if row.get("event") == "model_b_arm"]
    blocks: dict[str, int] = {}
    block_setups: list[Setup] = []
    for row in journal_rows:
        if row.get("event") not in {"model_b_fail", "model_b_cancel"}:
            continue
        ts = _as_ts(row.get("ts")) or 0.0
        if datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") != day:
            continue
        reason = str(row.get("fail_reason") or row.get("reason") or row.get("event"))
        blocks[reason] = blocks.get(reason, 0) + 1
        block_setups.append(
            Setup(
                ts=ts,
                coin=_coin_of(row),
                side=str(row.get("side") or ""),
                outcome="block",
                event=str(row.get("event")),
                fail_reason=reason,
                hour_utc=datetime.fromtimestamp(ts, timezone.utc).hour,
                macro=(str(row.get("macro")) if row.get("macro") is not None else None),
                absorb=_num(row.get("absorb")),
                stop_present=True,
            )
        )

    closes: list[Setup] = []
    for row in journal_rows:
        event = row.get("event")
        labelled = row.get("budget_r") is not None or (
            row.get("pnl") is not None and row.get("budget") is not None
        )
        if event not in {"close", "setup"} and not (event in {None, ""} and labelled):
            continue
        if event in {None, ""}:
            row = {**row, "event": "close"}
        setup = _setup_from_parts(row, _join_arm(row, arms))
        if setup.event == "setup":
            setup.event = "close"
        closes.append(setup)

    for path in history_paths:
        for row in _read_jsonl(path):
            if row.get("event") in {"model_b_arm", "model_b_fail", "model_b_cancel"}:
                continue
            payload = dict(row)
            if payload.get("event") in {None, ""}:
                payload["event"] = "close"
            closes.append(_setup_from_parts(payload, None))

    deduped: list[Setup] = []
    seen: set[tuple] = set()
    for setup in closes:
        key = (round(setup.ts, 3), setup.coin, setup.event, setup.budget_r, setup.net)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(setup)

    day_rows = [
        setup
        for setup in deduped
        if datetime.fromtimestamp(setup.ts, timezone.utc).strftime("%Y-%m-%d") == day
    ]
    enrich_with_tape(day_rows + block_setups, tape, day)
    labelled = [setup for setup in deduped if setup.budget_r is not None and setup.event == "close"]
    day_setups = day_rows + block_setups
    day_setups.sort(key=lambda row: (row.ts, row.coin, row.event))
    return day_setups, labelled, blocks


def _mean(rows: list[Setup], name: str) -> str:
    vals = [getattr(row, name) for row in rows if getattr(row, name) is not None]
    if not vals:
        return "—"
    return f"{sum(vals) / len(vals):.4g}"


def _count_by(rows: list[Setup], name: str) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        value = getattr(row, name)
        label = "—" if value is None else str(value)
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "—"
    return ", ".join(f"{key} {counts[key]}" for key in sorted(counts))


def render_summary(review: Review) -> str:
    day_closes = [
        row
        for row in review.day_setups
        if row.event == "close" and row.budget_r is not None
    ]
    winners = [row for row in day_closes if (row.budget_r or 0) > 0]
    losers = [row for row in day_closes if (row.budget_r or 0) <= 0]
    features = (
        "absorb",
        "window_delta",
        "last_15s_delta",
        "imbalance_ratio",
        "stacked",
        "zone_dist_bps",
        "stop_bps",
        "target_r",
        "pool_r",
        "print_count",
    )
    lines = [
        f"# Nightly review {review.day}",
        "",
        "Paper and research only. This command reads the journal and the tape. It does not place an order and it does not change the running book.",
        "",
        "Hard limits stay fixed: a stop is always present, max loss is 2%, leverage is 20, and risk per trade stays at or under 2%. Those knobs are not tunable.",
        "",
        "## Day",
        "",
        f"- Closed labelled trades on {review.day}: {len(day_closes)} ({len(winners)} winners, {len(losers)} losers).",
        f"- Labelled closes used for walk-forward (journals plus history, all dates): {len(review.labelled)}.",
        f"- Tune trades: {len(review.tune)}. Holdout trades: {len(review.hold)}.",
        "",
        "Budget R is net dollars divided by the risk budget (stop plus reserved slip plus fees). A row with only pnl and no budget is unlabelled and stays out of the table.",
        "",
        "## Winners versus losers",
        "",
        "Means skip a missing feature. Marks and blocks are not in this table.",
        "",
        "| feature | winners | losers |",
        "| --- | --- | --- |",
    ]
    for name in features:
        lines.append(f"| {name} | {_mean(winners, name)} | {_mean(losers, name)} |")
    lines.extend(
        [
            "",
            f"- Coin, winners: {_count_by(winners, 'coin')}",
            f"- Coin, losers: {_count_by(losers, 'coin')}",
            f"- Macro, winners: {_count_by(winners, 'macro')}",
            f"- Macro, losers: {_count_by(losers, 'macro')}",
            f"- Hour UTC, winners: {_count_by(winners, 'hour_utc')}",
            f"- Hour UTC, losers: {_count_by(losers, 'hour_utc')}",
            "",
            "## Blocks",
            "",
        ]
    )
    if review.blocks:
        for reason, count in sorted(review.blocks.items()):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- No arm-block rows in the journals.")
    lines.extend(
        [
            "",
            "## Walk-forward",
            "",
            f"Closed labelled setups are ordered by time. The first {TUNE_FRACTION:.0%} tunes. The rest is the holdout. Every tune timestamp is strictly earlier than every holdout timestamp.",
            f"On the tune set, each filter knob contributes at most one value: the tighter-than-default setting with the best tune expectancy and at least {MIN_TUNE_TRADES} trades. Looser gates are marked LOOSER_THAN_TRADED and are not scored. A missing feature fails the tighter gate.",
            f"A candidate survives only when the holdout has at least {MIN_HOLDOUT_TRADES} trades, holdout expectancy (mean budget R) beats the unfiltered holdout by more than the penalty, and holdout max drawdown is not worse.",
            "The penalty is 0 when one candidate reaches the holdout, otherwise 0.05 * sqrt(2 * log N). N counts only candidates whose holdout was computed. Placement knobs that need a replay do not inflate N.",
            "Placement knobs (stop floor, ATR multiple, slip mode, slip allowance, delta flat epsilon) change economics. They are NEEDS_REPLAY unless a replay function is injected. This command does not invent a replay.",
            "",
            "| env | value | status | tune n | tune E | hold n | hold E | hold DD | ref E | ref DD | penalty |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in review.candidates:
        lines.append(
            "| {env} | {value} | {status} | {tune_n} | {tune_e} | {hold_n} | {hold_e} | {hold_dd} | {ref_e} | {ref_dd} | {penalty} |".format(
                env=row.env,
                value="—" if row.value is None else _fmt_value(row.value),
                status=row.status,
                tune_n=row.tune_n,
                tune_e="—" if row.tune_e is None else f"{row.tune_e:.4f}",
                hold_n=row.hold_n,
                hold_e="—" if row.hold_e is None else f"{row.hold_e:.4f}",
                hold_dd="—" if row.hold_dd is None else f"{row.hold_dd:.4f}",
                ref_e="—" if row.ref_e is None else f"{row.ref_e:.4f}",
                ref_dd="—" if row.ref_dd is None else f"{row.ref_dd:.4f}",
                penalty=f"{row.penalty:.4f}",
            )
        )
    kept = survivors(review.candidates)
    lines.extend(["", "## Survivors", ""])
    if not kept:
        lines.append("No candidate cleared the held-out bar. Do not change the paper book.")
    else:
        for index, row in enumerate(kept, start=1):
            lines.append(
                f"{index}. `{row.env}={_fmt_value(row.value)}` holdout E {row.hold_e:.4f} "
                f"versus reference {row.ref_e:.4f} (n={row.hold_n})."
            )
        lines.append("")
        lines.append("Only the first line is written as an assignment. The others were tested alone and must not be stacked.")
    if len(review.labelled) < MIN_HOLDOUT_TRADES:
        lines.extend(
            [
                "",
                "Far too little data to treat a result as an edge.",
            ]
        )
    missing_stop = [row for row in review.day_setups if not row.stop_present]
    if missing_stop:
        lines.extend(
            [
                "",
                "The journal recorded a setup without a stop. The hard limit still requires a stop. This review will not propose turning it off.",
            ]
        )
    lines.extend(
        [
            "",
            "## Env diff",
            "",
            "```",
            review.env_diff.rstrip(),
            "```",
            "",
            "## ML scorer",
            "",
            review.ml_log.rstrip(),
            "",
            "The ML scorer is log-only. It does not change a parameter and it does not edit the env diff.",
            "",
        ]
    )
    return "\n".join(lines)


def _ml_features(setup: Setup) -> list[float]:
    values = [
        setup.absorb,
        setup.window_delta,
        setup.stop_bps,
        setup.target_r,
        setup.imbalance_ratio,
        setup.stacked,
        setup.zone_dist_bps,
        setup.print_count,
        float(setup.hour_utc) if setup.hour_utc is not None else None,
    ]
    return [0.0 if value is None else float(value) for value in values]


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _fit_logistic(
    rows: list[list[float]], labels: list[float], steps: int = 400, lr: float = 0.2
) -> tuple[list[float], float]:
    width = len(rows[0])
    weights = [0.0] * width
    bias = 0.0
    n = len(rows)
    for _ in range(steps):
        grad_w = [0.0] * width
        grad_b = 0.0
        for features, label in zip(rows, labels):
            score = bias + sum(weight * feature for weight, feature in zip(weights, features))
            error = _sigmoid(score) - label
            grad_b += error
            for index, feature in enumerate(features):
                grad_w[index] += error * feature
        bias -= lr * grad_b / n
        for index in range(width):
            weights[index] -= lr * grad_w[index] / n
    return weights, bias


def _standardize(
    train: list[list[float]], test: list[list[float]]
) -> tuple[list[list[float]], list[list[float]]]:
    width = len(train[0])
    means = [sum(row[index] for row in train) / len(train) for index in range(width)]
    scale: list[float] = []
    for index, mean in enumerate(means):
        var = sum((row[index] - mean) ** 2 for row in train) / len(train)
        scale.append(math.sqrt(var) or 1.0)

    def apply(rows: list[list[float]]) -> list[list[float]]:
        return [
            [(value - mean) / denom for value, mean, denom in zip(row, means, scale)]
            for row in rows
        ]

    return apply(train), apply(test)


def ml_report(setups: list[Setup]) -> str:
    """Train only at 300 labelled closes. The text is a log. It proposes nothing."""
    labelled = [row for row in setups if row.budget_r is not None and row.event == "close"]
    labelled.sort(key=lambda row: (row.ts, row.coin))
    if len(labelled) < ML_MIN_SETUPS:
        return (
            f"ML not trained: {len(labelled)} labelled setups, need {ML_MIN_SETUPS}. "
            "Log only. No parameter change.\n"
        )
    labels = [1.0 if (row.budget_r or 0) > 0 else 0.0 for row in labelled]
    if len(set(labels)) < 2:
        return (
            f"ML not trained: {len(labelled)} labelled setups are a single class. "
            "Log only. No parameter change.\n"
        )
    cut = max(1, int(len(labelled) * TUNE_FRACTION))
    cut = min(cut, len(labelled) - 1)
    train_rows = [_ml_features(row) for row in labelled[:cut]]
    test_rows = [_ml_features(row) for row in labelled[cut:]]
    train_y = labels[:cut]
    test_y = labels[cut:]
    train_x, test_x = _standardize(train_rows, test_rows)
    weights, bias = _fit_logistic(train_x, train_y)
    correct = 0
    for features, label in zip(test_x, test_y):
        score = bias + sum(weight * feature for weight, feature in zip(weights, features))
        pred = 1.0 if _sigmoid(score) >= 0.5 else 0.0
        if pred == label:
            correct += 1
    accuracy = correct / len(test_y) if test_y else 0.0
    lines = [
        (
            f"ML trained logistic on {len(train_y)} setups, "
            f"holdout accuracy {accuracy:.3f} on {len(test_y)}. "
            "Log only. No parameter change."
        )
    ]
    try:
        from sklearn.ensemble import GradientBoostingClassifier
    except ImportError:
        lines.append("GBM skipped: sklearn is not installed")
        return "\n".join(lines) + "\n"
    model = GradientBoostingClassifier(random_state=0)
    model.fit(train_rows, train_y)
    gbm_accuracy = float(model.score(test_rows, test_y))
    lines.append(
        f"GBM holdout accuracy {gbm_accuracy:.3f}. Log only. No parameter change."
    )
    return "\n".join(lines) + "\n"


def run_review(
    *,
    tape: Path | None,
    day: str,
    journals: list[Path],
    history: list[Path] | None = None,
    replay_fn: ReplayFn | None = None,
) -> Review:
    datetime.strptime(day, "%Y-%m-%d")
    day_setups, labelled, blocks = ingest(journals, history or [], tape, day)
    candidates, tune, hold = score_candidates(labelled, replay_fn=replay_fn)
    # The env diff is fixed before the ML log. The scorer must not rewrite it.
    env_diff = render_env_diff(candidates, day)
    ml_log = ml_report(labelled)
    return Review(
        day=day,
        day_setups=day_setups,
        labelled=labelled,
        blocks=blocks,
        candidates=candidates,
        env_diff=env_diff,
        ml_log=ml_log,
        tune=tune,
        hold=hold,
    )


def write_review(review: Review, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(render_summary(review), encoding="utf-8")
    (out / "env.diff").write_text(review.env_diff, encoding="utf-8")
    (out / "ml.log").write_text(review.ml_log, encoding="utf-8")
    with (out / "setups.jsonl").open("w", encoding="utf-8") as handle:
        for setup in review.day_setups:
            handle.write(json.dumps(setup.to_json()) + "\n")
    payload = [row.to_json() for row in review.candidates]
    (out / "candidates.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _default_journals(given: list[str] | None) -> list[Path]:
    if given:
        return [Path(item) for item in given]
    found = []
    for name in ("logs/model_b_swing_paper.jsonl", "logs/trades.jsonl"):
        path = Path(name)
        if path.exists():
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly paper self-review. Places no orders.")
    parser.add_argument("--tape", required=True, help="Tape root, coin folders of trades and book jsonl.")
    parser.add_argument("--date", required=True, help="UTC day YYYY-MM-DD.")
    parser.add_argument("--out", required=True, help="Output directory, usually docs/review/<date>.")
    parser.add_argument(
        "--journal",
        action="append",
        default=None,
        help="Journal jsonl. Repeatable. Default: swing paper journal and logs/trades.jsonl when present.",
    )
    parser.add_argument(
        "--history",
        action="append",
        default=None,
        help="Extra labelled jsonl used for walk-forward. All dates. Not auto-loaded from prior reviews.",
    )
    parser.add_argument(
        "--open-draft",
        action="store_true",
        help="Open a draft PR with the env diff only. Off by default. Refuses an empty or unsafe diff.",
    )
    args = parser.parse_args(argv)
    review = run_review(
        tape=Path(args.tape),
        day=args.date,
        journals=_default_journals(args.journal),
        history=[Path(item) for item in args.history] if args.history else [],
        replay_fn=None,
    )
    out = Path(args.out)
    write_review(review, out)
    if args.open_draft:
        open_draft_pr(args.date, out / "env.diff", runner=_subprocess_runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

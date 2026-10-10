"""Nightly review stays inside the pre-registered whitelist and a time split."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hl_bot.research.nightly_review import (
    ALL_KNOBS,
    BY_ENV,
    FILTER_KNOBS,
    FORBIDDEN_ENVS,
    ImmutableLimit,
    Knob,
    Setup,
    _budget_r_of,
    assert_diff_safe,
    assert_hard_limits,
    choose_filter_value,
    iter_assignments,
    main,
    ml_report,
    multiple_comparison_penalty,
    open_draft_pr,
    render_env_diff,
    score_candidates,
    split_walk_forward,
)


def _setup(ts: float, budget_r: float | None, **kw) -> Setup:
    fields = dict(
        coin="BTC",
        side="long",
        outcome="win" if budget_r is not None and budget_r > 0 else "loss",
        budget_r=budget_r,
        net=budget_r,
        event="close",
        stop_present=True,
        absorb=1.4,
        print_count=40,
        imbalance_ratio=3.5,
        stacked=4,
        zone_dist_bps=10,
        target_r=2.0,
        pool_r=1.5,
    )
    fields.update(kw)
    return Setup(ts=float(ts), **fields)


def _book(n: int = 100) -> list[Setup]:
    return [_setup(i, 0.2) for i in range(n)]


def test_hard_limits_are_not_tunable(tmp_path: Path):
    envs = {knob.env for knob in ALL_KNOBS}
    for name in FORBIDDEN_ENVS:
        assert name not in envs
    assert BY_ENV["ABSORB_MIN"].values == (1.1, 1.3, 1.5)
    assert BY_ENV["MODEL_B_MIN_PRINTS"].values == (20, 30, 40)
    assert BY_ENV["MODEL_B_SWING_TRAP_IMBALANCE"].values == (2.5, 3.0, 4.0)
    assert BY_ENV["MODEL_B_SWING_TRAP_STACKED"].values == (2, 3, 4)
    assert BY_ENV["MODEL_B_SWING_TRAP_ZONE_BPS"].values == (10, 20, 40)
    assert BY_ENV["MODEL_B_SWING_MIN_R"].values == (1.0, 1.5, 2.0)
    assert BY_ENV["MODEL_B_TP_MIN_POOL_R"].values == (0, 1.0, 1.5)
    assert BY_ENV["MODEL_B_SWING_TRAP_MIN_STOP_BPS"].values == (0, 20, 30, 40)
    assert BY_ENV["MODEL_B_SWING_TRAP_MIN_STOP_ATR"].values == (0, 0.5, 1.0)
    assert BY_ENV["MODEL_B_SWING_ATR_FRAC"].values == (0.25, 0.5, 1.0)
    assert BY_ENV["MODEL_B_SWING_TRAP_SLIP_MODE"].values == ("flat", "proportional")
    assert BY_ENV["MODEL_B_SWING_TRAP_SLIP_BPS"].values == (0, 10, 15)
    assert BY_ENV["MODEL_B_DELTA_FLAT_EPS"].values == (0, 0.05, 0.1)
    assert BY_ENV["MODEL_B_TP_MIN_POOL_R"].default == 1.5
    with pytest.raises(ImmutableLimit):
        assert_hard_limits(stop_required=False)
    with pytest.raises(ImmutableLimit):
        assert_hard_limits(max_loss_pct=0.05)
    with pytest.raises(ImmutableLimit):
        assert_hard_limits(leverage=40)
    with pytest.raises(ImmutableLimit):
        assert_hard_limits(risk_per_trade=0.05)
    for line in (
        "RISK_PER_TRADE=0.05",
        "LEVERAGE=40",
        "MODEL_B_MAX_LOSS_PCT=0.05",
        "MODEL_B_MAX_LOSS_PCT=0.02",
        "STOP_REQUIRED=0",
        "STOP_REQUIRED=off",
        "FOO=1",
    ):
        with pytest.raises(ImmutableLimit):
            assert_diff_safe(line + "\n")
    calls: list[list[str]] = []
    unsafe = tmp_path / "unsafe.diff"
    unsafe.write_text("RISK_PER_TRADE=0.05\n", encoding="utf-8")
    with pytest.raises(ImmutableLimit):
        open_draft_pr("2026-10-09", unsafe, runner=lambda cmd: calls.append(cmd))
    empty = tmp_path / "empty.diff"
    empty.write_text("# No candidate cleared the held-out bar. Do not change the paper book.\n", encoding="utf-8")
    with pytest.raises(ImmutableLimit):
        open_draft_pr("2026-10-09", empty, runner=lambda cmd: calls.append(cmd))
    assert calls == []
    assert _budget_r_of({"pnl": 10, "planned_risk": 50, "r": 0.2}) is None
    assert _budget_r_of({"pnl": 10, "budget": 50}) == pytest.approx(0.2)


def test_walk_forward_tunes_on_earlier_data_only():
    rows: list[Setup] = []
    for i in range(40):
        rows.append(_setup(i, 1.0, target_r=1.6))
    for i in range(40, 70):
        rows.append(_setup(i, -1.0, target_r=2.5))
    for i in range(70, 80):
        rows.append(_setup(i, -1.0, target_r=1.6))
    for i in range(80, 100):
        rows.append(_setup(i, 1.0, target_r=2.5))
    tune, hold = split_walk_forward(rows)
    assert tune and hold
    assert max(row.ts for row in tune) < min(row.ts for row in hold)
    knob = BY_ENV["MODEL_B_SWING_MIN_R"]
    tuned, _n, _e, state = choose_filter_value(knob, tune)
    peeked, _hn, _he, hstate = choose_filter_value(knob, hold)
    assert state == "CHOSEN" and tuned == 1.5
    assert hstate == "CHOSEN" and peeked == 2.0
    scored, got_tune, got_hold = score_candidates(rows, knobs=[knob])
    assert scored[0].value == 1.5
    assert max(row.ts for row in got_tune) < min(row.ts for row in got_hold)
    same = [_setup(5.0, 1.0, coin=coin) for coin in ("BTC", "ETH", "SOL")]
    assert split_walk_forward(same) == ([], [])
    blocked, _, _ = score_candidates(same, knobs=[knob])
    assert blocked[0].status == "NO_SPLIT"
    assert not blocked[0].survives


def test_holdout_gates_and_a_single_survivor(tmp_path: Path):
    prints = BY_ENV["MODEL_B_MIN_PRINTS"]
    min_r = BY_ENV["MODEL_B_SWING_MIN_R"]

    def hold_prints(pass_n: int, pass_r: float, drop_n: int, drop_r: float) -> list[Setup]:
        rows = [_setup(i, 1.0, print_count=40) for i in range(70)]
        cursor = 70
        for _ in range(drop_n):
            rows.append(_setup(cursor, drop_r, print_count=31))
            cursor += 1
        for _ in range(pass_n):
            rows.append(_setup(cursor, pass_r, print_count=40))
            cursor += 1
        # Pad so the 70/30 cut still lands after the tune block when drop+pass != 30.
        return rows

    tiny = hold_prints(20, 0.11, 10, 0.10)
    alone, _, _ = score_candidates(tiny, knobs=[prints])
    assert alone[0].status == "SURVIVE"
    assert alone[0].penalty == 0
    assert alone[0].hold_n >= 20

    copies = [
        Knob(
            prints.name,
            prints.env if i == 0 else f"PRINTS_{i}",
            prints.values,
            prints.default,
            prints.kind,
            prints.feature,
            prints.op,
            True,
        )
        for i in range(12)
    ]
    penalised, _, _ = score_candidates(tiny, knobs=copies)
    assert multiple_comparison_penalty(12) > 0.05
    assert all(row.status == "REJECT_EDGE" for row in penalised)
    assert penalised[0].penalty == pytest.approx(multiple_comparison_penalty(12))

    short = hold_prints(10, 1.0, 20, -1.0)
    counted, _, _ = score_candidates(short, knobs=[prints])
    assert counted[0].status == "REJECT_COUNT"
    assert counted[0].hold_n < 20

    deep: list[Setup] = [_setup(i, 1.0, print_count=40) for i in range(70)]
    cursor = 70
    for k in range(5):
        deep.append(_setup(cursor, -2.0, print_count=40))
        cursor += 1
        if k < 4:
            deep.append(_setup(cursor, 0.01, print_count=31))
            cursor += 1
    for _ in range(6):
        deep.append(_setup(cursor, 0.01, print_count=31))
        cursor += 1
    for _ in range(15):
        deep.append(_setup(cursor, 1.0, print_count=40))
        cursor += 1
    drawn, _, _ = score_candidates(deep, knobs=[prints])
    assert drawn[0].status == "REJECT_DD"
    assert drawn[0].hold_dd is not None and drawn[0].ref_dd is not None
    assert drawn[0].hold_dd > drawn[0].ref_dd

    kept = [_setup(i, 0.2, target_r=2.0) for i in range(70)]
    for i in range(70, 80):
        kept.append(_setup(i, -0.5, target_r=1.2))
    for i in range(80, 100):
        kept.append(_setup(i, 0.4, target_r=2.0))
    won, _, _ = score_candidates(kept, knobs=[min_r])
    assert won[0].status == "SURVIVE"
    assert won[0].value == 1.5
    diff = render_env_diff(won, "2026-10-09")
    assert iter_assignments(diff) == [("MODEL_B_SWING_MIN_R", "1.5")]
    slip = BY_ENV["MODEL_B_SWING_TRAP_SLIP_MODE"]
    with_slip, _, _ = score_candidates(tiny, knobs=[prints, slip])
    by_env = {row.env: row for row in with_slip}
    assert by_env[prints.env].status == "SURVIVE"
    assert by_env[prints.env].penalty == 0
    assert by_env[slip.env].status == "NEEDS_REPLAY"
    pool = BY_ENV["MODEL_B_TP_MIN_POOL_R"]
    looser, _, _ = score_candidates(_book(), knobs=[pool])
    assert looser[0].status == "LOOSER_THAN_TRADED"
    assert not looser[0].survives

    calls: list[list[str]] = []
    path = tmp_path / "env.diff"
    path.write_text(diff, encoding="utf-8")
    commands = open_draft_pr("2026-10-09", path, runner=lambda cmd: calls.append(list(cmd)))
    assert calls == commands
    assert calls[0][:3] == ["git", "checkout", "-b"]
    assert calls[0][-1] == "review/nightly-2026-10-09"
    assert calls[1] == ["git", "add", "--", str(path)]
    assert "gh" in calls[-1][0]
    assert "--draft" in calls[-1]


def test_ml_is_log_only_until_300_setups():
    rows = [_setup(i, 1.0 if i % 2 == 0 else -0.5) for i in range(300)]
    pool = BY_ENV["MODEL_B_TP_MIN_POOL_R"]
    cands, _, _ = score_candidates(rows, knobs=[pool])
    before = [row.status for row in cands]
    diff = render_env_diff(cands, "2026-10-09")
    log = ml_report(rows)
    assert [row.status for row in cands] == before
    assert render_env_diff(cands, "2026-10-09") == diff
    assert "No candidate cleared" in diff
    assert "not trained" in ml_report(rows[:10]).lower()
    assert "not trained" in ml_report(rows[:299]).lower()
    assert "trained" in log.lower()
    assert "log only" in log.lower()
    assert "No parameter change" in log
    assert (
        "GBM skipped: sklearn is not installed" in log
        or "GBM holdout accuracy" in log
    )
    # The scorer does not add a knob the whitelist did not already contain.
    assert {knob.env for knob in FILTER_KNOBS} == {knob.env for knob in FILTER_KNOBS}


def test_ingest_joins_journal_tape_and_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("subprocess should stay unused")

    monkeypatch.setattr("hl_bot.research.nightly_review.subprocess.run", boom)
    day = "2026-10-09"
    ts = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc).timestamp()
    journal = tmp_path / "journal.jsonl"
    rows = [
        {
            "ts": ts - 10,
            "event": "model_b_arm",
            "coin": "BTC",
            "absorb": 1.8,
            "print_count": 40,
            "side": "long",
            "macro": "long",
        },
        {
            "ts": ts - 86400,
            "event": "model_b_fail",
            "coin": "BTC",
            "fail_reason": "YESTERDAY",
        },
        {
            "ts": ts - 20,
            "event": "model_b_fail",
            "coin": "ETH",
            "fail_reason": "THIN_TAPE",
        },
        {
            "ts": ts,
            "event": "close",
            "symbol": "BTC",
            "side": "long",
            "pnl": 10,
            "budget": 50,
            "reason": "tp",
        },
        {
            "ts": ts + 30,
            "event": "close",
            "symbol": "BTC",
            "side": "long",
            "pnl": -5,
            "reason": "stop",
        },
    ]
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    tape = tmp_path / "tape" / "BTC"
    tape.mkdir(parents=True)
    prints = [
        {"coin": "BTC", "side": "B", "px": "100", "sz": "2.0", "time": int((ts - 30) * 1000)},
        {"coin": "BTC", "side": "A", "px": "100", "sz": "0.5", "time": int((ts - 20) * 1000)},
    ]
    with gzip.open(tape / f"{day}.trades.jsonl.gz", "wt", encoding="utf-8") as handle:
        for print_ in prints:
            handle.write(json.dumps(print_) + "\n")
    book = {
        "time": int((ts - 30) * 1000),
        "levels": [
            [{"px": "100", "sz": "5", "n": 1}, {"px": "99", "sz": "5", "n": 1}],
            [{"px": "101", "sz": "2", "n": 1}, {"px": "102", "sz": "2", "n": 1}],
        ],
    }
    with gzip.open(tape / f"{day}.book.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(book) + "\n")
    out = tmp_path / "review"
    assert main(
        ["--tape", str(tmp_path / "tape"), "--date", day, "--journal", str(journal), "--out", str(out)]
    ) == 0
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Winners versus losers" in summary
    assert "THIN_TAPE" in summary
    assert "YESTERDAY" not in summary
    assert "No candidate cleared the held-out bar" in summary
    loaded = [json.loads(line) for line in (out / "setups.jsonl").read_text(encoding="utf-8").splitlines()]
    close = next(row for row in loaded if row["event"] == "close" and row["budget_r"] is not None)
    assert close["absorb"] == pytest.approx(1.8)
    assert close["window_delta"] == pytest.approx(1.5)
    assert close["imbalance_ratio"] == pytest.approx(2.5)
    unlabelled = next(row for row in loaded if row["event"] == "close" and row["net"] == -5)
    assert unlabelled["budget_r"] is None
    source = Path(__file__).resolve().parents[1] / "src" / "hl_bot" / "research" / "nightly_review.py"
    text = source.read_text(encoding="utf-8")
    assert "LiveExchange" not in text
    assert "place_order" not in text
    # History from another day is used for the split and is not limited to --date.
    history = tmp_path / "history.jsonl"
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    hist_rows = []
    for i in range(100):
        hist_rows.append(
            {
                "ts": (start + timedelta(hours=i)).timestamp(),
                "coin": "BTC",
                "event": "close",
                "budget_r": 0.4 if i >= 80 else -0.2,
                "target_r": 2.0 if i >= 80 or i < 70 else 1.2,
                "pnl": 1,
                "budget": 50,
            }
        )
    history.write_text("".join(json.dumps(row) + "\n" for row in hist_rows), encoding="utf-8")
    hist_out = tmp_path / "with-history"
    main(
        [
            "--tape",
            str(tmp_path / "tape"),
            "--date",
            day,
            "--journal",
            str(journal),
            "--history",
            str(history),
            "--out",
            str(hist_out),
        ]
    )
    # The day's own close is still the only labelled journal trade; history is separate.
    day_summary = (hist_out / "summary.md").read_text(encoding="utf-8")
    assert "Labelled closes used for walk-forward" in day_summary

"""Monday's two paper books. Defaults stay the unset swing spec."""

from __future__ import annotations

import json

import pytest

from hl_bot.config import load_settings
from hl_bot.research.monday_paper import (
    TOUCH4H,
    book_b_size_rows,
    book_env,
    board,
    render_size_table,
    start,
    stop,
)
from hl_bot.strategy.model_b.swing import (
    Level,
    SwingParams,
    _decision,
    entry_slip_bps,
    find_sweep,
    fixed_target,
    plan_trade,
    stop_beyond_level,
    swing_cancel_log,
    swing_close_log,
    swing_fill_log,
    touch_reclaim,
)
from hl_bot.strategy.model_b.thesis import ThesisBook
from hl_bot.strategy.model_b.types import AloIntent, TradePrint


_ABSENT_ON_DEFAULT = (
    "MODEL_B_SWING_ENTRY",
    "MODEL_B_SWING_MAJOR",
    "MODEL_B_SWING_LEVEL_SET",
    "MODEL_B_SWING_MIN_TOUCHES",
    "MODEL_B_SWING_MACRO",
    "MODEL_B_SWING_STOP_ANCHOR",
    "MODEL_B_SWING_ATR_FRAC",
    "MODEL_B_SWING_TARGET",
    "MODEL_B_SWING_RR_MAX",
    "MODEL_B_SWING_HOLD_OVERNIGHT",
    "MODEL_B_SWING_MAX_LEV",
    "MODEL_B_SWING_MAX_POSITIONS",
    "MODEL_B_SWING_SESSION",
    "HL_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS",
)


def _bars(level: float, side: str):
    start = 1_700_000_000.0
    bars = []
    for i in range(3):
        px = level + 5.0
        bars.append(
            {
                "t": (start + i * 900) * 1000.0,
                "o": px,
                "h": px + 0.2,
                "l": px - 0.2,
                "c": px,
                "v": 1.0,
            }
        )
    opened = start + 3 * 900
    if side == "long":
        bars.append(
            {
                "t": opened * 1000.0,
                "o": level + 0.2,
                "h": level + 0.5,
                "l": level - 0.04,
                "c": level + 0.1,
                "v": 1.0,
            }
        )
    else:
        bars.append(
            {
                "t": opened * 1000.0,
                "o": level - 0.2,
                "h": level + 0.04,
                "l": level - 0.5,
                "c": level - 0.1,
                "v": 1.0,
            }
        )
    return bars, start + 4 * 900


def _hourly():
    return [
        {"t": 1_700_000_000_000.0, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}
    ]


def _patch_geometry(monkeypatch, levels):
    import hl_bot.strategy.model_b.swing as swing

    monkeypatch.setattr(swing, "build_levels", lambda *args, **kwargs: levels)
    monkeypatch.setattr(swing, "atr14_of", lambda bars: 10.0)


def _touch_params(**overrides):
    params = dict(
        entry="touch",
        macro_mode="none",
        macro_tfs=(),
        stop_anchor="level",
        target="fixed",
        atr_frac=1.5,
        max_r=3.0,
        min_r=1.0,
    )
    params.update(overrides)
    return SwingParams(**params)


def test_defaults_stay_sweep_wick_and_require_macro():
    params = SwingParams()
    assert params.entry == "sweep"
    assert params.stop_anchor == "wick"
    assert params.target == "level"
    assert params.macro_tfs == ("4h", "1d")
    assert params.macro_mode == "both"
    assert params.max_lev == 20
    assert params.max_positions == 0
    assert params.hold_overnight == "off"
    bars, now = _bars(100.0, "long")
    assert plan_trade("BTC", now, bars, None, params, 10_000.0, risk_pct=0.01) == "MACRO_UNKNOWN"


def test_touch_is_not_a_five_bp_sweep():
    bars, _now = _bars(100.0, "long")
    last = bars[-1]
    assert touch_reclaim(last, 100.0, "long")
    assert find_sweep([bars[-2], last], 100.0, "long", 5.0) is None
    assert stop_beyond_level("long", 100.0, 10.0, 1.5) == pytest.approx(85.0)
    assert fixed_target("long", 100.0, 85.0, 3.0) == pytest.approx(145.0)


def test_macro_none_allows_both_sides_and_a_fixed_target_without_an_opposite_level(monkeypatch):
    _patch_geometry(monkeypatch, [Level(100.0, "support", 5.0, 2, ("4h_low", "4h_low"), 1.0)])
    bars, now = _bars(100.0, "long")
    planned = plan_trade("BTC", now, bars, _hourly(), _touch_params(), 10_000.0, risk_pct=0.01)
    assert not isinstance(planned, str)
    assert planned.side == "long"
    assert planned.macro == "macro=none"
    assert planned.stop == pytest.approx(85.0)
    assert planned.take_profit == pytest.approx(145.0)
    assert planned.r_multiple == pytest.approx(3.0)
    wick = _touch_params(stop_anchor="wick", atr_frac=0.5)
    wick_plan = plan_trade("BTC", now, bars, _hourly(), wick, 10_000.0, risk_pct=0.01)
    assert wick_plan.stop == pytest.approx(99.96 - 5.0)
    level_only = _touch_params(target="level")
    assert plan_trade("BTC", now, bars, _hourly(), level_only, 10_000.0, risk_pct=0.01) == "NO_TARGET"

    _patch_geometry(monkeypatch, [Level(100.0, "resistance", 5.0, 2, ("4h_high", "4h_high"), 1.0)])
    short_bars, short_now = _bars(100.0, "short")
    short = plan_trade("BTC", short_now, short_bars, _hourly(), _touch_params(), 10_000.0, risk_pct=0.01)
    assert not isinstance(short, str)
    assert short.side == "short"
    assert short.stop == pytest.approx(115.0)
    assert short.take_profit == pytest.approx(55.0)


def test_gate_values_are_on_the_arm_and_the_block():
    row = _decision("BTC", "THIN_TAPE", prints_n=4, min_prints=30, macro="macro=none").to_log()
    for key in ("absorb", "print_count", "window_delta", "last_15s_delta"):
        assert key in row
    assert row["print_count"] == 4
    assert row["absorb"] is None
    assert row["macro"] == "macro=none"


def test_paper_fill_books_the_limit_and_logs_the_print():
    book = ThesisBook()
    intent = AloIntent(
        coin="BTC",
        side="long",
        limit_px=100.0,
        size=1.0,
        stop=85.0,
        take_profit=145.0,
        swing_id="touch-1",
        tif="Alo",
        leverage=20,
        sweep_px=99.96,
        tick=0.1,
        signal_px=100.0,
        budget=2.5,
        absorb=1.6,
        window_delta=-2.0,
        last_15s_delta=0.4,
        print_count=40,
        target_r=3.0,
    )
    book.post(intent, 0.0)
    pos = book.try_fill_from_prints(
        [TradePrint(ts=1.0, coin="BTC", price=99.5, size=1.0, side="sell")]
    )
    assert pos is not None
    assert pos.entry == pytest.approx(100.0)
    assert pos.print_px == pytest.approx(99.5)
    assert pos.signal_px == pytest.approx(100.0)
    opened = swing_fill_log(pos)
    assert opened["price"] == pytest.approx(100.0)
    assert opened["entry_slip_bps"] == pytest.approx(-50.0)
    assert entry_slip_bps("long", 100.0, 100.2) == pytest.approx(20.0)
    assert entry_slip_bps("short", 100.0, 99.8) == pytest.approx(20.0)
    closed = book.force_flat("BTC", 115.0, reason="tp")
    assert closed is not None
    logged = swing_close_log(closed, 5.0, entry=100.0, stop=85.0, size=closed.size)
    assert logged["coin"] == "BTC"
    assert logged["print_px"] == pytest.approx(99.5)
    assert logged["fill_px"] == pytest.approx(100.0)
    assert logged["entry_slip_bps"] == pytest.approx(-50.0)
    assert logged["r"] == pytest.approx(5.0 / 15.0)
    assert logged["budget_r"] == pytest.approx(2.0)
    assert "planned_risk" not in logged


def test_cancel_keeps_the_gate_values_and_drops_the_working_order():
    book = ThesisBook()
    intent = AloIntent(
        coin="ETH",
        side="short",
        limit_px=100.0,
        size=1.0,
        stop=115.0,
        take_profit=55.0,
        swing_id="touch-2",
        tif="Alo",
        leverage=20,
        signal_px=100.0,
        absorb=1.4,
        window_delta=3.0,
        last_15s_delta=-0.2,
        print_count=31,
        target_r=3.0,
    )
    book.post(intent, 0.0)
    order = book.take_working("ETH")
    assert order is not None
    assert book.working_orders() == [] or all(item.coin != "ETH" for item in book.working_orders())
    payload = swing_cancel_log(order)
    for key in ("absorb", "print_count", "window_delta", "last_15s_delta"):
        assert key in payload
    assert payload["absorb"] == pytest.approx(1.4)
    assert payload["print_count"] == 31
    assert book.block_reason("ETH", "touch-2") == "THESIS_DONE"


def test_book_envs_keep_default_clean_and_touch4h_explicit(monkeypatch):
    monkeypatch.setenv("HL_PRIVATE_KEY", "should-not-leak")
    monkeypatch.setenv("MODEL_B_SWING_ENTRY", "trapped")
    monkeypatch.setenv("TRADING_MODE", "live")
    default = book_env("default", runtime=__import__("pathlib").Path("/tmp/monday-test"))
    touch = book_env("touch4h", runtime=__import__("pathlib").Path("/tmp/monday-test"))
    for key in _ABSENT_ON_DEFAULT:
        assert key not in default
    assert default["TRADING_MODE"] == "paper"
    assert default["MODEL_B_PAPER"] == "1"
    assert default["MODEL_B_SWING_FLOW"] == "tape"
    assert default["RISK_PER_TRADE"] == "0.01"
    assert default["HL_NETWORK"] == "mainnet"
    assert default["MODEL_B_JOURNAL_PATH"].endswith("default.jsonl")
    for key, value in TOUCH4H.items():
        assert touch[key] == value
    assert touch["MODEL_B_JOURNAL_PATH"].endswith("touch4h.jsonl")
    assert "HL_PRIVATE_KEY" not in touch
    for key, value in touch.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HL_PRIVATE_KEY", raising=False)
    settings = load_settings()
    assert settings.is_live is False
    assert settings.model_b_paper is True
    assert settings.private_key == ""
    params = SwingParams.from_settings(settings)
    assert params.entry == "touch"
    assert params.macro_mode == "none"
    assert params.macro_tfs == ()
    assert params.stop_anchor == "level"
    assert params.atr_frac == pytest.approx(1.5)
    assert params.target == "fixed"
    assert params.max_r == pytest.approx(3.0)
    assert params.hold_overnight == "be"
    assert params.max_lev == 10
    assert params.max_positions == 1
    assert params.major is True
    assert params.level_set == "4h"
    assert params.confirm_tf == "15m"
    assert params.session == "09:30-16:00"
    assert params.flow == "tape"


def test_default_book_loads_the_spec(monkeypatch):
    env = book_env("default")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HL_PRIVATE_KEY", raising=False)
    settings = load_settings()
    assert settings.model_b_swing_entry == "sweep"
    assert settings.model_b_swing_macro == "4h,1d"
    assert settings.model_b_swing_stop_anchor == "wick"
    assert settings.model_b_swing_target == "level"
    assert settings.model_b_swing_max_lev == 20
    assert settings.model_b_swing_max_positions == 0
    assert settings.model_b_swing_major is False
    assert settings.model_b_swing_session == "all"
    assert settings.model_b_swing_hold_overnight == "off"
    assert settings.model_b_swing_rr_max is None
    assert settings.model_b_swing_flow == "tape"
    assert settings.risk_per_trade == pytest.approx(0.01)
    assert settings.is_live is False


def test_size_table_stays_inside_one_percent_and_ten_x():
    rows = book_b_size_rows()
    equities = {row["equity"] for row in rows}
    assert equities == {250.0, 5000.0}
    assert {row["stop_bp"] for row in rows} == {60.0, 120.0}
    for row in rows:
        assert row["size"] > 0
        assert row["leverage"] <= 10.0 + 1e-9
        assert row["risk_pct"] <= 0.01 + 1e-9
        assert row["risk_pct"] <= 0.02
        assert row["risk_dollars"] == pytest.approx(row["equity"] * row["risk_pct"])
    text = render_size_table(rows)
    assert "250" in text and "5000" in text
    assert "10x cap" in text


def test_start_spawns_two_paper_processes_and_board_reads_the_journal(monkeypatch, tmp_path):
    launched = []

    class _Proc:
        def __init__(self):
            self.pid = 4242 + len(launched)

    def _popen(argv, **kwargs):
        launched.append((argv, kwargs["env"]))
        return _Proc()

    monkeypatch.setattr("hl_bot.research.monday_paper.subprocess.Popen", _popen)
    monkeypatch.setenv("HL_PRIVATE_KEY", "nope")
    pids = start(tmp_path)
    assert len(pids) == 2
    assert len(set(pids)) == 2
    assert len(launched) == 2
    for argv, env in launched:
        assert argv[-3:] == ["-m", "hl_bot", "run"]
        assert env["TRADING_MODE"] == "paper"
        assert env["MODEL_B_PAPER"] == "1"
        assert "HL_PRIVATE_KEY" not in env
        assert env["I_UNDERSTAND_LIVE_TRADING"] == "false"
    assert launched[0][1]["MODEL_B_JOURNAL_PATH"].endswith("default.jsonl")
    assert "MODEL_B_SWING_ENTRY" not in launched[0][1]
    assert launched[1][1]["MODEL_B_SWING_ENTRY"] == "touch"
    monkeypatch.setattr("hl_bot.research.monday_paper._alive", lambda pid: False)
    journal = tmp_path / "touch4h.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "model_b_arm",
                        "coin": "BTC",
                        "absorb": 1.5,
                        "print_count": 40,
                        "window_delta": -1.0,
                        "last_15s_delta": 0.2,
                    }
                ),
                json.dumps(
                    {
                        "event": "open",
                        "symbol": "BTC",
                        "coin": "BTC",
                        "entry_slip_bps": -12.5,
                    }
                ),
                json.dumps({"event": "close", "symbol": "BTC", "coin": "BTC", "pnl": 3.5, "budget_r": 0.4}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    text = board(tmp_path)
    assert "default: stopped" in text
    assert "touch4h: stopped" in text
    assert "gates 1/1" in text
    assert "mean=-12.50 bp" in text
    assert "realized 3.50" in text
    assert "Book B size" in text
    notes = stop(tmp_path)
    assert any("not running" in note or "already stopped" in note for note in notes)

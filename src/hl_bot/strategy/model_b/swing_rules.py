"""Pre-registered reclaim-bar and fail-fast scratch grid.

The cells below were fixed before any replay in this file. Nothing is added
or dropped after the second half is seen. A cell passes a book when its
expectancy beats that book's reference in both halves, on at least 4 of 6
coins, with at least 20 trades, and with neither half under 8 trades.
Profitable means that pass plus expectancy above zero in both halves.

The paper flags stay off unless the same cell is profitable on both books.
The 15m book is the spec baseline (what Monday paper runs). The 1h book is
the winner-profile book (1h confirm, 1h+4h ADX, 4h leads).

Run: ``python -m hl_bot.strategy.model_b.swing_rules --cache /tmp/hl_swing_cache``
"""

from __future__ import annotations

import json
from pathlib import Path

from hl_bot.strategy.model_b.swing_adjust import (
    BASELINE,
    STRUCT,
    _judge,
    _midpoint,
)
from hl_bot.strategy.model_b.swing_replay import Summary, _variant, load_cache, replay

# Reclaim: skip or retest when the bar is wider than X bps OR Y × ATR.
_RECLAIM_X = (40.0, 50.0, 60.0)
_RECLAIM_Y = (0.5, 0.75, 1.0)
# Time scratch: MFE has not reached T within W minutes.
_SCRATCH_MFE = (0.3, 0.5)
_SCRATCH_MINUTES = (60.0, 90.0, 120.0)
# MAE scratch: adverse hits M before +0.5R favorable. 0.5R is not gridded.
_SCRATCH_MAE = (0.6, 0.8)


def _grid() -> list[tuple[str, str, dict]]:
    """(name, family, params). Built with no trade results in hand."""
    rows: list[tuple[str, str, dict]] = []
    reclaim: list[tuple[str, dict]] = []
    for x in _RECLAIM_X:
        for y in _RECLAIM_Y:
            label = f"{x:.0f}bp-{y:g}atr"
            for mode in ("skip", "retest"):
                name = f"reclaim-{mode}-{label}"
                params = {
                    "reclaim_mode": mode,
                    "max_reclaim_bps": float(x),
                    "max_reclaim_atr": float(y),
                }
                reclaim.append((name, params))
                rows.append((name, "reclaim", params))
    scratch: list[tuple[str, dict]] = []
    for mfe in _SCRATCH_MFE:
        for minutes in _SCRATCH_MINUTES:
            name = f"scratch-mfe{mfe:g}-{minutes:.0f}m"
            params = {"scratch_mfe_r": float(mfe), "scratch_minutes": float(minutes)}
            scratch.append((name, params))
            rows.append((name, "scratch", params))
    for mae in _SCRATCH_MAE:
        name = f"scratch-mae{mae:g}"
        params = {"scratch_mae_r": float(mae)}
        scratch.append((name, params))
        rows.append((name, "scratch", params))
    for r_name, r_params in reclaim:
        for s_name, s_params in scratch:
            merged = dict(r_params)
            merged.update(s_params)
            rows.append((f"{r_name}+{s_name}", "combined", merged))
    return rows


def _reasons(summary: Summary) -> dict[str, int]:
    out: dict[str, int] = {}
    for trade in summary.blotter:
        out[trade.reason] = out.get(trade.reason, 0) + 1
    return out


_DATA = None


def _init_cache(path: str) -> None:
    global _DATA
    _DATA = load_cache(path)


def _run_one(spec: tuple) -> tuple[str, str, str, Summary]:
    book, family, name, params = spec
    summary = replay(_DATA, params, risk_pct=0.01, name=name)
    return book, family, name, summary


def _render(payload: dict) -> str:
    lines = [
        "# Reclaim cap and fail-fast scratch",
        "",
        "The grid was fixed before this run. A cell is a yes on a book when expectancy "
        "beats that book's reference in both halves, on at least 4 of 6 coins, with at least "
        "20 trades, and with neither half under 8 trades. Profitable also needs expectancy "
        "above zero in both halves. The paper flags stay off unless the same cell is profitable "
        "on both books. H1 of the 1h book is where the wide-reclaim clue came from; that half "
        "is not fresh evidence.",
        "",
        payload["note"],
        "",
        "## Verdict",
        "",
        payload["verdict"],
        "",
        "## Cells",
        "",
        "| book | family | cell | trades | win | E | net | DD | H1 n/E | H2 n/E | coins | <20 | thin | yes | profitable | exits |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in payload["rows"]:
        full = row["full"]
        exits = ",".join(f"{key}:{value}" for key, value in sorted(row["exits"].items()))
        lines.append(
            f"| {row['book']} | {row['family']} | {row['name']} | {full['trades']} | "
            f"{full['win_rate']:.1%} | {full['expectancy_r']:.3f} | {full['net_pct']:.2f}% | "
            f"{full['max_dd_pct']:.2f}% | {row['half1']['trades']}/{row['half1']['expectancy_r']:.3f} | "
            f"{row['half2']['trades']}/{row['half2']['expectancy_r']:.3f} | "
            f"{row['coins_better']} | {row['under_20']} | {row['thin_half']} | "
            f"{row['improves']} | {row['profitable']} | {exits} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from concurrent.futures import ProcessPoolExecutor, as_completed

    parser = argparse.ArgumentParser(description="Pre-registered reclaim and scratch grid")
    parser.add_argument("--cache", default="/tmp/hl_swing_cache")
    parser.add_argument("--out", default="docs/pnl/swing_rules.json")
    parser.add_argument("--report", default="docs/pnl/swing_rules.md")
    args = parser.parse_args(argv)
    data = load_cache(args.cache)
    if not data:
        print("no candles")
        return 1
    books = {"15m-52d": BASELINE, "1h-130d": STRUCT}
    print("replay references", flush=True)
    refs: dict[str, Summary] = {}
    mids: dict[str, float] = {}
    with ProcessPoolExecutor(max_workers=2, initializer=_init_cache, initargs=(args.cache,)) as pool:
        jobs = [(name, "ref", "ref", params) for name, params in books.items()]
        for book, _family, _name, summary in pool.map(_run_one, jobs):
            print(book, summary.row(), flush=True)
            refs[book] = summary
            mids[book] = _midpoint(data, books[book].confirm_tf)
    expected = {"15m-52d": 16, "1h-130d": 85}
    for name, n in expected.items():
        if refs[name].trades != n:
            print(f"reference {name} has {refs[name].trades} trades, expected {n}. aborting.")
            return 2

    grid = _grid()
    partial_path = Path("/tmp/hl_swing_cache/rules_partial.jsonl")
    done: dict[tuple[str, str], dict] = {}
    if partial_path.exists():
        for line in partial_path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            done[(row["book"], row["name"])] = row
    jobs = []
    for name, params in books.items():
        for cell, family, overrides in grid:
            if (name, cell) in done:
                continue
            jobs.append((name, family, cell, _variant(params, **overrides)))
    print(f"replay {len(jobs)} cells ({len(done)} already saved)", flush=True)
    rows = list(done.values())
    with partial_path.open("a") as partial, ProcessPoolExecutor(
        max_workers=4, initializer=_init_cache, initargs=(args.cache,)
    ) as pool:
        futs = [pool.submit(_run_one, job) for job in jobs]
        for fut in as_completed(futs):
            book, family, name, summary = fut.result()
            print(book, name, summary.row(), flush=True)
            judged = _judge(list(summary.blotter), list(refs[book].blotter), mids[book])
            judged["book"] = book
            judged["family"] = family
            judged["name"] = name
            judged["full"] = {
                "trades": summary.trades,
                "win_rate": summary.win_rate,
                "avg_win_r": summary.avg_win_r,
                "avg_loss_r": summary.avg_loss_r,
                "expectancy_r": summary.expectancy_r,
                "net_pct": summary.net_pct,
                "max_dd_pct": summary.max_dd_pct,
            }
            judged["exits"] = _reasons(summary)
            judged["under_20"] = summary.trades < 20
            rows.append(judged)
            partial.write(json.dumps(judged, default=str) + "\n")
            partial.flush()
    for book, summary in refs.items():
        judged = _judge(list(summary.blotter), list(summary.blotter), mids[book])
        judged["book"] = book
        judged["family"] = "ref"
        judged["name"] = "ref"
        judged["improves"] = False
        judged["profitable"] = False
        judged["full"] = {
            "trades": summary.trades,
            "win_rate": summary.win_rate,
            "avg_win_r": summary.avg_win_r,
            "avg_loss_r": summary.avg_loss_r,
            "expectancy_r": summary.expectancy_r,
            "net_pct": summary.net_pct,
            "max_dd_pct": summary.max_dd_pct,
        }
        judged["exits"] = _reasons(summary)
        judged["under_20"] = summary.trades < 20
        rows.append(judged)
    rows.sort(key=lambda row: (
        row["book"],
        {"ref": 0, "reclaim": 1, "scratch": 2, "combined": 3}.get(row["family"], 9),
        -row["full"]["expectancy_r"],
    ))

    by_key: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row["family"] == "ref":
            continue
        by_key.setdefault((row["family"], row["name"]), []).append(row)

    both_profitable = []
    one_book_yes = []
    for key, pair in by_key.items():
        flags = {row["book"]: row for row in pair}
        if all(flags.get(book, {}).get("profitable") for book in books):
            both_profitable.append(key[1])
        elif any(row["improves"] for row in pair):
            one_book_yes.append(key[1])

    families = ("reclaim", "scratch", "combined")
    family_hits = {family: [] for family in families}
    for (family, name), pair in by_key.items():
        if all(next((row["profitable"] for row in pair if row["book"] == book), False) for book in books):
            family_hits[family].append(name)

    by_name = {(row["book"], row["name"]): row for row in rows}
    clock_bits = []
    for book in ("1h-130d", "15m-52d"):
        checks = mismatches = 0
        for (b, name), row in by_name.items():
            if b != book or "-90m" not in name:
                continue
            other = by_name.get((book, name.replace("-90m", "-120m")))
            if other is None:
                continue
            checks += 1
            if (
                row["full"]["trades"] != other["full"]["trades"]
                or abs(row["full"]["expectancy_r"] - other["full"]["expectancy_r"]) > 1e-9
            ):
                mismatches += 1
        if checks == 0:
            continue
        if mismatches == 0:
            clock_bits.append(
                f"All {checks} {book} pairs of 90 and 120 minutes matched, "
                "because both windows first come due on the bar two hours after the fill."
            )
        else:
            clock_bits.append(
                f"{mismatches} of {checks} {book} pairs of 90 and 120 minutes differed. "
                "On 15m those windows are separate rules."
            )
    clock_note = " ".join(clock_bits)
    if both_profitable:
        verdict = (
            "YES on these cells, profitable on both books: "
            + ", ".join(both_profitable)
            + ". That is one look inside a "
            f"{len(grid)}-cell grid on two books. Do not retune the thresholds. "
            "Order flow was off. " + clock_note
        )
    else:
        verdict = (
            "NO on the reclaim cap, NO on the retest alternative, NO on the fail-fast scratch, "
            "and NO on the combination. No cell was profitable on both the 15m spec book and the "
            f"1h book. The grid was {len(grid)} cells, fixed before the run "
            f"({sum(1 for _n, fam, _p in grid if fam == 'reclaim')} reclaim, "
            f"{sum(1 for _n, fam, _p in grid if fam == 'scratch')} scratch, "
            f"{sum(1 for _n, fam, _p in grid if fam == 'combined')} combined). "
            "A cell that only improves one book, or that improves both halves and still loses, "
            "does not turn a flag on. "
            + clock_note + " "
            "The wide-reclaim clue was the first half of the 1h book (losers had wider reclaim bars). "
            "The second half of that book flipped, and it was not used to change the grid. "
            "MFE, MAE, and time-to-target are path stats. Using them as an exit is still a "
            "look at the same trades that suggested them. Order flow was off. Monday paper stays "
            "on the spec defaults."
        )
    note = (
        f"References: 15m-52d {refs['15m-52d'].trades} trades, "
        f"1h-130d {refs['1h-130d'].trades} trades. "
        f"Cells that improved a book but were not profitable on both: {len(one_book_yes)}."
    )
    payload = {
        "note": note,
        "verdict": verdict,
        "family_hits": family_hits,
        "both_profitable": both_profitable,
        "rows": rows,
        "grid_n": len(grid),
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, default=str))
    Path(args.report).write_text(_render(payload))
    print(verdict)
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

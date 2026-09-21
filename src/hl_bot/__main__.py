"""CLI entry: python -m hl_bot run"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hl_bot", description="Hyperliquid multi-symbol perp bot (BTC/SOL/XRP)"
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Start the trading loop")
    run_p.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop after N loops (useful for smoke tests)",
    )
    run_p.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Optional path to .env",
    )

    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 1

    from hl_bot.config import load_settings
    from hl_bot.execution.loop import run_bot

    settings = load_settings(args.env_file)
    if settings.is_live:
        print(
            "WARNING: LIVE TRADING MODE — real orders will be sent. "
            "Ctrl+C to abort.",
            file=sys.stderr,
        )
    else:
        print("PAPER mode — no live orders.", file=sys.stderr)
    print(f"Symbols: {', '.join(settings.symbols)}", file=sys.stderr)

    run_bot(settings, max_iterations=args.max_iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

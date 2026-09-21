"""CLI entry: python -m hl_bot run | dashboard"""

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

    dash_p = sub.add_parser("dashboard", help="Serve local live trading dashboard")
    dash_p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    dash_p.add_argument("--port", type=int, default=8787, help="Bind port (default 8787)")
    dash_p.add_argument(
        "--journal",
        default=None,
        help="Journal JSONL path (default JOURNAL_PATH or logs/trades.jsonl)",
    )
    dash_p.add_argument(
        "--reload",
        action="store_true",
        help="Dev auto-reload (uvicorn)",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
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

    if args.command == "dashboard":
        from dashboard.__main__ import main as dash_main

        dash_argv = ["--host", args.host, "--port", str(args.port)]
        if args.journal:
            dash_argv.extend(["--journal", args.journal])
        if args.reload:
            dash_argv.append("--reload")
        return dash_main(dash_argv)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

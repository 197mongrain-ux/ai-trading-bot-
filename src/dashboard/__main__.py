"""CLI: python -m dashboard [--host 127.0.0.1] [--port 8787]"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard",
        description="hl-bot live trading dashboard (paper-first)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default 8787)")
    parser.add_argument(
        "--journal",
        default=None,
        help="Journal JSONL path (default JOURNAL_PATH or logs/trades.jsonl)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Dev auto-reload (uvicorn)",
    )
    args = parser.parse_args(argv)

    if args.journal:
        os.environ["JOURNAL_PATH"] = args.journal

    journal = args.journal or os.getenv("JOURNAL_PATH", "logs/trades.jsonl")
    print(f"Dashboard -> http://{args.host}:{args.port}/", flush=True)
    print(f"Journal   → {Path(journal).resolve() if Path(journal).is_absolute() else Path.cwd() / journal}", flush=True)
    print("PAPER badge shown unless latest start event mode=LIVE", flush=True)

    import uvicorn

    # Pass factory so JOURNAL_PATH / cwd are picked up at worker start
    uvicorn.run(
        "dashboard.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

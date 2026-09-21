"""FastAPI dashboard: GET / and GET /api/state."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dashboard.marks import fetch_all_mids
from dashboard.state import DEFAULT_SYMBOLS, load_state, resolve_journal_path

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    *,
    journal_path: str | Path | None = None,
    project_root: str | Path | None = None,
    api_url: str | None = None,
) -> FastAPI:
    root = Path(project_root) if project_root is not None else Path.cwd()
    path = resolve_journal_path(journal_path, project_root=root)
    hl_api = (api_url or os.getenv("HL_API_URL") or "https://api.hyperliquid.xyz").rstrip("/")

    app = FastAPI(title="hl-bot dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def api_state() -> dict:
        coins = [s["coin"] for s in DEFAULT_SYMBOLS]
        marks = fetch_all_mids(coins, api_url=hl_api)
        return load_state(path, last_n=100, marks=marks or None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()

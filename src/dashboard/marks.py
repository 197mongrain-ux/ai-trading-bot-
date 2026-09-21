"""Optional Hyperliquid allMids fetch for unrealized PnL (fail soft)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_API = "https://api.hyperliquid.xyz"


def fetch_all_mids(
    coins: list[str] | None = None,
    *,
    api_url: str = DEFAULT_API,
    timeout: float = 2.5,
) -> dict[str, float]:
    """Return {COIN: mid} for requested coins. Empty dict on any failure."""
    try:
        import requests
    except ImportError:
        logger.debug("requests not installed; skipping marks")
        return {}

    want = {c.upper() for c in (coins or ["BTC", "SOL", "XRP"])}
    url = api_url.rstrip("/") + "/info"
    try:
        resp = requests.post(url, json={"type": "allMids"}, timeout=timeout)
        resp.raise_for_status()
        raw: Any = resp.json()
    except Exception as exc:
        logger.debug("allMids fetch failed: %s", exc)
        return {}

    if not isinstance(raw, dict):
        return {}

    out: dict[str, float] = {}
    for k, v in raw.items():
        key = str(k).upper()
        # bare coin or prefix (e.g. BTC-PERP)
        coin = key.split("-")[0] if "-" in key else key
        if coin in want or key in want:
            try:
                out[coin if coin in want else key] = float(v)
            except (TypeError, ValueError):
                continue
    # Prefer exact bare keys when both exist
    for c in want:
        if c in raw:
            try:
                out[c] = float(raw[c])
            except (TypeError, ValueError):
                pass
    return {c: out[c] for c in want if c in out}

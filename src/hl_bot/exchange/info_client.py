"""Hyperliquid Info API wrapper for mark/mid prices and candles."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class InfoClient:
    """Thin wrapper around hyperliquid.info.Info for market data.

    Candle / historical data availability depends on the Info API
    ``candleSnapshot`` endpoint. Session VWAP in paper/offline tests can
    also be fed from synthetic bars via ``inject_bars``.

    Hyperliquid perp mid keys use bare coin names: BTC, SOL, XRP, etc.
    """

    def __init__(self, base_url: str = "https://api.hyperliquid.xyz", skip_ws: bool = True):
        self.base_url = base_url.rstrip("/")
        self._info: Any = None
        self._skip_ws = skip_ws
        # coin -> bars; "*" applies to any coin without a specific inject
        self._injected_bars: dict[str, list[dict[str, float]]] = {}

    def _ensure_client(self) -> Any:
        if self._info is None:
            try:
                from hyperliquid.info import Info

                self._info = Info(self.base_url, skip_ws=self._skip_ws)
            except Exception as exc:  # pragma: no cover - network/import
                logger.warning("Could not init Hyperliquid Info client: %s", exc)
                raise
        return self._info

    def get_mid_price(self, coin: str = "BTC") -> float:
        """Return mid price for ``coin`` from allMids (bare coin name)."""
        info = self._ensure_client()
        mids = info.all_mids()
        key = coin if coin in mids else f"{coin}-PERP"
        if key not in mids:
            # try bare coin / prefix match
            for k, v in mids.items():
                if k.upper() == coin.upper() or k.upper().startswith(coin.upper()):
                    return float(v)
            raise KeyError(f"No mid price for {coin} in allMids")
        return float(mids[key])

    def get_mark_price(self, coin: str = "BTC") -> float:
        """Return approximate mark price.

        Hyperliquid exposes mid via allMids; metaAndAssetCtxs provides
        markPx when available. Falls back to mid.
        """
        try:
            info = self._ensure_client()
            meta_and_ctx = info.meta_and_asset_ctxs()
            meta, ctxs = meta_and_ctx[0], meta_and_ctx[1]
            universe = meta.get("universe", [])
            for i, asset in enumerate(universe):
                name = asset.get("name", "")
                if name.upper() == coin.upper() or name.upper().startswith(coin.upper()):
                    ctx = ctxs[i]
                    mark = ctx.get("markPx") or ctx.get("midPx")
                    if mark is not None:
                        return float(mark)
        except Exception as exc:
            logger.debug("markPx lookup failed (%s); falling back to mid", exc)
        return self.get_mid_price(coin)

    def get_candles(
        self,
        coin: str = "BTC",
        interval: str = "1m",
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, float]]:
        """Fetch candle snapshot if available; else return injected bars.

        Each bar: {t, o, h, l, c, v} with t in ms.

        Hyperliquid returns an empty list when start/end are both 0, so we
        default to the last 24 hours of candles for VWAP.
        """
        import time

        coin_key = coin.upper()
        if coin_key in self._injected_bars:
            return list(self._injected_bars[coin_key])
        if "*" in self._injected_bars:
            return list(self._injected_bars["*"])

        info = self._ensure_client()
        now_ms = int(time.time() * 1000)
        if end_ms is None or end_ms <= 0:
            end_ms = now_ms
        if start_ms is None or start_ms <= 0:
            start_ms = end_ms - 24 * 60 * 60 * 1000

        req: dict[str, Any] = {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        }

        try:
            raw = info.candles_snapshot(coin, interval, start_ms, end_ms)
        except Exception:
            # Older SDK signature variations
            try:
                raw = info.post("/info", {"type": "candleSnapshot", "req": req})
            except Exception as exc:
                logger.warning("candleSnapshot unavailable: %s", exc)
                return []

        bars: list[dict[str, float]] = []
        for c in raw or []:
            bars.append(
                {
                    "t": float(c.get("t") or c.get("T") or 0),
                    "o": float(c.get("o") or c.get("open") or 0),
                    "h": float(c.get("h") or c.get("high") or 0),
                    "l": float(c.get("l") or c.get("low") or 0),
                    "c": float(c.get("c") or c.get("close") or 0),
                    "v": float(c.get("v") or c.get("volume") or 0),
                }
            )
        return bars

    def inject_bars(self, bars: list[dict[str, float]], coin: str | None = None) -> None:
        """Inject synthetic bars (for tests / offline VWAP).

        If ``coin`` is None, bars apply to any symbol without a specific inject.
        """
        key = coin.upper() if coin else "*"
        self._injected_bars[key] = list(bars)

    def clear_injected_bars(self) -> None:
        self._injected_bars = {}

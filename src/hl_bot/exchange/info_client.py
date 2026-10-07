"""Hyperliquid Info API wrapper for mark/mid prices and candles."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from hl_bot.strategy.model_b.universe import canon_coin

logger = logging.getLogger(__name__)

# One fresh snapshot per coin per minute is enough for 1m swings and
# PDH/PDL/WKH/WKL. The hunt used to call candleSnapshot for every coin on
# every pass (~10 coins every few seconds), which is what triggered the
# Hyperliquid rate-limit toasts.
CANDLE_TTL_SEC = 60.0
CANDLE_BACKOFF_BASE_SEC = 5.0
CANDLE_BACKOFF_CAP_SEC = 60.0


class _CandleCache:
    """TTL cache shared by every coin on this client, plus one global 429 backoff.

    A single HTTP 429 parks every coin. The next call serves last-good bars
    and does not open another request until the backoff has elapsed. Backoff
    doubles on each new 429 and resets only after a 200. There is no retry
    loop inside a fetch.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str], tuple[float, list[dict[str, float]]]] = {}
        self._backoff_until = 0.0
        self._backoff_sec = 0.0

    def fresh(self, key: tuple[str, str], now: float) -> list[dict[str, float]] | None:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or now - slot[0] >= CANDLE_TTL_SEC:
                return None
            return list(slot[1])

    def last_good(self, key: tuple[str, str]) -> list[dict[str, float]] | None:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                return None
            return list(slot[1])

    def in_backoff(self, now: float) -> bool:
        with self._lock:
            return now < self._backoff_until

    def store(self, key: tuple[str, str], bars: list[dict[str, float]], now: float) -> None:
        with self._lock:
            self._slots[key] = (now, list(bars))
            self._backoff_sec = 0.0
            self._backoff_until = 0.0

    def penalize(self, now: float) -> float:
        with self._lock:
            if self._backoff_sec <= 0:
                step = CANDLE_BACKOFF_BASE_SEC
            else:
                step = min(CANDLE_BACKOFF_CAP_SEC, self._backoff_sec * 2)
            self._backoff_sec = step
            self._backoff_until = now + step
            return step


def _post_candle_snapshot(
    base_url: str,
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    timeout: float = 15.0,
) -> tuple[int, object]:
    """One POST /info candleSnapshot. No retries. 429 is returned as a status."""
    url = base_url.rstrip("/") + "/info"
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            return status, json.loads(raw.decode() or "null")
    except urllib.error.HTTPError as exc:
        return int(exc.code), None


def _post_info(base_url: str, payload: dict, timeout: float = 15.0) -> tuple[int, object]:
    """One POST /info. No retries."""
    url = base_url.rstrip("/") + "/info"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            return status, json.loads(raw.decode() or "null")
    except urllib.error.HTTPError as exc:
        return int(exc.code), None


def parse_spot_usdc_total(payload: object) -> float | None:
    """USDC ``total`` from a ``spotClearinghouseState`` body.

    Perp ``marginSummary.accountValue`` is not read. A payload with no
    USDC row is ``None``, not zero, so a bad body cannot size a trade.
    A present USDC total of 0 is a real empty balance.
    """
    if not isinstance(payload, dict):
        return None
    balances = payload.get("balances")
    if not isinstance(balances, list):
        return None
    for row in balances:
        if not isinstance(row, dict):
            continue
        if str(row.get("coin") or "").upper() != "USDC":
            continue
        try:
            total = float(row.get("total"))
        except (TypeError, ValueError):
            return None
        if total < 0:
            return None
        return total
    return None


def _parse_candle_rows(raw: object) -> list[dict[str, float]]:
    if not isinstance(raw, list):
        return []
    bars: list[dict[str, float]] = []
    for candle in raw:
        if not isinstance(candle, dict):
            continue
        bars.append(
            {
                "t": float(candle.get("t") or candle.get("T") or 0),
                "o": float(candle.get("o") or candle.get("open") or 0),
                "h": float(candle.get("h") or candle.get("high") or 0),
                "l": float(candle.get("l") or candle.get("low") or 0),
                "c": float(candle.get("c") or candle.get("close") or 0),
                "v": float(candle.get("v") or candle.get("volume") or 0),
            }
        )
    return bars


def _slice_bars(
    bars: list[dict[str, float]],
    start_ms: int | None,
    end_ms: int | None,
) -> list[dict[str, float]]:
    selected: list[dict[str, float]] = []
    for bar in bars:
        ts = float(bar.get("t") or 0)
        if start_ms is not None and ts < start_ms:
            continue
        if end_ms is not None and ts > end_ms:
            continue
        selected.append(bar)
    return selected


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
        self._candles = _CandleCache()
        self._now = time.time
        self._has_injected_spot = False
        self._injected_spot_usdc: float | None = None

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

        Results are cached per ``(coin, interval)`` for ``CANDLE_TTL_SEC``.
        HTTP 429 backs off globally and returns the last good snapshot for
        that coin instead of retrying. A coin that has never succeeded
        comes back empty so pool bias falls through to NONE.
        """
        coin_key = canon_coin(coin)
        if coin_key in self._injected_bars:
            return list(self._injected_bars[coin_key])
        if "*" in self._injected_bars:
            return list(self._injected_bars["*"])

        now_ms = int(self._now() * 1000)
        if end_ms is None or end_ms <= 0:
            end_ms = now_ms
        if start_ms is None or start_ms <= 0:
            start_ms = end_ms - 24 * 60 * 60 * 1000

        key = (coin_key, interval)
        now = float(self._now())
        cached = self._candles.fresh(key, now)
        if cached is not None:
            return _slice_bars(cached, start_ms, end_ms)
        if self._candles.in_backoff(now):
            prior = self._candles.last_good(key)
            return _slice_bars(prior, start_ms, end_ms) if prior is not None else []

        # One attempt. A 429 (or any other failure) must not loop.
        try:
            status, raw = _post_candle_snapshot(
                self.base_url, coin_key, interval, start_ms, end_ms
            )
        except Exception as exc:
            delay = self._candles.penalize(now)
            logger.warning(
                "candleSnapshot failed for %s %s (%s); backoff %.0fs, serving last-good",
                coin_key,
                interval,
                exc,
                delay,
            )
            prior = self._candles.last_good(key)
            return _slice_bars(prior, start_ms, end_ms) if prior is not None else []

        if status == 429:
            delay = self._candles.penalize(now)
            logger.warning(
                "candleSnapshot HTTP 429 for %s %s; backoff %.0fs, serving last-good",
                coin_key,
                interval,
                delay,
            )
            prior = self._candles.last_good(key)
            return _slice_bars(prior, start_ms, end_ms) if prior is not None else []

        if status != 200:
            delay = self._candles.penalize(now)
            logger.warning(
                "candleSnapshot HTTP %s for %s %s; backoff %.0fs, serving last-good",
                status,
                coin_key,
                interval,
                delay,
            )
            prior = self._candles.last_good(key)
            return _slice_bars(prior, start_ms, end_ms) if prior is not None else []

        bars = _parse_candle_rows(raw)
        self._candles.store(key, bars, now)
        return _slice_bars(bars, start_ms, end_ms)

    def spot_usdc_balance(self, user: str | None) -> float | None:
        """Spot USDC ``total`` for ``user``. Not perp account value.

        Model B sizes 2% of this balance. A thin perp ``accountValue`` is
        the wrong base when the USDC sits in spot. ``None`` means the
        balance could not be read — callers must not substitute perp AV.
        An injected balance (tests) is returned without a network call.
        """
        if self._has_injected_spot:
            return self._injected_spot_usdc
        user = (user or "").strip()
        if not user:
            return None
        try:
            status, raw = _post_info(
                self.base_url,
                {"type": "spotClearinghouseState", "user": user},
            )
        except Exception as exc:
            logger.warning("spotClearinghouseState failed: %s", exc)
            return None
        if status != 200:
            logger.warning("spotClearinghouseState HTTP %s", status)
            return None
        return parse_spot_usdc_total(raw)

    def inject_spot_usdc(self, balance: float | None) -> None:
        """Force the spot USDC balance. ``None`` is a failed read, not zero."""
        self._has_injected_spot = True
        self._injected_spot_usdc = None if balance is None else float(balance)

    def inject_bars(self, bars: list[dict[str, float]], coin: str | None = None) -> None:
        """Inject synthetic bars (for tests / offline VWAP).

        If ``coin`` is None, bars apply to any symbol without a specific inject.
        """
        key = canon_coin(coin) if coin else "*"
        self._injected_bars[key] = list(bars)

    def clear_injected_bars(self) -> None:
        self._injected_bars = {}

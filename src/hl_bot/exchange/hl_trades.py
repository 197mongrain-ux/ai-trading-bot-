"""Hyperliquid aggressor trade feed.

Public trades on the info websocket include ``side``:

- ``B`` — buy aggressor (taker bought)
- ``A`` — sell aggressor (taker sold)

Anything else, including a missing ``side``, is stored as ``side=None``.
The engine then fails closed with ``NO_SIDE``. Buyer/seller addresses are
not used to invent a side. Book depth is not an entry input; the best
bid and ask are kept only so the Alo anchor can prove it will not cross.

Testnet: ``wss://api.hyperliquid-testnet.xyz/ws``
Mainnet: ``wss://api.hyperliquid.xyz/ws``
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from itertools import count

from hl_bot.strategy.model_b.types import TradePrint
from hl_bot.strategy.model_b.universe import canon_coin

logger = logging.getLogger(__name__)

_BUY = {"B", "BUY", "BID"}
_SELL = {"A", "SELL", "ASK", "S"}


# Hyperliquid closes a socket that never sends an application ping
# (`{"method":"ping"}`) with close code 1000 and reason "Expired".
# websocket-client protocol pings are answered at the gateway and do not
# keep that session alive, so they are not used.
APP_PING_SEC = 20.0


def app_ping() -> dict:
    return {"method": "ping"}


def normalize_coin(raw: object) -> str:
    """Bare uppercase perps, or HIP-3 ``dex:ASSET`` with a lowercase dex.

    ``btc-perp`` is ``BTC``. ``XYZ:gold`` is ``xyz:GOLD`` — the info API
    rejects the uppercased dex prefix.
    """
    return canon_coin(raw)


def ws_url(network: str) -> str:
    if (network or "").strip().lower() == "testnet":
        return "wss://api.hyperliquid-testnet.xyz/ws"
    return "wss://api.hyperliquid.xyz/ws"


def trades_subscribe(coin: str) -> dict:
    return {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}


def bbo_subscribe(coin: str) -> dict:
    return {"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}}


def user_fills_subscribe(user: str) -> dict:
    return {"method": "subscribe", "subscription": {"type": "userFills", "user": user}}


def map_aggressor_side(raw: object) -> str | None:
    """Map a Hyperliquid trade side. Unknown / missing → None (fail closed)."""
    if raw is None:
        return None
    key = str(raw).strip().upper()
    if not key:
        return None
    if key in _BUY:
        return "buy"
    if key in _SELL:
        return "sell"
    return None


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ts_seconds(raw: object) -> float | None:
    ts = _as_float(raw)
    if ts is None:
        return None
    if ts > 1e12:
        return ts / 1000.0
    return ts


def parse_hl_trade(raw: dict, *, seq: int = 0) -> TradePrint | None:
    """Parse one WsTrade. A missing aggressor side is kept, not inferred."""
    if not isinstance(raw, dict):
        return None
    price = _as_float(raw.get("px", raw.get("price")))
    size = _as_float(raw.get("sz", raw.get("size")))
    if price is None or size is None or price <= 0 or size <= 0:
        return None
    coin = normalize_coin(raw.get("coin"))
    if not coin:
        return None
    ts = _ts_seconds(raw.get("time", raw.get("ts")))
    if ts is None:
        return None
    # ``users`` is [buyer, seller]. Do not derive side from it.
    side = map_aggressor_side(raw.get("side"))
    return TradePrint(ts=ts, coin=coin, price=price, size=size, side=side, seq=seq)


def _level_px(level: object) -> float | None:
    if not isinstance(level, dict):
        return None
    px = _as_float(level.get("px"))
    if px is None or px <= 0:
        return None
    return px


def parse_bbo(data: dict) -> tuple[str, float | None, float | None] | None:
    """Top of book only. Deeper levels are not read."""
    if not isinstance(data, dict):
        return None
    coin = normalize_coin(data.get("coin"))
    bbo = data.get("bbo")
    if not coin or not isinstance(bbo, (list, tuple)) or len(bbo) < 2:
        return None
    return coin, _level_px(bbo[0]), _level_px(bbo[1])


def parse_l2_top(data: dict) -> tuple[str, float | None, float | None] | None:
    """Best bid/ask from an l2Book snapshot. Levels below the top are discarded."""
    if not isinstance(data, dict):
        return None
    coin = normalize_coin(data.get("coin"))
    levels = data.get("levels")
    if not coin or not isinstance(levels, (list, tuple)) or len(levels) < 2:
        return None
    bids, asks = levels[0], levels[1]
    bid = _level_px(bids[0]) if isinstance(bids, list) and bids else None
    ask = _level_px(asks[0]) if isinstance(asks, list) and asks else None
    return coin, bid, ask


class UserFill:
    def __init__(
        self,
        coin: str,
        oid: object | None,
        price: float,
        size: float,
        ts: float,
        crossed: bool | None,
    ):
        self.coin = canon_coin(coin)
        self.oid = oid
        self.price = price
        self.size = size
        self.ts = ts
        self.crossed = crossed


def parse_user_fill(raw: dict) -> UserFill | None:
    if not isinstance(raw, dict):
        return None
    coin = normalize_coin(raw.get("coin"))
    price = _as_float(raw.get("px"))
    size = _as_float(raw.get("sz"))
    ts = _ts_seconds(raw.get("time"))
    if not coin or price is None or size is None or ts is None or price <= 0 or size <= 0:
        return None
    crossed_raw = raw.get("crossed")
    crossed: bool | None
    if isinstance(crossed_raw, bool):
        crossed = crossed_raw
    else:
        crossed = None
    return UserFill(
        coin=coin,
        oid=raw.get("oid"),
        price=price,
        size=size,
        ts=ts,
        crossed=crossed,
    )


class MemoryFeed:
    """In-memory print source for tests and offline paper runs."""

    def __init__(
        self,
        prints: list[TradePrint] | None = None,
        bbo: dict[str, tuple[float | None, float | None]] | None = None,
        user_fills: list[UserFill] | None = None,
    ):
        self._prints = list(prints or [])
        self._bbo = {canon_coin(k): v for k, v in (bbo or {}).items()}
        self._fills = list(user_fills or [])

    def prints(self, coin: str) -> list[TradePrint]:
        coin = canon_coin(coin)
        return [p for p in self._prints if p.coin == coin]

    def bbo(self, coin: str) -> tuple[float | None, float | None]:
        return self._bbo.get(canon_coin(coin), (None, None))

    def take_user_fills(self) -> list[UserFill]:
        out = list(self._fills)
        self._fills.clear()
        return out

    def connect(self) -> bool:
        return False


class HyperliquidTradeFeed:
    """Websocket trades + top-of-book. Safe to construct with no network."""

    def __init__(
        self,
        network: str = "testnet",
        coins: tuple[str, ...] | list[str] = (),
        user: str | None = None,
        maxlen: int = 5000,
    ):
        self.network = network
        self.url = ws_url(network)
        self.coins = tuple(dict.fromkeys(canon_coin(c) for c in coins if canon_coin(c)))
        self.user = user or None
        self._prints: dict[str, deque[TradePrint]] = {}
        self._bbo: dict[str, tuple[float | None, float | None]] = {}
        self._fills: list[UserFill] = []
        self._lock = threading.Lock()
        self._seq = count()
        self._maxlen = maxlen
        self._seen: set[tuple[str, int | str]] = set()
        self._seen_order: deque[tuple[str, int | str]] = deque()
        self._stop = False
        self._ping_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None

    def prints(self, coin: str) -> list[TradePrint]:
        with self._lock:
            return list(self._prints.get(normalize_coin(coin), ()))

    def bbo(self, coin: str) -> tuple[float | None, float | None]:
        with self._lock:
            return self._bbo.get(normalize_coin(coin), (None, None))

    def take_user_fills(self) -> list[UserFill]:
        with self._lock:
            out = list(self._fills)
            self._fills.clear()
            return out

    def _store_print(self, print_: TradePrint) -> None:
        bucket = self._prints.get(print_.coin)
        if bucket is None:
            bucket = deque(maxlen=self._maxlen)
            self._prints[print_.coin] = bucket
        bucket.append(print_)

    def _mark_new(self, coin: str, tid: object) -> bool:
        """False when this trade id was already stored. Missing tid is kept.

        Reconnect snapshots repeat the last trades. Counting them twice
        would fake a thick tape. The print buffer itself is not cleared
        on reconnect.
        """
        if isinstance(tid, bool) or tid is None or tid == "":
            return True
        if not isinstance(tid, (int, str)):
            tid = str(tid)
        key = (coin, tid)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        cap = max(self._maxlen * 4, 1)
        while len(self._seen_order) > cap:
            self._seen.discard(self._seen_order.popleft())
        return True

    def resubscribe_payloads(self) -> list[dict]:
        """Messages sent on every socket open, including after ``Expired``.

        Trades are subscribed before BBO and user fills so one bad send
        cannot skip the tape. The trailing ping is the application heartbeat.
        """
        payloads: list[dict] = []
        for coin in self.coins:
            payloads.append(trades_subscribe(coin))
        for coin in self.coins:
            payloads.append(bbo_subscribe(coin))
        if self.user:
            payloads.append(user_fills_subscribe(self.user))
        payloads.append(app_ping())
        return payloads

    def _send_subscriptions(self, ws) -> None:
        for payload in self.resubscribe_payloads():
            try:
                ws.send(json.dumps(payload))
            except Exception:
                logger.exception("trade feed send failed (%s)", payload.get("method"))

    def ingest(self, message: dict | list) -> list[TradePrint]:
        """Parse one websocket payload. Returns newly stored prints."""
        if isinstance(message, list):
            out: list[TradePrint] = []
            for item in message:
                if isinstance(item, dict):
                    out.extend(self.ingest(item))
            return out
        if not isinstance(message, dict):
            return []

        channel = message.get("channel")
        if channel in {"pong", "subscriptionResponse"} or message.get("method") in {
            "pong",
            "ping",
        }:
            return []

        data = message.get("data")
        if channel == "trades":
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = [data]
            else:
                rows = []
            stored: list[TradePrint] = []
            with self._lock:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    parsed = parse_hl_trade(row, seq=next(self._seq))
                    if parsed is None:
                        continue
                    if not self._mark_new(parsed.coin, row.get("tid")):
                        continue
                    self._store_print(parsed)
                    stored.append(parsed)
            return stored

        if channel == "bbo" and isinstance(data, dict):
            parsed_bbo = parse_bbo(data)
            if parsed_bbo is not None:
                coin, bid, ask = parsed_bbo
                with self._lock:
                    self._bbo[coin] = (bid, ask)
            return []

        if channel == "l2Book" and isinstance(data, dict):
            parsed_l2 = parse_l2_top(data)
            if parsed_l2 is not None:
                coin, bid, ask = parsed_l2
                with self._lock:
                    # Depth below the top was discarded in parse_l2_top.
                    self._bbo[coin] = (bid, ask)
            return []

        if channel == "userFills" and isinstance(data, dict):
            if data.get("isSnapshot"):
                return []
            fills = data.get("fills") or []
            with self._lock:
                for row in fills:
                    if isinstance(row, dict):
                        parsed_fill = parse_user_fill(row)
                        if parsed_fill is not None:
                            self._fills.append(parsed_fill)
            return []
        return []

    def connect(self) -> bool:
        """Start a daemon reader. Returns False if the client library is missing.

        Disconnects are retried. Empty tape fails closed in the engine
        (``THIN_TAPE``) rather than inventing prints.
        """
        try:
            import websocket  # type: ignore
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning("trade feed not started (%s); tape will fail closed", exc)
            return False

        url = self.url

        def on_open(ws) -> None:
            self._send_subscriptions(ws)
            logger.info(
                "trade feed subscribed %s coins on %s (buffer kept)",
                len(self.coins),
                url,
            )

        def on_message(ws, message: str) -> None:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            if message == "Websocket connection established.":
                return
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                return
            if isinstance(payload, dict) and (
                payload.get("channel") == "ping" or payload.get("method") == "ping"
            ):
                try:
                    ws.send(json.dumps({"method": "pong"}))
                except Exception:
                    logger.debug("pong failed", exc_info=True)
                return
            try:
                self.ingest(payload)
            except Exception:
                logger.exception("trade feed parse failed")

        def on_error(ws, error) -> None:
            logger.warning("trade feed error: %s", error)

        def on_close(ws, code, reason) -> None:
            # Includes "Expired" (code 1000). The outer loop opens a new
            # socket and on_open resubscribes. Prints already stored stay.
            logger.warning("trade feed closed (%s): %s — resubscribing", code, reason)

        def runner() -> None:
            while not self._stop:
                self._ping_stop.clear()
                holder: dict = {}

                def on_open_ping(ws, _holder=holder) -> None:
                    _holder["ws"] = ws
                    on_open(ws)

                def ping_loop() -> None:
                    while not self._ping_stop.wait(APP_PING_SEC):
                        if self._stop:
                            return
                        ws = holder.get("ws")
                        if ws is None:
                            continue
                        try:
                            ws.send(json.dumps(app_ping()))
                        except Exception:
                            logger.debug("trade feed ping failed", exc_info=True)

                try:
                    self._ws = websocket.WebSocketApp(
                        url,
                        on_open=on_open_ping,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close,
                    )
                    pinger = threading.Thread(
                        target=ping_loop, name="hl-model-b-ping", daemon=True
                    )
                    pinger.start()
                    # No ping_interval: protocol pings do not stop "Expired".
                    self._ws.run_forever()
                except Exception:
                    logger.exception("trade feed disconnected")
                finally:
                    self._ping_stop.set()
                    holder.clear()
                if not self._stop:
                    time.sleep(2)

        self._stop = False
        self._ping_stop.clear()
        self._thread = threading.Thread(target=runner, name="hl-model-b-trades", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop = True
        self._ping_stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.debug("trade feed close failed", exc_info=True)

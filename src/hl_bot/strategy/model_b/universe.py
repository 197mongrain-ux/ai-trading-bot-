"""Hunt universe for Model B.

NY hours and after hours hunt the same list. The clock still exists
(``in_ny_session``) but it does not drop coins. Crypto trades through
the weekend, and the desk wants the mainnet names overnight too.

The default is Chris's mainnet list: majors first, then the rest of the
top 10, then gold, the S&P proxy, and ``xyz:XYZ100`` (the Nasdaq proxy).
When ``ENTRY_MODE=model_b`` and ``SYMBOLS`` (or ``SYMBOL``) is set, that
env list replaces this default so the next edit does not need a code change.
The breakout default ``BTC, SOL, XRP`` is not used as a hunt list.

HIP-3 names keep a lowercase dex prefix. ``XYZ:GOLD`` is rejected by the
info API; the wire form is ``xyz:GOLD``.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# Majors first, then the rest of the top 10, then the xyz index/metal proxies.
DEFAULT_HUNT_COINS: tuple[str, ...] = (
    "BTC",
    "ETH",
    "SOL",
    "NEAR",
    "PUMP",
    "LIT",
    "AAVE",
    "ONDO",
    "WLD",
    "TAO",
    "xyz:GOLD",
    "xyz:SP500",
    "xyz:XYZ100",
)

# Both sessions. Kept as names so existing imports stay valid.
NY_COINS: tuple[str, ...] = DEFAULT_HUNT_COINS
AFTER_HOURS_COINS: tuple[str, ...] = DEFAULT_HUNT_COINS

# 09:00 inclusive, 16:00 exclusive. The coin list does not use this cut.
_OPEN_MIN = 9 * 60
_CLOSE_MIN = 16 * 60


def canon_coin(raw: object) -> str:
    """Stable hunt name, and the string the exchange accepts.

    Bare perps are uppercase (``btc-perp`` → ``BTC``). A builder perp
    keeps the dex lowercase (``XYZ:gold`` → ``xyz:GOLD``).
    """
    coin = str(raw or "").strip()
    if not coin:
        return ""
    if coin.upper().endswith("-PERP"):
        coin = coin[: -len("-PERP")].strip()
    if ":" in coin:
        dex, name = coin.split(":", 1)
        dex = dex.strip().lower()
        name = name.strip().upper()
        if dex and name:
            return f"{dex}:{name}"
    return coin.upper()


def resolve_hunt_coins(symbols: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Canonicalize ``symbols``, keeping order and dropping blanks.

    ``None`` is the default mainnet list. An explicit empty list stays empty.
    """
    if symbols is None:
        return DEFAULT_HUNT_COINS
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        coin = canon_coin(raw)
        if not coin or coin in seen:
            continue
        seen.add(coin)
        out.append(coin)
    return tuple(out)


def perp_dexs_for(coins: tuple[str, ...] | list[str]) -> list[str] | None:
    """Dex names the SDK must load so a builder perp has an asset id.

    ``None`` leaves the client on the original perp dex. ``""`` is that
    dex and stays first when a builder dex is also required.
    """
    dexs: list[str] = []
    for coin in coins:
        name = canon_coin(coin)
        if ":" not in name:
            continue
        dex = name.split(":", 1)[0]
        if dex and dex not in dexs:
            dexs.append(dex)
    if not dexs:
        return None
    return ["", *dexs]


def as_ny(now: datetime | float) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=NY)
        return now.astimezone(NY)
    return datetime.fromtimestamp(float(now), tz=NY)


def in_ny_session(now: datetime | float) -> bool:
    ny = as_ny(now)
    minutes = ny.hour * 60 + ny.minute
    return _OPEN_MIN <= minutes < _CLOSE_MIN


def session_coins(
    now: datetime | float,
    symbols: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """Coins hunted at ``now``.

    Both the NY window and after hours return the same list. ``symbols``
    is the configured env list; ``None`` uses the default mainnet universe.
    """
    as_ny(now)
    return resolve_hunt_coins(symbols)

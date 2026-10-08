"""Per-coin maker/taker rates for the 2% cap and the TP fee floor.

Hyperliquid's published base tier is 1.5 bps maker / 4.5 bps taker.
That is what sizing used for every coin. Builder-dex coins do not all
pay it. xyz names that are not in growth mode pay about twice the
account's rate (observed 2.88 / 8.64 bps against a 1.44 / 4.32 account).
On a 15 bps stop, sizing as if fees were 6 bps and then paying 11.5 bps
loses about 2.5% at the stop. Growth-mode xyz coins pay about 0.2× the
account rate (~0.29 / 0.86 bps).

Resolution order, highest trust first:

1. Both maker and taker present on the asset's meta row.
2. ``userFees`` (``userAddRate`` / ``userCrossRate``) scaled by dex:
   main dex uses them as-is; xyz growth uses 0.2×; any other xyz (or an
   xyz coin whose growth flag was not in the payload) uses 2×. Unknown
   never takes the discount.
3. Conservative table when the account rates were not read: main
   1.5 / 4.5 bps; xyz growth 0.30 / 0.90 bps (0.2× base, a hair above
   the observed 0.29 / 0.86); other xyz 3.0 / 9.0 bps (above 2.88 / 8.64).

``MODEL_B_COIN_FEES=0`` forces the base tier for every coin.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.strategy.model_b.risk import MAKER_FEE_RATE, TAKER_FEE_RATE
from hl_bot.strategy.model_b.universe import canon_coin

# xyz non-growth is ~2× the user's rate. The table uses 2× the published
# base, which sits above the observed 2.88 / 8.64 bps.
XYZ_FEE_MULT = 2.0
# Growth mode is ~0.2× the user's main rate (0.29 / 0.86 vs 1.44 / 4.32).
GROWTH_FEE_MULT = 0.2


@dataclass(frozen=True)
class CoinFees:
    maker: float
    taker: float
    source: str

    @property
    def round_trip(self) -> float:
        return float(self.maker) + float(self.taker)


def _dex(coin: str) -> str:
    name = canon_coin(coin)
    if ":" not in name:
        return ""
    return name.split(":", 1)[0]


def _rate(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0:
        return None
    # A payload in bps (2.88) rather than a fraction (0.000288).
    if rate > 0.01:
        rate = rate / 10_000.0
    return rate


def conservative_fees(coin: str, *, growth: bool | None = None) -> CoinFees:
    """Table used when userFees and the asset row did not name a rate."""
    name = canon_coin(coin)
    if _dex(name) == "xyz" and growth is True:
        return CoinFees(
            MAKER_FEE_RATE * GROWTH_FEE_MULT,
            TAKER_FEE_RATE * GROWTH_FEE_MULT,
            "table-growth",
        )
    if _dex(name):
        return CoinFees(
            MAKER_FEE_RATE * XYZ_FEE_MULT,
            TAKER_FEE_RATE * XYZ_FEE_MULT,
            "table-xyz",
        )
    return CoinFees(MAKER_FEE_RATE, TAKER_FEE_RATE, "table-main")


def parse_user_fees(raw: object) -> tuple[float, float] | None:
    """``(maker, taker)`` fractions from a ``userFees`` body, or None."""
    if not isinstance(raw, dict):
        return None
    add = _rate(raw.get("userAddRate"))
    cross = _rate(raw.get("userCrossRate"))
    if add is None or cross is None:
        schedule = raw.get("feeSchedule")
        if isinstance(schedule, dict):
            add = add if add is not None else _rate(schedule.get("add"))
            cross = cross if cross is not None else _rate(schedule.get("cross"))
    if add is None or cross is None:
        return None
    return add, cross


def _opt_bool(item: dict, *keys: str) -> bool | None:
    for key in keys:
        if key in item:
            value = item.get(key)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            return bool(value)
    return None


def parse_asset_flags(raw: object, dex: str = "") -> dict[str, dict]:
    """Per-asset growth / isolated / optional fee overrides from one meta."""
    if not isinstance(raw, dict):
        return {}
    universe = raw.get("universe")
    if not isinstance(universe, list):
        return {}
    prefix = str(dex or "").strip().lower()
    out: dict[str, dict] = {}
    for item in universe:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        raw_name = str(name).strip()
        if ":" in raw_name or not prefix:
            key = canon_coin(raw_name)
        else:
            key = canon_coin(f"{prefix}:{raw_name}")
        if not key:
            continue
        out[key] = {
            "growth": _opt_bool(item, "growthMode", "growth_mode"),
            "only_isolated": _opt_bool(item, "onlyIsolated", "only_isolated") is True,
            "maker": _rate(item.get("makerFee", item.get("makerFeeRate"))),
            "taker": _rate(item.get("takerFee", item.get("takerFeeRate"))),
        }
    return out


def resolve_fees(
    coin: str,
    *,
    user_add: float | None = None,
    user_cross: float | None = None,
    growth: bool | None = None,
    meta_maker: float | None = None,
    meta_taker: float | None = None,
    force_base: bool = False,
) -> CoinFees:
    """The rate pair sizing and the TP floor use for ``coin``."""
    if force_base:
        return CoinFees(MAKER_FEE_RATE, TAKER_FEE_RATE, "base")
    if meta_maker is not None and meta_taker is not None and meta_maker >= 0 and meta_taker >= 0:
        return CoinFees(float(meta_maker), float(meta_taker), "meta")
    name = canon_coin(coin)
    if user_add is not None and user_cross is not None:
        dex = _dex(name)
        if dex == "xyz" and growth is True:
            return CoinFees(
                float(user_add) * GROWTH_FEE_MULT,
                float(user_cross) * GROWTH_FEE_MULT,
                "user-growth",
            )
        if dex:
            return CoinFees(
                float(user_add) * XYZ_FEE_MULT,
                float(user_cross) * XYZ_FEE_MULT,
                "user-xyz",
            )
        return CoinFees(float(user_add), float(user_cross), "user")
    return conservative_fees(name, growth=growth)


@dataclass
class FeeBook:
    """Resolved rates for one pass. Built from userFees + meta, or the table."""

    user_add: float | None = None
    user_cross: float | None = None
    flags: dict | None = None
    force_base: bool = False

    def for_coin(self, coin: str) -> CoinFees:
        flag = (self.flags or {}).get(canon_coin(coin)) or {}
        return resolve_fees(
            coin,
            user_add=self.user_add,
            user_cross=self.user_cross,
            growth=flag.get("growth"),
            meta_maker=flag.get("maker"),
            meta_taker=flag.get("taker"),
            force_base=self.force_base,
        )

    def pair(self, coin: str) -> tuple[float, float]:
        fees = self.for_coin(coin)
        return fees.maker, fees.taker

    def only_isolated(self, coin: str) -> bool:
        if self.force_base:
            return False
        flag = (self.flags or {}).get(canon_coin(coin)) or {}
        return bool(flag.get("only_isolated"))


def build_fee_book(info, coins, *, user: str | None, enabled: bool) -> FeeBook:
    """Read userFees and asset flags once. Failures fall through to the table.

    ``enabled=False`` is ``MODEL_B_COIN_FEES=0``: every coin stays on the
    published base tier.
    """
    if not enabled:
        return FeeBook(force_base=True)
    add = cross = None
    flags: dict = {}
    reader = getattr(info, "user_fee_rates", None)
    if callable(reader):
        try:
            got = reader(user)
        except Exception:
            got = None
        if isinstance(got, tuple) and len(got) == 2 and got[0] is not None and got[1] is not None:
            add, cross = float(got[0]), float(got[1])
    flag_reader = getattr(info, "asset_flags", None)
    if callable(flag_reader):
        try:
            from hl_bot.exchange.account import dexs_for_coins

            loaded = flag_reader(dexs_for_coins(tuple(coins)))
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            flags = loaded
    return FeeBook(user_add=add, user_cross=cross, flags=flags)

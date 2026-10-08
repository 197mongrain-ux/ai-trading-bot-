"""Live Hyperliquid Exchange wrapper — ONLY used when TRADING_MODE=live."""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def _isolated_only_error(value: object) -> bool:
    text = str(value or "").lower()
    return "isolated" in text and ("only" in text or "must" in text or "not" in text)

# Seconds before an order / cancel / trigger HTTP call gives up.
HTTP_TIMEOUT_SEC = 10.0


class LiveExchange:
    """Wraps hyperliquid.exchange.Exchange for authenticated order placement.

    This module is imported and instantiated only in LIVE mode after the
    I_UNDERSTAND_LIVE_TRADING gate passes. Paper mode never calls order().
    """

    def __init__(
        self,
        private_key: str,
        account_address: str | None = None,
        base_url: str = "https://api.hyperliquid.xyz",
        perp_dexs: list[str] | None = None,
    ):
        if not private_key:
            raise ValueError("LiveExchange requires HL_PRIVATE_KEY")

        from eth_account import Account
        from hyperliquid.exchange import Exchange

        self._wallet = Account.from_key(private_key)
        self.account_address = account_address or self._wallet.address
        # ``perp_dexs`` includes "" (the original dex) plus builder dexes
        # such as ``xyz`` so ``xyz:GOLD`` resolves to an asset id. Omitting
        # it leaves BTC/ETH/SOL on the original dex only.
        exchange_kwargs: dict[str, Any] = {
            "account_address": self.account_address if account_address else None,
        }
        if perp_dexs:
            exchange_kwargs["perp_dexs"] = list(perp_dexs)
        # Without a timeout a stalled HTTP call blocks the order path (and the
        # guard thread sharing this client) forever: no stop, no loss kill.
        exchange_kwargs["timeout"] = HTTP_TIMEOUT_SEC
        try:
            self._exchange: Any = Exchange(
                self._wallet,
                base_url,
                **exchange_kwargs,
            )
        except TypeError:
            # Older SDK without the ``timeout`` keyword.
            exchange_kwargs.pop("timeout", None)
            logger.warning("LIVE Exchange SDK has no timeout kwarg: HTTP calls may hang")
            self._exchange = Exchange(
                self._wallet,
                base_url,
                **exchange_kwargs,
            )
        logger.warning(
            "LIVE Exchange initialized for %s — real orders will be sent",
            self.account_address,
        )


    # --- szDecimals / tick rounding (restored after universe expansion) ---
    def _sz_decimals_for(self, coin: str) -> int | None:
        try:
            info = self._exchange.info
            return int(info.asset_to_sz_decimals[info.name_to_asset(coin)])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("szDecimals lookup failed for %s: %s", coin, exc)
            return None

    def _round_size(self, coin: str, size: float) -> float:
        """Floor size onto the coin's szDecimals lot so HL never sees an invalid size."""
        dec = self._sz_decimals_for(coin)
        if dec is None:
            return float(size)
        factor = 10 ** dec
        rounded = math.floor(float(size) * factor + 1e-9) / factor
        if rounded <= 0:
            raise ValueError(f"size rounds to 0 for {coin}: raw={size} dec={dec}")
        return round(rounded, dec)

    def _round_price(self, coin: str, px: float) -> float:
        """HL perp price rule: <=5 significant figures and <= (6 - szDecimals) decimals."""
        dec = self._sz_decimals_for(coin)
        if dec is None:
            return float(px)
        px = float(px)
        if px >= 1 and abs(px - round(px)) < 1e-12:
            return float(round(px))
        return round(float(f"{px:.5g}"), max(0, 6 - dec))

    def round_price_toward(self, coin: str, px: float, up: bool) -> float:
        """Valid HL price at or beyond ``px`` in one direction (stop never loosens)."""
        px = float(px)
        r = self._round_price(coin, px)
        if (up and r >= px) or (not up and r <= px):
            return r
        step = 1.0 + (1e-4 if up else -1e-4)
        q = px
        for _ in range(50):
            q *= step
            r = self._round_price(coin, q)
            if (up and r >= px) or (not up and r <= px):
                return r
        return r

    def round_size(self, coin: str, size: float) -> float:
        return self._round_size(coin, size)

    def size_step(self, coin: str) -> float:
        """One lot (10 ** -szDecimals), or 0 when the coin's step is unknown."""
        dec = self._sz_decimals_for(coin)
        if dec is None or int(dec) < 0:
            return 0.0
        return 10.0 ** (-int(dec))

    def market_open(self, coin: str, is_buy: bool, size: float, leverage: int = 20) -> Any:
        """Place a market-style order (IOC / aggressive limit via SDK helpers).

        Leverage is set on the exchange for margin efficiency; position size
        is still chosen by the risk manager from dollar risk / stop distance.
        """
        size = self._round_size(coin, size)
        logger.warning(
            "LIVE ORDER: %s %s size=%.6f lev=%sx",
            "BUY" if is_buy else "SELL",
            coin,
            size,
            leverage,
        )
        # Update leverage first (cross margin)
        try:
            self._exchange.update_leverage(leverage, coin, is_cross=True)
        except Exception as exc:
            logger.error("update_leverage failed: %s", exc)

        # Prefer market_open if available on SDK, else market_order
        if hasattr(self._exchange, "market_open"):
            return self._exchange.market_open(coin, is_buy, size)
        return self._exchange.order(
            coin,
            is_buy,
            size,
            px=None,  # type: ignore[arg-type]
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=False,
        )

    def market_close(self, coin: str, size: float | None = None) -> Any:
        """Flatten / reduce position."""
        if size is not None:
            size = self._round_size(coin, size)
        logger.warning("LIVE CLOSE: %s size=%s", coin, size)
        if hasattr(self._exchange, "market_close"):
            return self._exchange.market_close(coin, sz=size)
        raise RuntimeError("SDK market_close unavailable; close manually")

    def set_stop_loss(
        self, coin: str, is_buy: bool, size: float, trigger_px: float
    ) -> Any:
        """Place a trigger stop order if supported by the SDK."""
        size = self._round_size(coin, size)
        trigger_px = self._round_price(coin, trigger_px)
        logger.warning(
            "LIVE STOP: %s trigger=%.2f size=%.6f", coin, trigger_px, size
        )
        order_type = {
            "trigger": {
                "triggerPx": trigger_px,
                "isMarket": True,
                "tpsl": "sl",
            }
        }
        return self._exchange.order(
            coin, is_buy, size, trigger_px, order_type, reduce_only=True
        )

    def _set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> Any:
        """Set leverage. An isolated-only reject retries with isolated margin.

        Cross coins stay cross. The retry only runs when the caller asked
        for cross and the exchange says this asset cannot use it.
        """
        try:
            updated = self._exchange.update_leverage(leverage, coin, is_cross=is_cross)
        except Exception as exc:
            if is_cross and _isolated_only_error(exc):
                logger.warning(
                    "MODEL_B LEVERAGE %s cross rejected (%s); isolated margin",
                    coin,
                    exc,
                )
                try:
                    return self._exchange.update_leverage(leverage, coin, is_cross=False)
                except Exception as retry_exc:
                    logger.error("update_leverage isolated retry failed: %s", retry_exc)
                    raise
            logger.error("update_leverage failed: %s", exc)
            raise
        if is_cross and _isolated_only_error(updated):
            logger.warning(
                "MODEL_B LEVERAGE %s cross rejected (%s); isolated margin",
                coin,
                updated,
            )
            return self._exchange.update_leverage(leverage, coin, is_cross=False)
        return updated

    def place_alo(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        leverage: int = 20,
        is_cross: bool = True,
    ) -> Any:
        """Post-only Alo. Model B never crosses and never falls back to market.

        Leverage is the coin max already used for margin. It is set cross
        before the order, unless the asset is isolated-only (``is_cross``
        false, or the exchange rejects cross with an isolated-only error).
        A failed update does not send the order: the ticket would otherwise
        rest at leftover leverage while the margin math assumed the coin max.
        Margin is still notional / leverage either way.
        """
        lev = int(leverage)
        if lev < 1:
            raise ValueError(f"leverage must be >= 1; got {leverage}")
        size = self._round_size(coin, size)
        limit_px = self._round_price(coin, limit_px)
        logger.warning(
            "LIVE ALO: %s %s size=%.6f px=%.6f lev=%sx tif=Alo cross=%s",
            "BUY" if is_buy else "SELL",
            coin,
            size,
            limit_px,
            lev,
            bool(is_cross),
        )
        updated = self._set_leverage(coin, lev, is_cross=bool(is_cross))
        if isinstance(updated, dict):
            status = updated.get("status")
            if status not in (None, "ok"):
                raise RuntimeError(f"update_leverage rejected: {updated}")
        order_type = {"limit": {"tif": "Alo"}}
        return self._exchange.order(
            coin,
            is_buy,
            size,
            limit_px,
            order_type,
            reduce_only=False,
        )

    def cancel_order(self, coin: str, oid: int) -> Any:
        """Cancel a resting Alo when the thesis is stale or an optional timer elapses."""
        logger.warning("LIVE CANCEL: %s oid=%s", coin, oid)
        return self._exchange.cancel(coin, oid)

    def set_take_profit(
        self, coin: str, is_buy: bool, size: float, trigger_px: float
    ) -> Any:
        """Reduce-only TP trigger. Not a flow exit."""
        size = self._round_size(coin, size)
        trigger_px = self._round_price(coin, trigger_px)
        logger.warning(
            "LIVE TP: %s trigger=%.6f size=%.6f", coin, trigger_px, size
        )
        order_type = {
            "trigger": {
                "triggerPx": trigger_px,
                "isMarket": True,
                "tpsl": "tp",
            }
        }
        return self._exchange.order(
            coin, is_buy, size, trigger_px, order_type, reduce_only=True
        )

    def reduce_only_ioc(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        ref_px: float | None = None,
        slippage: float = 0.05,
    ) -> Any:
        """Reduce-only IOC close / cut. Used by the protection guard.

        ``ref_px`` is the mark; the limit is that +/- ``slippage`` so the IOC
        crosses. Without a reference the SDK ``market_close`` path is used
        (it reads the dex position itself). Reduce-only: it can never open
        or flip a position.
        """
        size = self._round_size(coin, size)
        if ref_px is None or float(ref_px) <= 0:
            logger.warning("LIVE REDUCE_ONLY (market_close): %s size=%s", coin, size)
            return self._exchange.market_close(coin, sz=size)
        raw = float(ref_px) * (1.0 + slippage if is_buy else 1.0 - slippage)
        px = self._round_price(coin, raw)
        logger.warning(
            "LIVE REDUCE_ONLY IOC: %s %s size=%.6f px=%s",
            "BUY" if is_buy else "SELL",
            coin,
            size,
            px,
        )
        return self._exchange.order(
            coin, is_buy, size, px, {"limit": {"tif": "Ioc"}}, reduce_only=True
        )

"""Live Hyperliquid Exchange wrapper — ONLY used when TRADING_MODE=live."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
    ):
        if not private_key:
            raise ValueError("LiveExchange requires HL_PRIVATE_KEY")

        from eth_account import Account
        from hyperliquid.exchange import Exchange

        self._wallet = Account.from_key(private_key)
        self.account_address = account_address or self._wallet.address
        self._exchange: Any = Exchange(
            self._wallet,
            base_url,
            account_address=self.account_address if account_address else None,
        )
        logger.warning(
            "LIVE Exchange initialized for %s — real orders will be sent",
            self.account_address,
        )

    def market_open(self, coin: str, is_buy: bool, size: float, leverage: int = 5) -> Any:
        """Place a market-style order (IOC / aggressive limit via SDK helpers)."""
        logger.warning(
            "LIVE ORDER: %s %s size=%.6f lev=%sx",
            "BUY" if is_buy else "SELL",
            coin,
            size,
            leverage,
        )
        # Update leverage first
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
        logger.warning("LIVE CLOSE: %s size=%s", coin, size)
        if hasattr(self._exchange, "market_close"):
            return self._exchange.market_close(coin, sz=size)
        raise RuntimeError("SDK market_close unavailable; close manually")

    def set_stop_loss(
        self, coin: str, is_buy: bool, size: float, trigger_px: float
    ) -> Any:
        """Place a trigger stop order if supported by the SDK."""
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

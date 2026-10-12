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

    def market_open(self, coin: str, is_buy: bool, size: float, leverage: int = 20) -> Any:
        """Place a market-style order (IOC / aggressive limit via SDK helpers).

        Leverage is set on the exchange for margin efficiency; position size
        is still chosen by the risk manager from dollar risk / stop distance.
        """
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
        logger.warning("LIVE CLOSE: %s size=%s", coin, size)
        if hasattr(self._exchange, "market_close"):
            return self._exchange.market_close(coin, sz=size)
        raise RuntimeError("SDK market_close unavailable; close manually")

    def set_stop_loss(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        trigger_px: float,
        sz_decimals: int = 0,
    ) -> Any:
        """Place a reduce-only trigger stop, tick-rounded with Decimal."""
        from hl_bot.execution.brackets import (
            order_response_ok,
            quantize_size,
            quantize_trigger_px,
            wire_float,
        )

        px = wire_float(quantize_trigger_px(trigger_px, sz_decimals))
        sz = wire_float(quantize_size(size, sz_decimals))
        logger.warning("LIVE STOP: %s trigger=%s size=%s", coin, px, sz)
        order_type = {
            "trigger": {
                "triggerPx": px,
                "isMarket": True,
                "tpsl": "sl",
            }
        }
        resp = self._exchange.order(
            coin, is_buy, sz, px, order_type, reduce_only=True
        )
        if not order_response_ok(resp):
            raise RuntimeError(f"stop rejected for {coin}: {resp}")
        return resp

    def fetch_account(self):
        """Positions, resting orders, and perp szDecimals for bracket sync."""
        from hl_bot.execution.brackets import account_from_raw

        info = self._exchange.info
        state = info.user_state(self.account_address)
        try:
            raw_orders = info.frontend_open_orders(self.account_address)
        except Exception:
            logger.exception("frontend_open_orders failed; falling back to open_orders")
            raw_orders = info.open_orders(self.account_address)
        sz_decimals: dict[str, int] = {}
        for name, asset in getattr(info, "coin_to_asset", {}).items():
            if not isinstance(asset, int) or asset >= 10_000:
                continue
            dec = getattr(info, "asset_to_sz_decimals", {}).get(asset)
            if dec is not None:
                sz_decimals[str(name)] = int(dec)
        return account_from_raw(state, raw_orders, sz_decimals)

    def cancel_orders(self, orders: list[tuple[str, int]]) -> Any:
        """Cancel resting orders (used to pull entry Alos before a halt)."""
        from hl_bot.execution.brackets import order_response_ok

        if not orders:
            return None
        resp = self._exchange.bulk_cancel(
            [{"coin": coin, "oid": int(oid)} for coin, oid in orders]
        )
        if not order_response_ok(resp):
            raise RuntimeError(f"cancel failed: {resp}")
        return resp

    def resize_model3_brackets(
        self,
        coin: str,
        side: str,
        size: float,
        stop_px: float,
        tp_px: float,
        sz_decimals: int,
        cancel_oids: list[int] | tuple[int, ...] | None = None,
        existing_legs: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None = None,
    ) -> Any:
        """Resize reduce-only TP/SL to the live position size.

        When both legs already rest, modify them in place. Otherwise cancel
        and place a ``positionTpsl`` pair. Triggers are Decimal-quantized
        before they hit the wire.
        """
        from hl_bot.execution.brackets import (
            order_response_ok,
            quantize_size,
            quantize_trigger_px,
            wire_float,
        )

        sz = wire_float(quantize_size(size, sz_decimals))
        sl = wire_float(quantize_trigger_px(stop_px, sz_decimals))
        tp = wire_float(quantize_trigger_px(tp_px, sz_decimals))
        # Closing side: sell a long, buy a short.
        is_buy = side == "short"
        logger.warning(
            "LIVE BRACKETS: %s %s sz=%s sl=%s tp=%s",
            coin,
            side,
            sz,
            sl,
            tp,
        )

        def _leg(trigger: float, kind: str) -> dict[str, Any]:
            return {
                "coin": coin,
                "is_buy": is_buy,
                "sz": sz,
                "limit_px": trigger,
                "order_type": {
                    "trigger": {
                        "triggerPx": trigger,
                        "isMarket": True,
                        "tpsl": kind,
                    }
                },
                "reduce_only": True,
            }

        # Prefer modify when both legs already rest. Cancel+replace opens a
        # window where a crash leaves the position naked (the BLUR failure).
        by_kind: dict[str, list[int]] = {}
        for oid, kind in existing_legs or []:
            if kind in {"tp", "sl"} and oid:
                by_kind.setdefault(kind, []).append(int(oid))
        both_legs = (
            len(by_kind.get("sl", [])) == 1
            and len(by_kind.get("tp", [])) == 1
            and len(existing_legs or []) == 2
        )
        if both_legs:
            modifies = [
                {"oid": by_kind[kind][0], "order": _leg(trigger, kind)}
                for kind, trigger in (("sl", sl), ("tp", tp))
            ]
            try:
                modified = self._exchange.bulk_modify_orders_new(modifies)
            except Exception:
                logger.exception("bracket modify failed for %s; falling back to replace", coin)
            else:
                if order_response_ok(modified):
                    return modified
                logger.error("bracket modify rejected for %s: %s", coin, modified)

        oids = [int(oid) for oid in (cancel_oids or []) if oid]
        if oids:
            cancel_resp = self._exchange.bulk_cancel(
                [{"coin": coin, "oid": oid} for oid in oids]
            )
            if not order_response_ok(cancel_resp):
                raise RuntimeError(f"bracket cancel failed for {coin}: {cancel_resp}")
        resp = self._exchange.bulk_orders(
            [_leg(sl, "sl"), _leg(tp, "tp")],
            grouping="positionTpsl",
        )
        if not order_response_ok(resp):
            raise RuntimeError(f"bracket place failed for {coin}: {resp}")
        return resp

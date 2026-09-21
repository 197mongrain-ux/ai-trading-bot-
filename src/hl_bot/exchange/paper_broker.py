"""Paper broker: simulates fills at mark/mid with no network orders."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

Side = Literal["long", "short"]


@dataclass
class Position:
    side: Side
    size: float  # absolute size in coin units
    entry_price: float
    stop_price: float
    take_profit: float
    opened_at: float = field(default_factory=time.time)
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    @property
    def signed_size(self) -> float:
        return self.size if self.side == "long" else -self.size


@dataclass
class Fill:
    trade_id: str
    side: Side
    size: float
    price: float
    action: Literal["open", "close"]
    pnl: float = 0.0
    reason: str = ""
    ts: float = field(default_factory=time.time)


class PaperBroker:
    """Simulates a single-position BTC perp account filled at mark."""

    def __init__(self, starting_equity: float = 5000.0, symbol: str = "BTC"):
        self.symbol = symbol
        self.starting_equity = starting_equity
        self.equity = starting_equity
        self.realized_pnl = 0.0
        self.position: Position | None = None
        self.fills: list[Fill] = []
        self._mark: float = 0.0

    def set_mark(self, price: float) -> None:
        self._mark = float(price)

    @property
    def mark(self) -> float:
        return self._mark

    @property
    def has_position(self) -> bool:
        return self.position is not None

    def unrealized_pnl(self, mark: float | None = None) -> float:
        if self.position is None:
            return 0.0
        px = mark if mark is not None else self._mark
        pos = self.position
        if pos.side == "long":
            return (px - pos.entry_price) * pos.size
        return (pos.entry_price - px) * pos.size

    def equity_mark_to_market(self, mark: float | None = None) -> float:
        return self.starting_equity + self.realized_pnl + self.unrealized_pnl(mark)

    def open_position(
        self,
        side: Side,
        size: float,
        stop_price: float,
        take_profit: float,
        price: float | None = None,
    ) -> Fill:
        if self.position is not None:
            raise RuntimeError("Max 1 open position; flatten before opening another")
        if size <= 0:
            raise ValueError("size must be positive")
        if stop_price <= 0 or take_profit <= 0:
            raise ValueError("stop and take-profit required")

        fill_px = float(price if price is not None else self._mark)
        if fill_px <= 0:
            raise ValueError("mark/fill price must be set")

        pos = Position(
            side=side,
            size=size,
            entry_price=fill_px,
            stop_price=stop_price,
            take_profit=take_profit,
        )
        self.position = pos
        fill = Fill(
            trade_id=pos.trade_id,
            side=side,
            size=size,
            price=fill_px,
            action="open",
            reason="entry",
        )
        self.fills.append(fill)
        return fill

    def close_position(self, reason: str = "manual", price: float | None = None) -> Fill | None:
        if self.position is None:
            return None
        pos = self.position
        fill_px = float(price if price is not None else self._mark)
        if pos.side == "long":
            pnl = (fill_px - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - fill_px) * pos.size
        self.realized_pnl += pnl
        self.equity = self.starting_equity + self.realized_pnl
        fill = Fill(
            trade_id=pos.trade_id,
            side=pos.side,
            size=pos.size,
            price=fill_px,
            action="close",
            pnl=pnl,
            reason=reason,
        )
        self.fills.append(fill)
        self.position = None
        return fill

    def check_stops(self, mark: float | None = None) -> Fill | None:
        """Close if mark hits stop or take-profit. Returns fill if closed."""
        if self.position is None:
            return None
        px = mark if mark is not None else self._mark
        pos = self.position
        if pos.side == "long":
            if px <= pos.stop_price:
                return self.close_position(reason="stop", price=px)
            if px >= pos.take_profit:
                return self.close_position(reason="take_profit", price=px)
        else:
            if px >= pos.stop_price:
                return self.close_position(reason="stop", price=px)
            if px <= pos.take_profit:
                return self.close_position(reason="take_profit", price=px)
        return None

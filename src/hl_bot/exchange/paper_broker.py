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
    symbol: str = "BTC"
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
    symbol: str = "BTC"
    ts: float = field(default_factory=time.time)


def _pnl_for(pos: Position, px: float) -> float:
    if pos.side == "long":
        return (px - pos.entry_price) * pos.size
    return (pos.entry_price - px) * pos.size


class PaperBroker:
    """Simulates a multi-symbol perp account filled at mark.

    One open position max per symbol. Equity = cash (starting + realized)
    + sum of mark-to-market on all open positions.
    """

    def __init__(
        self,
        starting_equity: float = 5000.0,
        symbol: str = "BTC",
        symbols: tuple[str, ...] | list[str] | None = None,
    ):
        if symbols:
            self.symbols: tuple[str, ...] = tuple(s.upper() for s in symbols)
        else:
            self.symbols = (symbol.upper(),)
        self.default_symbol = self.symbols[0]
        self.symbol = self.default_symbol  # backward compat
        self.starting_equity = starting_equity
        self.equity = starting_equity
        self.realized_pnl = 0.0
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self._marks: dict[str, float] = {}

    # --- mark helpers ---

    def set_mark(self, price: float, symbol: str | None = None) -> None:
        sym = (symbol or self.default_symbol).upper()
        self._marks[sym] = float(price)

    def get_mark(self, symbol: str | None = None) -> float:
        sym = (symbol or self.default_symbol).upper()
        return self._marks.get(sym, 0.0)

    @property
    def mark(self) -> float:
        """Mark for default symbol (single-symbol backward compat)."""
        return self._marks.get(self.default_symbol, 0.0)

    # --- position queries ---

    @property
    def has_position(self) -> bool:
        return bool(self.positions)

    def has_position_for(self, symbol: str) -> bool:
        return symbol.upper() in self.positions

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    @property
    def position(self) -> Position | None:
        """Position for default symbol (single-symbol backward compat)."""
        return self.positions.get(self.default_symbol)

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol.upper())

    def unrealized_pnl(self, mark: float | None = None, symbol: str | None = None) -> float:
        if symbol is not None:
            sym = symbol.upper()
            pos = self.positions.get(sym)
            if pos is None:
                return 0.0
            px = float(mark if mark is not None else self._marks.get(sym, 0.0))
            return _pnl_for(pos, px)

        total = 0.0
        for sym, pos in self.positions.items():
            if mark is not None and sym == self.default_symbol:
                px = float(mark)
            else:
                px = float(self._marks.get(sym, 0.0))
            total += _pnl_for(pos, px)
        return total

    def equity_mark_to_market(
        self,
        mark: float | None = None,
        marks: dict[str, float] | None = None,
    ) -> float:
        if marks:
            for s, p in marks.items():
                self._marks[s.upper()] = float(p)
        return self.starting_equity + self.realized_pnl + self.unrealized_pnl(mark)

    def open_position(
        self,
        side: Side,
        size: float,
        stop_price: float,
        take_profit: float,
        price: float | None = None,
        symbol: str | None = None,
    ) -> Fill:
        sym = (symbol or self.default_symbol).upper()
        if sym in self.positions:
            raise RuntimeError(
                f"Max 1 open position per symbol ({sym}); flatten before opening another"
            )
        if size <= 0:
            raise ValueError("size must be positive")
        if stop_price <= 0 or take_profit <= 0:
            raise ValueError("stop and take-profit required")

        fill_px = float(price if price is not None else self._marks.get(sym, 0.0))
        if fill_px <= 0:
            raise ValueError("mark/fill price must be set")

        pos = Position(
            side=side,
            size=size,
            entry_price=fill_px,
            stop_price=stop_price,
            take_profit=take_profit,
            symbol=sym,
        )
        self.positions[sym] = pos
        fill = Fill(
            trade_id=pos.trade_id,
            side=side,
            size=size,
            price=fill_px,
            action="open",
            reason="entry",
            symbol=sym,
        )
        self.fills.append(fill)
        return fill

    def close_position(
        self,
        reason: str = "manual",
        price: float | None = None,
        symbol: str | None = None,
    ) -> Fill | None:
        if symbol is not None:
            sym = symbol.upper()
        elif len(self.positions) == 1:
            sym = next(iter(self.positions))
        else:
            sym = self.default_symbol

        pos = self.positions.get(sym)
        if pos is None:
            return None

        fill_px = float(price if price is not None else self._marks.get(sym, 0.0))
        pnl = _pnl_for(pos, fill_px)
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
            symbol=sym,
        )
        self.fills.append(fill)
        del self.positions[sym]
        return fill

    def check_stops(
        self, mark: float | None = None, symbol: str | None = None
    ) -> Fill | None:
        """Close if mark hits stop or take-profit. Returns fill if closed.

        If ``symbol`` is given, only that symbol is checked. Otherwise all open
        positions are checked (first hit wins / returned).
        """
        if symbol is not None:
            targets = [symbol.upper()] if symbol.upper() in self.positions else []
        else:
            targets = list(self.positions.keys())

        for sym in targets:
            pos = self.positions[sym]
            px = float(mark if mark is not None else self._marks.get(sym, 0.0))
            if pos.side == "long":
                if px <= pos.stop_price:
                    return self.close_position(reason="stop", price=px, symbol=sym)
                if px >= pos.take_profit:
                    return self.close_position(reason="take_profit", price=px, symbol=sym)
            else:
                if px >= pos.stop_price:
                    return self.close_position(reason="stop", price=px, symbol=sym)
                if px <= pos.take_profit:
                    return self.close_position(reason="take_profit", price=px, symbol=sym)
        return None

    def close_all(
        self, reason: str = "flatten", marks: dict[str, float] | None = None
    ) -> list[Fill]:
        """Close every open position. Returns list of fills."""
        fills: list[Fill] = []
        for sym in list(self.positions.keys()):
            px = None
            if marks and sym in marks:
                px = marks[sym]
            fill = self.close_position(reason=reason, price=px, symbol=sym)
            if fill:
                fills.append(fill)
        return fills

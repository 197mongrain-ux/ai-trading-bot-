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

    Positions are keyed by ``trade_id`` so the same symbol can hold multiple
    independent opens (each with its own stop/tp/size). Equity = cash
    (starting + realized) + sum of mark-to-market on all open positions.

    ``max_positions_per_symbol``: 0 = unlimited per symbol (still subject to
    caller / account risk caps).
    """

    def __init__(
        self,
        starting_equity: float = 5000.0,
        symbol: str = "BTC",
        symbols: tuple[str, ...] | list[str] | None = None,
        max_positions_per_symbol: int = 0,
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
        # trade_id -> Position (multiple per symbol allowed)
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self._marks: dict[str, float] = {}
        self.max_positions_per_symbol = int(max_positions_per_symbol)

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

    def positions_for(self, symbol: str) -> list[Position]:
        """Open positions for ``symbol`` in open order (oldest first)."""
        sym = symbol.upper()
        return [p for p in self.positions.values() if p.symbol == sym]

    def position_count_for(self, symbol: str) -> int:
        return len(self.positions_for(symbol))

    def has_position_for(self, symbol: str) -> bool:
        return self.position_count_for(symbol) > 0

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    @property
    def position(self) -> Position | None:
        """Oldest position for default symbol (single-symbol backward compat)."""
        poses = self.positions_for(self.default_symbol)
        return poses[0] if poses else None

    def get_position(self, symbol: str) -> Position | None:
        """Oldest open position for symbol (backward compat)."""
        poses = self.positions_for(symbol)
        return poses[0] if poses else None

    def get_position_by_id(self, trade_id: str) -> Position | None:
        return self.positions.get(trade_id)

    def unrealized_pnl(self, mark: float | None = None, symbol: str | None = None) -> float:
        if symbol is not None:
            sym = symbol.upper()
            total = 0.0
            px = float(mark if mark is not None else self._marks.get(sym, 0.0))
            for pos in self.positions_for(sym):
                total += _pnl_for(pos, px)
            return total

        total = 0.0
        for pos in self.positions.values():
            if mark is not None and pos.symbol == self.default_symbol:
                px = float(mark)
            else:
                px = float(self._marks.get(pos.symbol, 0.0))
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
        if self.max_positions_per_symbol > 0:
            if self.position_count_for(sym) >= self.max_positions_per_symbol:
                raise RuntimeError(
                    f"Max {self.max_positions_per_symbol} open position(s) per "
                    f"symbol ({sym}); flatten or wait before opening another"
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
        self.positions[pos.trade_id] = pos
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
        trade_id: str | None = None,
    ) -> Fill | None:
        """Close one position by trade_id, or the oldest for ``symbol``.

        If neither is given and exactly one position is open, close that one;
        otherwise close the oldest on the default symbol.
        """
        pos: Position | None = None
        if trade_id is not None:
            pos = self.positions.get(trade_id)
        elif symbol is not None:
            poses = self.positions_for(symbol)
            pos = poses[0] if poses else None
        elif len(self.positions) == 1:
            pos = next(iter(self.positions.values()))
        else:
            poses = self.positions_for(self.default_symbol)
            pos = poses[0] if poses else None

        if pos is None:
            return None

        sym = pos.symbol
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
        del self.positions[pos.trade_id]
        return fill

    def check_stops(
        self, mark: float | None = None, symbol: str | None = None
    ) -> list[Fill]:
        """Evaluate stop/TP on each matching open position.

        Returns a list of close fills (may be empty). Each position is checked
        independently so one stop does not close siblings on the same symbol.
        """
        if symbol is not None:
            candidates = list(self.positions_for(symbol))
        else:
            candidates = list(self.positions.values())

        closed: list[Fill] = []
        for pos in candidates:
            # Skip if already closed earlier in this pass
            if pos.trade_id not in self.positions:
                continue
            px = float(
                mark if mark is not None else self._marks.get(pos.symbol, 0.0)
            )
            hit: str | None = None
            if pos.side == "long":
                if px <= pos.stop_price:
                    hit = "stop"
                elif px >= pos.take_profit:
                    hit = "take_profit"
            else:
                if px >= pos.stop_price:
                    hit = "stop"
                elif px <= pos.take_profit:
                    hit = "take_profit"
            if hit:
                fill = self.close_position(
                    reason=hit, price=px, trade_id=pos.trade_id
                )
                if fill:
                    closed.append(fill)
        return closed

    def close_all(
        self, reason: str = "flatten", marks: dict[str, float] | None = None
    ) -> list[Fill]:
        """Close every open position. Returns list of fills."""
        fills: list[Fill] = []
        for tid in list(self.positions.keys()):
            pos = self.positions.get(tid)
            if pos is None:
                continue
            px = None
            if marks and pos.symbol in marks:
                px = marks[pos.symbol]
            fill = self.close_position(reason=reason, price=px, trade_id=tid)
            if fill:
                fills.append(fill)
        return fills

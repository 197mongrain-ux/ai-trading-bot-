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
    size: float  # absolute size in coin units (remaining after scale-outs)
    entry_price: float
    stop_price: float
    take_profit: float
    symbol: str = "BTC"
    opened_at: float = field(default_factory=time.time)
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    # Scale-out / sell-into-strength state
    scaled: bool = False
    initial_size: float = 0.0
    initial_stop: float = 0.0  # original stop for R math after BE move

    def __post_init__(self) -> None:
        if self.initial_size <= 0:
            self.initial_size = float(self.size)
        if self.initial_stop <= 0:
            self.initial_stop = float(self.stop_price)

    @property
    def signed_size(self) -> float:
        return self.size if self.side == "long" else -self.size

    @property
    def risk_distance(self) -> float:
        """Original 1R distance in price units (entry ↔ initial stop)."""
        return abs(self.entry_price - self.initial_stop)

    def unrealized_r(self, mark: float) -> float:
        """Unrealized R-multiple vs original stop distance."""
        rd = self.risk_distance
        if rd <= 0:
            return 0.0
        if self.side == "long":
            return (float(mark) - self.entry_price) / rd
        return (self.entry_price - float(mark)) / rd


@dataclass
class Fill:
    trade_id: str
    side: Side
    size: float
    price: float
    action: Literal["open", "close", "scale_out"]
    pnl: float = 0.0
    reason: str = ""
    symbol: str = "BTC"
    ts: float = field(default_factory=time.time)
    remaining_size: float | None = None
    scaled: bool | None = None
    stop_price: float | None = None
    take_profit: float | None = None


def _pnl_for(pos: Position, px: float, size: float | None = None) -> float:
    sz = float(pos.size if size is None else size)
    if pos.side == "long":
        return (px - pos.entry_price) * sz
    return (pos.entry_price - px) * sz


def breakeven_stop(
    side: Side, entry: float, be_buffer_bps: float = 2.0
) -> float:
    """Stop at entry ± tiny buffer in the favorable direction.

    Long: slightly below entry (entry * (1 - bps/1e4)).
    Short: slightly above entry (entry * (1 + bps/1e4)).
    Buffer 0 → exactly entry.
    """
    buf = max(0.0, float(be_buffer_bps)) / 10_000.0
    if side == "long":
        return float(entry) * (1.0 - buf)
    return float(entry) * (1.0 + buf)


class PaperBroker:
    """Simulates a multi-symbol perp account filled at mark.

    Positions are keyed by ``trade_id`` so the same symbol can hold multiple
    independent opens (each with its own stop/tp/size). Equity = cash
    (starting + realized) + sum of mark-to-market on all open positions.

    ``max_positions_per_symbol``: 0 = unlimited per symbol (still subject to
    caller / account risk caps).

    Scale-out (sell into strength): at ``SCALE_OUT_R`` unrealized R, close a
    fraction of size, move stop to breakeven ± buffer, leave runner to TP.
    """

    def __init__(
        self,
        starting_equity: float = 5000.0,
        symbol: str = "BTC",
        symbols: tuple[str, ...] | list[str] | None = None,
        max_positions_per_symbol: int = 0,
        *,
        scale_out_enabled: bool = True,
        scale_out_r: float = 1.0,
        scale_out_pct: float = 0.5,
        be_buffer_bps: float = 2.0,
        runner_tp_r: float | None = None,
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
        self.scale_out_enabled = bool(scale_out_enabled)
        self.scale_out_r = float(scale_out_r)
        self.scale_out_pct = float(scale_out_pct)
        self.be_buffer_bps = float(be_buffer_bps)
        self.runner_tp_r = runner_tp_r

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
            initial_size=size,
            initial_stop=stop_price,
            scaled=False,
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
            remaining_size=size,
            scaled=False,
            stop_price=stop_price,
            take_profit=take_profit,
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
        # Prefer breakeven_stop label when scaled runner hits BE stop
        close_reason = reason
        if reason == "stop" and pos.scaled:
            close_reason = "breakeven_stop"
        fill = Fill(
            trade_id=pos.trade_id,
            side=pos.side,
            size=pos.size,
            price=fill_px,
            action="close",
            pnl=pnl,
            reason=close_reason,
            symbol=sym,
            remaining_size=0.0,
            scaled=pos.scaled,
            stop_price=pos.stop_price,
            take_profit=pos.take_profit,
        )
        self.fills.append(fill)
        del self.positions[pos.trade_id]
        return fill

    def close_partial(
        self,
        trade_id: str,
        *,
        close_size: float | None = None,
        close_pct: float | None = None,
        reason: str = "sell_into_strength",
        price: float | None = None,
        move_stop_to_be: bool = True,
        be_buffer_bps: float | None = None,
        runner_tp_r: float | None = None,
    ) -> Fill | None:
        """Close a fraction of an open leg; leave remainder with optional BE stop.

        Updates size, sets ``scaled=True``, optionally moves stop to breakeven
        ± buffer and/or retargets TP via ``runner_tp_r``.
        """
        pos = self.positions.get(trade_id)
        if pos is None:
            return None

        if close_size is not None:
            sz = float(close_size)
        elif close_pct is not None:
            pct = float(close_pct)
            if not (0.0 < pct < 1.0):
                raise ValueError("close_pct must be in (0, 1)")
            sz = pos.size * pct
        else:
            raise ValueError("close_size or close_pct required")

        if sz <= 0 or sz >= pos.size:
            # Full close path — caller should use close_position
            if sz >= pos.size:
                return self.close_position(
                    reason=reason, price=price, trade_id=trade_id
                )
            return None

        sym = pos.symbol
        fill_px = float(price if price is not None else self._marks.get(sym, 0.0))
        pnl = _pnl_for(pos, fill_px, size=sz)
        self.realized_pnl += pnl
        self.equity = self.starting_equity + self.realized_pnl

        pos.size = pos.size - sz
        pos.scaled = True

        buf = self.be_buffer_bps if be_buffer_bps is None else float(be_buffer_bps)
        if move_stop_to_be:
            pos.stop_price = breakeven_stop(pos.side, pos.entry_price, buf)

        r_tp = self.runner_tp_r if runner_tp_r is None else runner_tp_r
        if r_tp is not None and pos.risk_distance > 0:
            if pos.side == "long":
                pos.take_profit = pos.entry_price + float(r_tp) * pos.risk_distance
            else:
                pos.take_profit = pos.entry_price - float(r_tp) * pos.risk_distance

        fill = Fill(
            trade_id=pos.trade_id,
            side=pos.side,
            size=sz,
            price=fill_px,
            action="scale_out",
            pnl=pnl,
            reason=reason,
            symbol=sym,
            remaining_size=pos.size,
            scaled=True,
            stop_price=pos.stop_price,
            take_profit=pos.take_profit,
        )
        self.fills.append(fill)
        return fill

    def check_scale_outs(
        self,
        mark: float | None = None,
        symbol: str | None = None,
        *,
        scale_out_r: float | None = None,
        scale_out_pct: float | None = None,
        be_buffer_bps: float | None = None,
        runner_tp_r: float | None = None,
        enabled: bool | None = None,
    ) -> list[Fill]:
        """Scale out legs that have reached ``SCALE_OUT_R`` and are not yet scaled.

        Call before ``check_stops`` so a gap through 1R+TP scales then TPs remainder.
        """
        use_enabled = self.scale_out_enabled if enabled is None else bool(enabled)
        if not use_enabled:
            return []

        r_thresh = self.scale_out_r if scale_out_r is None else float(scale_out_r)
        pct = self.scale_out_pct if scale_out_pct is None else float(scale_out_pct)
        if not (0.0 < pct < 1.0) or r_thresh <= 0:
            return []

        if symbol is not None:
            candidates = list(self.positions_for(symbol))
        else:
            candidates = list(self.positions.values())

        scaled_fills: list[Fill] = []
        for pos in candidates:
            if pos.trade_id not in self.positions:
                continue
            if pos.scaled:
                continue
            px = float(
                mark if mark is not None else self._marks.get(pos.symbol, 0.0)
            )
            if px <= 0:
                continue
            if pos.unrealized_r(px) < r_thresh:
                continue
            fill = self.close_partial(
                pos.trade_id,
                close_pct=pct,
                reason="sell_into_strength",
                price=px,
                move_stop_to_be=True,
                be_buffer_bps=be_buffer_bps,
                runner_tp_r=runner_tp_r,
            )
            if fill:
                scaled_fills.append(fill)
        return scaled_fills

    def check_stops(
        self, mark: float | None = None, symbol: str | None = None
    ) -> list[Fill]:
        """Evaluate stop/TP on each matching open position.

        Returns a list of close fills (may be empty). Each position is checked
        independently so one stop does not close siblings on the same symbol.
        Scaled runners that hit the BE stop are labeled ``breakeven_stop``.
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

"""Hard-enforced risk checks and position sizing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    size: float = 0.0
    dollar_risk: float = 0.0


@dataclass
class RiskManager:
    """Enforces daily loss, drawdown, trade caps, consecutive losses, sizing.

    Shared across all symbols. Position size is ALWAYS derived from
    dollar risk / stop distance — leverage is a config ceiling only
    (exchange margin setting), never the sizing driver. Max one position
    per symbol (enforced by caller via has_open_position); max_open_positions
    caps total.

    max_trades_per_day: 0 means unlimited (no count cap). Strategy entry
    rules + daily loss / DD / consecutive-loss still apply.
    """

    starting_equity: float
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.08
    max_trades_per_day: int = 0  # 0 = unlimited
    max_consecutive_losses: int = 3
    leverage: int = 20
    kill_switch: bool = False
    max_open_positions: int = 3

    day_start_equity: float = 0.0
    high_water_mark: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    halted_daily_loss: bool = False
    killed: bool = False
    pause_entries: bool = False
    _day_utc: str = ""
    open_positions: int = 0

    def __post_init__(self) -> None:
        if self.day_start_equity <= 0:
            self.day_start_equity = self.starting_equity
        if self.high_water_mark <= 0:
            self.high_water_mark = self.starting_equity
        self._day_utc = self._utc_day()

    @staticmethod
    def _utc_day(now: datetime | None = None) -> str:
        n = now or datetime.now(timezone.utc)
        return n.strftime("%Y-%m-%d")

    def maybe_roll_day(self, equity: float, now: datetime | None = None) -> None:
        """Reset daily counters at UTC midnight."""
        day = self._utc_day(now)
        if day != self._day_utc:
            self._day_utc = day
            self.day_start_equity = equity
            self.trades_today = 0
            self.halted_daily_loss = False
            self.pause_entries = False
            # consecutive losses intentionally persist across days? Spec: pause
            # after 3 consecutive — reset on new day is friendlier; keep reset.
            self.consecutive_losses = 0

    def update_equity(self, equity: float) -> None:
        if equity > self.high_water_mark:
            self.high_water_mark = equity
        dd = (self.high_water_mark - equity) / self.high_water_mark if self.high_water_mark else 0.0
        if dd >= self.max_drawdown_pct:
            self.killed = True
        daily_loss = (self.day_start_equity - equity) / self.day_start_equity if self.day_start_equity else 0.0
        if daily_loss >= self.max_daily_loss_pct:
            self.halted_daily_loss = True

    def set_kill_switch(self, active: bool) -> None:
        if active:
            self.killed = True
            self.kill_switch = True

    def record_trade_open(self) -> None:
        self.trades_today += 1
        self.open_positions += 1

    def record_trade_close(self, pnl: float) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.pause_entries = True
        else:
            self.consecutive_losses = 0

    def size_position(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
    ) -> RiskDecision:
        """Size = dollar_risk / stop_distance. Caps notional by leverage * equity."""
        if entry_price <= 0 or stop_price <= 0:
            return RiskDecision(False, "invalid prices")
        stop_dist = abs(entry_price - stop_price)
        if stop_dist <= 0:
            return RiskDecision(False, "stop distance is zero")

        dollar_risk = equity * self.risk_per_trade
        size = dollar_risk / stop_dist

        max_notional = equity * self.leverage
        notional = size * entry_price
        if notional > max_notional:
            size = max_notional / entry_price
            dollar_risk = size * stop_dist

        if size <= 0:
            return RiskDecision(False, "computed size <= 0")

        return RiskDecision(True, "ok", size=size, dollar_risk=dollar_risk)

    def allow_entry(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        *,
        has_open_position: bool = False,
        open_position_count: int | None = None,
        kill_file_active: bool = False,
        env_kill: bool = False,
    ) -> RiskDecision:
        self.maybe_roll_day(equity)
        self.update_equity(equity)

        if env_kill or kill_file_active or self.kill_switch or self.killed:
            self.killed = True
            return RiskDecision(False, "kill switch active")
        if self.halted_daily_loss:
            return RiskDecision(False, "max daily loss reached — halted until next UTC day")
        if self.pause_entries:
            return RiskDecision(False, "paused after consecutive losses")
        # 0 = unlimited daily trade count
        if self.max_trades_per_day > 0 and self.trades_today >= self.max_trades_per_day:
            return RiskDecision(False, "max trades/day reached")
        # No averaging down on the same symbol
        if has_open_position:
            return RiskDecision(False, "already in position for symbol")
        count = self.open_positions if open_position_count is None else open_position_count
        if count >= self.max_open_positions:
            return RiskDecision(False, "max open positions reached")
        if stop_price <= 0:
            return RiskDecision(False, "stop required")

        sizing = self.size_position(equity, entry_price, stop_price)
        if not sizing.allowed:
            return sizing
        return sizing

    def should_flatten(self, equity: float, kill_file_active: bool = False, env_kill: bool = False) -> tuple[bool, str]:
        self.maybe_roll_day(equity)
        self.update_equity(equity)
        if env_kill or kill_file_active or self.kill_switch or self.killed:
            self.killed = True
            return True, "kill switch"
        if self.halted_daily_loss:
            return True, "daily loss limit"
        return False, ""

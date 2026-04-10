"""Position management module — Sweep & Reclaim Strategy v1.0.

Single-entry positions with wick-based stops. No more DCA pyramid.

Position = one surgical entry at the reclaim candle close.
Stop = under the sweep wick with dynamic buffer (vol_ratio scaled).
Target = FVG if available, otherwise 2R fallback.
Trail-to-BE after 1R profit.
"""

import config


class SweepPosition:
    """Tracks a single-entry Sweep & Reclaim position."""

    def __init__(self, direction: str, zone: dict, atr: float,
                 risk_amount: float, sweep_wick: float, vol_ratio: float = 1.0):
        self.direction = direction
        self.zone = zone
        self.atr = atr
        self.risk_amount = risk_amount
        self.vol_ratio = vol_ratio
        self.mode = "sweep_reclaim"

        # Entry tracking
        self.entry_price: float = 0.0
        self.entry_time = None
        self.size: float = 0.0

        # Stop-loss: sweep wick + dynamic buffer
        buffer = atr * config.SWEEP_STOP_BUFFER_ATR * vol_ratio
        if direction == "long":
            self.stop_loss = sweep_wick - buffer
        else:
            self.stop_loss = sweep_wick + buffer

        self.initial_stop = self.stop_loss

        # Take-profit (set after entry, may be updated with FVG target)
        self.take_profit: float = 0.0
        self.fvg_target: float | None = None

    @property
    def entry_count(self) -> int:
        return 1 if self.entry_price > 0 else 0

    @property
    def is_full(self) -> bool:
        return self.entry_price > 0

    @property
    def avg_entry_price(self) -> float:
        return self.entry_price

    @property
    def total_size(self) -> float:
        return self.size

    @property
    def first_entry_time(self):
        return self.entry_time

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss) if self.entry_price > 0 else 0

    @property
    def risk_reward(self) -> float:
        if self.stop_distance > 0 and self.take_profit > 0:
            reward = abs(self.take_profit - self.entry_price)
            return reward / self.stop_distance
        return 0.0

    def execute_entry(self, price: float, time, leverage: float):
        """Execute the single entry. Position size = risk / stop_distance."""
        self.entry_price = price
        self.entry_time = time

        stop_dist = abs(price - self.stop_loss)
        if stop_dist > 0:
            self.size = self.risk_amount / stop_dist
        else:
            self.size = self.risk_amount / price

        self._update_take_profit()

    def set_fvg_target(self, fvg_price: float):
        """Set FVG-based take-profit target if it offers better R:R than 2R."""
        if fvg_price is None:
            return

        fvg_distance = abs(fvg_price - self.entry_price)
        default_distance = abs(self.take_profit - self.entry_price) if self.take_profit else 0

        # Only use FVG target if it's in the right direction and at least 1R
        min_distance = self.stop_distance * 1.0  # At least 1R
        if fvg_distance >= min_distance:
            if self.direction == "long" and fvg_price > self.entry_price:
                self.fvg_target = fvg_price
                if fvg_distance < default_distance:
                    self.take_profit = fvg_price
            elif self.direction == "short" and fvg_price < self.entry_price:
                self.fvg_target = fvg_price
                if fvg_distance < default_distance:
                    self.take_profit = fvg_price

    def _update_take_profit(self):
        """Calculate take-profit at 2R from entry (fallback target)."""
        if self.entry_price <= 0:
            return

        risk_distance = abs(self.entry_price - self.stop_loss)
        target_r = config.SWEEP_TARGET_R

        if self.direction == "long":
            self.take_profit = self.entry_price + risk_distance * target_r
        else:
            self.take_profit = self.entry_price - risk_distance * target_r

    def should_trail_stop(self, price: float) -> float | None:
        """Trail stop to break-even after 1R profit."""
        if self.entry_price <= 0:
            return None

        risk_distance = abs(self.entry_price - self.initial_stop)
        trail_r = config.SWEEP_TRAIL_BE_R

        if self.direction == "long":
            unrealized_r = (price - self.entry_price) / risk_distance if risk_distance > 0 else 0
            if unrealized_r >= trail_r:
                new_stop = self.entry_price * 1.001  # Small buffer for fees
                return max(new_stop, self.stop_loss)
        else:
            unrealized_r = (self.entry_price - price) / risk_distance if risk_distance > 0 else 0
            if unrealized_r >= trail_r:
                new_stop = self.entry_price * 0.999
                return min(new_stop, self.stop_loss)

        return None

    def check_stop_loss(self, high: float, low: float) -> bool:
        """Check if stop-loss was hit."""
        if self.direction == "long":
            return low <= self.stop_loss
        else:
            return high >= self.stop_loss

    def check_take_profit(self, high: float, low: float) -> bool:
        """Check if take-profit was hit."""
        if self.direction == "long":
            return high >= self.take_profit
        else:
            return low <= self.take_profit

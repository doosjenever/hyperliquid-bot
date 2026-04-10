"""Finite State Machine — Sweep & Reclaim Strategy v1.0.

Each asset gets its own FSM instance. A global PortfolioManager
coordinates circuit breakers and risk across all FSMs.

States:
  IDLE            -> No position, watching S/R zones
  EVALUATING      -> Scanning for sweep candidates near zones
  SWEEP_DETECTED  -> Sweep detected, waiting for reclaim
  RECLAIM_PENDING -> Reclaim detected, scoring confluence
  IN_POSITION     -> Position open, monitoring exits
  EXIT_PENDING    -> Exit signal, waiting for order execution
  COOLDOWN        -> Post-exit pause (adaptive: 3 * candle period * vol_ratio)
  SYNCING         -> WebSocket reconnect, reconciling state via REST
  HALTED          -> Circuit breaker active (global)

Designed by Claude (Opus) and Krabje (OpenClaw).
"""

import asyncio
import logging
import random
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from strategy.asset_profile import AssetProfile, load_or_build_profile
from strategy.dca import SweepPosition
from strategy.confluence import (
    calculate_rsi, calculate_atr, calculate_ema,
    score_sweep_confluence, find_sweepable_zones,
)
from strategy.sweep_reclaim import (
    detect_sweep, detect_reclaim, check_sweep_still_valid,
    calculate_cvd_proxy, cvd_confirms_direction,
    find_fair_value_gaps, find_fvg_target,
)
from strategy.support_resistance import find_sr_zones
from strategy.volume_profile import calculate_volume_profile
from execution.orders import OrderExecutor

logger = logging.getLogger(__name__)


class State(Enum):
    IDLE = auto()
    EVALUATING = auto()
    SWEEP_DETECTED = auto()
    RECLAIM_PENDING = auto()
    IN_POSITION = auto()
    EXIT_PENDING = auto()
    COOLDOWN = auto()
    SYNCING = auto()
    HALTED = auto()


@dataclass
class OrderbookSnapshot:
    """Latest L2 orderbook state for an asset."""
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float:
        return float(self.bids[0]["px"]) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return float(self.asks[0]["px"]) if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return 0.0

    @property
    def spread_bps(self) -> float:
        if self.best_bid > 0:
            return (self.best_ask - self.best_bid) / self.best_bid * 10_000
        return 0.0


@dataclass
class TradeEvent:
    """A single trade from the trades feed."""
    coin: str
    side: str  # "B" or "A" (buy/sell aggressor)
    price: float
    size: float
    timestamp: float


class FsmBot:
    """Per-asset FSM trading bot — Sweep & Reclaim Strategy v1.0.

    Receives events via an asyncio.Queue from the central WebSocket multiplexer.
    Processes orderbook updates, trade events, and candle closes.
    """

    def __init__(self, symbol: str, profile: AssetProfile, equity: float,
                 executor: OrderExecutor = None, margin_check=None, max_leverage: int = None):
        self.symbol = symbol
        self.profile = profile
        self.equity = equity
        self.executor = executor
        self._margin_check = margin_check
        self.max_leverage = max_leverage or 5
        self.state = State.IDLE
        self.queue: asyncio.Queue = asyncio.Queue()

        # Position state
        self.position: SweepPosition | None = None

        # Sweep state
        self._active_sweep: dict | None = None

        # Market data (continuously updated from WS)
        self.orderbook = OrderbookSnapshot()
        self.recent_trades: list[TradeEvent] = []
        self.last_price: float = profile.price

        # S/R zones and volume profile (recalculated periodically)
        self.htf_zones: dict | None = None
        self.volume_profile: dict | None = None

        # Cooldown timer
        self._cooldown_until: float = 0.0

        # Exit retry state (exponential backoff)
        self._exit_retries: int = 0
        self._exit_max_retries: int = 5
        self._exit_next_retry: float = 0.0

        # Rotation flag
        self._rotating_out: bool = False

        # Stats
        self.trades_completed: int = 0
        self.total_pnl: float = 0.0

        logger.info(f"[{symbol}] FSM initialized | state=IDLE | profile: {profile.summary()}")

    @property
    def cooldown_seconds(self) -> float:
        """Adaptive cooldown: 3 candles * vol_ratio."""
        vol_scale = max(1.0, self.profile.vol_ratio)
        return config.COOLDOWN_BASE_SECONDS * config.COOLDOWN_BASE_CANDLES * vol_scale

    def _calculate_live_cvd(self) -> dict:
        """Calculate CVD from actual WebSocket trade data.

        Uses recent_trades (trade-level data) for real buy/sell volume split.
        This is more accurate than the candle-based proxy used in backtesting.
        """
        if not self.recent_trades:
            return {"cvd_delta": 0, "buy_volume": 0, "sell_volume": 0,
                    "delta_ratio": 0, "total_volume": 0}

        buy_vol = sum(t.size for t in self.recent_trades if t.side == "B")
        sell_vol = sum(t.size for t in self.recent_trades if t.side == "A")
        total = buy_vol + sell_vol
        delta = buy_vol - sell_vol
        delta_ratio = delta / total if total > 0 else 0.0

        return {
            "cvd_delta": delta,
            "buy_volume": buy_vol,
            "sell_volume": sell_vol,
            "delta_ratio": delta_ratio,
            "total_volume": total,
        }

    async def run(self):
        """Main event loop — processes events from the queue."""
        logger.info(f"[{self.symbol}] FSM started")
        while True:
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=60.0)
                await self._process_event(event)
            except asyncio.TimeoutError:
                await self._heartbeat()
            except asyncio.CancelledError:
                logger.info(f"[{self.symbol}] FSM cancelled")
                break
            except Exception as e:
                logger.error(f"[{self.symbol}] FSM error: {e}", exc_info=True)

    async def _process_event(self, event: dict):
        """Route event to the appropriate handler based on type."""
        event_type = event.get("type")

        if event_type == "l2_update":
            self._update_orderbook(event["data"])
        elif event_type == "trade":
            self._record_trade(event["data"])
        elif event_type == "candle_close":
            await self._on_candle_close(event["data"])
        elif event_type == "sr_update":
            self.htf_zones = event["data"]["zones"]
            self.volume_profile = event["data"]["volume_profile"]
        elif event_type == "halt":
            await self._force_halt(event.get("reason", "circuit_breaker"))
        elif event_type == "resume":
            if self.state == State.HALTED:
                self.state = State.IDLE
                logger.info(f"[{self.symbol}] Resumed from HALTED")
        elif event_type == "sync_required":
            if self.state in (State.HALTED, State.SYNCING):
                return
            active_states = (State.SWEEP_DETECTED, State.RECLAIM_PENDING,
                             State.EXIT_PENDING, State.IN_POSITION, State.EVALUATING)
            if self.state in active_states or self.position is not None:
                logger.warning(f"[{self.symbol}] WS reconnect in {self.state.name} -- SYNCING")
                self.state = State.SYNCING
            else:
                logger.info(f"[{self.symbol}] WS reconnect in {self.state.name} -- safe")
        elif event_type == "exit_only":
            self._rotating_out = True
            if self.position is not None:
                logger.info(f"[{self.symbol}] Rotating out -- position continues until closed")
            else:
                logger.info(f"[{self.symbol}] Rotating out -- no position, ready to remove")
        elif event_type == "cancel_rotation":
            if self._rotating_out:
                self._rotating_out = False
                logger.info(f"[{self.symbol}] Rotation cancelled")

    def _update_orderbook(self, data: dict):
        """Update the local orderbook snapshot from L2 feed."""
        self.orderbook.bids = data.get("bids", [])
        self.orderbook.asks = data.get("asks", [])
        self.orderbook.timestamp = data.get("time", time.time())

        if self.orderbook.mid_price > 0:
            self.last_price = self.orderbook.mid_price

    def _record_trade(self, data: dict):
        """Record a trade event for CVD calculation."""
        trade = TradeEvent(
            coin=data.get("coin", self.symbol),
            side=data.get("side", ""),
            price=float(data.get("px", 0)),
            size=float(data.get("sz", 0)),
            timestamp=float(data.get("time", time.time())),
        )
        self.recent_trades.append(trade)
        if len(self.recent_trades) > 500:
            self.recent_trades = self.recent_trades[-500:]

        self.last_price = trade.price

    async def _on_candle_close(self, candle: dict):
        """Handle a new candle close — the main decision point."""
        now = time.time()

        if self.state == State.HALTED:
            return

        if self.state == State.SYNCING:
            await asyncio.sleep(random.uniform(0.1, 0.5))
            await self._reconcile()
            return

        if self.state == State.COOLDOWN:
            if now >= self._cooldown_until:
                self.state = State.IDLE
                logger.info(f"[{self.symbol}] Cooldown expired -> IDLE")
            else:
                return

        price = float(candle.get("close", self.last_price))
        self.last_price = price

        if self.state == State.IDLE:
            if self._rotating_out:
                return
            try:
                await self._scan_for_sweeps(candle)
            except Exception as e:
                logger.error(f"[{self.symbol}] Sweep scan crashed: {e}", exc_info=True)
                self.state = State.IDLE

        elif self.state == State.SWEEP_DETECTED:
            await self._wait_for_reclaim(candle)

        elif self.state == State.IN_POSITION:
            await self._manage_position(candle)

        elif self.state == State.EXIT_PENDING:
            exit_signal = self._check_exit(candle)
            if exit_signal:
                await self._execute_exit(exit_signal)

    async def _scan_for_sweeps(self, candle: dict):
        """Scan for sweeps near S/R zones."""
        if self.htf_zones is None or self.volume_profile is None:
            return

        if self._margin_check and not self._margin_check():
            return

        self.state = State.EVALUATING
        price = float(candle.get("close", self.last_price))
        atr = candle.get("atr")

        if atr is None or atr <= 0:
            self.state = State.IDLE
            return

        # Range filter: skip when market is too choppy (Kaufman Efficiency Ratio)
        # Threshold scales inversely with vol_ratio²: BTC (1.0) strict, alts relaxed
        if config.RANGE_FILTER_ENABLED:
            lookback = config.RANGE_FILTER_LOOKBACK
            closes = [t.price for t in self.recent_trades[-lookback:]] if len(self.recent_trades) >= lookback else []
            if len(closes) >= lookback:
                net_move = abs(closes[-1] - closes[0])
                sum_moves = sum(abs(closes[j] - closes[j - 1]) for j in range(1, len(closes)))
                efficiency = net_move / sum_moves if sum_moves > 0 else 0
                min_eff = config.RANGE_FILTER_MIN_EFFICIENCY / (self.profile.vol_ratio ** 2)
                if efficiency < min_eff:
                    self.state = State.IDLE
                    return

        high = float(candle.get("high", price))
        low = float(candle.get("low", price))

        candidates = find_sweepable_zones(price, self.htf_zones, atr)

        for candidate in candidates:
            candle_dict = {"high": high, "low": low, "close": price,
                           "open": float(candle.get("open", price))}
            sweep = detect_sweep(candle_dict, candidate["zone"], candidate["direction"], atr)

            if sweep:
                self._active_sweep = sweep
                self._active_sweep["atr"] = atr
                logger.info(
                    f"[{self.symbol}] SWEEP DETECTED {sweep['direction']} | "
                    f"depth={sweep['sweep_depth_atr']:.2f} ATR | "
                    f"zone={sweep['zone']['low']:.2f}-{sweep['zone']['high']:.2f}"
                )

                # Check if same candle also reclaims (V-reversal)
                reclaim = detect_reclaim(candle_dict, sweep)
                if reclaim:
                    await self._process_reclaim(candle, reclaim)
                else:
                    self.state = State.SWEEP_DETECTED
                return

        self.state = State.IDLE

    async def _wait_for_reclaim(self, candle: dict):
        """Wait for price to reclaim the zone after a sweep."""
        sweep = self._active_sweep
        if sweep is None:
            self.state = State.IDLE
            return

        atr = sweep.get("atr") or candle.get("atr") or 0
        if atr <= 0:
            self.state = State.IDLE
            self._active_sweep = None
            return

        price = float(candle.get("close", self.last_price))
        high = float(candle.get("high", price))
        low = float(candle.get("low", price))

        candle_dict = {"high": high, "low": low, "close": price,
                       "open": float(candle.get("open", price))}

        # Check if sweep is still valid (price-based window, not time-based)
        if not check_sweep_still_valid(candle_dict, sweep, atr):
            logger.info(f"[{self.symbol}] Sweep invalidated (too deep) -> COOLDOWN")
            self._active_sweep = None
            self._start_cooldown()
            return

        # Update sweep wick if price goes deeper (but still valid)
        if sweep["direction"] == "long" and low < sweep["sweep_wick"]:
            sweep["sweep_wick"] = low
            sweep["sweep_depth"] = sweep["zone"]["low"] - low
            sweep["sweep_depth_atr"] = sweep["sweep_depth"] / atr if atr > 0 else 0
        elif sweep["direction"] == "short" and high > sweep["sweep_wick"]:
            sweep["sweep_wick"] = high
            sweep["sweep_depth"] = high - sweep["zone"]["high"]
            sweep["sweep_depth_atr"] = sweep["sweep_depth"] / atr if atr > 0 else 0

        # Check for reclaim
        reclaim = detect_reclaim(candle_dict, sweep)
        if reclaim:
            await self._process_reclaim(candle, reclaim)

    async def _process_reclaim(self, candle: dict, reclaim: dict):
        """Process a reclaim candle — score confluence and enter if qualified."""
        sweep = self._active_sweep
        if sweep is None:
            self.state = State.IDLE
            return

        self.state = State.RECLAIM_PENDING

        # Calculate CVD from live trade data
        cvd = self._calculate_live_cvd()
        cvd_ok = cvd_confirms_direction(cvd, sweep["direction"])

        # Score confluence
        rsi_val = candle.get("rsi")
        ema_val = candle.get("ema_200")
        funding_val = candle.get("funding_rate")

        signal = score_sweep_confluence(
            direction=sweep["direction"],
            zone=sweep["zone"],
            volume_profile=self.volume_profile,
            rsi_value=rsi_val,
            funding_rate=funding_val,
            ema_200=ema_val,
            cvd_confirms=cvd_ok,
            price=reclaim["entry_price"],
            rsi_oversold=self.profile.rsi_oversold,
            rsi_overbought=self.profile.rsi_overbought,
        )

        logger.info(
            f"[{self.symbol}] RECLAIM {sweep['direction']} | "
            f"score={signal['score']} | components={signal['components']} | "
            f"CVD={'OK' if cvd_ok else 'FAIL'} (ratio={cvd['delta_ratio']:.3f})"
        )

        if signal["trade"]:
            atr = sweep.get("atr") or candle.get("atr") or 0

            # Min stop distance check
            risk_amount = self.equity * config.MAX_RISK_PER_TRADE
            pos = SweepPosition(
                direction=sweep["direction"],
                zone=sweep["zone"],
                atr=atr,
                risk_amount=risk_amount,
                sweep_wick=sweep["sweep_wick"],
                vol_ratio=self.profile.vol_ratio,
            )

            min_stop_dist = atr * config.SWEEP_MIN_STOP_DISTANCE_ATR
            stop_dist = abs(reclaim["entry_price"] - pos.stop_loss)
            if stop_dist < min_stop_dist:
                logger.info(f"[{self.symbol}] Stop too tight ({stop_dist:.2f} < {min_stop_dist:.2f}) -> skip")
                self._active_sweep = None
                self.state = State.IDLE
                return

            await self._execute_entry(pos, reclaim["entry_price"], candle)
        else:
            logger.info(f"[{self.symbol}] Score too low ({signal['score']}) -> COOLDOWN")
            self._active_sweep = None
            self._start_cooldown()

    async def _execute_entry(self, pos: SweepPosition, price: float, candle: dict):
        """Execute the sweep & reclaim entry."""
        sweep = self._active_sweep
        atr = sweep.get("atr") or candle.get("atr") or 0

        pos.execute_entry(price, datetime.now(timezone.utc), self.max_leverage)

        # Find FVG target (from recent candle data if available)
        # In live, we'd need candle history — for now use 2R fallback
        # FVG detection will be enhanced when we have candle buffer

        # Place order on exchange
        if self.executor:
            leverage = self.max_leverage
            is_buy = pos.direction == "long"
            size = pos.size

            lev_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.executor.set_leverage(self.symbol, leverage, cross=True)
            )
            if not lev_result["success"]:
                logger.error(f"[{self.symbol}] Failed to set leverage, aborting entry")
                self._active_sweep = None
                self.state = State.IDLE
                return

            order_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.executor.market_open(self.symbol, is_buy, size, slippage=config.MAX_SLIPPAGE)
            )
            if not order_result["success"]:
                logger.error(f"[{self.symbol}] Entry order rejected: {order_result}")
                self._active_sweep = None
                self.state = State.IDLE
                return

            # Update entry with actual fill data
            fills = order_result.get("fills", [])
            if fills and isinstance(fills[0], dict) and "filled" in fills[0]:
                fill_data = fills[0]["filled"]
                fill_px = float(fill_data.get("avgPx", price))
                fill_sz = float(fill_data.get("totalSz", size))
                pos.entry_price = fill_px
                if fill_sz < size * 0.99:
                    logger.warning(
                        f"[{self.symbol}] Partial fill: requested {size:.6f}, got {fill_sz:.6f}"
                    )
                    pos.size = fill_sz
                pos._update_take_profit()

        self.position = pos
        self.state = State.IN_POSITION
        logger.info(
            f"[{self.symbol}] ENTRY {pos.direction} @ {pos.entry_price:.2f} | "
            f"sweep_depth={sweep['sweep_depth_atr']:.2f} ATR | "
            f"SL={pos.stop_loss:.2f} | TP={pos.take_profit:.2f}"
        )

    async def _manage_position(self, candle: dict):
        """Manage an open position — check exits, trail stops."""
        pos = self.position
        if pos is None:
            self.state = State.IDLE
            return

        price = float(candle.get("close", self.last_price))
        high = float(candle.get("high", price))
        low = float(candle.get("low", price))
        rsi = candle.get("rsi")

        exit_signal = self._check_exit_detailed(price, high, low, rsi)
        if exit_signal:
            await self._execute_exit(exit_signal)
            return

        # Trail stop to break-even
        new_stop = pos.should_trail_stop(price)
        if new_stop is not None:
            old_stop = pos.stop_loss
            pos.stop_loss = new_stop
            logger.info(f"[{self.symbol}] Trail stop: {old_stop:.2f} -> {new_stop:.2f}")

    def _check_exit_detailed(self, price: float, high: float, low: float, rsi: float = None) -> dict | None:
        """Check all exit conditions."""
        pos = self.position
        if pos is None:
            return None

        if pos.direction == "long":
            if pos.check_stop_loss(high, low):
                return {"price": pos.stop_loss, "reason": "stop_loss"}
            if pos.check_take_profit(high, low):
                return {"price": pos.take_profit, "reason": "take_profit"}
            if rsi is not None and rsi > 75 and price > pos.avg_entry_price:
                return {"price": price, "reason": "rsi_exit"}
        else:
            if pos.check_stop_loss(high, low):
                return {"price": pos.stop_loss, "reason": "stop_loss"}
            if pos.check_take_profit(high, low):
                return {"price": pos.take_profit, "reason": "take_profit"}
            if rsi is not None and rsi < 25 and price < pos.avg_entry_price:
                return {"price": price, "reason": "rsi_exit"}

        return None

    def _check_exit(self, candle: dict) -> dict | None:
        """Convenience wrapper for candle-based exit check."""
        price = float(candle.get("close", self.last_price))
        high = float(candle.get("high", price))
        low = float(candle.get("low", price))
        rsi = candle.get("rsi")
        return self._check_exit_detailed(price, high, low, rsi)

    async def _execute_exit(self, exit_info: dict):
        """Execute an exit and transition to COOLDOWN.

        PnL is only booked AFTER a successful exchange close.
        """
        pos = self.position
        if pos is None:
            self.state = State.IDLE
            return

        exit_price = exit_info["price"]
        reason = exit_info["reason"]

        # Close position on exchange FIRST
        if self.executor:
            now = time.time()
            if now < self._exit_next_retry:
                return

            self.state = State.EXIT_PENDING

            close_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.executor.market_close(self.symbol, slippage=config.MAX_SLIPPAGE)
            )
            if not close_result["success"]:
                self._exit_retries += 1
                backoff = min(2 ** self._exit_retries, 30)
                self._exit_next_retry = now + backoff
                logger.error(
                    f"[{self.symbol}] Exit failed (attempt {self._exit_retries}/{self._exit_max_retries}) "
                    f"-- retry in {backoff}s"
                )
                if self._exit_retries >= self._exit_max_retries:
                    logger.critical(f"[{self.symbol}] Exit failed {self._exit_max_retries}x -- HALTING")
                    self.state = State.HALTED
                    self._exit_retries = 0
                return

            # Use actual fill price from exchange
            result_data = close_result.get("result", {})
            statuses = result_data.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and isinstance(statuses[0], dict) and "filled" in statuses[0]:
                fill_data = statuses[0]["filled"]
                actual_exit = float(fill_data.get("avgPx", exit_price))
                if actual_exit > 0:
                    exit_price = actual_exit

        self._exit_retries = 0
        self._exit_next_retry = 0.0

        # Calculate PnL
        from backtest.slippage import calculate_live_pnl
        pnl = calculate_live_pnl(
            pos.avg_entry_price, exit_price,
            pos.total_size, pos.direction,
        )

        self.equity += pnl["net_pnl"]
        self.total_pnl += pnl["net_pnl"]
        self.trades_completed += 1

        logger.info(
            f"[{self.symbol}] EXIT {pos.direction} @ {exit_price:.2f} | "
            f"reason={reason} | PnL=${pnl['net_pnl']:.2f} | equity=${self.equity:.2f}"
        )

        await self._save_trade(pos, exit_price, reason, pnl)

        self.position = None
        self._active_sweep = None

        if self._rotating_out:
            self.state = State.IDLE
            logger.info(f"[{self.symbol}] Trade closed + rotating out -- ready for removal")
        else:
            self._start_cooldown()

    def _start_cooldown(self):
        """Transition to COOLDOWN with adaptive duration."""
        self._cooldown_until = time.time() + self.cooldown_seconds
        self.state = State.COOLDOWN
        minutes = int(self.cooldown_seconds / 60)
        logger.info(f"[{self.symbol}] Cooldown {minutes}m (vol_ratio={self.profile.vol_ratio:.2f}x)")

    async def _force_halt(self, reason: str):
        """Emergency halt — close position if any, enter HALTED state."""
        if self.position is not None:
            logger.warning(f"[{self.symbol}] HALT with open position! Closing at market.")
            await self._execute_exit({"price": self.last_price, "reason": f"halt_{reason}"})

        self.state = State.HALTED
        logger.warning(f"[{self.symbol}] HALTED: {reason}")

    async def _save_trade(self, pos: SweepPosition, exit_price: float, reason: str, pnl: dict):
        """Save completed trade to SQLite audit trail."""
        try:
            from state.database import get_connection
            conn = get_connection()

            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_time TEXT,
                    exit_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    size REAL NOT NULL,
                    dca_entries INTEGER NOT NULL,
                    exit_reason TEXT NOT NULL,
                    gross_pnl REAL NOT NULL,
                    fees REAL NOT NULL,
                    slippage REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    equity_after REAL NOT NULL
                )
            """)

            conn.execute("""
                INSERT INTO trades (
                    symbol, direction, entry_time, exit_time,
                    entry_price, exit_price, stop_loss, take_profit,
                    size, dca_entries, exit_reason,
                    gross_pnl, fees, slippage, net_pnl, equity_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.symbol, pos.direction,
                pos.first_entry_time.isoformat() if pos.first_entry_time else None,
                datetime.now(timezone.utc).isoformat(),
                pos.avg_entry_price, exit_price,
                pos.stop_loss, pos.take_profit,
                pos.total_size, 1, reason,
                pnl.get("gross_pnl", 0), pnl.get("fees", 0),
                pnl.get("slippage", 0), pnl["net_pnl"], self.equity,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[{self.symbol}] Failed to save trade: {e}")

    async def _reconcile(self):
        """Reconcile local state with exchange state after reconnect."""
        if not self.executor:
            self.state = State.IDLE
            return

        recon = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.executor.reconcile(self.symbol)
        )

        if recon["has_position"]:
            pos_data = recon["position"]
            logger.info(
                f"[{self.symbol}] SYNC: found {pos_data['direction']} position "
                f"size={pos_data['size']} entry={pos_data['entry_price']:.2f}"
            )
            if self.position is None:
                atr = pos_data["entry_price"] * 0.01  # Fallback ATR estimate
                self.position = SweepPosition(
                    direction=pos_data["direction"],
                    zone={"low": pos_data["entry_price"] * 0.97,
                          "high": pos_data["entry_price"] * 1.03,
                          "price": pos_data["entry_price"], "strength": 1},
                    atr=atr,
                    risk_amount=self.equity * config.MAX_RISK_PER_TRADE,
                    sweep_wick=pos_data["entry_price"] * (0.98 if pos_data["direction"] == "long" else 1.02),
                    vol_ratio=self.profile.vol_ratio,
                )
                self.position.entry_price = pos_data["entry_price"]
                self.position.size = abs(pos_data["size"])
                self.position.entry_time = datetime.now(timezone.utc)
                self.position._update_take_profit()
            self.state = State.IN_POSITION
        else:
            if self.position is not None:
                logger.warning(f"[{self.symbol}] SYNC: local position but none on exchange -- clearing")
                self.position = None
            self.state = State.IDLE

        logger.info(f"[{self.symbol}] Synced -> {self.state.name}")

    async def _heartbeat(self):
        """Periodic check for stale state."""
        if self.state == State.SWEEP_DETECTED:
            # If we've been waiting too long for reclaim, give up
            if self._active_sweep and time.time() - self._active_sweep.get("_detect_time", 0) > 3600:
                logger.info(f"[{self.symbol}] Sweep expired (1h) -> COOLDOWN")
                self._active_sweep = None
                self._start_cooldown()

    def status(self) -> dict:
        """Return current FSM status for monitoring."""
        pos_info = None
        if self.position and self.position.is_full:
            pos_info = {
                "direction": self.position.direction,
                "entry_price": self.position.entry_price,
                "stop_loss": self.position.stop_loss,
                "take_profit": self.position.take_profit,
                "size": self.position.size,
            }

        sweep_info = None
        if self._active_sweep:
            sweep_info = {
                "direction": self._active_sweep["direction"],
                "sweep_wick": self._active_sweep["sweep_wick"],
                "depth_atr": self._active_sweep["sweep_depth_atr"],
            }

        return {
            "symbol": self.symbol,
            "state": self.state.name,
            "price": self.last_price,
            "equity": self.equity,
            "leverage": self.max_leverage,
            "trades": self.trades_completed,
            "total_pnl": self.total_pnl,
            "position": pos_info,
            "active_sweep": sweep_info,
            "orderbook_spread_bps": self.orderbook.spread_bps,
        }

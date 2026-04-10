"""Portfolio Manager — global coordinator for all per-asset FSMs.

Responsibilities:
- Spawns and manages per-asset FsmBot instances
- Global circuit breaker (5% portfolio drawdown in 15 min -> halt all)
- REST candle polling (exchange-verified OHLCV, no self-aggregation)
- Periodic S/R zone and indicator recalculation
- Status reporting + halt flag for CLI control

Architecture (designed with Krabje):
  ┌──────────────────┐
  │ PortfolioManager │  <- global circuit breaker, equity tracking
  ├──────────────────┤
  │ WebSocketMux     │  <- single WS (L2 orderbook + trades only)
  ├──────────────────┤
  │ REST Candle Poll │  <- exchange-verified OHLCV every 60s
  ├──────────────────┤
  │ FsmBot(BTC)      │  <- per-asset state machine
  │ FsmBot(ETH)      │
  │ FsmBot(SOL)      │
  │ FsmBot(AVAX)     │
  └──────────────────┘

Deployment (designed with Krabje):
  - Runs inside OpenClaw workspace on VPS (no separate Docker)
  - Krabje reads SQLite directly for status/PnL
  - CLI tool (cli.py) for kill/status commands
  - No HTTP/FastAPI — pure local SQLite + UNIX signals
"""

import asyncio
import logging
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import requests

import config
from execution.fsm import FsmBot, State
from execution.orders import OrderExecutor
from execution.websocket import WebSocketMux
from strategy.asset_profile import load_or_build_profile
from strategy.support_resistance import find_sr_zones
from strategy.volume_profile import calculate_volume_profile
from strategy.confluence import (
    calculate_rsi, calculate_atr, calculate_ema,
    calculate_mfi, calculate_cci,
)
from data.fetcher import fetch_and_cache, set_testnet
from data.universe import get_top_symbols, get_top_symbols_with_leverage

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Coordinates all per-asset FSMs with global risk management."""

    def __init__(
        self,
        assets: list[str] = None,
        equity: float = 10_000,
        testnet: bool = True,
        top_n: int = 25,
    ):
        self.testnet = testnet
        self.top_n = top_n
        self.initial_equity = equity
        self.equity = equity

        # Per-asset max leverage from exchange metadata
        self._max_leverage: dict[str, int] = {}

        # Dynamic universe — fetch from API if no explicit assets given
        if assets:
            self.assets = assets
            self._dynamic_universe = False
            # Still fetch leverage from exchange for explicit assets
            try:
                all_lev = get_top_symbols_with_leverage(n=200, testnet=testnet)
                self._max_leverage = {s: all_lev[s] for s in assets if s in all_lev}
            except Exception:
                pass  # Will use fallback of 5x per asset
        else:
            leverage_map = get_top_symbols_with_leverage(n=top_n, testnet=testnet)
            self.assets = list(leverage_map.keys())
            self._max_leverage = leverage_map
            self._dynamic_universe = True

        # Per-asset FSMs
        self.bots: dict[str, FsmBot] = {}

        # Per-asset FSM tasks (for dynamic lifecycle)
        self._fsm_tasks: dict[str, asyncio.Task] = {}

        # Order executor (shared across all FSMs)
        self.executor = OrderExecutor(testnet=testnet)

        # WebSocket multiplexer
        self.ws_mux = WebSocketMux(self.assets, testnet=testnet)

        # REST API URL
        self._api_url = (
            "https://api.hyperliquid-testnet.xyz/info" if testnet
            else "https://api.hyperliquid.xyz/info"
        )

        # Shared margin pool
        self._max_margin_usage = 0.75  # Max 75% of equity used as margin

        # Circuit breaker state
        self._equity_history: list[tuple[float, float]] = []  # (timestamp, equity)
        self._halted = False
        self._halt_until: float = 0.0

        # Candle polling state
        self._candle_interval = "1h"
        self._candle_poll_seconds = 60  # Poll REST every 60 seconds
        self._last_candle_time: dict[str, int] = {}  # Track last seen candle per asset

        # HTF data refresh interval
        self._htf_refresh_interval = 14400  # 4 hours
        self._last_htf_refresh: dict[str, float] = {}

        # Universe rotation interval (24 hours)
        self._universe_rotation_interval = 86400  # 24h
        self._last_universe_rotation: float = 0.0

        # Halt flag file (for CLI kill command)
        self._halt_flag = config.BASE_DIR / "state" / ".halt"

        # Task references
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        """Initialize and start all components."""
        # Configure fetcher for testnet/mainnet
        set_testnet(self.testnet)

        logger.info(f"PortfolioManager starting | assets={self.assets} | equity=${self.equity:.2f}")

        # Build profiles and FSMs
        await self._init_bots()

        # Initial S/R zone calculation
        await self._refresh_all_htf_data()

        # Register queues with WebSocket
        for symbol, bot in self.bots.items():
            self.ws_mux.register_queue(symbol, bot.queue)

        # Start tasks
        self._tasks = [
            asyncio.create_task(self.ws_mux.run(), name="ws_mux"),
            asyncio.create_task(self._candle_poll_loop(), name="candle_poll"),
            asyncio.create_task(self._circuit_breaker_loop(), name="circuit_breaker"),
            asyncio.create_task(self._htf_refresh_loop(), name="htf_refresh"),
            asyncio.create_task(self._halt_flag_loop(), name="halt_flag"),
            asyncio.create_task(self._status_loop(), name="status"),
        ]

        # Universe rotation (only if dynamic)
        if self._dynamic_universe:
            self._tasks.append(
                asyncio.create_task(self._universe_rotation_loop(), name="universe_rotation")
            )
            self._last_universe_rotation = time.time()

        # Start per-asset FSM tasks
        for symbol, bot in self.bots.items():
            task = asyncio.create_task(bot.run(), name=f"fsm_{symbol}")
            self._fsm_tasks[symbol] = task
            self._tasks.append(task)

        logger.info(f"All tasks started ({len(self._tasks)} total)")

        # Wait for all tasks
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("PortfolioManager shutting down...")
        finally:
            await self.stop()

    async def stop(self):
        """Gracefully stop all components."""
        logger.info("Stopping PortfolioManager...")
        for task in self._tasks:
            task.cancel()
        await self.ws_mux.stop()
        logger.info("PortfolioManager stopped")

    async def _init_bots(self):
        """Initialize FSM bots with calibrated profiles.

        Uses shared margin pool — each bot gets the full equity reference
        but risk is capped at 2% per trade by the FSM.
        """
        # Use actual account value from exchange if available
        loop = asyncio.get_event_loop()
        real_equity = await loop.run_in_executor(
            None, self.executor.get_account_value
        )
        if real_equity > 0:
            self.equity = real_equity
            self.initial_equity = real_equity
            logger.info(f"Using exchange account value: ${real_equity:,.2f}")

        df_btc = await loop.run_in_executor(
            None, lambda: fetch_and_cache("BTC", "1h", days=90)
        )

        for symbol in self.assets:
            await self._add_bot(symbol, df_btc)

        logger.info(f"Initialized {len(self.bots)} bots | shared equity=${self.equity:,.2f}")

    async def _add_bot(self, symbol: str, df_btc=None):
        """Add a single FSM bot for a symbol."""
        if symbol in self.bots:
            return

        loop = asyncio.get_event_loop()
        if df_btc is None:
            df_btc = await loop.run_in_executor(
                None, lambda: fetch_and_cache("BTC", "1h", days=90)
            )

        df = await loop.run_in_executor(
            None, lambda: fetch_and_cache(symbol, "1h", days=90)
        ) if symbol != "BTC" else df_btc
        if df.empty:
            logger.warning(f"No data for {symbol}, skipping")
            return

        df_ref = df_btc if symbol != "BTC" else None
        profile = load_or_build_profile(symbol, df, df_ref, use_db=True)

        # Shared margin pool — each bot sees total equity for risk calculation
        max_lev = self._max_leverage.get(symbol, 5)
        bot = FsmBot(symbol, profile, self.equity, executor=self.executor,
                     margin_check=self.check_margin_available, max_leverage=max_lev)
        self.bots[symbol] = bot

        logger.info(f"Initialized {symbol} | leverage={max_lev}x | {profile.summary()}")

    async def _refresh_all_htf_data(self):
        """Fetch HTF data and calculate S/R zones + volume profiles for all assets."""
        for symbol, bot in list(self.bots.items()):
            try:
                await self._refresh_htf_for_asset(symbol, bot)
            except Exception as e:
                logger.error(f"HTF refresh failed for {symbol}: {e}")

    async def _refresh_htf_for_asset(self, symbol: str, bot: FsmBot):
        """Refresh S/R zones and volume profile for a single asset."""
        loop = asyncio.get_event_loop()
        df_htf = await loop.run_in_executor(
            None, lambda: fetch_and_cache(symbol, "4h", days=config.SR_ROLLING_WINDOW_DAYS)
        )
        if df_htf.empty:
            return

        zones = find_sr_zones(df_htf, window=bot.profile.swing_window)
        vp = calculate_volume_profile(df_htf)

        # Send to FSM
        await bot.queue.put({
            "type": "sr_update",
            "data": {"zones": zones, "volume_profile": vp},
        })

        self._last_htf_refresh[symbol] = time.time()
        logger.info(f"[{symbol}] HTF refreshed | S={len(zones.get('support',[]))} R={len(zones.get('resistance',[]))} zones")

    async def _candle_poll_loop(self):
        """Poll REST API for new candle closes.

        Uses exchange-verified OHLCV data (not self-aggregated from ticks).
        Polls every 60 seconds. When a new candle close is detected,
        calculates indicators and pushes enriched candle to the FSM.
        """
        # Wait for initial data
        await asyncio.sleep(5.0)

        while True:
            try:
                await asyncio.sleep(self._candle_poll_seconds)

                for symbol, bot in list(self.bots.items()):
                    try:
                        candle = await self._fetch_latest_candle(symbol)
                        if candle is None:
                            continue

                        candle_t = candle["t"]
                        last_t = self._last_candle_time.get(symbol, 0)

                        if candle_t > last_t:
                            # New candle close detected
                            self._last_candle_time[symbol] = candle_t

                            # Enrich with indicators
                            enriched = await self._enrich_candle(symbol, candle)

                            await bot.queue.put({
                                "type": "candle_close",
                                "data": enriched,
                            })
                            logger.info(
                                f"[{symbol}] Candle close: {enriched['close']:.2f} | "
                                f"RSI={enriched.get('rsi', '?')} MFI={enriched.get('mfi', '?')}"
                            )
                    except Exception as e:
                        logger.error(f"[{symbol}] Candle poll error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Candle poll loop error: {e}")

    async def _fetch_latest_candle(self, symbol: str) -> dict | None:
        """Fetch the most recent closed candle from REST API."""
        now_ms = int(time.time() * 1000)
        # Fetch last 3 candles to ensure we get the latest closed one
        start_ms = now_ms - 3600_000 * 3  # 3 hours back for 1h candles

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": self._candle_interval,
                "startTime": start_ms,
                "endTime": now_ms,
            }
        }

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: requests.post(self._api_url, json=payload, timeout=10)
        )

        if resp.status_code != 200:
            logger.warning(f"[{symbol}] Candle API returned {resp.status_code}")
            return None

        candles = resp.json()
        if not candles:
            return None

        # Return the second-to-last candle (last closed one)
        # The very last candle might still be forming
        if len(candles) >= 2:
            return candles[-2]
        return candles[-1]

    async def _enrich_candle(self, symbol: str, raw_candle: dict) -> dict:
        """Enrich a REST candle with calculated indicators."""
        candle = {
            "open": float(raw_candle["o"]),
            "high": float(raw_candle["h"]),
            "low": float(raw_candle["l"]),
            "close": float(raw_candle["c"]),
            "volume": float(raw_candle["v"]),
            "datetime": datetime.fromtimestamp(raw_candle["t"] / 1000, tz=timezone.utc),
            "t": raw_candle["t"],
        }

        # Fetch recent LTF data for indicator calculation
        loop = asyncio.get_event_loop()
        df_ltf = await loop.run_in_executor(
            None, lambda: fetch_and_cache(symbol, "1h", days=7)
        )
        if df_ltf.empty:
            return candle

        rsi = calculate_rsi(df_ltf["close"], period=14)
        atr = calculate_atr(df_ltf, period=config.ATR_PERIOD)
        ema = calculate_ema(df_ltf["close"], period=200)
        mfi = calculate_mfi(df_ltf, period=14)
        cci = calculate_cci(df_ltf, period=20)

        def _last_valid(series):
            if series is not None and len(series) > 0:
                val = series.iloc[-1]
                if not pd.isna(val):
                    return float(val)
            return None

        candle["rsi"] = _last_valid(rsi)
        candle["atr"] = _last_valid(atr)
        candle["ema_200"] = _last_valid(ema)
        candle["mfi"] = _last_valid(mfi)
        candle["cci"] = _last_valid(cci)

        # Fetch current funding rate
        try:
            funding = await loop.run_in_executor(
                None, lambda: self._fetch_funding_rate(symbol)
            )
            candle["funding_rate"] = funding
        except Exception:
            candle["funding_rate"] = None

        return candle

    async def _circuit_breaker_loop(self):
        """Monitor portfolio equity for circuit breaker trigger.

        Checks every 10 seconds. If portfolio drops 5% in 15 minutes → halt all.
        """
        while True:
            try:
                await asyncio.sleep(10.0)
                now = time.time()

                # Check if we're in halt cooldown
                if self._halted and now < self._halt_until:
                    continue
                elif self._halted and now >= self._halt_until:
                    self._halted = False
                    logger.info("Circuit breaker cooldown expired, resuming all bots")
                    for bot in self.bots.values():
                        await bot.queue.put({"type": "resume"})
                    continue

                # Calculate current portfolio equity (initial + sum of all PnL)
                total_pnl = sum(bot.total_pnl for bot in self.bots.values())
                current_equity = self.initial_equity + total_pnl
                self._equity_history.append((now, current_equity))

                # Keep only last 15 minutes
                cutoff = now - config.CIRCUIT_BREAKER_WINDOW_MIN * 60
                self._equity_history = [(t, e) for t, e in self._equity_history if t >= cutoff]

                if len(self._equity_history) < 2:
                    continue

                # Check drawdown from peak in window
                peak_equity = max(e for _, e in self._equity_history)
                drawdown = (peak_equity - current_equity) / peak_equity

                if drawdown >= config.CIRCUIT_BREAKER_DRAWDOWN:
                    logger.warning(
                        f"CIRCUIT BREAKER TRIGGERED! "
                        f"Drawdown: {drawdown*100:.1f}% in {config.CIRCUIT_BREAKER_WINDOW_MIN}min | "
                        f"Peak: ${peak_equity:.2f} → Current: ${current_equity:.2f}"
                    )
                    self._halted = True
                    self._halt_until = now + config.CIRCUIT_BREAKER_PAUSE_HOURS * 3600

                    for bot in self.bots.values():
                        await bot.queue.put({
                            "type": "halt",
                            "reason": f"drawdown_{drawdown*100:.1f}pct",
                        })

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Circuit breaker error: {e}")

    async def _htf_refresh_loop(self):
        """Periodically refresh HTF data (S/R zones, volume profile)."""
        while True:
            try:
                await asyncio.sleep(60.0)
                now = time.time()

                for symbol, bot in list(self.bots.items()):
                    last = self._last_htf_refresh.get(symbol, 0)
                    if now - last >= self._htf_refresh_interval:
                        await self._refresh_htf_for_asset(symbol, bot)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"HTF refresh loop error: {e}")

    async def _universe_rotation_loop(self):
        """Every 24 hours, refresh the top N universe and rotate assets.

        New assets get a fresh FSM. Removed assets get _rotating_out signal
        (close position naturally via TP/SL/DCA, then remove).
        Assets returning to top N get their _rotating_out flag reset.
        """
        while True:
            try:
                await asyncio.sleep(3600.0)  # Check every hour
                now = time.time()

                if now - self._last_universe_rotation < self._universe_rotation_interval:
                    continue

                self._last_universe_rotation = now
                new_leverage_map = get_top_symbols_with_leverage(n=self.top_n, testnet=self.testnet)
                self._max_leverage.update(new_leverage_map)
                new_universe = list(new_leverage_map.keys())

                current = set(self.bots.keys())
                target = set(new_universe)

                entering = target - current
                leaving = current - target

                # Check for assets returning to top N (cancel their rotation)
                returning = set()
                for symbol in target & current:
                    bot = self.bots.get(symbol)
                    if bot and bot._rotating_out:
                        returning.add(symbol)
                        await bot.queue.put({"type": "cancel_rotation"})

                if not entering and not leaving and not returning:
                    logger.info(f"Universe unchanged ({len(current)} assets)")
                    continue

                logger.info(
                    f"Universe rotation: +{len(entering)} -{len(leaving)} "
                    f"↩{len(returning)} | "
                    f"In: {', '.join(entering) or 'none'} | "
                    f"Out: {', '.join(leaving) or 'none'} | "
                    f"Back: {', '.join(returning) or 'none'}"
                )

                # Signal leaving assets
                for symbol in leaving:
                    bot = self.bots.get(symbol)
                    if bot:
                        await bot.queue.put({"type": "exit_only"})

                # Add new assets
                loop = asyncio.get_event_loop()
                df_btc = await loop.run_in_executor(
                    None, lambda: fetch_and_cache("BTC", "1h", days=90)
                )
                for symbol in entering:
                    await self._add_bot(symbol, df_btc)

                    if symbol in self.bots:
                        bot = self.bots[symbol]
                        # Register WS queue
                        self.ws_mux.register_queue(symbol, bot.queue)
                        await self.ws_mux.subscribe_asset(symbol)
                        # HTF data
                        await self._refresh_htf_for_asset(symbol, bot)
                        # Start FSM task
                        task = asyncio.create_task(bot.run(), name=f"fsm_{symbol}")
                        self._fsm_tasks[symbol] = task
                        self._tasks.append(task)

                # Clean up fully exited bots
                await self._cleanup_rotated_bots()

                # Update active assets list
                self.assets = [s for s in new_universe if s in self.bots]

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Universe rotation error: {e}", exc_info=True)

    async def _cleanup_rotated_bots(self):
        """Remove bots that are in EXIT_ONLY/IDLE with no position."""
        to_remove = []
        for symbol, bot in self.bots.items():
            if bot._rotating_out and bot.position is None:
                to_remove.append(symbol)

        for symbol in to_remove:
            logger.info(f"[{symbol}] Removing rotated-out bot")
            task = self._fsm_tasks.pop(symbol, None)
            if task:
                task.cancel()
            self.bots.pop(symbol, None)
            self.ws_mux.unregister_queue(symbol)

    def _fetch_funding_rate(self, symbol: str) -> float | None:
        """Fetch current funding rate for a symbol from REST API."""
        try:
            resp = requests.post(self._api_url, json={"type": "metaAndAssetCtxs"}, timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json()
            meta_universe = data[0]["universe"]
            asset_ctxs = data[1]
            for u, c in zip(meta_universe, asset_ctxs):
                if u["name"] == symbol:
                    return float(c.get("funding", 0))
            return None
        except Exception:
            return None

    def check_margin_available(self) -> bool:
        """Check if we have margin available for new trades.

        Returns True if total margin used is below 75% of equity.
        Uses live equity and per-asset leverage for margin calculation.
        """
        total_margin = 0.0
        for bot in self.bots.values():
            if bot.position is not None:
                total_margin += bot.position.total_size * bot.last_price / bot.max_leverage

        current_equity = self.initial_equity + sum(bot.total_pnl for bot in self.bots.values())
        usage = total_margin / current_equity if current_equity > 0 else 1.0
        return usage < self._max_margin_usage

    async def _halt_flag_loop(self):
        """Monitor halt flag file for CLI kill command.

        Krabje or owner can create state/.halt to force-halt all bots.
        The bot checks every 5 seconds.
        """
        while True:
            try:
                await asyncio.sleep(5.0)

                if self._halt_flag.exists():
                    reason = self._halt_flag.read_text().strip() or "cli_kill"
                    logger.warning(f"HALT FLAG DETECTED: {reason}")

                    for bot in self.bots.values():
                        await bot.queue.put({"type": "halt", "reason": reason})

                    self._halted = True
                    self._halt_flag.unlink()  # Remove flag after processing

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Halt flag loop error: {e}")

    async def _status_loop(self):
        """Print status every 5 minutes and save to DB."""
        while True:
            try:
                await asyncio.sleep(300.0)
                self._print_status()
                self._save_status_to_db()
            except asyncio.CancelledError:
                break

    def _print_status(self):
        """Print a compact status summary."""
        total_pnl = sum(bot.total_pnl for bot in self.bots.values())
        total_equity = self.initial_equity + total_pnl
        ws_stats = self.ws_mux.stats()

        lines = [
            f"\n{'='*60}",
            f"  PORTFOLIO STATUS | {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            f"  Equity: ${total_equity:,.2f} ({total_pnl:+,.2f}) | "
            f"WS: {'OK' if ws_stats['connected'] else 'DISCONNECTED'} | "
            f"Msgs: {ws_stats['messages']}",
            f"{'='*60}",
        ]

        for symbol, bot in self.bots.items():
            s = bot.status()
            pos = ""
            if s["position"]:
                p = s["position"]
                pos = f" | {p['direction']} {p['entries']}x @ {p['avg_entry']:.2f}"
            lines.append(
                f"  {symbol:6s} {s['state']:15s} "
                f"${s['equity']:>10,.2f} "
                f"PnL: ${s['total_pnl']:>8,.2f} "
                f"({s['trades']} trades){pos}"
            )

        logger.info("\n".join(lines))

    def _save_status_to_db(self):
        """Save current status to SQLite for Krabje/CLI to read."""
        try:
            from state.database import get_connection
            conn = get_connection()

            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    total_equity REAL NOT NULL,
                    total_pnl REAL NOT NULL,
                    halted INTEGER NOT NULL,
                    ws_connected INTEGER NOT NULL,
                    status_json TEXT NOT NULL
                )
            """)

            status_data = self.status()
            conn.execute("""
                INSERT INTO bot_status (timestamp, total_equity, total_pnl, halted, ws_connected, status_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                status_data["equity"],
                status_data["total_pnl"],
                int(status_data["halted"]),
                int(status_data["ws"]["connected"]),
                json.dumps(status_data, default=str),
            ))
            conn.commit()

            # Keep only last 1000 status entries
            conn.execute("DELETE FROM bot_status WHERE id NOT IN (SELECT id FROM bot_status ORDER BY id DESC LIMIT 1000)")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to save status to DB: {e}")

    def status(self) -> dict:
        """Return full portfolio status as dict."""
        total_pnl = sum(bot.total_pnl for bot in self.bots.values())
        return {
            "equity": self.initial_equity + total_pnl,
            "initial_equity": self.initial_equity,
            "total_pnl": total_pnl,
            "halted": self._halted,
            "ws": self.ws_mux.stats(),
            "bots": {symbol: bot.status() for symbol, bot in self.bots.items()},
        }

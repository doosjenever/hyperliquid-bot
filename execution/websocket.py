"""Central WebSocket multiplexer for Hyperliquid.

One connection, multiple subscriptions. Routes messages to per-asset FSM queues.

Architecture (designed with Krabje):
  ┌─────────────────────┐
  │  Hyperliquid WS     │
  │  (L2 + trades)      │
  └──────────┬──────────┘
             │
  ┌──────────▼���─────────┐
  │  WebSocketMux       │
  │  (single connection)│
  └──────────┬──────────┘
             │  routes by coin
    ┌────────┼────────┐
    ▼        ▼        ▼
  BTC.q    ETH.q    SOL.q   (asyncio.Queue per FSM)
"""

import asyncio
import json
import logging
import time
from typing import Callable

import websockets

import config

logger = logging.getLogger(__name__)

# WebSocket endpoints
WS_MAINNET = "wss://api.hyperliquid.xyz/ws"
WS_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"


class WebSocketMux:
    """Central WebSocket multiplexer.

    Subscribes to L2 orderbook and trades for all configured assets.
    Routes incoming messages to the correct FSM's queue.
    Handles reconnection automatically.
    """

    def __init__(self, assets: list[str], testnet: bool = True):
        self.assets = assets
        self.url = WS_TESTNET if testnet else WS_MAINNET
        self.testnet = testnet

        # Per-asset queues (set by PortfolioManager)
        self.queues: dict[str, asyncio.Queue] = {}

        # Callbacks for custom event handling
        self._callbacks: list[Callable] = []

        # Connection state
        self._ws = None
        self._connected = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._running = False

        # Stats
        self.messages_received = 0
        self.last_message_time = 0.0

    def register_queue(self, symbol: str, queue: asyncio.Queue):
        """Register a FSM's queue for receiving events."""
        self.queues[symbol] = queue

    def unregister_queue(self, symbol: str):
        """Remove a FSM's queue (asset rotated out)."""
        self.queues.pop(symbol, None)
        if symbol in self.assets:
            self.assets.remove(symbol)

    async def subscribe_asset(self, symbol: str):
        """Subscribe to a new asset's channels on the live connection."""
        if symbol not in self.assets:
            self.assets.append(symbol)
        if self._ws and self._connected:
            await self._subscribe(self._ws, "l2Book", symbol)
            await self._subscribe(self._ws, "trades", symbol)
            logger.info(f"Subscribed new asset: {symbol}")

    def on_message(self, callback: Callable):
        """Register a callback for all messages (monitoring)."""
        self._callbacks.append(callback)

    async def run(self):
        """Main loop — connect, subscribe, receive, route. Auto-reconnects."""
        self._running = True

        while self._running:
            try:
                await self._connect_and_listen()
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning(f"WebSocket disconnected: {e}. Reconnecting in {self._reconnect_delay}s...")
                self._connected = False

                # Notify all FSMs of disconnect
                await self._broadcast_event({"type": "sync_required"})

                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            except asyncio.CancelledError:
                logger.info("WebSocket multiplexer cancelled")
                break
            except Exception as e:
                logger.error(f"WebSocket unexpected error: {e}", exc_info=True)
                if not self._running:
                    break
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_listen(self):
        """Connect to Hyperliquid WS and start listening."""
        logger.info(f"Connecting to {self.url} ({'testnet' if self.testnet else 'mainnet'})...")

        async with websockets.connect(
            self.url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = 1.0  # Reset on successful connect

            logger.info(f"Connected! Subscribing to {len(self.assets)} assets...")

            # Subscribe to L2 and trades for each asset
            for coin in self.assets:
                await self._subscribe(ws, "l2Book", coin)
                await self._subscribe(ws, "trades", coin)

            logger.info(f"Subscribed to {len(self.assets) * 2} channels")

            # Listen loop
            async for raw_msg in ws:
                self.messages_received += 1
                self.last_message_time = time.time()

                try:
                    msg = json.loads(raw_msg)
                    await self._route_message(msg)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON: {raw_msg[:100]}")
                except Exception as e:
                    logger.error(f"Error routing message: {e}")

    async def _subscribe(self, ws, channel_type: str, coin: str):
        """Send a subscription message."""
        msg = {
            "method": "subscribe",
            "subscription": {
                "type": channel_type,
                "coin": coin,
            }
        }
        await ws.send(json.dumps(msg))
        logger.debug(f"Subscribed: {channel_type}/{coin}")

    async def _route_message(self, msg: dict):
        """Route an incoming message to the correct FSM queue."""
        channel = msg.get("channel", "")
        data = msg.get("data", {})

        if channel == "l2Book":
            coin = data.get("coin", "")
            event = {
                "type": "l2_update",
                "data": {
                    "coin": coin,
                    "bids": data.get("levels", [[]])[0] if data.get("levels") else [],
                    "asks": data.get("levels", [[], []])[1] if len(data.get("levels", [])) > 1 else [],
                    "time": data.get("time", time.time()),
                },
            }
            await self._send_to_queue(coin, event)

        elif channel == "trades":
            # Trades come as a list
            trades = data if isinstance(data, list) else [data]
            for trade in trades:
                coin = trade.get("coin", "")
                event = {
                    "type": "trade",
                    "data": trade,
                }
                await self._send_to_queue(coin, event)

        # Fire callbacks
        for cb in self._callbacks:
            try:
                cb(msg)
            except Exception:
                pass

    async def _send_to_queue(self, coin: str, event: dict):
        """Send an event to the correct FSM's queue."""
        queue = self.queues.get(coin)
        if queue is not None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest message if queue is full
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)

    async def _broadcast_event(self, event: dict):
        """Send an event to ALL FSM queues."""
        for queue in self.queues.values():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def stop(self):
        """Gracefully stop the multiplexer."""
        self._running = False
        if self._ws:
            await self._ws.close()
        logger.info("WebSocket multiplexer stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def stats(self) -> dict:
        return {
            "connected": self._connected,
            "messages": self.messages_received,
            "last_msg_age_s": time.time() - self.last_message_time if self.last_message_time else -1,
            "url": self.url,
            "assets": self.assets,
        }

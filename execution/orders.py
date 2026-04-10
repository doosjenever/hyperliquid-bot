"""Hyperliquid SDK order execution module.

Wraps the hyperliquid-python-sdk Exchange class for order execution,
and uses direct REST calls for info queries (SDK Info class has init bugs).

All order actions are logged for audit trail.
"""

import logging
import time
from typing import Optional

import requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config

logger = logging.getLogger(__name__)


def _patch_info_init():
    """Patch Info.__init__ to handle testnet spot metadata issues.

    The SDK crashes on testnet when spot token indices are out of range.
    This wraps the problematic section in a try/except.
    """
    original_init = Info.__init__

    def patched_init(self, base_url, skip_ws=False, meta=None, spot_meta=None, perp_dexs=None, timeout=None):
        try:
            if timeout is not None:
                original_init(self, base_url, skip_ws, meta, spot_meta, perp_dexs, timeout)
            else:
                original_init(self, base_url, skip_ws, meta, spot_meta, perp_dexs)
        except (IndexError, KeyError) as e:
            # Testnet spot metadata can be incomplete — init with perps only
            logger.warning(f"SDK Info init failed (testnet spot data issue): {e}")
            self.base_url = base_url
            self.timeout = timeout

            if meta is None:
                meta = self.meta()
            if spot_meta is None:
                try:
                    spot_meta = self.spot_meta()
                except Exception:
                    spot_meta = {"universe": [], "tokens": []}

            self.coin_to_asset = {}
            self.name_to_coin = {}
            self.asset_to_sz_decimals = {}

            # Load perp assets (skip broken spot assets)
            for asset_info in meta["universe"]:
                asset = meta["universe"].index(asset_info)
                self.coin_to_asset[asset_info["name"]] = asset
                self.name_to_coin[asset_info["name"]] = asset_info["name"]
                self.asset_to_sz_decimals[asset] = asset_info["szDecimals"]

    Info.__init__ = patched_init


# Apply patch before creating any Exchange/Info instances
_patch_info_init()


class OrderExecutor:
    """Handles all order execution against the Hyperliquid API."""

    def __init__(self, testnet: bool = True):
        self.testnet = testnet
        self.wallet_address = config.HL_WALLET_ADDRESS
        self.base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.info_url = self.base_url + "/info"

        # Exchange client (for signing/sending orders)
        account = Account.from_key(config.HL_PRIVATE_KEY)
        self.exchange = Exchange(wallet=account, base_url=self.base_url)

        logger.info(f"OrderExecutor initialized | {'testnet' if testnet else 'MAINNET'} | wallet={self.wallet_address[:10]}...")

    def _info_request(self, payload: dict) -> dict | list:
        """Make a REST request to the info endpoint."""
        resp = requests.post(self.info_url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def set_leverage(self, coin: str, leverage: int, cross: bool = True) -> dict:
        """Set leverage for an asset."""
        try:
            result = self.exchange.update_leverage(leverage, coin, is_cross=cross)
            logger.info(f"[{coin}] Leverage set to {leverage}x ({'cross' if cross else 'isolated'})")
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"[{coin}] Failed to set leverage: {e}")
            return {"success": False, "error": str(e)}

    def market_open(self, coin: str, is_buy: bool, size: float, slippage: float = 0.05) -> dict:
        """Open a market position."""
        direction = "LONG" if is_buy else "SHORT"
        try:
            result = self.exchange.market_open(coin, is_buy, size, slippage=slippage)
            status = result.get("status", "unknown")
            logger.info(f"[{coin}] Market {direction} {size} | status={status}")

            if status == "ok":
                fills = result.get("response", {}).get("data", {}).get("statuses", [])
                return {"success": True, "result": result, "fills": fills}
            else:
                logger.warning(f"[{coin}] Order rejected: {result}")
                return {"success": False, "result": result}

        except Exception as e:
            logger.error(f"[{coin}] Market open failed: {e}")
            return {"success": False, "error": str(e)}

    def market_close(self, coin: str, size: float = None, slippage: float = 0.05) -> dict:
        """Close a position (fully or partially)."""
        try:
            result = self.exchange.market_close(coin, sz=size, slippage=slippage)
            status = result.get("status", "unknown")
            logger.info(f"[{coin}] Market close {'full' if size is None else size} | status={status}")
            return {"success": status == "ok", "result": result}
        except Exception as e:
            logger.error(f"[{coin}] Market close failed: {e}")
            return {"success": False, "error": str(e)}

    def get_position(self, coin: str) -> dict | None:
        """Get current position for an asset via REST."""
        try:
            state = self._info_request({"type": "clearinghouseState", "user": self.wallet_address})
            for asset_pos in state.get("assetPositions", []):
                pos = asset_pos.get("position", {})
                if pos.get("coin") == coin:
                    szi = float(pos.get("szi", 0))
                    if szi != 0:
                        return {
                            "coin": coin,
                            "size": szi,
                            "direction": "long" if szi > 0 else "short",
                            "entry_price": float(pos.get("entryPx", 0)),
                            "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                            "margin_used": float(pos.get("marginUsed", 0)),
                            "liquidation_price": float(pos.get("liquidationPx", 0)) if pos.get("liquidationPx") else None,
                        }
            return None
        except Exception as e:
            logger.error(f"[{coin}] Failed to get position: {e}")
            return None

    def get_all_positions(self) -> list[dict]:
        """Get all open positions."""
        try:
            state = self._info_request({"type": "clearinghouseState", "user": self.wallet_address})
            positions = []
            for asset_pos in state.get("assetPositions", []):
                pos = asset_pos.get("position", {})
                szi = float(pos.get("szi", 0))
                if szi != 0:
                    positions.append({
                        "coin": pos.get("coin"),
                        "size": szi,
                        "direction": "long" if szi > 0 else "short",
                        "entry_price": float(pos.get("entryPx", 0)),
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    })
            return positions
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        try:
            return self._info_request({"type": "openOrders", "user": self.wallet_address})
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    def get_account_value(self) -> float:
        """Get total account value (equity).

        On unified accounts, perp accountValue only shows margin-related value.
        We combine perp accountValue + spot USDC for total equity.
        """
        try:
            total = 0.0

            # Perp value (margin + unrealized PnL)
            state = self._info_request({"type": "clearinghouseState", "user": self.wallet_address})
            margin = state.get("marginSummary", {})
            perp_value = float(margin.get("accountValue", 0))
            total += perp_value

            # Spot USDC (free collateral not locked in perp margin)
            spot_state = self._info_request({"type": "spotClearinghouseState", "user": self.wallet_address})
            for bal in spot_state.get("balances", []):
                if bal.get("coin") == "USDC":
                    total += float(bal.get("total", 0))

            return total
        except Exception as e:
            logger.error(f"Failed to get account value: {e}")
            return 0.0

    def reconcile(self, coin: str) -> dict:
        """Reconcile local state with exchange state (for SYNCING)."""
        position = self.get_position(coin)
        orders = self.get_open_orders()
        coin_orders = [o for o in orders if o.get("coin") == coin]
        account_value = self.get_account_value()

        result = {
            "coin": coin,
            "has_position": position is not None,
            "position": position,
            "open_orders": coin_orders,
            "account_value": account_value,
        }

        logger.info(
            f"[{coin}] Reconcile: pos={'YES' if position else 'NO'} | "
            f"orders={len(coin_orders)} | equity=${account_value:,.2f}"
        )
        return result

    def cancel_all_orders(self, coin: str = None) -> dict:
        """Cancel all open orders for a coin (or all coins)."""
        try:
            orders = self.get_open_orders()
            if coin:
                orders = [o for o in orders if o.get("coin") == coin]

            if not orders:
                return {"success": True, "cancelled": 0}

            cancels = [{"coin": o["coin"], "oid": o["oid"]} for o in orders]
            result = self.exchange.bulk_cancel(cancels)
            logger.info(f"Cancelled {len(cancels)} orders" + (f" for {coin}" if coin else ""))
            return {"success": True, "cancelled": len(cancels), "result": result}
        except Exception as e:
            logger.error(f"Cancel orders failed: {e}")
            return {"success": False, "error": str(e)}

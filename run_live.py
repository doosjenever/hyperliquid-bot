"""Live trading bot entry point.

Starts the PortfolioManager with all configured assets.
Connects to Hyperliquid WebSocket (testnet by default).

Usage:
    python run_live.py                    # Testnet, all assets
    python run_live.py --mainnet          # Mainnet (careful!)
    python run_live.py --assets BTC ETH   # Specific assets only
    python run_live.py --equity 5000      # Custom starting equity
"""

import argparse
import asyncio
import logging
import sys

import config
from execution.manager import PortfolioManager


def setup_logging(level: str = "INFO"):
    """Configure logging for the live bot."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ],
    )
    # Quiet down noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid S/R Trading Bot")
    parser.add_argument("--mainnet", action="store_true", help="Use mainnet (default: testnet)")
    parser.add_argument("--assets", nargs="+", default=None, help="Assets to trade (default: dynamic top N)")
    parser.add_argument("--top", type=int, default=25, help="Number of top assets by OI (default: 25)")
    parser.add_argument("--equity", type=float, default=10_000, help="Starting equity (default: 10000)")
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    args = parser.parse_args()

    setup_logging(args.log_level)

    assets = args.assets  # None = dynamic top N
    testnet = not args.mainnet

    logger = logging.getLogger(__name__)
    mode = f"top {args.top} by OI" if not assets else f"assets={assets}"
    logger.info(f"Starting bot | {'TESTNET' if testnet else 'MAINNET'} | {mode} | equity=${args.equity}")

    if not testnet:
        logger.warning("MAINNET MODE — real money at risk!")

    pm = PortfolioManager(
        assets=assets,
        equity=args.equity,
        testnet=testnet,
        top_n=args.top,
    )

    try:
        asyncio.run(pm.start())
    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C)")


if __name__ == "__main__":
    main()

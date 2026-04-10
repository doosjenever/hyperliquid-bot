#!/usr/bin/env python3
"""Run a backtest of the S/R confluence strategy.

Usage:
    python run_backtest.py                    # Default: BTC, 90 days, 3x leverage
    python run_backtest.py ETH 60 5          # ETH, 60 days, 5x leverage
"""

import sys
from backtest.engine import Backtester


def main():
    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    leverage = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0

    bt = Backtester(coin=coin, leverage=leverage, equity=10_000)
    results = bt.run(days=days)
    bt.print_summary(results)

    # Print individual trades
    if results["trades"]:
        print(f"\n{'='*80}")
        print("TRADE LOG")
        print(f"{'='*80}")
        for i, t in enumerate(results["trades"], 1):
            dir_emoji = "L" if t["direction"] == "long" else "S"
            pnl_emoji = "+" if t["net_pnl"] > 0 else ""
            print(
                f"#{i:3d} [{dir_emoji}] "
                f"{str(t['entry_time'])[:16]} -> {str(t['exit_time'])[:16]} | "
                f"Entry: {t['entry_price']:>10.2f} Exit: {t['exit_price']:>10.2f} | "
                f"PnL: {pnl_emoji}{t['net_pnl']:>8.2f} ({pnl_emoji}{t['return_pct']:.1f}%) | "
                f"{t.get('exit_reason', '?')}"
            )


if __name__ == "__main__":
    main()

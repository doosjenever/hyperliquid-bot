#!/usr/bin/env python3
"""Run backtests across all configured assets — Sweep & Reclaim Strategy v1.0.

Uses per-asset exchange max leverage.

Usage:
    python run_backtest_all.py           # Default: all assets, 90 days
    python run_backtest_all.py 60        # 60 days
"""

import sys
import numpy as np
import config
from backtest.engine import Backtester

# Per-asset max leverage (exchange limits)
ASSET_LEVERAGE = {
    "BTC": 40,
    "ETH": 25,
    "SOL": 20,
    "DOGE": 10,
    "AVAX": 10,
}


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90

    assets = config.BACKTEST_ASSETS
    all_results = {}

    for coin in assets:
        leverage = ASSET_LEVERAGE.get(coin, 10)
        print(f"\n{'#'*60}")
        print(f"# {coin} ({leverage}x leverage)")
        print(f"{'#'*60}")
        bt = Backtester(coin=coin, leverage=leverage, equity=10_000)
        results = bt.run(days=days)
        bt.print_summary(results)
        all_results[coin] = results

    # Combined summary
    print(f"\n{'='*60}")
    print("TOTAAL OVERZICHT — Sweep & Reclaim v1.0")
    print(f"{'='*60}")
    total_trades = 0
    total_pnl = 0
    total_wins = 0
    for coin, r in all_results.items():
        trades = r["trades"]
        pnl = sum(t["net_pnl"] for t in trades)
        wins = sum(1 for t in trades if t["net_pnl"] > 0)
        losses = [t for t in trades if t["net_pnl"] <= 0]
        gross_wins = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
        gross_losses = abs(sum(t["net_pnl"] for t in losses)) if losses else 1
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        total_trades += len(trades)
        total_pnl += pnl
        total_wins += wins
        leverage = ASSET_LEVERAGE.get(coin, 10)
        print(f"  {coin:6s} {leverage:2d}x: {len(trades):3d} trades | PnL: ${pnl:>+9.2f} ({pnl/100:+.1f}%) | W: {wins}/{len(trades)} | PF: {pf:.2f}")

    winrate = total_wins / total_trades * 100 if total_trades > 0 else 0
    print(f"{'='*60}")
    print(f"  TOTAAL:    {total_trades:3d} trades | PnL: ${total_pnl:>+9.2f} ({total_pnl/100:+.1f}%) | Winrate: {winrate:.0f}%")


if __name__ == "__main__":
    main()

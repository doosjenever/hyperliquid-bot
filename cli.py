"""CLI tool for bot management.

Used by Krabje (OpenClaw) and the owner to monitor and control the bot.
Reads directly from SQLite — no HTTP/API needed.

Usage:
    python cli.py status          # Show current bot status
    python cli.py positions       # Show open positions
    python cli.py trades          # Show recent trades
    python cli.py kill [reason]   # Force halt all bots
    python cli.py resume          # Remove halt flag
    python cli.py profiles        # Show active asset profiles
    python cli.py history [asset] # Show calibration history
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import config


def get_db():
    """Get database connection."""
    from state.database import get_connection
    return get_connection()


def cmd_status(args):
    """Show current bot status from DB."""
    conn = get_db()

    try:
        row = conn.execute(
            "SELECT * FROM bot_status ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        print("Bot status not yet available (bot may not be running)")
        return

    if row is None:
        print("No status data found. Is the bot running?")
        return

    status = json.loads(row["status_json"])
    age = ""
    try:
        ts = datetime.fromisoformat(row["timestamp"])
        age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
        age = f" ({int(age_sec)}s ago)"
    except Exception:
        pass

    print(f"{'='*55}")
    print(f"  BOT STATUS{age}")
    print(f"{'='*55}")
    print(f"  Equity:  ${status['equity']:,.2f}")
    print(f"  PnL:     ${status['equity'] - status['initial_equity']:+,.2f}")
    print(f"  Halted:  {'YES' if status['halted'] else 'No'}")
    print(f"  WS:      {'Connected' if status['ws']['connected'] else 'DISCONNECTED'}")
    print(f"  Messages: {status['ws']['messages']}")
    print(f"{'='*55}")

    for symbol, bot in status.get("bots", {}).items():
        pos = ""
        if bot.get("position"):
            p = bot["position"]
            pos = f" | {p['direction']} {p['entries']}x @ {p['avg_entry']:.2f}"
        print(
            f"  {symbol:6s} {bot['state']:15s} "
            f"${bot['equity']:>10,.2f} "
            f"PnL: ${bot['total_pnl']:>8,.2f} "
            f"({bot['trades']} trades){pos}"
        )

    conn.close()


def cmd_positions(args):
    """Show open positions."""
    conn = get_db()

    try:
        row = conn.execute(
            "SELECT status_json FROM bot_status ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        print("No status data available")
        return

    if row is None:
        print("No data")
        return

    status = json.loads(row["status_json"])
    has_positions = False

    for symbol, bot in status.get("bots", {}).items():
        if bot.get("position"):
            has_positions = True
            p = bot["position"]
            print(f"{symbol}: {p['direction']} | {p['entries']}x entries | "
                  f"avg={p['avg_entry']:.2f} | SL={p['stop_loss']:.2f} | TP={p['take_profit']:.2f}")

    if not has_positions:
        print("No open positions")

    conn.close()


def cmd_trades(args):
    """Show recent trades."""
    conn = get_db()
    limit = args.limit or 20

    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    except Exception:
        print("No trades table yet")
        return

    if not rows:
        print("No trades recorded")
        return

    print(f"{'Symbol':<6} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Reason':<15} {'DCA':<4}")
    print("-" * 70)
    for r in rows:
        print(
            f"{r['symbol']:<6} {r['direction']:<6} "
            f"${r['entry_price']:>9,.2f} ${r['exit_price']:>9,.2f} "
            f"${r['net_pnl']:>9,.2f} {r['exit_reason']:<15} {r['dca_entries']:<4}"
        )

    total = sum(r["net_pnl"] for r in rows)
    print(f"\nTotal PnL (last {len(rows)} trades): ${total:,.2f}")
    conn.close()


def cmd_kill(args):
    """Force halt all bots via halt flag file."""
    reason = args.reason or "cli_kill"
    halt_file = config.BASE_DIR / "state" / ".halt"
    halt_file.parent.mkdir(parents=True, exist_ok=True)
    halt_file.write_text(reason)
    print(f"HALT flag written: {reason}")
    print(f"Bot will halt within 5 seconds")


def cmd_resume(args):
    """Remove halt flag to allow bots to resume."""
    halt_file = config.BASE_DIR / "state" / ".halt"
    if halt_file.exists():
        halt_file.unlink()
        print("Halt flag removed. Bots will resume on next check.")
    else:
        print("No halt flag active.")


def cmd_profiles(args):
    """Show active asset profiles from DB (all assets, not just backtest set)."""
    conn = get_db()

    try:
        rows = conn.execute(
            "SELECT * FROM asset_profiles WHERE active = 1 ORDER BY symbol"
        ).fetchall()
    except Exception:
        print("No profiles table yet")
        conn.close()
        return

    if not rows:
        print("No active profiles found")
        conn.close()
        return

    for row in rows:
        print(
            f"{row['symbol']:6s}: vol={row['vol_ratio']:.2f}x | "
            f"RSI=[{row['rsi_oversold']:.0f}/{row['rsi_overbought']:.0f}] "
            f"MFI=[{row['mfi_oversold']:.0f}/{row['mfi_overbought']:.0f}] "
            f"CCI=[{row['cci_oversold']:.0f}/{row['cci_overbought']:.0f}] | "
            f"swing_w={row['swing_window']} | "
            f"calibrated={row['calibrated_at'][:10]}"
        )

    conn.close()


def cmd_history(args):
    """Show calibration history for an asset."""
    conn = get_db()
    symbol = args.asset or "BTC"

    rows = conn.execute(
        "SELECT calibrated_at, vol_ratio, rsi_oversold, rsi_overbought, swing_window, price "
        "FROM asset_profiles WHERE symbol = ? ORDER BY id DESC LIMIT 12",
        (symbol,)
    ).fetchall()

    if not rows:
        print(f"No calibration history for {symbol}")
        return

    print(f"Calibration history for {symbol} (last {len(rows)} entries):")
    print(f"{'Date':<12} {'Vol':>6} {'RSI_lo':>7} {'RSI_hi':>7} {'Swing':>6} {'Price':>10}")
    print("-" * 55)
    for r in rows:
        print(
            f"{r['calibrated_at'][:10]:<12} "
            f"{r['vol_ratio']:>6.2f} "
            f"{r['rsi_oversold']:>7.1f} "
            f"{r['rsi_overbought']:>7.1f} "
            f"{r['swing_window']:>6} "
            f"${r['price']:>9,.2f}"
        )

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Bot CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current bot status")
    sub.add_parser("positions", help="Show open positions")

    trades_p = sub.add_parser("trades", help="Show recent trades")
    trades_p.add_argument("--limit", type=int, default=20, help="Number of trades to show")

    kill_p = sub.add_parser("kill", help="Force halt all bots")
    kill_p.add_argument("reason", nargs="?", default="cli_kill", help="Halt reason")

    sub.add_parser("resume", help="Remove halt flag")
    sub.add_parser("profiles", help="Show active asset profiles")

    hist_p = sub.add_parser("history", help="Show calibration history")
    hist_p.add_argument("asset", nargs="?", default="BTC", help="Asset symbol")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    commands = {
        "status": cmd_status,
        "positions": cmd_positions,
        "trades": cmd_trades,
        "kill": cmd_kill,
        "resume": cmd_resume,
        "profiles": cmd_profiles,
        "history": cmd_history,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()

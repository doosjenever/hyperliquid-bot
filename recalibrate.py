"""Weekly asset recalibration script.

Designed to run as a cron job (e.g., every Sunday 02:00 UTC).
For each asset:
  1. Fetches fresh OHLCV data (last 90 days)
  2. Builds a new AssetProfile (percentile-based calibration)
  3. Applies EMA smoothing against previous profile (prevents whiplash)
  4. Saves to SQLite (append-only history)
  5. The live bot reads the active profile from the DB

Key design decisions (agreed with Krabje):
  - No grid search / PnL optimization → overfitting doodszonde
  - EMA smoothing (alpha=0.3): 30% new + 70% old for stability
  - Asymmetric vol smoothing: fast up (alpha=0.8), slow down (alpha=0.2)
  - Confluence threshold stays at 80 until 30+ trades per asset
  - All parameters derived from data, nothing hardcoded

Usage:
    python recalibrate.py              # Recalibrate all assets
    python recalibrate.py --asset BTC  # Recalibrate one asset
    python recalibrate.py --dry-run    # Show what would change, don't save
"""

import argparse
import sys
from datetime import datetime, timezone

from data.fetcher import fetch_and_cache
from strategy.asset_profile import AssetProfile
from state.database import (
    get_connection, save_profile, load_active_profile, count_trades
)
import config


# Smoothing constants
DEFAULT_ALPHA = 0.3          # Standard EMA smoothing for most parameters
VOL_UP_ALPHA = 0.8           # Fast adaptation when volatility increases
VOL_DOWN_ALPHA = 0.2         # Slow adaptation when volatility decreases
MIN_TRADES_FOR_THRESHOLD = 30  # Minimum trades before tuning confluence score


def smooth(new_val: float, old_val: float | None, alpha: float = DEFAULT_ALPHA) -> float:
    """Apply EMA smoothing: result = alpha * new + (1-alpha) * old."""
    if old_val is None:
        return new_val
    return alpha * new_val + (1 - alpha) * old_val


def smooth_asymmetric_vol(new_val: float, old_val: float | None) -> float:
    """Asymmetric smoothing for volatility parameters.

    Fast up (alpha=0.8): quickly widen stops when vol spikes
    Slow down (alpha=0.2): slowly tighten when vol drops
    Protects against sudden chaos, prevents premature tightening.
    """
    if old_val is None:
        return new_val
    alpha = VOL_UP_ALPHA if new_val > old_val else VOL_DOWN_ALPHA
    return alpha * new_val + (1 - alpha) * old_val


def calibrate_asset(symbol: str, days: int = 90, dry_run: bool = False) -> dict:
    """Calibrate a single asset and save to database.

    Returns the smoothed profile data dict.
    """
    print(f"\n{'='*50}")
    print(f"  Calibrating {symbol}")
    print(f"{'='*50}")

    # Fetch fresh data
    ltf = "1h"
    df = fetch_and_cache(symbol, ltf, days=days)
    if df.empty:
        print(f"  SKIP: No data for {symbol}")
        return {}

    df_btc = fetch_and_cache("BTC", ltf, days=days) if symbol != "BTC" else None

    # Build fresh profile from data
    profile = AssetProfile(symbol, df, df_btc)
    print(f"  Raw: {profile.summary()}")

    # Extract raw values
    raw = {
        "vol_ratio": profile.vol_ratio,
        "avg_atr_pct": profile.avg_atr_pct,
        "current_atr_pct": profile.current_atr_pct,
        "current_atr": profile.current_atr,
        "inv_multiplier": profile.inv_multiplier,
        "stop_multiplier": profile.stop_multiplier,
        "dca_spacing": profile.dca_spacing,
        "rsi_oversold": profile.rsi_oversold,
        "rsi_overbought": profile.rsi_overbought,
        "rsi_dca_long": profile.rsi_dca_long,
        "rsi_dca_short": profile.rsi_dca_short,
        "mfi_oversold": profile.mfi_oversold,
        "mfi_overbought": profile.mfi_overbought,
        "cci_oversold": profile.cci_oversold,
        "cci_overbought": profile.cci_overbought,
        "swing_window": profile.swing_window,
        "ema_200": profile.ema_200,
        "price": profile.price,
        "trend_bullish": profile.trend_bullish,
        "avg_volume": profile.avg_volume,
        "estimated_slippage_bps": profile.estimated_slippage_bps,
    }

    # Load previous active profile for smoothing
    conn = get_connection()
    prev = load_active_profile(conn, symbol)

    if prev:
        print(f"  Previous profile found (from {prev['calibrated_at'][:10]})")

        # Apply EMA smoothing
        smoothed = {
            # Asymmetric smoothing for volatility-related params
            "vol_ratio": smooth_asymmetric_vol(raw["vol_ratio"], prev["vol_ratio"]),
            "avg_atr_pct": smooth_asymmetric_vol(raw["avg_atr_pct"], prev["avg_atr_pct"]),
            "current_atr_pct": raw["current_atr_pct"],  # Current = no smoothing (real-time)
            "current_atr": raw["current_atr"],            # Current = no smoothing
            "inv_multiplier": smooth_asymmetric_vol(raw["inv_multiplier"], prev["inv_multiplier"]),
            "stop_multiplier": smooth_asymmetric_vol(raw["stop_multiplier"], prev["stop_multiplier"]),
            "dca_spacing": smooth(raw["dca_spacing"], prev["dca_spacing"]),

            # Standard EMA smoothing for indicator thresholds
            "rsi_oversold": round(smooth(raw["rsi_oversold"], prev["rsi_oversold"]), 1),
            "rsi_overbought": round(smooth(raw["rsi_overbought"], prev["rsi_overbought"]), 1),
            "rsi_dca_long": round(smooth(raw["rsi_dca_long"], prev["rsi_dca_long"]), 1),
            "rsi_dca_short": round(smooth(raw["rsi_dca_short"], prev["rsi_dca_short"]), 1),
            "mfi_oversold": round(smooth(raw["mfi_oversold"], prev["mfi_oversold"]), 1),
            "mfi_overbought": round(smooth(raw["mfi_overbought"], prev["mfi_overbought"]), 1),
            "cci_oversold": round(smooth(raw["cci_oversold"], prev["cci_oversold"]), 1),
            "cci_overbought": round(smooth(raw["cci_overbought"], prev["cci_overbought"]), 1),

            # Integer params: round after smoothing
            "swing_window": round(smooth(raw["swing_window"], prev["swing_window"])),

            # Context (not smoothed)
            "ema_200": raw["ema_200"],
            "price": raw["price"],
            "trend_bullish": raw["trend_bullish"],
            "avg_volume": raw["avg_volume"],
            "estimated_slippage_bps": smooth(raw["estimated_slippage_bps"], prev["estimated_slippage_bps"]),
        }

        # Show what changed
        _print_changes(symbol, prev, smoothed, raw)
    else:
        print(f"  First calibration — no smoothing applied")
        smoothed = raw.copy()

    # Confluence threshold: stays at 80 until enough trades
    trade_count = count_trades(conn, symbol)
    smoothed["min_confluence_score"] = 80
    if trade_count >= MIN_TRADES_FOR_THRESHOLD:
        print(f"  {trade_count} trades logged — confluence threshold tuning unlocked (not yet implemented)")

    # Store raw values for debugging
    smoothed["raw_values"] = raw
    smoothed["smoothing_alpha"] = DEFAULT_ALPHA

    if not dry_run:
        save_profile(conn, symbol, smoothed)
        print(f"  Saved to database (active=1)")
    else:
        print(f"  DRY RUN — not saved")

    conn.close()
    return smoothed


def _print_changes(symbol: str, prev: dict, smoothed: dict, raw: dict):
    """Print a compact diff of what changed."""
    params = [
        ("vol_ratio", ".2f"),
        ("inv_multiplier", ".2f"),
        ("stop_multiplier", ".2f"),
        ("rsi_oversold", ".1f"),
        ("rsi_overbought", ".1f"),
        ("mfi_oversold", ".1f"),
        ("mfi_overbought", ".1f"),
        ("cci_oversold", ".0f"),
        ("cci_overbought", ".0f"),
        ("swing_window", "d"),
    ]

    changes = []
    for param, fmt in params:
        old_v = prev.get(param)
        new_v = smoothed.get(param)
        raw_v = raw.get(param)
        if old_v is not None and new_v is not None and abs(float(new_v) - float(old_v)) > 0.01:
            changes.append(
                f"    {param}: {format(old_v, fmt)} -> {format(new_v, fmt)} "
                f"(raw: {format(raw_v, fmt)})"
            )

    if changes:
        print(f"  Changes (smoothed):")
        for c in changes:
            print(c)
    else:
        print(f"  No significant changes")


def main():
    parser = argparse.ArgumentParser(description="Weekly asset recalibration")
    parser.add_argument("--asset", type=str, help="Calibrate a single asset (e.g., BTC)")
    parser.add_argument("--days", type=int, default=90, help="Lookback days (default: 90)")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without saving")
    args = parser.parse_args()

    assets = [args.asset] if args.asset else config.BACKTEST_ASSETS

    print(f"Recalibration started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Assets: {', '.join(assets)} | Lookback: {args.days} days")
    if args.dry_run:
        print("DRY RUN MODE — no changes will be saved")

    results = {}
    for symbol in assets:
        results[symbol] = calibrate_asset(symbol, days=args.days, dry_run=args.dry_run)

    # Summary
    print(f"\n{'='*50}")
    print(f"  RECALIBRATION COMPLETE")
    print(f"{'='*50}")
    for symbol, data in results.items():
        if data:
            trend = "BULL" if data.get("trend_bullish") else "BEAR" if data.get("trend_bullish") is False else "?"
            print(
                f"  {symbol}: vol={data['vol_ratio']:.2f}x | "
                f"RSI=[{data['rsi_oversold']:.0f}/{data['rsi_overbought']:.0f}] "
                f"MFI=[{data['mfi_oversold']:.0f}/{data['mfi_overbought']:.0f}] | "
                f"trend={trend}"
            )


if __name__ == "__main__":
    main()

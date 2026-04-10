#!/usr/bin/env python3
"""Market analysis script for Krabje (CRO role).

Fetches live market data, bot status, and calculates indicators on multiple
timeframes. Outputs structured text that Krabje can read and act on.

Called every 15 minutes via OpenClaw cron job.

Usage:
    python analyze_market.py              # Full analysis, all assets
    python analyze_market.py --json       # JSON output (for programmatic use)
    python analyze_market.py --asset BTC  # Single asset analysis
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import config
from data.fetcher import fetch_candles_paginated, INTERVAL_MS
from strategy.confluence import calculate_rsi, calculate_atr, calculate_ema

# ---------------------------------------------------------------------------
# Hyperliquid API helpers
# ---------------------------------------------------------------------------

API_URL = "https://api.hyperliquid.xyz/info"

ASSETS = ["BTC", "ETH", "SOL", "DOGE"]
TIMEFRAMES = ["15m", "1h", "4h"]
LEVERAGE = {"BTC": 40, "ETH": 25, "SOL": 20, "DOGE": 10}


def fetch_meta_and_contexts() -> dict:
    """Fetch current funding, OI, price for all assets."""
    resp = requests.post(API_URL, json={"type": "metaAndAssetCtxs"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    meta_universe = data[0]["universe"]
    ctxs = data[1]

    result = {}
    for i, m in enumerate(meta_universe):
        name = m["name"]
        if name in ASSETS:
            ctx = ctxs[i]
            result[name] = {
                "price": float(ctx.get("markPx", 0)),
                "oracle_price": float(ctx.get("oraclePx", 0)),
                "funding_rate": float(ctx.get("funding", 0)),
                "open_interest": float(ctx.get("openInterest", 0)),
                "day_volume": float(ctx.get("dayNtlVlm", 0)),
                "premium": float(ctx.get("premium", 0)),
                "max_leverage": m.get("maxLeverage", 0),
            }
    return result


def fetch_recent_candles(coin: str, interval: str, count: int = 200) -> pd.DataFrame:
    """Fetch recent candles without caching (always fresh)."""
    interval_ms = INTERVAL_MS.get(interval, 3_600_000)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (count * interval_ms)

    try:
        df = fetch_candles_paginated(coin, interval, start_ms, end_ms)
    except requests.exceptions.HTTPError as e:
        if "429" in str(e) or "rate" in str(e).lower():
            time.sleep(5)  # Back off on rate limit
            df = fetch_candles_paginated(coin, interval, start_ms, end_ms)
        else:
            raise
    return df


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def kaufman_efficiency_ratio(closes: pd.Series, lookback: int = 20) -> float:
    """Calculate Kaufman Efficiency Ratio for the most recent candle."""
    if len(closes) < lookback + 1:
        return 0.0
    net_move = abs(closes.iloc[-1] - closes.iloc[-1 - lookback])
    sum_moves = sum(
        abs(closes.iloc[j] - closes.iloc[j - 1])
        for j in range(len(closes) - lookback, len(closes))
    )
    return net_move / sum_moves if sum_moves > 0 else 0.0


def _fmt_price(price: float) -> str:
    """Format price with appropriate decimals."""
    if price >= 100:
        return f"{price:,.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.6f}"


def detect_sr_proximity(price: float, zones: list, atr: float) -> list:
    """Find S/R zones within 2 ATR of current price."""
    nearby = []
    for z in zones:
        dist_low = abs(price - z["low"])
        dist_high = abs(price - z["high"])
        min_dist = min(dist_low, dist_high)
        if min_dist <= 2 * atr:
            side = "above" if price > z["high"] else "below" if price < z["low"] else "inside"
            nearby.append({
                "zone": f"{_fmt_price(z['low'])}-{_fmt_price(z['high'])}",
                "strength": z.get("strength", 1),
                "distance_atr": round(min_dist / atr, 2) if atr > 0 else 0,
                "side": side,
            })
    return nearby


def analyze_single_timeframe(coin: str, interval: str) -> dict:
    """Run indicator analysis on a single timeframe."""
    df = fetch_recent_candles(coin, interval, count=220)
    if df.empty or len(df) < 50:
        return {"error": f"Insufficient data for {coin} {interval}"}

    closes = df["close"]
    rsi = calculate_rsi(closes, period=14)
    atr = calculate_atr(df, period=14)
    ema_50 = calculate_ema(closes, period=50)
    ema_200 = calculate_ema(closes, period=200)

    last_idx = len(df) - 1
    current_price = closes.iloc[-1]
    current_atr = atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else 0
    current_rsi = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50
    current_ema50 = ema_50.iloc[-1] if not pd.isna(ema_50.iloc[-1]) else current_price
    current_ema200 = ema_200.iloc[-1] if not pd.isna(ema_200.iloc[-1]) else current_price

    # Trend determination
    if current_price > current_ema200 and current_ema50 > current_ema200:
        trend = "BULLISH"
    elif current_price < current_ema200 and current_ema50 < current_ema200:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    # Kaufman ER
    er = kaufman_efficiency_ratio(closes, lookback=20)

    # Recent price action
    change_5 = (closes.iloc[-1] / closes.iloc[-6] - 1) * 100 if len(closes) > 5 else 0
    change_20 = (closes.iloc[-1] / closes.iloc[-21] - 1) * 100 if len(closes) > 20 else 0

    # Volatility regime
    atr_ratio = current_atr / current_price if current_price > 0 else 0

    # S/R zones from swing highs/lows
    from strategy.support_resistance import find_sr_zones
    zones_dict = find_sr_zones(df, window=4)
    all_zones = zones_dict.get("support", []) + zones_dict.get("resistance", [])
    nearby_zones = detect_sr_proximity(current_price, all_zones, current_atr)

    return {
        "interval": interval,
        "price": round(current_price, 4),
        "rsi": round(current_rsi, 1),
        "atr": round(current_atr, 4),
        "atr_pct": round(atr_ratio * 100, 3),
        "ema_50": round(current_ema50, 2),
        "ema_200": round(current_ema200, 2),
        "trend": trend,
        "kaufman_er": round(er, 3),
        "regime": "TRENDING" if er > 0.3 else "CHOPPY" if er < 0.15 else "MIXED",
        "change_5": round(change_5, 2),
        "change_20": round(change_20, 2),
        "nearby_sr_zones": nearby_zones,
    }


# ---------------------------------------------------------------------------
# Bot status from SQLite
# ---------------------------------------------------------------------------

def get_bot_status() -> dict | None:
    """Read current bot status from SQLite."""
    try:
        from state.database import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT status_json, timestamp FROM bot_status ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            status = json.loads(row["status_json"])
            status["_db_timestamp"] = row["timestamp"]
            return status
    except Exception:
        pass
    return None


def get_recent_trades(limit: int = 10) -> list:
    """Read recent trades from SQLite."""
    try:
        from state.database import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze_asset(coin: str, live_data: dict) -> dict:
    """Full multi-timeframe analysis for a single asset."""
    asset_live = live_data.get(coin, {})

    timeframe_analysis = {}
    for tf in TIMEFRAMES:
        try:
            timeframe_analysis[tf] = analyze_single_timeframe(coin, tf)
        except Exception as e:
            timeframe_analysis[tf] = {"error": str(e)}
        time.sleep(1.0)  # Rate limit protection between API calls

    # Consensus across timeframes
    trends = [v.get("trend") for v in timeframe_analysis.values() if "trend" in v]
    bull_count = trends.count("BULLISH")
    bear_count = trends.count("BEARISH")
    if bull_count > bear_count:
        consensus = "BULLISH"
    elif bear_count > bull_count:
        consensus = "BEARISH"
    else:
        consensus = "NEUTRAL"

    # Risk flags
    flags = []
    funding = asset_live.get("funding_rate", 0)
    if abs(funding) >= config.EXTREME_FUNDING_RATE:
        direction = "LONG" if funding > 0 else "SHORT"
        flags.append(f"EXTREME FUNDING: {funding*100:.4f}% (crowd is {direction})")

    for tf, data in timeframe_analysis.items():
        if tf == "15m":
            continue  # 15m chop is normal, only flag 1h+ timeframes
        if isinstance(data, dict) and data.get("regime") == "CHOPPY":
            flags.append(f"CHOPPY on {tf} (ER={data.get('kaufman_er', 0):.3f})")
        rsi = data.get("rsi", 50) if isinstance(data, dict) else 50
        if rsi > 80:
            flags.append(f"RSI OVERBOUGHT on {tf}: {rsi:.0f}")
        elif rsi < 20:
            flags.append(f"RSI OVERSOLD on {tf}: {rsi:.0f}")

    return {
        "coin": coin,
        "leverage": LEVERAGE.get(coin, 10),
        "live": asset_live,
        "timeframes": timeframe_analysis,
        "consensus_trend": consensus,
        "risk_flags": flags,
    }


def format_text_report(analysis: dict, bot_status: dict | None, recent_trades: list) -> str:
    """Format analysis as readable text for Krabje."""
    lines = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"{'='*65}")
    lines.append(f"  MARKET ANALYSIS -- {now}")
    lines.append(f"{'='*65}")

    # Bot status
    if bot_status:
        age = ""
        try:
            ts = datetime.fromisoformat(bot_status["_db_timestamp"])
            age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
            age = f" (data {int(age_sec)}s oud)"
        except Exception:
            pass

        lines.append(f"\n--- BOT STATUS{age} ---")
        lines.append(f"  Equity: ${bot_status.get('equity', 0):,.2f}")
        halted = bot_status.get("halted", False)
        lines.append(f"  Halted: {'JA!' if halted else 'Nee'}")

        for symbol, bot in bot_status.get("bots", {}).items():
            state = bot.get("state", "?")
            pnl = bot.get("total_pnl", 0)
            pos_str = ""
            if bot.get("position"):
                p = bot["position"]
                pos_str = (
                    f" | {p['direction']} @ {p['avg_entry']:.2f}"
                    f" SL={p['stop_loss']:.2f} TP={p['take_profit']:.2f}"
                )
            lines.append(f"  {symbol:6s} {state:15s} PnL: ${pnl:+,.2f}{pos_str}")
    else:
        lines.append("\n--- BOT STATUS: niet beschikbaar (bot draait niet?) ---")

    # Per-asset analysis
    for coin_data in analysis.values():
        coin = coin_data["coin"]
        live = coin_data.get("live", {})
        lines.append(f"\n{'='*65}")
        lines.append(f"  {coin} ({coin_data['leverage']}x) -- Consensus: {coin_data['consensus_trend']}")
        lines.append(f"{'='*65}")

        # Live data
        if live:
            lines.append(
                f"  Prijs: ${live.get('price', 0):,.2f} | "
                f"Funding: {live.get('funding_rate', 0)*100:.4f}% | "
                f"OI: {live.get('open_interest', 0):,.0f} | "
                f"24h Vol: ${live.get('day_volume', 0):,.0f}"
            )

        # Timeframes
        for tf, data in coin_data.get("timeframes", {}).items():
            if "error" in data:
                lines.append(f"  [{tf}] ERROR: {data['error']}")
                continue

            er_bar = "#" * int(data["kaufman_er"] * 20)
            lines.append(
                f"  [{tf:3s}] RSI={data['rsi']:5.1f} | "
                f"Trend={data['trend']:8s} | "
                f"ER={data['kaufman_er']:.3f} [{er_bar:<6s}] {data['regime']:8s} | "
                f"5c={data['change_5']:+.2f}% 20c={data['change_20']:+.2f}%"
            )

            # Nearby S/R zones
            for z in data.get("nearby_sr_zones", [])[:2]:
                lines.append(
                    f"        S/R {z['zone']} (str={z['strength']}) "
                    f"{z['distance_atr']:.1f} ATR {z['side']}"
                )

        # Risk flags
        if coin_data.get("risk_flags"):
            lines.append(f"  ** RISK FLAGS **")
            for flag in coin_data["risk_flags"]:
                lines.append(f"    ! {flag}")

    # Recent trades
    if recent_trades:
        lines.append(f"\n{'='*65}")
        lines.append(f"  LAATSTE {len(recent_trades)} TRADES")
        lines.append(f"{'='*65}")
        for t in recent_trades[:5]:
            lines.append(
                f"  {t['symbol']:6s} {t['direction']:5s} "
                f"${t['entry_price']:>10,.2f} -> ${t['exit_price']:>10,.2f} "
                f"PnL: ${t['net_pnl']:>8,.2f} | {t['exit_reason']}"
            )

    # Decision guidance
    lines.append(f"\n{'='*65}")
    lines.append(f"  ACTIE NODIG?")
    lines.append(f"{'='*65}")

    all_flags = []
    for coin_data in analysis.values():
        all_flags.extend(coin_data.get("risk_flags", []))

    if not all_flags:
        lines.append("  Geen risk flags. Bot kan doordraaien.")
    else:
        lines.append(f"  {len(all_flags)} risk flag(s) gedetecteerd:")
        for f in all_flags:
            lines.append(f"    - {f}")
        lines.append("")
        lines.append("  Overweeg:")
        lines.append("    cli.py kill 'reden'     -- Noodstop")
        lines.append("    cli.py resume            -- Herstart na check")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Market analysis for Krabje CRO")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--asset", type=str, help="Analyze single asset")
    parser.add_argument("--quiet", action="store_true", help="Only output if risk flags found")
    args = parser.parse_args()

    assets = [args.asset.upper()] if args.asset else ASSETS

    # Fetch live data
    try:
        live_data = fetch_meta_and_contexts()
    except Exception as e:
        print(f"ERROR: Kan live data niet ophalen: {e}", file=sys.stderr)
        live_data = {}

    # Analyze each asset (with rate limit protection)
    analysis = {}
    for i, coin in enumerate(assets):
        try:
            analysis[coin] = analyze_asset(coin, live_data)
        except Exception as e:
            analysis[coin] = {"coin": coin, "error": str(e), "risk_flags": [f"ANALYSIS FAILED: {e}"]}
        if i < len(assets) - 1:
            time.sleep(1.0)  # Pause between assets

    # Bot status
    bot_status = get_bot_status()
    recent_trades = get_recent_trades(10)

    # Quiet mode: only output if risk flags
    if args.quiet:
        all_flags = []
        for a in analysis.values():
            all_flags.extend(a.get("risk_flags", []))
        if not all_flags:
            return  # Silent exit

    # Output
    if args.json:
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_status": bot_status,
            "recent_trades": recent_trades,
            "analysis": analysis,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        report = format_text_report(analysis, bot_status, recent_trades)
        print(report)


if __name__ == "__main__":
    main()

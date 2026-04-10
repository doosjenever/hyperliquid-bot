"""Sweep & Reclaim detection module.

Core entry logic for the Sweep & Reclaim strategy:
1. Detect when price sweeps THROUGH an S/R zone (liquidity grab)
2. Wait for price to reclaim the zone (close back inside)
3. Confirm with CVD (real buyers/sellers, not empty liquidity)
4. Detect Fair Value Gaps as take-profit targets

CVD in backtester uses a candle-based proxy (buy/sell volume from price action).
Live bot uses actual trade-level CVD from WebSocket data.
"""

import numpy as np
import pandas as pd

import config


def detect_sweep(candle: dict, zone: dict, direction: str, atr: float) -> dict | None:
    """Detect if a candle sweeps through an S/R zone.

    Sweep = price breaks THROUGH zone boundary (triggers retail stops).
    For longs: low goes below support zone low.
    For shorts: high goes above resistance zone high.

    Returns sweep info dict or None.
    """
    max_depth = atr * config.SWEEP_INVALIDATION_ATR
    min_depth = atr * config.SWEEP_MIN_DEPTH_ATR

    if direction == "long":
        zone_boundary = zone["low"]
        if candle["low"] < zone_boundary:
            sweep_depth = zone_boundary - candle["low"]
            if min_depth <= sweep_depth <= max_depth:
                return {
                    "direction": direction,
                    "zone": zone,
                    "sweep_wick": candle["low"],
                    "sweep_depth": sweep_depth,
                    "sweep_depth_atr": sweep_depth / atr if atr > 0 else 0,
                }
    else:  # short
        zone_boundary = zone["high"]
        if candle["high"] > zone_boundary:
            sweep_depth = candle["high"] - zone_boundary
            if min_depth <= sweep_depth <= max_depth:
                return {
                    "direction": direction,
                    "zone": zone,
                    "sweep_wick": candle["high"],
                    "sweep_depth": sweep_depth,
                    "sweep_depth_atr": sweep_depth / atr if atr > 0 else 0,
                }

    return None


def detect_reclaim(candle: dict, sweep: dict) -> dict | None:
    """Detect if a candle reclaims the zone after a sweep.

    Reclaim = candle CLOSES back inside the zone.
    For longs: close is above support zone low.
    For shorts: close is below resistance zone high.

    Returns reclaim info dict or None.
    """
    zone = sweep["zone"]

    if sweep["direction"] == "long":
        if candle["close"] > zone["low"]:
            return {
                "entry_price": candle["close"],
                "reclaim_candle_low": candle["low"],
                "reclaim_candle_high": candle["high"],
            }
    else:  # short
        if candle["close"] < zone["high"]:
            return {
                "entry_price": candle["close"],
                "reclaim_candle_low": candle["low"],
                "reclaim_candle_high": candle["high"],
            }

    return None


def check_sweep_still_valid(candle: dict, sweep: dict, atr: float) -> bool:
    """Check if sweep is still valid (price hasn't gone too deep).

    Invalidation: price goes > 1.5x ATR beyond zone = not a sweep, it's a trend break.
    This replaces the old time-based reclaim window with a price-based one.
    """
    max_depth = atr * config.SWEEP_INVALIDATION_ATR

    if sweep["direction"] == "long":
        deepest = candle["low"]
        zone_boundary = sweep["zone"]["low"]
        return (zone_boundary - deepest) <= max_depth
    else:
        deepest = candle["high"]
        zone_boundary = sweep["zone"]["high"]
        return (deepest - zone_boundary) <= max_depth


def calculate_cvd_proxy(df: pd.DataFrame, idx: int, lookback: int = None) -> dict:
    """Calculate CVD (Cumulative Volume Delta) proxy from candle data.

    Since backtester doesn't have tick data, we estimate:
    - Buy volume: volume on green candles (close > open)
    - Sell volume: volume on red candles (close < open)
    - For doji candles: split 50/50

    Returns dict with cvd_delta, buy_volume, sell_volume, delta_ratio.
    """
    if lookback is None:
        lookback = config.CVD_LOOKBACK_CANDLES

    start = max(0, idx - lookback + 1)
    window = df.iloc[start:idx + 1]

    buy_vol = 0.0
    sell_vol = 0.0

    for _, row in window.iterrows():
        vol = row["volume"]
        if vol <= 0:
            continue

        body = row["close"] - row["open"]
        candle_range = row["high"] - row["low"]

        if candle_range > 0:
            # Proportional split based on close position within range
            buy_ratio = (row["close"] - row["low"]) / candle_range
            sell_ratio = (row["high"] - row["close"]) / candle_range
            buy_vol += vol * buy_ratio
            sell_vol += vol * sell_ratio
        else:
            buy_vol += vol * 0.5
            sell_vol += vol * 0.5

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


def cvd_confirms_direction(cvd: dict, direction: str) -> bool:
    """Check if CVD confirms the trade direction.

    Long: net buying pressure (positive delta ratio above threshold).
    Short: net selling pressure (negative delta ratio below -threshold).
    """
    threshold = config.CVD_MIN_DELTA_RATIO

    if direction == "long":
        return cvd["delta_ratio"] > threshold
    else:
        return cvd["delta_ratio"] < -threshold


def find_fair_value_gaps(df: pd.DataFrame, idx: int, atr: float,
                         max_age: int = None) -> list[dict]:
    """Find unfilled Fair Value Gaps (FVGs) in recent candle data.

    Bullish FVG: gap between candle[i-2].high and candle[i].low
      (price moved up so fast it left a gap — tends to fill)
    Bearish FVG: gap between candle[i-2].low and candle[i].high
      (price moved down so fast it left a gap — tends to fill)

    Returns list of unfilled FVGs sorted by proximity to current price.
    """
    if max_age is None:
        max_age = config.FVG_MAX_AGE_CANDLES

    min_size = atr * config.FVG_MIN_SIZE_ATR
    fill_threshold = config.FVG_FILL_THRESHOLD

    start_idx = max(2, idx - max_age)
    current_price = df.iloc[idx]["close"]

    fvgs = []

    for i in range(start_idx, idx - 1):
        candle_before = df.iloc[i - 2]
        candle_after = df.iloc[i]

        # Bullish FVG: gap up
        gap_low = candle_before["high"]
        gap_high = candle_after["low"]
        if gap_high > gap_low and (gap_high - gap_low) >= min_size:
            # Check if FVG has been filled
            filled_amount = 0
            gap_size = gap_high - gap_low
            for j in range(i + 1, idx + 1):
                low_j = df.iloc[j]["low"]
                if low_j <= gap_low:
                    filled_amount = gap_size  # fully filled
                    break
                elif low_j < gap_high:
                    filled_amount = max(filled_amount, gap_high - low_j)

            fill_ratio = filled_amount / gap_size if gap_size > 0 else 1.0
            if fill_ratio < fill_threshold:
                fvgs.append({
                    "type": "bullish",
                    "low": gap_low,
                    "high": gap_high,
                    "midpoint": (gap_low + gap_high) / 2,
                    "age": idx - i,
                    "fill_ratio": fill_ratio,
                })

        # Bearish FVG: gap down
        gap_high_b = candle_before["low"]
        gap_low_b = candle_after["high"]
        if gap_high_b > gap_low_b and (gap_high_b - gap_low_b) >= min_size:
            filled_amount = 0
            gap_size = gap_high_b - gap_low_b
            for j in range(i + 1, idx + 1):
                high_j = df.iloc[j]["high"]
                if high_j >= gap_high_b:
                    filled_amount = gap_size
                    break
                elif high_j > gap_low_b:
                    filled_amount = max(filled_amount, high_j - gap_low_b)

            fill_ratio = filled_amount / gap_size if gap_size > 0 else 1.0
            if fill_ratio < fill_threshold:
                fvgs.append({
                    "type": "bearish",
                    "low": gap_low_b,
                    "high": gap_high_b,
                    "midpoint": (gap_low_b + gap_high_b) / 2,
                    "age": idx - i,
                    "fill_ratio": fill_ratio,
                })

    # Sort by proximity to current price
    fvgs.sort(key=lambda f: abs(f["midpoint"] - current_price))

    return fvgs


def find_fvg_target(fvgs: list[dict], entry_price: float, direction: str) -> float | None:
    """Find the nearest FVG as a take-profit target.

    Long: nearest bearish FVG ABOVE entry (price tends to fill gaps above).
    Short: nearest bullish FVG BELOW entry (price tends to fill gaps below).

    Returns the FVG midpoint as target price, or None if no suitable FVG found.
    """
    for fvg in fvgs:
        if direction == "long" and fvg["type"] == "bearish" and fvg["midpoint"] > entry_price:
            return fvg["midpoint"]
        if direction == "short" and fvg["type"] == "bullish" and fvg["midpoint"] < entry_price:
            return fvg["midpoint"]

    return None

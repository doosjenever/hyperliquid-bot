"""Support and Resistance level detection.

Identifies S/R zones from historical price data using:
1. Swing highs and lows (local extrema)
2. Zone clustering (nearby levels merged into zones)
3. Touch count (how often price has reacted at a level)

Works on any timeframe — use HTF (4H/Daily) for major zones,
LTF (15m/1H) for entry timing.
"""

import numpy as np
import pandas as pd

import config


def find_swing_points(df: pd.DataFrame, window: int = 5) -> tuple[list[float], list[float]]:
    """Find swing highs and swing lows using a rolling window.

    A swing high is a candle whose high is the highest in the surrounding window.
    A swing low is a candle whose low is the lowest in the surrounding window.
    """
    highs = df["high"].values
    lows = df["low"].values
    swing_highs = []
    swing_lows = []

    for i in range(window, len(df) - window):
        # Swing high: highest high in the window
        if highs[i] == max(highs[i - window:i + window + 1]):
            swing_highs.append(highs[i])
        # Swing low: lowest low in the window
        if lows[i] == min(lows[i - window:i + window + 1]):
            swing_lows.append(lows[i])

    return swing_highs, swing_lows


def cluster_levels(levels: list[float], threshold: float = None) -> list[dict]:
    """Cluster nearby price levels into S/R zones.

    Merges levels that are within `threshold` (as fraction of price) of each other.
    Returns zones with: center price, strength (touch count), and price range.
    """
    if not levels:
        return []

    if threshold is None:
        threshold = config.SR_ZONE_THRESHOLD

    sorted_levels = sorted(levels)
    zones = []
    current_cluster = [sorted_levels[0]]

    for level in sorted_levels[1:]:
        # If this level is close enough to the cluster, add it
        cluster_center = np.mean(current_cluster)
        if abs(level - cluster_center) / cluster_center <= threshold:
            current_cluster.append(level)
        else:
            # Finalize the current cluster
            zones.append({
                "price": float(np.mean(current_cluster)),
                "strength": len(current_cluster),
                "low": float(min(current_cluster)),
                "high": float(max(current_cluster)),
            })
            current_cluster = [level]

    # Don't forget the last cluster
    zones.append({
        "price": float(np.mean(current_cluster)),
        "strength": len(current_cluster),
        "low": float(min(current_cluster)),
        "high": float(max(current_cluster)),
    })

    return zones


def find_sr_zones(df: pd.DataFrame, window: int = 5) -> dict:
    """Find S/R zones from a DataFrame of candles.

    Returns dict with 'support' and 'resistance' zone lists,
    each sorted by strength (strongest first).
    """
    swing_highs, swing_lows = find_swing_points(df, window=window)

    resistance_zones = cluster_levels(swing_highs)
    support_zones = cluster_levels(swing_lows)

    # Sort by strength (most touches first)
    resistance_zones.sort(key=lambda z: z["strength"], reverse=True)
    support_zones.sort(key=lambda z: z["strength"], reverse=True)

    return {
        "support": support_zones,
        "resistance": resistance_zones,
    }


def price_near_zone(price: float, zones: list[dict], proximity: float = None) -> dict | None:
    """Check if current price is near any S/R zone.

    Returns the nearest zone if within proximity, or None.
    proximity is a fraction of price (default: SR_ZONE_THRESHOLD * 2).
    """
    if proximity is None:
        proximity = config.SR_ZONE_THRESHOLD * 2

    nearest = None
    min_distance = float("inf")

    for zone in zones:
        distance = abs(price - zone["price"]) / price
        if distance <= proximity and distance < min_distance:
            min_distance = distance
            nearest = zone

    return nearest


def find_multi_timeframe_sr(candles_by_tf: dict[str, pd.DataFrame]) -> dict:
    """Find S/R zones across multiple timeframes.

    candles_by_tf: {"4h": df_4h, "1d": df_1d, "15m": df_15m, ...}

    Returns combined zones with timeframe weighting.
    HTF zones are stronger than LTF zones.
    """
    htf_weight = 2.0
    ltf_weight = 1.0

    all_support = []
    all_resistance = []

    for tf, df in candles_by_tf.items():
        if df.empty:
            continue
        zones = find_sr_zones(df)
        weight = htf_weight if tf in config.HTF_TIMEFRAMES else ltf_weight

        for zone in zones["support"]:
            zone["strength"] = int(zone["strength"] * weight)
            zone["timeframe"] = tf
            all_support.append(zone)

        for zone in zones["resistance"]:
            zone["strength"] = int(zone["strength"] * weight)
            zone["timeframe"] = tf
            all_resistance.append(zone)

    # Re-cluster across timeframes (HTF and LTF zones near each other = strong)
    support_prices = [z["price"] for z in all_support]
    resistance_prices = [z["price"] for z in all_resistance]

    return {
        "support": cluster_levels(support_prices) if support_prices else [],
        "resistance": cluster_levels(resistance_prices) if resistance_prices else [],
    }

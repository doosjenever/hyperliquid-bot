"""Volume Profile analysis.

Calculates the Volume Profile (VPVR) from historical candle data:
- Point of Control (POC): price level with highest traded volume
- Value Area High/Low: range containing ~70% of total volume

These are powerful S/R indicators because they show where real
trading activity occurred, not just where price bounced.
"""

import numpy as np
import pandas as pd

import config


def calculate_volume_profile(df: pd.DataFrame, bins: int = None) -> dict:
    """Calculate Volume Profile from OHLCV data.

    Distributes each candle's volume across its price range (high-low),
    then bins by price to create the profile.

    Returns:
        poc: Point of Control price (highest volume)
        value_area_high: upper bound of 70% volume area
        value_area_low: lower bound of 70% volume area
        profile: list of {price, volume} bins
    """
    if bins is None:
        bins = config.VOLUME_PROFILE_BINS

    if df.empty:
        return {"poc": 0, "value_area_high": 0, "value_area_low": 0, "profile": []}

    price_min = df["low"].min()
    price_max = df["high"].max()

    if price_min == price_max:
        return {"poc": price_min, "value_area_high": price_max, "value_area_low": price_min, "profile": []}

    # Create price bins
    bin_edges = np.linspace(price_min, price_max, bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    bin_volumes = np.zeros(bins)

    # Distribute each candle's volume across its price range
    for _, row in df.iterrows():
        candle_low = row["low"]
        candle_high = row["high"]
        candle_volume = row["volume"]

        if candle_high == candle_low or candle_volume == 0:
            # Single price candle: all volume in one bin
            idx = np.searchsorted(bin_edges, candle_low, side="right") - 1
            idx = max(0, min(idx, bins - 1))
            bin_volumes[idx] += candle_volume
            continue

        # Find which bins this candle's range covers
        for i in range(bins):
            bin_low = bin_edges[i]
            bin_high = bin_edges[i + 1]

            # Overlap between candle range and bin
            overlap_low = max(candle_low, bin_low)
            overlap_high = min(candle_high, bin_high)

            if overlap_high > overlap_low:
                # Fraction of candle range in this bin
                fraction = (overlap_high - overlap_low) / (candle_high - candle_low)
                bin_volumes[i] += candle_volume * fraction

    # Point of Control: bin with highest volume
    poc_idx = np.argmax(bin_volumes)
    poc_price = float(bin_centers[poc_idx])

    # Value Area: 70% of total volume, expanding from POC
    total_volume = bin_volumes.sum()
    target_volume = total_volume * 0.70

    va_low_idx = poc_idx
    va_high_idx = poc_idx
    accumulated = bin_volumes[poc_idx]

    while accumulated < target_volume and (va_low_idx > 0 or va_high_idx < bins - 1):
        # Expand to the side with more volume
        low_vol = bin_volumes[va_low_idx - 1] if va_low_idx > 0 else 0
        high_vol = bin_volumes[va_high_idx + 1] if va_high_idx < bins - 1 else 0

        if low_vol >= high_vol and va_low_idx > 0:
            va_low_idx -= 1
            accumulated += bin_volumes[va_low_idx]
        elif va_high_idx < bins - 1:
            va_high_idx += 1
            accumulated += bin_volumes[va_high_idx]
        else:
            va_low_idx -= 1
            accumulated += bin_volumes[va_low_idx]

    profile = [
        {"price": float(bin_centers[i]), "volume": float(bin_volumes[i])}
        for i in range(bins) if bin_volumes[i] > 0
    ]

    # Average volume per non-empty bin (for high-volume node filtering)
    non_zero = bin_volumes[bin_volumes > 0]
    avg_vol = float(non_zero.mean()) if len(non_zero) > 0 else 0

    return {
        "poc": poc_price,
        "value_area_high": float(bin_centers[va_high_idx]),
        "value_area_low": float(bin_centers[va_low_idx]),
        "profile": profile,
        "avg_bin_volume": avg_vol,
    }


def price_near_poc(price: float, vp: dict, proximity: float = None) -> bool:
    """Check if price is near the Volume Profile Point of Control."""
    if proximity is None:
        proximity = config.SR_ZONE_THRESHOLD * 2
    if vp["poc"] == 0:
        return False
    return abs(price - vp["poc"]) / price <= proximity


def is_high_volume_node(price: float, vp: dict, threshold: float = 1.5) -> bool:
    """Check if price sits at a High Volume Node (HVN).

    A HVN has volume >= threshold * average bin volume.
    Only HVNs are meaningful S/R — low volume nodes are noise.
    """
    avg = vp.get("avg_bin_volume", 0)
    if avg == 0:
        return False

    for node in vp.get("profile", []):
        # Check if this node covers the price
        if abs(price - node["price"]) / price <= config.SR_ZONE_THRESHOLD * 2:
            if node["volume"] >= avg * threshold:
                return True
    return False


def price_in_value_area(price: float, vp: dict) -> bool:
    """Check if price is within the Value Area (70% volume zone)."""
    return vp["value_area_low"] <= price <= vp["value_area_high"]

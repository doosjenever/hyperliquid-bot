"""Confluence scoring engine — Sweep & Reclaim Strategy v1.0.

New scoring system centered on Liquidity Sweep & Reclaim:
  - Sweep + Reclaim confirmed: +50
  - Volume Profile POC/HVN: +25
  - OI Divergence (gunstig): +30 (live only, placeholder in backtest)
  - CVD confirmation: +20
  - Extreme funding (gunstig): +30 / (tegen): -40
  - RSI extreme: +10
  - EMA trend alignment: +10

Hard vetoes:
  - Extreme funding against direction → -40 penalty
  - No sweep detected → no trade (sweep IS the entry)

Minimum for trade: 90 (configurable via config.MIN_CONFLUENCE_SCORE)
"""

import numpy as np
import pandas as pd

import config
from strategy.support_resistance import price_near_zone, find_sr_zones
from strategy.volume_profile import calculate_volume_profile, price_near_poc, is_high_volume_node


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI (Relative Strength Index)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Money Flow Index (MFI) — volume-weighted RSI."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    money_flow = typical_price * df["volume"]

    delta = typical_price.diff()
    pos_flow = money_flow.where(delta > 0, 0.0)
    neg_flow = money_flow.where(delta < 0, 0.0)

    pos_sum = pos_flow.rolling(window=period, min_periods=period).sum()
    neg_sum = neg_flow.rolling(window=period, min_periods=period).sum()

    mfr = pos_sum / neg_sum.replace(0, 1e-10)
    mfi = 100 - (100 / (1 + mfr))
    return mfi


def calculate_cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Calculate Commodity Channel Index (CCI)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    sma = typical_price.rolling(window=period, min_periods=period).mean()
    mad = typical_price.rolling(window=period, min_periods=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    cci = (typical_price - sma) / (0.015 * mad.replace(0, 1e-10))
    return cci


def calculate_atr(df: pd.DataFrame, period: int = None) -> pd.Series:
    """Calculate Average True Range (ATR)."""
    if period is None:
        period = config.ATR_PERIOD

    high = df["high"]
    low = df["low"]
    close = df["close"].shift(1)

    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(span=period, min_periods=period).mean()
    return atr


def calculate_volatility_ratio(df: pd.DataFrame, df_reference: pd.DataFrame = None) -> float:
    """Calculate asset's volatility relative to BTC (or reference)."""
    atr = calculate_atr(df)
    atr_pct = (atr / df["close"]).dropna()
    asset_vol = atr_pct.mean()

    if df_reference is not None:
        atr_ref = calculate_atr(df_reference)
        atr_ref_pct = (atr_ref / df_reference["close"]).dropna()
        ref_vol = atr_ref_pct.mean()
        return asset_vol / ref_vol if ref_vol > 0 else 1.0

    return 1.0


def calculate_ema(series: pd.Series, period: int = 200) -> pd.Series:
    """Calculate Exponential Moving Average."""
    return series.ewm(span=period, min_periods=period).mean()


def score_trend_alignment(price: float, ema_200: float, direction: str) -> int:
    """Score based on trend alignment with 200 EMA.

    Sweep & Reclaim v1.0: stronger penalties for counter-trend.
    Counter-trend longs in a bear market are the #1 cause of false entries.
    """
    if pd.isna(ema_200):
        return 0

    if direction == "long":
        if price > ema_200:
            return 10   # Trend-aligned long
        else:
            return -30  # Counter-trend long (falling knife = very dangerous)
    else:
        if price < ema_200:
            return 10   # Trend-aligned short
        else:
            return 0    # Counter-trend short (mean reversion at resistance = valid)


def score_sweep_confluence(
    direction: str,
    zone: dict,
    volume_profile: dict,
    rsi_value: float | None = None,
    funding_rate: float | None = None,
    ema_200: float | None = None,
    cvd_confirms: bool = False,
    price: float = 0.0,
    rsi_oversold: float = 35.0,
    rsi_overbought: float = 65.0,
) -> dict:
    """Calculate confluence score for a Sweep & Reclaim trade.

    The sweep+reclaim has already been confirmed before calling this.
    This scores the additional confluence factors.

    Score components:
      - Sweep + Reclaim (confirmed): +50 (always, since this function is only called after reclaim)
      - Volume Profile POC/HVN: +25
      - CVD confirmation: +20
      - Funding rate: +30 (gunstig) / -40 (veto)
      - RSI confirmation: +10
      - EMA trend: +10/-10
    """
    components = {}

    # 1. Sweep + Reclaim confirmed (+50) — always present
    components["sweep_reclaim"] = 50

    # 1b. Zone strength bonus (multi-touch zones are stronger)
    zone_strength = zone.get("strength", 1)
    if zone_strength >= 3:
        components["zone_strength"] = 15
    elif zone_strength >= 2:
        components["zone_strength"] = 10

    # 2. Volume Profile POC (+25) or HVN (+20)
    zone_price = zone["price"]
    if price_near_poc(zone_price, volume_profile):
        components["volume_poc"] = 25
    elif is_high_volume_node(zone_price, volume_profile):
        components["volume_hvn"] = 20

    # 3. CVD confirmation (+20)
    if cvd_confirms:
        components["cvd"] = 20

    # 4. Funding rate (+30 gunstig / -40 veto)
    if funding_rate is not None:
        extreme = config.EXTREME_FUNDING_RATE
        if direction == "long" and funding_rate < -extreme:
            components["funding"] = config.FUNDING_BOOST_SCORE  # +30
        elif direction == "short" and funding_rate > extreme:
            components["funding"] = config.FUNDING_BOOST_SCORE  # +30
        elif direction == "long" and funding_rate > extreme * 2:
            components["funding_veto"] = config.FUNDING_VETO_SCORE  # -40
        elif direction == "short" and funding_rate < -extreme * 2:
            components["funding_veto"] = config.FUNDING_VETO_SCORE  # -40

    # 5. RSI confirmation (+10)
    if rsi_value is not None:
        if direction == "long" and rsi_value < rsi_oversold:
            components["rsi"] = 10
        elif direction == "short" and rsi_value > rsi_overbought:
            components["rsi"] = 10

    # 6. EMA trend alignment (+10/-10)
    if ema_200 is not None:
        trend_score = score_trend_alignment(price, ema_200, direction)
        if trend_score != 0:
            components["trend"] = trend_score

    total_score = sum(components.values())

    return {
        "score": total_score,
        "components": components,
        "trade": total_score >= config.MIN_CONFLUENCE_SCORE,
        "direction": direction,
        "price": price,
    }


def find_sweepable_zones(price: float, htf_zones: dict, atr: float) -> list[dict]:
    """Find S/R zones that are close enough for a potential sweep.

    Returns zones where price is within 2x ATR of the zone boundary.
    Used by the backtester and FSM to know which zones to monitor.
    """
    candidates = []
    proximity = atr * 2  # Watch zones within 2 ATR
    min_strength = config.SWEEP_MIN_ZONE_STRENGTH

    for zone in htf_zones.get("support", []):
        if zone["strength"] >= min_strength and abs(price - zone["low"]) <= proximity and price >= zone["low"] - atr:
            candidates.append({"zone": zone, "direction": "long"})

    for zone in htf_zones.get("resistance", []):
        if zone["strength"] >= min_strength and abs(price - zone["high"]) <= proximity and price <= zone["high"] + atr:
            candidates.append({"zone": zone, "direction": "short"})

    # Sort by zone strength
    candidates.sort(key=lambda c: c["zone"]["strength"], reverse=True)
    return candidates

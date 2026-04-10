"""Adaptive Asset Profile.

Auto-calibrates ALL trading parameters per asset from measured market data.
Nothing hardcoded — every asset gets its own personality derived from:
- Volatility (ATR% vs BTC baseline)
- RSI distribution (percentile-based thresholds)
- Swing characteristics (average swing duration → S/R window)
- Volume/liquidity (slippage estimation)

Key design decisions:
- Continuous scaling with clamped extremes (vangrails against outliers)
- RSI thresholds from actual percentiles, not fixed values
- S/R swing window derived from measured swing duration
- Trading hours only relevant for PAXG/TradFi proxies
- Self-recalibrates on every call with fresh data

Designed by Claude (Opus) and Krabje (OpenClaw).
"""

import pandas as pd
import numpy as np

import config
from strategy.confluence import calculate_atr, calculate_ema, calculate_rsi, calculate_mfi, calculate_cci


# Clamp boundaries (vangrails against extreme outliers)
INV_MULT_MIN = 0.8
INV_MULT_MAX = 3.0
DCA_SPACING_MIN = 0.15
DCA_SPACING_MAX = 1.5
STOP_MULT_MIN = 1.0
STOP_MULT_MAX = 3.0
RSI_OVERSOLD_MIN = 20    # RSI threshold can't go below 20
RSI_OVERSOLD_MAX = 45    # ... or above 45 (otherwise we buy "normal" dips)
RSI_OVERBOUGHT_MIN = 55  # Overbought can't go below 55
RSI_OVERBOUGHT_MAX = 80  # ... or above 80
SWING_WINDOW_MIN = 3     # Minimum S/R swing detection window
SWING_WINDOW_MAX = 12    # Maximum

# PAXG / TradFi proxy settings
TRADFI_PROXIES = {"PAXG"}
PAXG_LIQUID_HOURS = (14, 21)  # UTC hours (US session overlap)


class AssetProfile:
    """Adaptive parameter profile for a single asset.

    Every parameter is derived from the asset's own data.
    No fixed values — each asset gets what it needs.
    """

    def __init__(self, symbol: str, df: pd.DataFrame, df_reference: pd.DataFrame = None):
        self.symbol = symbol
        self.is_tradfi_proxy = symbol in TRADFI_PROXIES
        self._df = df

        # --- Volatility metrics ---
        atr = calculate_atr(df)
        atr_pct = (atr / df["close"]).dropna()
        self.avg_atr_pct = float(atr_pct.mean()) if len(atr_pct) > 0 else 0.01
        self.current_atr_pct = float(atr_pct.iloc[-1]) if len(atr_pct) > 0 else 0.01
        self.current_atr = float(atr.iloc[-1]) if len(atr) > 0 and not pd.isna(atr.iloc[-1]) else 0

        # Volatility ratio vs reference (BTC = 1.0)
        if df_reference is not None and not df_reference.empty:
            ref_atr = calculate_atr(df_reference)
            ref_atr_pct = (ref_atr / df_reference["close"]).dropna()
            ref_vol = float(ref_atr_pct.mean()) if len(ref_atr_pct) > 0 else 0.01
            self.vol_ratio = self.avg_atr_pct / ref_vol if ref_vol > 0 else 1.0
        else:
            self.vol_ratio = 1.0

        # --- Volume/liquidity ---
        self.avg_volume = float(df["volume"].mean()) if "volume" in df.columns else 0

        # --- Trend context ---
        ema = calculate_ema(df["close"], period=200)
        self.ema_200 = float(ema.iloc[-1]) if len(ema) > 0 and not pd.isna(ema.iloc[-1]) else None
        self.price = float(df["close"].iloc[-1])
        self.trend_bullish = self.price > self.ema_200 if self.ema_200 else None

        # --- Calibrate everything from data ---
        self._calibrate_volatility()
        self._calibrate_rsi(df)
        self._calibrate_swing_window(df)
        self._calibrate_mfi_cci(df)
        self._calibrate_slippage()

    def _calibrate_volatility(self):
        """Scale ATR-based parameters from volatility ratio.

        Sweep & Reclaim v1.0: stop buffer and invalidation are now based on
        sweep wick + vol_ratio scaling. These legacy multipliers are kept for
        compatibility with recalibration DB but recalculated from vol_ratio.
        """
        # Sweep stop buffer scales with vol_ratio (0.3 * ATR * vol_ratio)
        self.inv_multiplier = _clamp(1.0 * self.vol_ratio, INV_MULT_MIN, INV_MULT_MAX)
        self.stop_multiplier = _clamp(1.5 * self.vol_ratio, STOP_MULT_MIN, STOP_MULT_MAX)
        self.dca_spacing = _clamp(0.3 * self.vol_ratio, DCA_SPACING_MIN, DCA_SPACING_MAX)

    def _calibrate_rsi(self, df: pd.DataFrame):
        """Derive RSI thresholds from this asset's actual RSI distribution.

        Instead of fixed 35/65, we use the 10th and 90th percentile of
        the asset's historical RSI. What's "oversold" for BTC is different
        from what's "oversold" for SOL.
        """
        rsi = calculate_rsi(df["close"], period=14)
        rsi_clean = rsi.dropna().values

        if len(rsi_clean) < 50:
            # Not enough data — use sensible defaults
            self.rsi_oversold = 35.0
            self.rsi_overbought = 65.0
            self.rsi_dca_long = 40.0
            self.rsi_dca_short = 60.0
            return

        # Percentile-based thresholds (clamped)
        p10 = float(np.percentile(rsi_clean, 10))
        p90 = float(np.percentile(rsi_clean, 90))
        p20 = float(np.percentile(rsi_clean, 20))
        p80 = float(np.percentile(rsi_clean, 80))

        # Confluence scoring thresholds (stricter: 10th/90th percentile)
        self.rsi_oversold = _clamp(p10, RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX)
        self.rsi_overbought = _clamp(p90, RSI_OVERBOUGHT_MIN, RSI_OVERBOUGHT_MAX)

        # DCA entry thresholds (looser: 20th/80th percentile)
        # DCA entries should trigger more often than initial signals
        self.rsi_dca_long = _clamp(p20, RSI_OVERSOLD_MIN, RSI_OVERSOLD_MAX + 5)
        self.rsi_dca_short = _clamp(p80, RSI_OVERBOUGHT_MIN - 5, RSI_OVERBOUGHT_MAX)

    def _calibrate_mfi_cci(self, df: pd.DataFrame):
        """Derive MFI and CCI thresholds from this asset's distribution.

        Same percentile approach as RSI — what's "extreme" for BTC
        is different from what's "extreme" for SOL.
        """
        mfi = calculate_mfi(df, period=14)
        mfi_clean = mfi.dropna().values

        if len(mfi_clean) >= 50:
            self.mfi_oversold = _clamp(float(np.percentile(mfi_clean, 10)), 15, 40)
            self.mfi_overbought = _clamp(float(np.percentile(mfi_clean, 90)), 60, 85)
        else:
            self.mfi_oversold = 30.0
            self.mfi_overbought = 70.0

        cci = calculate_cci(df, period=20)
        cci_clean = cci.dropna().values

        if len(cci_clean) >= 50:
            self.cci_oversold = _clamp(float(np.percentile(cci_clean, 5)), -300, -80)
            self.cci_overbought = _clamp(float(np.percentile(cci_clean, 95)), 80, 300)
        else:
            self.cci_oversold = -150.0
            self.cci_overbought = 150.0

    def _calibrate_swing_window(self, df: pd.DataFrame):
        """Derive S/R swing detection window from average swing duration.

        Assets with fast, choppy swings need a smaller window.
        Assets with long, smooth trends need a larger window.
        Measured by counting candles between consecutive swing highs.
        """
        highs = df["high"].values
        n = len(highs)

        if n < 30:
            self.swing_window = 5
            return

        # Find local maxima with a small reference window
        ref_window = 3
        swing_indices = []
        for i in range(ref_window, n - ref_window):
            if highs[i] == max(highs[i - ref_window:i + ref_window + 1]):
                swing_indices.append(i)

        if len(swing_indices) < 3:
            self.swing_window = 5
            return

        # Average distance between consecutive swings
        distances = [swing_indices[i+1] - swing_indices[i] for i in range(len(swing_indices) - 1)]
        avg_swing_duration = float(np.median(distances))

        # Swing window = roughly half the swing duration (to catch the peaks)
        raw_window = int(avg_swing_duration / 2)
        self.swing_window = _clamp(raw_window, SWING_WINDOW_MIN, SWING_WINDOW_MAX)

    def _calibrate_slippage(self):
        """Estimate slippage from volume."""
        base_slippage = config.BACKTEST_SLIPPAGE_BPS
        if self.avg_volume > 0:
            vol_factor = max(1.0, min(3.0, 1e8 / max(self.avg_volume, 1)))
        else:
            vol_factor = 2.0
        self.estimated_slippage_bps = base_slippage * vol_factor

    def is_tradable_now(self, current_time=None) -> bool:
        """Check if asset should be traded at the given time."""
        if not self.is_tradfi_proxy:
            return True
        if current_time is None:
            return True
        hour = current_time.hour if hasattr(current_time, 'hour') else 12
        return PAXG_LIQUID_HOURS[0] <= hour < PAXG_LIQUID_HOURS[1]

    @property
    def entry_mode(self) -> str:
        """Entry mode — unified Sweep & Reclaim for all assets."""
        return "sweep_reclaim"

    def summary(self) -> str:
        """One-line summary for logging."""
        trend = "BULL" if self.trend_bullish else "BEAR" if self.trend_bullish is False else "?"
        return (
            f"{self.symbol}: vol={self.vol_ratio:.2f}x | "
            f"RSI=[{self.rsi_oversold:.0f}/{self.rsi_overbought:.0f}] "
            f"MFI=[{self.mfi_oversold:.0f}/{self.mfi_overbought:.0f}] "
            f"CCI=[{self.cci_oversold:.0f}/{self.cci_overbought:.0f}] | "
            f"swing_w={self.swing_window} | mode=sweep_reclaim | trend={trend}"
        )


def build_profiles(
    assets: list[str],
    timeframe: str = "1h",
    days: int = 90,
) -> dict[str, AssetProfile]:
    """Build AssetProfiles for multiple assets."""
    from data.fetcher import fetch_and_cache

    df_btc = fetch_and_cache("BTC", timeframe, days=days)
    profiles = {}
    for symbol in assets:
        df = fetch_and_cache(symbol, timeframe, days=days) if symbol != "BTC" else df_btc
        if df.empty:
            continue
        ref = df_btc if symbol != "BTC" else None
        profiles[symbol] = AssetProfile(symbol, df, ref)
    return profiles


def load_or_build_profile(
    symbol: str,
    df: pd.DataFrame,
    df_reference: pd.DataFrame = None,
    use_db: bool = True,
) -> AssetProfile:
    """Load a calibrated profile from DB, or build fresh if not available.

    For live trading: loads the recalibrated (smoothed) profile from SQLite.
    Falls back to fresh calibration if no DB profile exists.
    """
    if use_db:
        try:
            from state.database import get_connection, load_active_profile
            conn = get_connection()
            db_profile = load_active_profile(conn, symbol)
            conn.close()

            if db_profile:
                # Build a fresh AssetProfile and override with DB values
                profile = AssetProfile(symbol, df, df_reference)
                profile.vol_ratio = db_profile["vol_ratio"]
                profile.inv_multiplier = db_profile["inv_multiplier"]
                profile.stop_multiplier = db_profile["stop_multiplier"]
                profile.dca_spacing = db_profile["dca_spacing"]
                profile.rsi_oversold = db_profile["rsi_oversold"]
                profile.rsi_overbought = db_profile["rsi_overbought"]
                profile.rsi_dca_long = db_profile["rsi_dca_long"]
                profile.rsi_dca_short = db_profile["rsi_dca_short"]
                profile.mfi_oversold = db_profile["mfi_oversold"]
                profile.mfi_overbought = db_profile["mfi_overbought"]
                profile.cci_oversold = db_profile["cci_oversold"]
                profile.cci_overbought = db_profile["cci_overbought"]
                profile.swing_window = db_profile["swing_window"]
                profile.estimated_slippage_bps = db_profile["estimated_slippage_bps"]
                return profile
        except Exception:
            pass  # Fall through to fresh calibration

    return AssetProfile(symbol, df, df_reference)


def _clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(value, max_val))

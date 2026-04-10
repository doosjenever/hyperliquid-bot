"""Configuration for Hyperliquid trading bot — Sweep & Reclaim Strategy v1.0."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

# Hyperliquid
HL_API_URL = os.environ.get("HL_API_URL", "https://api.hyperliquid-testnet.xyz")
HL_WALLET_ADDRESS = os.environ.get("HL_WALLET_ADDRESS", "")
HL_PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")

# Trading parameters
MAX_RISK_PER_TRADE = 0.02  # 2% of equity per trade
MIN_CONFLUENCE_SCORE = 90  # Raised from 80 — sweep already gives +50
MAX_SLIPPAGE = 0.005  # 0.5%
MAX_MARGIN_USAGE = 0.75  # 75% max margin

# Circuit breaker
CIRCUIT_BREAKER_DRAWDOWN = 0.05  # 5% equity drop
CIRCUIT_BREAKER_WINDOW_MIN = 15
CIRCUIT_BREAKER_PAUSE_HOURS = 24

# S/R parameters
HTF_TIMEFRAMES = ["4h", "1d"]
LTF_TIMEFRAMES = ["15m", "1h"]
SR_LOOKBACK_CANDLES = 200
SR_ZONE_THRESHOLD = 0.003  # 0.3% price range for S/R zone clustering
VOLUME_PROFILE_BINS = 50
SR_ROLLING_WINDOW_DAYS = 14  # Rolling window for S/R recalculation

# ATR parameters
ATR_PERIOD = 14
ATR_VOLATILITY_REFERENCE = "BTC"  # Reference asset for volatility scaling

# Sweep & Reclaim parameters
SWEEP_INVALIDATION_ATR = 1.5  # Sweep failed if price goes > 1.5x ATR beyond zone
SWEEP_MIN_DEPTH_ATR = 0.15    # Minimum sweep depth to filter noise
SWEEP_MIN_ZONE_STRENGTH = 1   # Minimum zone touches (1 = no filter)
SWEEP_STOP_BUFFER_ATR = 0.5   # Stop = sweep_wick - (0.5 * ATR * vol_ratio) — wider for safety
SWEEP_MIN_STOP_DISTANCE_ATR = 0.5  # Skip trade if stop distance < 0.5 ATR (prevents micro-stops)
SWEEP_TARGET_R = 2.0          # Fallback take-profit at 2R (if no FVG available)
SWEEP_TRAIL_BE_R = 1.0        # Trail stop to break-even after this R-multiple

# Range filter — skip trades when market is too choppy
RANGE_FILTER_ENABLED = True        # Enable range detection filter
RANGE_FILTER_LOOKBACK = 20         # Candles to measure efficiency over
RANGE_FILTER_MIN_EFFICIENCY = 0.20 # Kaufman ER baseline (scaled by vol_ratio)

# CVD (Cumulative Volume Delta) parameters
CVD_LOOKBACK_CANDLES = 10     # Window for CVD calculation
CVD_MIN_DELTA_RATIO = 0.1     # Minimum net delta as fraction of total volume

# Fair Value Gap parameters
FVG_MIN_SIZE_ATR = 0.3        # Minimum FVG size as fraction of ATR
FVG_MAX_AGE_CANDLES = 50      # Ignore FVGs older than this
FVG_FILL_THRESHOLD = 0.5      # FVG considered filled if 50% covered

# Funding rate
EXTREME_FUNDING_RATE = 0.001  # 0.1% per 8h is extreme
FUNDING_BOOST_SCORE = 30      # Score for extreme funding in our direction
FUNDING_VETO_SCORE = -40      # Penalty for extreme funding against our direction

# Backtestable assets
BACKTEST_ASSETS = ["BTC", "ETH", "SOL", "DOGE"]

# Backtesting
BACKTEST_FEE_RATE = 0.00035  # 0.035% taker fee on Hyperliquid
BACKTEST_SLIPPAGE_BPS = 5    # 5 basis points estimated slippage

# Cooldown
COOLDOWN_BASE_CANDLES = 3     # 3 candles base cooldown
COOLDOWN_BASE_SECONDS = 3600  # 1h candle as base

# Paths
BASE_DIR = Path(__file__).parent
STATE_DB = BASE_DIR / "state" / "trades.db"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_TRADE", "")
TELEGRAM_CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS_TRADE", "").split(",") if c.strip()]

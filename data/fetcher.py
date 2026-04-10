"""Historical candle data fetcher for Hyperliquid.

Fetches OHLCV candles via the Hyperliquid info endpoint.
Supports pagination for fetching more than the 5000 candle limit.
Caches data locally as parquet files to avoid repeated API calls.

API: POST https://api.hyperliquid.xyz/info
Body: {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": ms, "endTime": ms}}

Response fields:
  t: open time (ms), T: close time (ms), o: open, h: high, l: low, c: close,
  v: volume, n: number of trades, s: symbol, i: interval
"""

import time
import requests
import pandas as pd
from pathlib import Path

import config

CACHE_DIR = config.BASE_DIR / "data" / "cache"
API_URL_MAINNET = "https://api.hyperliquid.xyz/info"
API_URL_TESTNET = "https://api.hyperliquid-testnet.xyz/info"
# Module-level flag, set by PortfolioManager at startup
_use_testnet = False
MAX_CANDLES_PER_REQUEST = 5000


def set_testnet(testnet: bool):
    """Set whether fetcher uses testnet or mainnet API."""
    global _use_testnet
    _use_testnet = testnet


def _get_api_url() -> str:
    return API_URL_TESTNET if _use_testnet else API_URL_MAINNET

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch candles from Hyperliquid API. Single request, max 5000 candles."""
    resp = requests.post(_get_api_url(), json={
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_candles_paginated(coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch candles with automatic pagination to bypass the 5000 candle limit."""
    interval_ms = INTERVAL_MS.get(interval)
    if not interval_ms:
        raise ValueError(f"Unknown interval: {interval}. Use one of: {list(INTERVAL_MS.keys())}")

    all_candles = []
    current_start = start_ms

    while current_start < end_ms:
        # Fetch a batch
        batch = fetch_candles(coin, interval, current_start, end_ms)
        if not batch:
            break

        all_candles.extend(batch)

        # Move start to after the last candle
        last_time = max(c["t"] for c in batch)
        current_start = last_time + interval_ms

        # Rate limit: be nice to the API
        if current_start < end_ms:
            time.sleep(0.5)

    if not all_candles:
        return pd.DataFrame()

    df = _candles_to_dataframe(all_candles)
    # Remove duplicates (overlap between batches)
    df = df.drop_duplicates(subset=["timestamp"], keep="last")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    """Convert raw API candle data to a clean DataFrame."""
    df = pd.DataFrame(candles)
    df = df.rename(columns={
        "t": "timestamp",
        "T": "close_time",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "n": "trades",
        "s": "symbol",
        "i": "interval",
    })
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype(int)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def fetch_and_cache(coin: str, interval: str, days: int = 90) -> pd.DataFrame:
    """Fetch candles and cache locally as parquet. Returns cached data if fresh."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{coin}_{interval}_{days}d.parquet"

    # Use cache if less than 1 hour old
    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < 1:
            return pd.read_parquet(cache_file)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 86_400_000)

    print(f"Fetching {coin} {interval} candles ({days} days)...")
    df = fetch_candles_paginated(coin, interval, start_ms, end_ms)

    if not df.empty:
        df.to_parquet(cache_file, index=False)
        print(f"Cached {len(df)} candles to {cache_file.name}")

    return df


def fetch_funding_rates(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch historical funding rate data."""
    resp = requests.post(_get_api_url(), json={
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_ms,
        "endTime": end_ms,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if "fundingRate" in df.columns:
        df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    if "time" in df.columns:
        df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df

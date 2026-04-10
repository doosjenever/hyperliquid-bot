"""Dynamic universe builder — fetches top N assets by Open Interest.

Queries the Hyperliquid API (testnet or mainnet) for all perp assets,
ranks them by OI in USD, and returns the top N tradeable symbols.

Runs once at startup and every 24 hours via the PortfolioManager.
"""

import logging
import requests

import config

logger = logging.getLogger(__name__)

# Minimum 24h volume to be considered tradeable (filters dead markets)
MIN_24H_VOLUME_USD = 10_000


def fetch_top_assets(n: int = 25, testnet: bool = True) -> list[dict]:
    """Fetch top N assets by Open Interest from the exchange.

    Returns list of dicts: [{"name": "BTC", "oi_usd": 1234.5, "vol_24h": 567.8}, ...]
    Sorted by OI descending.
    """
    api_url = (
        "https://api.hyperliquid-testnet.xyz/info" if testnet
        else "https://api.hyperliquid.xyz/info"
    )

    resp = requests.post(api_url, json={"type": "metaAndAssetCtxs"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    meta_universe = data[0]["universe"]
    asset_ctxs = data[1]

    assets = []
    for u, c in zip(meta_universe, asset_ctxs):
        mark_px = float(c.get("markPx", 0))
        oi = float(c.get("openInterest", 0)) * mark_px
        vol = float(c.get("dayNtlVlm", 0))

        # Skip assets with zero price or negligible volume
        if mark_px <= 0:
            continue

        assets.append({
            "name": u["name"],
            "oi_usd": oi,
            "vol_24h": vol,
            "mark_price": mark_px,
            "sz_decimals": u["szDecimals"],
            "max_leverage": u.get("maxLeverage", 5),
        })

    # Sort by OI descending
    assets.sort(key=lambda x: x["oi_usd"], reverse=True)

    # Take top N, filter on minimum volume
    top = []
    for a in assets:
        if len(top) >= n:
            break
        if a["vol_24h"] >= MIN_24H_VOLUME_USD or a["oi_usd"] > 50_000:
            top.append(a)

    logger.info(
        f"Universe: {len(top)} assets from {len(assets)} total | "
        f"Top 3: {', '.join(a['name'] for a in top[:3])}"
    )

    return top


def get_top_symbols(n: int = 25, testnet: bool = True) -> list[str]:
    """Convenience: returns just the symbol names."""
    return [a["name"] for a in fetch_top_assets(n, testnet)]


def get_top_symbols_with_leverage(n: int = 25, testnet: bool = True) -> dict[str, int]:
    """Returns {symbol: max_leverage} for top N assets."""
    return {a["name"]: a["max_leverage"] for a in fetch_top_assets(n, testnet)}

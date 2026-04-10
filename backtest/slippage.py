"""Slippage and fee model for backtesting.

Models realistic execution costs including:
- Trading fees (taker/maker)
- Slippage based on order size and volatility
"""

import config


def calculate_execution_price(price: float, direction: str, volume_24h: float = 0) -> float:
    """Calculate the realistic execution price including slippage.

    For backtesting, we apply a fixed slippage in basis points.
    In live trading, this would use orderbook depth.
    """
    slippage_fraction = config.BACKTEST_SLIPPAGE_BPS / 10_000

    if direction == "long":
        return price * (1 + slippage_fraction)
    else:
        return price * (1 - slippage_fraction)


def calculate_fees(notional_value: float) -> float:
    """Calculate trading fees for a given notional value."""
    return notional_value * config.BACKTEST_FEE_RATE


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    position_size: float,
    direction: str,
) -> dict:
    """Calculate PnL for a backtested trade including fees and simulated slippage.

    Returns dict with gross_pnl, fees, net_pnl, return_pct.
    position_size is in coins (sized via risk_amount / stop_distance).
    """
    # Apply slippage to both entry and exit
    actual_entry = calculate_execution_price(entry_price, direction)
    exit_dir = "short" if direction == "long" else "long"
    actual_exit = calculate_execution_price(exit_price, exit_dir)

    notional = position_size * actual_entry

    if direction == "long":
        gross_pnl = (actual_exit - actual_entry) / actual_entry * notional
    else:
        gross_pnl = (actual_entry - actual_exit) / actual_entry * notional

    # Fees on both entry and exit
    total_fees = calculate_fees(notional) * 2

    net_pnl = gross_pnl - total_fees
    return_pct = net_pnl / (position_size * actual_entry) * 100 if position_size > 0 else 0

    return {
        "entry_price": actual_entry,
        "exit_price": actual_exit,
        "gross_pnl": gross_pnl,
        "fees": total_fees,
        "net_pnl": net_pnl,
        "return_pct": return_pct,
    }


def calculate_live_pnl(
    entry_price: float,
    exit_price: float,
    position_size: float,
    direction: str,
) -> dict:
    """Calculate PnL for a live trade using actual fill prices.

    Unlike calculate_pnl(), this does NOT apply simulated slippage —
    the entry and exit prices already include real exchange slippage.
    Only trading fees are deducted.
    """
    notional = position_size * entry_price

    if direction == "long":
        gross_pnl = (exit_price - entry_price) / entry_price * notional
    else:
        gross_pnl = (entry_price - exit_price) / entry_price * notional

    # Fees on both entry and exit
    total_fees = calculate_fees(notional) * 2

    net_pnl = gross_pnl - total_fees
    return_pct = net_pnl / (position_size * entry_price) * 100 if position_size > 0 else 0

    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_pnl": gross_pnl,
        "fees": total_fees,
        "net_pnl": net_pnl,
        "return_pct": return_pct,
    }

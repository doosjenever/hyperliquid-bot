"""Backtesting engine — Sweep & Reclaim Strategy v1.0.

Entry logic:
1. Monitor S/R zones for liquidity sweeps
2. After sweep: wait for reclaim candle (price-based window, not time-based)
3. Score confluence (CVD, funding, RSI, EMA, volume profile)
4. Single entry at reclaim close, stop under sweep wick
5. FVG as take-profit target, 2R fallback

Usage:
    from backtest.engine import Backtester
    bt = Backtester("BTC", leverage=40)
    results = bt.run(days=90)
    bt.print_summary(results)
"""

import pandas as pd
import numpy as np

import config
from data.fetcher import fetch_and_cache
from strategy.support_resistance import find_sr_zones
from strategy.volume_profile import calculate_volume_profile
from strategy.confluence import (
    calculate_rsi, calculate_atr, calculate_ema, calculate_mfi, calculate_cci,
    score_sweep_confluence, find_sweepable_zones,
)
from strategy.asset_profile import AssetProfile
from strategy.dca import SweepPosition
from strategy.sweep_reclaim import (
    detect_sweep, detect_reclaim, check_sweep_still_valid,
    calculate_cvd_proxy, cvd_confirms_direction,
    find_fair_value_gaps, find_fvg_target,
)
from backtest.slippage import calculate_pnl


class Backtester:
    def __init__(self, coin: str = "BTC", leverage: float = 40.0, equity: float = 10_000):
        self.coin = coin
        self.leverage = leverage
        self.initial_equity = equity
        self.equity = equity

    def run(self, days: int = 90, htf: str = "4h", ltf: str = "1h") -> dict:
        """Run the backtest with Sweep & Reclaim strategy."""
        print(f"=== Backtest: {self.coin} | {days} days | {self.leverage}x leverage ===")
        print(f"    Strategy: Sweep & Reclaim v1.0 | Min score: {config.MIN_CONFLUENCE_SCORE}")

        # Fetch data
        df_htf = fetch_and_cache(self.coin, htf, days=days)
        df_ltf = fetch_and_cache(self.coin, ltf, days=days)

        if df_htf.empty or df_ltf.empty:
            print("No data available!")
            return {"trades": [], "equity_curve": []}

        # Volume Profile from full HTF data
        vp = calculate_volume_profile(df_htf)
        print(f"Volume Profile POC: {vp['poc']:.2f}")

        # Calculate indicators on LTF
        rsi_ltf = calculate_rsi(df_ltf["close"], period=14)
        atr_ltf = calculate_atr(df_ltf, period=config.ATR_PERIOD)
        ema_ltf = calculate_ema(df_ltf["close"], period=200)

        # Build adaptive asset profile
        df_ref = fetch_and_cache("BTC", ltf, days=days) if self.coin != "BTC" else None
        profile = AssetProfile(self.coin, df_ltf, df_ref)
        print(f"Asset Profile: {profile.summary()}")

        # Walk through LTF candles
        trades = []
        equity_curve = [self.initial_equity]
        position: SweepPosition | None = None
        self.equity = self.initial_equity

        # Sweep state
        active_sweep: dict | None = None  # Current pending sweep waiting for reclaim

        # Cooldown
        cooldown_until_idx = -1
        vol_scale = max(1.0, profile.vol_ratio)
        ltf_seconds = 3600 if ltf == "1h" else 900
        cooldown_ltf_candles = int(
            (config.COOLDOWN_BASE_SECONDS * config.COOLDOWN_BASE_CANDLES * vol_scale) / ltf_seconds
        )

        # Rolling S/R state
        current_htf_zones = None
        last_sr_update_idx = -1
        rolling_window = config.SR_ROLLING_WINDOW_DAYS
        htf_hours = 4 if htf == "4h" else 24
        rolling_htf_candles = (rolling_window * 24) // htf_hours

        # Warmup
        warmup = max(config.ATR_PERIOD + 5, config.SR_LOOKBACK_CANDLES // 4)
        warmup = min(warmup, len(df_ltf) // 4)

        for i in range(warmup, len(df_ltf)):
            candle = df_ltf.iloc[i]
            price = candle["close"]
            high = candle["high"]
            low = candle["low"]
            candle_time = candle["datetime"]
            atr = atr_ltf.iloc[i] if i < len(atr_ltf) and not pd.isna(atr_ltf.iloc[i]) else None

            # --- Rolling S/R update ---
            htf_idx = self._find_htf_index(candle_time, df_htf)
            if htf_idx > last_sr_update_idx and htf_idx >= rolling_htf_candles:
                start_idx = max(0, htf_idx - rolling_htf_candles)
                df_htf_window = df_htf.iloc[start_idx:htf_idx + 1]
                current_htf_zones = find_sr_zones(df_htf_window, window=profile.swing_window)
                last_sr_update_idx = htf_idx

            if current_htf_zones is None:
                current_htf_zones = find_sr_zones(df_htf.iloc[:max(20, warmup // 4)], window=profile.swing_window)

            # --- Cooldown check ---
            if cooldown_until_idx >= i:
                equity_curve.append(self.equity)
                continue

            # --- If in position: check exits ---
            if position is not None and position.is_full:
                exit_price, exit_reason = self._check_exit(
                    position, price, high, low,
                    rsi_ltf.iloc[i] if i < len(rsi_ltf) else None
                )

                if exit_price is not None:
                    pnl = calculate_pnl(
                        position.avg_entry_price, exit_price,
                        position.total_size, position.direction,
                    )
                    self.equity += pnl["net_pnl"]
                    trades.append({
                        "entry_time": position.first_entry_time,
                        "exit_time": candle_time,
                        "direction": position.direction,
                        "entry_price": position.avg_entry_price,
                        "exit_price": exit_price,
                        "stop_loss": position.stop_loss,
                        "take_profit": position.take_profit,
                        "exit_reason": exit_reason,
                        "mode": "sweep_reclaim",
                        "sweep_depth_atr": active_sweep["sweep_depth_atr"] if active_sweep else 0,
                        "fvg_target": position.fvg_target,
                        **pnl,
                    })
                    position = None
                    active_sweep = None
                    cooldown_until_idx = i + cooldown_ltf_candles
                    equity_curve.append(self.equity)
                    continue

                # Trail stop to break-even
                new_stop = position.should_trail_stop(price)
                if new_stop is not None:
                    position.stop_loss = new_stop

                equity_curve.append(self.equity)
                continue

            # --- Not in position ---
            if position is None and atr is not None:

                # Range filter: skip when market is choppy (Kaufman Efficiency Ratio)
                # Threshold scales inversely with vol_ratio: BTC (1.0) = strict, alts = relaxed
                if config.RANGE_FILTER_ENABLED and i >= config.RANGE_FILTER_LOOKBACK:
                    lookback = config.RANGE_FILTER_LOOKBACK
                    net_move = abs(df_ltf.iloc[i]["close"] - df_ltf.iloc[i - lookback]["close"])
                    sum_moves = sum(
                        abs(df_ltf.iloc[j]["close"] - df_ltf.iloc[j - 1]["close"])
                        for j in range(i - lookback + 1, i + 1)
                    )
                    efficiency = net_move / sum_moves if sum_moves > 0 else 0
                    min_eff = config.RANGE_FILTER_MIN_EFFICIENCY / (profile.vol_ratio ** 2)
                    if efficiency < min_eff:
                        if active_sweep is not None:
                            active_sweep = None
                        equity_curve.append(self.equity)
                        continue

                # Check if we have an active sweep waiting for reclaim
                if active_sweep is not None:
                    # Check if sweep is still valid (price hasn't gone too deep)
                    if not check_sweep_still_valid(candle, active_sweep, atr):
                        active_sweep = None
                        cooldown_until_idx = i + cooldown_ltf_candles
                        equity_curve.append(self.equity)
                        continue

                    # Check for reclaim
                    reclaim = detect_reclaim(candle, active_sweep)
                    if reclaim:
                        # Calculate CVD
                        cvd = calculate_cvd_proxy(df_ltf, i)
                        cvd_ok = cvd_confirms_direction(cvd, active_sweep["direction"])

                        # Score confluence
                        rsi_val = rsi_ltf.iloc[i] if i < len(rsi_ltf) else None
                        ema_val = ema_ltf.iloc[i] if i < len(ema_ltf) else None

                        signal = score_sweep_confluence(
                            direction=active_sweep["direction"],
                            zone=active_sweep["zone"],
                            volume_profile=vp,
                            rsi_value=rsi_val,
                            ema_200=ema_val,
                            cvd_confirms=cvd_ok,
                            price=reclaim["entry_price"],
                            rsi_oversold=profile.rsi_oversold,
                            rsi_overbought=profile.rsi_overbought,
                        )

                        if signal["trade"]:
                            # Create position
                            risk_amount = self.equity * config.MAX_RISK_PER_TRADE
                            pos = SweepPosition(
                                direction=active_sweep["direction"],
                                zone=active_sweep["zone"],
                                atr=atr,
                                risk_amount=risk_amount,
                                sweep_wick=active_sweep["sweep_wick"],
                                vol_ratio=profile.vol_ratio,
                            )

                            # Check minimum stop distance (prevents micro-stops)
                            min_stop_dist = atr * config.SWEEP_MIN_STOP_DISTANCE_ATR
                            stop_dist = abs(reclaim["entry_price"] - pos.stop_loss)
                            if stop_dist >= min_stop_dist:
                                pos.execute_entry(reclaim["entry_price"], candle_time, self.leverage)
                                position = pos

                                # Find FVG target
                                fvgs = find_fair_value_gaps(df_ltf, i, atr)
                                fvg_target = find_fvg_target(fvgs, reclaim["entry_price"], active_sweep["direction"])
                                if fvg_target:
                                    position.set_fvg_target(fvg_target)
                            else:
                                active_sweep = None
                        else:
                            # Score too low — cooldown
                            active_sweep = None
                            cooldown_until_idx = i + cooldown_ltf_candles

                else:
                    # No active sweep — scan for new sweeps near S/R zones
                    candidates = find_sweepable_zones(price, current_htf_zones, atr)

                    for candidate in candidates:
                        sweep = detect_sweep(
                            {"high": high, "low": low, "close": price, "open": candle["open"]},
                            candidate["zone"],
                            candidate["direction"],
                            atr,
                        )
                        if sweep:
                            active_sweep = sweep
                            # Check if this same candle also reclaims (V-reversal)
                            reclaim = detect_reclaim(candle, sweep)
                            if reclaim:
                                cvd = calculate_cvd_proxy(df_ltf, i)
                                cvd_ok = cvd_confirms_direction(cvd, sweep["direction"])

                                rsi_val = rsi_ltf.iloc[i] if i < len(rsi_ltf) else None
                                ema_val = ema_ltf.iloc[i] if i < len(ema_ltf) else None

                                signal = score_sweep_confluence(
                                    direction=sweep["direction"],
                                    zone=sweep["zone"],
                                    volume_profile=vp,
                                    rsi_value=rsi_val,
                                    ema_200=ema_val,
                                    cvd_confirms=cvd_ok,
                                    price=reclaim["entry_price"],
                                    rsi_oversold=profile.rsi_oversold,
                                    rsi_overbought=profile.rsi_overbought,
                                )

                                if signal["trade"]:
                                    risk_amount = self.equity * config.MAX_RISK_PER_TRADE
                                    pos = SweepPosition(
                                        direction=sweep["direction"],
                                        zone=sweep["zone"],
                                        atr=atr,
                                        risk_amount=risk_amount,
                                        sweep_wick=sweep["sweep_wick"],
                                        vol_ratio=profile.vol_ratio,
                                    )

                                    min_stop_dist = atr * config.SWEEP_MIN_STOP_DISTANCE_ATR
                                    stop_dist = abs(reclaim["entry_price"] - pos.stop_loss)
                                    if stop_dist >= min_stop_dist:
                                        pos.execute_entry(reclaim["entry_price"], candle_time, self.leverage)
                                        position = pos

                                        fvgs = find_fair_value_gaps(df_ltf, i, atr)
                                        fvg_target = find_fvg_target(fvgs, reclaim["entry_price"], sweep["direction"])
                                        if fvg_target:
                                            position.set_fvg_target(fvg_target)
                                    else:
                                        active_sweep = None
                                else:
                                    active_sweep = None
                                    cooldown_until_idx = i + cooldown_ltf_candles
                            break  # Only track one sweep at a time

            equity_curve.append(self.equity)

        # Close any remaining position at last price
        if position is not None and position.is_full:
            last_price = df_ltf.iloc[-1]["close"]
            pnl = calculate_pnl(
                position.avg_entry_price, last_price,
                position.total_size, position.direction,
            )
            self.equity += pnl["net_pnl"]
            trades.append({
                "entry_time": position.first_entry_time,
                "exit_time": df_ltf.iloc[-1]["datetime"],
                "direction": position.direction,
                "entry_price": position.avg_entry_price,
                "exit_price": last_price,
                "exit_reason": "end_of_data",
                "mode": "sweep_reclaim",
                **pnl,
            })

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "final_equity": self.equity,
        }

    def _find_htf_index(self, ltf_time, df_htf: pd.DataFrame) -> int:
        """Find the latest HTF candle that closed before or at ltf_time."""
        htf_times = pd.to_datetime(df_htf["datetime"]).values
        ltf_ts = pd.Timestamp(ltf_time)
        if hasattr(ltf_ts, 'tz') and ltf_ts.tz is not None:
            ltf_ts = ltf_ts.tz_localize(None)
        htf_series = pd.Series(htf_times).dt.tz_localize(None).values
        idx = np.searchsorted(htf_series, ltf_ts.to_numpy(), side="right") - 1
        return max(0, int(idx))

    def _check_exit(self, pos: SweepPosition, price: float, high: float, low: float, rsi: float = None) -> tuple:
        """Check if position should be exited.

        Exit triggers (in priority order):
        1. Stop-loss (sweep wick based)
        2. Take-profit (FVG or 2R)
        3. RSI extreme exit (profit-taking)
        """
        if pos.direction == "long":
            if pos.check_stop_loss(high, low):
                return pos.stop_loss, "stop_loss"
            if pos.check_take_profit(high, low):
                return pos.take_profit, "take_profit"
            if rsi is not None and rsi > 75 and price > pos.avg_entry_price:
                return price, "rsi_exit"
        else:
            if pos.check_stop_loss(high, low):
                return pos.stop_loss, "stop_loss"
            if pos.check_take_profit(high, low):
                return pos.take_profit, "take_profit"
            if rsi is not None and rsi < 25 and price < pos.avg_entry_price:
                return price, "rsi_exit"

        return None, None

    def print_summary(self, results: dict):
        """Print a formatted backtest summary."""
        trades = results["trades"]
        if not trades:
            print("\nNo trades executed.")
            return

        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        total_pnl = sum(t["net_pnl"] for t in trades)
        total_fees = sum(t["fees"] for t in trades)

        equity_curve = results["equity_curve"]
        peak = equity_curve[0]
        max_dd = 0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        # Sharpe ratio
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        sharpe = 0
        if returns and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(365)

        # Profit factor
        gross_wins = sum(t["net_pnl"] for t in wins) if wins else 0
        gross_losses = abs(sum(t["net_pnl"] for t in losses)) if losses else 1
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        # Average R:R of winners
        avg_win = np.mean([t["net_pnl"] for t in wins]) if wins else 0
        avg_loss = abs(np.mean([t["net_pnl"] for t in losses])) if losses else 1

        print(f"\n{'='*60}")
        print(f"BACKTEST: {self.coin} | Sweep & Reclaim v1.0")
        print(f"{'='*60}")
        print(f"Startkapitaal:      ${self.initial_equity:,.2f}")
        print(f"Eindkapitaal:       ${results['final_equity']:,.2f}")
        print(f"Totaal PnL:         ${total_pnl:,.2f} ({total_pnl/self.initial_equity*100:+.1f}%)")
        print(f"Totaal fees:        ${total_fees:,.2f}")
        print(f"{'='*60}")
        print(f"Aantal trades:      {len(trades)}")
        print(f"Winstgevend:        {len(wins)} ({len(wins)/len(trades)*100:.0f}%)")
        print(f"Verliesgevend:      {len(losses)} ({len(losses)/len(trades)*100:.0f}%)")
        if wins:
            print(f"Gem. winst:         ${avg_win:,.2f}")
        if losses:
            print(f"Gem. verlies:       ${abs(np.mean([t['net_pnl'] for t in losses])):,.2f}")
        print(f"Profit Factor:      {profit_factor:.2f}")
        print(f"Win/Loss ratio:     {avg_win/avg_loss:.2f}x" if avg_loss > 0 else "")
        print(f"{'='*60}")
        print(f"Max drawdown:       {max_dd*100:.1f}%")
        print(f"Sharpe ratio:       {sharpe:.2f}")
        print(f"{'='*60}")

        # Exit reasons breakdown
        reasons = {}
        for t in trades:
            r = t.get("exit_reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        print("\nExit redenen:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            pnl_for_reason = sum(t["net_pnl"] for t in trades if t.get("exit_reason") == reason)
            print(f"  {reason}: {count} (${pnl_for_reason:+,.2f})")

        # FVG target usage
        fvg_trades = [t for t in trades if t.get("fvg_target") is not None]
        if fvg_trades:
            print(f"\nFVG targets: {len(fvg_trades)}/{len(trades)} trades had FVG target")

        # Sweep depth stats
        depths = [t.get("sweep_depth_atr", 0) for t in trades if t.get("sweep_depth_atr", 0) > 0]
        if depths:
            print(f"Gem. sweep diepte:  {np.mean(depths):.2f}x ATR")

        # Trade details
        print(f"\n{'='*60}")
        print("Individuele trades:")
        print(f"{'='*60}")
        for t in trades:
            pnl_str = f"${t['net_pnl']:+,.2f}"
            dir_str = "LONG " if t["direction"] == "long" else "SHORT"
            exit_str = t.get("exit_reason", "?")
            fvg_str = " [FVG]" if t.get("fvg_target") else ""
            print(f"  {dir_str} @ {t['entry_price']:,.2f} -> {t['exit_price']:,.2f} | {exit_str}{fvg_str} | {pnl_str}")

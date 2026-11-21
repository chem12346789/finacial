"""Download market data and print the latest risk parity portfolio weights."""

from __future__ import annotations

import argparse
import logging
import sys
import warnings
from datetime import date, timedelta
from math import isfinite
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from broker.market_data import (
    CachedHistoryClient,
    DEFAULT_CACHE_PATH,
    ThrottledHistoryClient,
    fetch_daily_klines,
    get_default_quote_client,
)
from risk_parity.optimization import (
    PipelineConfig,
    build_forecast_covariance,
    clean_returns,
    forecast_garch_volatility,
    optimize_risk_budget,
    validate_config,
    walk_forward_backtest,
)


CFG = PipelineConfig(
    safe_target={
        "BIL": 0.0,
        # "GLD": 0.05,
    },
    risk_budget={
        "VOO": 0.25,
        "QQQ": 0.5,
        "VXUS": 0.25,
    },
)


def download_prices(tickers: Iterable[str], start: str, end: str) -> pd.DataFrame:
    """Download forward-adjusted daily close prices from Futu."""
    tickers = list(dict.fromkeys(tickers))
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if start_date > end_date:
        raise ValueError("start must not be after end")

    client = CachedHistoryClient(
        ThrottledHistoryClient(get_default_quote_client()),
        DEFAULT_CACHE_PATH,
    )
    price_data: dict[str, pd.Series] = {}
    print("\nData coverage before filling:", file=sys.stderr)
    for ticker in tickers:
        code = ticker if ticker.startswith("US.") else f"US.{ticker}"
        ticker_prices: dict[date, float] = {}
        # Chunks must stay within Futu's 370-bar page (~533 sessions of 252);
        # a longer chunk is silently truncated and history before it is lost.
        chunk_days = 360 if (end_date - start_date).days + 1 <= 360 else 533
        if chunk_days > 360:
            print(f"  {ticker}: {chunk_days}-day chunks", file=sys.stderr)
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(end_date, chunk_start + timedelta(days=chunk_days - 1))
            rows = fetch_daily_klines(
                client,
                [ticker],
                start=chunk_start,
                end=chunk_end,
                autype=1,
                num=370,
            )[ticker]
            if rows and min(rows) > chunk_start and len(rows) >= 370:
                raise ValueError(
                    f"Futu truncated {code} at 370 bars for chunk "
                    f"{chunk_start}..{chunk_end}; refusing incomplete history"
                )
            for day, bar in rows.items():
                try:
                    ticker_prices[day] = float(bar["close"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid Futu K-line row for {code}") from exc
            chunk_start = chunk_end + timedelta(days=1)

        if not ticker_prices:
            raise ValueError(f"No valid Futu price data returned for {code}")
        series = pd.Series(ticker_prices, dtype=float).sort_index()
        price_data[ticker] = series
        print(
            f"  {ticker}: {series.index[0]} to {series.index[-1]} "
            f"({len(series)} points)", file=sys.stderr,
        )

    prices = pd.DataFrame(price_data)
    prices.index = pd.to_datetime(prices.index)
    prices = prices.sort_index().ffill().dropna(axis=0, how="all")
    if prices.empty:
        raise ValueError("Price frame is empty after cleanup.")

    earliest_start = prices.apply(lambda col: col.first_valid_index()).max()
    prices = prices.loc[earliest_start:]
    print(
        f"\nFinal price data: {len(prices)} rows from "
        f"{str(prices.index[0])[:10]} to {str(prices.index[-1])[:10]}",
        file=sys.stderr,
    )
    return prices


def print_latest_weights(
    weights: pd.Series, *, rebalance_date: pd.Timestamp | None = None,
) -> None:
    """Print the latest portfolio weights and their rebalance date when supplied."""
    if rebalance_date is not None:
        print(f"\nRebalance date: {rebalance_date:%Y-%m-%d}")
    print("\nLatest weights")
    preferred_order = ("QQQ", "BIL", "VOO", "VXUS")
    ordered_assets = [
        asset for asset in preferred_order if asset in weights.index
    ] + sorted(asset for asset in weights.index if asset not in preferred_order)
    print(f"{'Asset':<12} {'Weight':>12}")
    print("-" * 25)
    for asset, weight in weights.loc[ordered_assets].items():
        print(f"{asset:<12} {weight:>12.4%}")
    if "expected_annual_return" in weights.attrs:
        attrs = weights.attrs
        print("\nRisky basket diagnostics (before cash allocation; annualized)")
        print(f"Expected return (historical mean): {attrs['expected_annual_return']:.2%}")
        print(f"Expected return (3-SE downside): {attrs['expected_annual_return_3se']:.2%}")
        print(f"\n{'Volatility estimate':<24} {'Historical':>14} {'Forecast':>14}")
        print("-" * 54)
        print(
            f"{'Portfolio volatility':<24} "
            f"{attrs['historical_annualized_volatility']:>14.2%} "
            f"{attrs['annualized_volatility']:>14.2%}"
        )
        print(
            f"\n{'Benchmark':<24} {'Annual rate':>12} {'Excess return':>14} "
            f"{'Kelly 1/2 hist.':>16} {'Kelly 1/2 fcst.':>16}"
        )
        print("-" * 86)
        print(
            f"{'BIL':<24} {attrs['bil_annual_rate']:>12.2%} "
            f"{attrs['annualized_excess_return']:>14.2%} "
            f"{attrs['historical_kelly_half']:>16.4f} "
            f"{attrs['kelly_half']:>16.4f}"
        )


def _kelly_scenarios(
    returns: pd.DataFrame,
    weights: pd.Series,
    risk_free_returns: pd.Series,
    *,
    annualized_variance: float | None = None,
) -> np.ndarray:
    portfolio_returns = returns[weights.index].dot(weights)
    if annualized_variance is not None:
        historical_variance = portfolio_returns.var(ddof=1) * 252.0
        if not all(
            isfinite(variance) and variance > 0
            for variance in (annualized_variance, historical_variance)
        ):
            raise ValueError("Kelly factor requires finite, positive portfolio variance")
        # Preserve the sample mean and shape while matching forecast volatility.
        mean = portfolio_returns.mean()
        portfolio_returns = mean + (portfolio_returns - mean) * (
            annualized_variance / historical_variance
        ) ** 0.5

    benchmark = risk_free_returns.reindex(portfolio_returns.index).to_numpy()
    if not np.isfinite(benchmark).all() or (benchmark <= -1.0).any():
        raise ValueError("Kelly factor requires aligned, finite benchmark returns above -100%")
    # Factor out benchmark wealth from the daily gross return of the funded position.
    scenarios = (portfolio_returns.to_numpy() - benchmark) / (1.0 + benchmark)
    if not scenarios.size or not np.isfinite(scenarios).all():
        raise ValueError("Kelly factor requires non-empty, finite return scenarios")
    return scenarios


def calculate_half_kelly_position(
    returns: pd.DataFrame,
    weights: pd.Series,
    risk_free_returns: pd.Series,
    *,
    annualized_variance: float | None = None,
) -> float:
    """Return the signed half-Kelly position, capped at one full allocation."""
    scenario_values = _kelly_scenarios(
        returns, weights, risk_free_returns, annualized_variance=annualized_variance,
    )
    edge = scenario_values.mean()
    if edge == 0.0:
        return 0.0
    direction = 1.0 if edge > 0.0 else -1.0
    scenario_values *= direction
    worst_loss = -scenario_values.min()
    if worst_loss <= 0.0:
        return direction

    if (
        1.0 + 2.0 * scenario_values.min() > 0.0
        and (
            scenario_values / (1.0 + 2.0 * scenario_values)
        ).sum() >= 0.0
    ):
        return direction

    low, high = 0.0, min(2.0, 1.0 / worst_loss)
    for _ in range(53):
        mid = (low + high) / 2.0
        if (scenario_values / (1.0 + mid * scenario_values)).sum() > 0.0:
            low = mid
        else:
            high = mid
    return float(direction * min(1.0, 0.5 * ((low + high) / 2.0)))


def latest_weights(
    prices: pd.DataFrame,
    cfg: PipelineConfig,
) -> pd.Series:
    """Compute target weights from the latest return window, without backtesting."""
    returns = clean_returns(prices.pct_change(fill_method=None).dropna())
    window = clean_returns(returns.iloc[-cfg.lookback_window:][cfg.risk_assets])
    if len(window) < int(cfg.lookback_window * 0.8):
        raise ValueError("Insufficient return history for the latest weights")
    vols = forecast_garch_volatility(window, horizon=cfg.garch_horizon)
    cov = build_forecast_covariance(window, vols)
    risk_weights = optimize_risk_budget(cov, asset_names=cfg.risk_assets, risk_budget=cfg.risk_budget)
    risk_weights = risk_weights.reindex(cfg.risk_assets).fillna(0.0)
    if risk_weights.sum() <= 0:
        raise ValueError("Risk parity optimization produced no positive weights")
    risk_weights = risk_weights / risk_weights.sum()
    if "BIL" not in returns.columns:
        raise ValueError("BIL prices are required for the risk-free benchmark")
    risk_free_returns = returns.loc[window.index, "BIL"]
    portfolio_returns = window.dot(risk_weights)
    excess_returns = portfolio_returns - risk_free_returns
    forecast_variance = float(
        risk_weights.dot(cov.loc[risk_weights.index, risk_weights.index].dot(risk_weights))
    )
    kelly_half = calculate_half_kelly_position(
        window, risk_weights, risk_free_returns,
        annualized_variance=forecast_variance,
    )

    weights = pd.Series(0.0, index=returns.columns, dtype=float)
    weights.loc[cfg.risk_assets] = (1.0 - sum(cfg.safe_target.values())) * risk_weights
    weights.loc[cfg.safe_assets] = pd.Series(cfg.safe_target, dtype=float)
    weights.attrs["expected_annual_return"] = portfolio_returns.mean() * 252.0
    historical_daily_volatility = portfolio_returns.std(ddof=1)
    weights.attrs["expected_annual_return_3se"] = (
        portfolio_returns.mean()
        - 3.0 * historical_daily_volatility / np.sqrt(len(portfolio_returns))
    ) * 252.0
    weights.attrs["annualized_volatility"] = forecast_variance ** 0.5
    weights.attrs["historical_annualized_volatility"] = historical_daily_volatility * (252.0 ** 0.5)
    weights.attrs["annualized_excess_return"] = excess_returns.mean() * 252.0
    weights.attrs["kelly_half"] = kelly_half
    weights.attrs["historical_kelly_half"] = calculate_half_kelly_position(
        window, risk_weights, risk_free_returns,
    )
    weights.attrs["bil_annual_rate"] = risk_free_returns.mean() * 252.0
    return weights


def main(argv: Sequence[str] | None = None) -> tuple[pd.Series, pd.DataFrame]:
    """Print the latest weights; run the full walk-forward backtest only on request."""
    warnings.filterwarnings("ignore")
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backtest", action="store_true",
        help="run the full walk-forward backtest instead of only the latest rebalance",
    )
    args = parser.parse_args(argv)
    cfg = CFG

    validate_config(cfg)

    tickers = list(dict.fromkeys(cfg.risk_assets + cfg.safe_assets + ["BIL"]))
    analysis_start = pd.to_datetime(cfg.end_date if not args.backtest else cfg.start_date)
    fetch_start = (analysis_start - pd.offsets.BDay(cfg.lookback_window)).strftime("%Y-%m-%d")
    prices = download_prices(tickers, fetch_start, cfg.end_date)
    if cfg.verbose:
        print(
            f"Downloaded {len(prices)} rows from {str(prices.index[0])[:10]} "
            f"to {str(prices.index[-1])[:10]}", file=sys.stderr,
        )

    if not args.backtest:
        weights = latest_weights(prices, cfg)
        print_latest_weights(weights, rebalance_date=pd.Timestamp(prices.index[-1]))
        return pd.Series(dtype=float), weights.to_frame().T

    portfolio_returns, weight_history = walk_forward_backtest(prices, cfg=cfg)
    if weight_history.empty:
        print("No portfolio weights were produced by the backtest.", file=sys.stderr)
        return portfolio_returns, weight_history

    print_latest_weights(
        weight_history.iloc[-1], rebalance_date=pd.Timestamp(weight_history.index[-1]),
    )
    return portfolio_returns, weight_history


if __name__ == "__main__":
    main()

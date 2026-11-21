"""Calculate equal-risk weights for the top five momentum candidates."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import date, timedelta
import json
from pathlib import Path
import sys
from typing import Sequence

import numpy as np
import pandas as pd

from momentum_core import load_prices
from ouput.momentum import DEFAULT_MOMENTUM_PATH
from ouput.risk_parity import format_risk_parity


def validate_momentum_date(
    momentum: dict, *, max_age_days: int = 7, today: date | None = None,
) -> date:
    """Check the data date, not the file modification or generation time."""
    if max_age_days < 0:
        raise ValueError("--max-age-days must be non-negative")
    as_of = date.fromisoformat(momentum["as_of"])
    age = ((today if today is not None else date.today()) - as_of).days
    if age < 0:
        raise ValueError(f"Momentum as_of {as_of} is in the future")
    if age > max_age_days:
        raise ValueError(
            f"Momentum as_of {as_of} is {age} days old; "
            f"maximum age is {max_age_days} days. Run momentum.py again."
        )
    return as_of


def select_candidates(momentum: dict) -> list[dict]:
    """Select ranks 1 through 5, independent of JSON array ordering."""
    candidates = sorted(momentum["candidates"], key=lambda row: row["rank"])[:5]
    if [row["rank"] for row in candidates] != list(range(1, 6)):
        raise ValueError("Momentum results must contain ranks 1 through 5")
    symbols = [row["symbol"] for row in candidates]
    if len(set(symbols)) != 5 or any(not isinstance(s, str) or not s for s in symbols):
        raise ValueError("The top five momentum symbols must be distinct nonempty strings")
    return candidates


def fixed_stop_fraction(annualized_volatility: float) -> float:
    """Illustrative two-sigma, five-session distance; not a calibrated loss cap."""
    if not np.isfinite(annualized_volatility) or annualized_volatility <= 0:
        raise ValueError("Stock annualized volatility must be finite and positive")
    return float(2 * annualized_volatility * np.sqrt(5 / 252))


def calculate_holding_stop_risk(symbols: list[str], as_of: date, lookback: int) -> dict:
    """Forecast held-stock risk for reporting, separately from candidate selection."""
    from core_asset_risk_parity import download_prices
    from risk_parity.optimization import forecast_garch_volatility

    if not symbols:
        return {}
    prices = download_prices(
        symbols, (as_of - timedelta(days=lookback * 2 + 30)).isoformat(), as_of.isoformat(),
    )
    selected = prices.loc[prices.index <= pd.Timestamp(as_of), symbols]
    if selected.index.has_duplicates or not np.isfinite(selected.to_numpy()).all() or (selected <= 0).any().any():
        raise ValueError("Holding prices must be finite, positive and have unique dates")
    returns = selected.sort_index().pct_change(fill_method=None).dropna().tail(lookback)
    if len(returns) < lookback:
        raise ValueError(f"Need {lookback + 1} price sessions for held-stock stop estimates")
    with redirect_stdout(sys.stderr):
        vols = forecast_garch_volatility(returns, horizon=7)
    return {
        symbol: {
            "annualized_volatility": float(vols[symbol]),
            "stop_fraction": fixed_stop_fraction(float(vols[symbol])),
            "as_of": as_of.isoformat(),
        }
        for symbol in symbols
    }


def calculate_weights(
    momentum: dict, prices: pd.DataFrame, *, lookback: int = 63,
) -> dict:
    """Use the latest common return window, without a safe-asset allocation."""
    from risk_parity.optimization import (
        build_forecast_covariance,
        forecast_garch_volatility,
        optimize_risk_budget,
    )

    if lookback < 2:
        raise ValueError("lookback must be at least 2 return sessions")
    candidates = select_candidates(momentum)
    symbols = [row["symbol"] for row in candidates]
    as_of = date.fromisoformat(momentum["as_of"])
    missing = set(symbols) - set(prices.columns)
    if missing:
        raise ValueError(f"Missing prices for: {', '.join(sorted(missing))}")
    selected = prices.loc[:, symbols].copy()
    selected.index = pd.to_datetime(selected.index)
    selected = selected.loc[selected.index <= pd.Timestamp(as_of)].sort_index()
    if selected.index.has_duplicates:
        raise ValueError("Duplicate price dates")
    if not np.isfinite(selected.to_numpy()).all() or (selected <= 0).any().any():
        raise ValueError("Selected prices must be finite, positive and aligned on common dates")
    returns = selected.pct_change(fill_method=None).dropna().tail(lookback)
    if len(returns) < lookback:
        raise ValueError(
            f"Need {lookback + 1} common price sessions on or before {as_of}; "
            f"found {len(selected)}. Supply more history or reduce --lookback."
        )
    if (returns.std() <= 0).any():
        raise ValueError("Each selected stock must have nonzero return volatility")
    # Keep solver diagnostics out of machine-readable stdout.
    with redirect_stdout(sys.stderr):
        vols = forecast_garch_volatility(returns, horizon=7)
        covariance = build_forecast_covariance(returns, vols)
        weights = optimize_risk_budget(covariance, symbols)
    weights = weights.reindex(symbols)
    if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() <= 0:
        raise ValueError("Risk parity optimization returned invalid weights")
    weights = weights / weights.sum()
    marginal = covariance.loc[symbols, symbols] @ weights
    variance = float(weights @ marginal)
    if not np.isfinite(variance) or variance <= 0:
        raise ValueError("Risk parity portfolio variance must be finite and positive")
    contributions = weights * marginal / variance
    return {
        "as_of": as_of.isoformat(),
        "method": "equal risk; GARCH + Ledoit-Wolf covariance",
        "lookback": lookback,
        "start_date": selected.index[-len(returns) - 1].date().isoformat(),
        "end_date": returns.index[-1].date().isoformat(),
        "annualized_volatility": float(np.sqrt(variance)),
        "weights": [
            {
                "rank": row["rank"], "symbol": row["symbol"],
                "weight": float(weights[row["symbol"]]),
                "risk_budget": 0.2,
                "risk_contribution": float(contributions[row["symbol"]]),
                "annualized_volatility": float(np.sqrt(covariance.loc[row["symbol"], row["symbol"]])),
                "fixed_stop_fraction": fixed_stop_fraction(
                    float(np.sqrt(covariance.loc[row["symbol"], row["symbol"]])),
                ),
            }
            for row in candidates
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "momentum_json", type=Path, nargs="?", default=DEFAULT_MOMENTUM_PATH,
        help="momentum JSON (default: project ouput/momentum.json)",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=7,
        help="maximum age of momentum as_of in calendar days (default: 7)",
    )
    parser.add_argument(
        "--prices", type=Path,
        help="offline adjusted-close CSV from momentum.py --save-prices; otherwise fetch Futu",
    )
    parser.add_argument("--lookback", type=int, default=63, help="return sessions (default: 63)")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    if args.lookback < 2:
        parser.error("--lookback must be at least 2")
    if args.max_age_days < 0:
        parser.error("--max-age-days must be non-negative")
    try:
        with args.momentum_json.open(encoding="utf-8") as handle:
            momentum = json.load(handle)
        as_of = validate_momentum_date(momentum, max_age_days=args.max_age_days)
        symbols = [row["symbol"] for row in select_candidates(momentum)]
        if args.prices is not None:
            prices = pd.DataFrame(load_prices(args.prices))
            prices = prices.dropna(subset=[s for s in symbols if s in prices.columns])
        else:
            from core_asset_risk_parity import download_prices

            # Late-listed candidates need roughly 3 calendar days per trading
            # session; refetch with a wider window instead of failing outright.
            days = args.lookback * 2 + 30
            previous = -1
            while True:
                start = as_of - timedelta(days=days)
                prices = download_prices(symbols, start.isoformat(), as_of.isoformat())
                if len(prices) >= args.lookback + 1 or days > args.lookback * 5:
                    break
                # Stop when history stops growing: the limit is listing age or
                # delisting, not the window, so further refetches are pointless.
                if len(prices) == previous:
                    break
                previous = len(prices)
                days = round(days * 1.5)
        result = calculate_weights(momentum, prices, lookback=args.lookback)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.error(str(exc))
    print(format_risk_parity(result, output_format=args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

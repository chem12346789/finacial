"""Risk-parity calculation diagnostics and covariance validation tests."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import play_money_asset_risk_parity as play
from risk_parity import optimization


class RiskCalculationOutputTest(unittest.TestCase):
    def setUp(self):
        self.symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        self.momentum = {
            "as_of": "2026-10-20",
            "candidates": [
                {"rank": rank, "symbol": symbol}
                for rank, symbol in enumerate(self.symbols, 1)
            ],
        }
        days = pd.bdate_range(end="2026-10-20", periods=6)
        steps = np.arange(6, dtype=float)
        self.prices = pd.DataFrame(
            {
                symbol: 100 * np.cumprod(
                    1 + 0.01 * np.sin(steps * (rank + 1) + rank)
                )
                for rank, symbol in enumerate(self.symbols)
            },
            index=days,
        )
        self.covariance = pd.DataFrame(
            np.eye(5), index=self.symbols, columns=self.symbols,
        )

    def test_solver_diagnostics_go_to_stderr_and_weights_follow_symbol_order(self):
        def forecast(*args, **kwargs):
            print("forecast diagnostics")
            return object()

        def covariance(*args, **kwargs):
            print("covariance diagnostics")
            return self.covariance

        def optimize(*args, **kwargs):
            print("optimizer diagnostics")
            return pd.Series(
                [5, 4, 3, 2, 1], index=list(reversed(self.symbols)),
            )

        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(optimization, "forecast_garch_volatility", side_effect=forecast),
            patch.object(optimization, "build_forecast_covariance", side_effect=covariance),
            patch.object(optimization, "optimize_risk_budget", side_effect=optimize),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = play.calculate_weights(self.momentum, self.prices, lookback=4)

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("forecast diagnostics", stderr.getvalue())
        self.assertIn("covariance diagnostics", stderr.getvalue())
        self.assertIn("optimizer diagnostics", stderr.getvalue())
        weights = [row["weight"] for row in result["weights"]]
        np.testing.assert_allclose(weights, np.arange(1, 6) / 15)
        self.assertEqual([row["symbol"] for row in result["weights"]], self.symbols)

    def test_zero_portfolio_variance_is_rejected(self):
        zero_covariance = pd.DataFrame(
            0.0, index=self.symbols, columns=self.symbols,
        )
        with (
            patch.object(optimization, "forecast_garch_volatility"),
            patch.object(
                optimization, "build_forecast_covariance",
                return_value=zero_covariance,
            ),
            patch.object(
                optimization, "optimize_risk_budget",
                return_value=pd.Series(1.0, index=self.symbols),
            ),
            self.assertRaisesRegex(ValueError, "variance must be finite and positive"),
        ):
            play.calculate_weights(self.momentum, self.prices, lookback=4)


if __name__ == "__main__":
    unittest.main()

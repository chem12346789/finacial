"""Offline checks for momentum-selected equal-risk allocations."""

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import date, timedelta
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import play_money_asset_risk_parity as play
from ouput.risk_parity import format_risk_parity
from ouput.momentum import save_momentum
from risk_parity import optimization


class PlayMoneyRiskParityTest(unittest.TestCase):
    def setUp(self):
        clock = patch("play_money_asset_risk_parity.date", wraps=date)
        self.clock = clock.start()
        self.clock.today.return_value = date(2026, 10, 20)
        self.addCleanup(clock.stop)
        self.symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        self.momentum = {
            "as_of": "2026-10-15",
            "candidates": [
                {"rank": i, "symbol": symbol}
                for i, symbol in enumerate(self.symbols + ["FFF"], 1)
            ][::-1],
        }
        days = pd.bdate_range(end="2026-10-15", periods=61)
        steps = np.arange(61, dtype=float)
        self.prices = pd.DataFrame({
            symbol: 100 * np.cumprod(1 + 0.01 * np.sin(steps * (i + 1) + i))
            for i, symbol in enumerate(self.symbols)
        }, index=days)
        self.covariance = pd.DataFrame(
            np.diag(np.square(np.arange(1, 6) * 0.1)),
            index=self.symbols, columns=self.symbols,
        )

    def calculate(self, prices=None, lookback=20):
        with (
            patch.object(optimization, "forecast_garch_volatility") as forecast,
            patch.object(optimization, "build_forecast_covariance",
                         return_value=self.covariance),
        ):
            result = play.calculate_weights(
                self.momentum, self.prices if prices is None else prices,
                lookback=lookback,
            )
        return result, forecast

    def test_default_window_requires_64_prices(self):
        steps = np.arange(64, dtype=float)
        prices = pd.DataFrame({
            symbol: 100 * np.cumprod(1 + 0.01 * np.sin(steps * (i + 1) + i))
            for i, symbol in enumerate(self.symbols)
        }, index=pd.bdate_range(end=self.momentum["as_of"], periods=64))
        with (
            patch.object(optimization, "forecast_garch_volatility") as forecast,
            patch.object(optimization, "build_forecast_covariance",
                         return_value=self.covariance),
        ):
            with self.assertRaisesRegex(ValueError, "Need 64 common price sessions.*found 63"):
                play.calculate_weights(self.momentum, prices.tail(63))
            result = play.calculate_weights(self.momentum, prices)
        self.assertEqual(result["lookback"], 63)
        self.assertEqual(len(forecast.call_args.args[0]), 63)
        self.assertEqual(forecast.call_args.kwargs, {"horizon": 7})

    def test_rank_selection_and_real_optimizer_equal_risk_not_equal_capital(self):
        before = copy.deepcopy(self.momentum)
        original_prices = self.prices.copy()
        result, forecast = self.calculate()
        self.assertEqual([row["symbol"] for row in result["weights"]], self.symbols)
        for row, volatility in zip(result["weights"], np.arange(1, 6) * 0.1):
            self.assertAlmostEqual(row["annualized_volatility"], volatility)
            self.assertAlmostEqual(row["fixed_stop_fraction"], 2 * volatility * np.sqrt(5 / 252))
        weights = np.array([row["weight"] for row in result["weights"]])
        expected = 1 / np.arange(1, 6)
        np.testing.assert_allclose(weights, expected / expected.sum(), atol=0.002)
        self.assertAlmostEqual(weights.sum(), 1.0)
        np.testing.assert_allclose(
            [row["risk_contribution"] for row in result["weights"]], 0.2, atol=0.002,
        )
        self.assertTrue((weights > 0).all())
        self.assertEqual(len(forecast.call_args.args[0]), 20)
        self.assertEqual(forecast.call_args.kwargs, {"horizon": 7})
        self.assertEqual(result["end_date"], self.momentum["as_of"])
        self.assertEqual(self.momentum, before)
        pd.testing.assert_frame_equal(self.prices, original_prices)

    def test_holding_stop_risk_uses_same_garch_forecast(self):
        with (
            patch("core_asset_risk_parity.download_prices", return_value=self.prices),
            patch.object(
                optimization, "forecast_garch_volatility",
                return_value=pd.Series(0.6, index=self.symbols),
            ) as forecast,
        ):
            risk = play.calculate_holding_stop_risk(
                self.symbols, date(2026, 10, 15), 20,
            )
        self.assertEqual(forecast.call_args.kwargs, {"horizon": 7})
        self.assertEqual(len(forecast.call_args.args[0]), 20)
        self.assertAlmostEqual(risk["AAA"]["stop_fraction"], 0.16903085094570333)
        self.assertEqual(risk["AAA"]["as_of"], "2026-10-15")

    def test_future_prices_and_unselected_stocks_do_not_change_weights(self):
        future = self.prices.copy()
        future.loc[pd.Timestamp("2026-10-16")] = 1e9
        future["FFF"] = np.nan
        expected, _ = self.calculate()
        actual, forecast = self.calculate(future)
        self.assertEqual(actual, expected)
        self.assertLessEqual(
            forecast.call_args.args[0].index.max(), pd.Timestamp("2026-10-15"),
        )

    def test_insufficient_candidates_or_duplicate_symbols_fail(self):
        for candidates in (
            self.momentum["candidates"][:4],
            [{"rank": i, "symbol": "AAA"} for i in range(1, 6)],
        ):
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                play.select_candidates({"candidates": candidates})

    def test_invalid_price_inputs_fail_without_silent_shortening(self):
        invalid = [
            self.prices.drop(columns="AAA"),
            self.prices.tail(20),
            self.prices.assign(AAA=0),
            self.prices.assign(AAA=np.inf),
            self.prices.assign(AAA=np.nan),
            self.prices.assign(AAA=100),
            pd.concat([self.prices, self.prices.tail(1)]),
        ]
        for prices in invalid:
            with self.subTest(shape=prices.shape), self.assertRaises(ValueError):
                play.calculate_weights(self.momentum, prices, lookback=20)

    def test_invalid_solver_weights_fail(self):
        for values in ([0] * 5, [np.nan] * 5, [-1, 1, 1, 1, 1]):
            with (
                self.subTest(values=values),
                patch.object(optimization, "optimize_risk_budget",
                             return_value=pd.Series(values, index=self.symbols)),
                self.assertRaisesRegex(ValueError, "invalid weights"),
            ):
                self.calculate()

    def test_full_forecasting_pipeline(self):
        result = play.calculate_weights(self.momentum, self.prices, lookback=60)
        rows = result["weights"]
        self.assertEqual(len(rows), 5)
        self.assertAlmostEqual(sum(row["weight"] for row in rows), 1.0)
        self.assertGreater(result["annualized_volatility"], 0)
        np.testing.assert_allclose(
            [row["risk_contribution"] for row in rows], 0.2, atol=0.002,
        )
        json.dumps(result, allow_nan=False)

    def test_formatter_json_table_and_invalid_format(self):
        result, _ = self.calculate()
        self.assertEqual(json.loads(format_risk_parity(result, output_format="json")), result)
        table = format_risk_parity(result)
        self.assertIn("Risk contribution", table)
        self.assertIn("AAA", table)
        self.assertIn("20.00%", table)
        with self.assertRaises(ValueError):
            format_risk_parity(result, output_format="csv")

    def test_cli_offline_table_json_and_default_lookback(self):
        result, _ = self.calculate()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "momentum.json"
            source.write_text(json.dumps(self.momentum), encoding="utf-8")
            csv = Path(directory) / "prices.csv"
            csv.write_text(
                "date,symbol,adjusted_close\n" + "".join(
                    f"{day.date()},{symbol},{self.prices.loc[day, symbol]}\n"
                    for day in self.prices.index for symbol in self.symbols
                ),
                encoding="utf-8",
            )
            for options in ([], ["--format", "json"]):
                stdout = StringIO()
                with (
                    patch.object(play, "calculate_weights", return_value=result) as calculate,
                    patch("core_asset_risk_parity.download_prices") as download,
                    redirect_stdout(stdout),
                ):
                    status = play.main([str(source), "--prices", str(csv), *options])
                self.assertEqual(status, 0)
                self.assertEqual(calculate.call_args.kwargs["lookback"], 63)
                download.assert_not_called()
                self.assertEqual(list(calculate.call_args.args[1].columns), self.symbols)
                if options:
                    self.assertEqual(json.loads(stdout.getvalue()), result)
                else:
                    self.assertIn("Risk parity", stdout.getvalue())
            stdout = StringIO()
            with redirect_stdout(stdout):
                status = play.main([
                    str(source), "--prices", str(csv),
                    "--lookback", "60", "--format", "json",
                ])
            self.assertEqual(status, 0)
            actual = json.loads(stdout.getvalue())
            self.assertEqual([row["symbol"] for row in actual["weights"]], self.symbols)
            self.assertAlmostEqual(sum(row["weight"] for row in actual["weights"]), 1.0)
            np.testing.assert_allclose(
                [row["risk_contribution"] for row in actual["weights"]], 0.2, atol=0.002,
            )

    def test_cli_online_requests_only_top_five_and_stops_at_as_of(self):
        result, _ = self.calculate()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "momentum.json"
            source.write_text(json.dumps(self.momentum), encoding="utf-8")
            with (
                patch("core_asset_risk_parity.download_prices",
                      return_value=self.prices) as download,
                patch.object(play, "calculate_weights", return_value=result),
                redirect_stdout(StringIO()),
            ):
                for lookback in (20, 252):
                    self.assertEqual(
                        play.main([str(source), "--lookback", str(lookback)]), 0,
                    )
                    if len(self.prices) >= lookback + 1:
                        download.assert_called_once_with(
                            self.symbols,
                            (date(2026, 10, 15) - timedelta(days=lookback * 2 + 30)).isoformat(),
                            "2026-10-15",
                        )
                    else:
                        # Refetches once with a 1.5x wider window, then stops
                        # because identical row counts mean listing history,
                        # not the window, is the limit.
                        self.assertEqual(download.call_count, 2)
                        starts = [
                            date.fromisoformat(call.args[1])
                            for call in download.call_args_list
                        ]
                        self.assertEqual(
                            starts, [date(2025, 4, 29), date(2024, 8, 5)],
                        )
                    download.reset_mock()

    def test_cli_online_keeps_widening_while_history_grows(self):
        result, _ = self.calculate()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "momentum.json"
            source.write_text(json.dumps(self.momentum), encoding="utf-8")
            days = pd.bdate_range(end="2026-10-15", periods=300)
            steps = np.arange(300, dtype=float)
            long_prices = pd.DataFrame({
                symbol: 100 * np.cumprod(1 + 0.01 * np.sin(steps * (i + 1) + i))
                for i, symbol in enumerate(self.symbols)
            }, index=days)
            downloads = [
                long_prices.tail(61), long_prices.tail(120), long_prices,
            ]
            with (
                patch("core_asset_risk_parity.download_prices",
                      side_effect=downloads) as download,
                patch.object(play, "calculate_weights", return_value=result),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(play.main([str(source), "--lookback", "252"]), 0)
            self.assertEqual(download.call_count, 3)
            starts = [
                date.fromisoformat(call.args[1])
                for call in download.call_args_list
            ]
            self.assertEqual(
                starts,
                [date(2025, 4, 29), date(2024, 8, 5), date(2023, 7, 1)],
            )

    def test_cli_errors_are_explicit_and_do_not_fetch(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "momentum.json"
            for contents in ('{"candidates": []}', "not json"):
                source.write_text(contents, encoding="utf-8")
                stdout, stderr = StringIO(), StringIO()
                with (
                    patch("core_asset_risk_parity.download_prices") as download,
                    redirect_stdout(stdout), redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    play.main([str(source)])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("error:", stderr.getvalue())
                download.assert_not_called()

    def test_date_validity_boundaries_use_data_date(self):
        for day in ("2026-10-20", "2026-10-13"):
            self.assertEqual(
                play.validate_momentum_date({"as_of": day}), date.fromisoformat(day),
            )
        for day in ("2026-10-12", "2026-10-21", "invalid"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                play.validate_momentum_date({"as_of": day})
        self.assertEqual(
            play.validate_momentum_date({"as_of": "2026-10-01"}, max_age_days=19),
            date(2026, 10, 1),
        )
        with self.assertRaises(ValueError):
            play.validate_momentum_date(self.momentum, max_age_days=-1)

    def test_default_file_is_loaded_and_stale_results_never_fetch(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "ouput" / "momentum.json"
            save_momentum(self.momentum, source)
            with (
                patch.object(play, "DEFAULT_MOMENTUM_PATH", source),
                patch("core_asset_risk_parity.download_prices",
                      return_value=self.prices) as download,
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(play.main(["--lookback", "20"]), 0)
                download.assert_called_once()
            for day in ("2026-10-12", "2026-10-21"):
                save_momentum({**self.momentum, "as_of": day}, source)
                with (
                    patch.object(play, "DEFAULT_MOMENTUM_PATH", source),
                    patch("core_asset_risk_parity.download_prices") as download,
                    redirect_stderr(StringIO()) as stderr,
                    redirect_stdout(StringIO()) as stdout,
                    self.assertRaises(SystemExit) as raised,
                ):
                    play.main([])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("error:", stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                download.assert_not_called()

    def test_bash_routes_order_options_without_duplicate_screening(self):
        script = Path(play.__file__).resolve().parent / "run_play_money.sh"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runner, log = root / "python", root / "calls"
            runner.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$@" > "$CALL_LOG"\n'
                'exit 7\n',
                encoding="utf-8",
            )
            runner.chmod(0o700)
            cases = [
                ([], []),
                (["--help"], ["--help"]),
                (["--account-id", "sim", "--ledger", "ledger with spaces.sqlite3"],
                 ["--account-id", "sim", "--ledger", "ledger with spaces.sqlite3"]),
                (["2026-10-20", "--ledger", "ledger.sqlite3"],
                 ["--as-of", "2026-10-20", "--ledger", "ledger.sqlite3"]),
                (["2026-10-20"], ["--as-of", "2026-10-20"]),
            ]
            for supplied, expected in cases:
                with self.subTest(args=supplied):
                    result = subprocess.run(
                        ["bash", str(script), *supplied], cwd=root,
                        env={**os.environ, "PYTHON": str(runner), "CALL_LOG": str(log)},
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 7)
                    self.assertEqual(
                        log.read_text(encoding="utf-8").splitlines(),
                        [str(script.parent / "play_money_orders.py"), *expected],
                    )
                    self.assertEqual(result.stdout, "")

    def test_bash_preserves_order_runner_exit_status(self):
        script = Path(play.__file__).resolve().parent / "run_play_money.sh"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runner, log = root / "python", root / "calls"
            runner.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >> "$CALL_LOG"\n'
                'printf "order output\\n"\n'
                'exit "$ORDER_STATUS"\n',
                encoding="utf-8",
            )
            runner.chmod(0o700)
            for status in (0, 1, 2, 3):
                log.write_text("", encoding="utf-8")
                completed = subprocess.run(
                    ["bash", str(script), "--ledger", "ledger.sqlite3"], cwd=root,
                    env={
                        **os.environ, "PYTHON": str(runner), "CALL_LOG": str(log),
                        "ORDER_STATUS": str(status),
                    },
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(completed.returncode, status)
                calls = log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(calls, [
                    f'{script.parent / "play_money_orders.py"} --ledger ledger.sqlite3',
                ])
                self.assertEqual(completed.stdout, "order output\n")


if __name__ == "__main__":
    unittest.main()

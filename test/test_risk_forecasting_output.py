"""Offline checks for the final-weight-only research entry point."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import core_asset_risk_parity as pipeline
from risk_parity import optimization as core


class RiskForecastingOutputTest(unittest.TestCase):
    def assert_two_session_kelly(
        self, factor, portfolio_returns, benchmark, annual_variance=None,
    ):
        if annual_variance is not None:
            mean = portfolio_returns.mean()
            scale = (annual_variance / (252 * portfolio_returns.var(ddof=1))) ** 0.5
            portfolio_returns = mean + scale * (portfolio_returns - mean)
        a, b = (portfolio_returns - benchmark) / (1.0 + benchmark)
        self.assertLess(a * b, 0)
        expected = max(-1.0, min(1.0, -(a + b) / (4 * a * b)))
        self.assertAlmostEqual(factor, expected)

    def test_bil_benchmark_is_independent_of_safe_bucket(self):
        cfg = pipeline.PipelineConfig(
            lookback_window=2, safe_target={"GLD": 0.05}, risk_budget={"VOO": 1.0},
        )
        prices = pd.DataFrame(
            {"VOO": [100.0, 99.0, 101.0], "BIL": [100.0, 100.01, 100.03],
             "GLD": [100.0, 105.0, 110.0]},
        )
        with (
            patch.object(pipeline, "forecast_garch_volatility"),
            patch.object(
                pipeline, "build_forecast_covariance",
                return_value=pd.DataFrame([[0.04]], index=["VOO"], columns=["VOO"]),
            ),
            patch.object(pipeline, "optimize_risk_budget", return_value=pd.Series({"VOO": 1.0})),
        ):
            weights = pipeline.latest_weights(prices, cfg)

        returns = prices.pct_change(fill_method=None).dropna()
        mu = returns["VOO"].mean() * 252
        variance = 0.04
        bil_rate = returns["BIL"].mean() * 252
        self.assertAlmostEqual(weights.attrs["bil_annual_rate"], bil_rate)
        self.assertAlmostEqual(weights.attrs["annualized_excess_return"], mu - bil_rate)
        self.assert_two_session_kelly(
            weights.attrs["kelly_half"], returns["VOO"], returns["BIL"], variance,
        )
        self.assertEqual(weights.to_dict(), {"VOO": 0.95, "BIL": 0.0, "GLD": 0.05})

    def test_latest_weights_prints_annualized_return_metrics(self):
        weights = pd.Series({"VOO": 0.95, "BIL": 0.05})
        weights.attrs.update(
            expected_annual_return=0.08,
            expected_annual_return_3se=-0.12,
            annualized_volatility=0.15,
            historical_annualized_volatility=0.20,
            annualized_excess_return=0.05,
            kelly_half=0.9000,
            historical_kelly_half=0.6250,
            bil_annual_rate=0.03,
        )
        stdout = StringIO()
        with redirect_stdout(stdout):
            pipeline.print_latest_weights(weights)

        self.assertIn("Expected return (historical mean): 8.00%", stdout.getvalue())
        self.assertIn("Expected return (3-SE downside): -12.00%", stdout.getvalue())
        rows = [line.split() for line in stdout.getvalue().splitlines()]
        self.assertIn(["Volatility", "estimate", "Historical", "Forecast"], rows)
        self.assertIn(["Portfolio", "volatility", "20.00%", "15.00%"], rows)
        self.assertIn(["BIL", "3.00%", "5.00%", "0.6250", "0.9000"], rows)
        self.assertNotIn("Kelly 1/3 hist.", stdout.getvalue())
        self.assertNotIn("Kelly 1/3 fcst.", stdout.getvalue())
        self.assertIn("Kelly 1/2 hist.", stdout.getvalue())
        self.assertIn("Kelly 1/2 fcst.", stdout.getvalue())

    def test_half_kelly_early_exit_caps_position_at_one(self):
        returns = pd.DataFrame({"VOO": [-0.10, 0.30]})
        factor = pipeline.calculate_half_kelly_position(
            returns, pd.Series({"VOO": 1.0}), pd.Series([0.0, 0.0]),
        )
        self.assertEqual(factor, 1.0)

    def test_half_kelly_below_full_position_keeps_signed_fraction(self):
        returns = pd.DataFrame({"VOO": [-0.30, 0.20]})
        factor = pipeline.calculate_half_kelly_position(
            returns, pd.Series({"VOO": 1.0}), pd.Series([0.0, 0.0]),
        )
        self.assertAlmostEqual(factor, -5.0 / 12.0)

    def test_half_kelly_rejects_invalid_variance(self):
        returns = pd.DataFrame({"VOO": [0.01, 0.02]})
        for variance in (0.0, -0.01, float("nan"), float("inf")):
            with self.subTest(variance=variance), self.assertRaisesRegex(
                ValueError, "finite, positive portfolio variance",
            ):
                pipeline.calculate_half_kelly_position(
                    returns, pd.Series({"VOO": 1.0}), pd.Series([0.0, 0.0]),
                    annualized_variance=variance,
                )

    def test_default_end_date_excludes_new_york_today_and_weekends(self):
        for instant, expected in (
            ("2026-10-20T02:12:00+08:00", "2026-10-16"),
            ("2026-01-20T02:12:00+08:00", "2026-01-16"),
            ("2026-10-20T23:12:00+08:00", "2026-10-19"),
            ("2026-10-17T23:12:00+08:00", "2026-10-16"),
            ("2026-10-18T23:12:00+08:00", "2026-10-16"),
            ("2026-01-20T23:12:00+08:00", "2026-01-19"),
        ):
            now = pd.Timestamp(instant)
            with self.subTest(instant=instant), patch.object(core.pd, "Timestamp") as clock:
                clock.now.return_value = now.tz_convert("America/New_York")
                self.assertEqual(core.PipelineConfig().end_date, expected)
                clock.now.assert_called_once_with(tz="America/New_York")
                self.assertEqual(core.PipelineConfig(end_date="2020-01-10").end_date,
                                 "2020-01-10")
                clock.now.assert_called_once()

    def test_previous_session_download_is_cached_across_weekends_and_holidays(self):
        for instant, last_session in (
            ("2026-10-19T10:00:00-04:00", "2026-10-16"),
            ("2026-01-20T10:00:00-05:00", "2026-01-16"),
        ):
            now = pd.Timestamp(instant)
            with patch.object(core.pd, "Timestamp") as clock:
                clock.now.return_value = now
                cfg = core.PipelineConfig()
            with (
                self.subTest(instant=instant),
                TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory,
                patch.object(pipeline, "DEFAULT_CACHE_PATH", Path(directory) / "kline.sqlite3"),
                patch.object(pipeline, "get_default_quote_client") as get_client,
                patch("broker.market_data.datetime") as cache_clock,
                redirect_stderr(StringIO()),
            ):
                cache_clock.now.return_value = now.to_pydatetime()
                client = get_client.return_value
                client.get_history_kline.return_value = {"kline_list": [
                    {"time_key": last_session, "close": "100.0"},
                    {"time_key": now.date().isoformat(), "close": "101.0"},
                ]}
                first = pipeline.download_prices(["VOO"], last_session, cfg.end_date)
                second = pipeline.download_prices(["VOO"], last_session, cfg.end_date)
                client.get_history_kline.assert_called_once()
                self.assertEqual(client.get_history_kline.call_args.args[1], cfg.end_date)
                self.assertEqual(first.index.tolist(), [pd.Timestamp(last_session)])
                pd.testing.assert_frame_equal(first, second)

    def test_config_contains_only_active_settings(self):
        self.assertEqual(
            {field.name for field in fields(core.PipelineConfig)},
            {
                "start_date", "end_date", "lookback_window", "rebalance_freq",
                "transaction_cost_rate", "garch_horizon", "safe_target",
                "risk_budget", "verbose",
            },
        )

    def test_config_class_and_defaults_remain_in_core(self):
        self.assertIs(pipeline.PipelineConfig, core.PipelineConfig)
        self.assertEqual(core.PipelineConfig.__module__, core.__name__)
        self.assertIsNot(pipeline.CFG, core.CFG)
        self.assertEqual(pipeline.CFG.safe_target, {})
        self.assertEqual(pipeline.CFG.risk_budget, {"VOO": 0.25, "QQQ": 0.5, "VXUS": 0.25})
        self.assertIsNot(pipeline.CFG.safe_target, core.CFG.safe_target)
        self.assertIsNot(pipeline.CFG.risk_budget, core.CFG.risk_budget)
        cfg = pipeline.PipelineConfig(
            safe_target={"BIL": 0.05, "GLD": 0.0},
            risk_budget={"VOO": 1.0, "QQQ": 0.0},
        )
        self.assertEqual(cfg.safe_assets, ["BIL"])
        self.assertEqual(cfg.risk_assets, ["VOO"])
        self.assertEqual(cfg.tickers, ["VOO", "BIL"])
        core.validate_config(cfg)

    def test_backtest_accepts_default_and_explicit_config(self):
        prices = pd.DataFrame(
            {"VOO": [100.0, 101.0, 102.0]}, index=pd.date_range("2026-10-14", periods=3),
        )
        for kwargs in ({}, {"cfg": pipeline.PipelineConfig()}):
            with (
                self.subTest(kwargs=kwargs),
                patch.object(core, "tqdm", side_effect=lambda x, **kw: x),
                self.assertRaisesRegex(ValueError, "no out-of-sample returns"),
            ):
                core.walk_forward_backtest(prices, **kwargs)

    def test_main_prints_latest_weights_without_backtest(self):
        cfg = pipeline.CFG
        dates = pd.date_range("2026-10-15", periods=3)
        prices = pd.DataFrame({"VOO": [100.0, 101.0, 102.0]}, index=dates)
        weights = pd.Series({"VOO": 0.95, "BIL": 0.05})
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(pipeline, "download_prices", return_value=prices) as download,
            patch.object(pipeline, "latest_weights", return_value=weights) as latest,
            patch.object(pipeline, "walk_forward_backtest") as backtest,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = pipeline.main([])

        self.assertEqual(
            stdout.getvalue(),
            "\nRebalance date: 2026-10-17\n"
            "\nLatest weights\n"
            "Asset              Weight\n-------------------------\n"
            "BIL               5.0000%\nVOO              95.0000%\n",
        )
        self.assertTrue(result[0].empty)
        self.assertEqual(list(result[1].iloc[-1]), [0.95, 0.05])
        download.assert_called_once_with(
            list(dict.fromkeys(cfg.risk_assets + cfg.safe_assets + ["BIL"])),
            (pd.Timestamp(cfg.end_date) - pd.offsets.BDay(cfg.lookback_window)).strftime(
                "%Y-%m-%d",
            ),
            cfg.end_date,
        )
        latest.assert_called_once_with(prices, cfg)
        backtest.assert_not_called()

    def test_latest_weights_combines_risk_parity_and_safe_target(self):
        cfg = pipeline.PipelineConfig(
            lookback_window=2,
            safe_target={"BIL": 0.05}, risk_budget={"VOO": 0.5, "QQQ": 0.5},
        )
        dates = pd.date_range("2026-10-15", periods=3)
        prices = pd.DataFrame(
            {
                "VOO": [100.0, 101.0, 100.0],
                "QQQ": [100.0, 99.0, 98.0],
                "BIL": [100.0, 100.0, 100.0],
            },
            index=dates,
        )
        forecast_covariance = pd.DataFrame(
            [[0.09, 0.01], [0.01, 0.04]],
            index=["QQQ", "VOO"], columns=["QQQ", "VOO"],
        )
        with (
            patch.object(
                pipeline, "forecast_garch_volatility",
                return_value=pd.Series({"VOO": 0.2, "QQQ": 0.3}),
            ) as garch,
            patch.object(
                pipeline, "build_forecast_covariance", return_value=forecast_covariance,
            ) as covariance,
            patch.object(
                pipeline, "optimize_risk_budget",
                return_value=pd.Series({"VOO": 0.6, "QQQ": 0.4}),
            ) as optimize,
        ):
            weights = pipeline.latest_weights(prices, cfg)

        self.assertEqual(
            weights.to_dict(), {"VOO": 0.6 * 0.95, "QQQ": 0.4 * 0.95, "BIL": 0.05},
        )
        returns = prices.pct_change(fill_method=None).dropna()
        portfolio_returns = returns[["VOO", "QQQ"]].dot(pd.Series({"VOO": 0.6, "QQQ": 0.4}))
        historical_variance = portfolio_returns.var(ddof=1) * 252
        self.assertAlmostEqual(
            weights.attrs["historical_annualized_volatility"], historical_variance ** 0.5,
        )
        self.assert_two_session_kelly(
            weights.attrs["historical_kelly_half"], portfolio_returns, returns["BIL"],
        )
        self.assertAlmostEqual(
            weights.attrs["expected_annual_return"],
            portfolio_returns.mean() * 252,
        )
        self.assertAlmostEqual(
            weights.attrs["expected_annual_return_3se"],
            (
                portfolio_returns.mean()
                - 3 * portfolio_returns.std(ddof=1) / len(portfolio_returns) ** 0.5
            ) * 252,
        )
        self.assertAlmostEqual(
            weights.attrs["annualized_volatility"],
            (0.6 ** 2 * 0.04 + 2 * 0.6 * 0.4 * 0.01 + 0.4 ** 2 * 0.09) ** 0.5,
        )
        self.assertAlmostEqual(
            weights.attrs["annualized_excess_return"],
            portfolio_returns.mean() * 252,
        )
        self.assertAlmostEqual(weights.attrs["bil_annual_rate"], 0.0)
        self.assert_two_session_kelly(
            weights.attrs["kelly_half"], portfolio_returns, returns["BIL"],
            weights.attrs["annualized_volatility"] ** 2,
        )
        window = garch.call_args.args[0]
        self.assertEqual(list(window.columns), ["VOO", "QQQ"])
        self.assertEqual(garch.call_args.kwargs, {"horizon": cfg.garch_horizon})
        covariance.assert_called_once()
        optimize.assert_called_once_with(
            forecast_covariance, asset_names=cfg.risk_assets, risk_budget=cfg.risk_budget,
        )

    def test_main_with_backtest_prints_only_final_weights_without_plotting(self):
        cfg = pipeline.CFG
        dates = pd.to_datetime(["2026-10-15", "2026-10-16"])
        prices = pd.DataFrame(
            {"VOO": [100.0, 101.0, 102.0]},
            index=pd.date_range("2026-10-15", periods=3),
        )
        returns = pd.Series([0.01], index=dates[-1:])
        weights = pd.DataFrame(
            {"VOO": [0.8, 0.7], "BIL": [0.2, 0.3]}, index=dates,
        )
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(pipeline, "download_prices", return_value=prices) as download,
            patch.object(
                pipeline, "walk_forward_backtest", return_value=(returns, weights),
            ) as backtest,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = pipeline.main(["--backtest"])

        self.assertEqual(
            stdout.getvalue(),
            "\nRebalance date: 2026-10-16\n"
            "\nLatest weights\n"
            "Asset              Weight\n-------------------------\n"
            "BIL              30.0000%\nVOO              70.0000%\n",
        )
        self.assertIs(result[0], returns)
        self.assertIs(result[1], weights)
        download.assert_called_once_with(
            list(dict.fromkeys(cfg.risk_assets + cfg.safe_assets + ["BIL"])),
            (pd.Timestamp(cfg.start_date) - pd.offsets.BDay(cfg.lookback_window)).strftime(
                "%Y-%m-%d",
            ),
            cfg.end_date,
        )
        backtest.assert_called_once_with(prices, cfg=cfg)

    def test_empty_history_is_reported_without_stdout(self):
        returns, weights = pd.Series(dtype=float), pd.DataFrame()
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(pipeline, "download_prices", return_value=pd.DataFrame()),
            patch.object(
                pipeline, "walk_forward_backtest", return_value=(returns, weights),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = pipeline.main(["--backtest"])

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("No portfolio weights were produced", stderr.getvalue())
        self.assertIs(result[0], returns)
        self.assertIs(result[1], weights)

    def test_download_diagnostics_use_stderr(self):
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(pipeline, "get_default_quote_client"),
            patch.object(pipeline, "fetch_daily_klines", return_value={
                "VOO": {date(2026, 10, 16): {"close": "100.0"}},
            }),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            prices = pipeline.download_prices(["VOO"], "2026-10-16", "2026-10-16")

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Data coverage", stderr.getvalue())
        self.assertIn("Final price data", stderr.getvalue())
        self.assertEqual(prices.loc[pd.Timestamp("2026-10-16"), "VOO"], 100.0)

    def test_advancing_cutoff_only_downloads_the_last_chunk(self):
        with (
            TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory,
            patch.object(pipeline, "DEFAULT_CACHE_PATH", Path(directory) / "kline.sqlite3"),
            patch.object(pipeline, "get_default_quote_client") as get_client,
            patch("broker.market_data.time.sleep"),
            redirect_stderr(StringIO()),
        ):
            client = get_client.return_value

            def response(code, end, *, start, **kwargs):
                return {"kline_list": [
                    {"time_key": day, "close": "100.0"}
                    for day in sorted({start, end})
                ]}

            client.get_history_kline.side_effect = response
            pipeline.download_prices(["VOO"], "2020-01-01", "2021-01-01")
            self.assertEqual(client.get_history_kline.call_count, 1)
            client.get_history_kline.reset_mock()
            first = pipeline.download_prices(["VOO"], "2020-01-01", "2021-01-02")
            client.get_history_kline.assert_called_once_with(
                "US.VOO", "2021-01-02", start="2020-01-01",
                ktype=2, autype=1, num=370, extended_time=0,
            )
            second = pipeline.download_prices(["VOO"], "2020-01-01", "2021-01-02")
            self.assertEqual(client.get_history_kline.call_count, 1)
            pd.testing.assert_frame_equal(first, second)

    def test_multi_year_windows_chunk_within_the_370_bar_page(self):
        with (
            TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory,
            patch.object(pipeline, "DEFAULT_CACHE_PATH", Path(directory) / "kline.sqlite3"),
            patch.object(pipeline, "get_default_quote_client") as get_client,
            patch("broker.market_data.time.sleep"),
            redirect_stderr(StringIO()),
        ):
            client = get_client.return_value

            def response(code, end, *, start, **kwargs):
                return {"kline_list": [
                    {"time_key": day, "close": "100.0"}
                    for day in sorted({start, end})
                ]}

            client.get_history_kline.side_effect = response
            pipeline.download_prices(["VOO"], "2020-01-01", "2023-01-01")
            self.assertEqual(client.get_history_kline.call_count, 3)
            starts, ends = zip(*(
                (call.kwargs["start"], call.args[1])
                for call in client.get_history_kline.call_args_list
            ))
            self.assertEqual(starts[0], "2020-01-01")
            self.assertEqual(ends[-1], "2023-01-01")
            for start, end in zip(starts, ends):
                self.assertLessEqual(
                    (date.fromisoformat(end) - date.fromisoformat(start)).days, 532,
                )
            for previous, start in zip(ends, starts[1:]):
                self.assertEqual(
                    date.fromisoformat(start),
                    date.fromisoformat(previous) + timedelta(days=1),
                )

    def test_download_reuses_completed_chunk_after_interruption(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            client = Mock()
            completed = {"kline_list": [
                {"time_key": "2020-01-02", "close": "90.0"},
            ]}
            client.get_history_kline.side_effect = [
                completed, RuntimeError("interrupted"),
            ]
            with (
                patch.object(pipeline, "DEFAULT_CACHE_PATH", Path(directory) / "kline.sqlite3"),
                patch.object(pipeline, "get_default_quote_client", return_value=client),
                patch("broker.market_data.time.sleep"),
                redirect_stderr(StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    pipeline.download_prices(["VOO"], "2020-01-01", "2021-12-31")
                client.get_history_kline.reset_mock()
                client.get_history_kline.side_effect = None
                client.get_history_kline.return_value = {"kline_list": [
                    {"time_key": "2021-12-31", "close": "100.0"},
                ]}
                prices = pipeline.download_prices(["VOO"], "2020-01-01", "2021-12-31")
            # Only the unfinished chunk is refetched; chunk one is served from
            # the cache that was committed before the interruption.
            client.get_history_kline.assert_called_once()
            self.assertEqual(
                client.get_history_kline.call_args.kwargs["start"], "2021-06-17",
            )
            self.assertEqual(prices.loc[pd.Timestamp("2020-01-02"), "VOO"], 90.0)
            self.assertEqual(prices.loc[pd.Timestamp("2021-12-31"), "VOO"], 100.0)


if __name__ == "__main__":
    unittest.main()

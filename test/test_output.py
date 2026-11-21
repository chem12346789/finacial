"""Offline result formatting and CLI output tests."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date, timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from momentum import Security, Universe, main
from ouput.momentum import format_momentum, save_momentum


class MomentumOutputTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.output_path = Path(temp.name) / "ouput" / "momentum.json"
        output = patch("momentum.DEFAULT_MOMENTUM_PATH", self.output_path)
        output.start()
        self.addCleanup(output.stop)
        self.result = {
            "as_of": "2026-10-16",
            "holdings_as_of": "2026-10-16",
            "config": {"lookback": 252, "skip": 21, "top": 20, "positive_only": False},
            "universe_size": 3,
            "eligible_count": 2,
            "excluded": [{"symbol": "SHORT", "reason": "insufficient_history"}],
            "candidates": [
                {"rank": 1, "symbol": "BRK.B", "sources": ("NYSE",),
                 "score": 12.5, "raw_return": 0.125, "daily_volatility": 0.01,
                 "start_date": "2025-10-15", "end_date": "2026-09-16",
                 "start_price": 100.0, "end_price": 112.5},
                {"rank": 2, "symbol": "AAA", "sources": ("NASDAQ", "OTHER"),
                 "score": -2.5, "raw_return": -0.05, "daily_volatility": 0.02,
                 "start_date": "2025-10-15", "end_date": "2026-09-16",
                 "start_price": 10.0, "end_price": 9.5},
            ],
        }

    def test_table_columns_values_alignment_and_no_mutation(self):
        original = deepcopy(self.result)
        output = format_momentum(self.result)
        for text in (
            "Rank", "Symbol", "Sources", "RA score", "Return", "Daily vol", "Start date", "End date",
            "Start price", "End price", "12.50%", "-5.00%", "100.0000", "9.5000",
            "12.5000", "-2.5000", "1.00%", "2.00%",
            "BRK.B", "NASDAQ, OTHER", "2025-10-15", "2026-09-16",
            "Universe: 3", "Eligible: 2", "Shown: 2", "Excluded: 1",
        ):
            self.assertIn(text, output)
        lines = [line for line in output.splitlines() if line.startswith(("+", "|"))]
        self.assertEqual(len({len(line) for line in lines}), 1)
        cells = [line for line in lines if line.startswith("|")]
        self.assertEqual(
            [[i for i, char in enumerate(line) if char == "|"] for line in cells],
            [[i for i, char in enumerate(cells[0]) if char == "|"]] * len(cells),
        )
        self.assertLess(output.index("BRK.B"), output.index("AAA"))
        self.assertEqual(self.result, original)

    def test_empty_table(self):
        self.result["candidates"] = []
        self.result["eligible_count"] = 0
        output = format_momentum(self.result)
        self.assertIn("No candidates.", output)
        self.assertIn("Shown: 0", output)
        self.assertIn("Symbol", output)

    def test_json_preserves_full_result(self):
        self.assertEqual(
            json.loads(format_momentum(self.result, output_format="json")),
            json.loads(json.dumps(self.result)),
        )
        self.result["candidates"][0]["score"] = float("nan")
        with self.assertRaises(ValueError):
            format_momentum(self.result, output_format="json")

    def test_unknown_format_raises(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            format_momentum(self.result, output_format="csv")

    def test_tables_describe_each_method(self):
        for method, description in (
            ("roc", "cumulative simple return"),
            ("sharpe", "mean daily return / sample daily volatility"),
            ("regression", "daily log-price regression slope * R-squared"),
        ):
            with self.subTest(method=method):
                self.result["scoring"] = {"method": method}
                output = format_momentum(self.result)
                self.assertIn(f"Method: {method}", output)
                self.assertIn(description, output)
                self.assertNotIn("RA score", output)

    def test_cli_methods_share_download_and_save_distinct_results(self):
        day = date(2026, 10, 16)
        methods = ("volatility_scaled", "roc", "sharpe", "regression", "ensemble")
        prices = {"AAA": {
            day - timedelta(days=2): 100.0,
            day - timedelta(days=1): 110.0, day: 115.0,
        }}
        with (
            patch("momentum._credentials_client", return_value=Mock()),
            patch("momentum.fetch_tradingview_securities", return_value=[
                Security("AAA", "NASDAQ", "common_stock", 1000),
            ]) as screener,
            patch("momentum.fetch_futu_daily_bars", return_value={}) as bars,
            patch("momentum.build_dynamic_universe", return_value=SimpleNamespace(
                universe=Universe(day, {"AAA": ("NASDAQ",)}),
                eligible_count=1, excluded=(),
            )),
            patch("momentum.fetch_futu_prices", return_value=prices) as fetch,
            redirect_stdout(StringIO()) as stdout,
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main([
                "--tradingview", "--as-of", day.isoformat(), "--no-cache",
                "--lookback", "2", "--methods", *methods,
                "--save-prices", str(self.output_path.parent / "prices.csv"),
            ]), 0)
        screener.assert_called_once()
        bars.assert_called_once()
        fetch.assert_called_once()
        results = json.loads(stdout.getvalue())
        self.assertEqual([result["scoring"]["method"] for result in results], list(methods))
        self.assertFalse(self.output_path.exists())
        for method, result in zip(methods, results):
            destination = self.output_path.with_name(f"momentum_{method}.json")
            self.assertEqual(json.loads(destination.read_text()), result)
        self.assertTrue((self.output_path.parent / "prices.csv").is_file())

    def test_ensemble_table_displays_average_and_component_ranks(self):
        self.result["scoring"] = {"method": "ensemble"}
        for candidate in self.result["candidates"]:
            candidate.update({
                "average_rank": 1.5, "risk_adjusted_rank": 1.5,
                "method_ranks": {
                    method: 1.5
                    for method in ("volatility_scaled", "sharpe", "roc", "regression")
                },
            })
        original = deepcopy(self.result)
        output = format_momentum(self.result)
        for label in ("Avg rank", "Risk rank", "Vol rank", "Sharpe rank", "ROC rank", "Trend rank"):
            self.assertIn(label, output)
        lines = [line for line in output.splitlines() if line.startswith(("+", "|"))]
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertIn("lower Avg rank is better", output)
        self.assertEqual(self.result, original)

    def test_single_method_preserves_output_path_and_batch_empty_exits_nonzero(self):
        day = date(2026, 10, 16)
        prices = {"AAA": {
            day - timedelta(days=2): 100.0,
            day - timedelta(days=1): 100.0, day: 100.0,
        }}
        for options, expected_status in (
            (["--method", "roc"], 0),
            (["--method", "ensemble"], 1),
            (["--methods", "roc", "sharpe"], 1),
            (["--methods", "roc", "sharpe", "--format", "table"], 1),
        ):
            with (
                self.subTest(options=options),
                patch("momentum._credentials_client", return_value=Mock()),
                patch("momentum.fetch_tradingview_securities", return_value=[
                    Security("AAA", "NASDAQ", "common_stock", 1000),
                ]),
                patch("momentum.fetch_futu_daily_bars", return_value={}),
                patch("momentum.build_dynamic_universe", return_value=SimpleNamespace(
                    universe=Universe(day, {"AAA": ("NASDAQ",)}),
                    eligible_count=1, excluded=(),
                )),
                patch("momentum.fetch_futu_prices", return_value=prices),
                redirect_stdout(StringIO()) as stdout,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(main([
                    "--tradingview", "--as-of", day.isoformat(), "--no-cache",
                    "--lookback", "2", *options,
                ]), expected_status)
            if options[0] == "--method":
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["scoring"]["method"], options[1])
                self.assertEqual(json.loads(self.output_path.read_text()), result)
            elif options[-1] == "table":
                self.assertIn("Method: roc", stdout.getvalue())
                self.assertIn("Method: sharpe", stdout.getvalue())
                self.assertIn("No candidates.", stdout.getvalue())
            else:
                results = json.loads(stdout.getvalue())
                self.assertEqual(results[1]["candidates"], [])
                saved = self.output_path.with_name("momentum_sharpe.json")
                self.assertEqual(json.loads(saved.read_text()), results[1])

    def test_cli_default_json_table_and_empty_result_exit_status(self):
        day = date(2026, 10, 16)
        for options, empty in (
            ([], False), (["--format", "table"], False), ([], True),
            (["--format", "table"], True),
            (["--output", str(self.output_path.parent / "custom.json")], False),
        ):
            result = deepcopy(self.result)
            if empty:
                result["candidates"] = []
            with (
                self.subTest(options=options, empty=empty),
                patch("momentum._credentials_client", return_value=Mock()),
                patch("momentum.fetch_tradingview_securities", return_value=[
                    Security("AAA", "NASDAQ", "common_stock", 1000),
                ]),
                patch("momentum.fetch_futu_daily_bars", return_value={}),
                patch("momentum.build_dynamic_universe", return_value=SimpleNamespace(
                    universe=Universe(day, {"AAA": ("NASDAQ",)}),
                    eligible_count=1, excluded=(),
                )),
                patch("momentum.fetch_futu_prices", return_value={}),
                patch("momentum.screen", return_value=result),
                redirect_stdout(StringIO()) as stdout,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(main([
                    "--tradingview", "--as-of", day.isoformat(), "--no-cache", *options,
                ]), 1 if empty else 0)
            output_path = Path(options[1]) if options[:1] == ["--output"] else self.output_path
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["candidates"], json.loads(json.dumps(result["candidates"])))
            if options[:1] != ["--format"]:
                self.assertEqual(json.loads(stdout.getvalue())["candidates"],
                                 json.loads(json.dumps(result["candidates"])))
            else:
                self.assertIn("Momentum as of 2026-10-16", stdout.getvalue())
                self.assertIn("No candidates." if empty else "BRK.B", stdout.getvalue())

    def test_save_rejects_invalid_json_without_overwriting_previous_result(self):
        save_momentum(self.result, self.output_path)
        before = self.output_path.read_text(encoding="utf-8")
        self.result["candidates"][0]["score"] = float("nan")
        with self.assertRaises(ValueError):
            save_momentum(self.result, self.output_path)
        self.assertEqual(self.output_path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.output_path.parent.glob("*.tmp")), [])

    def test_save_failure_preserves_previous_result_and_cleans_temporary_file(self):
        save_momentum(self.result, self.output_path)
        before = self.output_path.read_text(encoding="utf-8")
        with (
            patch("ouput.momentum.Path.replace", side_effect=OSError("write denied")),
            self.assertRaisesRegex(OSError, "write denied"),
        ):
            save_momentum(self.result, self.output_path)
        self.assertEqual(self.output_path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.output_path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

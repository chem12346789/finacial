"""Offline tests for constituent selection and momentum ranking."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from io import StringIO
import json
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from momentum import (
    DailyBar,
    DynamicUniverseConfig,
    MomentumConfig,
    Security,
    Universe,
    build_dynamic_universe,
    _credentials_client,
    fetch_futu_daily_bars,
    fetch_futu_prices,
    load_prices,
    load_universe,
    main,
    screen,
)


class MomentumTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        output = patch("momentum.DEFAULT_MOMENTUM_PATH", self.root / "momentum.json")
        output.start()
        self.addCleanup(output.stop)
        self.days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
        self.universe = Universe(
            self.days[0], {
                "AAA": ("RUSSELL1000", "VOO"),
                "BBB": ("QQQ", "RUSSELL1000", "VOO"),
            },
        )
        self.prices = {
            "AAA": dict(zip(self.days, [100, 110, 120, 130, 140, 1])),
            "BBB": dict(zip(self.days, [100, 110, 120, 125, 130, 1000])),
        }
        self.config = MomentumConfig(lookback=5, skip=1, top=20)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_jump_risk_industries_are_excluded_before_rank_cutoffs(self):
        day = date(2026, 7, 6)
        industries = (
            "Biotechnology", "Pharmaceuticals: Major", "Pharmaceuticals: Generic",
            "Pharmaceuticals: Other", "352010", "35202010",
        )
        securities = [
            Security(f"RISK{i}", "NASDAQ", "common_stock", 1000, industry=industry)
            for i, industry in enumerate(industries)
        ]
        securities.append(Security("SAFE", "NYSE", "common_stock", 100))
        bars = {security.symbol: {day: DailyBar(10, security.market_cap)}
                for security in securities}
        result = build_dynamic_universe(
            securities, bars, as_of=day,
            config=DynamicUniverseConfig(adv_days=1, adv_top=1, market_cap_top=1),
        )
        self.assertEqual(result.universe.members, {"SAFE": ("NYSE",)})
        self.assertEqual(result.excluded, tuple(
            {"symbol": f"RISK{i}", "reason": "excluded_industry"}
            for i in range(len(industries))
        ))

    def test_earnings_blackout_inclusive_sessions_across_holiday_weekend(self):
        event = date(2026, 7, 6)
        for day, excluded in (
            (date(2026, 6, 30), False), (date(2026, 7, 1), True),
            (date(2026, 7, 2), True), (event, True),
            (date(2026, 7, 7), True), (date(2026, 7, 8), False),
        ):
            for earnings in ((event,), (date(2026, 4, 6), event), (date(2026, 7, 4),)):
                with self.subTest(day=day, earnings=earnings):
                    securities = [
                        Security("EVENT", "NYSE", "common_stock", 1000,
                                 earnings_dates=earnings),
                        Security("SAFE", "NYSE", "common_stock", 100),
                    ]
                    bars = {security.symbol: {day: DailyBar(10, security.market_cap)}
                            for security in securities}
                    result = build_dynamic_universe(
                        securities, bars, as_of=day,
                        config=DynamicUniverseConfig(adv_days=1, adv_top=1, market_cap_top=1),
                    )
                    self.assertEqual(
                        result.universe.members, {"SAFE" if excluded else "EVENT": ("NYSE",)},
                    )
                    if excluded:
                        self.assertEqual(result.excluded, (
                            {"symbol": "EVENT", "reason": "earnings_blackout"},
                        ))

    def test_dynamic_universe_intersects_independent_adv_and_cap_ranks(self):
        days = [date(2026, 1, 1), date(2026, 1, 2)]
        securities = [
            Security("aaa", "NYSE", "common_stock", 1000),
            Security("BBB", "NASDAQ", "common_stock", 900),
            Security("CCC", "AMEX", "common_stock", 800),
            Security("DDD", "NYSE", "common_stock", 700),
            Security("LOW", "NYSE", "common_stock", 600),
            Security("ADR", "NASDAQ", "common_stock", 500, is_adr=True),
            Security("REIT", "AMEX", "common_stock", 400, is_reit=True),
        ]
        dollar_volumes = {
            "AAA": 1000, "BBB": 10000, "CCC": 9000, "DDD": 8000,
            "LOW": 7000, "ADR": 6000, "REIT": 5000,
        }
        bars = {
            symbol: {
                day: DailyBar(
                    4.99 if symbol == "LOW" and day == days[-1] else 10,
                    dollar_volume / 10,
                )
                for day in days
            }
            for symbol, dollar_volume in dollar_volumes.items()
        }
        result = build_dynamic_universe(
            securities, bars, as_of=days[-1],
            config=DynamicUniverseConfig(
                adv_days=2, adv_top=2, market_cap_top=2,
                exclude_adrs=True, exclude_reits=True,
            ),
        )
        self.assertEqual(result.universe.members, {"BBB": ("NASDAQ",)})
        self.assertEqual(result.eligible_count, 1)
        self.assertEqual(result.excluded, (
            {"symbol": "AAA", "reason": "adv_below_rank_cutoff"},
            {"symbol": "ADR", "reason": "adr"},
            {"symbol": "CCC", "reason": "market_cap_below_rank_cutoff"},
            {"symbol": "DDD", "reason": "adv_below_rank_cutoff,market_cap_below_rank_cutoff"},
            {"symbol": "LOW", "reason": "price_below_minimum"},
            {"symbol": "REIT", "reason": "reit"},
        ))

    def test_dynamic_universe_includes_adrs_by_default_and_can_exclude_them(self):
        day = date(2026, 10, 16)
        securities = [Security("ADR", "NYSE", "adr", 1000, is_adr=True)]
        bars = {"ADR": {day: DailyBar(10, 100)}}
        included = build_dynamic_universe(
            securities, bars, as_of=day, config=DynamicUniverseConfig(adv_days=1),
        )
        self.assertEqual(included.universe.members, {"ADR": ("NYSE",)})
        excluded = build_dynamic_universe(
            securities, bars, as_of=day,
            config=DynamicUniverseConfig(adv_days=1, exclude_adrs=True),
        )
        self.assertEqual(excluded.universe.members, {})
        self.assertEqual(excluded.excluded, ({"symbol": "ADR", "reason": "adr"},))

    def test_cli_includes_adrs_by_default_and_can_exclude_them(self):
        day = date(2026, 10, 16)
        securities = [
            Security("AAA", "NASDAQ", "common_stock", 1000),
            Security("ADR", "NYSE", "adr", 900, is_adr=True),
        ]
        for options, expected_symbols in (
            ([], ["AAA", "ADR"]), (["--exclude-adrs"], ["AAA"]),
        ):
            client = Mock()
            client.get_history_kline.return_value = {"kline_list": [
                {"time_key": (day - timedelta(days=i)).isoformat(),
                 "close": 100 + i, "volume": 200}
                for i in reversed(range(3))
            ]}
            with (
                self.subTest(options=options),
                patch("momentum._credentials_client", return_value=client),
                patch("momentum.fetch_tradingview_securities",
                      return_value=securities) as screener,
                patch("momentum.time.sleep"),
                redirect_stderr(StringIO()),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(main([
                    "--tradingview", "--as-of", day.isoformat(), "--no-cache",
                    "--format", "json",
                    "--adv-days", "2", "--lookback", "2", "--skip", "0", *options,
                ]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(
                [item["symbol"] for item in result["candidates"]], expected_symbols,
            )
            self.assertEqual(screener.call_args.args[0].exclude_adrs, bool(options))
            self.assertEqual(result["dynamic_universe"]["config"]["exclude_adrs"],
                             bool(options))

    def test_reits_are_included_by_default_and_can_be_excluded(self):
        day = date(2026, 10, 16)
        securities = [Security("TRUST", "NYSE", "reit", 1000, is_reit=True)]
        bars = {"TRUST": {day: DailyBar(10, 100)}}
        for exclude in (False, True):
            with self.subTest(exclude=exclude):
                config = (
                    DynamicUniverseConfig(adv_days=1, exclude_reits=True)
                    if exclude else DynamicUniverseConfig(adv_days=1)
                )
                result = build_dynamic_universe(securities, bars, as_of=day, config=config)
                self.assertEqual(
                    result.universe.members, {} if exclude else {"TRUST": ("NYSE",)},
                )
                self.assertEqual(
                    result.excluded,
                    ({"symbol": "TRUST", "reason": "reit"},) if exclude else (),
                )

    def test_cli_reit_default_exclusion_and_legacy_inclusion(self):
        day = date(2026, 10, 16)
        for options, expected in (
            ([], ["AAA", "TRUST"]),
            (["--exclude-reits"], ["AAA"]),
            (["--include-reits"], ["AAA", "TRUST"]),
        ):
            client = Mock()
            client.get_history_kline.return_value = {"kline_list": [
                {"time_key": (day - timedelta(days=i)).isoformat(),
                 "close": 100 + i, "volume": 200}
                for i in reversed(range(3))
            ]}
            with (
                self.subTest(options=options),
                patch("momentum._credentials_client", return_value=client),
                patch("momentum.fetch_tradingview_securities", return_value=[
                    Security("AAA", "NASDAQ", "common_stock", 1000),
                    Security("TRUST", "NYSE", "reit", 900, is_reit=True),
                ]) as screener,
                patch("momentum.time.sleep"),
                redirect_stderr(StringIO()),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(main([
                    "--tradingview", "--as-of", day.isoformat(), "--no-cache",
                    "--format", "json",
                    "--adv-days", "2", "--lookback", "2", "--skip", "0", *options,
                ]), 0)
            result = json.loads(output.getvalue())
            exclude = "--exclude-reits" in options
            self.assertEqual([item["symbol"] for item in result["candidates"]], expected)
            self.assertEqual(screener.call_args.args[0].exclude_reits, exclude)
            self.assertEqual(result["dynamic_universe"]["config"]["exclude_reits"], exclude)

    def test_cli_rejects_conflicting_reit_flags_before_network(self):
        with (
            patch("momentum._credentials_client") as factory,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main([
                "--tradingview", "--as-of", "2026-10-16",
                "--include-reits", "--exclude-reits",
            ])
        self.assertEqual(error.exception.code, 2)
        factory.assert_not_called()

    def test_cli_max_securities_limits_kline_downloads(self):
        day = date(2026, 10, 16)
        securities = [
            Security(f"S{i}", "NASDAQ", "common_stock", 1000 - i) for i in range(5)
        ]
        client = Mock()
        client.get_history_kline.return_value = {"kline_list": [
            {"time_key": (day - timedelta(days=i)).isoformat(),
             "close": 100 + i, "volume": 200}
            for i in reversed(range(3))
        ]}
        with (
            patch("momentum._credentials_client", return_value=client),
            patch("momentum.fetch_tradingview_securities", return_value=securities),
            patch("momentum.time.sleep"),
            redirect_stderr(StringIO()),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main([
                "--tradingview", "--as-of", day.isoformat(), "--no-cache",
                "--format", "json", "--max-securities", "2",
                "--adv-days", "2", "--lookback", "2", "--skip", "0",
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(
            [item["symbol"] for item in result["candidates"]], ["S0", "S1"],
        )

    def test_cli_rejects_invalid_max_securities(self):
        with (
            patch("momentum._credentials_client") as factory,
            patch("momentum.fetch_tradingview_securities",
                  return_value=[Security("AAA", "NASDAQ", "common_stock", 1000)]),
            redirect_stderr(StringIO()),
            redirect_stdout(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main([
                "--tradingview", "--as-of", "2026-10-16", "--max-securities", "0",
            ])
        self.assertEqual(error.exception.code, 2)

    def test_dynamic_universe_requires_complete_adv_sessions_and_valid_listing(self):
        day = date(2026, 1, 2)
        security = Security("AAA", "NYSE", "common_stock", 100)
        with self.assertRaisesRegex(ValueError, "completed trading session"):
            build_dynamic_universe(
                [security], {"AAA": {date(2026, 1, 1): DailyBar(10, 100)}},
                as_of=day, config=DynamicUniverseConfig(adv_days=1),
            )
        result = build_dynamic_universe(
            [security, Security("BBB", "NASDAQ", "common_stock", 100)],
            {
                "AAA": {day: DailyBar(10, 100)},
                "BBB": {
                    date(2026, 1, 1): DailyBar(10, 100),
                    day: DailyBar(10, 100),
                },
            },
            as_of=day, config=DynamicUniverseConfig(adv_days=2),
        )
        self.assertIn(
            {"symbol": "AAA", "reason": "insufficient_adv_history"},
            result.excluded,
        )
        for invalid in (
            ("AAA", "OTC", "common_stock", 100),
            ("AAA", "NYSE", "etf", 100),
            ("AAA", "NYSE", "common_stock", 0),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Security(*invalid)

    def run_screen(self, **kwargs):
        return screen(
            self.universe, self.prices,
            as_of=kwargs.pop("as_of", self.days[-1]),
            config=kwargs.pop("config", self.config), **kwargs,
        )

    def test_union_deduplicates_normalizes_and_preserves_membership(self):
        universe = load_universe(self.write(
            "holdings.csv",
            "as_of,source,symbol\n2026-01-01,RUSSELL1000,US.BRK.B\n"
            "2026-01-01,QQQ,brk-b\n2026-01-01,VOO,AAPL\n"
            "2026-01-01,RUSSELL1000,AAPL\n",
        ))
        self.assertEqual(universe.members, {
            "AAPL": ("RUSSELL1000", "VOO"),
            "BRK.B": ("QQQ", "RUSSELL1000"),
        })

    def test_bad_holdings_fail(self):
        for rows in (
            "2026-01-01,VOO,AAPL\n",
            "2026-01-01,VOO,AAPL\n2026-01-02,QQQ,MSFT\n",
            "2026-01-01,RUSSELL1000,VOO\n2026-01-01,QQQ,MSFT\n"
            "2026-01-01,VOO,AAPL\n",
            "2026-01-01,SPY,AAPL\n",
            "2026-01-01,VOO,USD CASH\n",
            "2026-01-01,RUSSELL1000,AAPL\n2026-01-01,VOO,AAPL\n",
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                load_universe(self.write("bad.csv", "as_of,source,symbol\n" + rows))

    def test_exact_formula_skips_recent_price_and_sorts(self):
        result = self.run_screen()
        self.assertEqual(result["universe_size"], 2)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["excluded"], [])
        first, second = result["candidates"]
        self.assertEqual((first["symbol"], first["rank"]), ("AAA", 1))
        self.assertAlmostEqual(first["raw_return"], 0.4)
        self.assertAlmostEqual(second["raw_return"], 0.3)
        self.assertEqual(first["start_date"], self.days[0].isoformat())
        self.assertEqual(first["end_date"], self.days[-2].isoformat())

    def test_default_short_term_momentum_uses_as_of_and_21_session_offset(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(22)]
        prices = {"AAA": {day: 100 + i for i, day in enumerate(days)}}
        result = screen(
            Universe(days[0], {"AAA": ("NYSE",)}), prices, as_of=days[-1],
        )
        row = result["candidates"][0]
        self.assertEqual(result["config"]["lookback"], 21)
        self.assertEqual(result["config"]["skip"], 0)
        self.assertEqual(row["start_date"], days[0].isoformat())
        self.assertEqual(row["end_date"], days[-1].isoformat())
        self.assertAlmostEqual(row["raw_return"], 0.21)
        with self.assertRaisesRegex(ValueError, "22 shared sessions"):
            screen(
                Universe(days[0], {"AAA": ("NYSE",)}),
                {"AAA": {day: prices["AAA"][day] for day in days[1:]}},
                as_of=days[-1],
            )

    def test_explicit_12_minus_1_uses_exact_session_offsets(self):
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(253)]
        prices = {"AAA": {day: 100 + i for i, day in enumerate(days)}}
        result = screen(
            Universe(days[0], {"AAA": ("RUSSELL1000", "VOO", "QQQ")}),
            prices, as_of=days[-1], config=MomentumConfig(252, 21),
        )
        row = result["candidates"][0]
        self.assertEqual(row["start_date"], days[0].isoformat())
        self.assertEqual(row["end_date"], days[231].isoformat())
        self.assertAlmostEqual(row["raw_return"], 2.31)

    def test_top_limit_and_deterministic_ties(self):
        self.prices["BBB"] = self.prices["AAA"].copy()
        result = self.run_screen(config=MomentumConfig(5, 1, 1))
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["symbol"], "AAA")

    def test_risk_adjusted_formula_uses_sample_daily_volatility(self):
        days = self.days[:3]
        prices = {"AAA": dict(zip(days, [100, 110, 120]))}
        result = screen(
            Universe(days[0], {"AAA": ("NYSE",)}), prices,
            as_of=days[-1], config=MomentumConfig(2, 0),
        )
        row = result["candidates"][0]
        expected_volatility = abs(0.1 - (120 / 110 - 1)) / 2 ** 0.5
        self.assertAlmostEqual(row["raw_return"], 0.2)
        self.assertAlmostEqual(row["daily_volatility"], expected_volatility)
        self.assertAlmostEqual(row["score"], 0.2 / expected_volatility)
        self.assertEqual(result["scoring"]["ddof"], 1)
        self.assertFalse(result["scoring"]["annualized"])

    def test_lower_raw_return_can_rank_higher_when_less_volatile(self):
        days = self.days[:3]
        result = screen(
            Universe(days[0], {"STEADY": ("NYSE",), "VOLATILE": ("NYSE",)}),
            {"STEADY": dict(zip(days, [100, 110, 120])),
             "VOLATILE": dict(zip(days, [100, 150, 130]))},
            as_of=days[-1], config=MomentumConfig(2, 0),
        )
        first, second = result["candidates"]
        self.assertEqual(first["symbol"], "STEADY")
        self.assertLess(first["raw_return"], second["raw_return"])
        self.assertGreater(first["score"], second["score"])

    def test_skipped_prices_do_not_change_return_volatility_or_score(self):
        config = MomentumConfig(5, 1)
        before = self.run_screen(config=config)
        self.prices["AAA"][self.days[-1]] = 1000000
        self.prices["BBB"][self.days[-1]] = 0.01
        self.assertEqual(self.run_screen(config=config), before)

    def test_zero_volatility_is_explicitly_excluded(self):
        days = self.days[:3]
        result = screen(
            Universe(days[0], {"FLAT": ("NYSE",), "CONSTANT": ("NYSE",)}),
            {"FLAT": dict(zip(days, [100, 100, 100])),
             "CONSTANT": dict(zip(days, [100, 150, 225]))},
            as_of=days[-1], config=MomentumConfig(2, 0),
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"], [
            {"symbol": "CONSTANT", "reason": "zero_volatility"},
            {"symbol": "FLAT", "reason": "zero_volatility"},
        ])

    def test_short_term_method_formulas(self):
        days = self.days[:3]
        prices = {"AAA": dict(zip(days, [1.0, math.e, math.e]))}
        returns = [math.e - 1, 0.0]
        volatility = (math.e - 1) / math.sqrt(2)
        expected = {
            "volatility_scaled": (math.e - 1) / volatility,
            "roc": math.e - 1,
            "sharpe": sum(returns) / 2 / volatility,
            "regression": 0.375,
        }
        for method, score in expected.items():
            with self.subTest(method=method):
                result = screen(
                    Universe(days[0], {"AAA": ("NYSE",)}), prices,
                    as_of=days[-1], config=MomentumConfig(2, method=method),
                )
                self.assertEqual(result["scoring"]["method"], method)
                self.assertEqual(result["config"]["method"], method)
                self.assertAlmostEqual(result["candidates"][0]["score"], score)

    def test_roc_and_regression_allow_constant_returns(self):
        days = self.days[:3]
        prices = {
            "GROW": dict(zip(days, [100, 150, 225])),
            "FLAT": dict(zip(days, [100, 100, 100])),
        }
        for method, expected in (("roc", 1.25), ("regression", math.log(1.5))):
            with self.subTest(method=method):
                result = screen(
                    Universe(days[0], {symbol: ("NYSE",) for symbol in prices}), prices,
                    as_of=days[-1], config=MomentumConfig(2, method=method),
                )
                self.assertEqual(result["excluded"], [])
                self.assertEqual(result["candidates"][0]["symbol"], "GROW")
                self.assertAlmostEqual(result["candidates"][0]["score"], expected)
                self.assertEqual(result["candidates"][1]["score"], 0.0)
        sharpe = screen(
            Universe(days[0], {symbol: ("NYSE",) for symbol in prices}), prices,
            as_of=days[-1], config=MomentumConfig(2, method="sharpe"),
        )
        self.assertEqual(sharpe["candidates"], [])
        self.assertTrue(all(row["reason"] == "zero_volatility" for row in sharpe["excluded"]))

    def test_all_methods_ignore_skipped_and_future_prices(self):
        for method in ("volatility_scaled", "roc", "sharpe", "regression", "ensemble"):
            with self.subTest(method=method):
                config = MomentumConfig(5, 1, method=method)
                before = self.run_screen(config=config)
                changed = {symbol: dict(series) for symbol, series in self.prices.items()}
                for series in changed.values():
                    series[self.days[-1]] = 1e9
                    series[self.days[-1] + timedelta(days=1)] = 1e12
                after = screen(
                    self.universe, changed, as_of=self.days[-1], config=config,
                )
                self.assertEqual(after, before)

    def test_negative_regression_score_and_positive_filter(self):
        days = self.days[:3]
        for positive_only in (False, True):
            result = screen(
                Universe(days[0], {"AAA": ("NYSE",)}),
                {"AAA": dict(zip(days, [math.e, math.e, 1.0]))},
                as_of=days[-1],
                config=MomentumConfig(2, positive_only=positive_only, method="regression"),
            )
            if positive_only:
                self.assertEqual(result["excluded"], [
                    {"symbol": "AAA", "reason": "non_positive_momentum"},
                ])
            else:
                self.assertAlmostEqual(result["candidates"][0]["score"], -0.375)

    def test_ensemble_ranks_full_common_pool_before_top_selection(self):
        days = self.days[:3]
        symbols = ("AAA", "BBB", "CCC", "DDD", "EEE")
        prices = {
            symbol: dict(zip(days, [100 * i, 110 * i, 115 * i]))
            for i, symbol in enumerate(symbols, 1)
        }
        prices["FLAT"] = dict(zip(days, [1, 1, 1]))
        scores = {
            "volatility_scaled": [5, 2, 1, 4, 3],
            "sharpe": [1, 5, 4, 2, 3],
            "roc": [1, 5, 2, 4, 3],
            "regression": [1, 2, 5, 4, 3],
        }

        def score(method, closes, *args):
            if closes[0] == 1:
                return 999
            return scores[method][int(closes[0] / 100) - 1]

        with patch("momentum_core.core._momentum_score", side_effect=score):
            result = screen(
                Universe(days[0], {symbol: ("NYSE",) for symbol in prices}), prices,
                as_of=days[-1], config=MomentumConfig(2, top=1, method="ensemble"),
            )
        self.assertEqual(result["eligible_count"], 5)
        self.assertEqual(result["excluded"], [
            {"symbol": "FLAT", "reason": "zero_volatility"},
        ])
        row = result["candidates"][0]
        self.assertEqual((row["symbol"], row["rank"]), ("DDD", 1))
        self.assertEqual(row["method_ranks"], {
            "volatility_scaled": 2.0, "sharpe": 4.0, "roc": 2.0, "regression": 2.0,
        })
        self.assertEqual(row["method_scores"], {
            "volatility_scaled": 4, "sharpe": 2, "roc": 4, "regression": 4,
        })
        self.assertEqual(row["risk_adjusted_rank"], 3.0)
        self.assertEqual(row["average_rank"], 7 / 3)
        self.assertEqual(row["score"], -7 / 3)
        self.assertEqual(result["scoring"]["weights"]["roc"], 1 / 3)
        self.assertEqual(result["scoring"]["weights"]["sharpe"], 1 / 6)

    def test_ensemble_averages_ties_and_sorts_symbols(self):
        prices = {
            "BBB": self.prices["AAA"].copy(),
            "AAA": self.prices["AAA"].copy(),
        }
        result = screen(
            self.universe, prices, as_of=self.days[-1],
            config=MomentumConfig(5, 1, method="ensemble"),
        )
        self.assertEqual([row["symbol"] for row in result["candidates"]], ["AAA", "BBB"])
        for row in result["candidates"]:
            self.assertEqual(row["average_rank"], 1.5)
            self.assertTrue(all(rank == 1.5 for rank in row["method_ranks"].values()))

    def test_ensemble_uses_three_group_weights(self):
        result = self.run_screen(config=MomentumConfig(5, 1, method="ensemble"))
        for row in result["candidates"]:
            ranks = row["method_ranks"]
            expected = (
                (ranks["volatility_scaled"] + ranks["sharpe"]) / 2
                + ranks["roc"] + ranks["regression"]
            ) / 3
            self.assertEqual(row["average_rank"], expected)
            self.assertEqual(row["score"], -expected)
        self.assertEqual(
            [row["average_rank"] for row in result["candidates"]],
            sorted(row["average_rank"] for row in result["candidates"]),
        )

    def test_ensemble_empty_pool_retains_exclusion_reasons(self):
        days = self.days[:3]
        result = screen(
            Universe(days[0], {"FLAT": ("NYSE",)}),
            {"FLAT": dict(zip(days, [100, 100, 100]))},
            as_of=days[-1], config=MomentumConfig(2, method="ensemble"),
        )
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"], [
            {"symbol": "FLAT", "reason": "zero_volatility"},
        ])

    def test_unknown_momentum_method_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown momentum method"):
            MomentumConfig(method="invalid")
        with (
            patch("momentum._credentials_client") as client,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(["--tradingview", "--as-of", "2026-10-16", "--method", "invalid"])
        self.assertEqual(error.exception.code, 2)
        client.assert_not_called()

    def test_risk_adjusted_window_requires_two_returns(self):
        for args in ((1, 0), (21, 20)):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "2 daily returns"):
                MomentumConfig(*args)
        with (
            patch("momentum._credentials_client") as factory,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(["--tradingview", "--as-of", "2026-10-16", "--lookback", "1"])
        self.assertEqual(error.exception.code, 2)
        factory.assert_not_called()

    def test_positive_filter_is_explicit(self):
        self.prices["BBB"] = dict(zip(self.days, [100, 101, 99, 101, 100, 100]))
        self.assertEqual(self.run_screen()["eligible_count"], 2)
        result = self.run_screen(config=MomentumConfig(5, 1, 20, True))
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["excluded"], [
            {"symbol": "BBB", "reason": "non_positive_momentum"},
        ])

    def test_missing_stale_short_and_gapped_are_reported(self):
        for change, reason in (
            (lambda: self.prices.pop("BBB"), "missing_prices"),
            (lambda: self.prices["BBB"].pop(self.days[-1]), "stale_prices"),
            (lambda: self.prices["BBB"].pop(self.days[0]), "insufficient_history"),
            (lambda: self.prices["BBB"].pop(self.days[2]), "missing_sessions"),
            (lambda: self.prices["BBB"].pop(self.days[-2]), "missing_sessions"),
        ):
            with self.subTest(reason=reason):
                self.set_prices_for_bbb()
                change()
                self.assertEqual(self.run_screen()["excluded"], [
                    {"symbol": "BBB", "reason": reason},
                ])

    def set_prices_for_bbb(self):
        self.prices["BBB"] = dict(zip(self.days, [100, 110, 120, 125, 130, 1000]))

    def test_fetch_futu_prices_uses_daily_qfq_and_discards_future_bars(self):
        class Client:
            def __init__(self):
                self.requests = []

            def get_history_kline(self, code, end, **kwargs):
                self.requests.append((code, end, kwargs))
                return {"kline_list": [
                    {"time_key": f"{day} 16:00:00", "close": str(close)}
                    for day, close in zip(self.days, [100, 110, 120, 130, 140, 1])
                ] + [{"time_key": "2026-01-11 16:00:00", "close": "999"}]}

        client = Client()
        client.days = self.days
        prices = fetch_futu_prices(client, self.universe, as_of=self.days[-1])
        self.assertEqual(prices["AAA"], self.prices["AAA"])
        self.assertEqual(client.requests, [
            ("US.AAA", self.days[-1].isoformat(), {
                "ktype": 2, "autype": 1, "num": 370, "extended_time": 0,
            }),
            ("US.BBB", self.days[-1].isoformat(), {
                "ktype": 2, "autype": 1, "num": 370, "extended_time": 0,
            }),
        ])

    def test_share_class_codes_work_in_both_history_fetch_paths(self):
        from broker import FutuClient, TokenSet

        day = date(2026, 10, 16)
        client = FutuClient(TokenSet("test-token"))
        response = {"kline_list": [{
            "date": 20261016, "close": 100, "volume": 200,
        }]}
        universe = Universe(day, {"BRK.B": ("NYSE",)})
        with patch.object(client, "_request", return_value=response) as request:
            self.assertEqual(
                fetch_futu_prices(client, universe, as_of=day),
                {"BRK.B": {day: 100}},
            )
            self.assertEqual(
                fetch_futu_daily_bars(
                    client, [Security("BRK.B", "NYSE", "common_stock", 1000)],
                    as_of=day, num=20,
                ),
                {"BRK.B": {day: DailyBar(100, 200)}},
            )
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(
                call.args, ("GET", "/api/v1.0/quote/US.BRK.B/history-kline"),
            )

    def test_fetch_futu_prices_rejects_malformed_rows(self):
        class Client:
            def get_history_kline(self, code, end, **kwargs):
                return {"kline_list": [{"time_key": "2026-01-05 16:00:00"}]}

        with self.assertRaisesRegex(ValueError, "Invalid Futu K-line row"):
            fetch_futu_prices(Client(), self.universe, as_of=self.days[-1])

    def test_fetch_futu_daily_bars_uses_raw_daily_bars(self):
        class Client:
            def __init__(self):
                self.request = None

            def get_history_kline(self, code, end, **kwargs):
                self.request = (code, end, kwargs)
                return {"kline_list": [
                    {"time_key": f"{self_day} 16:00:00", "close": "10", "volume": "100"}
                    for self_day in self.days[-2:]
                ]}

        client = Client()
        client.days = self.days
        security = Security("AAA", "NYSE", "common_stock", 100)
        bars = fetch_futu_daily_bars(
            client, [security], as_of=self.days[-1], num=2,
        )
        self.assertEqual(bars["AAA"], {
            day: DailyBar(10, 100) for day in self.days[-2:]
        })
        self.assertEqual(client.request, (
            "US.AAA", self.days[-1].isoformat(), {
                "ktype": 2, "autype": 0, "num": 2, "extended_time": 0,
            },
        ))

    def test_credentials_client_uses_default_quote_client(self):
        quote_client = object()
        with patch(
            "broker.market_data.get_default_quote_client",
            return_value=quote_client,
        ) as factory:
            self.assertIs(_credentials_client(), quote_client)
        factory.assert_called_once_with()

    def test_rest_kline_millisecond_timestamps_in_both_fetch_paths(self):
        client = Mock()
        client.get_history_kline.return_value = {"kline_list": [
            {"time_key": 1779249600000, "close": 124.72, "volume": 2960896},
        ]}
        day = date(2026, 5, 20)
        universe = Universe(day, {"NVDA": ("NASDAQ",)})
        self.assertEqual(
            fetch_futu_prices(client, universe, as_of=day),
            {"NVDA": {day: 124.72}},
        )
        self.assertEqual(
            fetch_futu_daily_bars(
                client, [Security("NVDA", "NASDAQ", "common_stock", 100)],
                as_of=day, num=20,
            ),
            {"NVDA": {day: DailyBar(124.72, 2960896)}},
        )

    def test_rest_kline_dates_and_local_timezone(self):
        from broker.market_data import futu_bar_date as _futu_bar_date

        # Midnight UTC is still the previous US trading date.
        self.assertEqual(
            _futu_bar_date({"time_key": 1779235200000}, "NVDA"),
            date(2026, 5, 19),
        )
        self.assertEqual(
            _futu_bar_date(
                {"time_key": 1779235200000, "time_zone": 480}, "NVDA",
            ),
            date(2026, 5, 20),
        )
        self.assertEqual(
            _futu_bar_date({"date": 20260520, "time_key": 0}, "NVDA"),
            date(2026, 5, 20),
        )
        for bar in ({"date": 20260230}, {"time_key": None}, {"time_key": True}, {}):
            with self.subTest(bar=bar), self.assertRaisesRegex(ValueError, "date/time_key"):
                _futu_bar_date(bar, "NVDA")

    def test_download_progress_reports_both_stages_without_polluting_stdout(self):
        client = Mock()
        client.get_history_kline.return_value = {"kline_list": [{
            "date": 20261016, "close": 100, "volume": 200,
        }]}
        day = date(2026, 10, 16)
        with (
            redirect_stderr(StringIO()) as stderr,
            redirect_stdout(StringIO()) as stdout,
        ):
            fetch_futu_daily_bars(
                client, [Security("NVDA", "NASDAQ", "common_stock", 1000)],
                as_of=day, num=20, progress=True,
            )
            fetch_futu_prices(
                client, Universe(day, {"NVDA": ("NASDAQ",)}),
                as_of=day, progress=True,
            )
        self.assertEqual(stdout.getvalue(), "")
        for label in ("ADV", "Momentum"):
            self.assertIn(f"{label} [{'#' * 24}] 1/1 NVDA", stderr.getvalue())
        self.assertIn("elapsed=", stderr.getvalue())
        self.assertIn("ETA=0s", stderr.getvalue())

    def test_download_progress_does_not_count_failed_requests_as_complete(self):
        client = Mock()
        client.get_history_kline.side_effect = ValueError("download failed")
        with (
            redirect_stderr(StringIO()) as stderr,
            self.assertRaisesRegex(ValueError, "download failed"),
        ):
            fetch_futu_prices(
                client, Universe(date(2026, 10, 16), {"NVDA": ("NASDAQ",)}),
                as_of=date(2026, 10, 16), progress=True,
            )
        self.assertIn("0/1 NVDA", stderr.getvalue())
        self.assertNotIn("1/1", stderr.getvalue())

    def test_shared_progress_accumulates_stages_and_updates_total(self):
        from momentum import _DownloadProgress

        client = Mock()
        client.get_history_kline.return_value = {"kline_list": [{
            "date": 20261016, "close": 100, "volume": 200,
        }]}
        day = date(2026, 10, 16)
        securities = [
            Security(symbol, "NASDAQ", "common_stock", 1000)
            for symbol in ("NVDA", "AAPL")
        ]
        with (
            patch("momentum.time.monotonic", return_value=100),
            redirect_stderr(StringIO()) as stderr,
            redirect_stdout(StringIO()) as stdout,
        ):
            progress = _DownloadProgress(4)
            fetch_futu_daily_bars(client, securities, as_of=day, num=20, progress=progress)
            self.assertEqual(progress.completed, 2)
            self.assertIn("2/4 ADV AAPL", stderr.getvalue())
            progress.total = 3
            fetch_futu_prices(
                client, Universe(day, {"NVDA": ("NASDAQ",)}),
                as_of=day, progress=progress,
            )
            progress.close()
        self.assertEqual(progress.completed, 3)
        self.assertEqual(progress.started, 100)
        self.assertIn(f"Kline [{'#' * 24}] 3/3 Momentum NVDA", stderr.getvalue())
        self.assertNotIn("0/3", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(client.get_history_kline.call_count, 3)
        self.assertEqual([
            call.kwargs["autype"] for call in client.get_history_kline.call_args_list
        ], [0, 0, 1])

    def test_shared_progress_failure_does_not_complete_failed_task(self):
        from momentum import _DownloadProgress

        day = date(2026, 10, 16)
        client = Mock()
        client.get_history_kline.return_value = {"kline_list": []}
        with redirect_stderr(StringIO()) as stderr:
            progress = _DownloadProgress(2)
            fetch_futu_daily_bars(
                client, [Security("NVDA", "NASDAQ", "common_stock", 1000)],
                as_of=day, num=20, progress=progress,
            )
            client.get_history_kline.side_effect = ValueError("download failed")
            with self.assertRaisesRegex(ValueError, "download failed"):
                fetch_futu_prices(
                    client, Universe(day, {"NVDA": ("NASDAQ",)}),
                    as_of=day, progress=progress,
                )
            progress.close()
        self.assertEqual(progress.completed, 1)
        self.assertIn("1/2 Momentum NVDA", stderr.getvalue())
        self.assertNotIn("2/2", stderr.getvalue())

    def test_interactive_progress_clears_previous_line_and_only_closes_once(self):
        from momentum import _DownloadProgress, _progress

        output = StringIO()
        with redirect_stderr(output), patch.object(output, "isatty", return_value=True):
            progress = _DownloadProgress(2)
            list(_progress(["VERY_LONG_SYMBOL"], "ADV", progress))
            list(_progress(["A"], "Momentum", progress))
            self.assertNotIn("\n", output.getvalue())
            progress.close()
        lines = output.getvalue().split("\r")[1:]
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertTrue(lines[-1].endswith("\n"))
        for previous, current in zip(lines, lines[1:]):
            self.assertGreaterEqual(len(current.rstrip("\n")), len(previous))

    def test_cli_uses_one_progress_counter_for_both_fetch_stages(self):
        day = date(2026, 10, 16)
        client = Mock()
        client.get_history_kline.return_value = {"kline_list": [
            {"time_key": (day - timedelta(days=i)).isoformat(),
             "close": 100 + i, "volume": 200}
            for i in reversed(range(3))
        ]}
        with (
            patch("momentum._credentials_client", return_value=client),
            patch("momentum.fetch_tradingview_securities", return_value=[
                Security("AAA", "NASDAQ", "common_stock", 1000),
                Security("BBB", "NASDAQ", "common_stock", 100),
            ]),
            patch("momentum.time.sleep"),
            redirect_stderr(StringIO()) as stderr,
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(main([
                "--tradingview", "--as-of", day.isoformat(), "--no-cache",
                "--format", "json",
                "--adv-days", "2", "--adv-top", "1", "--market-cap-top", "1",
                "--lookback", "2", "--skip", "0",
            ]), 0)
        output = stderr.getvalue()
        self.assertIn("0/4 ADV", output)
        self.assertIn("2/4 ADV BBB", output)
        self.assertIn("2/3 Momentum AAA", output)
        self.assertIn(f"Kline [{'#' * 24}] 3/3 Momentum AAA", output)
        self.assertNotIn("\nADV [", output)
        self.assertNotIn("\nMomentum [", output)
        self.assertEqual(json.loads(stdout.getvalue())["candidates"][0]["symbol"], "AAA")
        self.assertEqual(client.get_history_kline.call_count, 3)

    def test_history_requests_are_throttled_across_both_fetch_paths(self):
        from broker.market_data import ThrottledHistoryClient as _ThrottledHistoryClient

        raw = Mock()
        raw.get_history_kline.return_value = {"kline_list": []}
        client = _ThrottledHistoryClient(raw)
        day = date(2026, 10, 16)
        with (
            patch("momentum.time.monotonic", return_value=100),
            patch("momentum.time.sleep") as sleep,
        ):
            fetch_futu_daily_bars(
                client, [Security("NVDA", "NASDAQ", "common_stock", 1000)],
                as_of=day, num=20,
            )
            fetch_futu_prices(
                client, Universe(day, {"NVDA": ("NASDAQ",)}), as_of=day,
            )
        sleep.assert_called_once_with(2.0)
        self.assertEqual(raw.get_history_kline.call_count, 2)

    def test_history_retry_count_recovers_one_step_per_success(self):
        from broker import FutuAPIError
        from broker.market_data import ThrottledHistoryClient as _ThrottledHistoryClient

        raw = Mock()
        raw.get_history_kline.side_effect = [
            FutuAPIError("rate limit exceeded"), FutuAPIError("rate limit exceeded"),
            {"kline_list": []},
            FutuAPIError("rate limit exceeded"), {"kline_list": []},
            {"kline_list": []},
        ]
        client = _ThrottledHistoryClient(raw)
        kwargs = {"ktype": 2, "autype": 0, "num": 20, "extended_time": 0}
        with (
            patch("momentum.time.sleep") as sleep,
            patch("momentum.time.monotonic", return_value=100),
            redirect_stderr(StringIO()) as stderr,
        ):
            client.get_history_kline("US.NVDA", "2026-10-16", **kwargs)
            self.assertEqual(client.retry_count, 1)
            self.assertEqual(client.interval, 2.5)
            client.get_history_kline("US.AAPL", "2026-10-16", **kwargs)
            self.assertEqual(client.interval, 2.5)
            client.get_history_kline("US.MSFT", "2026-10-16", **kwargs)
            self.assertEqual(client.retry_count, 0)
            self.assertEqual(client.interval, 2.0)
            raw.get_history_kline.side_effect = None
            raw.get_history_kline.return_value = {"kline_list": []}
            client.get_history_kline("US.MSFT", "2026-10-16", **kwargs)
            self.assertEqual(client.retry_count, 0)
        sleep.assert_any_call(2)
        sleep.assert_any_call(4)
        sleep.assert_any_call(2.5)
        self.assertIn("retry_count=2; subsequent request interval=3s", stderr.getvalue())

    def test_request_interval_is_configurable_and_validated(self):
        from broker.market_data import ThrottledHistoryClient as _ThrottledHistoryClient

        client = _ThrottledHistoryClient(Mock(), interval=5)
        self.assertEqual(client.interval, 5)
        for value in ("0", "-1", "nan", "inf"):
            with (
                self.subTest(value=value),
                patch("momentum._credentials_client") as factory,
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                main([
                    "--tradingview", "--as-of", "2026-10-16",
                    "--request-interval", value,
                ])
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()

    def test_retry_interval_uses_custom_initial_interval(self):
        from broker import FutuAPIError
        from broker.market_data import ThrottledHistoryClient

        raw = Mock()
        raw.get_history_kline.side_effect = [
            FutuAPIError("rate limit exceeded"), {"kline_list": []},
            {"kline_list": []},
        ]
        client = ThrottledHistoryClient(raw, interval=5)
        with (
            patch("broker.market_data.time.sleep") as sleep,
            patch("broker.market_data.time.monotonic", return_value=100),
            redirect_stderr(StringIO()),
        ):
            for symbol in ("US.AAA", "US.BBB"):
                client.get_history_kline(
                    symbol, "2026-10-16", ktype=2, autype=0, num=20, extended_time=0,
                )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5.5, 5.0])
        self.assertEqual(client.retry_count, 0)
        self.assertEqual(client.initial_interval, 5)
        self.assertEqual(client.interval, 5)

    def test_history_rate_limits_retry_but_other_errors_do_not(self):
        from broker import FutuAPIError
        from broker.market_data import ThrottledHistoryClient as _ThrottledHistoryClient

        kwargs = {"ktype": 2, "autype": 0, "num": 20, "extended_time": 0}
        for error in (
            FutuAPIError("rate limit exceeded"),
            FutuAPIError("Too many requests", status=429),
        ):
            with self.subTest(error=str(error)):
                raw = Mock()
                raw.get_history_kline.side_effect = [error, {"kline_list": []}]
                with (
                    patch("momentum.time.sleep") as sleep,
                    patch("momentum.time.monotonic", return_value=100),
                    redirect_stderr(StringIO()) as stderr,
                ):
                    result = _ThrottledHistoryClient(raw).get_history_kline(
                        "US.NVDA", "2026-10-16", **kwargs,
                    )
                self.assertEqual(result, {"kline_list": []})
                sleep.assert_any_call(2)
                self.assertIn("retry 1/4", stderr.getvalue())
                self.assertEqual(raw.get_history_kline.call_count, 2)

        for error, expected_calls in (
            (FutuAPIError("rate limit exceeded"), 5),
            (FutuAPIError("permission denied"), 1),
        ):
            raw = Mock()
            raw.get_history_kline.side_effect = error
            with (
                patch("momentum.time.sleep"),
                redirect_stderr(StringIO()),
                self.assertRaises(FutuAPIError),
            ):
                _ThrottledHistoryClient(raw).get_history_kline(
                    "US.NVDA", "2026-10-16", **kwargs,
                )
            self.assertEqual(raw.get_history_kline.call_count, expected_calls)

    def test_rate_limit_retry_delays_and_interval_increments(self):
        from broker import FutuAPIError
        from broker.market_data import ThrottledHistoryClient

        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                raw = Mock()
                raw.get_history_kline.side_effect = [
                    FutuAPIError("rate limit exceeded") for _ in range(4)
                ] + ([{"kline_list": []}] if succeeds else [FutuAPIError("rate limit exceeded")])
                client = ThrottledHistoryClient(raw)
                with (
                    patch("broker.market_data.time.sleep") as sleep,
                    patch("broker.market_data.time.monotonic", return_value=100),
                    redirect_stderr(StringIO()) as stderr,
                ):
                    if succeeds:
                        self.assertEqual(client.get_history_kline(
                            "US.NVDA", "2026-10-16",
                            ktype=2, autype=0, num=20, extended_time=0,
                        ), {"kline_list": []})
                    else:
                        with self.assertRaises(FutuAPIError):
                            client.get_history_kline(
                                "US.NVDA", "2026-10-16",
                                ktype=2, autype=0, num=20, extended_time=0,
                            )
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list],
                    [2, 2.5, 4, 3.0, 6, 3.5, 8, 4.0],
                )
                self.assertEqual(client.retry_count, 3 if succeeds else 5)
                self.assertEqual(client.interval, 3.5 if succeeds else 4.5)
                self.assertEqual(raw.get_history_kline.call_count, 5)
                for attempt, wait in enumerate((2, 4, 6, 8), 1):
                    self.assertIn(f"retry {attempt}/4 after {wait}s", stderr.getvalue())

    def test_tradingview_cli_builds_universe_without_csv_paths(self):
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(253)]
        securities = [Security("AAA", "NYSE", "common_stock", 1000)]
        bars = {"AAA": {day: DailyBar(10, 100) for day in days[-2:]}}
        prices = {"AAA": {day: 100 + index for index, day in enumerate(days)}}
        output = StringIO()
        with (
            patch("momentum._credentials_client", return_value=object()),
            patch("momentum.fetch_tradingview_securities", return_value=securities),
            patch("momentum.fetch_futu_daily_bars", return_value=bars),
            patch("momentum.fetch_futu_prices", return_value=prices),
            redirect_stdout(output),
        ):
            self.assertEqual(main([
                "--tradingview",
                "--format", "json",
                "--as-of", days[-1].isoformat(),
                "--adv-days", "2", "--market-cap-top", "1", "--adv-top", "1",
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["universe_source"], "TradingView screener + Futu daily bars")
        self.assertEqual(result["dynamic_universe"]["eligible_count"], 1)
        self.assertEqual(result["candidates"][0]["symbol"], "AAA")
        self.assertEqual(result["config"]["lookback"], 21)
        self.assertEqual(result["config"]["skip"], 0)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["start_date"], days[-22].isoformat())
        self.assertEqual(candidate["end_date"], days[-1].isoformat())
        self.assertAlmostEqual(candidate["raw_return"], 352 / 331 - 1)

    def test_future_prices_and_non_constituents_cannot_change_result(self):
        before = self.run_screen()
        self.prices["AAA"][self.days[-1] + timedelta(days=1)] = 999999
        self.prices["OTHER"] = {date(2020, 1, 1): 1}
        self.assertEqual(before, self.run_screen())

    def test_rejects_future_holdings_insufficient_history_and_stale_date(self):
        for day in (self.days[0] - timedelta(days=1), self.days[3],
                    self.days[-1] + timedelta(days=1)):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.run_screen(as_of=day)

    def test_skip_zero_includes_as_of_price(self):
        result = self.run_screen(config=MomentumConfig(5, 0, 20))
        self.assertEqual(result["candidates"][0]["symbol"], "BBB")
        by_symbol = {row["symbol"]: row for row in result["candidates"]}
        self.assertAlmostEqual(by_symbol["BBB"]["raw_return"], 9.0)

    def test_invalid_config_rejected(self):
        for args in ((0, 0, 1), (5, 5, 1), (5, -1, 1),
                     (5, 1, 0), (5.5, 1, 1), (True, 0, 1), (5, 1, 1, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                MomentumConfig(*args)

    def test_invalid_prices_duplicate_and_malformed_csv_rejected(self):
        for rows in (
            "", "2026-01-01,AAA,0\n", "2026-01-01,AAA,-1\n",
            "2026-01-01,AAA,nan\n", "2026-01-01,AAA,inf\n",
            "2026-01-01,AAA,100\n2026-01-01,US.AAA,101\n",
            "2026-01-01,AAA\n", "2026-01-01,AAA,100,extra\n",
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                load_prices(self.write("prices.csv", "date,symbol,adjusted_close\n" + rows))
        with self.assertRaises(ValueError):
            load_prices(self.write("missing.csv", "date,symbol,close\n2026-01-01,AAA,1\n"))

    def test_cli_rejects_removed_holdings_and_prices_options(self):
        for option in ("--holdings", "--prices"):
            with self.subTest(option=option):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                    main([option, "unused.csv", "--as-of", "2026-01-05"])
                self.assertEqual(error.exception.code, 2)

    def test_cli_dynamic_options_are_validated_before_network(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(["--tradingview", "--as-of", "2026-01-05", "--lookback", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_requires_tradingview_mode(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(["--as-of", "2026-01-05"])
        self.assertEqual(error.exception.code, 2)

    def test_import_does_not_load_broker_or_risk_modules(self):
        process = subprocess.run(
            [
                sys.executable, "-c",
                "import sys; import momentum; "
                "assert not any(name.split('.')[0] in "
                "{'broker', 'risk_parity', 'FUTU', 'RFO', "
                "'tradingview_screener'} "
                "for name in sys.modules)",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(process.stdout, "")


if __name__ == "__main__":
    unittest.main()

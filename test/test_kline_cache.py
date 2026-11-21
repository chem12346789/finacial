"""Offline persistence and incremental daily K-line cache tests."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from broker.market_data import CachedHistoryClient, ThrottledHistoryClient
from momentum import fetch_futu_daily_bars, fetch_futu_prices, main, Security, Universe


def bars(*days, close=10):
    return {"kline_list": [
        {"time_key": day.isoformat(), "close": close, "volume": 100}
        for day in days
    ]}


class KlineCacheTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "nested" / "kline.sqlite3"
        output = patch("momentum.DEFAULT_MOMENTUM_PATH", Path(temp.name) / "momentum.json")
        output.start()
        self.addCleanup(output.stop)
        self.raw = Mock()
        self.client = CachedHistoryClient(self.raw, self.path)
        self.day = date(2020, 1, 10)
        self.kwargs = dict(ktype=2, autype=0, num=20, extended_time=0)

    def fetch(self, day=None, **kwargs):
        return self.client.get_history_kline(
            "US.AAA", (day or self.day).isoformat(), **{**self.kwargs, **kwargs},
        )

    def test_persists_across_clients_and_reuses_larger_window(self):
        days = [self.day - timedelta(days=i) for i in range(20)]
        self.raw.get_history_kline.return_value = bars(*reversed(days))
        expected = self.fetch()
        self.client = CachedHistoryClient(self.raw, self.path)
        self.assertEqual(self.fetch(), expected)
        self.assertEqual(self.fetch(num=2), bars(*sorted(days)[-2:]))
        self.raw.get_history_kline.assert_called_once()

    def test_incremental_request_uses_start_and_only_small_missing_window(self):
        self.raw.get_history_kline.return_value = bars(
            self.day - timedelta(days=1), self.day,
        )
        self.fetch()
        next_day = self.day + timedelta(days=3)
        self.raw.get_history_kline.return_value = bars(self.day, next_day)
        result = self.fetch(next_day)
        self.raw.get_history_kline.assert_called_with(
            "US.AAA", next_day.isoformat(), **{**self.kwargs, "num": 4},
            start=self.day.isoformat(),
        )
        self.assertEqual(result, bars(self.day - timedelta(days=1), self.day, next_day))
        self.assertEqual(self.fetch(next_day), result)
        self.assertEqual(self.raw.get_history_kline.call_count, 2)

    def test_weekend_empty_increment_preserves_history_and_is_cached(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch()
        sunday = self.day + timedelta(days=2)
        self.assertEqual(self.fetch(sunday), bars(self.day))
        self.assertEqual(self.fetch(sunday), bars(self.day))
        self.assertEqual(self.raw.get_history_kline.call_count, 2)

    def test_adjustment_basis_change_refetches_full_qfq_window(self):
        self.raw.get_history_kline.return_value = bars(
            self.day - timedelta(days=1), self.day,
        )
        self.fetch(autype=1)
        next_day = self.day + timedelta(days=1)
        refreshed = bars(self.day - timedelta(days=1), self.day, next_day, close=5)
        self.raw.get_history_kline.side_effect = [
            bars(self.day, next_day, close=5), refreshed,
        ]
        self.assertEqual(self.fetch(next_day, autype=1), refreshed)
        self.raw.get_history_kline.assert_called_with(
            "US.AAA", next_day.isoformat(), **{**self.kwargs, "autype": 1},
        )
        self.assertEqual(self.fetch(next_day, autype=1), refreshed)
        self.assertEqual(self.raw.get_history_kline.call_count, 3)

    def test_stable_qfq_increment_trims_to_requested_window(self):
        days = [self.day - timedelta(days=i) for i in range(5)]
        self.raw.get_history_kline.return_value = bars(*sorted(days))
        self.fetch(autype=1, num=5)
        next_day = self.day + timedelta(days=1)
        self.raw.get_history_kline.return_value = bars(self.day, next_day)
        result = self.fetch(next_day, autype=1, num=5)
        self.assertEqual(result, bars(*sorted(days)[1:], next_day))
        self.raw.get_history_kline.assert_called_with(
            "US.AAA", next_day.isoformat(),
            **{**self.kwargs, "autype": 1, "num": 2}, start=self.day.isoformat(),
        )

    def test_raw_overlap_is_updated_without_replacing_older_bars(self):
        previous = self.day - timedelta(days=1)
        self.raw.get_history_kline.return_value = bars(previous, self.day)
        self.fetch()
        next_day = self.day + timedelta(days=1)
        self.raw.get_history_kline.return_value = bars(self.day, next_day, close=12)
        result = self.fetch(next_day)
        self.assertEqual(result["kline_list"], [
            *bars(previous)["kline_list"],
            *bars(self.day, next_day, close=12)["kline_list"],
        ])
        self.assertEqual(self.raw.get_history_kline.call_count, 2)

    def test_missing_anchor_refetches_full_window(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch()
        next_day = self.day + timedelta(days=1)
        self.raw.get_history_kline.side_effect = [
            bars(next_day), bars(self.day, next_day),
        ]
        self.assertEqual(self.fetch(next_day), bars(self.day, next_day))
        self.assertEqual(self.raw.get_history_kline.call_count, 3)
        self.raw.get_history_kline.assert_called_with(
            "US.AAA", next_day.isoformat(), **self.kwargs,
        )

    def test_larger_window_earlier_end_and_large_gap_fetch_full_windows(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch()
        for day, num in (
            (self.day, 370),
            (self.day - timedelta(days=1), 20),
            (self.day + timedelta(days=400), 20),
        ):
            with self.subTest(day=day, num=num):
                self.raw.get_history_kline.return_value = bars(day)
                self.fetch(day, num=num)
                self.raw.get_history_kline.assert_called_with(
                    "US.AAA", day.isoformat(), **{**self.kwargs, "num": num},
                )
        self.assertEqual(self.raw.get_history_kline.call_count, 4)

    def test_cache_separates_symbols_adjustment_and_sessions(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch()
        self.fetch(autype=1)
        self.fetch(extended_time=1)
        self.client.get_history_kline("US.BBB", self.day.isoformat(), **self.kwargs)
        self.assertEqual(self.raw.get_history_kline.call_count, 4)

    def test_current_and_future_dates_save_snapshots_without_reusing_them(self):
        today = datetime.now(ZoneInfo("America/New_York")).date()
        for day in (today, today + timedelta(days=1)):
            with self.subTest(day=day):
                self.raw.get_history_kline.return_value = bars(day)
                self.fetch(day)
                self.fetch(day)
        self.assertEqual(self.raw.get_history_kline.call_count, 4)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM daily_kline_range_cache",
            ).fetchone()[0], 2)

    def test_explicit_ranges_are_cached_separately_and_other_intervals_bypass(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch()
        self.fetch(start="2020-01-01")
        self.raw.get_history_kline.assert_called_with(
            "US.AAA", self.day.isoformat(), start="2020-01-01", **self.kwargs,
        )
        self.fetch(start="2020-01-01")
        self.fetch(ktype=1)
        self.assertEqual(self.raw.get_history_kline.call_count, 3)

    def test_range_is_committed_before_next_request_and_survives_failure(self):
        def request(code, end, **kwargs):
            if code == "US.BBB":
                with sqlite3.connect(self.path) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT count(*) FROM daily_kline_range_cache",
                    ).fetchone()[0], 1)
                raise RuntimeError("download failed")
            return bars(self.day)

        self.raw.get_history_kline.side_effect = request
        from broker.market_data import fetch_daily_klines
        with self.assertRaisesRegex(RuntimeError, "download failed"):
            fetch_daily_klines(
                self.client, ["AAA", "BBB"], start=self.day, end=self.day,
                autype=1, num=370,
            )
        restarted = CachedHistoryClient(self.raw, self.path)
        self.raw.get_history_kline.reset_mock()
        fetch_daily_klines(
            restarted, ["AAA"], start=self.day, end=self.day, autype=1, num=370,
        )
        self.raw.get_history_kline.assert_not_called()

    def test_range_refresh_and_invalid_response_preserve_previous_cache(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch(start="2020-01-01")
        refresh = CachedHistoryClient(self.raw, self.path, refresh=True)
        self.raw.get_history_kline.return_value = bars(self.day, close=20)
        refresh.get_history_kline(
            "US.AAA", self.day.isoformat(), start="2020-01-01", **self.kwargs,
        )
        self.assertEqual(self.fetch(start="2020-01-01")["kline_list"][0]["close"], 20)
        self.raw.get_history_kline.return_value = bars(self.day, close=-1)
        with self.assertRaises(ValueError):
            refresh.get_history_kline(
                "US.AAA", self.day.isoformat(), start="2020-01-01", **self.kwargs,
            )
        self.assertEqual(self.fetch(start="2020-01-01")["kline_list"][0]["close"], 20)

    def test_live_range_is_saved_but_never_reused_after_close(self):
        today = datetime.now(ZoneInfo("America/New_York")).date()
        self.raw.get_history_kline.return_value = bars(today)
        self.fetch(day=today, start=today.isoformat())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM daily_kline_range_cache",
            ).fetchone()[0], 1)
        self.fetch(day=today, start=today.isoformat())
        with patch("broker.market_data.datetime") as clock:
            clock.now.return_value.date.return_value = today + timedelta(days=1)
            self.fetch(day=today, start=today.isoformat())
            self.fetch(day=today, start=today.isoformat())
        self.assertEqual(self.raw.get_history_kline.call_count, 3)

    def test_completed_symbols_are_preserved_after_later_download_failure(self):
        securities = [
            Security(symbol, "NASDAQ", "common_stock", 1000)
            for symbol in ("AAA", "BBB")
        ]
        self.raw.get_history_kline.side_effect = [
            bars(self.day), RuntimeError("download failed"),
        ]
        with self.assertRaisesRegex(RuntimeError, "download failed"):
            fetch_futu_daily_bars(self.client, securities, as_of=self.day, num=20)
        self.client = CachedHistoryClient(self.raw, self.path)
        self.raw.get_history_kline.side_effect = None
        self.raw.get_history_kline.return_value = bars(self.day)
        result = fetch_futu_daily_bars(self.client, securities, as_of=self.day, num=20)
        self.assertEqual(set(result), {"AAA", "BBB"})
        self.assertEqual(self.raw.get_history_kline.call_count, 3)
        self.assertEqual(self.raw.get_history_kline.call_args.args[0], "US.BBB")

    def test_refresh_replaces_existing_cached_response(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.fetch()
        refreshed = bars(self.day, close=12)
        self.raw.get_history_kline.return_value = refreshed
        refresh_client = CachedHistoryClient(self.raw, self.path, refresh=True)
        self.assertEqual(refresh_client.get_history_kline(
            "US.AAA", self.day.isoformat(), **self.kwargs,
        ), refreshed)
        self.assertEqual(self.fetch(), refreshed)
        self.assertEqual(self.raw.get_history_kline.call_count, 2)

    def test_failures_and_invalid_responses_are_not_cached(self):
        for response in (
            None, {"kline_list": [None]}, bars(self.day, self.day),
            bars(self.day, close=float("nan")), {"kline_list": [{"close": 1}]},
        ):
            with self.subTest(response=response):
                self.raw.get_history_kline.return_value = response
                with self.assertRaises(ValueError):
                    self.fetch()
        self.raw.get_history_kline.side_effect = RuntimeError("download failed")
        with self.assertRaisesRegex(RuntimeError, "download failed"):
            self.fetch()
        self.raw.get_history_kline.side_effect = None
        self.raw.get_history_kline.return_value = bars(self.day)
        self.assertEqual(self.fetch(), bars(self.day))
        self.assertEqual(self.raw.get_history_kline.call_count, 7)

    def test_fetch_paths_reuse_cache_without_network_or_throttle_sleep(self):
        self.raw.get_history_kline.return_value = bars(self.day)
        self.client = CachedHistoryClient(ThrottledHistoryClient(self.raw), self.path)
        security = Security("AAA", "NASDAQ", "common_stock", 1000)
        universe = Universe(self.day, {"AAA": ("NASDAQ",)})
        with patch("momentum.time.sleep"):
            expected_bars = fetch_futu_daily_bars(
                self.client, [security], as_of=self.day, num=20,
            )
            expected_prices = fetch_futu_prices(self.client, universe, as_of=self.day)
        with patch("momentum.time.sleep") as sleep:
            self.assertEqual(fetch_futu_daily_bars(
                self.client, [security], as_of=self.day, num=20,
            ), expected_bars)
            self.assertEqual(fetch_futu_prices(
                self.client, universe, as_of=self.day,
            ), expected_prices)
            sleep.assert_not_called()
        self.assertEqual(self.raw.get_history_kline.call_count, 2)

    def test_throttler_forwards_incremental_start(self):
        client = ThrottledHistoryClient(self.raw)
        client.get_history_kline(
            "US.AAA", self.day.isoformat(), start="2020-01-09", **self.kwargs,
        )
        self.raw.get_history_kline.assert_called_once_with(
            "US.AAA", self.day.isoformat(), start="2020-01-09", **self.kwargs,
        )

    def test_cli_second_run_uses_persisted_bars_without_futu_requests(self):
        days = [self.day - timedelta(days=i) for i in reversed(range(3))]
        response = bars(*days)
        for index, bar in enumerate(response["kline_list"]):
            bar["close"] += index
        self.raw.get_history_kline.return_value = response
        options = [
            "--tradingview", "--as-of", self.day.isoformat(),
            "--cache-path", str(self.path), "--adv-days", "2",
            "--lookback", "2", "--skip", "0",
        ]
        with (
            patch("momentum._credentials_client", return_value=self.raw),
            patch("momentum.fetch_tradingview_securities", return_value=[
                Security("AAA", "NASDAQ", "common_stock", 1000),
            ]),
            patch("momentum.time.sleep"),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(options), 0)
            self.assertEqual(self.raw.get_history_kline.call_count, 2)
            self.raw.get_history_kline.side_effect = AssertionError("unexpected network")
            self.assertEqual(main(options), 0)
        self.assertEqual(self.raw.get_history_kline.call_count, 2)

    def test_cli_default_cache_is_project_local_from_another_directory(self):
        with (
            chdir(self.path.parent),
            patch("momentum._credentials_client", return_value=Mock()),
            patch("momentum.fetch_tradingview_securities", return_value=[]),
            patch("broker.market_data.CachedHistoryClient") as cache,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["--tradingview", "--as-of", self.day.isoformat()])
        self.assertEqual(
            cache.call_args.args[1],
            Path(__file__).resolve().parents[1] / ".cache" / "finacial" / "kline.sqlite3",
        )

    def test_cli_cache_options_and_sqlite_errors(self):
        for options, cache_enabled in (
            ([], True), (["--no-cache"], False), (["--refresh-cache"], True),
        ):
            with (
                self.subTest(options=options),
                patch("momentum._credentials_client", return_value=Mock()),
                patch("momentum.fetch_tradingview_securities", return_value=[]),
                patch("broker.market_data.CachedHistoryClient") as cache,
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit),
            ):
                main([
                    "--tradingview", "--as-of", self.day.isoformat(),
                    "--cache-path", str(self.path), *options,
                ])
                self.assertEqual(cache.called, cache_enabled)
                if cache_enabled:
                    self.assertEqual(cache.call_args.kwargs["refresh"],
                                     "--refresh-cache" in options)
        self.path.write_text("not a sqlite database", encoding="utf-8")
        with (
            patch("momentum._credentials_client", return_value=Mock()),
            redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as error,
        ):
            main([
                "--tradingview", "--as-of", self.day.isoformat(),
                "--cache-path", str(self.path),
            ])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("not a database", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

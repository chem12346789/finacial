"""Weekend-only screening and durable, week-bound execution inputs."""

from argparse import Namespace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from play_money_orders import load_weekend_allocation, prepare_weekend
from strategy_ledger import StrategyLedger


class WeekendInputsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = StrategyLedger(Path(directory.name) / "strategy.sqlite3").__enter__()
        self.addCleanup(self.ledger.__exit__)
        self.args = Namespace(as_of=None, lookback=252)
        self.allocation = {
            "as_of": "2026-10-23", "end_date": "2026-10-23",
            "annualized_volatility": 0.4, "weights": [{"symbol": "AAA", "weight": 1}],
        }

    def test_weekend_preparation_is_durable_and_same_day_idempotent(self):
        with patch("play_money_orders.screen_and_allocate", return_value=self.allocation) as screen:
            result = prepare_weekend(self.ledger, self.args, date(2026, 10, 24))
            prepare_weekend(self.ledger, self.args, date(2026, 10, 24))
            screen.assert_called_once_with("2026-10-23", 252)
        self.assertEqual(result["week"], "2026-10-26")
        path = self.ledger.path
        self.ledger.__exit__()
        with StrategyLedger(path) as reopened:
            self.assertEqual(load_weekend_allocation(reopened, "2026-10-26", self.args), self.allocation)

    def test_sunday_refreshes_saturdays_inputs(self):
        with patch("play_money_orders.screen_and_allocate", return_value=self.allocation) as screen:
            prepare_weekend(self.ledger, self.args, date(2026, 10, 24))
            prepare_weekend(self.ledger, self.args, date(2026, 10, 25))
            self.assertEqual(screen.call_count, 2)
        self.assertEqual(self.ledger.db.execute("SELECT prepared_at FROM weekly_inputs").fetchone()[0],
                         "2026-10-25")

    def test_weekday_preparation_and_wrong_asof_are_rejected(self):
        with patch("play_money_orders.screen_and_allocate") as screen:
            with self.assertRaisesRegex(ValueError, "weekend"):
                prepare_weekend(self.ledger, self.args, date(2026, 10, 26))
            self.args.as_of = "2026-10-22"
            with self.assertRaisesRegex(ValueError, "latest completed"):
                prepare_weekend(self.ledger, self.args, date(2026, 10, 24))
            screen.assert_not_called()

    def test_incomplete_or_failed_data_never_replaces_saved_input(self):
        self.ledger.save_inputs("2026-10-26", "2026-10-24", 252, self.allocation)
        bad = {**self.allocation, "end_date": "2026-10-22"}
        with patch("play_money_orders.screen_and_allocate", return_value=bad):
            with self.assertRaisesRegex(ValueError, "latest completed"):
                prepare_weekend(self.ledger, self.args, date(2026, 10, 25))
        self.assertEqual(load_weekend_allocation(self.ledger, "2026-10-26", self.args), self.allocation)

    def test_missing_wrong_week_and_changed_settings_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "No weekend inputs"):
            load_weekend_allocation(self.ledger, "2026-10-26", self.args)
        self.ledger.save_inputs("2026-10-26", "2026-10-18", 252, self.allocation)
        with self.assertRaisesRegex(ValueError, "preceding weekend"):
            load_weekend_allocation(self.ledger, "2026-10-26", self.args)
        self.ledger.save_inputs("2026-10-26", "2026-10-24", 252, self.allocation)
        for args in (Namespace(as_of=None, lookback=63), Namespace(as_of="2026-10-22", lookback=252)):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "settings differ"):
                load_weekend_allocation(self.ledger, "2026-10-26", args)

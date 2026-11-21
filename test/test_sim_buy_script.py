"""Offline tests for the one-shot simulation buy diagnostic script."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import MagicMock, patch

from broker import SimTradingClient, TokenSet
from broker.client import FutuAPIError
import test_sim_buy


class SimBuyScriptTest(unittest.TestCase):
    def setUp(self):
        self.client = SimTradingClient(TokenSet("offline"), account_id="private", market=100)
        self.output, self.errors = StringIO(), StringIO()
        self.quotes = MagicMock()
        self.quotes.get_market_snapshot.return_value = {
            "snapshot_list": [{"code": "US.HAS", "last_price": "100.01"}],
        }
        patches = (
            patch("test_sim_buy.get_store", return_value=MagicMock(
                sim_client=MagicMock(return_value=self.client))),
            patch.object(self.client, "select_account"),
            patch.object(self.client, "_request"),
            patch("test_sim_buy.get_default_quote_client", return_value=self.quotes),
            patch("broker.client._OPENER.open", side_effect=AssertionError("No network")),
            redirect_stdout(self.output), redirect_stderr(self.errors),
        )
        for context in patches:
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.client._request.return_value = {"max_cash_buy_qty_round_lot": "8"}

    def test_preview_does_not_submit_or_query_capacity(self):
        self.assertEqual(test_sim_buy.main(["--price", "100"]), 0)
        self.client._request.assert_not_called()
        self.assertIn('"symbol": "HAS"', self.output.getvalue())
        self.assertNotIn("private", self.output.getvalue())

    def test_invalid_price_never_submits(self):
        self.assertEqual(test_sim_buy.main(["--submit", "--price", "0"]), 1)
        self.client._request.assert_not_called()

    def test_automatic_quote_premium_rounds_up_to_cents(self):
        self.assertEqual(test_sim_buy.main([]), 0)
        self.quotes.get_market_snapshot.assert_called_once_with(["US.HAS"])
        self.assertIn('"price": "101.02"', self.output.getvalue())
        self.client._request.assert_not_called()

    def test_two_percent_premium_is_used_for_capacity_and_submission(self):
        self.client._request.side_effect = [
            {"max_cash_buy_qty_round_lot": "8"}, {"order_id": "test-order"},
        ]
        self.assertEqual(test_sim_buy.main(["--submit", "--premium-percent", "2"]), 0)
        calls = self.client._request.call_args_list
        self.assertEqual(calls[0].kwargs["query"]["price"], "102.02")
        self.assertEqual(calls[1].kwargs["body"]["price"], "102.02")

    def test_missing_quote_blocks_submission(self):
        self.quotes.get_market_snapshot.return_value = {"snapshot_list": []}
        self.assertEqual(test_sim_buy.main(["--submit"]), 1)
        self.client._request.assert_not_called()

    def test_submit_sends_exactly_one_buy_limit_with_price(self):
        self.client._request.side_effect = [
            {"max_cash_buy_qty_round_lot": "8"}, {"order_id": "test-order"},
        ]
        self.assertEqual(test_sim_buy.main(["--submit", "--price", "100"]), 0)
        self.assertEqual(self.client._request.call_count, 2)
        call = self.client._request.call_args
        self.assertEqual(call.args[0], "POST")
        body = call.kwargs["body"]
        self.assertEqual({k: v for k, v in body.items() if k != "text"}, {
            "market": 100, "symbol": "HAS", "order_type": 1, "order_side": 1, "qty": "8",
            "price": "100",
        })
        self.assertTrue(body["text"].startswith("test:"))

    def test_error_prints_diagnostics_without_retry(self):
        self.client._request.side_effect = [
            {"max_cash_buy_qty_round_lot": "8"},
            FutuAPIError("backend business error", code=-5, status=200,
                         details={"access_token": "secret-value"}),
        ]
        self.assertEqual(test_sim_buy.main(["--submit", "--price", "100"]), 1)
        self.assertEqual(self.client._request.call_count, 2)
        self.assertIn('"code": -5', self.errors.getvalue())
        self.assertIn("No retry", self.errors.getvalue())
        self.assertNotIn("secret-value", self.errors.getvalue())

    def test_insufficient_cash_capacity_prevents_submission(self):
        self.client._request.return_value = {"max_cash_buy_qty_round_lot": "7"}
        self.assertEqual(test_sim_buy.main(["--submit", "--price", "100"]), 1)
        self.client._request.assert_called_once()
        self.assertIn("Stopped before submission", self.errors.getvalue())

    def test_nested_broker_error_values_are_hidden_by_default(self):
        self.client._request.side_effect = [
            {"max_cash_buy_qty_round_lot": "8"},
            FutuAPIError("backend business error", code=-5, details={
                "error": {"code": "INTERNAL_TEST_CODE", "message": "private-error-message"},
            }),
        ]
        self.assertEqual(test_sim_buy.main(["--submit", "--price", "100"]), 1)
        self.assertNotIn("INTERNAL_TEST_CODE", self.errors.getvalue())
        self.assertNotIn("private-error-message", self.errors.getvalue())

    def test_opt_in_nested_error_details_only_print_code_and_message(self):
        self.client._request.side_effect = [
            {"max_cash_buy_qty_round_lot": "8"},
            FutuAPIError("backend business error", code=-5, details={
                "access_token": "top-level-secret",
                "error": {"code": "INTERNAL_TEST_CODE", "message": "private-error-message",
                          "token": "nested-secret"},
            }),
        ]
        self.assertEqual(test_sim_buy.main([
            "--submit", "--price", "100", "--show-error-details",
        ]), 1)
        self.assertEqual(self.client._request.call_count, 2)
        self.assertIn("INTERNAL_TEST_CODE", self.errors.getvalue())
        self.assertIn("private-error-message", self.errors.getvalue())
        self.assertIn("Redact before sharing", self.errors.getvalue())
        self.assertNotIn("top-level-secret", self.errors.getvalue())
        self.assertNotIn("nested-secret", self.errors.getvalue())

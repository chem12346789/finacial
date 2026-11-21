"""Deterministic Futu HK fixed-plan whole-share estimates."""

from decimal import Decimal
import unittest

from us_stock_fees import estimate_sim_order_fees, estimate_us_stock_fees


class StockFeesTest(unittest.TestCase):
    def test_simulated_fee_is_flat_per_order_not_per_share(self):
        for side in ("BUY", "SELL"):
            for qty in ("1", "8", "1000"):
                fee = estimate_sim_order_fees(Decimal(qty), Decimal(qty) * 100, side)
                self.assertEqual(fee["total"], "0.99")
                self.assertEqual(fee["platform"], "0.00")
        self.assertEqual(estimate_sim_order_fees(Decimal(0), Decimal(0), "BUY")["total"], "0.00")

    def fees(self, qty, notional, side="BUY"):
        return estimate_us_stock_fees(Decimal(qty), Decimal(notional), side)

    def test_small_buy_and_sell_include_minimums_and_cent_rounding(self):
        self.assertEqual(self.fees("2", "478.17"), {
            "commission": "0.99", "platform": "1.00", "settlement": "0.01",
            "sec": "0.00", "taf": "0.00", "cat": "0.01", "total": "2.01",
        })
        self.assertEqual(self.fees("2", "478.17", "SELL")["total"], "2.03")

    def test_large_order_per_share_fees(self):
        fees = self.fees("1000", "10000", "SELL")
        self.assertEqual(fees, {
            "commission": "4.90", "platform": "5.00", "settlement": "3.00",
            "sec": "0.21", "taf": "0.20", "cat": "0.01", "total": "13.32",
        })

    def test_low_price_cap_and_minimum_precedence(self):
        self.assertEqual(self.fees("1000", "100")["commission"], "0.99")
        self.assertEqual(self.fees("1000", "100")["platform"], "1.00")
        self.assertEqual(self.fees("1000", "500")["commission"], "2.50")
        self.assertEqual(self.fees("1000", "500")["platform"], "2.50")

    def test_taf_cap_and_zero_order(self):
        self.assertEqual(self.fees("100000", "1000000", "SELL")["taf"], "9.79")
        self.assertEqual(self.fees("0", "0")["total"], "0.00")

    def test_invalid_inputs_are_explicit_errors(self):
        for qty, value, side in (
            ("-1", "10", "BUY"), ("NaN", "10", "BUY"), ("1", "Infinity", "BUY"),
            ("0.5", "10", "BUY"), ("0", "10", "BUY"), ("1", "0", "BUY"), ("1", "10", "HOLD"),
        ):
            with self.subTest(qty=qty, value=value, side=side), self.assertRaises(ValueError):
                self.fees(qty, value, side)

"""Offline budget, workflow, routing and fail-closed submission checks."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import ANY, MagicMock, patch

from broker import FutuClient, SimTradingClient, TokenSet
from broker.client import FutuAPIError, FutuTransportError, OrderConfirmationRequired
import play_money_orders as orders
from play_money_orders import load_weekend_allocation
from strategy_ledger import StrategyLedger
from ouput.paths import output_path


class PlanFixture:
    def setUp(self):
        today = date.today().isoformat()
        self.allocation = {
            "as_of": today, "end_date": today,
            "annualized_volatility": 1.0,
            "weights": [
                {"symbol": symbol, "weight": 0.2}
                for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE")
            ],
        }
        self.snapshots = {
            "snapshot_list": [
                {"code": f'US.{row["symbol"]}', "last_price": "100"}
                for row in self.allocation["weights"]
            ],
        }

    def plan(self, capital="3000", target="0.5", max_leverage="1.33"):
        return orders.build_plan(
            self.allocation, self.snapshots, Decimal(capital), Decimal(target),
            Decimal(max_leverage),
        )


class OrderPlanTest(PlanFixture, unittest.TestCase):
    def test_volatility_target_caps_exposure_at_3990(self):
        self.allocation["annualized_volatility"] = 0.25
        plan = self.plan()
        self.assertEqual(Decimal(plan["theoretical_leverage"]), Decimal("2"))
        self.assertEqual(Decimal(plan["target_leverage"]), Decimal("1.33"))
        self.assertTrue(plan["leverage_cap_hit"])
        self.assertEqual(Decimal(plan["investment_cap_usd"]), Decimal("3990"))
        self.assertLessEqual(Decimal(plan["order_notional_usd"]), Decimal("3990"))
        self.assertEqual(Decimal(plan["target_annualized_volatility_before_rounding"]),
                         Decimal("0.3325"))
        self.assertEqual(Decimal(plan["borrowing_before_fees_usd"]), Decimal("500"))
        self.assertEqual(Decimal(plan["remaining_cash_before_fees_usd"]), Decimal("0"))

    def test_volatility_target_can_leave_cash_or_use_uncapped_leverage(self):
        for forecast, leverage, investment in (("0.8", "0.625", "1875"), ("0.4", "1.25", "3750")):
            self.allocation["annualized_volatility"] = forecast
            plan = self.plan()
            with self.subTest(forecast=forecast):
                self.assertEqual(Decimal(plan["target_leverage"]), Decimal(leverage))
                self.assertEqual(Decimal(plan["investment_cap_usd"]), Decimal(investment))
                self.assertEqual(Decimal(plan["target_annualized_volatility_before_rounding"]),
                                 Decimal("0.5"))
                self.assertFalse(plan["leverage_cap_hit"])

    def test_cap_threshold_and_exact_equality(self):
        for forecast, hit in (("0.3759", True), ("0.3760", False)):
            self.allocation["annualized_volatility"] = forecast
            self.assertEqual(self.plan()["leverage_cap_hit"], hit)
        self.allocation["annualized_volatility"] = "0.4"
        self.assertTrue(self.plan(target="0.532")["leverage_cap_hit"])

    def test_invalid_forecast_and_leverage_fail(self):
        for forecast in (0, -1, "NaN", "Infinity", None):
            self.allocation["annualized_volatility"] = forecast
            with self.subTest(forecast=forecast), self.assertRaises(ValueError):
                self.plan()
        self.allocation["annualized_volatility"] = 0.5
        for leverage in ("0", "0.99", "1.34", "NaN", "Infinity"):
            with self.subTest(leverage=leverage), self.assertRaises(ValueError):
                self.plan(max_leverage=leverage)
        del self.allocation["annualized_volatility"]
        with self.assertRaises(KeyError):
            self.plan()

    def test_exact_budget_and_whole_shares(self):
        plan = self.plan()
        self.assertEqual(Decimal(plan["investment_cap_usd"]), Decimal("1500"))
        self.assertEqual(Decimal(plan["order_notional_usd"]), Decimal("1500"))
        self.assertEqual(Decimal(plan["remaining_cash_before_fees_usd"]), Decimal("1500"))
        self.assertEqual([row["qty"] for row in plan["orders"]], [3] * 5)

    def test_weighted_allocation_not_equal_cash(self):
        for row, weight in zip(self.allocation["weights"], (0.4, 0.3, 0.1, 0.1, 0.1)):
            row["weight"] = weight
        self.assertEqual([row["qty"] for row in self.plan()["orders"]], [6, 4, 1, 1, 1])

    def test_rounding_and_small_positions_never_exceed_cap(self):
        self.snapshots["snapshot_list"][0]["last_price"] = "500"
        self.snapshots["snapshot_list"][1]["last_price"] = "100.019"
        plan = self.plan()
        self.assertEqual(plan["orders"][0]["status"], "skipped_below_one_share")
        self.assertEqual(plan["orders"][1]["limit_price"], "100.01")
        self.assertEqual(plan["orders"][1]["qty"], 2)
        self.assertLessEqual(Decimal(plan["order_notional_usd"]), Decimal("1500"))

    def test_weight_roundoff_does_not_exceed_budget(self):
        self.allocation["weights"][0]["weight"] = 0.2000001
        self.assertLessEqual(Decimal(self.plan()["order_notional_usd"]), Decimal("1500"))

    def test_quote_rounding_cannot_exceed_marked_exposure_cap(self):
        self.allocation["annualized_volatility"] = "0.5"
        for quote in self.snapshots["snapshot_list"]:
            quote["last_price"] = "100.001"
        plan = self.plan(max_leverage="1")
        marked = sum(Decimal(row["qty"]) * Decimal("100.001") for row in plan["orders"])
        self.assertLessEqual(marked, Decimal(plan["investment_cap_usd"]))

    def test_invalid_quotes_fail(self):
        for value in ("0", "-1", "NaN", "Infinity", None, "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.snapshots["snapshot_list"][0]["last_price"] = value
                self.plan()

    def test_missing_or_duplicate_quotes_fail(self):
        original = deepcopy(self.snapshots)
        self.snapshots["snapshot_list"].pop()
        with self.assertRaisesRegex(ValueError, "Missing snapshot"):
            self.plan()
        self.snapshots = original
        self.snapshots["snapshot_list"].append(self.snapshots["snapshot_list"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate snapshot"):
            self.plan()

    def test_invalid_weights_and_symbols_fail(self):
        original = deepcopy(self.allocation)
        for value in (-0.2, 0.8, "NaN"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.allocation["weights"][0]["weight"] = value
                self.plan()
        for symbol in ("US.AAA", "BBB", "../bad"):
            self.allocation = deepcopy(original)
            self.allocation["weights"][0]["symbol"] = symbol
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                self.plan()

    def test_old_or_future_data_fail(self):
        today = date.today().isoformat()
        for field in ("as_of", "end_date"):
            for days in (-8, 1):
                self.allocation[field] = (date.today() + timedelta(days=days)).isoformat()
                with self.subTest(field=field, days=days), self.assertRaises(ValueError):
                    self.plan()
                self.allocation[field] = today

    def test_invalid_budget_fails(self):
        for capital, target in (("0", ".5"), ("-1", ".5"), ("3000", "0"),
                                ("3000", "1.01"), ("NaN", ".5"), ("3000", "Infinity")):
            with self.subTest(capital=capital, target=target), self.assertRaises(ValueError):
                self.plan(capital, target)


class OrderExecutionTest(PlanFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.journal = Path(directory.name) / "ledger.sqlite3"
        self.broker_orders = []
        self.quote = FutuClient(TokenSet("offline"), account_id="real")
        self.sim = SimTradingClient(TokenSet("offline"), account_id="sim", market=100)
        patches = {
            "screen": patch.object(orders, "screen_and_allocate", return_value=self.allocation),
            "inputs": patch.object(orders, "load_weekend_allocation", return_value=self.allocation),
            "opening": patch.object(orders, "at_market_open", return_value=True),
            "quote_factory": patch.object(orders, "get_default_quote_client", return_value=self.quote),
            "sim_factory": patch.object(orders, "get_store", return_value=MagicMock(sim_client=MagicMock(return_value=self.sim))),
            "quotes": patch.object(
                self.quote, "get_market_snapshot",
                side_effect=lambda codes: {"snapshot_list": [
                    {**row, "bid_price": row["last_price"], "ask_price": row["last_price"],
                     "update_time": int(datetime.now().timestamp() * 1000)}
                    for row in self.snapshots["snapshot_list"] if row["code"] in codes
                ]},
            ),
            "holding_risk": patch(
                "play_money_asset_risk_parity.calculate_holding_stop_risk",
                side_effect=lambda symbols, as_of, lookback: {
                    symbol: {
                        "annualized_volatility": 0.6,
                        "stop_fraction": 0.16903085094570333, "as_of": as_of.isoformat(),
                    } for symbol in symbols
                },
            ),
            "live_select": patch.object(self.quote, "select_account"),
            "sim_select": patch.object(self.sim, "select_account"),
            "sim_orders": patch.object(self.sim, "get_orders", return_value={"orders": self.broker_orders}),
            "sim_history": patch.object(self.sim, "iter_history_orders", return_value=[]),
            "sim_positions": patch.object(self.sim, "get_positions", return_value={"positions": []}),
            "live_funds": patch.object(
                self.quote, "get_funds", return_value={"us_cash": "3000", "us_net_cash_power": "3000"},
            ),
            "sim_funds": patch.object(
                self.sim, "get_funds", return_value={"balance": "3000", "hold": "0"},
            ),
            "live_capacity": patch.object(
                self.quote, "get_max_qty", return_value={"max_cash_buy": "30"},
            ),
            "sim_capacity": patch.object(
                self.sim, "get_max_qty", return_value={"max_cash_buy_qty_round_lot": "30"},
            ),
            "live_order": patch.object(self.quote, "place_order", return_value={"order_id": "real-1"}),
            "sim_order": patch.object(self.sim, "place_order", side_effect=self.broker_submit),
            "legacy": patch.object(orders, "DEFAULT_JOURNAL_PATH", Path(directory.name) / "legacy.jsonl"),
            "format": patch.object(orders, "format_order_result", wraps=orders.format_order_result),
            "clock": patch.object(orders, "new_york_today", return_value=date(2026, 10, 19)),
            "network": patch("broker.client._OPENER.open", side_effect=AssertionError("No network")),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.stdout = StringIO()
        self.stderr = StringIO()

    def broker_submit(self, symbol, qty, side, *, order_type, price, text):
        order_id = str(len(self.broker_orders) + 1)
        self.broker_orders.append({
            "order_id": order_id, "symbol": symbol, "side": 1 if side == "BUY" else 2,
            "qty": str(qty), "price": price if price is not None else "100.00",
            "order_type": 3 if order_type == "MARKET" else 1, "status": 2, "cum_qty": "0",
            "avg_fill_price": "0", "text": text, "update_time": "1",
        })
        return {"order_id": order_id}

    def events(self):
        with sqlite3.connect(self.journal) as db:
            return [
                {"event": kind, **json.loads(payload)}
                for kind, payload in db.execute("SELECT kind,payload FROM events ORDER BY id")
            ]

    def fill_all_orders(self):
        cash = Decimal("3000")
        quantities = {}
        for row in self.broker_orders:
            row.update(status=4, cum_qty=row["qty"], avg_fill_price=row["price"], update_time="2")
            qty = Decimal(row["qty"])
            sign = 1 if row["side"] == 1 else -1
            quantities[row["symbol"]] = quantities.get(row["symbol"], Decimal(0)) + sign * qty
            cash -= sign * qty * Decimal(row["price"])
            cash -= Decimal("0.99")
        self.mocks["sim_funds"].return_value = {
            "balance": str(cash), "hold": "0", "max_power_long": "6000",
        }
        self.mocks["sim_positions"].return_value = {"positions": [
            {"symbol": symbol, "qty": str(qty), "market": 100, "pstn_type": 0}
            for symbol, qty in quantities.items() if qty
        ]}

    def assert_no_intents(self):
        if self.journal.exists():
            with sqlite3.connect(self.journal) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM intents").fetchone()[0], 0)

    def run_main(self, *args):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return orders.main(["--account-id", "chosen", "--order-type", "LIMIT", *args])

    def execute(self):
        return self.run_main("--ledger", str(self.journal))

    def test_submission_is_default_and_never_writes_to_live_account(self):
        self.assertEqual(self.run_main("--mode", "sim", "--ledger", str(self.journal)), 0)
        self.mocks["live_order"].assert_not_called()
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        self.assertTrue(self.quote.dry_run)
        self.assertFalse(self.quote.allow_live_trading)

    def test_execution_quotes_use_ask_for_buy_and_bid_for_sell(self):
        snapshot = {"snapshot_list": [{
            "code": "US.AAA", "last_price": "100", "bid_price": "98.129",
            "ask_price": "101.231", "update_time": int(datetime.now().timestamp() * 1000),
        }]}
        for side, expected in (("BUY", "101.24"), ("SELL", "98.12")):
            self.assertEqual(orders.executable_prices(snapshot, [
                {"symbol": "AAA", "side": side},
            ])["AAA"], Decimal(expected))

    def test_quote_freshness_exact_boundary_and_invalid_market(self):
        row = {"code": "US.AAA", "last_price": "100", "bid_price": "99",
               "ask_price": "101", "update_time": 940000}
        with patch.object(orders, "datetime") as clock:
            clock.now.return_value.timestamp.return_value = 1000
            self.assertEqual(orders.executable_prices({"snapshot_list": [row]}, [
                {"symbol": "AAA", "side": "BUY"},
            ])["AAA"], Decimal("101.00"))
            for changed in (
                {"update_time": 939999}, {"update_time": 1000001},
                {"bid_price": "0"}, {"bid_price": "102"},
                {"ask_price": None}, {"update_time": None},
            ):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    orders.executable_prices({"snapshot_list": [{**row, **changed}]}, [
                        {"symbol": "AAA", "side": "BUY"},
                    ])

    def test_stale_execution_quote_prevents_intent_creation(self):
        self.mocks["quotes"].side_effect = lambda codes: {"snapshot_list": [
            {**row, "bid_price": "100", "ask_price": "100", "update_time": 1}
            for row in self.snapshots["snapshot_list"] if row["code"] in codes
        ]}
        self.assertEqual(self.execute(), 1)
        self.assertIn("stale", self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()
        with StrategyLedger(self.journal) as ledger:
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM intents").fetchone()[0], 0)

    def test_last_second_quote_move_stops_without_intent(self):
        order = {"symbol": "AAA", "side": "BUY", "limit_price": "99.00"}
        with self.assertRaisesRegex(ValueError, "beyond checked limit"):
            orders.submission_check(self.quote, order)

    def test_ask_price_is_used_for_capacity_submission_and_leverage(self):
        self.mocks["quotes"].side_effect = lambda codes: {"snapshot_list": [
            {**row, "bid_price": "99", "ask_price": "101",
             "update_time": int(datetime.now().timestamp() * 1000)}
            for row in self.snapshots["snapshot_list"] if row["code"] in codes
        ]}
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        for call in self.mocks["sim_order"].call_args_list:
            self.assertEqual(call.kwargs["price"], "101.00")
        for call in self.mocks["sim_capacity"].call_args_list:
            self.assertEqual(call.kwargs["price"], "101.00")

    def test_high_ask_breaching_exposure_cap_prevents_submission(self):
        self.mocks["quotes"].side_effect = lambda codes: {"snapshot_list": [
            {**row, "bid_price": "99", "ask_price": "300",
             "update_time": int(datetime.now().timestamp() * 1000)}
            for row in self.snapshots["snapshot_list"] if row["code"] in codes
        ]}
        self.assertEqual(self.execute(), 1)
        self.assertIn("leverage cap", self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()

    def test_fixed_stop_plans_then_submits_between_rebalances(self):
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 20)
        # 83 breaches the 16.90% risk-estimate stop (83.10); the 25% floored cap
        # (75.00) is looser and never tightens this fixture.
        self.snapshots["snapshot_list"][0]["last_price"] = "83"
        self.mocks["sim_capacity"].return_value = {"max_sell_qty_round_lot": "3"}
        self.seed_holding_risk()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(
            self.run_main("--ledger", str(self.journal), "--plan-only"),
            0, self.stderr.getvalue(),
        )
        self.mocks["sim_order"].assert_not_called()
        result = self.mocks["format"].call_args.args[0]
        self.assertEqual(result["status"], "stop_loss_planned")
        # The risk estimate (~16.90%) is tighter than the 25% cap, so the
        # estimate's stop triggers and is reported.
        self.assertEqual(result["plan"]["stop_loss_limit"], "0.16903085094570333")
        self.assertIn("Fixed stop below cost", self.stdout.getvalue())
        self.assertIn("No orders submitted (--plan-only)", self.stdout.getvalue())
        self.assertEqual(
            self.run_main("--ledger", str(self.journal), "--order-type", "MARKET"),
            0, self.stderr.getvalue(),
        )
        result = self.mocks["format"].call_args.args[0]
        self.assertEqual(result["status"], "stop_loss_submitted")
        call = self.mocks["sim_order"].call_args
        self.assertEqual(call.args, ("AAA", 3, "SELL"))
        self.assertEqual(call.kwargs["order_type"], "LIMIT")
        self.assertEqual(call.kwargs["price"], "83.00")
        self.mocks["live_order"].assert_not_called()
        self.assertFalse(self.sim.dry_run)
        self.assertTrue(self.journal.exists())

    def seed_holding_risk(self):
        # Weekend runs save holding_stop_risk with the inputs; this Monday ledger
        # has none, so seed the same 16.90% risk estimate for the stop branches.
        risk = {
            symbol: {
                "annualized_volatility": 0.6,
                "stop_fraction": 0.16903085094570333, "as_of": "2026-10-23",
            } for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE")
        }
        with sqlite3.connect(self.journal) as db:
            db.execute(
                "INSERT INTO weekly_inputs VALUES (?,?,?,?)",
                ("2026-10-19", "2026-10-17", 250, json.dumps({"holding_stop_risk": risk})),
            )

    def prepare_stop_loss(self):
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.fill_all_orders()
        self.seed_holding_risk()
        # The default stop cap is 25% x gross leverage; 70 is below every such
        # trigger (75.00 at the 1x floor) while staying above the tighter
        # risk-based estimate.
        self.snapshots["snapshot_list"][0]["last_price"] = "70"
        self.mocks["sim_capacity"].return_value = {"max_sell_qty_round_lot": "30"}
        self.mocks["sim_order"].reset_mock()

    def test_stop_loss_outside_window_creates_no_intents(self):
        self.prepare_stop_loss()
        for day in (date(2026, 10, 20), date(2026, 10, 25)):
            self.mocks["clock"].return_value = day
            self.mocks["opening"].return_value = False
            with patch.object(orders, "prepare_weekend", return_value={"status": "prepared"}):
                self.assertEqual(self.execute(), 0, self.stderr.getvalue())
            result = self.mocks["format"].call_args.args[0]
            self.assertEqual(result["status"], "reconciled")
            self.assertTrue(result["account"]["positions"][0]["stop_triggered"])
            self.assertNotIn("plan", result)
        self.mocks["sim_order"].assert_not_called()
        with sqlite3.connect(self.journal) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intents").fetchone()[0], 5)

    def test_stop_loss_rechecks_window_before_creating_intent(self):
        self.prepare_stop_loss()
        self.mocks["opening"].side_effect = [True, True, False]
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_not_called()
        with sqlite3.connect(self.journal) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intents").fetchone()[0], 5)

    def test_stop_loss_broker_error_preserves_safe_diagnostics(self):
        self.prepare_stop_loss()
        self.mocks["sim_order"].side_effect = FutuAPIError(
            "backend business error", code=-123, status=400,
            details={"access_token": "never-print-this"},
        )
        self.assertEqual(self.execute(), 1)
        self.assertIn("code=-123, HTTP status=400", self.stderr.getvalue())
        self.assertNotIn("never-print-this", self.stderr.getvalue())
        uncertain = [event for event in self.events() if event["event"] == "submission_uncertain"]
        self.assertEqual(uncertain[-1]["broker_code"], -123)
        self.assertEqual(uncertain[-1]["http_status"], 400)
        self.assertNotIn("never-print-this", json.dumps(uncertain))
        self.assertEqual(self.mocks["sim_order"].call_count, 1)

    def test_fresh_history_start_initializes_without_submitting_and_persists(self):
        self.broker_orders.append({
            "order_id": "old", "status": 4, "text": "finacial:old", "update_time": "1",
        })
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only",
                                       "--history-start", "2026-10-26T00:00:00-04:00"), 0,
                         self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()
        with StrategyLedger(self.journal) as ledger:
            self.assertEqual(ledger.history_start(), 1792987200000000)
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only"), 0,
                         self.stderr.getvalue())
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only",
                                       "--history-start", "now"), 1)
        self.assertIn("requires a fresh ledger", self.stderr.getvalue())

    def test_history_start_requires_timezone_and_reconcile_only(self):
        for extra in (
            ("--history-start", "now"),
            ("--reconcile-only", "--history-start", "2026-10-26"),
            ("--reconcile-only", "--history-start", "invalid"),
            ("--reconcile-only", "--history-start", "2099-01-01T00:00:00+00:00"),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_main("--ledger", str(self.journal), *extra), 1)
        self.mocks["sim_factory"].assert_not_called()

    def test_fresh_history_start_now_still_checks_old_open_history(self):
        self.mocks["sim_history"].return_value = [{
            "order_id": "old-open", "status": 2, "text": "", "update_time": "1",
        }]
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only",
                                       "--history-start", "now"), 1)
        self.mocks["sim_history"].assert_called_once_with(time_begin=None)
        self.assertIn("Cannot initialize", self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()

    def test_weekend_stop_alert_does_not_hide_five_selected_targets(self):
        self.prepare_stop_loss()
        self.mocks["clock"].return_value = date(2026, 10, 25)
        self.mocks["opening"].return_value = False
        self.allocation["as_of"] = self.allocation["end_date"] = "2026-10-23"
        self.allocation["weights"] = [
            {"symbol": symbol, "weight": 0.2} for symbol in ("FFF", "GGG", "HHH", "III", "JJJ")
        ]
        self.snapshots["snapshot_list"].extend(
            {"code": f"US.{symbol}", "last_price": "100"}
            for symbol in ("FFF", "GGG", "HHH", "III", "JJJ")
        )
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        result = self.mocks["format"].call_args.args[0]
        self.assertEqual(result["upcoming_week"], "2026-10-26")
        self.assertEqual({row["symbol"] for row in result["upcoming_targets"]},
                         {"FFF", "GGG", "HHH", "III", "JJJ"})
        self.assertNotIn("plan", result)
        text = self.stdout.getvalue()
        self.assertIn("Planned actions (week of 2026-10-26, from saved weekend data)", text)
        self.assertIn("Selected weekend targets", text)
        self.assertIn("83.10 USD (triggered)", text)
        self.assertIn("-30.00%", text)
        self.mocks["sim_order"].assert_not_called()
        self.mocks["holding_risk"].assert_called_once()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["holding_risk"].assert_called_once()

    def test_monday_rebalance_takes_precedence_over_stop_only_batch(self):
        self.prepare_stop_loss()
        self.mocks["clock"].return_value = date(2026, 10, 26)
        self.allocation["as_of"] = self.allocation["end_date"] = "2026-10-23"
        self.allocation["weights"][0]["symbol"] = "FFF"
        self.snapshots["snapshot_list"].append({"code": "US.FFF", "last_price": "100"})
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        result = self.mocks["format"].call_args.args[0]
        self.assertEqual(result["plan"]["cycle_week"], "2026-10-26")
        self.assertNotIn("stop_loss_limit", result["plan"])
        self.assertEqual({row["symbol"] for row in result["plan"]["target_positions"]},
                         {"FFF", "BBB", "CCC", "DDD", "EEE"})

    def test_unusable_risk_distance_is_not_clamped_to_a_fake_stop(self):
        self.prepare_stop_loss()
        self.mocks["clock"].return_value = date(2026, 10, 25)
        self.mocks["opening"].return_value = False
        self.allocation["as_of"] = self.allocation["end_date"] = "2026-10-23"
        self.mocks["holding_risk"].side_effect = None
        self.mocks["holding_risk"].return_value = {
            "AAA": {"annualized_volatility": 5, "stop_fraction": 1.4, "as_of": "2026-10-23"},
        }
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        result = self.mocks["format"].call_args.args[0]
        self.assertIsNone(result["account"]["positions"][0]["risk_stop_price"])
        self.mocks["sim_order"].assert_not_called()

    def test_text_report_includes_risk_account_orders_and_broker_ids(self):
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        text = self.stdout.getvalue()
        for heading in (
            "Simulation strategy", "Account before new orders", "Current strategy holdings",
            "Risk and sizing", "Target weights", "Rebalance orders", "Submission results",
        ):
            self.assertIn(heading, text)
        for value in (
            "3,000.00 USD", "50.0000%", "100.0000%", "1.3300x", "0.5000x",
            "AAA     : 20.0000%", "BUY  3 shares", "limit 100.00 USD",
            "order ID 1", "Recorded fees", "Recorded interest", "not confirmed filled",
        ):
            self.assertIn(value, text)
        self.assertNotIn('{"', text)
        self.assertNotIn('"plan":', text)
        self.assertNotIn("|---", text)
        self.assertIn("Ledger", text)
        self.assertIn(str(self.journal), text)

    def test_legacy_market_argument_uses_limit_for_capacity_and_submission(self):
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--order-type", "MARKET",
        ), 0, self.stderr.getvalue())
        for call in self.mocks["sim_capacity"].call_args_list:
            self.assertEqual(call.kwargs, {"order_type": "LIMIT", "price": "100.00"})
        for call in self.mocks["sim_order"].call_args_list:
            self.assertEqual(call.kwargs["order_type"], "LIMIT")
            self.assertEqual(call.kwargs["price"], "100.00")
        output = self.mocks["format"].call_args.args[0]
        self.assertEqual(output["plan"]["order_type"], "LIMIT")
        self.assertIn("MARKET simulation orders are disabled", self.stderr.getvalue())
        submitted = [event for event in self.events() if event["event"] == "submitting"]
        self.assertTrue(all(event["order_type"] == "LIMIT" for event in submitted))
        self.assertTrue(all(event["price_role"] == "limit" for event in submitted))
        self.mocks["live_order"].assert_not_called()

    def test_switching_order_type_never_replaces_existing_pending_orders(self):
        self.assertEqual(self.execute(), 0)
        self.mocks["sim_order"].reset_mock()
        self.mocks["sim_capacity"].reset_mock()
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--order-type", "MARKET",
        ), 0, self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()
        self.mocks["sim_capacity"].assert_not_called()

    def test_saved_market_cycle_submits_new_orders_as_limit(self):
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.fill_all_orders()
        with StrategyLedger(self.journal) as ledger:
            cycle = ledger.active_cycle()
            plan = json.loads(cycle["plan"])
            plan["order_type"] = "MARKET"
            for row in plan["orders"]:
                row["qty"] += 1
            with ledger.db:
                ledger.db.execute("UPDATE rebalance_cycles SET plan=? WHERE week=?",
                                  (json.dumps(plan), cycle["week"]))
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        for call in self.mocks["sim_order"].call_args_list:
            self.assertEqual(call.kwargs["order_type"], "LIMIT")
            self.assertEqual(call.kwargs["price"], "100.00")

    def test_legacy_market_rebalance_sells_use_limit_capacity_and_price(self):
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 26)
        for snapshot in self.snapshots["snapshot_list"]:
            snapshot["last_price"] = "200"
        self.mocks["sim_capacity"].return_value["max_sell_qty_round_lot"] = "30"
        self.mocks["sim_capacity"].reset_mock()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--order-type", "MARKET",
        ), 0, self.stderr.getvalue())
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        for call in self.mocks["sim_order"].call_args_list:
            self.assertEqual(call.args[2], "SELL")
            self.assertEqual(call.kwargs["order_type"], "LIMIT")
            self.assertEqual(call.kwargs["price"], "200.00")
        for call in self.mocks["sim_capacity"].call_args_list:
            self.assertEqual(call.kwargs, {"order_type": "LIMIT", "price": "200.00"})

    def test_leveraged_sim_uses_margin_capacity(self):
        self.allocation["annualized_volatility"] = 0.25
        self.mocks["sim_funds"].return_value["max_power_long"] = "3990"
        self.mocks["sim_capacity"].return_value = {
            "max_cash_buy_qty_round_lot": "0", "max_margin_buy_qty_round_lot": "7",
        }
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["sim_order"].assert_any_call(
            "AAA", 7, "BUY", order_type="LIMIT", price="100.00", text=ANY,
        )
        self.mocks["live_order"].assert_not_called()

    def test_leveraged_sim_requires_total_buying_power_and_margin_eligibility(self):
        self.allocation["annualized_volatility"] = 0.25
        for power, capacity in (("3499", "7"), ("3990", None), ("3990", "6")):
            self.mocks["sim_funds"].return_value["max_power_long"] = power
            self.mocks["sim_capacity"].return_value = {"max_margin_buy_qty_round_lot": capacity}
            with self.subTest(power=power, capacity=capacity):
                self.assertEqual(self.execute(), 1)
                self.assert_no_intents()
        self.mocks["sim_order"].assert_not_called()

    def test_live_options_rejected_before_screening(self):
        for extra in (["--mode", "live"], ["--mode", "live", "--execute", "--confirm-live"],
                      ["--confirm-live"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as raised:
                self.run_main(*extra)
            self.assertEqual(raised.exception.code, 2)
        self.mocks["screen"].assert_not_called()
        self.mocks["quote_factory"].assert_not_called()
        self.mocks["live_select"].assert_not_called()
        self.mocks["live_order"].assert_not_called()

    def test_removed_options_rejected_before_screening(self):
        for extra in (["--observe-only"], ["--observations", "old.jsonl"], ["--execute"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as raised:
                self.run_main("--ledger", str(self.journal), *extra)
            self.assertEqual(raised.exception.code, 2)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_old_risk_fraction_option_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_main("--risk-fraction", "0.5")
        self.mocks["screen"].assert_not_called()

    def test_simulation_routing(self):
        self.assertEqual(self.execute(), 0)
        self.mocks["sim_select"].assert_any_call(market=100, account_id="chosen")
        self.mocks["live_order"].assert_not_called()
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        self.mocks["sim_order"].assert_any_call(
            "AAA", 3, "BUY", order_type="LIMIT", price="100.00", text=ANY,
        )
        self.assertFalse(self.sim.dry_run)
        self.assertTrue(self.quote.dry_run)
        self.assertFalse(self.quote.allow_live_trading)

    def test_default_profile_selects_unique_us_simulation_account_and_submits(self):
        with (
            patch.object(orders, "get_store") as store,
            redirect_stdout(self.stdout), redirect_stderr(self.stderr),
        ):
            store.return_value.sim_client.return_value = self.sim
            result = orders.main(["--ledger", str(self.journal)])
        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual(store.call_count, 2)
        store.assert_called_with("default")
        self.assertEqual(store.return_value.sim_client.call_count, 2)
        self.mocks["sim_select"].assert_any_call(market=100)
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        self.assertTrue(all(
            call.kwargs["order_type"] == "LIMIT" and call.kwargs["price"] == "100.00"
            for call in self.mocks["sim_order"].call_args_list
        ))
        self.mocks["live_order"].assert_not_called()
        self.mocks["live_select"].assert_not_called()

    def test_ambiguous_or_missing_default_simulation_account_stops_submission(self):
        for message in ("Multiple simulation accounts match", "No simulation account matches"):
            self.mocks["sim_select"].side_effect = ValueError(message)
            with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
                result = orders.main(["--ledger", str(self.journal)])
            with self.subTest(message=message):
                self.assertEqual(result, 1)
                self.assertIn(message, self.stderr.getvalue())
        self.mocks["sim_funds"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.mocks["live_order"].assert_not_called()
        self.assert_no_intents()

    def test_submission_helper_rejects_even_enabled_real_client(self):
        self.quote.dry_run = False
        self.quote.allow_live_trading = True
        with StrategyLedger(self.journal) as ledger:
            with self.assertRaisesRegex(ValueError, "SimTradingClient"):
                ledger.submit(self.quote, self.plan())
        self.assert_no_intents()
        self.mocks["live_order"].assert_not_called()

    def test_simulation_routing_and_persistent_ledger(self):
        self.assertEqual(self.execute(), 0)
        self.mocks["sim_select"].assert_any_call(market=100, account_id="chosen")
        self.mocks["live_select"].assert_not_called()
        self.mocks["live_funds"].assert_not_called()
        self.mocks["live_capacity"].assert_not_called()
        self.mocks["live_order"].assert_not_called()
        self.mocks["sim_order"].assert_any_call(
            "AAA", 3, "BUY", order_type="LIMIT", price="100.00", text=ANY,
        )
        self.assertTrue(self.quote.dry_run)
        self.assertFalse(self.quote.allow_live_trading)
        self.assertFalse(self.sim.dry_run)
        events = self.events()
        plan_event = next(event for event in events if event["event"] == "plan")
        self.assertEqual(plan_event["mode"], "sim")
        self.assertEqual(events[0]["event"], "initialized")
        self.assertEqual(events[-1]["event"], "batch_submitted")
        self.assertEqual(sum(event["event"] == "response" for event in events), 5)

    def test_reusing_ledger_with_pending_orders_does_not_duplicate(self):
        self.assertEqual(self.execute(), 0)
        self.mocks["sim_order"].reset_mock()
        self.mocks["screen"].reset_mock()
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.assertEqual(self.mocks["format"].call_args.args[0]["status"], "waiting_for_open_orders")
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_filled_batch_reconciles_then_holds_without_rebuying(self):
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["sim_order"].reset_mock()
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.assertEqual(self.mocks["format"].call_args.args[0]["status"], "reconciled")
        self.assertIn("Daily reconciliation complete; no orders scheduled", self.stdout.getvalue())
        self.mocks["sim_order"].assert_not_called()
        with StrategyLedger(self.journal) as ledger:
            self.assertEqual(ledger.book()["cash"], Decimal("1495.05"))
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM fills").fetchone()[0], 5)
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()

    def test_daily_reconciliation_does_not_rescreen_when_prices_or_rankings_change(self):
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 20)
        for row in self.snapshots["snapshot_list"]:
            row["last_price"] = "200"
        self.allocation["weights"][0]["symbol"] = "FFF"
        self.mocks["screen"].reset_mock()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.assertEqual(self.mocks["format"].call_args.args[0]["status"], "reconciled")
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_rebalance_weekday_and_missed_day_catchup(self):
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--rebalance-day", "WED",
        ), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.mocks["clock"].return_value = date(2026, 10, 22)
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--rebalance-day", "WED",
        ), 0, self.stderr.getvalue())
        self.mocks["inputs"].assert_called_once()
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        with StrategyLedger(self.journal) as ledger:
            row = ledger.active_cycle()
            self.assertEqual(row["week"], "2026-10-19")
            self.assertEqual(row["scheduled_date"], "2026-10-21")

    def test_reconcile_only_weekend_never_prepares_or_orders(self):
        for day in (24, 25):
            self.mocks["clock"].return_value = date(2026, 10, day)
            self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only"), 0,
                             self.stderr.getvalue())
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_weekend_run_prints_upcoming_week_plan(self):
        self.mocks["clock"].return_value = date(2026, 10, 25)  # Sunday
        self.allocation["as_of"] = self.allocation["end_date"] = "2026-10-23"
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--prepare-only"), 0,
                         self.stderr.getvalue())
        self.assertEqual(self.mocks["screen"].call_count, 1)
        # A normal run with the next week's inputs saved prints the preview plan.
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only"), 0,
                         self.stderr.getvalue())
        text = self.stdout.getvalue()
        self.assertIn("Planned actions (week of 2026-10-26, from saved weekend data)", text)
        self.assertIn("Limit price", text)
        self.assertIn("AAA      BUY       3 LIMIT", text)
        self.assertIn("100.00 USD", text)
        self.assertIn(
            "Selected weekend targets & protective stops (preview only; not broker orders)", text,
        )
        self.assertIn(
            "Symbol     Qty     Weight Type        Limit price   Stop % Risk est %    Trigger price"
            " Trailing",
            text,
        )
        self.assertIn(
            "AAA          3  20.0000% LIMIT        100.00 USD  16.90%    16.90%        83.10 USD"
            "      Off",
            text,
        )
        self.assertIn("recalculated after fill", text)
        self.assertIn("Aimed leverage: 0.5000x", text)
        self.assertIn("Simulation submits neither broker-held fixed nor trailing stops", text)
        self.assertIn("Scheduled date (NY)     : 2026-10-26", text)
        self.assertNotIn("2026-10-19", text)
        self.mocks["sim_order"].assert_not_called()
        self.assert_no_intents()

    def test_upcoming_week_plan_shows_sells_of_current_holdings(self):
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 25)  # Sunday
        self.allocation["as_of"] = self.allocation["end_date"] = "2026-10-23"
        # Next week rotates out of AAA..EEE into a fresh basket.
        self.allocation["weights"] = [
            {"symbol": symbol, "weight": 0.2} for symbol in ("FFF", "GGG", "HHH", "III", "JJJ")
        ]
        self.mocks["screen"].reset_mock()
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--prepare-only"), 0,
                         self.stderr.getvalue())
        self.snapshots["snapshot_list"] = [
            {"code": f"US.{s}", "last_price": "100"}
            for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ")
        ]
        self.stdout = StringIO()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only"), 0,
                         self.stderr.getvalue())
        text = self.stdout.getvalue()
        self.assertIn("Planned actions (week of 2026-10-26, from saved weekend data)", text)
        self.assertIn("Scheduled date (NY)     : 2026-10-26", text)
        result = self.mocks["format"].call_args.args[0]
        self.assertEqual({row["symbol"]: row["side"] for row in result["upcoming_plan"]},
                         {"AAA": "SELL", "BBB": "SELL", "CCC": "SELL",
                          "DDD": "SELL", "EEE": "SELL",
                          "FFF": "BUY", "GGG": "BUY", "HHH": "BUY",
                          "III": "BUY", "JJJ": "BUY"})
        self.assertEqual(result["preview_order_type"], "LIMIT")
        for row in result["upcoming_plan"]:
            self.assertEqual(row["limit_price"], "100.00")
            self.assertIn(f'{row["symbol"]:<8} {row["side"]:<5} {row["qty"]:>5} LIMIT', text)
        self.assertIn("sells must reconcile before buys", text)
        self.mocks["sim_order"].assert_not_called()
        self.stdout = StringIO()
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--reconcile-only", "--order-type", "MARKET",
        ), 0, self.stderr.getvalue())
        market_text = self.stdout.getvalue()
        self.assertIn("Limit price", market_text)
        self.assertIn("FFF      BUY       2 LIMIT", market_text)
        self.mocks["sim_order"].assert_not_called()

    def test_reconcile_prints_planned_actions_of_pending_cycle(self):
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        # One buy fills; the rest stay pending so the cycle is unfinished.
        self.broker_orders[0].update(status=4, cum_qty=self.broker_orders[0]["qty"],
                                     avg_fill_price=self.broker_orders[0]["price"],
                                     update_time="2")
        qty = self.broker_orders[0]["qty"]
        cash = Decimal("3000") - Decimal(qty) * Decimal(self.broker_orders[0]["price"])
        cash -= Decimal("0.99")
        self.mocks["sim_funds"].return_value = {"balance": str(cash), "hold": "0",
                                                "max_power_long": "6000"}
        self.mocks["sim_positions"].return_value = {"positions": [
            {"symbol": self.broker_orders[0]["symbol"], "qty": qty,
             "market": 100, "pstn_type": 0},
        ]}
        self.stdout = StringIO()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only"), 0,
                         self.stderr.getvalue())
        text = self.stdout.getvalue()
        self.assertIn("Waiting for open orders", text)
        self.assertIn("Planned actions (pending rebalance)", text)
        self.assertIn("BUY", text)
        self.assertNotIn("AAA      BUY", text)  # AAA target already filled
        self.mocks["sim_order"].assert_not_called()

    def test_saved_weekly_target_survives_sell_buy_phases_and_settings_change(self):
        self.allocation["annualized_volatility"] = 0.99
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 26)
        self.allocation["weights"][0]["symbol"] = "FFF"
        self.snapshots["snapshot_list"].append({"code": "US.FFF", "last_price": "100"})
        self.mocks["sim_capacity"].return_value["max_sell_qty_round_lot"] = "3"
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 27)
        self.snapshots["snapshot_list"] = [
            row for row in self.snapshots["snapshot_list"] if row["code"] != "US.AAA"
        ]
        self.allocation["weights"][0]["symbol"] = "GGG"
        self.mocks["screen"].reset_mock()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--target-volatility", "0.25", "--order-type", "MARKET",
        ), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_called_once_with(
            "FFF", 3, "BUY", order_type="LIMIT", price="100.00", text=ANY,
        )
        plan = self.mocks["format"].call_args.args[0]["plan"]
        self.assertEqual(plan["cycle_week"], "2026-10-26")
        self.assertEqual(Decimal(plan["target_annualized_volatility"]), Decimal("0.5"))

    def test_next_week_cannot_replace_unfinished_target(self):
        self.assertEqual(self.execute(), 0)
        self.mocks["clock"].return_value = date(2026, 10, 26)
        self.mocks["screen"].reset_mock()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_saved_target_rechecks_current_marked_leverage_before_buying(self):
        self.mocks["sim_capacity"].return_value["max_cash_buy_qty_round_lot"] = "0"
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_capacity"].return_value["max_cash_buy_qty_round_lot"] = "30"
        for row in self.snapshots["snapshot_list"]:
            row["last_price"] = "300"
        self.mocks["clock"].return_value = date(2026, 10, 20)
        self.mocks["screen"].reset_mock()
        self.assertEqual(self.execute(), 1)
        self.assertIn("current leverage cap", self.stderr.getvalue())
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_reranking_sells_removed_symbol_before_buying_replacement(self):
        self.allocation["annualized_volatility"] = 0.99
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 26)
        self.allocation["weights"][0]["symbol"] = "FFF"
        self.snapshots["snapshot_list"].append({"code": "US.FFF", "last_price": "100"})
        self.mocks["sim_capacity"].return_value["max_sell_qty_round_lot"] = "3"
        self.mocks["sim_order"].reset_mock()
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        plan = self.mocks["format"].call_args.args[0]["plan"]
        self.assertEqual(plan["phase"], "SELL")
        self.mocks["sim_order"].assert_called_once_with(
            "AAA", 3, "SELL", order_type="LIMIT", price="100.00", text=ANY,
        )
        self.fill_all_orders()
        self.snapshots["snapshot_list"] = [
            row for row in self.snapshots["snapshot_list"] if row["code"] != "US.AAA"
        ]
        self.mocks["sim_order"].reset_mock()
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.assertEqual(self.mocks["format"].call_args.args[0]["plan"]["phase"], "BUY")
        self.mocks["sim_order"].assert_called_once_with(
            "FFF", 3, "BUY", order_type="LIMIT", price="100.00", text=ANY,
        )

    def test_strategy_nav_not_initial_budget_determines_targets(self):
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["clock"].return_value = date(2026, 10, 26)
        for snapshot in self.snapshots["snapshot_list"]:
            snapshot["last_price"] = "200"
        self.mocks["sim_capacity"].return_value["max_sell_qty_round_lot"] = "30"
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        output = self.mocks["format"].call_args.args[0]
        self.assertEqual(Decimal(output["account_before_submission"]["nav_usd"]), Decimal("4495.05"))
        self.assertEqual(Decimal(output["plan"]["investment_cap_usd"]), Decimal("2247.52"))
        self.assertEqual(output["plan"]["phase"], "SELL")
        self.assertTrue(all(row["qty"] == 1 for row in output["plan"]["orders"]))

    def test_position_mismatch_blocks_before_screening(self):
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["sim_positions"].return_value["positions"][0]["qty"] = "99"
        self.mocks["screen"].reset_mock()
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.execute(), 1)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.assertIn("positions differ", self.stderr.getvalue())

    def test_account_changes_during_screening_block_before_submission(self):
        self.mocks["sim_funds"].side_effect = [
            {"balance": "3000", "hold": "0"}, {"balance": "2999", "hold": "0"},
        ]
        self.assertEqual(self.execute(), 1)
        self.mocks["inputs"].assert_called_once()
        self.mocks["sim_order"].assert_not_called()
        self.assertIn("cash mismatch", self.stderr.getvalue())

    def test_capital_change_is_not_treated_as_new_funding(self):
        self.assertEqual(self.execute(), 0)
        self.mocks["sim_order"].reset_mock()
        self.mocks["screen"].reset_mock()
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--capital", "4000"), 1)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_missing_broker_history_blocks_without_new_orders(self):
        self.assertEqual(self.execute(), 0)
        self.broker_orders.clear()
        self.mocks["sim_order"].reset_mock()
        self.mocks["screen"].reset_mock()
        self.assertEqual(self.execute(), 1)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.assertIn("missing", self.stderr.getvalue())

    def test_partial_execution_report_waits_and_accounts_only_the_fill(self):
        self.assertEqual(self.execute(), 0)
        self.broker_orders[0].update(status=3, cum_qty="1", avg_fill_price="100", update_time="2")
        self.mocks["sim_funds"].return_value["balance"] = "2899.01"
        self.mocks["sim_positions"].return_value = {"positions": [
            {"symbol": "AAA", "qty": "1", "market": 100, "pstn_type": 0},
        ]}
        self.mocks["screen"].reset_mock()
        self.mocks["sim_order"].reset_mock()
        self.stdout = StringIO()
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        output = self.mocks["format"].call_args.args[0]
        self.assertEqual(output["status"], "waiting_for_open_orders")
        self.assertEqual(output["account"]["positions"][0]["qty"], "1")
        self.assertEqual(output["account"]["cash_usd"], "2899.01")
        text = self.stdout.getvalue()
        self.assertIn("Waiting for open orders; no new orders", text)
        self.assertIn("AAA          1 ", text)
        self.assertIn("2,899.01 USD", text)
        self.assertIn("Planned actions (pending rebalance)", text)
        self.assertIn("BUY", text)
        self.assertNotIn("Rebalance orders", text)
        self.assertNotIn('"account":', text)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_fee_and_recovery_arguments_are_validated_before_broker_access(self):
        cases = [
            ["--fee-usd", "1"], ["--expense-id", "x"], ["--void-intent", "x"],
            ["--void-note", "verified"], ["--expense-id", "x", "--fee-usd", "1",
                                        "--expense-note", "new ledger cannot have past expenses"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                self.assertEqual(self.run_main("--ledger", str(self.journal), *extra), 1)
        self.mocks["sim_factory"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_cash_mismatch_can_only_be_booked_as_verified_expense(self):
        self.assertEqual(self.execute(), 0)
        self.fill_all_orders()
        self.mocks["sim_funds"].return_value["balance"] = "1493.55"
        self.mocks["sim_order"].reset_mock()
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_not_called()
        self.mocks["sim_capacity"].return_value["max_sell_qty_round_lot"] = "30"
        self.assertEqual(self.run_main(
            "--ledger", str(self.journal), "--expense-id", "fee-1",
            "--fee-usd", "1.50", "--expense-note", "broker statement reference",
        ), 0, self.stderr.getvalue())
        with StrategyLedger(self.journal) as ledger:
            self.assertEqual(ledger.book()["fees"], Decimal("6.45"))

    def test_exclusive_ledger_blocks_concurrent_execution(self):
        with StrategyLedger(self.journal):
            self.assertEqual(self.execute(), 1)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_no_arguments_submits_using_default_ledger(self):
        self.assertEqual(
            orders.DEFAULT_LEDGER_PATH, orders.ROOT / "ouput" / "play_money_ledger.sqlite3",
        )
        self.assertTrue(orders.DEFAULT_LEDGER_PATH.is_absolute())
        with (
            patch.object(orders, "DEFAULT_LEDGER_PATH", self.journal),
            redirect_stdout(self.stdout), redirect_stderr(self.stderr),
        ):
            result = orders.main([])
        self.assertEqual(result, 0, self.stderr.getvalue())
        self.mocks["sim_select"].assert_any_call(market=100)
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        events = self.events()
        self.assertEqual(events[-1]["event"], "batch_submitted")
        self.mocks["live_order"].assert_not_called()

    def test_existing_default_journal_blocks_before_screening(self):
        legacy = self.journal.parent / "legacy.jsonl"
        legacy.write_text("existing\n")
        self.assertEqual(self.execute(), 1)
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.assertEqual(legacy.read_text(), "existing\n")

    def test_insufficient_cash_prevents_submission(self):
        self.mocks["sim_funds"].return_value["balance"] = "2999"
        self.assertEqual(self.execute(), 1)
        self.mocks["live_order"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.assert_no_intents()

    def test_frozen_cash_cannot_fund_orders(self):
        self.mocks["sim_funds"].return_value = {"balance": "5000", "hold": "2001"}
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_not_called()
        self.mocks["live_order"].assert_not_called()

    def test_cash_capacity_checked_for_all_orders_before_first_write(self):
        field = "max_cash_buy_qty_round_lot"
        self.mocks["sim_capacity"].side_effect = [{field: "30"}] * 4 + [{field: "2"}]
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_not_called()
        self.mocks["live_order"].assert_not_called()
        self.assert_no_intents()

    def test_unknown_cash_schema_fails_closed(self):
        self.mocks["sim_funds"].return_value = {"max_power_long": "10000"}
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_not_called()

    def test_bad_quote_prevents_all_submission(self):
        self.snapshots["snapshot_list"].pop()
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_not_called()
        self.assert_no_intents()

    def test_missing_weekend_inputs_after_reconciliation_never_submits(self):
        self.mocks["inputs"].side_effect = ValueError("No weekend inputs")
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_orders"].assert_called_once()
        self.mocks["sim_order"].assert_not_called()

    def test_preopen_and_after_window_only_reconcile(self):
        self.mocks["opening"].return_value = False
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["inputs"].assert_not_called()
        self.mocks["screen"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.assertEqual(self.mocks["format"].call_args.args[0]["status"], "reconciled")

    def test_reconcile_only_at_open_never_submits(self):
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--reconcile-only"), 0)
        self.mocks["inputs"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()

    def test_plan_only_prints_orders_without_submitting(self):
        self.mocks["opening"].return_value = False
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--plan-only"), 0,
                         self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()
        self.assert_no_intents()
        text = self.stdout.getvalue()
        for value in (
            "Planned orders only; nothing submitted", "Rebalance orders",
            "BUY  3 shares", "No orders submitted (--plan-only)",
        ):
            self.assertIn(value, text)

    def test_plan_only_is_exclusive_with_prepare_and_reconcile(self):
        for option in ("--prepare-only", "--reconcile-only"):
            with self.subTest(option=option):
                with self.assertRaisesRegex(SystemExit, "2"):
                    self.run_main("--ledger", str(self.journal), option, "--plan-only")
                self.assertIn("not allowed with argument", self.stderr.getvalue())

    def test_opening_window_rechecked_for_every_order(self):
        self.mocks["opening"].side_effect = [True, True, True, False]
        self.assertEqual(self.execute(), 1)
        self.mocks["sim_order"].assert_called_once()
        self.assertIn("Opening window ended", self.stderr.getvalue())
        with StrategyLedger(self.journal) as ledger:
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM intents").fetchone()[0], 1)
        text = self.journal.with_suffix(".transactions.txt").read_text()
        self.assertIn("SUBMITTED", text)
        self.assertIn("Estimated order fees at submission: 0.99 USD", text)

    def test_weekend_prepare_only_never_accesses_broker(self):
        self.mocks["clock"].return_value = date(2026, 10, 24)
        self.allocation.update(as_of="2026-10-23", end_date="2026-10-23")
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--prepare-only"), 0,
                         self.stderr.getvalue())
        self.mocks["screen"].assert_called_once_with("2026-10-23", 63)
        self.mocks["sim_factory"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        self.assertIn("Weekend data prepared", self.stdout.getvalue())

    def test_recover_weekend_inputs_uses_pre_week_session_without_broker_or_orders(self):
        self.mocks["clock"].return_value = date(2026, 10, 28)
        self.allocation.update(as_of="2026-10-23", end_date="2026-10-23")
        self.assertEqual(self.run_main("--ledger", str(self.journal),
                                       "--recover-weekend-inputs"), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_called_once_with("2026-10-23", 63)
        self.mocks["sim_factory"].assert_not_called()
        self.mocks["sim_order"].assert_not_called()
        with StrategyLedger(self.journal) as ledger:
            row = ledger.db.execute("SELECT * FROM weekly_inputs").fetchone()
            self.assertEqual(row["week"], "2026-10-26")
            self.assertEqual(row["prepared_at"], "2026-10-28")
            args = MagicMock(lookback=63, as_of=None)
            allocation = load_weekend_allocation(ledger, "2026-10-26", args)
            self.assertEqual(allocation["as_of"], "2026-10-23")
        self.assertEqual(self.run_main("--ledger", str(self.journal),
                                       "--recover-weekend-inputs"), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_called_once()

    def test_recovery_rejects_wrong_as_of_and_incomplete_data(self):
        self.mocks["clock"].return_value = date(2026, 10, 26)
        self.assertEqual(self.run_main("--ledger", str(self.journal),
                                       "--recover-weekend-inputs", "--as-of", "2026-10-26"), 1)
        self.mocks["screen"].assert_not_called()
        self.allocation.update(as_of="2026-10-26", end_date="2026-10-26")
        self.assertEqual(self.run_main("--ledger", str(self.journal),
                                       "--recover-weekend-inputs"), 1)
        with StrategyLedger(self.journal) as ledger:
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM weekly_inputs").fetchone()[0], 0)
        self.mocks["sim_factory"].assert_not_called()

    def test_monday_executes_real_saved_weekend_inputs_without_screening(self):
        self.mocks["clock"].return_value = date(2026, 10, 24)
        self.allocation.update(as_of="2026-10-23", end_date="2026-10-23")
        self.assertEqual(self.run_main("--ledger", str(self.journal), "--prepare-only"), 0)
        self.mocks["screen"].reset_mock()
        self.mocks["clock"].return_value = date(2026, 10, 26)
        self.mocks["inputs"].side_effect = load_weekend_allocation
        with patch("play_money_asset_risk_parity.date", wraps=date) as calendar:
            calendar.today.return_value = date(2026, 10, 26)
            self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_not_called()
        self.assertEqual(self.mocks["sim_order"].call_count, 5)
        self.assertEqual(self.mocks["format"].call_args.args[0]["plan"]["as_of"], "2026-10-23")

    def test_default_weekend_prepares_then_reconciles_without_submitting(self):
        self.mocks["clock"].return_value = date(2026, 10, 24)
        self.mocks["opening"].return_value = False
        self.allocation.update(as_of="2026-10-23", end_date="2026-10-23")
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["screen"].assert_called_once_with("2026-10-23", 63)
        self.mocks["sim_funds"].assert_called_once()
        self.mocks["sim_order"].assert_not_called()

    def test_broker_buying_power_reserves_estimated_fees(self):
        self.allocation["annualized_volatility"] = "0.5"
        self.mocks["sim_funds"].return_value["max_power_long"] = "3000"
        self.assertEqual(self.execute(), 1)
        self.assertIn("plus estimated fees", self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()

    def test_empty_batch_records_hold_without_orders(self):
        for snapshot in self.snapshots["snapshot_list"]:
            snapshot["last_price"] = "1000"
        self.assertEqual(self.execute(), 0, self.stderr.getvalue())
        self.mocks["sim_order"].assert_not_called()
        self.assert_no_intents()

    def test_partial_failure_preserves_intent_and_never_retries(self):
        self.mocks["sim_order"].side_effect = [
            {"order_id": "one"}, FutuTransportError("uncertain"),
        ]
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.mocks["sim_order"].call_count, 2)
        events = [event["event"] for event in self.events()]
        self.assertEqual(events[-5:], [
            "plan", "submitting", "response", "submitting", "submission_uncertain",
        ])
        self.assertIn("Earlier orders may exist", self.stderr.getvalue())
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.mocks["sim_order"].call_count, 2)

    def test_confirmation_warning_stops_without_accepting(self):
        self.mocks["sim_order"].side_effect = OrderConfirmationRequired("review required")
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.mocks["sim_order"].call_count, 1)


class WorkflowTest(unittest.TestCase):
    def test_protective_stop_preview_uses_cost_and_skips_zero_targets(self):
        targets = [
            {"symbol": "AAA", "qty": 5}, {"symbol": "BBB", "qty": 2},
            {"symbol": "CCC", "qty": 0},
        ]
        holdings = {"AAA": {"qty": Decimal("3"), "cost": Decimal("300")}}
        prices = {"AAA": Decimal("120"), "BBB": Decimal("50"), "CCC": Decimal("1000")}
        stops = orders.protective_stop_preview(targets, holdings, prices, Decimal("0.10"))
        self.assertEqual([row["symbol"] for row in stops], ["AAA", "BBB"])
        self.assertEqual(Decimal(stops[0]["trigger_price"]), Decimal("90"))
        self.assertEqual(stops[0]["qty"], 5)
        self.assertNotIn("anchor", stops[0])
        self.assertEqual(Decimal(stops[1]["trigger_price"]), Decimal("45"))
        disabled = orders.protective_stop_preview(targets, holdings, prices, Decimal("0"))
        self.assertTrue(all(row["trigger_price"] is None for row in disabled))

    def test_risk_estimate_tightens_stop_up_to_the_limit(self):
        positions = {"MRNA": {"qty": Decimal("10"), "cost": Decimal("1000")}}
        prices = {"MRNA": Decimal("94")}
        risk = {"MRNA": {"stop_fraction": 0.05, "as_of": "2026-10-23"}}
        sells = orders.stop_loss_plan(positions, prices, Decimal("0.10"), risk)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "stop_loss")
        # A risk estimate looser than the limit never loosens the stop.
        loose = {"MRNA": {"stop_fraction": 0.20, "as_of": "2026-10-23"}}
        self.assertEqual(orders.stop_loss_plan(positions, prices, Decimal("0.10"), loose), [])
        # Zero disables exits even when a risk estimate exists.
        self.assertEqual(orders.stop_loss_plan(positions, prices, Decimal("0"), risk), [])
        # Unusable estimates (>= 100% or non-positive) fall back to the limit.
        for fraction in (1.4, 0, -0.1):
            bad = {"MRNA": {"stop_fraction": fraction, "as_of": "2026-10-23"}}
            self.assertEqual(orders.stop_loss_plan(positions, prices, Decimal("0.10"), bad), [])
            self.assertEqual(
                len(orders.stop_loss_plan(positions, {"MRNA": Decimal("90")},
                                          Decimal("0.10"), bad)),
                1,
            )

    def test_fixed_stop_loss_threshold_and_disable(self):
        positions = {"MRNA": {"qty": Decimal("10"), "cost": Decimal("1000")}}
        for price, expected in (("91", 0), ("90", 1), ("89", 1)):
            with self.subTest(price=price):
                sells = orders.stop_loss_plan(
                    positions, {"MRNA": Decimal(price)}, Decimal("0.10"),
                )
                self.assertEqual(len(sells), expected)
                if sells:
                    self.assertEqual(sells[0]["side"], "SELL")
                    self.assertEqual(sells[0]["qty"], 10)
                    self.assertEqual(sells[0]["reason"], "stop_loss")
        self.assertEqual(
            orders.stop_loss_plan(positions, {"MRNA": Decimal("1")}, Decimal("0")), [],
        )

    def test_default_lookback_matches_risk_parity_cli(self):
        self.assertEqual(orders.default_lookback(), 63)

    def test_failed_screen_does_not_run_optimizer(self):
        temporary_paths = []

        def fail_screen(command, **kwargs):
            path = output_path(command[command.index("--output") + 1])
            path.write_text("partial output", encoding="utf-8")
            temporary_paths.append(path)
            raise subprocess.CalledProcessError(2, "screen")

        with patch.object(orders.subprocess, "run", side_effect=fail_screen) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                orders.screen_and_allocate("2026-10-20", 252)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(temporary_paths), 1)
        self.assertTrue(all(not path.parent.exists() for path in temporary_paths))

    def test_pipeline_fetches_allocation_history_and_cleans_up(self):
        def run_stage(command, **kwargs):
            if "--output" in command:
                path = output_path(command[command.index("--output") + 1])
                self.assertEqual(path.parent.parent, orders.ROOT / ".cache" / "finacial")
                path.write_text("isolated output", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)
            self.assertTrue(Path(command[2]).is_file())
            self.assertNotIn("--prices", command)
            return subprocess.CompletedProcess(command, 0, stdout='{"weights": []}')

        with (
            TemporaryDirectory() as other_directory,
            patch.dict(os.environ, {"TMPDIR": other_directory}),
            patch.object(orders.subprocess, "run", side_effect=run_stage) as run,
        ):
            for lookback in (20, 252):
                self.assertEqual(
                    orders.screen_and_allocate("2026-10-20", lookback), {"weights": []},
                )
        self.assertNotEqual(run.call_args_list[0].args[0], run.call_args_list[2].args[0])
        self.assertEqual(run.call_count, 4)
        first, second = [call.args[0] for call in run.call_args_list[:2]]
        self.assertEqual(first[first.index("--method") + 1], "ensemble")
        self.assertEqual(first[first.index("--top") + 1], "5")
        self.assertNotIn("--save-prices", first)
        self.assertEqual(first[first.index("--output") + 1], second[2])
        self.assertEqual(second[second.index("--lookback") + 1], "20")
        last = run.call_args_list[-1].args[0]
        self.assertEqual(last[last.index("--lookback") + 1], "252")
        self.assertFalse(Path(second[2]).parent.exists())


if __name__ == "__main__":
    unittest.main()

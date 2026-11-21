"""Offline durable fill accounting, account isolation and reconciliation."""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from broker import FutuClient, SimTradingClient, TokenSet
from broker.client import FutuTransportError
from strategy_ledger import StrategyLedger, broker_positions


class StrategyLedgerTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "strategy.sqlite3"
        self.ledger = StrategyLedger(self.path).__enter__()
        self.addCleanup(self.ledger.__exit__)
        self.client = SimTradingClient(TokenSet("offline"), account_id="sim", market=100)
        self.ledger.bind("sim", Decimal("3000"), self.funds("10000"), {"positions": []}, [])
        self.network = patch("broker.client._OPENER.open", side_effect=AssertionError("No network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    @staticmethod
    def funds(balance):
        return {"balance": balance, "hold": "0", "max_power_long": "20000"}

    @staticmethod
    def positions(**quantities):
        return {"positions": [
            {"symbol": symbol, "qty": str(qty), "pstn_type": 0, "market": 100}
            for symbol, qty in quantities.items()
        ]}

    def submit(self, qty=3, side="BUY", symbol="AAA"):
        count = self.ledger.db.execute("SELECT count(*) FROM intents").fetchone()[0]
        order_id = str(count + 1)
        plan = {"orders": [{
            "symbol": symbol, "qty": qty, "side": side, "limit_price": "100.00",
        }]}
        with patch.object(self.client, "place_order", return_value={"order_id": order_id}) as place:
            self.ledger.submit(self.client, plan)
        row = self.ledger.db.execute("SELECT * FROM intents WHERE order_id=?", (order_id,)).fetchone()
        self.assertEqual(place.call_args.kwargs["text"], row["text"])
        return {
            "order_id": order_id, "symbol": symbol, "qty": str(qty),
            "side": 1 if side == "BUY" else 2, "status": 2, "cum_qty": "0",
            "avg_fill_price": "0", "text": row["text"], "update_time": "1",
        }

    @staticmethod
    def fill(row, qty, price="100", status=4, updated="2"):
        return {**row, "cum_qty": str(qty), "avg_fill_price": price,
                "status": status, "update_time": updated}

    def test_partial_fills_are_incremental_and_idempotent(self):
        row = self.submit()
        partial = self.fill(row, 1, status=3)
        self.assertFalse(self.ledger.reconcile([partial], self.funds("9900"), self.positions(AAA=1)))
        self.assertFalse(self.ledger.reconcile([partial], self.funds("9900"), self.positions(AAA=1)))
        final = self.fill(row, 3, "110", updated="3")
        self.assertTrue(self.ledger.reconcile([final], self.funds("9670"), self.positions(AAA=3)))
        self.assertTrue(self.ledger.reconcile([final], self.funds("9670"), self.positions(AAA=3)))
        book = self.ledger.book()
        self.assertEqual(book["cash"], Decimal("2670"))
        self.assertEqual(book["positions"]["AAA"], {"qty": Decimal(3), "cost": Decimal(330)})
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM fills").fetchone()[0], 2)

    def test_assumed_sim_fee_once_across_partial_fills_restart_and_cancellation(self):
        row = self.submit()
        self.assertFalse(self.ledger.reconcile(
            [row], self.funds("10000"), self.positions(), simulate_order_fees=True,
        ))
        self.assertEqual(self.ledger.book()["fees"], Decimal(0))
        partial = self.fill(row, 1, status=3)
        for _ in range(2):
            self.assertFalse(self.ledger.reconcile(
                [partial], self.funds("9899.01"), self.positions(AAA=1), simulate_order_fees=True,
            ))
        self.ledger.__exit__()
        with StrategyLedger(self.path) as ledger:
            cancelled = self.fill(row, 2, status=5, updated="3")
            self.assertTrue(ledger.reconcile(
                [cancelled], self.funds("9799.01"), self.positions(AAA=2),
                simulate_order_fees=True,
            ))
            self.assertEqual(ledger.book()["cash"], Decimal("2799.01"))
            self.assertEqual(ledger.book()["fees"], Decimal("0.99"))
            self.assertEqual(ledger.report({"AAA": Decimal("100")})["total_pnl_usd"], "-0.99")
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM expenses").fetchone()[0], 1)

    def test_backfill_five_filled_orders_fees_and_sell_fee(self):
        rows = []
        for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            rows.append(self.fill(self.submit(symbol=symbol), 3))
            self.ledger.sync_orders(rows)
        for _ in range(2):
            self.assertTrue(self.ledger.reconcile(
                rows, self.funds("8495.05"), self.positions(AAA=3, BBB=3, CCC=3, DDD=3, EEE=3),
                simulate_order_fees=True,
            ))
        self.assertEqual(self.ledger.book()["fees"], Decimal("4.95"))
        sale = self.fill(self.submit(qty=1, side="SELL"), 1)
        self.assertTrue(self.ledger.reconcile(
            [*rows, sale], self.funds("8594.06"), self.positions(AAA=2, BBB=3, CCC=3, DDD=3, EEE=3),
            simulate_order_fees=True,
        ))
        self.assertEqual(self.ledger.book()["fees"], Decimal("5.94"))

    def test_unfilled_terminal_order_has_no_assumed_fee(self):
        row = self.submit()
        self.assertTrue(self.ledger.reconcile(
            [{**row, "status": 6}], self.funds("10000"), self.positions(),
            simulate_order_fees=True,
        ))
        self.assertEqual(self.ledger.book()["fees"], Decimal(0))

    def test_readable_history_has_incremental_fills_and_separate_verified_fees(self):
        row = self.submit()
        partial = self.fill(row, 1, status=3)
        self.ledger.reconcile([partial], self.funds("9900"), self.positions(AAA=1))
        final = self.fill(row, 3, "110", updated="3")
        self.ledger.reconcile([final], self.funds("9670"), self.positions(AAA=3))
        self.ledger.add_expense("fee-1", Decimal("0.99"), Decimal("0"), "verified statement")
        self.ledger.export_transactions()
        first = self.ledger.transactions_path.read_text()
        self.ledger.export_transactions()
        self.assertEqual(self.ledger.transactions_path.read_text(), first)
        for text in (
            "BUY AAA | FILLED", "Broker order: 1", "Filled: 3 shares", "Average: 110 USD",
            "+1 shares at 100 USD", "+2 shares at 115 USD", "total=2.01",
            "RECORDED EXPENSES", "fees 0.99 USD", "verified statement", "Estimates are NOT booked charges",
        ):
            self.assertIn(text, first)
        self.assertEqual(first.count("  Observed "), 2)
        self.assertEqual(self.ledger.book()["fees"], Decimal("0.99"))
        self.assertFalse(self.ledger.transactions_path.with_suffix(".txt.tmp").exists())

    def test_readable_history_survives_cash_mismatch_and_uncertain_submission(self):
        row = self.fill(self.submit(), 3)
        with self.assertRaisesRegex(ValueError, "cash mismatch"):
            self.ledger.reconcile([row], self.funds("9699.01"), self.positions(AAA=3))
        self.ledger.export_transactions()
        self.assertIn("FILLED", self.ledger.transactions_path.read_text())
        with patch.object(self.client, "place_order", side_effect=FutuTransportError("timeout")):
            with self.assertRaises(FutuTransportError):
                self.ledger.submit(self.client, {"orders": [
                    {"symbol": "BBB", "qty": 1, "side": "BUY", "limit_price": "100"},
                ]})
        self.ledger.export_transactions()
        history = self.ledger.transactions_path.read_text()
        self.assertIn("BUY BBB | UNCERTAIN", history)
        self.assertIn("UNKNOWN; reconcile before retry", history)

    def test_sales_reduce_cost_and_record_realized_pnl(self):
        buy = self.fill(self.submit(), 3, "110")
        self.ledger.reconcile([buy], self.funds("9670"), self.positions(AAA=3))
        sale = self.fill(self.submit(1, "SELL"), 1, "120")
        self.ledger.reconcile([buy, sale], self.funds("9790"), self.positions(AAA=2))
        report = self.ledger.report({"AAA": Decimal(130)})
        self.assertEqual(Decimal(report["nav_usd"]), Decimal(3050))
        self.assertEqual(Decimal(report["realized_pnl_before_expenses_usd"]), Decimal(10))
        self.assertEqual(Decimal(report["unrealized_pnl_usd"]), Decimal(40))
        self.assertEqual(Decimal(report["total_pnl_usd"]), Decimal(50))
        self.assertEqual(report["positions"][0]["cost_usd"], "220")

    def test_cancelled_partial_fill_is_accounted_and_terminal(self):
        cancelled = self.fill(self.submit(), 1, status=5)
        self.assertTrue(self.ledger.reconcile(
            [cancelled], self.funds("9900"), self.positions(AAA=1),
        ))
        self.assertEqual(self.ledger.book()["positions"]["AAA"]["qty"], Decimal(1))

    def test_rejected_order_does_not_change_cash_or_holdings(self):
        rejected = self.fill(self.submit(), 0, "0", status=6)
        self.assertTrue(self.ledger.reconcile([rejected], self.funds("10000"), self.positions()))
        self.assertEqual(self.ledger.book()["cash"], Decimal(3000))
        self.assertEqual(self.ledger.book()["positions"], {})

    def test_account_and_initial_capital_are_bound_to_ledger(self):
        for account_id, capital in (("other", "3000"), ("sim", "4000")):
            with self.subTest(account_id=account_id, capital=capital), self.assertRaises(ValueError):
                self.ledger.bind(account_id, Decimal(capital), self.funds("10000"), self.positions(), [])

    def test_weekly_target_is_durable_and_unique_after_completion(self):
        plan = {"orders": [{"symbol": "AAA", "qty": 2}], "order_type": "MARKET"}
        self.ledger.save_cycle("2026-10-19", "2026-10-19", plan)
        cycle = self.ledger.active_cycle()
        self.assertEqual(cycle["week"], "2026-10-19")
        self.assertIn('"qty": 2', cycle["plan"])
        self.ledger.complete_cycle(cycle["week"])
        self.assertIsNone(self.ledger.active_cycle())
        self.assertEqual(self.ledger.db.execute(
            "SELECT count(*) FROM rebalance_cycles WHERE week='2026-10-19'",
        ).fetchone()[0], 1)

    def test_external_holdings_are_baseline_not_strategy_assets(self):
        path = self.path.parent / "baseline.sqlite3"
        with StrategyLedger(path) as ledger:
            ledger.bind("sim", Decimal(3000), self.funds("10000"), self.positions(AAA=10), [])
            self.assertTrue(ledger.reconcile([], self.funds("10000"), self.positions(AAA=10)))
            self.assertEqual(ledger.book()["positions"], {})
            self.assertEqual(ledger.report({})["nav_usd"], "3000")
            with self.assertRaisesRegex(ValueError, "positions differ"):
                ledger.reconcile([], self.funds("10000"), self.positions(AAA=9))

    def test_unexplained_cash_or_position_changes_block(self):
        with self.assertRaisesRegex(ValueError, "cash mismatch"):
            self.ledger.reconcile([], self.funds("9999"), self.positions())
        with self.assertRaisesRegex(ValueError, "positions differ"):
            self.ledger.reconcile([], self.funds("10000"), self.positions(AAA=1))

    def test_fresh_history_cutoff_persists_and_preserves_baseline(self):
        path = self.path.parent / "fresh.sqlite3"
        old = {"order_id": "old", "status": 4, "text": "finacial:old", "update_time": "99"}
        with StrategyLedger(path) as ledger:
            ledger.bind("sim", Decimal(3000), self.funds("10000"), self.positions(AAA=2),
                        [old], history_start=100)
            self.assertEqual(ledger.history_start(), 100)
            self.assertTrue(ledger.reconcile([old], self.funds("10000"), self.positions(AAA=2)))
            self.assertEqual(ledger.book()["positions"], {})
        with StrategyLedger(path) as ledger:
            self.assertEqual(ledger.history_start(), 100)
            self.assertTrue(ledger.reconcile([old], self.funds("10000"), self.positions(AAA=2)))
            for changed in ({"status": 2}, {"update_time": "100"}, {"update_time": "101"}):
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "Unattributed"):
                    ledger.sync_orders([{**old, **changed}])
            with self.assertRaisesRegex(ValueError, "fresh ledger"):
                ledger.bind("sim", Decimal(3000), self.funds("10000"), self.positions(AAA=2),
                            [], history_start=200)

    def test_history_cutoff_does_not_hide_open_orders_or_late_updates_at_initialization(self):
        old = {"order_id": "old", "status": 4, "text": "finacial:old", "update_time": "99"}
        for index, changed in enumerate(({"status": 2}, {"status": 3}, {"update_time": "100"})):
            with self.subTest(changed=changed), StrategyLedger(
                self.path.parent / f"blocked-{index}.sqlite3"
            ) as ledger:
                with self.assertRaisesRegex(ValueError, "Cannot initialize"):
                    ledger.bind("sim", Decimal(3000), self.funds("10000"), self.positions(),
                                [{**old, **changed}], history_start=100)

    def test_default_initialization_still_blocks_old_strategy_orders(self):
        with StrategyLedger(self.path.parent / "default.sqlite3") as ledger:
            with self.assertRaisesRegex(ValueError, "Cannot initialize"):
                ledger.bind("sim", Decimal(3000), self.funds("10000"), self.positions(), [
                    {"order_id": "old", "status": 4, "text": "finacial:old", "update_time": "99"},
                ])

    def test_sim_cash_reconciliation_minimum_tolerance_is_one_cent(self):
        for balance in ("9999.99", "10000.01"):
            with self.subTest(balance=balance):
                self.assertTrue(self.ledger.reconcile([], self.funds(balance), self.positions()))
                self.assertEqual(self.ledger.book()["cash"], Decimal("3000"))
        for balance in ("9999.9899", "10000.0101"):
            with self.subTest(balance=balance), self.assertRaisesRegex(ValueError, "cash mismatch"):
                self.ledger.reconcile([], self.funds(balance), self.positions())

    def test_sim_cash_difference_is_audited_without_adjusting_ledger(self):
        self.ledger.reconcile([], self.funds("9999.99"), self.positions())
        event = self.ledger.db.execute(
            "SELECT payload FROM events WHERE kind='reconciled' ORDER BY id DESC LIMIT 1"
        ).fetchone()["payload"]
        self.assertIn('"expected_cash": "10000"', event)
        self.assertIn('"cash_difference": "-0.01"', event)
        self.assertIn('"cash_tolerance": "0.01"', event)
        self.assertIn('"filled_orders": 0', event)
        self.assertEqual(self.ledger.book()["cash"], Decimal("3000"))
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM expenses").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "positions differ"):
            self.ledger.reconcile([], self.funds("9999.99"), self.positions(AAA=1))

    def test_sim_cash_tolerance_counts_each_filled_buy_and_sell_once(self):
        orders = []
        for index in range(8):
            side = "BUY" if index < 4 else "SELL"
            orders.append(self.fill(self.submit(qty=1, side=side), 1))
            held = index + 1 if index < 4 else 7 - index
            positions = self.positions(AAA=held) if held else self.positions()
            balance = Decimal("10000") - Decimal(100) * held
            self.assertTrue(self.ledger.reconcile(orders, self.funds(str(balance)), positions))
        for balance in ("9999.97", "9999.92", "10000.08"):
            self.assertTrue(self.ledger.reconcile(orders, self.funds(balance), self.positions()))
        self.ledger.__exit__()
        with StrategyLedger(self.path) as reopened:
            self.assertTrue(reopened.reconcile(orders, self.funds("9999.92"), self.positions()))
            for balance in ("9999.9199", "10000.0801"):
                with self.subTest(balance=balance), self.assertRaisesRegex(ValueError, "cash mismatch"):
                    reopened.reconcile(orders, self.funds(balance), self.positions())

    def test_sim_cash_tolerance_does_not_count_shares_partial_fills_or_unfilled_orders(self):
        order = self.submit(qty=3)
        self.assertFalse(self.ledger.reconcile([order], self.funds("10000"), self.positions()))
        for qty, status in ((1, 3), (2, 3), (3, 4)):
            filled = self.fill(order, qty, status=status, updated=str(qty + 1))
            balance = Decimal("10000") - Decimal(100) * qty
            self.ledger.reconcile([filled], self.funds(str(balance - Decimal("0.01"))),
                                  self.positions(AAA=qty))
            with self.assertRaisesRegex(ValueError, "cash mismatch"):
                self.ledger.reconcile([filled], self.funds(str(balance - Decimal("0.0101"))),
                                      self.positions(AAA=qty))
        unfilled = self.submit(qty=1)
        cancelled = {**unfilled, "status": 5}
        with self.assertRaisesRegex(ValueError, "cash mismatch"):
            self.ledger.reconcile([filled, cancelled], self.funds("9699.9899"), self.positions(AAA=3))

    def test_strategy_trades_do_not_consume_external_holdings_in_same_symbol(self):
        path = self.path.parent / "overlap.sqlite3"
        with StrategyLedger(path) as ledger:
            ledger.bind("sim", Decimal(3000), self.funds("10000"), self.positions(AAA=10), [])
            records = []
            for side, order_id, balance, broker_qty in (
                ("BUY", "buy", "9800", 12), ("SELL", "sell", "10000", 10),
            ):
                with patch.object(self.client, "place_order", return_value={"order_id": order_id}):
                    ledger.submit(self.client, {"orders": [
                        {"symbol": "AAA", "qty": 2, "side": side, "limit_price": "100"},
                    ]})
                records.append({
                    "order_id": order_id, "symbol": "AAA", "qty": "2",
                    "side": 1 if side == "BUY" else 2, "status": 4,
                    "cum_qty": "2", "avg_fill_price": "100",
                })
                self.assertTrue(ledger.reconcile(
                    records, self.funds(balance), self.positions(AAA=broker_qty),
                ))
            self.assertEqual(ledger.book()["positions"], {})
            self.assertEqual(ledger.book()["cash"], Decimal(3000))

    def test_expenses_are_explicit_idempotent_and_in_nav(self):
        self.ledger.add_expense("charge-1", Decimal(1), Decimal(2), "broker statement")
        self.ledger.add_expense("charge-1", Decimal(1), Decimal(2), "broker statement")
        self.assertTrue(self.ledger.reconcile([], self.funds("9997"), self.positions()))
        report = self.ledger.report({})
        self.assertEqual(report["fees_usd"], "1")
        self.assertEqual(report["interest_usd"], "2")
        self.assertEqual(report["nav_usd"], "2997")
        self.assertEqual(report["total_pnl_usd"], "-3")
        with self.assertRaises(ValueError):
            self.ledger.add_expense("charge-1", Decimal(2), Decimal(2), "broker statement")
        for fee, interest in ((Decimal(-1), Decimal(2)), (Decimal("NaN"), Decimal(0))):
            with self.assertRaises(ValueError):
                self.ledger.add_expense("invalid", fee, interest, "statement")

    def test_cash_debt_and_leverage_use_strategy_nav(self):
        path = self.path.parent / "margin.sqlite3"
        with StrategyLedger(path) as ledger:
            ledger.bind("sim", Decimal(3000), self.funds("3000"), self.positions(), [])
            with patch.object(self.client, "place_order", return_value={"order_id": "debt"}):
                ledger.submit(self.client, {"orders": [
                    {"symbol": "AAA", "qty": 35, "side": "BUY", "limit_price": "100"},
                ]})
            row = {"order_id": "debt", "qty": "35", "symbol": "AAA", "side": 1,
                   "cum_qty": "35", "avg_fill_price": "100", "status": 4}
            self.assertTrue(ledger.reconcile([row], self.funds("-500"), self.positions(AAA=35)))
            report = ledger.report({"AAA": Decimal(100)})
            self.assertEqual(report["nav_usd"], "3000")
            self.assertEqual(report["borrowing_usd"], "500")
            self.assertEqual(report["cash_usd"], "0")

    def test_pending_orders_block_submission(self):
        row = self.submit()
        self.assertFalse(self.ledger.reconcile([row], self.funds("10000"), self.positions()))
        with self.assertRaisesRegex(ValueError, "Outstanding"):
            self.submit()

    def test_uncertain_timeout_is_durable_and_recovered_by_unique_order_text(self):
        plan = {"orders": [{"symbol": "AAA", "qty": 3, "side": "BUY", "limit_price": "100"}]}
        with patch.object(self.client, "place_order", side_effect=FutuTransportError("timeout")):
            with self.assertRaises(FutuTransportError):
                self.ledger.submit(self.client, plan)
        intent = self.ledger.db.execute("SELECT * FROM intents").fetchone()
        self.assertIsNone(intent["order_id"])
        with self.assertRaisesRegex(ValueError, "Uncertain intent"):
            self.ledger.reconcile([], self.funds("10000"), self.positions())
        broker_row = {"order_id": "recovered", "qty": "3", "symbol": "AAA", "side": 1,
                      "cum_qty": "0", "avg_fill_price": "0", "status": 2, "text": intent["text"]}
        self.assertFalse(self.ledger.reconcile([broker_row], self.funds("10000"), self.positions()))
        self.assertEqual(self.ledger.db.execute("SELECT order_id FROM intents").fetchone()[0], "recovered")

    def test_missing_or_duplicate_broker_matches_never_retried(self):
        row = self.submit()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.ledger.reconcile([], self.funds("10000"), self.positions())
        with self.ledger.db:
            self.ledger.db.execute("UPDATE intents SET order_id=NULL")
        with self.assertRaisesRegex(ValueError, "unique broker order"):
            self.ledger.sync_orders([row, {**row, "order_id": "duplicate"}])

    def test_unknown_or_external_open_orders_block(self):
        external = {"order_id": "external", "status": 2, "text": ""}
        with self.assertRaisesRegex(ValueError, "Unattributed"):
            self.ledger.reconcile([external], self.funds("10000"), self.positions())

    def test_regressions_invalid_fills_and_unknown_states_are_atomic(self):
        row = self.submit()
        for changed in (
            {"status": 99}, {"side": 2}, {"symbol": "OTHER"}, {"qty": "4"},
            {"cum_qty": "4", "avg_fill_price": "100"},
            {"cum_qty": "1", "avg_fill_price": "0"},
            {"status": 4}, {"status": 6, "cum_qty": "1", "avg_fill_price": "100"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.ledger.sync_orders([{**row, **changed}])
        partial = self.fill(row, 1, status=3)
        self.ledger.sync_orders([partial])
        with self.assertRaisesRegex(ValueError, "regressed"):
            self.ledger.sync_orders([row])
        with self.assertRaisesRegex(ValueError, "revised fill"):
            self.ledger.sync_orders([{**partial, "avg_fill_price": "101"}])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM fills").fetchone()[0], 1)

    def test_latest_snapshot_wins_and_conflicting_timestamp_blocks(self):
        row = self.submit()
        final = self.fill(row, 3)
        self.ledger.sync_orders([final, row])
        self.assertEqual(self.ledger.book()["positions"]["AAA"]["qty"], Decimal(3))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.ledger.sync_orders([final, {**final, "status": 5}])

    def test_void_requires_manual_verification_and_no_matching_broker_order(self):
        row = self.submit()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE intents SET order_id=NULL")
        identity = self.ledger.db.execute("SELECT id FROM intents").fetchone()[0]
        with self.assertRaises(ValueError):
            self.ledger.void_uncertain(identity, "", [])
        with self.assertRaises(ValueError):
            self.ledger.void_uncertain(identity, "verified", [row])
        self.ledger.void_uncertain(identity, "broker verified no submission", [])
        self.assertTrue(self.ledger.reconcile([], self.funds("10000"), self.positions()))
        # A late broker appearance must block, not be silently ignored.
        with self.assertRaisesRegex(ValueError, "Unattributed"):
            self.ledger.reconcile([row], self.funds("10000"), self.positions())

    def test_restart_persists_book_and_deduplicates_execution(self):
        row = self.fill(self.submit(), 3)
        self.ledger.reconcile([row], self.funds("9700"), self.positions(AAA=3))
        expected = self.ledger.book()
        self.ledger.__exit__()
        with StrategyLedger(self.path) as restarted:
            restarted.bind("sim", Decimal(3000), self.funds("9700"), self.positions(AAA=3), [row])
            restarted.reconcile([row], self.funds("9700"), self.positions(AAA=3))
            self.assertEqual(restarted.book(), expected)
            self.assertEqual(restarted.db.execute("SELECT count(*) FROM fills").fetchone()[0], 1)

    def test_client_type_market_and_account_must_match(self):
        for client in (
            FutuClient(TokenSet("offline"), account_id="sim", dry_run=False, allow_live_trading=True),
            SimTradingClient(TokenSet("offline"), account_id="other", market=100),
            SimTradingClient(TokenSet("offline"), account_id="sim", market=2),
        ):
            with self.subTest(client=client), self.assertRaises(ValueError):
                self.ledger.submit(client, {"orders": []})
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM intents").fetchone()[0], 0)

    def test_short_positions_and_invalid_amounts_are_rejected(self):
        for row in (
            {"symbol": "AAA", "qty": "-1", "pstn_type": 0, "market": 100},
            {"symbol": "AAA", "qty": "NaN", "pstn_type": 0, "market": 100},
            {"symbol": "AAA", "qty": "1", "pstn_type": 1, "market": 100},
        ):
            with self.assertRaises(ValueError):
                broker_positions({"positions": [row]})


if __name__ == "__main__":
    unittest.main()

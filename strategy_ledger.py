"""Durable accounting and fail-closed reconciliation for one simulation strategy."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from broker import SimTradingClient
from broker.client import FutuAPIError, FutuError
from us_stock_fees import (
    FEE_SCHEDULE, FEE_SOURCE, FINRA_SOURCE, SEC_SOURCE, SIM_FEE_SCHEDULE, SIM_ORDER_FEE,
    estimate_sim_order_fees, estimate_us_stock_fees,
)


ZERO = Decimal(0)
TERMINAL = {4, 5, 6}
ORDER_PREFIX = "finacial:"


def number(value: object, name: str, *, signed: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or (result < 0 and not signed):
        raise ValueError(f"{name} must be finite" + ("" if signed else " and non-negative"))
    return result


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def broker_positions(response: dict) -> dict[str, Decimal]:
    positions = {}
    for row in response["positions"]:
        qty = number(row["qty"], "position quantity")
        if qty == 0:
            continue
        if row["pstn_type"] not in (0, "0") or row["market"] not in (100, "100"):
            raise ValueError("Only long US simulation positions are supported")
        symbol = row["symbol"]
        positions[symbol] = positions.get(symbol, ZERO) + qty
    return positions


class StrategyLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> StrategyLedger:
        self.path = self.path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix + ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise ValueError("Strategy ledger is already in use; do not run concurrently") from exc
        try:
            self.db = sqlite3.connect(self.path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS strategy (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    account_id TEXT NOT NULL, initial_capital TEXT NOT NULL,
                    baseline_cash TEXT NOT NULL, baseline_positions TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    qty TEXT NOT NULL, price TEXT NOT NULL, text TEXT NOT NULL UNIQUE,
                    order_id TEXT UNIQUE, status INTEGER,
                    cum_qty TEXT NOT NULL DEFAULT '0', cum_notional TEXT NOT NULL DEFAULT '0'
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id),
                    qty TEXT NOT NULL, notional TEXT NOT NULL,
                    cum_qty TEXT NOT NULL, cum_notional TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    UNIQUE(intent_id, cum_qty, cum_notional)
                );
                CREATE TABLE IF NOT EXISTS expenses (
                    id TEXT PRIMARY KEY, fee TEXT NOT NULL, interest TEXT NOT NULL,
                    note TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rebalance_cycles (
                    week TEXT PRIMARY KEY, scheduled_date TEXT NOT NULL,
                    plan TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS weekly_inputs (
                    week TEXT PRIMARY KEY, prepared_at TEXT NOT NULL,
                    lookback INTEGER NOT NULL, allocation TEXT NOT NULL
                );
            """)
        except (OSError, sqlite3.Error):
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise
        return self

    def __exit__(self, *args) -> None:
        if self.lock.closed:
            return
        try:
            self.export_transactions()
        finally:
            self.db.close()
            self.lock.close()

    def event(self, kind: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO events(recorded_at,kind,payload) VALUES (?,?,?)",
            (now(), kind, json.dumps(payload)),
        )

    def active_cycle(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM rebalance_cycles WHERE completed_at IS NULL ORDER BY week LIMIT 1"
        ).fetchone()

    def save_cycle(self, week: str, scheduled_date: str, plan: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO rebalance_cycles(week,scheduled_date,plan) VALUES (?,?,?)",
                (week, scheduled_date, json.dumps(plan)),
            )
            self.event("weekly_target", {"week": week, "scheduled_date": scheduled_date, "plan": plan})

    def complete_cycle(self, week: str) -> None:
        with self.db:
            self.db.execute("UPDATE rebalance_cycles SET completed_at=? WHERE week=?", (now(), week))
            self.event("weekly_complete", {"week": week})

    def save_inputs(self, week: str, prepared_at: str, lookback: int, allocation: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO weekly_inputs VALUES (?,?,?,?) ON CONFLICT(week) DO UPDATE SET "
                "prepared_at=excluded.prepared_at,lookback=excluded.lookback,allocation=excluded.allocation",
                (week, prepared_at, lookback, json.dumps(allocation)),
            )
            self.event("weekend_inputs", {"week": week, "prepared_at": prepared_at, "allocation": allocation})

    @property
    def transactions_path(self) -> Path:
        return self.path.with_suffix(".transactions.txt")

    def export_transactions(self) -> None:
        lines = [
            "SIMULATION STRATEGY TRANSACTIONS", f"Ledger: {self.path}",
            "Times: UTC, observed by reconciliation (not exchange execution timestamps).",
            f"Estimated fees: {FEE_SCHEDULE}", FEE_SOURCE, FINRA_SOURCE, SEC_SOURCE,
            "Estimates are NOT booked charges. Rounded up per component to cents.",
            "Estimates assume one trading day per order; multi-day fills can cost more.",
            "No financing interest, ADR/corporate-action fees, promotions or OTC pricing included.",
            SIM_FEE_SCHEDULE + "; automatically booked by simulation reconciliation.",
        ]
        state = self.db.execute("SELECT account_id FROM strategy WHERE id=1").fetchone()
        lines.append(f"Account: {state['account_id'] if state else 'not yet bound'}")
        submitting = {
            payload["intent_id"]: payload
            for row in self.db.execute("SELECT payload FROM events WHERE kind='submitting' ORDER BY id")
            for payload in [json.loads(row["payload"])]
        }
        statuses = {None: "UNCERTAIN", 2: "SUBMITTED", 3: "PARTIALLY FILLED",
                    4: "FILLED", 5: "CANCELLED", 6: "REJECTED"}
        for row in self.db.execute("SELECT * FROM intents ORDER BY created_at,id"):
            detail = submitting.get(row["id"], {})
            qty, notional = Decimal(row["cum_qty"]), Decimal(row["cum_notional"])
            estimated = detail.get("order", {}).get("estimated_fees")
            status = statuses[row["status"]]
            if row["status"] is None and row["order_id"] is not None:
                status = "SUBMITTED; awaiting broker status"
            lines.extend([
                "", f"{row['created_at']} | {row['side']} {row['symbol']} | {status}",
                f"Intent: {row['id']} | Broker order: {row['order_id'] or 'UNKNOWN; reconcile before retry'}",
                f"Requested: {row['qty']} shares | Type: {detail.get('order_type', 'not recorded')} | "
                f"Reference/limit: {row['price']} USD",
                f"Filled: {qty} shares | Notional: {notional} USD | "
                f"Average: {notional / qty if qty else 'N/A'} USD",
                f"Estimated order fees at submission: {estimated['total'] if estimated else 'not recorded'} USD",
            ])
            fees = estimate_us_stock_fees(qty, notional, row["side"])
            lines.append("Estimated fees on cumulative fills (USD): " + " | ".join(
                f"{name}={value}" for name, value in fees.items()
            ))
            lines.append("Simulation assumed fee on cumulative fills (USD): " +
                         estimate_sim_order_fees(qty, notional, row["side"])["total"])
            for fill in self.db.execute("SELECT * FROM fills WHERE intent_id=? ORDER BY id", (row["id"],)):
                price = Decimal(fill["notional"]) / Decimal(fill["qty"])
                lines.append(f"  Observed {fill['recorded_at']} | +{fill['qty']} shares at {price} USD | "
                             f"+{fill['notional']} USD | cumulative {fill['cum_qty']} shares")
        lines.extend(["", "RECORDED EXPENSES (includes assumed simulation fees; see notes)"])
        for expense in self.db.execute("SELECT * FROM expenses ORDER BY recorded_at,id"):
            lines.append(f"{expense['recorded_at']} | {expense['id']} | fees {expense['fee']} USD | "
                         f"interest {expense['interest']} USD | {expense['note']}")
        temporary = self.transactions_path.with_suffix(".txt.tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(self.transactions_path)

    def bind(
        self, account_id: str, capital: Decimal, funds: dict, positions: dict,
        orders: list[dict], *, history_start: int | None = None,
    ) -> None:
        state = self.db.execute("SELECT * FROM strategy WHERE id=1").fetchone()
        if state:
            if history_start is not None:
                raise ValueError("History cutoff may only be set on a fresh ledger")
            if state["account_id"] != account_id or Decimal(state["initial_capital"]) != capital:
                raise ValueError("Ledger account/initial capital differs; do not reset an existing strategy")
            return
        balance = number(funds["balance"], "account balance")
        hold = number(funds["hold"], "account frozen cash")
        if balance - hold < capital:
            raise ValueError("Insufficient available USD cash to fund the initial strategy capital")
        if any(
            order["status"] not in (4, 5, 6, "4", "5", "6")
            or str(order.get("text", "")).startswith(ORDER_PREFIX)
            for order in self.orders_since_start(orders, history_start)
        ):
            raise ValueError("Cannot initialize around open orders or earlier strategy orders")
        baseline = broker_positions(positions)
        with self.db:
            self.db.execute(
                "INSERT INTO strategy VALUES (1,?,?,?,?,?)",
                (account_id, str(capital), str(balance),
                 json.dumps({s: str(q) for s, q in baseline.items()}), now()),
            )
            self.event("initialized", {
                "account_id": account_id, "capital_usd": str(capital),
                "history_start": history_start,
            })

    def add_expense(self, expense_id: str, fee: Decimal, interest: Decimal, note: str) -> None:
        if any(not value.is_finite() or value < 0 for value in (fee, interest)):
            raise ValueError("Expense amounts must be finite and non-negative")
        if not expense_id.strip() or not note.strip() or fee + interest <= 0:
            raise ValueError("Expense requires a unique ID, positive fee/interest and explanatory note")
        old = self.db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if old:
            if (Decimal(old["fee"]), Decimal(old["interest"]), old["note"]) != (fee, interest, note):
                raise ValueError("Expense ID already exists with different values")
            return
        with self.db:
            self.db.execute("INSERT INTO expenses VALUES (?,?,?,?,?)",
                            (expense_id, str(fee), str(interest), note, now()))
            self.event("expense", {"id": expense_id, "fee": str(fee),
                                   "interest": str(interest), "note": note})

    def book(self) -> dict:
        state = self.db.execute("SELECT * FROM strategy WHERE id=1").fetchone()
        if state is None:
            raise ValueError("Strategy ledger is not initialized")
        cash = Decimal(state["initial_capital"])
        positions = {}
        realized = ZERO
        for row in self.db.execute(
            "SELECT fills.*, intents.symbol, intents.side FROM fills "
            "JOIN intents ON intents.id=fills.intent_id ORDER BY fills.id"
        ):
            symbol, qty, notional = row["symbol"], Decimal(row["qty"]), Decimal(row["notional"])
            holding = positions.setdefault(symbol, {"qty": ZERO, "cost": ZERO})
            if row["side"] == "BUY":
                holding["qty"] += qty
                holding["cost"] += notional
                cash -= notional
            else:
                if qty > holding["qty"]:
                    raise ValueError(f"{symbol}: strategy sale exceeds its own holdings")
                cost = holding["cost"] * qty / holding["qty"]
                holding["qty"] -= qty
                holding["cost"] -= cost
                cash += notional
                realized += notional - cost
        fee = interest = ZERO
        for row in self.db.execute("SELECT fee,interest FROM expenses"):
            fee += Decimal(row["fee"])
            interest += Decimal(row["interest"])
        return {
            "cash": cash - fee - interest, "initial_capital": Decimal(state["initial_capital"]),
            "positions": {s: p for s, p in positions.items() if p["qty"] != 0},
            "realized": realized, "fees": fee, "interest": interest,
        }

    def history_start(self) -> int | None:
        cutoff = self.history_cutoff()
        row = self.db.execute("SELECT min(created_at) AS created_at FROM intents").fetchone()
        if row["created_at"] is None:
            return cutoff
        start = int(datetime.fromisoformat(row["created_at"]).timestamp() * 1_000_000) - 86_400_000_000
        return max(start, cutoff) if cutoff is not None else start

    def history_cutoff(self) -> int | None:
        row = self.db.execute(
            "SELECT payload FROM events WHERE kind='initialized' ORDER BY id LIMIT 1"
        ).fetchone()
        return json.loads(row["payload"]).get("history_start") if row is not None else None

    def orders_since_start(self, orders: list[dict], cutoff: int | None = None) -> list[dict]:
        if cutoff is None:
            cutoff = self.history_cutoff()
        if cutoff is None:
            return orders
        return [
            order for order in orders
            if str(order["status"]) not in {"4", "5", "6"}
            or number(order["update_time"], "order update timestamp") >= cutoff
        ]

    def sync_orders(self, orders: list[dict]) -> None:
        by_id = {}
        for row in self.orders_since_start(orders):
            order_id = str(row["order_id"])
            if order_id in by_id and by_id[order_id] != row:
                old_time = number(by_id[order_id]["update_time"], "order update timestamp")
                new_time = number(row["update_time"], "order update timestamp")
                if new_time < old_time:
                    continue
                if new_time == old_time:
                    raise ValueError(f"Conflicting broker snapshots for order {order_id}; retry reconciliation")
            by_id[order_id] = row
        with self.db:
            for intent in self.db.execute("SELECT * FROM intents ORDER BY created_at").fetchall():
                if intent["order_id"] is None and intent["status"] == 6:
                    continue
                if intent["order_id"] is None:
                    matches = [row for row in by_id.values() if row.get("text") == intent["text"]]
                    if len(matches) != 1:
                        raise ValueError(
                            f"Uncertain intent {intent['id']}: cannot identify a unique broker order; "
                            "verify with broker, never resubmit blindly"
                        )
                    row = matches[0]
                else:
                    row = by_id.get(intent["order_id"])
                    if row is None:
                        raise ValueError(f"Broker order {intent['order_id']} missing; stop for reconciliation")
                self._sync_order(intent, row)
            known = {
                row["order_id"] for row in self.db.execute("SELECT order_id FROM intents")
            }
            for row in by_id.values():
                status = row["status"]
                if str(row["order_id"]) not in known and (
                    status not in (4, 5, 6, "4", "5", "6")
                    or str(row.get("text", "")).startswith(ORDER_PREFIX)
                ):
                    raise ValueError("Unattributed broker order found; stop instead of assigning it to strategy")

    def void_uncertain(self, identity: str, note: str, orders: list[dict]) -> None:
        intent = self.db.execute("SELECT * FROM intents WHERE id=?", (identity,)).fetchone()
        if intent is None or intent["order_id"] is not None or Decimal(intent["cum_qty"]) != 0:
            raise ValueError("Only an uncertain intent without broker ID/fills may be voided")
        if not note.strip():
            raise ValueError("Voiding requires a note confirming broker verification of non-submission")
        if any(row.get("text") == intent["text"] for row in orders):
            raise ValueError("Broker order matches this intent; reconcile it instead of voiding")
        if intent["status"] == 6:
            return
        with self.db:
            self.db.execute("UPDATE intents SET status=6 WHERE id=?", (identity,))
            self.event("manually_voided", {"intent_id": identity, "verification_note": note})

    def _sync_order(self, intent: sqlite3.Row, order: dict) -> None:
        side = 1 if intent["side"] == "BUY" else 2
        if (
            order["symbol"] != intent["symbol"]
            or order["side"] not in (side, str(side))
            or number(order["qty"], "order quantity") != Decimal(intent["qty"])
        ):
            raise ValueError("Broker order identity differs from the persisted intent")
        status_value = order["status"]
        statuses = {str(value): value for value in (2, 3, 4, 5, 6)}
        if str(status_value) not in statuses:
            raise ValueError("Unknown broker order status")
        status = statuses[str(status_value)]
        qty = number(order["cum_qty"], "cumulative filled quantity")
        if qty > Decimal(intent["qty"]):
            raise ValueError("Broker filled quantity exceeds the original order")
        price = number(order["avg_fill_price"], "average fill price") if qty else ZERO
        if qty and price <= 0:
            raise ValueError("Filled order has no valid average fill price")
        notional = qty * price
        old_qty, old_notional = Decimal(intent["cum_qty"]), Decimal(intent["cum_notional"])
        if qty < old_qty or notional < old_notional:
            raise ValueError("Cumulative execution regressed; manual reconciliation required")
        if qty == old_qty and notional != old_notional:
            raise ValueError("Broker revised fill price without quantity; manual reconciliation required")
        if intent["status"] in TERMINAL and status != intent["status"]:
            raise ValueError("Terminal order status changed; manual reconciliation required")
        if intent["status"] in TERMINAL and qty != old_qty:
            raise ValueError("Terminal cumulative execution changed; manual reconciliation required")
        if status == 2 and qty:
            raise ValueError("Submitted order unexpectedly has fills without a partial/filled status")
        if status == 3 and not 0 < qty < Decimal(intent["qty"]):
            raise ValueError("Partially filled order has inconsistent cumulative quantity")
        if status == 4 and qty != Decimal(intent["qty"]):
            raise ValueError("Fully filled order has inconsistent cumulative quantity")
        if status == 6 and qty:
            raise ValueError("Rejected order unexpectedly has executions")
        if qty > old_qty:
            if notional <= old_notional:
                raise ValueError("New fill has invalid incremental notional")
            self.db.execute(
                "INSERT INTO fills(intent_id,qty,notional,cum_qty,cum_notional,recorded_at) "
                "VALUES (?,?,?,?,?,?)",
                (intent["id"], str(qty - old_qty), str(notional - old_notional),
                 str(qty), str(notional), now()),
            )
        order_id = str(order["order_id"])
        if (intent["order_id"], intent["status"], old_qty, old_notional) != (
            order_id, status, qty, notional,
        ):
            self.event("broker_order", order)
        self.db.execute(
            "UPDATE intents SET order_id=?,status=?,cum_qty=?,cum_notional=? WHERE id=?",
            (order_id, status, str(qty), str(notional), intent["id"]),
        )

    def reconcile(self, orders: list[dict], funds: dict, positions: dict, *,
                  simulate_order_fees: bool = False) -> bool:
        self.sync_orders(orders)
        if simulate_order_fees:
            for intent in self.db.execute("SELECT id,cum_qty FROM intents").fetchall():
                if Decimal(intent["cum_qty"]) > 0:
                    self.add_expense(
                        "sim-order-fee:" + intent["id"], SIM_ORDER_FEE, ZERO,
                        "Assumed simulation fee: USD 0.99 once per order with fills; not broker-reported",
                    )
        book = self.book()
        state = self.db.execute("SELECT * FROM strategy WHERE id=1").fetchone()
        expected_positions = {
            s: Decimal(q) for s, q in json.loads(state["baseline_positions"]).items()
        }
        for symbol, holding in book["positions"].items():
            expected_positions[symbol] = expected_positions.get(symbol, ZERO) + holding["qty"]
        actual = broker_positions(positions)
        if actual != expected_positions:
            raise ValueError("Broker positions differ from baseline + strategy; no new orders allowed")
        expected_cash = Decimal(state["baseline_cash"]) + book["cash"] - book["initial_capital"]
        actual_cash = number(funds["balance"], "broker cash", signed=True)
        cash_difference = actual_cash - expected_cash
        filled_orders = sum(
            Decimal(row["cum_qty"]) > ZERO
            for row in self.db.execute("SELECT cum_qty FROM intents")
        )
        cash_tolerance = Decimal("0.01") * max(1, filled_orders)
        if abs(cash_difference) > cash_tolerance:
            raise ValueError(
                f"Broker cash mismatch: expected {expected_cash}, got {actual_cash}; "
                f"tolerance {cash_tolerance} USD; verify fees/interest or external activity"
            )
        pending = self.db.execute(
            "SELECT count(*) FROM intents WHERE status IS NULL OR status NOT IN (4,5,6)"
        ).fetchone()[0]
        with self.db:
            self.event("reconciled", {
                "broker_cash": str(actual_cash), "expected_cash": str(expected_cash),
                "cash_difference": str(cash_difference), "cash_tolerance": str(cash_tolerance),
                "filled_orders": filled_orders,
                "pending_orders": pending,
            })
        return pending == 0

    def report(self, prices: dict[str, Decimal]) -> dict:
        book = self.book()
        holdings = []
        market_value = unrealized = ZERO
        for symbol, holding in book["positions"].items():
            price = prices[symbol]
            value = holding["qty"] * price
            market_value += value
            unrealized += value - holding["cost"]
            holdings.append({
                "symbol": symbol, "qty": str(holding["qty"]), "cost_usd": str(holding["cost"]),
                "average_cost": str(holding["cost"] / holding["qty"]), "mark": str(price),
                "market_value_usd": str(value), "unrealized_pnl_usd": str(value - holding["cost"]),
            })
        nav = book["cash"] + market_value
        result = {
            "initial_capital_usd": str(book["initial_capital"]),
            "nav_usd": str(nav), "cash_usd": str(max(book["cash"], ZERO)),
            "borrowing_usd": str(max(-book["cash"], ZERO)), "market_value_usd": str(market_value),
            "gross_leverage": str(market_value / nav) if nav > 0 else None,
            "realized_pnl_before_expenses_usd": str(book["realized"]),
            "unrealized_pnl_usd": str(unrealized), "fees_usd": str(book["fees"]),
            "interest_usd": str(book["interest"]), "total_pnl_usd": str(nav - book["initial_capital"]),
            "expense_source": (
                "Recorded expenses include assumed simulation fees of USD 0.99 per filled order; "
                "broker order API has no fee/interest fields"
            ),
            "positions": holdings,
        }
        with self.db:
            self.event("valuation", result)
        return result

    def submit(self, client: SimTradingClient, plan: dict, *,
               submission_allowed: Callable[[], bool] | None = None,
               order_allowed: Callable[[dict], bool] | None = None) -> list[dict]:
        if not isinstance(client, SimTradingClient) or client.market != 100:
            raise ValueError("Strategy submission requires a US SimTradingClient")
        state = self.db.execute("SELECT account_id FROM strategy WHERE id=1").fetchone()
        if state is None or str(client.account_id) != state["account_id"]:
            raise ValueError("Simulation client account differs from ledger")
        if self.db.execute(
            "SELECT count(*) FROM intents WHERE status IS NULL OR status NOT IN (4,5,6)"
        ).fetchone()[0]:
            raise ValueError("Outstanding orders must be reconciled before another batch")
        order_type = plan.get("order_type", "LIMIT")
        if order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("Only LIMIT and MARKET strategy orders are supported")
        with self.db:
            self.event("plan", plan)
        responses = []
        client.dry_run = False
        for order in plan["orders"]:
            if submission_allowed is not None and not submission_allowed():
                raise ValueError("Opening window ended; reconcile any earlier orders before continuing")
            if order_allowed is not None and not order_allowed(order):
                raise ValueError("Opening window ended; reconcile any earlier orders before continuing")
            identity = uuid4().hex
            text = ORDER_PREFIX + identity
            with self.db:
                self.db.execute(
                    "INSERT INTO intents(id,created_at,symbol,side,qty,price,text) VALUES (?,?,?,?,?,?,?)",
                    (identity, now(), order["symbol"], order["side"],
                     str(order["qty"]), order["limit_price"], text),
                )
                self.event("submitting", {
                    "intent_id": identity, "order": order, "text": text, "order_type": order_type,
                    "price_role": "limit" if order_type == "LIMIT" else "sizing_reference_only",
                })
            try:
                response = client.place_order(
                    order["symbol"], order["qty"], order["side"],
                    order_type=order_type,
                    price=order["limit_price"] if order_type == "LIMIT" else None, text=text,
                )
            except (FutuError, OSError, ValueError, TypeError) as exc:
                diagnostics = {}
                if isinstance(exc, FutuAPIError):
                    diagnostics = {
                        "broker_code": exc.code if isinstance(exc.code, (str, int)) else None,
                        "http_status": exc.status,
                    }
                with self.db:
                    self.event("submission_uncertain", {
                        "intent_id": identity, "error_type": type(exc).__name__,
                        **diagnostics,
                    })
                raise
            if not isinstance(response, dict) or type(response.get("order_id")) not in (str, int):
                raise ValueError(f"Missing broker order ID for intent {identity}; reconcile before retry")
            order_id = str(response["order_id"])
            if not order_id:
                raise ValueError(f"Empty broker order ID for intent {identity}; reconcile before retry")
            with self.db:
                self.db.execute("UPDATE intents SET order_id=? WHERE id=?", (order_id, identity))
                self.event("response", {"intent_id": identity, "response": response})
            responses.append({"symbol": order["symbol"], "side": order["side"], "response": response})
        with self.db:
            self.event("batch_submitted", {"orders": len(responses)})
        return responses

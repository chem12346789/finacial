"""Screen, size and submit simulation-only US stock orders."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import json
from pathlib import Path
import re
import subprocess
import sys
import sqlite3
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from broker.client import FutuAPIError, FutuError
from broker.credentials import get_store
from broker.market_data import get_default_quote_client
from ouput.paths import output_path
from ouput.orders import format_order_result
from strategy_ledger import StrategyLedger, number as amount
from trading_schedule import opening_window, previous_session
from us_stock_fees import SIM_FEE_SCHEDULE, estimate_sim_order_fees


ROOT = Path(__file__).resolve().parent
DEFAULT_JOURNAL_PATH = ROOT / "ouput" / "sim-orders.jsonl"
DEFAULT_LEDGER_PATH = ROOT / "ouput" / "play_money_ledger.sqlite3"
def default_lookback() -> int:
    source = (ROOT / "play_money_asset_risk_parity.py").read_text(encoding="utf-8")
    match = re.search(r'--lookback", type=int, default=(\d+)', source)
    if match is None:
        raise ValueError("Cannot read the risk parity --lookback default")
    return int(match.group(1))


def new_york_today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def at_market_open(today: date) -> bool:
    return opening_window(today, datetime.now(ZoneInfo("America/New_York")).time())


def prepare_weekend(ledger: StrategyLedger, args: argparse.Namespace, today: date,
                    *, recover: bool = False) -> dict:
    if today.weekday() < 5 and not recover:
        raise ValueError("Data preparation is restricted to the New York weekend")
    if recover and today.weekday() >= 5:
        raise ValueError("Use --prepare-only on weekends to prepare the upcoming week")
    monday = today - timedelta(days=today.weekday()) if recover else (
        today + timedelta(days=7 - today.weekday())
    )
    week = monday.isoformat()
    as_of = previous_session(monday if recover else today).isoformat()
    if args.as_of is not None and args.as_of != as_of:
        raise ValueError(f"Weekend data must use the latest completed session: {as_of}")
    saved = ledger.db.execute("SELECT * FROM weekly_inputs WHERE week=?", (week,)).fetchone()
    if recover and saved is not None:
        load_weekend_allocation(ledger, week, args)
        return {
            "status": "prepared", "week": week, "as_of": as_of, "ledger": str(ledger.path),
            "transactions_file": str(ledger.transactions_path),
            "message": "Weekend inputs already exist; no data replaced or orders submitted.",
        }
    if saved is None or saved["prepared_at"] != today.isoformat() or saved["lookback"] != args.lookback:
        allocation = screen_and_allocate(as_of, args.lookback)
        if allocation["as_of"] != as_of or allocation["end_date"] != as_of:
            raise ValueError("Screening data does not end on the latest completed US session")
        if recover:
            allocation["recovered_for_week"] = week
        ledger.save_inputs(week, today.isoformat(), args.lookback, allocation)
    else:
        allocation = json.loads(saved["allocation"])
    initialized = ledger.db.execute("SELECT 1 FROM strategy WHERE id=1").fetchone() is not None
    held = set(ledger.book()["positions"]) if initialized else set()
    selected = {row["symbol"] for row in allocation["weights"]}
    missing_risk = (held | selected) - set(allocation.get("holding_stop_risk", {}))
    if missing_risk:
        from play_money_asset_risk_parity import calculate_holding_stop_risk

        allocation.setdefault("holding_stop_risk", {}).update(
            calculate_holding_stop_risk(sorted(missing_risk), date.fromisoformat(as_of), args.lookback),
        )
        ledger.save_inputs(week, today.isoformat(), args.lookback, allocation)
    return {
        "status": "prepared", "week": week, "as_of": as_of, "ledger": str(ledger.path),
        "transactions_file": str(ledger.transactions_path),
        "message": "Weekend inputs saved; no orders submitted.",
    }


def load_weekend_allocation(ledger: StrategyLedger, week: str, args: argparse.Namespace) -> dict:
    row = ledger.db.execute("SELECT * FROM weekly_inputs WHERE week=?", (week,)).fetchone()
    if row is None:
        raise ValueError(f"No weekend inputs for week {week}; run --recover-weekend-inputs "
                         "to rebuild from the preceding weekend's completed session")
    prepared = date.fromisoformat(row["prepared_at"])
    monday = date.fromisoformat(week)
    allocation = json.loads(row["allocation"])
    recovered = allocation.get("recovered_for_week") == week
    if recovered:
        expected = previous_session(monday).isoformat()
        if not monday <= prepared < monday + timedelta(days=5) or (
            allocation["as_of"] != expected or allocation["end_date"] != expected
        ):
            raise ValueError("Recovered inputs must use the preceding week's completed session")
    elif prepared not in (monday - timedelta(days=2), monday - timedelta(days=1)):
        raise ValueError("Weekly inputs were not prepared during the preceding weekend")
    if row["lookback"] != args.lookback or (args.as_of is not None and allocation["as_of"] != args.as_of):
        raise ValueError("Screening settings differ from saved weekend inputs; no new orders allowed")
    return allocation


def screen_and_allocate(as_of: str | None, lookback: int) -> dict:
    if as_of is None:
        from risk_parity.optimization import PipelineConfig

        as_of = PipelineConfig().end_date
    date.fromisoformat(as_of)
    workspace = output_path(".cache/finacial")
    workspace.mkdir(parents=True, exist_ok=True)
    # Isolated inputs prevent concurrent runs or failed screens reusing old results.
    with TemporaryDirectory(prefix="play-money-orders-", dir=workspace) as directory:
        momentum = Path(directory) / "momentum.json"
        subprocess.run(
            [
                sys.executable, str(ROOT / "momentum.py"), "--tradingview",
                "--as-of", as_of, "--top", "5", "--method", "ensemble",
                "--output", str(momentum),
            ],
            check=True, stdout=subprocess.DEVNULL,
        )
        output = subprocess.run(
            [
                sys.executable, str(ROOT / "play_money_asset_risk_parity.py"),
                str(momentum),
                "--lookback", str(lookback), "--format", "json",
            ],
            check=True, stdout=subprocess.PIPE, text=True,
        )
        return json.loads(output.stdout)


def build_plan(
    allocation: dict, snapshots: dict, capital: Decimal, target_volatility: Decimal,
    max_leverage: Decimal = Decimal("1.33"),
) -> dict:
    from play_money_asset_risk_parity import validate_momentum_date

    validate_momentum_date(allocation)
    validate_momentum_date({"as_of": allocation["end_date"]})
    if not capital.is_finite() or capital <= 0:
        raise ValueError("capital must be finite and positive")
    if not target_volatility.is_finite() or not 0 < target_volatility <= 1:
        raise ValueError("target annualized volatility must be in (0, 1]")
    if not max_leverage.is_finite() or not 1 <= max_leverage <= Decimal("1.33"):
        raise ValueError("max leverage must be in [1, 1.33]")
    forecast = amount(allocation["annualized_volatility"], "forecast annualized volatility")
    if forecast <= 0:
        raise ValueError("forecast annualized volatility must be positive")
    theoretical_leverage = target_volatility / forecast
    leverage = min(theoretical_leverage, max_leverage)
    rows = allocation["weights"]
    if len(rows) != 5:
        raise ValueError("Expected exactly five risk-parity weights")
    symbols = [row["symbol"] for row in rows]
    if len(set(symbols)) != 5 or any(
        not isinstance(symbol, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*", symbol)
        or symbol.startswith(("US.", "HK.", "SH.", "SZ.", "CA."))
        for symbol in symbols
    ):
        raise ValueError("Expected five unique, unprefixed US stock symbols")
    weights = [amount(row["weight"], "weight") for row in rows]
    total_weight = sum(weights, Decimal(0))
    if abs(total_weight - 1) > Decimal("0.000001"):
        raise ValueError("Risk-parity weights must sum to one")
    quotes = {}
    for quote in snapshots["snapshot_list"]:
        code = quote["code"]
        if code in quotes:
            raise ValueError(f"Duplicate snapshot: {code}")
        quotes[code] = quote
    investment = (capital * leverage).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    orders = []
    for row, symbol, weight in zip(rows, symbols, weights):
        code = f"US.{symbol}"
        if code not in quotes:
            raise ValueError(f"Missing snapshot: {code}")
        market_price = amount(quotes[code]["last_price"], f"{code} last_price")
        price = market_price.quantize(
            Decimal("0.01"), rounding=ROUND_DOWN,
        )
        if price <= 0:
            raise ValueError(f"{code} limit price must be positive")
        # Normalizing only rounding noise keeps the total allocation within budget.
        budget = investment * weight / total_weight
        qty = int(budget / market_price)
        orders.append({
            "symbol": symbol, "code": code, "weight": str(weight / total_weight),
            "qty": qty, "limit_price": str(price), "notional_usd": str(price * qty),
            "fixed_stop_fraction": row.get("fixed_stop_fraction"),
            "status": "planned" if qty else "skipped_below_one_share",
        })
    total = sum((Decimal(order["notional_usd"]) for order in orders), Decimal(0))
    return {
        "as_of": allocation["as_of"], "currency": "USD",
        "capital_usd": str(capital),
        "target_annualized_volatility": str(target_volatility),
        "forecast_annualized_volatility": str(forecast),
        "theoretical_leverage": str(theoretical_leverage),
        "max_leverage": str(max_leverage), "target_leverage": str(leverage),
        "leverage_cap_hit": theoretical_leverage >= max_leverage,
        "target_annualized_volatility_before_rounding": str(forecast * leverage),
        "order_leverage_after_rounding": str(total / capital),
        "investment_cap_usd": str(investment), "order_notional_usd": str(total),
        "remaining_cash_before_fees_usd": str(max(capital - total, Decimal(0))),
        "borrowing_before_fees_usd": str(max(total - capital, Decimal(0))),
        "orders": orders,
    }


def quote_prices(snapshots: dict, symbols: set[str]) -> dict[str, Decimal]:
    prices = {}
    for row in snapshots["snapshot_list"]:
        symbol = row["code"].removeprefix("US.")
        if row["code"] != f"US.{symbol}" or symbol not in symbols or symbol in prices:
            raise ValueError("Unexpected or duplicate market snapshot")
        price = amount(row["last_price"], f"{symbol} price")
        if price <= 0:
            raise ValueError(f"{symbol} price must be positive")
        prices[symbol] = price
    if set(prices) != symbols:
        raise ValueError("Missing market snapshots for strategy holdings/targets")
    return prices


def executable_prices(snapshots: dict, orders: list[dict]) -> dict[str, Decimal]:
    symbols = {order["symbol"] for order in orders}
    quote_prices(snapshots, symbols)
    sides = {order["symbol"]: order["side"] for order in orders}
    prices = {}
    now_ms = Decimal(str(datetime.now().timestamp())) * 1000
    for row in snapshots["snapshot_list"]:
        symbol = row["code"].removeprefix("US.")
        age = now_ms - amount(row.get("update_time"), f"{symbol} quote timestamp")
        if age < 0 or age > 60_000:
            raise ValueError(f"{symbol}: quote is stale or future-dated; no order submitted")
        bid = amount(row.get("bid_price"), f"{symbol} bid")
        ask = amount(row.get("ask_price"), f"{symbol} ask")
        if bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError(f"{symbol}: invalid bid/ask; no order submitted")
        prices[symbol] = (ask if sides[symbol] == "BUY" else bid).quantize(
            Decimal("0.01"), rounding=ROUND_UP if sides[symbol] == "BUY" else ROUND_DOWN,
        )
    return prices


def price_orders(quote_client, orders: list[dict]) -> None:
    if not orders:
        return
    snapshots = quote_client.get_market_snapshot([f"US.{o['symbol']}" for o in orders])
    prices = executable_prices(snapshots, orders)
    for order in orders:
        order["limit_price"] = str(prices[order["symbol"]])
        order["notional_usd"] = str(prices[order["symbol"]] * order["qty"])


def submission_check(quote_client, order: dict) -> bool:
    if not at_market_open(new_york_today()):
        return False
    price = executable_prices(
        quote_client.get_market_snapshot([f"US.{order['symbol']}"]), [order],
    )[order["symbol"]]
    limit = Decimal(order["limit_price"])
    if (order["side"] == "BUY" and price > limit) or (
        order["side"] == "SELL" and price < limit
    ):
        raise ValueError(f"{order['symbol']}: bid/ask moved beyond checked limit; rerun planning")
    return True


def rebalance_plan(
    plan: dict, holdings: dict, prices: dict[str, Decimal], *, include_buys: bool = False,
) -> dict:
    targets = {row["symbol"]: row for row in plan["orders"]}
    sells, buys = [], []
    for symbol in sorted(set(targets) | set(holdings)):
        current = holdings.get(symbol, {}).get("qty", Decimal(0))
        target = targets[symbol]["qty"] if symbol in targets else 0
        difference = Decimal(target) - current
        if difference == 0:
            continue
        if difference != difference.to_integral_value():
            raise ValueError(f"{symbol}: fractional strategy holdings require manual reconciliation")
        price = prices[symbol].quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if price <= 0:
            raise ValueError(f"{symbol}: invalid order limit")
        qty = int(abs(difference))
        order = {
            "symbol": symbol, "side": "SELL" if difference < 0 else "BUY",
            "qty": qty, "limit_price": str(price), "notional_usd": str(price * qty),
        }
        (sells if difference < 0 else buys).append(order)
    return {
        **plan, "target_positions": plan["orders"],
        "phase": "SELL" if sells else "BUY" if buys else "HOLD",
        "orders": sells + buys if include_buys else sells if sells else buys,
    }


def effective_stop_fraction(limit: Decimal, estimate: dict | None) -> Decimal:
    """Cap the fixed stop distance with the risk estimate; the cap applies without one."""
    if estimate is not None:
        fraction = Decimal(str(estimate["stop_fraction"]))
        if 0 < fraction < 1:
            return min(limit, fraction)
    return limit


def stop_loss_cap(limit: Decimal | None, gross_leverage: str | None) -> Decimal:
    """Per-stock loss backstop; default 25% x current gross leverage.

    The risk estimate normally binds; this cap only overrides it for extreme-vol
    names (weekly vol above half the cap) or positions without a saved estimate.
    """
    if limit is not None:
        return limit
    if gross_leverage is None:
        return Decimal(1)
    # Floored at 1x: unlevered or under-invested books still get the 25% cap.
    return min(Decimal("0.25") * max(Decimal(gross_leverage), Decimal(1)), Decimal(1))


def stop_loss_plan(positions: dict, prices: dict[str, Decimal],
                   limit: Decimal, risk: dict | None = None) -> list[dict]:
    """Flag holdings at least `limit` below average cost; zero disables exits.

    A saved per-stock risk estimate tighter than `limit` takes precedence.
    """
    if limit == 0:
        return []
    risk = risk or {}
    sells = []
    for symbol in sorted(positions):
        qty = positions[symbol]["qty"]
        if qty <= 0:
            continue
        average_cost = positions[symbol]["cost"] / qty
        price = prices[symbol].quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if price <= 0:
            raise ValueError(f"{symbol}: invalid order limit")
        fraction = effective_stop_fraction(limit, risk.get(symbol))
        if average_cost > 0 and price <= average_cost * (1 - fraction):
            sells.append({
                "symbol": symbol, "side": "SELL", "qty": int(qty),
                "limit_price": str(price), "notional_usd": str(price * qty),
                "stop_fraction": str(fraction),
                "reason": "stop_loss",
            })
    return sells


def protective_stop_preview(
    targets: list[dict], holdings: dict, prices: dict[str, Decimal], limit: Decimal,
    risk: dict | None = None,
) -> list[dict]:
    stops = []
    for row in targets:
        if not row["qty"]:
            continue
        symbol = row["symbol"]
        holding = holdings.get(symbol)
        reference = holding["cost"] / holding["qty"] if holding else prices[symbol]
        estimate = (risk or {}).get(symbol)
        if estimate is None:
            # Selected-but-unheld targets carry fixed_stop_fraction from risk parity.
            estimate = next((w for w in targets if w["symbol"] == symbol), None)
            estimate_fraction = (
                Decimal(str(estimate["fixed_stop_fraction"]))
                if estimate is not None and estimate.get("fixed_stop_fraction") else None
            )
        else:
            estimate_fraction = Decimal(str(estimate["stop_fraction"]))
        if estimate_fraction is not None and not 0 < estimate_fraction < 1:
            estimate_fraction = None
        fraction = min(limit, estimate_fraction) if estimate_fraction is not None else limit
        stops.append({
            "symbol": symbol, "qty": row["qty"], "stop_fraction": str(fraction),
            "risk_stop_fraction": str(estimate_fraction) if estimate_fraction is not None else None,
            "trigger_price": str(reference * (1 - fraction)) if fraction else None,
        })
    return stops


def saved_holding_risk(ledger: StrategyLedger) -> dict:
    risk = {}
    for saved in ledger.db.execute("SELECT allocation FROM weekly_inputs ORDER BY week"):
        risk.update(json.loads(saved["allocation"]).get("holding_stop_risk", {}))
    return risk


def holdings_report(ledger: StrategyLedger, prices: dict[str, Decimal],
                    limit: Decimal | None) -> dict:
    report = ledger.report(prices)
    risk = saved_holding_risk(ledger)
    cap = stop_loss_cap(limit, report["gross_leverage"])
    report["stop_loss_cap"] = str(cap)
    for row in report["positions"]:
        cost = Decimal(row["average_cost"])
        estimate = risk.get(row["symbol"])
        fraction = effective_stop_fraction(cap, estimate)
        row["active_fixed_stop"] = str(cost * (1 - fraction)) if cap else None
        row["active_stop_fraction"] = str(fraction) if cap else None
        quote = prices[row["symbol"]].quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        row["stop_triggered"] = bool(cap and quote <= cost * (1 - fraction))
        row["risk_stop_estimate"] = estimate
        row["risk_stop_price"] = (
            str(cost * (1 - Decimal(str(estimate["stop_fraction"]))))
            if estimate is not None and Decimal(str(estimate["stop_fraction"])) < 1 else None
        )
    return report


def run_strategy(args: argparse.Namespace, capital: Decimal, target: Decimal,
                 max_leverage: Decimal) -> dict:
    with StrategyLedger(args.ledger) as ledger:
        today = new_york_today()
        if args.recover_weekend_inputs:
            return prepare_weekend(ledger, args, today, recover=True)
        if args.prepare_only:
            return prepare_weekend(ledger, args, today)
        if args.history_start is not None and ledger.db.execute(
            "SELECT count(*) FROM strategy"
        ).fetchone()[0]:
            raise ValueError("--history-start requires a fresh ledger; archive the old ledger first")
        if ledger.db.execute("SELECT count(*) FROM strategy").fetchone()[0] == 0:
            if DEFAULT_JOURNAL_PATH.exists():
                raise ValueError("Legacy journal exists: reconcile earlier orders before starting a new ledger")
            if args.expense_id is not None or args.void_intent is not None:
                raise ValueError("Expense booking/intent recovery requires an already initialized ledger")
        client = get_store("default").sim_client()
        if args.account_id is None:
            client.select_account(market=100)
        else:
            client.select_account(market=100, account_id=args.account_id)
        current_orders = client.get_orders()["orders"]
        # Fresh initialization must inspect older history for still-open orders.
        history_start = ledger.history_start()
        history = list(client.iter_history_orders(time_begin=history_start))
        broker_orders = [*history, *current_orders]
        funds = client.get_funds()
        positions = client.get_positions()
        ledger.bind(
            str(client.account_id), capital, funds, positions, broker_orders,
            history_start=args.history_start,
        )
        if args.void_intent is not None:
            ledger.void_uncertain(args.void_intent, args.void_note, broker_orders)
        if args.expense_id is not None:
            ledger.add_expense(
                args.expense_id, amount(args.fee_usd, "fee"), amount(args.interest_usd, "interest"),
                args.expense_note,
            )
        ready = ledger.reconcile(broker_orders, funds, positions, simulate_order_fees=True)
        if today.weekday() >= 5 and not args.reconcile_only:
            prepare_weekend(ledger, args, today)
        book = ledger.book()
        monday = today - timedelta(days=today.weekday())
        week = monday.isoformat()
        rebalance_offset = ("MON", "TUE", "WED", "THU", "FRI").index(args.rebalance_day)
        scheduled = monday + timedelta(days=rebalance_offset)
        cycle = ledger.active_cycle()
        if ready and cycle is not None:
            saved = json.loads(cycle["plan"])
            targets = {row["symbol"]: Decimal(row["qty"]) for row in saved["orders"] if row["qty"]}
            holdings = {s: row["qty"] for s, row in book["positions"].items()}
            if holdings == targets:
                ledger.complete_cycle(cycle["week"])
                cycle = None
        weekly_rebalance = cycle is not None or (
            today.weekday() < 5 and today >= scheduled and ledger.db.execute(
                "SELECT 1 FROM rebalance_cycles WHERE week=?", (week,),
            ).fetchone() is None
        )
        stop_losses: list[dict] = []
        if ready and not args.reconcile_only and book["positions"]:
            # Loss limits are risk controls: check them on every run, not only at
            # the weekly rebalance, using the ledger's average cost per holding.
            quote_client = get_default_quote_client()
            held = set(book["positions"])
            snapshots = quote_client.get_market_snapshot([f"US.{s}" for s in sorted(held)])
            stop_prices = quote_prices(snapshots, held)
            stop_report = holdings_report(ledger, stop_prices, args.stop_loss)
            stop_cap = Decimal(stop_report["stop_loss_cap"])
            stop_losses = stop_loss_plan(
                book["positions"], stop_prices, stop_cap, saved_holding_risk(ledger),
            )
        if ready and stop_losses and not weekly_rebalance and at_market_open(today):
            price_orders(quote_client, stop_losses)
            for order in stop_losses:
                order["order_type"] = args.order_type
                order["reference_price"] = str(stop_prices[order["symbol"]])
                order["estimated_fees"] = estimate_sim_order_fees(
                    Decimal(order["qty"]), Decimal(order["notional_usd"]), order["side"],
                )
            plan = {
                "mode": "sim", "account_id": str(client.account_id),
                "ledger": str(ledger.path), "order_type": args.order_type,
                "as_of": today.isoformat(),
                "cycle_week": None, "fee_schedule": SIM_FEE_SCHEDULE,
                "phase": "SELL", "orders": stop_losses, "target_positions": [],
                "pricing_note": (
                    "Market orders use quotes for sizing only; execution price is not guaranteed."
                    if args.order_type == "MARKET" else "Limit orders may remain unfilled."
                ),
                "estimated_fees_usd": str(sum(
                    (Decimal(o["estimated_fees"]["total"]) for o in stop_losses), Decimal(0),
                )),
                # Report the tightest applied fraction: saved risk estimates may
                # tighten individual stops below the account-level cap.
                "stop_loss_limit": str(max(
                    (Decimal(o["stop_fraction"]) for o in stop_losses), default=stop_cap,
                )),
            }
            for order in stop_losses:
                capacity = client.get_max_qty(
                    order["symbol"], order_type=args.order_type,
                    price=order["limit_price"] if args.order_type == "LIMIT" else None,
                )
                if order["qty"] > amount(capacity["max_sell_qty_round_lot"], "broker quantity capacity"):
                    raise ValueError(f"{order['symbol']}: stop-loss quantity exceeds max_sell_qty_round_lot")
            waiting_for_window = not at_market_open(new_york_today())
            if args.plan_only or waiting_for_window:
                responses = []
            else:
                responses = ledger.submit(
                    client, plan, order_allowed=lambda order: submission_check(quote_client, order),
                )
            return {
                "status": (
                    "stop_loss_planned" if args.plan_only else
                    "stop_loss_waiting" if waiting_for_window else "stop_loss_submitted"
                ),
                "ledger": str(ledger.path),
                "transactions_file": str(ledger.transactions_path),
                "account": stop_report,
                "plan": plan, "responses": responses,
                "message": (
                    f"Stop loss ({stop_cap:.0%} below average cost) triggered; "
                    + ("no orders submitted outside the NY 09:30-17:30 trading window. "
                       if waiting_for_window else "")
                    + "weekly rebalance skipped until sells reconcile."
                ),
            }
        week_exists = ledger.db.execute(
            "SELECT 1 FROM rebalance_cycles WHERE week=?", (week,),
        ).fetchone() is not None
        next_week = (monday + timedelta(days=7)).isoformat()
        next_week_inputs = ledger.db.execute(
            "SELECT lookback, allocation FROM weekly_inputs WHERE week=?", (next_week,),
        ).fetchone()
        due = today.weekday() < 5 and today >= scheduled and not week_exists
        if (cycle is None and next_week_inputs is not None) or not ready or args.reconcile_only or (not args.plan_only and not at_market_open(today)) or (cycle is None and not due):
            quote_client = get_default_quote_client()
            plan = json.loads(cycle["plan"]) if cycle is not None else None
            symbols = set(book["positions"])
            if plan is not None:
                symbols |= {row["symbol"] for row in plan["orders"]}
            elif ready and next_week_inputs is not None and next_week_inputs["lookback"] == args.lookback:
                symbols |= {row["symbol"] for row in json.loads(next_week_inputs["allocation"])["weights"]}
            snapshots = quote_client.get_market_snapshot([f"US.{s}" for s in sorted(symbols)]) if symbols else {
                "snapshot_list": [],
            }
            prices = quote_prices(snapshots, symbols)
            result = {
                "status": "waiting_for_open_orders" if not ready else "reconciled",
                "ledger": str(ledger.path), "scheduled_date": scheduled.isoformat(),
                "transactions_file": str(ledger.transactions_path),
                "account": holdings_report(ledger, prices, args.stop_loss),
                "message": (
                    "Open/partially filled orders remain; no new orders submitted" if not ready else
                    "Reconciled; new orders require saved weekend data and the NY 09:30-17:30 trading window."
                ),
            }
            preview_cap = Decimal(result["account"]["stop_loss_cap"])
            if plan is not None:
                result["planned_actions"] = rebalance_plan(
                    plan, book["positions"], prices, include_buys=True,
                )["orders"]
                result["preview_order_type"] = args.order_type
                result["protective_stops"] = protective_stop_preview(
                    plan["orders"], book["positions"], prices, preview_cap,
                    saved_holding_risk(ledger),
                )
            elif ready and next_week_inputs is not None and next_week_inputs["lookback"] == args.lookback:
                allocation = json.loads(next_week_inputs["allocation"])
                preview = build_plan(
                    allocation,
                    {"snapshot_list": [
                        {"code": f"US.{row['symbol']}", "last_price": str(prices[row["symbol"]])}
                        for row in allocation["weights"]
                    ]},
                    Decimal(result["account"]["nav_usd"]), target, max_leverage,
                )
                # Preview the stops that would protect the upcoming targets: cap the
                # default with the planned basket's leverage, not today's holdings.
                upcoming_cap = stop_loss_cap(args.stop_loss, preview["order_leverage_after_rounding"])
                result["upcoming_week"] = next_week
                result["upcoming_targets"] = preview["orders"]
                result["upcoming_target_leverage"] = preview["target_leverage"]
                result["preview_order_type"] = args.order_type
                result["protective_stops"] = protective_stop_preview(
                    preview["orders"], book["positions"], prices, upcoming_cap,
                    allocation.get("holding_stop_risk"),
                )
                result["upcoming_plan"] = rebalance_plan(
                    preview, book["positions"], prices, include_buys=True,
                )["orders"] or [
                    {
                        "symbol": row["symbol"], "side": "HOLD", "qty": row["qty"],
                        "limit_price": str(prices[row["symbol"]]),
                        "notional_usd": str(prices[row["symbol"]] * row["qty"]),
                    }
                    for row in preview["orders"]
                ]
                result["scheduled_date"] = (
                    date.fromisoformat(next_week) + timedelta(days=rebalance_offset)
                ).isoformat()
            return result
        saved_plan = json.loads(cycle["plan"]) if cycle is not None else None
        allocation = None if saved_plan is not None else load_weekend_allocation(ledger, week, args)
        quote_client = get_default_quote_client()
        target_rows = saved_plan["orders"] if saved_plan is not None else allocation["weights"]
        symbols = {row["symbol"] for row in target_rows} | set(book["positions"])
        snapshots = quote_client.get_market_snapshot([f"US.{s}" for s in sorted(symbols)])
        prices = quote_prices(snapshots, symbols)
        # Screening can refresh credentials or take long enough for account state to change.
        account_id = str(client.account_id)
        client = get_store("default").sim_client()
        client.select_account(market=100, account_id=account_id)
        latest_orders = [
            *client.iter_history_orders(time_begin=ledger.history_start()),
            *client.get_orders()["orders"],
        ]
        funds = client.get_funds()
        if not ledger.reconcile(latest_orders, funds, client.get_positions(), simulate_order_fees=True):
            raise ValueError("Orders became pending during screening; rerun reconciliation")
        report = holdings_report(ledger, prices, args.stop_loss)
        nav = Decimal(report["nav_usd"])
        if nav <= 0:
            raise ValueError("Strategy NAV is non-positive; no new orders allowed")
        if saved_plan is None:
            target_plan = build_plan(allocation, snapshots, nav, target, max_leverage)
            target_plan["order_type"] = args.order_type
            ledger.save_cycle(week, scheduled.isoformat(), target_plan)
            cycle_week = week
        else:
            target_plan = saved_plan
            cycle_week = cycle["week"]
        order_type = args.order_type
        plan = rebalance_plan(target_plan, book["positions"], prices)
        plan.update({
            "mode": "sim", "account_id": str(client.account_id), "ledger": str(ledger.path),
            "order_type": order_type, "cycle_week": cycle_week,
            "fee_schedule": SIM_FEE_SCHEDULE,
            "pricing_note": (
                "Market orders use quotes for sizing only; execution price and leverage are not guaranteed."
                if order_type == "MARKET" else "Limit orders may remain unfilled."
            ),
        })
        price_orders(quote_client, plan["orders"])
        for order in plan["orders"]:
            order["order_type"] = order_type
            order["reference_price"] = str(prices[order["symbol"]])
            order["estimated_fees"] = estimate_sim_order_fees(
                Decimal(order["qty"]), Decimal(order["notional_usd"]), order["side"],
            )
        estimated_fees = sum(
            (Decimal(order["estimated_fees"]["total"]) for order in plan["orders"]), Decimal(0),
        )
        plan["estimated_fees_usd"] = str(estimated_fees)
        if plan["phase"] == "BUY" and plan["orders"]:
            target_value = sum(
                Decimal(row["qty"]) * prices[row["symbol"]] for row in target_plan["orders"]
            )
            target_value += sum(
                Decimal(row["qty"]) * max(
                    Decimal(row["limit_price"]) - prices[row["symbol"]], Decimal(0),
                ) for row in plan["orders"]
            )
            if target_value > nav * min(max_leverage, Decimal(target_plan["max_leverage"])):
                raise ValueError("Saved weekly target exceeds current leverage cap; no buy orders submitted")
            total = sum((Decimal(row["notional_usd"]) for row in plan["orders"]), Decimal(0))
            leveraged = total + estimated_fees > book["cash"]
            cash = amount(funds["balance"], "simulation balance", signed=True) - amount(
                funds["hold"], "frozen cash",
            )
            buying_power = amount(funds["max_power_long"], "buying power") if leveraged else cash
            if total + estimated_fees > buying_power:
                raise ValueError("Broker available buying power is below the buy batch total plus estimated fees")
        else:
            leveraged = False
        max_qty_field = (
            "max_sell_qty_round_lot" if plan["phase"] == "SELL" else
            "max_margin_buy_qty_round_lot" if leveraged else "max_cash_buy_qty_round_lot"
        )
        for order in plan["orders"]:
            capacity = client.get_max_qty(
                order["symbol"], order_type=order_type,
                price=order["limit_price"] if order_type == "LIMIT" else None,
            )
            if order["qty"] > amount(capacity[max_qty_field], "broker quantity capacity"):
                raise ValueError(f"{order['symbol']}: planned quantity exceeds {max_qty_field}")
        if args.plan_only:
            responses = []
        else:
            if not at_market_open(new_york_today()):
                raise ValueError("Opening window ended during planning; no new orders submitted")
            responses = ledger.submit(
                client, plan, order_allowed=lambda order: submission_check(quote_client, order),
            )
            if not plan["orders"]:
                ledger.complete_cycle(cycle_week)
        return {
            "status": "planned" if args.plan_only else "submitted" if responses else "at_target",
            "account_before_submission": report,
            "plan": plan, "responses": responses,
            "transactions_file": str(ledger.transactions_path),
            "message": (
                "Planned orders only; nothing submitted."
                if args.plan_only else
                "Submission is not execution; run again to reconcile. Sell orders must finish before new buys."
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("sim",), default="sim", help="simulation only")
    parser.add_argument("--order-type", choices=("MARKET", "LIMIT"), default="LIMIT",
                        help="new orders use LIMIT; legacy MARKET arguments are converted to LIMIT")
    parser.add_argument("--account-id", help="optional; otherwise select the unique US simulation account")
    parser.add_argument("--as-of", help="screening date YYYY-MM-DD")
    parser.add_argument("--capital", default="3000", help="strategy equity in USD")
    parser.add_argument("--target-volatility", default="0.50", help="annualized volatility target")
    parser.add_argument("--max-leverage", default="1.33", help="gross exposure/equity cap, at most 1.33")
    parser.add_argument("--lookback", type=int, default=default_lookback())
    parser.add_argument("--stop-loss",
                        help="liquidate a holding when its price falls this fraction below "
                             "average ledger cost (default: 25%% of current gross leverage, "
                             "a backstop so the risk estimate usually binds; 0 disables)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--prepare-only", action="store_true", help="weekend data only; no broker access")
    actions.add_argument("--recover-weekend-inputs", action="store_true",
                         help="rebuild missing current-week inputs from pre-week K-lines; no orders")
    actions.add_argument("--reconcile-only", action="store_true", help="reconcile only; never prepare or submit")
    actions.add_argument("--plan-only", action="store_true",
                         help="print the planned rebalance orders without submitting them")
    parser.add_argument("--rebalance-day", choices=("MON", "TUE", "WED", "THU", "FRI"),
                        default="MON", help="New York weekly rebalance weekday; missed day catches up this week")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH,
                        help="persistent strategy SQLite ledger; reuse the same file on every run")
    parser.add_argument("--history-start",
                        help="fresh ledger only: now or timezone-aware ISO timestamp; "
                             "ignore terminal orders last updated before this cutoff")
    parser.add_argument("--expense-id", help="unique ID of a verified strategy fee/interest charge")
    parser.add_argument("--fee-usd", default="0")
    parser.add_argument("--interest-usd", default="0")
    parser.add_argument("--expense-note", default="", help="source/reference of the verified expense")
    parser.add_argument("--void-intent", help="manually resolve an uncertain intent confirmed not submitted")
    parser.add_argument("--void-note", default="", help="broker verification reference; never assume timeout means no order")
    args = parser.parse_args(argv)
    if args.order_type == "MARKET":
        print("MARKET simulation orders are disabled; using LIMIT for new orders.",
              file=sys.stderr)
        args.order_type = "LIMIT"
    try:
        if args.history_start is not None:
            cutoff = (
                datetime.now(ZoneInfo("America/New_York")) if args.history_start == "now"
                else datetime.fromisoformat(args.history_start)
            )
            if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                raise ValueError("--history-start requires an explicit timezone")
            if cutoff.timestamp() <= 0 or cutoff.timestamp() > datetime.now().timestamp():
                raise ValueError("--history-start must be a past or current timestamp")
            args.history_start = int(cutoff.timestamp() * 1_000_000)
            if not args.reconcile_only:
                raise ValueError("--history-start requires --reconcile-only for initialization")
        if not args.ledger.is_absolute():
            args.ledger = ROOT / args.ledger
        capital = amount(args.capital, "capital")
        target = amount(args.target_volatility, "target annualized volatility")
        max_leverage = amount(args.max_leverage, "max leverage")
        args.stop_loss = amount(args.stop_loss, "stop loss") if args.stop_loss is not None else None
        if args.stop_loss is not None and not 0 <= args.stop_loss < 1:
            raise ValueError("stop loss must be in [0, 1); 0 disables")
        if capital <= 0 or not 0 < target <= 1 or args.lookback < 2:
            raise ValueError("Require capital > 0, 0 < target volatility <= 1 and lookback >= 2")
        if not 1 <= max_leverage <= Decimal("1.33"):
            raise ValueError("max leverage must be in [1, 1.33]")
        fee, interest = amount(args.fee_usd, "fee"), amount(args.interest_usd, "interest")
        if args.expense_id is None and (fee or interest or args.expense_note):
            raise ValueError("Expense booking requires --expense-id")
        if args.expense_id is not None and (fee + interest <= 0 or not args.expense_note.strip()):
            raise ValueError("Expense booking requires a positive charge and --expense-note")
        if bool(args.void_intent) != bool(args.void_note.strip()):
            raise ValueError("Manual void requires both --void-intent and --void-note")
        if (args.prepare_only or args.recover_weekend_inputs) and (
            args.expense_id is not None or args.void_intent is not None
        ):
            raise ValueError("Data-only preparation cannot book expenses or recover intents")
        result = run_strategy(args, capital, target, max_leverage)
        print(format_order_result(result), flush=True)
        return 0
    except (FutuError, OSError, sqlite3.Error, ValueError, KeyError, TypeError,
            subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if isinstance(exc, FutuAPIError):
            code = exc.code if isinstance(exc.code, (str, int)) else None
            print(f"Broker diagnostics: code={code!r}, HTTP status={exc.status}", file=sys.stderr)
            print(f"Broker request diagnostics: {json.dumps(exc.diagnostics())}", file=sys.stderr)
        print(
            "Stopped without retry or automatic confirmation. Earlier orders may exist; "
            "reuse the same ledger and reconcile with the broker before running again.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

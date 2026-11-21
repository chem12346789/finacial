"""Human-readable simulation strategy reports, without broker access."""

from datetime import date
from decimal import Decimal


def format_order_result(result: dict) -> str:
    lines = ["\nSimulation strategy"]

    def field(label: str, value: str) -> None:
        lines.append(f"{label:<24}: {value}")

    def money(value: str) -> str:
        return f"{Decimal(value):,.2f} USD"

    def ratio(value: str | None) -> str:
        return f"{Decimal(value):.4f}x" if value is not None else "N/A"

    if result["status"] == "prepared":
        field("Status", "Weekend data prepared; no broker orders")
        field("Rebalance week (NY)", result["week"])
        field("Data through", result["as_of"])
        field("Ledger", result["ledger"])
        field("Transaction history", result["transactions_file"])
        return "\n".join(lines)
    if "transactions_file" in result:
        field("Transaction history", result["transactions_file"])
    statuses = {
        "submitted": "Orders submitted (not confirmed filled)",
        "planned": "Planned orders only; nothing submitted",
        "stop_loss_planned": "Stop-loss orders planned; nothing submitted",
        "stop_loss_submitted": "Stop-loss orders submitted (not confirmed filled)",
        "stop_loss_waiting": "Stop loss triggered; waiting for trading window; no orders submitted",
        "at_target": "At target; no new orders",
        "waiting_for_open_orders": "Waiting for open orders; no new orders",
        "reconciled": "Daily reconciliation complete; no orders scheduled",
    }
    field("Status", statuses[result["status"]])
    account = result["account"] if "account" in result else result["account_before_submission"]
    lines.append("\nAccount before new orders")
    for label, key in (
        ("Initial capital", "initial_capital_usd"), ("Strategy NAV", "nav_usd"),
        ("Cash", "cash_usd"), ("Borrowing", "borrowing_usd"),
        ("Market value", "market_value_usd"), ("Realized P&L before fees", "realized_pnl_before_expenses_usd"),
        ("Unrealized P&L", "unrealized_pnl_usd"), ("Recorded fees", "fees_usd"),
        ("Recorded interest", "interest_usd"), ("Total P&L", "total_pnl_usd"),
    ):
        field(label, money(account[key]))
    field("Current gross leverage", ratio(account["gross_leverage"]))
    lines.append("Fees/interest are explicitly recorded amounts, not automatically verified totals.")
    lines.append("\nCurrent strategy holdings")
    if not account["positions"]:
        lines.append("None")
    else:
        positions = sorted(account["positions"], key=lambda row: row["symbol"])
        with_stops = any("active_fixed_stop" in row for row in positions)
        if with_stops:
            header = (
                f'{"Symbol":<8} {"Qty":>5} {"Average cost":>16} {"Value":>16} '
                f'{"Unrealized P&L":>16} {"Loss":>8} {"Stop %":>8} {"Active stop":>16}'
            )
        else:
            header = (
                f'{"Symbol":<8} {"Qty":>5} {"Average cost":>16} {"Value":>16} '
                f'{"Unrealized P&L":>16} {"Loss":>8}'
            )
        lines.extend([header, "-" * len(header)])
        for row in positions:
            loss = (
                Decimal(row["unrealized_pnl_usd"]) / Decimal(row["cost_usd"])
                if Decimal(row["cost_usd"]) > 0 else Decimal(0)
            )
            base = (
                f'{row["symbol"]:<8} {row["qty"]:>5} {money(row["average_cost"]):>16} '
                f'{money(row["market_value_usd"]):>16} {money(row["unrealized_pnl_usd"]):>16}'
                f' {loss:>7.2%}'
            )
            if not with_stops:
                lines.append(base)
                continue
            active = (
                money(row["active_fixed_stop"]) + (" (triggered)" if row["stop_triggered"] else "")
                if row["active_fixed_stop"] is not None else "disabled"
            )
            active_fraction = (
                f'{Decimal(row["active_stop_fraction"]):>7.2%}'
                if row.get("active_stop_fraction") is not None else f'{"-":>8}'
            )
            lines.append(f'{base} {active_fraction} {active:>16}')
    if any("active_fixed_stop" in row for row in account["positions"]):
        lines.append(
            "The active stop uses the tighter of --stop-loss and the risk estimate "
            "(2 x stock forecast weekly volatility below average cost); "
            "a tighter --stop-loss is never loosened. "
            "Triggered exits wait for the trading window; weekly rebalance targets take precedence."
        )
    def stop_table(rows: list[dict], targets: list[dict] | None = None) -> list[str]:
        order_type = result.get("preview_order_type")
        price_label = "Limit price" if order_type == "LIMIT" else "Reference price"
        target_rows = {row["symbol"]: row for row in targets} if targets else {}
        with_risk = any(row.get("risk_stop_fraction") for row in rows)
        header = (
            f'{"Symbol":<8} {"Qty":>5} '
            + (f'{"Weight":>10} ' if target_rows else "")
            + (f'{"Type":<6} {price_label:>16} ' if target_rows else "")
            + f'{"Stop %":>8} '
            + (f'{"Risk est %":>10} ' if with_risk else "")
            + f'{"Trigger price":>16} {"Trailing":>8}'
        )
        divider = "-" * len(header)
        body = [
            f'{row["symbol"]:<8} {row["qty"]:>5} '
            + (
                f'{Decimal(target_rows[row["symbol"]]["weight"]):>9.4%} '
                f'{order_type or "":<6} {money(target_rows[row["symbol"]]["limit_price"]):>16} '
                if target_rows else ""
            )
            + (
                f'{Decimal(row["stop_fraction"]):>7.2%} '
                + (
                    (f'{Decimal(row["risk_stop_fraction"]):>9.2%} '
                     if row.get("risk_stop_fraction") else f'{"-":>10} ')
                    if with_risk else ""
                )
                + f'{money(row["trigger_price"]):>16} '
                if row["trigger_price"] is not None else
                f'{"disabled":>8} ' + (f'{"-":>10} ' if with_risk else "") + f'{"-":>16} '
            )
            + f'{"Off":>8}'
            for row in rows
        ]
        return [header, divider, *body]

    def table(rows: list[dict]) -> list[str]:
        order_type = result.get("preview_order_type")
        price_label = "Limit price" if order_type == "LIMIT" else "Reference price"
        stops = {row["symbol"]: row for row in result.get("protective_stops", [])}
        header = (
            f'{"Symbol":<8} {"Side":<5} {"Qty":>5} '
            + (f'{"Type":<6} ' if order_type else "")
            + f'{price_label:>16} {"Est. value":>14}'
            + (f' {"Loss limit":>16}' if stops else "")
        )
        divider = "-" * len(header)
        body = [
            f'{row["symbol"]:<8} {row["side"]:<5} {row["qty"]:>5} '
            + (f'{order_type:<6} ' if order_type else "")
            + f'{money(row["limit_price"]):>16} {money(row["notional_usd"]):>14}'
            + (
                f' {money(stops[row["symbol"]]["trigger_price"]):>16}'
                if row["symbol"] in stops and stops[row["symbol"]]["trigger_price"] is not None
                else (f' {"-":>16}' if stops else "")
            )
            for row in rows
        ]
        return [header, divider, *body]

    if "plan" not in result:
        if result.get("planned_actions"):
            lines.append("\nPlanned actions (pending rebalance)")
            lines.extend(table(result["planned_actions"]))
        if result.get("upcoming_plan"):
            lines.append(f'\nPlanned actions (week of {result["upcoming_week"]}, from saved weekend data)')
            lines.extend(table(result["upcoming_plan"]))
        if result.get("planned_actions") or result.get("upcoming_plan"):
            lines.append("Full rebalance preview only; sells must reconcile before buys are submitted.")
            if result.get("preview_order_type") == "LIMIT":
                lines.append("Limit prices use current quotes rounded down to cents; orders may remain unfilled.")
            elif result.get("preview_order_type") == "MARKET":
                lines.append("Market reference prices are estimates, not limit prices or guaranteed fills.")
        if result.get("protective_stops"):
            lines.append("\nSelected weekend targets & protective stops (preview only; not broker orders)")
            lines.extend(stop_table(result["protective_stops"], result.get("upcoming_targets")))
            if result.get("upcoming_target_leverage"):
                lines.append(f'Aimed leverage: {Decimal(result["upcoming_target_leverage"]):.4f}x')
            lines.append(
                "Simulation submits neither broker-held fixed nor trailing stops. "
                "Fixed exits are checked on script runs; real trailing support requires explicit integration. "
                "These are protective exits, not LIMIT entry orders; trigger prices are not guaranteed fills. "
                "Fixed stops anchor to the current average cost, or to the estimated entry price for new "
                "targets and are recalculated after fill. Risk est % is 2 x stock forecast weekly volatility "
                "below the anchor, computed at the weekend refresh for both held and newly selected stocks."
            )
        elif result.get("upcoming_targets"):
            lines.append("\nSelected weekend targets (not additional orders)")
            lines.extend(stop_table([], result["upcoming_targets"]))
            if result.get("upcoming_target_leverage"):
                lines.append(f'Aimed leverage: {Decimal(result["upcoming_target_leverage"]):.4f}x')
        if "scheduled_date" in result:
            field("Scheduled date (NY)", result["scheduled_date"])
        field("Ledger", result["ledger"])
        lines.extend(["", result["message"]])
        if result.get("upcoming_plan"):
            weekday = date.fromisoformat(result["scheduled_date"]).strftime("%A")
            lines.append(f'\nOrders to submit on {weekday} ({result["scheduled_date"]})')
            order_type = result.get("preview_order_type") or "LIMIT"
            for row in result["upcoming_plan"]:
                if row["side"] not in {"BUY", "SELL"}:
                    continue
                if order_type == "LIMIT":
                    lines.append(
                        f'client.place_order("US.{row["symbol"]}", {row["qty"]}, '
                        f'"{row["side"]}", order_type="LIMIT", price="{row["limit_price"]}")'
                    )
                else:
                    lines.append(
                        f'client.place_order("US.{row["symbol"]}", {row["qty"]}, '
                        f'"{row["side"]}", order_type="{order_type}")'
                    )
            for stop in result.get("protective_stops", []):
                if stop["trigger_price"] is None:
                    continue
                lines.append(
                    f'client.place_order("US.{stop["symbol"]}", {stop["qty"]}, '
                    f'"SELL", order_type="STOP", aux_price="{stop["trigger_price"]}")'
                )
        return "\n".join(lines)
    plan = result["plan"]
    lines.append("\nRisk and sizing")
    field("As of", plan["as_of"])
    if "cycle_week" in plan:
        field("Rebalance week (NY)", plan["cycle_week"])
    field("Account", plan["account_id"])
    field("Order type", plan["order_type"])
    if "stop_loss_limit" in plan:
        field("Fixed stop below cost", f'{Decimal(plan["stop_loss_limit"]):.2%}')
    else:
        field("Forecast volatility", f'{Decimal(plan["forecast_annualized_volatility"]):.4%}')
        field("Target volatility", f'{Decimal(plan["target_annualized_volatility"]):.4%}')
        field("Theoretical leverage", ratio(plan["theoretical_leverage"]))
        field("Leverage cap", ratio(plan["max_leverage"]))
        field("Cap hit", "Yes" if plan["leverage_cap_hit"] else "No")
        field("Target leverage", ratio(plan["target_leverage"]))
        field(
            "Volatility before rounding",
            f'{Decimal(plan["target_annualized_volatility_before_rounding"]):.4%}',
        )
        field("Rounded target leverage", ratio(plan["order_leverage_after_rounding"]))
        field("Target stock value", money(plan["order_notional_usd"]))
        field("Target cash before fees", money(plan["remaining_cash_before_fees_usd"]))
        field("Target debt before fees", money(plan["borrowing_before_fees_usd"]))
    lines.append(plan["pricing_note"])
    lines.append("\nTarget weights")
    for row in sorted(plan["target_positions"], key=lambda row: row["symbol"]):
        lines.append(
            f'{row["symbol"]:<8}: {Decimal(row["weight"]):.4%} | '
            f'{row["qty"]} shares | estimated value {money(row["notional_usd"])}'
        )
    lines.append("\nRebalance orders")
    field("Phase", plan["phase"])
    if "estimated_fees_usd" in plan:
        field("Estimated batch fees", money(plan["estimated_fees_usd"]))
        field("Fee schedule", plan["fee_schedule"])
        lines.append("Estimates only; not actual booked expenses. Financing interest excluded.")
    if not plan["orders"]:
        lines.append("None")
    for row in plan["orders"]:
        label = "limit" if plan["order_type"] == "LIMIT" else "reference only"
        lines.append(
            f'{row["symbol"]:<8}: {row["side"]:<4} {row["qty"]} shares | '
            f'{label} {money(row["reference_price"])} | estimated value {money(row["notional_usd"])}'
            + (f' | estimated fees {money(row["estimated_fees"]["total"])}' if "estimated_fees" in row else "")
        )
    lines.append("\nSubmission results")
    if not result["responses"]:
        lines.append("No orders submitted (--plan-only)" if result["status"] in {"planned", "stop_loss_planned"}
                     else "No orders submitted")
    for row in result["responses"]:
        lines.append(
            f'{row["symbol"]:<8}: {row["side"]:<4} | order ID {row["response"]["order_id"]} | submitted'
        )
    field("Ledger", plan["ledger"])
    lines.extend(["", result["message"]])
    return "\n".join(lines)

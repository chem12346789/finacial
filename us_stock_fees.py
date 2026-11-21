"""Estimated Futu HK fixed-plan NMS stock fees; not actual broker expenses."""

from decimal import Decimal, ROUND_UP


FEE_SCHEDULE = "Futu HK fixed / NMS; SEC 2026-04-04; FINRA 2026"
FEE_SOURCE = "https://www.futuhk.com/en/support/topic2_283"
FINRA_SOURCE = "https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule"
SEC_SOURCE = "https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2"
SIM_FEE_SCHEDULE = "Simulation assumption: USD 0.99 once per order with any fill"
SIM_ORDER_FEE = Decimal("0.99")


def estimate_sim_order_fees(qty: Decimal, notional: Decimal, side: str) -> dict[str, str]:
    components = estimate_us_stock_fees(qty, notional, side)
    return {
        **{name: "0.00" for name in components},
        "commission": str(SIM_ORDER_FEE) if qty else "0.00",
        "total": str(SIM_ORDER_FEE) if qty else "0.00",
    }


def estimate_us_stock_fees(qty: Decimal, notional: Decimal, side: str) -> dict[str, str]:
    if side not in {"BUY", "SELL"}:
        raise ValueError("Fee estimate requires BUY or SELL")
    if not qty.is_finite() or not notional.is_finite() or qty < 0 or notional < 0:
        raise ValueError("Fee estimate requires finite non-negative quantity and notional")
    if qty != qty.to_integral_value() or bool(qty) != bool(notional):
        raise ValueError("Fee estimate supports whole shares with positive notional only")
    zero = Decimal(0)
    components = dict.fromkeys(("commission", "platform", "settlement", "sec", "taf", "cat"), zero)
    if qty:
        cap = notional * Decimal("0.005")
        components.update({
            "commission": max(Decimal("0.99"), min(qty * Decimal("0.0049"), cap)),
            "platform": max(Decimal("1"), min(qty * Decimal("0.005"), cap)),
            "settlement": qty * Decimal("0.003"),
            "cat": qty * Decimal("0.000003"),
        })
        if side == "SELL":
            components["sec"] = max(Decimal("0.01"), notional * Decimal("0.0000206"))
            components["taf"] = min(Decimal("9.79"), max(Decimal("0.01"), qty * Decimal("0.000195")))
    rounded = {name: value.quantize(Decimal("0.01"), rounding=ROUND_UP) for name, value in components.items()}
    return {**{name: str(value) for name, value in rounded.items()}, "total": str(sum(rounded.values(), zero))}

"""Preview or submit one simulation BUY 8 HAS LIMIT order, without retries."""

import argparse
from decimal import Decimal, ROUND_UP
import json
import sys
from uuid import uuid4

from broker.client import FutuAPIError, FutuError
from broker.credentials import get_store
from broker.market_data import get_default_quote_client
from play_money_orders import quote_prices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", help="select a specific US simulation account")
    parser.add_argument("--price", help="explicit maximum buy price; overrides automatic quote pricing")
    parser.add_argument("--premium-percent", type=int, choices=(1, 2), default=1,
                        help="premium above broker last price (default: 1%%)")
    parser.add_argument("--submit", action="store_true",
                        help="actually submit once; bypasses the strategy ledger")
    parser.add_argument("--show-error-details", action="store_true",
                        help="print raw broker error code/message locally; may contain sensitive data")
    args = parser.parse_args(argv)
    submitting = False
    try:
        client = get_store("default").sim_client()
        client.select_account(market=100, **(
            {"account_id": args.account_id} if args.account_id is not None else {}
        ))
        client.dry_run = True
        price = args.price
        if price is None:
            snapshots = get_default_quote_client().get_market_snapshot(["US.HAS"])
            quote = quote_prices(snapshots, {"HAS"})["HAS"]
            price = str((quote * (1 + Decimal(args.premium_percent) / 100)).quantize(
                Decimal("0.01"), rounding=ROUND_UP,
            ))
            print(json.dumps({
                "broker_last_price": str(quote), "premium_percent": args.premium_percent,
                "limit_price": price,
            }, indent=2), flush=True)
        remark = "test:" + uuid4().hex
        order = {"order_type": "LIMIT", "price": price, "text": remark}
        preview = client.place_order("HAS", qty="8", side="BUY", **order)
        print(json.dumps({
            "environment": "SIMULATE", "method": preview["method"],
            "endpoint": "/api/v1.0/sim-trade/{account_id}/orders",
            "body": preview["body"],
        }, indent=2), flush=True)
        if not args.submit:
            print("Preview only; no order submitted. Use --submit to send once.", flush=True)
            return 0
        if args.show_error_details:
            print("Private debug output enabled: broker messages may contain sensitive data. "
                  "Redact before sharing.", file=sys.stderr, flush=True)
        print("Submitting outside the strategy ledger; keep the timer paused.", flush=True)
        capacity = client.get_max_qty("HAS", order_type="LIMIT", price=price)
        print(json.dumps({"capacity": capacity}, indent=2), flush=True)
        if Decimal(capacity["max_cash_buy_qty_round_lot"]) < 8:
            raise ValueError("Fewer than 8 shares available for a cash buy; no order submitted")
        client.dry_run = False
        submitting = True
        response = client.place_order("HAS", qty="8", side="BUY", **order)
        print(json.dumps({"response": response}, indent=2), flush=True)
        print("Submission is not a fill; check broker orders and fills.", flush=True)
        return 0
    except (FutuError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Error type: {type(exc).__name__}", file=sys.stderr)
        if isinstance(exc, FutuAPIError):
            print(json.dumps(exc.diagnostics(), indent=2), file=sys.stderr)
            if args.show_error_details:
                error = exc.details.get("error")
                if isinstance(error, dict):
                    print(json.dumps({
                        "broker_error": {
                            key: error[key] for key in ("code", "message") if key in error
                        },
                    }, indent=2), file=sys.stderr)
                else:
                    print("No nested broker error object returned.", file=sys.stderr)
        if submitting:
            print("Submission uncertain. No retry. Check broker orders/fills using "
                  f"remark {remark} before another attempt.", file=sys.stderr)
        else:
            print("Stopped before submission; check configuration and cash-buy capacity.",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

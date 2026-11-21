"""Real-account Futu API client."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import time
from typing import Callable, Iterator
from urllib.parse import quote

from .client import (
    FutuAPIError, JSONObject, JSONValue, Number, OAuthClient, TokenSet,
    _ClientTransport, TradingDisabledError, _choice, _identifier,
    _positive, _symbol, _text, _validate_history_range,
)


class FutuClient(_ClientTransport):
    """Real-account queries plus guarded order mutations.

    dry_run previews writes without sending them. Actual writes require both
    dry_run=False and allow_live_trading=True. Use one client per worker.
    """

    def __init__(
        self, token: TokenSet, *, account_id: str | int | None = None,
        oauth: OAuthClient | None = None, dry_run: bool = True,
        allow_live_trading: bool = False, timeout: float = 15,
        on_token_refresh: Callable[[TokenSet], None] | None = None,
    ) -> None:
        if type(dry_run) is not bool or type(allow_live_trading) is not bool:
            raise ValueError("Trading flags must be actual booleans")
        super().__init__(
            token, oauth=oauth, timeout=timeout, on_token_refresh=on_token_refresh,
        )
        self.account_id = account_id
        self.dry_run = dry_run
        self.allow_live_trading = allow_live_trading

    def _account(self, resource: str) -> str:
        if self.account_id is None:
            raise ValueError("Select an explicit account_id from get_accounts() first")
        return f"/api/v1.0/accounts/{_identifier(self.account_id)}/{resource}"

    def _write(
        self, method: str, path: str, *,
        body: JSONObject | None = None, query: JSONObject | None = None,
    ) -> JSONValue:
        if self.dry_run is True:
            return {
                "dry_run": True,
                "method": method,
                "path": path,
                "body": body,
                "query": query,
            }
        if self.dry_run is not False or self.allow_live_trading is not True:
            raise TradingDisabledError(
                "Live trading requires dry_run=False AND allow_live_trading=True"
            )
        return self._request(method, path, body=body, query=query)

    def get_accounts(self) -> JSONValue:
        return self._request("GET", "/api/v1.0/accounts/authorized_trd_accs")

    def select_account(self, *, account_id: str | int | None = None) -> JSONObject:
        """Select the unique authorized account without enabling live trading."""
        if account_id is not None:
            _identifier(account_id)
        data = self.get_accounts()
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise FutuAPIError("Invalid authorized accounts response")
        matches: list[JSONObject] = []
        for account in data["accounts"]:
            if not isinstance(account, dict):
                raise FutuAPIError("Invalid authorized account record")
            selected_id = account.get("account_id")
            if (
                isinstance(selected_id, bool)
                or not isinstance(selected_id, (str, int))
                or not str(selected_id).strip()
            ):
                raise FutuAPIError("Invalid authorized account_id")
            if account_id is None or str(selected_id) == str(account_id):
                matches.append(account)
        if not matches:
            raise ValueError("No authorized account matches the requested account_id")
        if len(matches) != 1:
            raise ValueError("Multiple authorized accounts match; specify account_id")
        selected = matches[0]
        selected_id = selected["account_id"]
        if not isinstance(selected_id, (str, int)):
            raise FutuAPIError("Invalid selected authorized account_id")
        self.account_id = selected_id
        return selected

    def get_funds(self, currency: str = "USD", *, asset_category: str | None = None) -> JSONValue:
        return self._request("GET", self._account("funds"), query={
            "currency": _text(currency, "currency"), "asset_category": asset_category,
        })

    def get_positions(
        self, code: str | None = None, *, asset_category: str | None = None,
    ) -> JSONValue:
        return self._request("GET", self._account("positions"), query={
            "code": _symbol(code) if code is not None else None,
            "asset_category": asset_category,
        })

    def iter_orders(
        self, trd_market: str, *, history: bool = False, page_size: int = 50,
        code: str | None = None, start: int | None = None, end: int | None = None,
    ) -> Iterator[JSONObject]:
        """Yield real-account orders; history time filters use Unix microseconds."""
        query = self._history_filters(code, start, end, history=history)
        yield from self._pages(
            "orders_history" if history else "orders", "orders",
            trd_market, page_size, 100, query,
        )

    def iter_fills(
        self, trd_market: str, *, history: bool = False, page_size: int = 50,
        code: str | None = None, start: int | None = None, end: int | None = None,
    ) -> Iterator[JSONObject]:
        """Yield real-account fills using page_flag/completed pagination."""
        query = self._history_filters(code, start, end, history=history)
        yield from self._pages(
            "fills_history" if history else "order_fills", "order_fills",
            trd_market, page_size, 50 if history else 100, query,
        )

    @staticmethod
    def _history_filters(
        code: str | None, start: int | None, end: int | None, *, history: bool,
    ) -> JSONObject:
        if not history and any(value is not None for value in (code, start, end)):
            raise ValueError("code/start/end filters require history=True")
        _validate_history_range(start, end)
        return {"code": _symbol(code) if code is not None else None, "start": start, "end": end}

    def _pages(
        self, resource: str, key: str, market: str,
        page_size: int, maximum: int, filters: JSONObject,
    ) -> Iterator[JSONObject]:
        if type(page_size) is not int or not 10 <= page_size <= maximum:
            raise ValueError(f"page_size must be between 10 and {maximum}")
        query: JSONObject = {
            **filters,
            "trd_market": _text(market, "trd_market"),
            "page_size": page_size,
            "page_flag": "",
        }
        seen_cursors = {""}
        while True:
            page = self._request("GET", self._account(resource), query=query)
            if (
                not isinstance(page, dict) or type(page.get("completed")) is not bool
            ):
                raise FutuAPIError("Invalid paginated response")
            rows = page.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise FutuAPIError("Invalid paginated records")
            yield from rows
            if page["completed"]:
                return
            next_cursor = page.get("page_flag")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise FutuAPIError("Missing or repeated pagination cursor")
            seen_cursors.add(next_cursor)
            query["page_flag"] = next_cursor

    def get_order_details(self, exchange: str, order_ids: list[str]) -> JSONValue:
        """Read-only POST; exchange is e.g. SEHK, not the HK market prefix."""
        if not 1 <= len(order_ids) <= 50:
            raise ValueError("Provide 1-50 order IDs")
        return self._request("POST", self._account("orders/detail"), body={
            "exchange": _text(exchange, "exchange"),
            "order_ids": [_text(order_id, "order_id") for order_id in order_ids],
        })

    def get_max_qty(
        self, code: str, *, order_type: str = "LIMIT", price: Number | None = None,
        order_id: str | None = None, sub_acc_id: str | None = None,
        position_id: str | None = None,
    ) -> JSONValue:
        return self._request("GET", self._account("acctradinginfo"), query={
            "code": _symbol(code), "order_type": order_type,
            "price": _positive(price, "price") if price is not None else None,
            "order_id": order_id, "sub_acc_id": sub_acc_id, "position_id": position_id,
        })

    def get_market_snapshot(self, codes: list[str]) -> JSONValue:
        if not 1 <= len(codes) <= 400:
            raise ValueError("Provide 1-400 symbols")
        return self._request("POST", "/api/v1.0/quote/snapshot", body={
            "code_list": [_symbol(code) for code in codes],
        })

    def get_history_kline(
        self, code: str, end: str, *, start: str | None = None,
        ktype: int = 2, autype: int = 1, num: int = 370, extended_time: int = 0,
    ) -> JSONValue:
        """Return one page, with inclusive ISO dates; no automatic K-line paging."""
        end_date = date.fromisoformat(end)
        if start is not None and date.fromisoformat(start) > end_date:
            raise ValueError("start must not be after end")
        if type(num) is not int or not 1 <= num <= 370:
            raise ValueError("num must be between 1 and 370")
        return self._request(
            "GET", f"/api/v1.0/quote/{quote(_symbol(code), safe='')}/history-kline",
            query={
                "end": end, "start": start, "ktype": ktype, "autype": autype,
                "num": num, "extended_time": extended_time,
            },
        )

    def place_order(
        self, code: str, qty: Number, side: str, *, order_type: str = "LIMIT",
        price: Number | None = None, time_in_force: str = "DAY",
        session: str = "RTH", aux_price: Number | None = None,
        expire_time: int | None = None, remark: str | None = None,
        lot_type: str | None = None, sub_acc_id: str | None = None,
        position_id: str | None = None,
        trail_type: str | None = None, trail_value: Number | None = None,
    ) -> JSONValue:
        """Preview or submit a real-account order; expire_time uses microseconds."""
        _choice(side, {"BUY", "SELL", "SELL_SHORT", "BUY_BACK"}, "side")
        _choice(order_type, {
            "LIMIT", "MARKET", "AUCTION", "AUCTION_LIMIT", "STOP",
            "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED", "TRAILING_STOP",
        }, "order_type")
        _choice(time_in_force, {"DAY", "GTC", "GTD"}, "time_in_force")
        _choice(session, {"RTH", "RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY"}, "session")
        requires_limit_price = order_type in {
            "LIMIT", "AUCTION_LIMIT", "STOP_LIMIT", "LIMIT_IF_TOUCHED",
        }
        requires_trigger_price = order_type in {
            "STOP", "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED",
        }
        if requires_limit_price and price is None:
            raise ValueError("This order_type requires price")
        if requires_trigger_price and aux_price is None:
            raise ValueError("This order_type requires aux_price")
        if order_type == "TRAILING_STOP":
            if trail_type is None:
                raise ValueError("TRAILING_STOP requires trail_type")
            _choice(trail_type, {"RATIO", "AMOUNT"}, "trail_type")
            if trail_value is None:
                raise ValueError("TRAILING_STOP requires trail_value")
            trail_value = _positive(trail_value, "trail_value")
            if trail_type == "RATIO" and Decimal(trail_value) >= 100:
                raise ValueError("RATIO trail_value must be below 100 percent")
            if price is not None or aux_price is not None:
                raise ValueError("TRAILING_STOP uses trail_value, not price or aux_price")
        elif trail_type is not None or trail_value is not None:
            raise ValueError("Trailing parameters require TRAILING_STOP")
        if order_type in {"MARKET", "TRAILING_STOP"} and session != "RTH":
            raise ValueError("MARKET and TRAILING_STOP orders only support RTH")
        if time_in_force == "GTD" and (
            type(expire_time) is not int or expire_time <= time.time() * 1_000_000
        ):
            raise ValueError("GTD requires a future expire_time in Unix microseconds")
        if expire_time is not None and time_in_force != "GTD":
            raise ValueError("expire_time is only valid with GTD")
        if remark is not None and len(remark.encode("utf-8")) > 64:
            raise ValueError("remark must be at most 64 UTF-8 bytes")
        body: JSONObject = {
            "code": _symbol(code),
            "qty": _positive(qty, "qty"),
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "session": session,
        }
        optional: JSONObject = {
            "price": _positive(price, "price") if price is not None else None,
            "aux_price": _positive(aux_price, "aux_price") if aux_price is not None else None,
            "expire_time": expire_time,
            "remark": remark,
            "lot_type": lot_type,
            "sub_acc_id": sub_acc_id,
            "position_id": position_id,
            "trail_type": trail_type,
            "trail_value": trail_value,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._write("POST", self._account("orders"), body=body)

    def place_trailing_stop(
        self, code: str, qty: Number, *, trail_percent: Number = "10",
        time_in_force: str = "GTC", remark: str | None = None,
    ) -> JSONValue:
        """Preview/submit a long-position sell stop; 10 means 10%, not 0.10."""
        return self.place_order(
            code, qty, "SELL", order_type="TRAILING_STOP",
            time_in_force=time_in_force, session="RTH",
            trail_type="RATIO", trail_value=trail_percent, remark=remark,
        )

    def modify_order(
        self, order_id: str, exchange: str, qty: Number, price: Number, *,
        aux_price: Number | None = None,
    ) -> JSONValue:
        body: JSONObject = {
            "exchange": _text(exchange, "exchange"),
            "qty": _positive(qty, "qty"), "price": _positive(price, "price"),
        }
        if aux_price is not None:
            body["aux_price"] = _positive(aux_price, "aux_price")
        return self._write("PUT", self._account(f"orders/{_identifier(order_id)}"), body=body)

    def cancel_order(self, order_id: str, exchange: str) -> JSONValue:
        return self._write(
            "DELETE", self._account(f"orders/{_identifier(order_id)}"),
            query={"exchange": _text(exchange, "exchange")},
        )

    def confirm_order(self, confirm_id: str) -> JSONValue:
        return self._write("POST", self._account("order_confirm"), body={
            "confirm_id": _text(confirm_id, "confirm_id"),
        })

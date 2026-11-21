"""Project-owned cloud simulation client, not the official Futu SDK.

All endpoints use /sim-trade/. Symbols omit market prefixes, and the service
uses numeric market/order enums rather than real-account trading parameters.
"""

from __future__ import annotations

import re
from typing import Callable, Iterator

from .client import (
    FutuAPIError, JSONObject, JSONValue, Number, OAuthClient, TokenSet,
    TradingDisabledError, _ClientTransport, _choice, _identifier,
    _positive, _validate_history_range,
)

PREFIX = "/api/v1.0/sim-trade/"
MARKETS = {1, 2, 3, 9, 10, 11, 12, 13, 16, 18, 100, 102}
SIDES = {"BUY": 1, "SELL": 2, "SELL_SHORT": 3, "BUY_BACK": 4}
ORDER_TYPES = {"LIMIT": 1, "MARKET": 3}


def _market(value: int) -> int:
    if type(value) is not int or value not in MARKETS:
        raise ValueError(f"market must be a supported market_id: {sorted(MARKETS)}")
    return value


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
        raise ValueError("symbol must be an unprefixed simulation symbol, e.g. VOO or 00700")
    if value.startswith(("US.", "HK.", "SH.", "SZ.", "CA.")):
        raise ValueError("Simulation symbols must not include the market prefix")
    return value


def _order_price(order_type: str, price: Number | None) -> str | None:
    _choice(order_type, set(ORDER_TYPES), "order_type")
    if order_type == "LIMIT" and price is None:
        raise ValueError("LIMIT orders require price")
    return _positive(price, "price") if price is not None else None


class SimTradingClient:
    """Simulation-only API; dry_run=False submits to the cloud simulator.

    Account and market must be selected from get_accounts(), never copied
    from real-account results. Queries reach the server even in dry-run mode.
    """

    def __init__(
        self, token: TokenSet, *, account_id: str | int | None = None,
        market: int | None = None, oauth: OAuthClient | None = None,
        dry_run: bool = True, timeout: float = 15,
        on_token_refresh: Callable[[TokenSet], None] | None = None,
    ) -> None:
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be an actual boolean")
        self._transport = _ClientTransport(
            token, oauth=oauth, timeout=timeout, on_token_refresh=on_token_refresh,
        )
        self.account_id = account_id
        self.market = _market(market) if market is not None else None
        self.dry_run = dry_run

    @property
    def token(self) -> TokenSet:
        return self._transport.token

    def _request(
        self, method: str, path: str, *,
        query: JSONObject | None = None, body: JSONObject | None = None,
    ) -> JSONValue:
        if not path.startswith(PREFIX):
            raise ValueError("SimTradingClient only permits sim-trade endpoints")
        try:
            return self._transport._request(method, path, query=query, body=body)
        except FutuAPIError as exc:
            exc.request_context = {
                "environment": "SIMULATE", "method": method,
                "endpoint": PREFIX + (
                    path[len(PREFIX):] if path == PREFIX + "accounts"
                    else "{account_id}/" + path.rsplit("/", 1)[-1]
                ),
                "parameters": {
                    key: value for key, value in (body or query or {}).items()
                    if key in {"market", "symbol", "order_type", "order_side", "qty", "price"}
                },
            }
            raise

    def _account(self, resource: str) -> str:
        if self.account_id is None:
            raise ValueError("Select a simulation account_id from get_accounts() first")
        return f"{PREFIX}{_identifier(self.account_id)}/{resource}"

    def _required_market(self) -> int:
        if self.market is None:
            raise ValueError("Set market from the selected simulation account's market_id")
        return _market(self.market)

    def _write(self, path: str, body: JSONObject) -> JSONValue:
        if not path.startswith(PREFIX):
            raise ValueError("SimTradingClient only permits sim-trade endpoints")
        if self.dry_run is True:
            return {
                "environment": "SIMULATE",
                "dry_run": True,
                "method": "POST",
                "path": path,
                "body": body,
            }
        if self.dry_run is not False:
            raise TradingDisabledError("dry_run must be False to submit simulation orders")
        return self._request("POST", path, body=body)

    def get_accounts(self) -> JSONValue:
        """The service may create simulation accounts on this first query."""
        return self._request("GET", PREFIX + "accounts")

    def select_account(
        self, *, market: int | None = None, account_id: str | int | None = None,
    ) -> JSONObject:
        """Fetch accounts and set both fields only for a unique matching account."""
        if market is not None:
            _market(market)
        if account_id is not None:
            _identifier(account_id)
        data = self.get_accounts()
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise FutuAPIError("Invalid simulation accounts response")
        matches: list[JSONObject] = []
        for index, account in enumerate(data["accounts"]):
            if not isinstance(account, dict):
                raise FutuAPIError("Invalid simulation account record")
            selected_id = account.get("account_id")
            selected_market = account.get("market_id")
            if isinstance(selected_market, str) and selected_market in {
                str(value) for value in MARKETS
            }:
                selected_market = int(selected_market)
            if (
                isinstance(selected_id, bool)
                or not isinstance(selected_id, (str, int))
                or not str(selected_id).strip()
            ):
                raise FutuAPIError(
                    f"Invalid simulation accounts[{index}].account_id "
                    f"(type: {type(selected_id).__name__})"
                )
            if type(selected_market) is not int:
                raise FutuAPIError(
                    f"Invalid simulation accounts[{index}].market_id "
                    f"(type: {type(selected_market).__name__}); "
                    f"expected one of {sorted(MARKETS)} (integer or numeric string)"
                )
            if market is not None and selected_market != market:
                continue
            if account_id is not None and str(selected_id) != str(account_id):
                continue
            if selected_market not in MARKETS:
                raise FutuAPIError(
                    f"Unsupported simulation accounts[{index}].market_id: {selected_market}; "
                    "specify a supported market or account_id to select another account"
                )
            matches.append({**account, "market_id": selected_market})
        if not matches:
            raise ValueError("No simulation account matches the requested market/account_id")
        if len(matches) != 1:
            raise ValueError("Multiple simulation accounts match; specify market or account_id")
        selected = matches[0]
        selected_id = selected["account_id"]
        selected_market = selected["market_id"]
        if not isinstance(selected_id, (str, int)) or not isinstance(selected_market, int):
            raise FutuAPIError("Invalid selected simulation account")
        self.account_id = selected_id
        self.market = selected_market
        return selected

    def get_funds(self) -> JSONValue:
        """Return simulation fields such as balance/total_asset, not real funds."""
        return self._request("GET", self._account("cash-info"))

    def get_positions(self) -> JSONValue:
        return self._request("GET", self._account("positions"), query={
            "market": _market(self.market) if self.market is not None else None,
        })

    def get_orders(self) -> JSONValue:
        """Query today's simulation orders for the selected account and market."""
        market = self._required_market()
        return self._request("GET", self._account("orders"), query={"market": market})

    def iter_history_orders(
        self, *, time_begin: int | None = None, time_end: int | None = None,
        page_size: int = 50,
    ) -> Iterator[JSONObject]:
        """Yield history for the selected market; times are Unix microseconds."""
        _validate_history_range(time_begin, time_end)
        if type(page_size) is not int or page_size <= 0:
            raise ValueError("page_size must be a positive integer")
        query: JSONObject = {
            "market": self._required_market(),
            "time_begin": time_begin,
            "time_end": time_end,
            "page_size": page_size,
            "next_key": "",
        }
        seen_cursors = {""}
        while True:
            page = self._request("GET", self._account("history-orders"), query=query)
            if not isinstance(page, dict):
                raise FutuAPIError(
                    "Invalid simulation history response: "
                    f"expected object, got {type(page).__name__}"
                )
            orders = page.get("orders")
            pagination = page.get("pagination")
            if not isinstance(orders, list):
                raise FutuAPIError(
                    "Invalid simulation history orders: expected list, "
                    f"got {type(orders).__name__}"
                )
            if any(not isinstance(order, dict) for order in orders):
                raise FutuAPIError(
                    "Invalid simulation history orders: expected every order to be an object"
                )
            # The service can omit pagination on a short final page.
            if "pagination" not in page:
                if len(orders) >= page_size:
                    raise FutuAPIError(
                        "Simulation history pagination missing on a full page; "
                        "cannot determine whether more orders exist"
                    )
                pagination = {"has_more": False}
            if not isinstance(pagination, dict):
                raise FutuAPIError(
                    "Invalid simulation history pagination: expected object, "
                    f"got {type(pagination).__name__}"
                )
            if type(pagination.get("has_more")) is not bool:
                raise FutuAPIError(
                    "Invalid simulation history pagination.has_more: expected bool, "
                    f"got {type(pagination.get('has_more')).__name__}"
                )
            for order in orders:
                if isinstance(order, dict):
                    yield order
            if not pagination["has_more"]:
                return
            next_cursor = pagination.get("next_key")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise FutuAPIError("Missing or repeated simulation pagination cursor")
            seen_cursors.add(next_cursor)
            query["next_key"] = next_cursor

    def get_max_qty(
        self, symbol: str, *, order_type: str = "LIMIT",
        price: Number | None = None, order_id: str | None = None,
    ) -> JSONValue:
        """Query only; symbol is VOO/00700 and market comes from this account."""
        market = self._required_market()
        path = self._account("max-buy-sell")
        symbol = _symbol(symbol)
        order_type = _choice(order_type, set(ORDER_TYPES), "order_type")
        # The running service requires market although the docs omit it.
        return self._request("GET", path, query={
            "market": market,
            "symbol": symbol,
            "order_type": ORDER_TYPES[order_type],
            "price": _order_price(order_type, price),
            "order_id": order_id,
        })

    def place_order(
        self, symbol: str, qty: Number, side: str, *,
        order_type: str = "LIMIT", price: Number | None = None, text: str | None = None,
    ) -> JSONValue:
        """Preview/submit a simulation order; numeric enums are encoded here."""
        if self.market is None:
            raise ValueError("Set market from the selected simulation account's market_id")
        price_value = _order_price(order_type, price)
        if text is not None and (
            not isinstance(text, str) or len(text.encode("utf-8")) > 100
        ):
            raise ValueError("text must be a string of at most 100 UTF-8 bytes")
        body: JSONObject = {
            "market": _market(self.market),
            "symbol": _symbol(symbol),
            "order_type": ORDER_TYPES[order_type],
            "order_side": SIDES[_choice(side, set(SIDES), "side")],
            "qty": _positive(qty, "qty"),
        }
        if price_value is not None:
            body["price"] = price_value
        if text is not None:
            body["text"] = text
        return self._write(self._account("orders"), body)

    def modify_order(
        self, order_id: str, *, qty: Number | None = None, price: Number | None = None,
    ) -> JSONValue:
        """Change quantity and/or price; server allows only submitted orders."""
        if qty is None and price is None:
            raise ValueError("Provide qty or price to modify")
        body: JSONObject = {"market": self._required_market()}
        if qty is not None:
            body["new_qty"] = _positive(qty, "qty")
        if price is not None:
            body["new_price"] = _positive(price, "price")
        return self._write(self._account(f"orders/{_identifier(order_id)}/modify"), body)

    def cancel_order(self, order_id: str) -> JSONValue:
        """Cancel a submitted simulation order, not a filled order."""
        return self._write(
            self._account(f"orders/{_identifier(order_id)}/cancel"),
            {"market": self._required_market()},
        )

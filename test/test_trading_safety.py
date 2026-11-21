"""Offline live-trading guards and simulation isolation; no broker requests."""

from decimal import Decimal
from http.client import IncompleteRead
from io import BytesIO
import json
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from broker import (
    FutuAPIError,
    FutuClient,
    FutuTransportError,
    OAuthClient,
    OrderConfirmationRequired,
    SimTradingClient,
    TokenSet,
    TradingDisabledError,
)
from broker.client import HOST, _NoRedirect
from broker.credentials import Credentials, SecretServiceStore


class LiveTradingSafetyTest(unittest.TestCase):
    def setUp(self):
        patcher = patch("broker.client._OPENER.open")
        self.open = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = FutuClient(TokenSet("access-secret"), account_id="123")

    def enable_live(self):
        self.client = FutuClient(
            TokenSet("access-secret"), account_id="123",
            dry_run=False, allow_live_trading=True,
        )

    def test_default_preview_never_calls_network_or_refresh(self):
        self.client.token = TokenSet("expired", expires_at=0)
        result = self.client.place_order("US.VOO", Decimal("1.25"), "BUY", price="123.4500")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["path"], "/api/v1.0/accounts/123/orders")
        self.assertEqual(result["body"]["qty"], "1.25")
        self.assertEqual(result["body"]["price"], "123.4500")
        self.assertNotIn("expired", json.dumps(result))
        self.open.assert_not_called()

    def test_all_mutations_require_both_live_flags(self):
        for operation in (
            lambda: self.client.place_order("US.VOO", 1, "BUY", price=100),
            lambda: self.client.place_trailing_stop("US.VOO", 1),
            lambda: self.client.modify_order("abc", "US", 2, 101),
            lambda: self.client.cancel_order("abc", "US"),
            lambda: self.client.confirm_order("warning-id"),
        ):
            for dry_run, allow_live in ((True, False), (True, True), (False, False)):
                with self.subTest(dry_run=dry_run, allow_live=allow_live):
                    self.client.dry_run = dry_run
                    self.client.allow_live_trading = allow_live
                    if dry_run:
                        self.assertTrue(operation()["dry_run"])
                    else:
                        with self.assertRaises(TradingDisabledError):
                            operation()
        self.open.assert_not_called()

    def test_string_boolean_cannot_enable_live_trading(self):
        with self.assertRaises(ValueError):
            FutuClient(TokenSet("secret"), dry_run="false")
        self.client.dry_run = False
        self.client.allow_live_trading = "false"
        with self.assertRaises(TradingDisabledError):
            self.client.cancel_order("abc", "US")
        self.open.assert_not_called()

    def test_trailing_stop_preview_uses_percentage_and_gtc(self):
        result = self.client.place_trailing_stop("US.MRNA", Decimal("10"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["body"], {
            "code": "US.MRNA", "qty": "10", "side": "SELL",
            "order_type": "TRAILING_STOP", "time_in_force": "GTC",
            "session": "RTH", "trail_type": "RATIO", "trail_value": "10",
        })
        self.open.assert_not_called()

    def test_trailing_stop_explicit_live_submission(self):
        self.enable_live()
        self.open.side_effect = [BytesIO(b'{"s":"ok","d":{"order_id":"trail"}}')]
        self.assertEqual(
            self.client.place_trailing_stop("US.MRNA", 10, trail_percent="12.5"),
            {"order_id": "trail"},
        )
        request = self.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(urlsplit(request.full_url).path, "/api/v1.0/accounts/123/orders")
        self.assertEqual(body["trail_value"], "12.5")
        self.assertEqual(body["order_type"], "TRAILING_STOP")
        self.assertEqual(self.open.call_count, 1)

    def test_invalid_trailing_parameters_never_reach_network(self):
        for overrides in (
            {"trail_type": None}, {"trail_type": "NONE"}, {"trail_value": None},
            {"trail_value": 0}, {"trail_value": -1}, {"trail_value": "NaN"},
            {"trail_value": True}, {"trail_value": "Infinity"}, {"trail_value": 100},
            {"trail_value": 101}, {"price": 90}, {"aux_price": 90},
            {"session": "ALL_DAY"}, {"order_type": "STOP", "aux_price": 90},
        ):
            args = {
                "order_type": "TRAILING_STOP", "trail_type": "RATIO",
                "trail_value": "10", **overrides,
            }
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.client.place_order("US.MRNA", 10, "SELL", **args)
        self.open.assert_not_called()

    def test_amount_trailing_stop_and_fixed_stop_remain_distinct(self):
        trailing = self.client.place_order(
            "US.MRNA", 10, "SELL", order_type="TRAILING_STOP",
            trail_type="AMOUNT", trail_value="150",
        )
        self.assertEqual(trailing["body"]["trail_value"], "150")
        fixed = self.client.place_order(
            "US.MRNA", 10, "SELL", order_type="STOP", aux_price="90", time_in_force="GTC",
        )
        self.assertEqual(fixed["body"]["aux_price"], "90")
        self.assertNotIn("trail_value", fixed["body"])
        self.open.assert_not_called()

    def test_explicit_live_flags_allow_one_submission(self):
        self.enable_live()
        self.open.side_effect = [BytesIO(b'{"s":"ok","d":{"order_id":"abc"}}')]
        self.assertEqual(
            self.client.place_order("US.VOO", 1, "BUY", price=100), {"order_id": "abc"},
        )
        self.open.assert_called_once()
        request = self.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlsplit(request.full_url).path, "/api/v1.0/accounts/123/orders")

    def test_confirmation_and_browser_action_are_not_automatically_accepted(self):
        self.enable_live()
        for fields in (
            {"need_order_confirm": True, "confirm_id": "warning"},
            {"jump_url": "https://example.invalid/action"},
        ):
            with self.subTest(fields=fields):
                payload = {"s": "error", "errcode": -2100, "errmsg": "Action needed", **fields}
                self.open.side_effect = [BytesIO(json.dumps(payload).encode())]
                before = self.open.call_count
                with self.assertRaises(OrderConfirmationRequired) as caught:
                    self.client.place_order("US.VOO", 1, "BUY", price=100)
                self.assertEqual(caught.exception.confirm_id, fields.get("confirm_id"))
                self.assertEqual(caught.exception.jump_url, fields.get("jump_url"))
                self.assertEqual(self.open.call_count, before + 1)

    def test_invalid_order_fields_never_reach_network(self):
        for overrides in (
            {"qty": 0}, {"qty": -1}, {"qty": "NaN"}, {"qty": float("inf")},
            {"qty": True}, {"price": "garbage"}, {"price": None},
            {"code": "VOO"}, {"side": "buy"}, {"order_type": "UNKNOWN"},
            {"order_type": "STOP", "aux_price": None},
            {"order_type": "MARKET", "session": "OVERNIGHT"},
            {"time_in_force": "GTD"}, {"expire_time": 123},
            {"remark": "\u4e2d" * 22},
        ):
            args = {"code": "US.VOO", "qty": 1, "side": "BUY", "price": "100", **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.client.place_order(**args)
        self.open.assert_not_called()

    def test_refresh_failure_does_not_send_original_order(self):
        self.enable_live()
        self.client.token = TokenSet("old", "refresh", time.time() - 1)
        self.client.oauth = OAuthClient("client-id", "http://localhost:60355/callback")
        self.open.side_effect = [
            BytesIO(b'{"error":"invalid_grant","error_description":"Refresh expired"}'),
        ]
        with self.assertRaises(FutuAPIError):
            self.client.place_order("US.VOO", 1, "BUY", price=100)
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(self.open.call_args.args[0].full_url, HOST + "/oauth2/token")

    def test_network_failure_never_retries_order(self):
        self.enable_live()
        for error in (URLError("connection lost"), TimeoutError("timeout"), IncompleteRead(b"partial")):
            with self.subTest(error=type(error).__name__):
                self.open.reset_mock()
                self.open.side_effect = error
                with self.assertRaises(FutuTransportError):
                    self.client.place_order("US.VOO", 1, "BUY", price=100)
                self.assertEqual(self.open.call_count, 1)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(_NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://other.invalid",
        ))


class SimulationIsolationTest(unittest.TestCase):
    def setUp(self):
        patcher = patch("broker.client._OPENER.open")
        self.open = patcher.start()
        self.addCleanup(patcher.stop)
        self.sim = SimTradingClient(TokenSet("test-secret"), account_id="sim123", market=100)

    def respond(self, data):
        self.open.side_effect = [
            BytesIO(json.dumps({"ret_code": 0, "data": data}).encode()),
        ]

    def test_business_error_diagnostics_capture_status_without_sensitive_values(self):
        self.sim.dry_run = False
        for envelope in (
            {"ret_code": -5, "ret_msg": "backend business error"},
            {"s": "error", "errcode": -5, "errmsg": "backend business error"},
        ):
            with self.subTest(envelope=envelope):
                payload = {
                    **envelope, "access_token": "response-secret",
                    "data": {"account_id": "private-account", "cookie": "private-cookie"},
                }
                response = BytesIO(json.dumps(payload).encode())
                response.status = 200
                self.open.side_effect = [response]
                with self.assertRaises(FutuAPIError) as caught:
                    self.sim.place_order("MRNA", 2, "SELL", order_type="MARKET", text="private-note")
                diagnostic = caught.exception.diagnostics()
                self.assertEqual(diagnostic["http_status"], 200)
                self.assertEqual(diagnostic["code"], -5)
                self.assertEqual(diagnostic["request"]["parameters"], {
                    "market": 100, "symbol": "MRNA", "order_type": 3, "order_side": 2, "qty": "2",
                })
                self.assertEqual(diagnostic["request"]["endpoint"],
                                 "/api/v1.0/sim-trade/{account_id}/orders")
                self.assertEqual(diagnostic["response_objects"]["data"]["account_id"], "str")
                for secret in ("test-secret", "response-secret", "private-account",
                               "private-cookie", "private-note", "sim123"):
                    self.assertNotIn(secret, json.dumps(diagnostic))
                self.assertEqual(caught.exception.details, payload)
                self.assertFalse(diagnostic["confirmation_required"])

    def test_http_error_preserves_status_and_does_not_retry(self):
        self.sim.dry_run = False
        self.open.side_effect = HTTPError(
            "https://example.invalid", 403, "Forbidden", {},
            BytesIO(b'{"ret_code":-5,"ret_msg":"backend business error"}'),
        )
        with self.assertRaises(FutuAPIError) as caught:
            self.sim.place_order("MRNA", 2, "SELL", order_type="MARKET")
        self.assertEqual(caught.exception.diagnostics()["http_status"], 403)
        self.open.assert_called_once()

    def test_default_preview_never_calls_server(self):
        result = self.sim.place_order("VOO", Decimal("1.25"), "BUY", price="600.0100")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["environment"], "SIMULATE")
        self.assertEqual(result["body"], {
            "market": 100, "symbol": "VOO", "order_type": 1, "order_side": 1,
            "qty": "1.25", "price": "600.0100",
        })
        self.assertTrue(self.sim.modify_order("abc", price=100)["dry_run"])
        self.assertTrue(self.sim.cancel_order("abc")["dry_run"])
        self.open.assert_not_called()

    def test_submissions_only_use_simulation_routes(self):
        self.sim.dry_run = False
        for order_type, numeric, price in (("LIMIT", 1, "100"), ("MARKET", 3, None)):
            for side, numeric_side in (("BUY", 1), ("SELL", 2), ("SELL_SHORT", 3), ("BUY_BACK", 4)):
                with self.subTest(order_type=order_type, side=side):
                    self.respond({"order_id": "abc"})
                    self.assertEqual(
                        self.sim.place_order("VOO", 1, side, order_type=order_type, price=price),
                        {"order_id": "abc"},
                    )
                    request = self.open.call_args.args[0]
                    self.assertEqual(
                        urlsplit(request.full_url).path, "/api/v1.0/sim-trade/sim123/orders",
                    )
                    self.assertEqual(request.method, "POST")
                    body = json.loads(request.data)
                    self.assertEqual(body["market"], 100)
                    self.assertEqual(body["order_type"], numeric)
                    self.assertEqual(body["order_side"], numeric_side)
                    self.assertEqual("price" in body, price is not None)

    def test_modify_and_cancel_only_use_simulation_routes(self):
        self.sim.dry_run = False
        for suffix, operation in (
            ("modify", lambda: self.sim.modify_order("abc", qty="2", price="99.0100")),
            ("cancel", lambda: self.sim.cancel_order("abc")),
        ):
            with self.subTest(suffix=suffix):
                self.respond({"order_id": "abc"})
                operation()
                request = self.open.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(
                    urlsplit(request.full_url).path,
                    f"/api/v1.0/sim-trade/sim123/orders/abc/{suffix}",
                )
                self.assertEqual(json.loads(request.data), {
                    "market": 100,
                    **({"new_qty": "2", "new_price": "99.0100"} if suffix == "modify" else {}),
                })

    def test_modify_and_cancel_preview_include_selected_market(self):
        self.assertEqual(self.sim.cancel_order("abc")["body"], {"market": 100})
        self.assertEqual(self.sim.modify_order("abc", qty=2)["body"],
                         {"market": 100, "new_qty": "2"})
        self.open.assert_not_called()

    def test_modify_and_cancel_require_selected_market(self):
        sim = SimTradingClient(TokenSet("secret"), account_id="sim123", dry_run=False)
        for operation in (lambda: sim.cancel_order("abc"), lambda: sim.modify_order("abc", qty=2)):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "market"):
                operation()
        self.open.assert_not_called()

    def test_account_and_market_require_explicit_selection(self):
        sim = SimTradingClient(TokenSet("secret"))
        with self.assertRaises(ValueError):
            sim.get_funds()
        sim.account_id = "sim123"
        with self.assertRaises(ValueError):
            sim.place_order("VOO", 1, "BUY", price=100)
        for market in (True, "2", 0, 999):
            with self.subTest(market=market), self.assertRaises(ValueError):
                SimTradingClient(TokenSet("secret"), market=market)
        self.open.assert_not_called()

    def test_string_boolean_cannot_enable_submission(self):
        with self.assertRaises(ValueError):
            SimTradingClient(TokenSet("secret"), dry_run="false")
        self.sim.dry_run = "false"
        with self.assertRaises(TradingDisabledError):
            self.sim.cancel_order("abc")
        self.open.assert_not_called()

    def test_real_account_paths_are_rejected(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                self.sim._request(method, "/api/v1.0/accounts/123/orders")
        with self.assertRaises(ValueError):
            self.sim._write("/api/v1.0/accounts/123/orders", {})
        self.assertFalse(hasattr(self.sim, "confirm_order"))
        self.open.assert_not_called()

    def test_keyring_does_not_reuse_saved_real_account_id(self):
        store = SecretServiceStore("default")
        credentials = Credentials(TokenSet("old", "refresh", 1), "client", account_id="REAL123")
        with (
            patch.object(store, "load", return_value=credentials),
            patch.object(store, "save") as save,
        ):
            sim = store.sim_client()
            self.assertIsNone(sim.account_id)
            self.assertIsNone(sim.market)
            self.assertTrue(sim.dry_run)
            with patch.object(
                sim._transport.oauth, "refresh", return_value=TokenSet("new", "refresh", 9999999999),
            ):
                self.respond({"accounts": []})
                sim.get_accounts()
            saved = save.call_args.args[0]
            self.assertEqual(saved.account_id, "REAL123")
            self.assertEqual(saved.tokens.access_token, "new")
            self.assertEqual(sim.token.access_token, "new")

    def test_server_failure_is_explicit_without_retry(self):
        self.sim.dry_run = False
        self.open.side_effect = [BytesIO(b'{"ret_code":-3,"ret_msg":"invalid_parameter"}')]
        with self.assertRaises(FutuAPIError):
            self.sim.place_order("VOO", 1, "BUY", price=100)
        self.assertEqual(self.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()

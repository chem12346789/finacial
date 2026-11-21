"""Shared HTTP and OAuth layer for the project-owned Futu clients.

The official Futu SDK is a separate package; these clients call its HTTP API.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
from http.client import HTTPException
import json
import math
import re
import secrets
import time
from typing import Callable, Mapping, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONObject: TypeAlias = dict[str, JSONValue]
Number: TypeAlias = str | int | float | Decimal
HOST = "https://webapi.futunn.com"
TOKEN_REFRESH_LEEWAY_SECONDS = 30


class FutuError(Exception):
    """Base error for transport, protocol and trading errors."""


class FutuTransportError(FutuError):
    """Request failed; a submitted order may already have reached the server."""


class FutuAPIError(FutuError):
    """Server/protocol failure; details may contain sensitive response fields."""

    def __init__(
        self, message: str, *, code: JSONValue = None,
        status: int | None = None, details: JSONObject | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details if details is not None else {}
        self.request_context: JSONObject = {}

    def diagnostics(self) -> JSONObject:
        """Return request context and response structure, never raw response values."""
        return {
            "code": self.code if isinstance(self.code, (str, int)) else None,
            "http_status": self.status,
            "request": self.request_context,
            "response_fields": sorted(self.details),
            "response_objects": {
                key: {name: type(value).__name__ for name, value in nested.items()}
                for key in ("error", "data", "d")
                if isinstance(nested := self.details.get(key), dict)
            },
            "confirmation_required": isinstance(self, OrderConfirmationRequired),
        }


class OrderConfirmationRequired(FutuAPIError):
    """Inspect confirm_id / jump_url; never automatically accept a warning."""

    @property
    def confirm_id(self) -> JSONValue:
        return self.details.get("confirm_id")

    @property
    def jump_url(self) -> JSONValue:
        return self.details.get("jump_url")


class TradingDisabledError(FutuError):
    """Live order mutation was not explicitly enabled."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward bearer tokens or replay trading requests on redirects.
        return None


_OPENER = build_opener(_NoRedirect())


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _identifier(value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Account/order ID must be a string or integer")
    return quote(_text(str(value), "ID"), safe="")


def _positive(value: Number, name: str) -> str:
    """Validate a decimal and serialize without float conversion or rounding."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a positive finite decimal")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a positive finite decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a positive finite decimal")
    return format(number, "f")


def _symbol(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(
        r"[A-Z]+\.[^\s/?.#]+(?:\.[^\s/?.#]+)*", code
    ):
        raise ValueError("code must use MARKET.SYMBOL format, e.g. US.VOO")
    return code


def _choice(value: str, choices: set[str], name: str) -> str:
    if value not in choices:
        raise ValueError(f"{name} must be one of {sorted(choices)}")
    return value


def _raise_api(payload: JSONObject, status: int | None = None) -> None:
    error = payload.get("error")
    message = payload.get("errmsg") or payload.get("ret_msg") or payload.get("error_description")
    if not message and isinstance(error, dict):
        message = error.get("message")
    if not message and isinstance(error, str):
        message = error
    code = payload.get("errcode", payload.get("ret_code", payload.get("error")))
    error_type = (
        OrderConfirmationRequired
        if payload.get("need_order_confirm") is True or payload.get("jump_url")
        else FutuAPIError
    )
    raise error_type(
        str(message or f"Futunn API request failed (HTTP {status})"),
        code=code, status=status, details=payload,
    )


def _http(
    method: str, path: str, *, timeout: float,
    token: str | None = None, query: Mapping[str, JSONValue] | None = None,
    body: JSONObject | None = None, form: Mapping[str, str] | None = None,
) -> JSONObject:
    """Send once without redirects/retries; business errors are decoded above."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    url = HOST + path
    if query is not None:
        query_parameters = {key: value for key, value in query.items() if value is not None}
        url += "?" + urlencode(query_parameters)
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {_text(token, 'access_token')}"
    data = None
    if body is not None:
        data = json.dumps(body, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=data, headers=headers, method=method)
    status = None
    response_status = None
    try:
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                response_status = getattr(response, "status", None)
                raw = response.read()
        except HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            finally:
                exc.close()
    except (URLError, OSError, HTTPException) as exc:
        raise FutuTransportError(
            f"{method} {path} failed; reconcile orders before retrying a mutation"
        ) from exc
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FutuAPIError("Non-JSON API response", status=status or response_status) from exc
    if not isinstance(payload, dict):
        raise FutuAPIError("Expected a JSON object response", status=status or response_status)
    if status is not None or (
        ("s" in payload and payload["s"] != "ok")
        or ("s" not in payload and "ret_code" in payload and payload["ret_code"] != 0)
    ):
        _raise_api(payload, status or response_status)
    return payload


@dataclass(frozen=True)
class TokenSet:
    """OAuth credentials; expires_at is Unix seconds, not a duration."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    scope: str = ""

    def __post_init__(self) -> None:
        _text(self.access_token, "access_token")
        if self.refresh_token is not None:
            _text(self.refresh_token, "refresh_token")
        if self.expires_at is not None and not math.isfinite(self.expires_at):
            raise ValueError("expires_at must be finite")

    @classmethod
    def from_response(
        cls, response: JSONObject, previous_refresh_token: str | None = None,
    ) -> TokenSet:
        access_token = response.get("access_token")
        expires_in = response.get("expires_in")
        refresh_token = response.get("refresh_token", previous_refresh_token)
        scope = response.get("scope", "")
        token_type = response.get("token_type", "Bearer")
        if not isinstance(access_token, str) or not access_token.strip():
            raise FutuAPIError("Invalid OAuth token response")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            raise FutuAPIError("Invalid OAuth token response")
        if refresh_token is not None and (
            not isinstance(refresh_token, str) or not refresh_token.strip()
        ):
            raise FutuAPIError("Invalid OAuth token response")
        if (
            not isinstance(scope, str)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
        ):
            raise FutuAPIError("Invalid OAuth token response")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
            scope=scope,
        )


@dataclass(frozen=True)
class AuthorizationSession:
    """Keep with its URL until callback exchange; regenerating breaks PKCE."""

    url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    redirect_uri: str
    client_id: str


class OAuthClient:
    """OAuth public client with PKCE; caller owns secure token persistence."""

    def __init__(self, client_id: str, redirect_uri: str, *, timeout: float = 15) -> None:
        self.client_id = _text(client_id, "client_id")
        self.redirect_uri = _text(redirect_uri, "redirect_uri")
        self.timeout = timeout

    @staticmethod
    def register(
        redirect_uri: str, client_name: str = "Python quant client", *, timeout: float = 15,
    ) -> JSONObject:
        payload = _http("POST", "/oauth2/register", timeout=timeout, body={
            "redirect_uris": [_text(redirect_uri, "redirect_uri")],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": _text(client_name, "client_name"),
        })
        if "error" in payload:
            _raise_api(payload)
        if not isinstance(payload.get("client_id"), str):
            raise FutuAPIError("Missing client_id in registration response")
        return payload

    def authorize(self) -> AuthorizationSession:
        """Build the browser URL locally; no authorization or HTTP call yet."""
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        url = HOST + "/oauth2/authorize/confirm?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        })
        return AuthorizationSession(
            url=url,
            state=state,
            code_verifier=verifier,
            redirect_uri=self.redirect_uri,
            client_id=self.client_id,
        )

    def exchange_callback(self, callback_url: str, session: AuthorizationSession) -> TokenSet:
        """Validate the matching callback before exchanging its single-use code."""
        actual = urlsplit(callback_url)
        expected = urlsplit(self.redirect_uri)
        if (
            session.client_id != self.client_id or session.redirect_uri != self.redirect_uri
            or (actual.scheme, actual.netloc, actual.path)
            != (expected.scheme, expected.netloc, expected.path)
            or actual.fragment
        ):
            raise ValueError("Callback does not match this OAuth authorization session")
        query = parse_qs(actual.query, keep_blank_values=True)
        states = query.get("state", [])
        if len(states) != 1 or not secrets.compare_digest(states[0], session.state):
            raise ValueError("Invalid OAuth state")
        if "error" in query:
            raise FutuAPIError("OAuth authorization denied", code=query["error"][0])
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise ValueError("Callback must contain exactly one authorization code")
        return self._token({
            "grant_type": "authorization_code",
            "code": codes[0],
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_verifier": session.code_verifier,
        })

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token({
            "grant_type": "refresh_token",
            "refresh_token": _text(refresh_token, "refresh_token"),
            "client_id": self.client_id,
        }, previous_refresh_token=refresh_token)

    def _token(
        self, form: Mapping[str, str], previous_refresh_token: str | None = None,
    ) -> TokenSet:
        payload = _http("POST", "/oauth2/token", timeout=self.timeout, form=form)
        if "error" in payload:
            _raise_api(payload)
        return TokenSet.from_response(payload, previous_refresh_token)


def _validate_history_range(start: int | None, end: int | None) -> None:
    for value in (start, end):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("start/end must be non-negative Unix microseconds")
    if start and end and end <= start:
        raise ValueError("end must be after start")


class _ClientTransport:
    """Shared token refresh and response-envelope handling for API clients."""

    def __init__(
        self, token: TokenSet, *, oauth: OAuthClient | None = None,
        timeout: float = 15,
        on_token_refresh: Callable[[TokenSet], None] | None = None,
    ) -> None:
        self.token = token
        self.oauth = oauth
        self.timeout = timeout
        self.on_token_refresh = on_token_refresh

    def _refresh_token_if_needed(self) -> None:
        if (
            self.token.expires_at is not None
            and self.token.expires_at <= time.time() + TOKEN_REFRESH_LEEWAY_SECONDS
        ):
            if self.oauth is None or self.token.refresh_token is None:
                raise FutuAPIError("Token expiring/expired; OAuth refresh credentials required")
            refreshed = self.oauth.refresh(self.token.refresh_token)
            if self.on_token_refresh is not None:
                self.on_token_refresh(refreshed)
            self.token = refreshed

    def _request(
        self, method: str, path: str, *,
        query: JSONObject | None = None, body: JSONObject | None = None,
    ) -> JSONValue:
        """Refresh, send once, then decode either supported API envelope."""
        self._refresh_token_if_needed()
        payload = _http(
            method, path, timeout=self.timeout, token=self.token.access_token,
            query=query, body=body,
        )
        if "s" in payload:
            if payload["s"] != "ok":
                _raise_api(payload)
            if "d" not in payload and method not in {"PUT", "DELETE"}:
                raise FutuAPIError("Trading response missing d")
            return payload.get("d")
        if "ret_code" in payload:
            if payload["ret_code"] != 0:
                _raise_api(payload)
            if "data" not in payload:
                raise FutuAPIError("Quote response missing data")
            return payload["data"]
        raise FutuAPIError("Unrecognized API response envelope")

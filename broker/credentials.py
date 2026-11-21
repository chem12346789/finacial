"""Persist OAuth credentials in Secret Service or GCP Secret Manager, never plaintext files.

Credentials is the stored record; SecretServiceStore owns the profile and
client factories. The CLI only prompts locally and never prints secrets.

On headless GCP hosts without a Secret Service session bus, the store uses
Google Cloud Secret Manager instead. There the profile lives at
{project}/secrets/finacial-futu-{profile} (project defaults to the metadata
server's project ID). Access token refreshes write a new secret version.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from getpass import getpass
import json
import math
import re
import sys
from typing import TYPE_CHECKING, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .futu_client import FutuClient
from .client import FutuError, OAuthClient, TokenSet

if TYPE_CHECKING:
    from secretstorage.collection import Collection
    from .sim_trading_client import SimTradingClient

APPLICATION = "finacial-futu"
DEFAULT_REDIRECT = "http://localhost:60355/callback"


class CredentialStoreError(FutuError):
    """Secret Service is unavailable, locked, missing data, or failed."""


@dataclass(frozen=True)
class Credentials:
    """One stored OAuth profile; account_id selects a real account only."""

    tokens: TokenSet = field(repr=False)
    client_id: str
    redirect_uri: str = DEFAULT_REDIRECT
    account_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise ValueError("client_id is required")
        if not isinstance(self.redirect_uri, str) or not self.redirect_uri.strip():
            raise ValueError("redirect_uri is required")
        if self.account_id is not None and (
            not isinstance(self.account_id, str) or not self.account_id.strip()
        ):
            raise ValueError("account_id must be a non-empty string")

    def serialize(self) -> bytes:
        """Encode secret content for Secret Service, not for logging or files."""
        return json.dumps({
            "version": 1,
            "access_token": self.tokens.access_token,
            "refresh_token": self.tokens.refresh_token,
            "expires_at": self.tokens.expires_at,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "account_id": self.account_id,
        }, allow_nan=False).encode("utf-8")

    @classmethod
    def deserialize(cls, raw: bytes) -> Credentials:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported credential format")
            expiry = data["expires_at"]
            if expiry is not None and (
                isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
            ):
                raise ValueError("Invalid expiry")
            return cls(
                tokens=TokenSet(
                    access_token=data["access_token"],
                    refresh_token=data["refresh_token"],
                    expires_at=expiry,
                ),
                client_id=data["client_id"],
                redirect_uri=data["redirect_uri"],
                account_id=data["account_id"],
            )
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise CredentialStoreError("Invalid stored credentials; replace the profile") from exc


class SecretServiceStore:
    """Access one application profile in the existing default collection.

    profile is a record name, not a password or account ID. No method unlocks
    the keyring, changes its password, or falls back to a plaintext backend.
    """

    def __init__(self, profile: str = "default") -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile):
            raise ValueError("profile must contain 1-64 letters, digits, underscores or hyphens")
        self.profile = profile
        self.attributes = {"application": APPLICATION, "profile": profile}

    def _connect(self):
        try:
            import secretstorage
        except ImportError as exc:
            raise CredentialStoreError(
                "Install the project dependencies from the repository root with "
                "`python -m pip install -r requirements.txt`"
            ) from exc
        try:
            return secretstorage, secretstorage.dbus_init()
        except (secretstorage.SecretStorageException, OSError) as exc:
            raise CredentialStoreError(
                "Cannot connect to this user's Secret Service session bus"
            ) from exc

    @staticmethod
    def _collection(secretstorage, connection) -> Collection:
        # Collection() fails if missing rather than creating/unlocking a keyring.
        collection = secretstorage.Collection(connection)
        if collection.is_locked():
            raise CredentialStoreError(
                "Default keyring is locked; unlock it with Passwords and Keys (seahorse)"
            )
        collection.session = secretstorage.util.open_session(connection)
        if not collection.session.encrypted:
            raise CredentialStoreError("Secret Service does not support encrypted sessions")
        return collection

    def check(self) -> None:
        """Check service and lock state without retrieving any stored secrets."""
        secretstorage, connection = self._connect()
        try:
            self._collection(secretstorage, connection)
        except (secretstorage.SecretStorageException, OSError) as exc:
            raise CredentialStoreError("Cannot access the default Secret Service collection") from exc
        finally:
            connection.close()

    def save(self, credentials: Credentials) -> None:
        """Replace this profile only; tokens remain inside the secret payload."""
        secretstorage, connection = self._connect()
        try:
            collection = self._collection(secretstorage, connection)
            collection.create_item(
                f"Futu API ({self.profile})", self.attributes,
                credentials.serialize(), replace=True, content_type="application/json",
            )
        except (secretstorage.SecretStorageException, OSError) as exc:
            raise CredentialStoreError("Failed to save credentials to Secret Service") from exc
        finally:
            connection.close()

    def load(self) -> Credentials:
        secretstorage, connection = self._connect()
        try:
            collection = self._collection(secretstorage, connection)
            items = list(collection.search_items(self.attributes))
            if not items:
                raise CredentialStoreError(
                    f"No credentials for profile {self.profile!r}; run the set command first"
                )
            if len(items) != 1:
                raise CredentialStoreError("Duplicate credential entries; resolve them in seahorse")
            item = items[0]
            if item.is_locked():
                raise CredentialStoreError("Credential entry is locked; unlock it in seahorse")
            return Credentials.deserialize(item.get_secret())
        except (secretstorage.SecretStorageException, OSError) as exc:
            raise CredentialStoreError("Failed to read credentials from Secret Service") from exc
        finally:
            connection.close()

    def delete(self) -> None:
        """Remove local records for this profile; does not revoke OAuth tokens."""
        secretstorage, connection = self._connect()
        try:
            collection = self._collection(secretstorage, connection)
            items = list(collection.search_items(self.attributes))
            if not items:
                raise CredentialStoreError(f"No credentials for profile {self.profile!r}")
            for item in items:
                item.delete()
        except (secretstorage.SecretStorageException, OSError) as exc:
            raise CredentialStoreError("Failed to delete credentials from Secret Service") from exc
        finally:
            connection.close()

    def sim_client(self) -> SimTradingClient:
        """Reuse OAuth, but never reuse the saved real-account selection."""
        from .sim_trading_client import SimTradingClient

        real_client = self.client()
        return SimTradingClient(
            real_client.token,
            oauth=real_client.oauth,
            on_token_refresh=real_client.on_token_refresh,
        )

    def client(self) -> FutuClient:
        """Create a dry-run client and persist refreshed tokens before API calls."""
        credentials = self.load()

        def persist_refreshed_tokens(tokens: TokenSet) -> None:
            nonlocal credentials
            updated = replace(credentials, tokens=tokens)
            self.save(updated)
            credentials = updated

        return FutuClient(
            credentials.tokens,
            account_id=credentials.account_id,
            oauth=OAuthClient(credentials.client_id, credentials.redirect_uri),
            on_token_refresh=persist_refreshed_tokens,
        )


class _SecretManagerAPI(Protocol):
    def access_secret_version(self, name: str) -> bytes: ...
    def add_secret_version(self, parent: str, data: bytes) -> None: ...
    def create_secret(self, project: str, secret_id: str) -> None: ...


_METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
_TIMEOUT_SECONDS = 10


def _metadata_project_id() -> str | None:
    """Resolve the GCP project ID from the instance metadata server."""
    try:
        request = Request(_METADATA_URL, headers={"Metadata-Flavor": "Google"})
        with urlopen(request, timeout=2) as response:
            project = response.read().decode("utf-8").strip()
    except (HTTPError, URLError, OSError, UnicodeDecodeError):
        return None
    return project or None


class _GCPSecretManagerAPI:
    """Minimal Secret Manager v1 REST client over urllib; only writes versions."""

    _HOST = "https://secretmanager.googleapis.com/v1"

    def __init__(self, access_token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, url: str, body: JSONObject | None) -> JSONObject:
        data = json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None
        request = Request(url, data=data, headers=self._headers, method=method)
        try:
            with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise CredentialStoreError(f"Secret Manager request failed: HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise CredentialStoreError("Cannot reach GCP Secret Manager") from exc

    @staticmethod
    def _name(project: str, secret_id: str) -> str:
        return f"projects/{project}/secrets/{secret_id}"

    def create_secret(self, project: str, secret_id: str) -> None:
        self._call("POST", f"{self._HOST}/projects/{project}/secrets?secretId={secret_id}", {
            "replication": {"automatic": {}},
        })

    def add_secret_version(self, parent: str, data: bytes) -> None:
        import base64
        self._call("POST", f"{self._HOST}/{parent}:addVersion", {
            "payload": {"data": base64.b64encode(data).decode("ascii")},
        })

    def access_secret_version(self, name: str) -> bytes:
        import base64
        result = self._call("GET", f"{self._HOST}/{name}:access", None)
        return base64.b64decode(result["payload"]["data"])


class SecretManagerStore(SecretServiceStore):
    """Store the profile in GCP Secret Manager; same interface as the keyring."""

    def __init__(self, profile: str = "default", *, project: str | None = None) -> None:
        super().__init__(profile)
        self.project = project if project is not None else _metadata_project_id()
        if self.project is None:
            raise CredentialStoreError(
                "GCP project ID is unavailable; pass project= or run on a GCP instance"
            )

    @property
    def _secret_id(self) -> str:
        return f"{APPLICATION}-{self.profile}"

    def _api(self) -> _SecretManagerAPI:
        try:
            import google.auth  # type: ignore[import-untyped]
            import google.auth.transport.requests  # type: ignore[import-untyped]
        except ImportError as exc:
            raise CredentialStoreError(
                "Install google-auth to use GCP Secret Manager credentials"
            ) from exc
        try:
            credentials, _ = google.auth.default(scopes=[
                "https://www.googleapis.com/auth/cloud-platform",
            ])
            credentials.refresh(google.auth.transport.requests.Request())
        except Exception as exc:
            raise CredentialStoreError("GCP application default credentials are unavailable") from exc
        return _GCPSecretManagerAPI(credentials.token)

    def check(self) -> None:
        self._api().access_secret_version(
            f"{_GCPSecretManagerAPI._name(self.project, self._secret_id)}/versions/latest"
        )

    def save(self, credentials: Credentials) -> None:
        api = self._api()
        parent = _GCPSecretManagerAPI._name(self.project, self._secret_id)
        try:
            api.add_secret_version(parent, credentials.serialize())
        except CredentialStoreError:
            api.create_secret(self.project, self._secret_id)
            api.add_secret_version(parent, credentials.serialize())

    def load(self) -> Credentials:
        raw = self._api().access_secret_version(
            f"{_GCPSecretManagerAPI._name(self.project, self._secret_id)}/versions/latest"
        )
        return Credentials.deserialize(raw)

    def delete(self) -> None:
        api = self._api()
        api._call("DELETE", f"{api._HOST}/{_GCPSecretManagerAPI._name(self.project, self._secret_id)}", None)


def get_store(profile: str = "default") -> SecretServiceStore:
    """Pick Secret Service locally, Secret Manager on headless GCP hosts."""
    store = SecretServiceStore(profile)
    try:
        store.check()
        return store
    except CredentialStoreError:
        pass
    return SecretManagerStore(profile)


def _store_for(profile: str, backend: str) -> SecretServiceStore:
    if backend == "gcp":
        return SecretManagerStore(profile)
    if backend == "secret-service":
        return SecretServiceStore(profile)
    raise ValueError(f"Unknown credential backend: {backend}")


def _prompt_credentials() -> Credentials:
    client_id = input("OAuth client ID: ").strip()
    redirect_uri = input(f"Redirect URI [{DEFAULT_REDIRECT}]: ").strip() or DEFAULT_REDIRECT
    account_id = getpass("Account ID (optional, hidden): ").strip() or None
    access_token = getpass("Access token (hidden): ").strip()
    refresh_token = getpass("Refresh token (optional, hidden): ").strip() or None
    expiry_text = input("Access token expires_at (Unix seconds, optional): ").strip()
    tokens = TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expiry_text) if expiry_text else None,
    )
    return Credentials(
        tokens=tokens,
        client_id=client_id,
        redirect_uri=redirect_uri,
        account_id=account_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Futu credentials in Secret Service or GCP Secret Manager")
    parser.add_argument("command", choices=["check", "set", "delete"])
    parser.add_argument("--profile", default="default")
    parser.add_argument(
        "--backend", choices=["auto", "secret-service", "gcp"], default="auto",
        help="auto uses Secret Service when unlocked, else GCP Secret Manager",
    )
    args = parser.parse_args(argv)
    try:
        store = get_store(args.profile) if args.backend == "auto" else _store_for(args.profile, args.backend)
        if args.command == "check":
            store.check()
            print(f"Credential store ready ({type(store).__name__}); no secrets were read.")
        elif args.command == "set":
            if not sys.stdin.isatty():
                raise CredentialStoreError("Run set in an interactive terminal; stdin must be a TTY")
            print(f"Saving will replace any existing Futu credentials in profile {args.profile!r}.")
            store.save(_prompt_credentials())
            print("Credentials saved. No plaintext credential file was created.")
        else:
            if not sys.stdin.isatty():
                raise CredentialStoreError("Run delete in an interactive terminal")
            if input(f"Type {args.profile!r} to delete this Futu profile: ") != args.profile:
                print("Deletion cancelled.")
                return 0
            store.delete()
            print("Futu credentials deleted. OAuth tokens have NOT been revoked at the provider.")
    except (FutuError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

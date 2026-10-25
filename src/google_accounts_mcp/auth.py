import os
import sqlite3
import threading
import time
from datetime import UTC, datetime

from google.auth import credentials as ga_credentials
from google.auth import exceptions as ga_exceptions
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from google_accounts_mcp import config
from google_accounts_mcp.broker_client import BrokerClient, BrokerError
from google_accounts_mcp.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET


class TokenStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        # Refresh tokens are stored in plaintext — keep the DB owner-only.
        # Create with 0600 (sqlite would use the umask) and tighten any
        # pre-existing DB; WAL sidecar files inherit the main DB's mode.
        fd = os.open(self.db_path, os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o600)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY,
                    refresh_token TEXT NOT NULL
                )
            """)
            conn.commit()

    def list_accounts(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT email FROM accounts ORDER BY email").fetchall()
        return [r[0] for r in rows]

    def get_credentials(self, email: str) -> Credentials | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT refresh_token FROM accounts WHERE email = ?", (email,)
            ).fetchone()
        if not row:
            return None

        # Do NOT pass scopes= here. google-auth would include it in the
        # refresh request, which makes Google reject with invalid_scope if
        # the stored refresh token was granted a narrower set than our
        # current SCOPES (e.g. an old Gmail-only token vs. the new
        # Gmail+Drive SCOPES list). Refreshing without a scope parameter
        # returns an access token scoped to whatever the refresh token
        # was originally granted, which is the behaviour we want.
        creds = Credentials(
            token=None,
            refresh_token=row[0],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
        creds.refresh(Request())
        return creds

    def save_credentials(self, email: str, refresh_token: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO accounts (email, refresh_token) VALUES (?, ?)",
                (email, refresh_token),
            )
            conn.commit()


_BROKER_HINTS = {
    "reauth_required": "Google rejected its stored token: re-authorise the account in the google-auth broker (Reissue token).",
    "insufficient_scope": "it has not granted a scope this server needs: reissue its token in the google-auth broker and approve every permission.",
    "forbidden": "it is not shared with this server in the google-auth broker.",
    "unknown_account": "it is not authorised in the google-auth broker.",
    "unauthorized": "the google-auth broker rejected this server's bearer (GOOGLE_AUTH_BEARER).",
    "unavailable": "the google-auth broker could not be reached or could not reach Google; try again shortly.",
}


def _refresh_error(account: str, exc: BrokerError) -> ga_exceptions.RefreshError:
    hint = _BROKER_HINTS.get(exc.code, exc.message)
    missing = exc.detail.get("missing")
    if missing:
        hint += " Missing: " + ", ".join(s.rsplit("/", 1)[-1] for s in missing)
    return ga_exceptions.RefreshError(f"{account}: {hint}", retryable=exc.transient)


class BrokerCredentials(ga_credentials.Credentials):
    """google-auth credentials whose refresh() asks the google-auth broker.

    A cached service object built on these picks up a re-authorisation on its
    next refresh: nothing here holds a refresh token."""

    def __init__(self, client: BrokerClient, account: str):
        super().__init__()
        self._client = client
        self._account = account

    def refresh(self, request=None):
        # Asked to refresh while the token still looked valid = the API
        # rejected it (401): skip the client cache.
        force = self.token is not None and not self.expired
        try:
            info = self._client.token_info(self._account, force=force)
        except BrokerError as exc:
            raise _refresh_error(self._account, exc) from None
        self.token = info["access_token"]
        # google-auth compares against a naive UTC datetime.
        self.expiry = datetime.fromtimestamp(info["expires_at"], UTC).replace(tzinfo=None)


class BrokerTokenStore:
    """Same surface as TokenStore, backed by the google-auth broker. The
    account list is whatever the broker shares with this server."""

    _LIST_TTL = 30

    def __init__(self, url: str, bearer: str):
        # 300 s: google-auth treats a token as expired 225 s before its
        # expiry, so the client must fetch a fresh one before that point.
        self._client = BrokerClient(url, bearer, min_ttl=300)
        self._accounts: tuple[float, list[str]] | None = None
        self._lock = threading.Lock()

    def list_accounts(self) -> list[str]:
        with self._lock:
            if self._accounts and time.monotonic() - self._accounts[0] < self._LIST_TTL:
                return list(self._accounts[1])
            try:
                status = self._client.status()
            except BrokerError as exc:
                raise ValueError(f"Cannot list accounts: {_BROKER_HINTS.get(exc.code, exc.message)}") from None
            accounts = sorted(a["account"] for a in status.get("accounts", []))
            self._accounts = (time.monotonic(), accounts)
            return list(accounts)

    def get_credentials(self, email: str) -> BrokerCredentials | None:
        creds = BrokerCredentials(self._client, email)
        creds.refresh()
        return creds

    def save_credentials(self, email: str, refresh_token: str):
        raise RuntimeError("Accounts are managed in the google-auth broker when GOOGLE_AUTH_URL is set.")


def make_store():
    """The token source for this process: the broker when configured, else
    the local refresh-token database."""
    if config.GOOGLE_AUTH_URL:
        if not config.GOOGLE_AUTH_BEARER:
            raise SystemExit("GOOGLE_AUTH_URL is set but GOOGLE_AUTH_BEARER is not.")
        return BrokerTokenStore(config.GOOGLE_AUTH_URL, config.GOOGLE_AUTH_BEARER)
    return TokenStore(config.DB_PATH)

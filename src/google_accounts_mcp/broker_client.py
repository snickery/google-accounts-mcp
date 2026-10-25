"""Vendored verbatim from google-auth (src/google_auth_broker/client.py); do not
edit here. Broker client. Standard library only and self-contained: copy this one
file into a consumer that should not depend on the google-auth-broker package.

    client = BrokerClient("http://google-auth:8335", bearer)
    token = client.token("someone@example.com")        # cached until near expiry

Thread-safe. Raises BrokerError with a stable ``code``:
  reauth_required     the account must be re-authorised (not transient)
  insufficient_scope  the account/grant lacks a needed scope (``missing``)
  forbidden           the account is not shared with this service
  unknown_account     no such account
  unauthorized        bad bearer
  unavailable         broker unreachable or erroring (transient; retry)
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class BrokerError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, detail: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}

    @property
    def transient(self) -> bool:
        return self.code == "unavailable"


class BrokerClient:
    def __init__(self, url: str, bearer: str, timeout: float = 40, min_ttl: int = 120):
        self.url = url.rstrip("/")
        self._bearer = bearer
        self.timeout = timeout
        self.min_ttl = min_ttl
        self._cache: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def _get(self, path: str, params: dict) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
        req = urllib.request.Request(
            f"{self.url}{path}?{query}", headers={"Authorization": f"Bearer {self._bearer}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except ValueError:
                body = {}
            code = body.get("error") or "unavailable"
            if exc.code >= 500:
                code = "unavailable"
            raise BrokerError(code, body.get("message", f"HTTP {exc.code}"), exc.code, body) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BrokerError("unavailable", f"broker unreachable: {exc}") from None

    def token_info(self, account: str, scopes: str = "", force: bool = False) -> dict:
        """{access_token, expires_at, scopes}; cached until min_ttl from expiry."""
        key = (account.lower(), scopes)
        with self._lock:
            cached = self._cache.get(key)
            if not force and cached and cached["expires_at"] - time.time() > self.min_ttl:
                return cached
            try:
                info = self._get("/token", {"account": account, "min_ttl": self.min_ttl * 2, "scopes": scopes})
            except BrokerError:
                self._cache.pop(key, None)
                raise
            self._cache[key] = info
            return info

    def token(self, account: str, scopes: str = "", force: bool = False) -> str:
        """An access token. Pass force=True after an API 401 to bypass the cache."""
        return self.token_info(account, scopes, force)["access_token"]

    def status(self) -> dict:
        """Accounts shared with this service and their state."""
        return self._get("/status", {})

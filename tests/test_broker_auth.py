"""Broker-backed token source (GOOGLE_AUTH_URL set): no local refresh tokens."""

import time

import pytest
from google.auth import exceptions as ga_exceptions

from google_accounts_mcp import auth, config
from google_accounts_mcp.broker_client import BrokerError


class FakeBroker:
    def __init__(self):
        self.calls = []
        self.error = None
        self.n = 0
        self.life = 3600

    def token_info(self, account, scopes="", force=False):
        self.calls.append((account, force))
        if self.error:
            raise self.error
        self.n += 1
        return {"access_token": f"at-{self.n}", "expires_at": time.time() + self.life, "scopes": []}

    def status(self):
        self.calls.append(("status", False))
        if self.error:
            raise self.error
        return {"accounts": [{"account": "b@example.com", "state": "ok"}, {"account": "a@example.com", "state": "ok"}]}


@pytest.fixture
def store():
    s = auth.BrokerTokenStore("http://broker", "bearer")
    s._client = FakeBroker()
    return s


def test_make_store_picks_broker_only_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tokens.db"))
    monkeypatch.setattr(config, "GOOGLE_AUTH_URL", "")
    assert isinstance(auth.make_store(), auth.TokenStore)
    monkeypatch.setattr(config, "GOOGLE_AUTH_URL", "http://broker:8335")
    monkeypatch.setattr(config, "GOOGLE_AUTH_BEARER", "")
    with pytest.raises(SystemExit):
        auth.make_store()
    monkeypatch.setattr(config, "GOOGLE_AUTH_BEARER", "b")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "unused.db"))
    assert isinstance(auth.make_store(), auth.BrokerTokenStore)
    assert not (tmp_path / "unused.db").exists()


def test_accounts_come_from_the_broker_and_are_cached_briefly(store):
    assert store.list_accounts() == ["a@example.com", "b@example.com"]
    store.list_accounts()
    assert store._client.calls == [("status", False)]


def test_credentials_are_valid_and_refresh_through_the_broker(store):
    creds = store.get_credentials("a@example.com")
    assert creds.token == "at-1" and creds.valid
    headers = {}
    creds.before_request(None, "GET", "https://example.com", headers)
    assert headers["authorization"] == "Bearer at-1"
    assert store._client.calls == [("a@example.com", False)]


def test_refresh_when_near_expiry_is_not_forced(store):
    store._client.life = 60          # inside google-auth's expiry margin
    creds = store.get_credentials("a@example.com")
    assert not creds.valid
    creds.refresh()
    assert store._client.calls[-1] == ("a@example.com", False)


def test_refresh_of_a_token_that_still_looked_valid_bypasses_the_cache(store):
    creds = store.get_credentials("a@example.com")
    creds.refresh()                  # what google-auth does after an API 401
    assert store._client.calls[-1] == ("a@example.com", True) and creds.token == "at-2"


@pytest.mark.parametrize("code,needle,retryable", [
    ("reauth_required", "Reissue token", False),
    ("forbidden", "not shared with this server", False),
    ("unavailable", "try again", True),
])
def test_broker_errors_become_refresh_errors_with_a_hint(store, code, needle, retryable):
    store._client.error = BrokerError(code, "m")
    with pytest.raises(ga_exceptions.RefreshError) as e:
        store.get_credentials("a@example.com")
    assert "a@example.com" in str(e.value) and needle in str(e.value)
    assert e.value.retryable is retryable


def test_missing_scopes_are_named(store):
    store._client.error = BrokerError("insufficient_scope", "m", 409, {"missing": ["https://www.googleapis.com/auth/tasks"]})
    with pytest.raises(ga_exceptions.RefreshError, match="Missing: tasks"):
        store.get_credentials("a@example.com")


def test_list_accounts_failure_is_a_clear_error(store):
    store._client.error = BrokerError("unauthorized", "m")
    with pytest.raises(ValueError, match="GOOGLE_AUTH_BEARER"):
        store.list_accounts()


def test_saving_is_refused_in_broker_mode(store):
    with pytest.raises(RuntimeError):
        store.save_credentials("a@example.com", "rt")

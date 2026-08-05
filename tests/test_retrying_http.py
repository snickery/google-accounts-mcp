"""Tier 1/2 tests for the stale-socket retry transport.

The google-api-python-client caches one ``build(...)`` service object — and
its underlying httplib2 connection — per account for the life of the
process. When an account sits idle, Google closes the socket server-side, so
the next call reuses a dead connection and fails with "[Errno 104]
Connection reset by peer". ``_RetryingHttp`` retries such connection-level
errors exactly once on a fresh socket. These tests pin that behaviour so it
can't silently regress.
"""

from unittest import mock

import httplib2
import pytest

from google_accounts_mcp.gmail_client import _HTTP_TIMEOUT, _RetryingHttp


def test_timeout_is_applied():
    h = _RetryingHttp(timeout=_HTTP_TIMEOUT)
    assert h.timeout == _HTTP_TIMEOUT


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        TimeoutError("timed out"),
        OSError("generic socket error"),
        httplib2.HttpLib2Error("transport error"),
    ],
)
def test_retries_once_then_succeeds(exc):
    """A connection-level error on the first attempt is retried and the
    second attempt's result is returned."""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return ("RESP", "BODY")

    with mock.patch.object(httplib2.Http, "request", side_effect=flaky):
        result = _RetryingHttp().request("https://example/x", "GET")

    assert result == ("RESP", "BODY")
    assert calls["n"] == 2


def test_retry_drops_stale_pooled_connections():
    """Before retrying, the dead pooled connection must be closed/cleared so
    httplib2 dials a fresh socket instead of reusing the bad one."""
    h = _RetryingHttp()
    conn = mock.Mock()
    h.connections["https:example"] = conn

    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError(104, "reset")
        return ("RESP", "BODY")

    with mock.patch.object(httplib2.Http, "request", side_effect=flaky):
        h.request("https://example/x", "GET")

    conn.close.assert_called_once()
    assert h.connections == {}


def test_caps_at_a_single_retry():
    """A second consecutive failure propagates — no infinite retry loop."""
    calls = {"n": 0}

    def always_reset(*a, **k):
        calls["n"] += 1
        raise ConnectionResetError(104, "reset")

    with mock.patch.object(httplib2.Http, "request", side_effect=always_reset):
        with pytest.raises(ConnectionResetError):
            _RetryingHttp().request("https://example/x", "GET")

    assert calls["n"] == 2


def test_http_error_status_is_not_retried():
    """A real HTTP error status is a normal response, not a transport error,
    and must be returned to the caller without a retry."""
    calls = {"n": 0}

    def http_500(*a, **k):
        calls["n"] += 1
        return (httplib2.Response({"status": "500"}), b"err")

    with mock.patch.object(httplib2.Http, "request", side_effect=http_500):
        resp, _body = _RetryingHttp().request("https://example/x", "GET")

    assert calls["n"] == 1
    assert resp.status == 500

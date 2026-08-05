"""Tier 3 integration tests — hit the real Google Calendar API.

Skipped unless GMAIL_TEST_ACCOUNT is set (the same env var the Gmail
integration tests use). Every test creates its own fixtures and cleans
up after itself — nothing is left on the calendar on success.

Run with:
    GMAIL_TEST_ACCOUNT=bob@example.com \\
    TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db \\
    uv run --extra test pytest tests/test_calendar_integration.py -v

Requires:
  * A refresh token stored for GMAIL_TEST_ACCOUNT that was issued AFTER
    calendar.events was added to SCOPES (re-run scripts/authorize.py
    against the account if this is the first calendar test run).
  * GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import requires_gmail


@pytest.fixture(scope="module")
def real_calendar():
    """A fully-wired CalendarClient against the real Calendar API."""
    from google_accounts_mcp.calendar_client import CalendarClient
    return CalendarClient()


@pytest.fixture(scope="module")
def test_account():
    return os.environ["GMAIL_TEST_ACCOUNT"]


@pytest.fixture
def run_id():
    return f"cal-itest-{uuid.uuid4().hex[:8]}"


def _future_window(hours_from_now: int = 24, duration_minutes: int = 30):
    """Return (start, end) as RFC3339 strings — far enough in the future
    to avoid stomping on anything real on the test account's primary
    calendar, and short enough to be obviously a test."""
    start = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    end = start + timedelta(minutes=duration_minutes)
    # Strip microseconds for readability.
    start = start.replace(microsecond=0)
    end = end.replace(microsecond=0)
    return start.isoformat(), end.isoformat()


def _delete_quietly(real_calendar, event_id, calendar_id="primary"):
    try:
        real_calendar.delete_event(
            event_id=event_id, calendar_id=calendar_id, send_updates="none",
        )
    except Exception:
        pass


def _extract_event_id(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("id:"):
            return line.split("id:", 1)[1].strip()
    raise AssertionError(f"Could not find id: in output:\n{output}")


# ===================================================================
# Connectivity
# ===================================================================

@requires_gmail
class TestConnectivity:
    def test_list_calendars(self, real_calendar, test_account):
        out = real_calendar.list_calendars(account=test_account)
        # Every account has at least a primary calendar.
        assert "primary" in out.lower() or "(primary)" in out


# ===================================================================
# Event CRUD
# ===================================================================

@requires_gmail
class TestEventCrud:
    def test_create_read_update_delete(self, real_calendar, test_account, run_id):
        start, end = _future_window()
        created = real_calendar.create_event(
            summary=f"{run_id} integration",
            start=start,
            end=end,
            description="Created by gmail-mcp integration test",
            account=test_account,
        )
        assert run_id in created
        event_id = _extract_event_id(created)
        try:
            # Read it back.
            fetched = real_calendar.get_event(
                event_id=event_id, account=test_account,
            )
            assert run_id in fetched

            # Patch summary and description clear.
            updated = real_calendar.update_event(
                event_id=event_id,
                account=test_account,
                summary=f"{run_id} updated",
                description="",
            )
            assert "updated" in updated
        finally:
            _delete_quietly(real_calendar, event_id)

    def test_all_day_event(self, real_calendar, test_account, run_id):
        day = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
        next_day = (
            datetime.now(timezone.utc) + timedelta(days=4)
        ).strftime("%Y-%m-%d")
        created = real_calendar.create_event(
            summary=f"{run_id} all-day",
            start=day,
            end=next_day,
            account=test_account,
        )
        event_id = _extract_event_id(created)
        try:
            fetched = real_calendar.get_event(event_id, account=test_account)
            assert day in fetched
        finally:
            _delete_quietly(real_calendar, event_id)


# ===================================================================
# list_events windowed query
# ===================================================================

@requires_gmail
class TestListEvents:
    def test_list_finds_created_event(self, real_calendar, test_account, run_id):
        start, end = _future_window(hours_from_now=48)
        created = real_calendar.create_event(
            summary=f"{run_id} window",
            start=start,
            end=end,
            account=test_account,
        )
        event_id = _extract_event_id(created)
        try:
            # Query a window around the event.
            qs = (datetime.now(timezone.utc) + timedelta(hours=47)).isoformat()
            qe = (datetime.now(timezone.utc) + timedelta(hours=49)).isoformat()
            out = real_calendar.list_events(
                account=test_account,
                time_min=qs,
                time_max=qe,
                query=run_id,
                max_results=10,
            )
            assert run_id in out
        finally:
            _delete_quietly(real_calendar, event_id)


# ===================================================================
# quick_add_event
# ===================================================================

@requires_gmail
class TestQuickAdd:
    def test_quick_add(self, real_calendar, test_account, run_id):
        # Google's quickAdd parses loose natural language. A date far in the
        # future keeps test-created events off the visible week.
        text = f"{run_id} test event next year January 15 3pm"
        out = real_calendar.quick_add_event(text=text, account=test_account)
        event_id = _extract_event_id(out)
        try:
            assert run_id in out
        finally:
            _delete_quietly(real_calendar, event_id)


# ===================================================================
# free_busy
# ===================================================================

@requires_gmail
class TestFreeBusy:
    def test_primary_window(self, real_calendar, test_account):
        qs = datetime.now(timezone.utc).isoformat()
        qe = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        out = real_calendar.free_busy(
            time_min=qs, time_max=qe, account=test_account,
        )
        # Either free or N busy blocks — both are valid, we just want a
        # well-formed response that mentions 'primary'.
        assert "primary" in out

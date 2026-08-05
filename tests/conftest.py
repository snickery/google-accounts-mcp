"""Shared fixtures for google-accounts-mcp tests."""

import base64
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Config isolation — point the client at a temp DB + temp attachments dir
# so tests never touch the real rootless data volume.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_attachments_dir(tmp_path, monkeypatch):
    """Redirect ATTACHMENTS_DIR to a tmp path for the duration of the test."""
    d = tmp_path / "attachments"
    d.mkdir()
    monkeypatch.setattr("google_accounts_mcp.config.ATTACHMENTS_DIR", str(d))
    monkeypatch.setattr("google_accounts_mcp.gmail_client.ATTACHMENTS_DIR", str(d))
    return d


@pytest.fixture
def tmp_shared_dir(tmp_path, monkeypatch):
    """Redirect SHARED_DIR to a tmp path for the duration of the test."""
    d = tmp_path / "shared"
    d.mkdir()
    monkeypatch.setattr("google_accounts_mcp.config.SHARED_DIR", str(d))
    monkeypatch.setattr("google_accounts_mcp.gmail_client.SHARED_DIR", str(d))
    return d


@pytest.fixture
def tmp_token_db(tmp_path, monkeypatch):
    """Create an empty token DB and point DB_PATH at it."""
    db = tmp_path / "tokens.db"
    monkeypatch.setattr("google_accounts_mcp.config.DB_PATH", str(db))
    monkeypatch.setattr("google_accounts_mcp.gmail_client.DB_PATH", str(db))
    return str(db)


# ---------------------------------------------------------------------------
# Client factory — gives back a GmailClient wired to the tmp DB/attachments
# with its _get_service method patched so no Google API calls happen.
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_token_db, tmp_attachments_dir, tmp_shared_dir):
    """A GmailClient with a tmp token DB and stubbed service resolution."""
    from google_accounts_mcp.gmail_client import GmailClient

    # Seed the token DB with a couple of fake accounts so _resolve_account works.
    with sqlite3.connect(tmp_token_db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS accounts "
            "(email TEXT PRIMARY KEY, refresh_token TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO accounts (email, refresh_token) VALUES (?, ?)",
            [
                ("alice@example.com", "fake-refresh-1"),
                ("bob@example.com", "fake-refresh-2"),
            ],
        )
        conn.commit()

    c = GmailClient()
    return c


@pytest.fixture
def mock_service():
    """A MagicMock shaped like a googleapiclient Resource for Gmail."""
    return MagicMock(name="gmail_service")


@pytest.fixture
def mock_calendar_service():
    """A MagicMock shaped like a googleapiclient Resource for Calendar."""
    return MagicMock(name="calendar_service")


@pytest.fixture
def calendar_client(client, mock_calendar_service, monkeypatch):
    """CalendarClient wired to the same temp token DB as `client`, with
    _get_service short-circuited so tests can assert on the raw Google-API
    mock calls. Shares the GmailClient's TokenStore via `client.store`."""
    from google_accounts_mcp.calendar_client import CalendarClient

    cc = CalendarClient(store=client.store)

    def fake_get_service(account):
        acct = cc._resolve_account(account)
        return mock_calendar_service, acct

    monkeypatch.setattr(cc, "_get_service", fake_get_service)
    return cc


@pytest.fixture
def mock_tasks_service():
    """A MagicMock shaped like a googleapiclient Resource for Tasks."""
    return MagicMock(name="tasks_service")


@pytest.fixture
def tasks_client(client, mock_tasks_service, monkeypatch):
    """TasksClient wired to the same temp token DB as `client`, with
    _get_service short-circuited so tests can assert on the raw Google-API
    mock calls. Shares the GmailClient's TokenStore via `client.store`."""
    from google_accounts_mcp.tasks_client import TasksClient

    tc = TasksClient(store=client.store)

    def fake_get_service(account):
        acct = tc._resolve_account(account)
        return mock_tasks_service, acct

    monkeypatch.setattr(tc, "_get_service", fake_get_service)
    return tc


@pytest.fixture
def mock_people_service():
    """A MagicMock shaped like a googleapiclient Resource for People."""
    return MagicMock(name="people_service")


@pytest.fixture
def contacts_client(client, mock_people_service, monkeypatch):
    """ContactsClient wired to the same temp token DB as `client`, with
    _get_service short-circuited and the warmup pause zeroed."""
    from google_accounts_mcp import contacts_client as contacts_module
    from google_accounts_mcp.contacts_client import ContactsClient

    cc = ContactsClient(store=client.store)

    def fake_get_service(account):
        acct = cc._resolve_account(account)
        return mock_people_service, acct

    monkeypatch.setattr(cc, "_get_service", fake_get_service)
    monkeypatch.setattr(contacts_module, "_WARMUP_PAUSE", 0)
    return cc


@pytest.fixture
def mock_drive_service():
    """A MagicMock shaped like a googleapiclient Resource for Drive."""
    return MagicMock(name="drive_service")


@pytest.fixture
def patched_client(client, mock_service, mock_drive_service, monkeypatch):
    """Client with _get_service / _get_drive_service / _get_drive_folder_id
    short-circuited, so tests can assert on the raw Google-API mock calls."""
    def fake_get_service(account):
        acct = client._resolve_account(account)
        return mock_service, acct

    monkeypatch.setattr(client, "_get_service", fake_get_service)
    monkeypatch.setattr(client, "_get_drive_service", lambda: mock_drive_service)
    monkeypatch.setattr(client, "_get_drive_folder_id", lambda: "folder-123")
    return client


# ---------------------------------------------------------------------------
# Gmail API response helpers
# ---------------------------------------------------------------------------

def make_header(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def make_message(
    msg_id: str = "msg-001",
    thread_id: str = "thread-001",
    subject: str = "Test subject",
    sender: str = "alice@example.com",
    to: str = "bob@example.com",
    date: str = "Fri, 11 Apr 2026 12:00:00 +1000",
    body: str = "Hello body",
    snippet: str = "Hello snippet",
    labels: list[str] | None = None,
    body_mime: str = "text/plain",
) -> dict:
    """Build a minimal Gmail API message response."""
    encoded_body = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": labels or ["INBOX", "UNREAD"],
        "snippet": snippet,
        "payload": {
            "mimeType": body_mime,
            "headers": [
                make_header("Date", date),
                make_header("From", sender),
                make_header("To", to),
                make_header("Subject", subject),
                make_header("Message-ID", f"<{msg_id}@example.com>"),
            ],
            "body": {"data": encoded_body, "size": len(body)},
        },
    }


# ---------------------------------------------------------------------------
# Integration test marker (Tier 3)
# ---------------------------------------------------------------------------

requires_gmail = pytest.mark.skipif(
    not os.environ.get("GMAIL_TEST_ACCOUNT"),
    reason="Set GMAIL_TEST_ACCOUNT to run integration tests",
)


@pytest.fixture(autouse=True)
def neutral_account_config(monkeypatch):
    """Pin the account defaults to the fixture identities so tests never
    depend on (or leak) a deployer's real configuration."""
    for mod in ("gmail_client", "calendar_client", "tasks_client", "contacts_client"):
        monkeypatch.setattr(
            f"google_accounts_mcp.{mod}.DEFAULT_ACCOUNT",
            "alice@example.com",
            raising=False,
        )
    monkeypatch.setattr(
        "google_accounts_mcp.gmail_client.DRIVE_ACCOUNT",
        "bob@example.com",
        raising=False,
    )

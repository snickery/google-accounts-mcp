"""Integration tests — hit the real Gmail + Drive APIs.

Skipped unless GMAIL_TEST_ACCOUNT is set. These tests catch API drift and
verify the full OAuth token -> client -> API path works end-to-end.

Run with:
    GMAIL_TEST_ACCOUNT=bob@example.com \\
    TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db \\
    uv run --extra test pytest tests/test_integration.py -v

The tests create drafts, upload files to the shared Drive folder, and
clean up after themselves. Nothing is left behind on success. Drafts are
safe because they're not sent — no real email goes out.

Requires:
  * A refresh token already stored for GMAIL_TEST_ACCOUNT in TOKEN_DB_PATH
    (run scripts/authorize.py first against the real OAuth client).
  * GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars for token refresh.
"""

import base64
import os
import uuid

import pytest

from tests.conftest import requires_gmail


@pytest.fixture(scope="module")
def real_client():
    """A fully-wired GmailClient that talks to the real Google APIs."""
    from google_accounts_mcp.gmail_client import GmailClient
    return GmailClient()


@pytest.fixture(scope="module")
def test_account():
    return os.environ["GMAIL_TEST_ACCOUNT"]


@pytest.fixture
def run_id():
    """A unique tag for test artifacts so parallel runs don't collide."""
    return f"gaccounts-itest-{uuid.uuid4().hex[:8]}"


# ===================================================================
# Basic connectivity
# ===================================================================

@requires_gmail
class TestConnectivity:
    def test_list_accounts_includes_test_account(self, real_client, test_account):
        out = real_client.list_accounts()
        assert test_account in out

    def test_list_labels(self, real_client, test_account):
        out = real_client.list_labels(account=test_account)
        # Every Gmail mailbox has the INBOX system label.
        assert "INBOX" in out


# ===================================================================
# Search / read (non-destructive)
# ===================================================================

@requires_gmail
class TestSearchAndRead:
    def test_search_in_inbox(self, real_client, test_account):
        out = real_client.search_email(
            "in:inbox", account=test_account, max_results=3
        )
        # Either results or an explicit empty-result message — both are fine.
        assert "Search results" in out or "No messages" in out


# ===================================================================
# Draft lifecycle — create then delete
# ===================================================================

@requires_gmail
class TestDraftLifecycle:
    def test_create_and_delete_draft(self, real_client, test_account, run_id):
        out = real_client.draft_email(
            to=[test_account],  # send to self — never actually goes out
            subject=f"{run_id} integration test",
            body=f"Draft created by {run_id}",
            account=test_account,
        )
        assert "Draft ID" in out
        # Pull the draft ID out of the formatted response for cleanup.
        draft_id = next(
            line.split("Draft ID:", 1)[1].strip()
            for line in out.splitlines()
            if "Draft ID:" in line
        )
        try:
            # The draft should appear in the drafts list.
            service, _ = real_client._get_service(test_account)
            drafts_resp = service.users().drafts().list(userId="me").execute()
            draft_ids = [d["id"] for d in drafts_resp.get("drafts", [])]
            assert draft_id in draft_ids
        finally:
            service, _ = real_client._get_service(test_account)
            service.users().drafts().delete(userId="me", id=draft_id).execute()


# ===================================================================
# Upload / attach local file to a draft
# ===================================================================

@requires_gmail
class TestUploadAndAttach:
    def test_upload_then_draft_with_attachment(
        self, real_client, test_account, run_id, tmp_path
    ):
        # upload_file writes under ATTACHMENTS_DIR, which in integration mode
        # is the real /data/attachments. Use a unique name to avoid collisions.
        filename = f"{run_id}.txt"
        content_b64 = base64.b64encode(
            f"hello from {run_id}".encode()
        ).decode()
        up = real_client.upload_file(filename, content_b64)
        assert filename in up

        try:
            out = real_client.draft_email(
                to=[test_account],
                subject=f"{run_id} with attachment",
                body="See attached",
                account=test_account,
                attachments=[f"local:uploads/{filename}"],
            )
            assert "Draft ID" in out
            assert filename in out
            draft_id = next(
                line.split("Draft ID:", 1)[1].strip()
                for line in out.splitlines()
                if "Draft ID:" in line
            )
        finally:
            # Clean up the draft if it got created.
            try:
                service, _ = real_client._get_service(test_account)
                service.users().drafts().delete(userId="me", id=draft_id).execute()
            except Exception:
                pass
            # Clean up the uploaded file.
            from google_accounts_mcp.config import ATTACHMENTS_DIR
            from pathlib import Path
            (Path(ATTACHMENTS_DIR) / "uploads" / filename).unlink(missing_ok=True)


# ===================================================================
# Drive upload / list / download / delete round-trip
# ===================================================================

@requires_gmail
class TestDriveRoundTrip:
    def test_upload_list_download_delete(self, real_client, run_id):
        filename = f"{run_id}.txt"
        content = f"drive roundtrip {run_id}".encode()
        content_b64 = base64.b64encode(content).decode()

        up = real_client.drive_upload(filename, content_b64)
        assert filename in up
        # Extract the Drive file ID for cleanup.
        file_id = next(
            line.split("File ID:", 1)[1].strip()
            for line in up.splitlines()
            if "File ID:" in line
        )

        try:
            # It should appear in drive_list.
            listing = real_client.drive_list(filename)
            assert filename in listing

            # And download it back inline as base64.
            dl = real_client.drive_download(filename, return_base64=True)
            assert "Base64" in dl
            # Parse out the base64 payload and verify round-trip.
            b64_line = dl.rsplit("\n", 1)[-1]
            assert base64.standard_b64decode(b64_line) == content
        finally:
            drive = real_client._get_drive_service()
            drive.files().delete(fileId=file_id).execute()


# ===================================================================
# Draft with drive: source — end-to-end unified scheme test
# ===================================================================

@requires_gmail
class TestDraftWithDriveAttachment:
    def test_drive_scheme_attaches_file(self, real_client, test_account, run_id):
        filename = f"{run_id}-drive-src.txt"
        content_b64 = base64.b64encode(
            f"drive source test {run_id}".encode()
        ).decode()

        # Upload to Drive, then reference it via drive:<name> in draft_email.
        up = real_client.drive_upload(filename, content_b64)
        file_id = next(
            line.split("File ID:", 1)[1].strip()
            for line in up.splitlines()
            if "File ID:" in line
        )

        draft_id = None
        try:
            out = real_client.draft_email(
                to=[test_account],
                subject=f"{run_id} drive: scheme",
                body="attached via drive: scheme",
                account=test_account,
                attachments=[f"drive:{filename}"],
            )
            assert filename in out
            draft_id = next(
                line.split("Draft ID:", 1)[1].strip()
                for line in out.splitlines()
                if "Draft ID:" in line
            )
        finally:
            if draft_id:
                try:
                    service, _ = real_client._get_service(test_account)
                    service.users().drafts().delete(userId="me", id=draft_id).execute()
                except Exception:
                    pass
            drive = real_client._get_drive_service()
            drive.files().delete(fileId=file_id).execute()


# ===================================================================
# Label modification round-trip — uses a draft so we don't mutate
# real inbox messages.
# ===================================================================

@requires_gmail
class TestLabelsRoundTrip:
    def test_star_and_unstar(self, real_client, test_account, run_id):
        # Create a draft, grab its underlying message ID, star and unstar it.
        out = real_client.draft_email(
            to=[test_account],
            subject=f"{run_id} label test",
            body="label test",
            account=test_account,
        )
        draft_id = next(
            line.split("Draft ID:", 1)[1].strip()
            for line in out.splitlines()
            if "Draft ID:" in line
        )
        try:
            service, _ = real_client._get_service(test_account)
            draft = service.users().drafts().get(userId="me", id=draft_id).execute()
            msg_id = draft["message"]["id"]

            real_client.modify_labels(msg_id, add_labels=["STARRED"], account=test_account)
            real_client.modify_labels(msg_id, remove_labels=["STARRED"], account=test_account)
        finally:
            service, _ = real_client._get_service(test_account)
            service.users().drafts().delete(userId="me", id=draft_id).execute()

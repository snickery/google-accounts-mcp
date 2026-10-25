"""Tier 2 client tests — mock the Google API service layer.

These tests exercise GmailClient's public methods end-to-end (input parsing,
request construction, response formatting) without hitting the Google APIs.
The `patched_client` fixture from conftest replaces `_get_service` and
`_get_drive_service` with MagicMocks, so every tests can assert on the
Google-API call shape via `mock_service.users.return_value...`.
"""

import base64
from unittest.mock import MagicMock

import pytest

from tests.conftest import make_message

# ---------------------------------------------------------------------------
# Helpers for mocking the chained googleapiclient builder calls.
# ---------------------------------------------------------------------------

def _chain(mock_service, *path, execute_return=None):
    """Walk a chain like users().messages().get().execute() and configure the
    final .execute() return value. Returns the terminal execute mock so the
    test can assert on call args."""
    node = mock_service
    for name in path:
        node = getattr(node, name).return_value
    node.execute.return_value = execute_return
    return node.execute


# ===================================================================
# _resolve_account
# ===================================================================

class TestResolveAccount:
    def test_default_when_empty(self, client):
        assert client._resolve_account("") == "alice@example.com"

    def test_exact_match(self, client):
        assert client._resolve_account("bob@example.com") == "bob@example.com"

    def test_partial_unique_match(self, client):
        # "alice" uniquely matches alice@example.com (not bob@example.com)
        assert client._resolve_account("alice") == "alice@example.com"

    def test_ambiguous_raises(self, client):
        with pytest.raises(ValueError, match="Ambiguous"):
            client._resolve_account("example")  # both accounts contain "example"

    def test_missing_raises(self, client):
        with pytest.raises(ValueError, match="not found"):
            client._resolve_account("missing@example.com")


# ===================================================================
# list_accounts
# ===================================================================

class TestListAccounts:
    def test_lists_all(self, client):
        out = client.list_accounts()
        assert "alice@example.com" in out
        assert "bob@example.com" in out
        assert "(default)" in out

    def test_filter(self, client):
        out = client.list_accounts("bob")
        assert "bob@example.com" in out
        assert "alice@example.com" not in out

    def test_no_match(self, client):
        out = client.list_accounts("zzz")
        assert "No accounts matching" in out


# ===================================================================
# search_email
# ===================================================================

class TestSearchEmail:
    def test_formats_results(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "list",
               execute_return={"messages": [{"id": "m1"}, {"id": "m2"}]})
        # Both .get().execute() calls use the same chain — return different
        # messages via side_effect instead.
        mock_service.users.return_value.messages.return_value.get.return_value.execute.side_effect = [
            make_message(msg_id="m1", subject="First"),
            make_message(msg_id="m2", subject="Second"),
        ]

        out = patched_client.search_email("is:unread")
        assert "First" in out
        assert "Second" in out
        assert "m1" in out
        assert "m2" in out

    def test_no_results(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "list",
               execute_return={"messages": []})
        out = patched_client.search_email("from:nobody@example.com")
        assert "No messages" in out

    def test_passes_max_results_to_api(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "list",
               execute_return={"messages": []})
        patched_client.search_email("x", max_results=25)
        # Assert maxResults was forwarded in the list() call.
        list_call = mock_service.users.return_value.messages.return_value.list
        assert list_call.call_args.kwargs["maxResults"] == 25
        assert list_call.call_args.kwargs["q"] == "x"


# ===================================================================
# read_email / read_thread
# ===================================================================

class TestReadEmail:
    def test_returns_formatted(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return=make_message(body="hello world"))
        out = patched_client.read_email("msg-001")
        assert "hello world" in out
        assert "Test subject" in out


class TestReadThread:
    def test_multiple_messages(self, patched_client, mock_service):
        _chain(mock_service, "users", "threads", "get",
               execute_return={"messages": [
                   make_message(msg_id="m1", subject="A", body="body-a"),
                   make_message(msg_id="m2", subject="B", body="body-b"),
               ]})
        out = patched_client.read_thread("thread-001")
        assert "2 messages" in out
        assert "body-a" in out
        assert "body-b" in out


# ===================================================================
# modify_labels and wrappers
# ===================================================================

class TestModifyLabels:
    def test_add_and_remove(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "modify", execute_return={})
        out = patched_client.modify_labels(
            "m1", add_labels=["STARRED"], remove_labels=["INBOX", "UNREAD"]
        )
        modify_call = mock_service.users.return_value.messages.return_value.modify
        body = modify_call.call_args.kwargs["body"]
        assert body["addLabelIds"] == ["STARRED"]
        assert body["removeLabelIds"] == ["INBOX", "UNREAD"]
        assert "added: STARRED" in out
        assert "removed: INBOX, UNREAD" in out

    def test_no_changes(self, patched_client):
        out = patched_client.modify_labels("m1")
        assert "No label changes" in out

    def test_empty_lists_treated_as_no_changes(self, patched_client):
        out = patched_client.modify_labels("m1", add_labels=[], remove_labels=[])
        assert "No label changes" in out

    def test_archive_removes_inbox(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "modify", execute_return={})
        patched_client.archive_email("m1")
        modify_call = mock_service.users.return_value.messages.return_value.modify
        assert modify_call.call_args.kwargs["body"] == {"removeLabelIds": ["INBOX"]}

    def test_mark_read_removes_unread(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "modify", execute_return={})
        patched_client.mark_read("m1")
        body = mock_service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
        assert body == {"removeLabelIds": ["UNREAD"]}

    def test_mark_unread_adds_unread(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "modify", execute_return={})
        patched_client.mark_unread("m1")
        body = mock_service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
        assert body == {"addLabelIds": ["UNREAD"]}


# ===================================================================
# list_labels
# ===================================================================

class TestListLabels:
    def test_separates_system_and_user(self, patched_client, mock_service):
        _chain(mock_service, "users", "labels", "list",
               execute_return={"labels": [
                   {"id": "INBOX", "name": "INBOX", "type": "system"},
                   {"id": "STARRED", "name": "STARRED", "type": "system"},
                   {"id": "Label_1", "name": "Personal", "type": "user"},
               ]})
        out = patched_client.list_labels()
        assert "System labels" in out
        assert "User labels" in out
        assert "INBOX" in out
        assert "Personal" in out


# ===================================================================
# draft_email / send_email (with reply threading)
# ===================================================================

class TestDraftEmail:
    def test_basic_draft(self, patched_client, mock_service):
        _chain(mock_service, "users", "drafts", "create",
               execute_return={"id": "draft-999"})
        out = patched_client.draft_email(
            to=["a@example.com"], subject="Hi", body="Hello"
        )
        assert "draft-999" in out
        create_call = mock_service.users.return_value.drafts.return_value.create
        body = create_call.call_args.kwargs["body"]
        assert "raw" in body["message"]
        # Draft body should decode back to a valid email
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        assert b"a@example.com" in raw
        assert b"Subject: Hi" in raw

    def test_multiple_recipients_cc_bcc(self, patched_client, mock_service):
        _chain(mock_service, "users", "drafts", "create",
               execute_return={"id": "draft-multi"})
        patched_client.draft_email(
            to=["a@example.com", "b@example.com"],
            subject="Hi",
            body="Hello",
            cc=["cc1@example.com", "cc2@example.com"],
            bcc=["bcc@example.com"],
        )
        body = mock_service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        assert b"To: a@example.com, b@example.com" in raw
        assert b"Cc: cc1@example.com, cc2@example.com" in raw
        assert b"Bcc: bcc@example.com" in raw

    def test_reply_sets_threading_headers(self, patched_client, mock_service):
        # get() returns the original message headers
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "threadId": "thread-xyz",
            "payload": {"headers": [{"name": "Message-ID", "value": "<orig@example.com>"}]},
        }
        _chain(mock_service, "users", "drafts", "create",
               execute_return={"id": "draft-123"})

        patched_client.draft_email(
            to=["a@example.com"], subject="Re: Hi", body="reply",
            reply_to_message_id="orig-msg-id",
        )

        create_call = mock_service.users.return_value.drafts.return_value.create
        body = create_call.call_args.kwargs["body"]
        assert body["message"]["threadId"] == "thread-xyz"
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        assert b"In-Reply-To: <orig@example.com>" in raw
        assert b"References: <orig@example.com>" in raw

    def test_draft_with_local_attachment(self, patched_client, mock_service, tmp_attachments_dir):
        f = tmp_attachments_dir / "uploads" / "report.pdf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF-1.4 fake")
        _chain(mock_service, "users", "drafts", "create",
               execute_return={"id": "draft-with-attach"})
        out = patched_client.draft_email(
            to=["a@example.com"], subject="Hi", body="See attached",
            attachments=["local:uploads/report.pdf"],
        )
        assert "report.pdf" in out
        body = mock_service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        assert b"report.pdf" in raw
        assert b"%PDF-1.4 fake" in raw or b"JVBERi0xLjQg" in raw  # inline or b64-encoded

    def test_draft_with_drive_attachment(self, patched_client, mock_service, mock_drive_service):
        # _resolve_drive_file walks files.list first...
        _chain(mock_drive_service, "files", "list",
               execute_return={"files": [
                   {"id": "drive-1", "name": "contract.docx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "size": "10"},
               ]})
        # ..._fetch_drive_bytes uses get_media + next_chunk. Mock the chunked
        # downloader path by patching MediaIoBaseDownload.
        from unittest.mock import MagicMock as MM
        from unittest.mock import patch

        import google_accounts_mcp.gmail_client as gc
        downloader = MM()
        downloader.next_chunk.return_value = (None, True)

        def _fake_downloader(buf, _req):
            buf.write(b"contract-bytes")
            return downloader
        _chain(mock_service, "users", "drafts", "create",
               execute_return={"id": "draft-drive"})
        with patch.object(gc, "MediaIoBaseDownload", side_effect=_fake_downloader):
            out = patched_client.draft_email(
                to=["a@example.com"], subject="Hi", body="See attached",
                attachments=["drive:contract.docx"],
            )
        assert "contract.docx" in out

    def test_unknown_scheme_raises(self, patched_client):
        with pytest.raises(ValueError, match="Unknown attachment scheme"):
            patched_client.draft_email(
                to=["a@example.com"], subject="Hi", body="b",
                attachments=["ftp:bogus.pdf"],
            )

    def test_draft_with_shared_attachment(
        self, patched_client, mock_service, tmp_shared_dir,
    ):
        (tmp_shared_dir / "handoff.pdf").write_bytes(b"%PDF-1.4 from notion")
        _chain(mock_service, "users", "drafts", "create",
               execute_return={"id": "draft-shared"})
        out = patched_client.draft_email(
            to=["a@example.com"], subject="Hi", body="See attached",
            attachments=["shared:handoff.pdf"],
        )
        assert "handoff.pdf" in out
        body = mock_service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        assert b"handoff.pdf" in raw

    def test_shared_attachment_missing_raises(
        self, patched_client, tmp_shared_dir,
    ):
        with pytest.raises(ValueError, match="not found"):
            patched_client.draft_email(
                to=["a@example.com"], subject="Hi", body="b",
                attachments=["shared:ghost.pdf"],
            )

    def test_shared_attachment_rejects_path_components(
        self, patched_client, tmp_shared_dir,
    ):
        with pytest.raises(ValueError, match="bare filename"):
            patched_client.draft_email(
                to=["a@example.com"], subject="Hi", body="b",
                attachments=["shared:../etc/passwd"],
            )


class TestSendEmail:
    def test_basic_send(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "send",
               execute_return={"id": "sent-1"})
        out = patched_client.send_email(
            to=["a@example.com"], subject="Hi", body="Hello"
        )
        assert "sent-1" in out

    def test_reply_threads(self, patched_client, mock_service):
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "threadId": "thread-abc",
            "payload": {"headers": [{"name": "Message-ID", "value": "<orig@example.com>"}]},
        }
        _chain(mock_service, "users", "messages", "send",
               execute_return={"id": "sent-2"})

        patched_client.send_email(
            to=["a@example.com"], subject="Re: Hi", body="reply",
            reply_to_message_id="orig-msg-id",
        )

        send_call = mock_service.users.return_value.messages.return_value.send
        body = send_call.call_args.kwargs["body"]
        assert body["threadId"] == "thread-abc"
        raw = base64.urlsafe_b64decode(body["raw"])
        assert b"In-Reply-To: <orig@example.com>" in raw


# ===================================================================
# upload_file
# ===================================================================

class TestUploadFile:
    def test_writes_to_uploads_dir(self, patched_client, tmp_attachments_dir):
        content = base64.b64encode(b"hello world").decode()
        out = patched_client.upload_file("hello.txt", content)
        saved = tmp_attachments_dir / "uploads" / "hello.txt"
        assert saved.read_bytes() == b"hello world"
        # The returned reference must be directly usable as an
        # attachments= entry, scheme included.
        assert "local:uploads/hello.txt" in out

    def test_returns_sha256(self, patched_client, tmp_attachments_dir):
        import hashlib
        payload = b"hash me"
        out = patched_client.upload_file(
            "h.bin", base64.b64encode(payload).decode(),
        )
        assert hashlib.sha256(payload).hexdigest() in out

    def test_collision_auto_renames(self, patched_client, tmp_attachments_dir):
        c1 = base64.b64encode(b"first").decode()
        c2 = base64.b64encode(b"second").decode()
        patched_client.upload_file("dup.txt", c1)
        out = patched_client.upload_file("dup.txt", c2)
        assert "dup-2.txt" in out
        assert (tmp_attachments_dir / "uploads" / "dup.txt").read_bytes() == b"first"
        assert (tmp_attachments_dir / "uploads" / "dup-2.txt").read_bytes() == b"second"

    def test_rejects_path_separators(self, patched_client):
        content = base64.b64encode(b"x").decode()
        with pytest.raises(ValueError, match="bare filename"):
            patched_client.upload_file("sub/dir/file.txt", content)

    def test_rejects_dotfile(self, patched_client):
        content = base64.b64encode(b"x").decode()
        with pytest.raises(ValueError, match="bare filename"):
            patched_client.upload_file(".ssh", content)

    def test_rejects_bad_base64(self, patched_client):
        with pytest.raises(ValueError, match="Invalid base64"):
            patched_client.upload_file("x.txt", "!!!not-base64!!!")


# ===================================================================
# list_attachments / download_attachment
# ===================================================================

class TestListAttachments:
    def test_lists_with_metadata(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-1",
                   "payload": {"parts": [
                       {"filename": "doc.pdf", "mimeType": "application/pdf",
                        "body": {"size": 123, "attachmentId": "a1"}},
                   ]},
               })
        out = patched_client.list_attachments("msg-1")
        assert "doc.pdf" in out
        assert "application/pdf" in out
        assert "123" in out

    def test_none_found(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return={"id": "msg-1", "payload": {}})
        out = patched_client.list_attachments("msg-1")
        assert "No attachments" in out


class TestDownloadAttachment:
    def test_saves_to_disk(self, patched_client, mock_service, tmp_attachments_dir):
        # First the full message is fetched to find the attachment metadata...
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-1",
                   "payload": {"parts": [
                       {"filename": "doc.txt", "mimeType": "text/plain",
                        "body": {"size": 5, "attachmentId": "a1"}},
                   ]},
               })
        # ...then the raw attachment bytes are fetched separately.
        data_b64 = base64.urlsafe_b64encode(b"hello").decode()
        _chain(mock_service, "users", "messages", "attachments", "get",
               execute_return={"data": data_b64})

        out = patched_client.download_attachment("msg-1", "doc.txt")
        saved = tmp_attachments_dir / "msg-1" / "doc.txt"
        assert saved.read_bytes() == b"hello"
        assert "Downloaded attachment" in out

    def test_returns_base64_when_requested(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-1",
                   "payload": {"parts": [
                       {"filename": "x.bin", "mimeType": "application/octet-stream",
                        "body": {"size": 3, "attachmentId": "a1"}},
                   ]},
               })
        data_b64 = base64.urlsafe_b64encode(b"ABC").decode()
        _chain(mock_service, "users", "messages", "attachments", "get",
               execute_return={"data": data_b64})

        out = patched_client.download_attachment("msg-1", "x.bin", return_base64=True)
        assert "Base64" in out
        # The standard b64 of b"ABC" should be somewhere in the output.
        assert base64.standard_b64encode(b"ABC").decode() in out

    def test_missing_attachment(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return={"id": "msg-1", "payload": {"parts": []}})
        out = patched_client.download_attachment("msg-1", "ghost.pdf")
        assert "not found" in out

    def test_saves_to_shared_destination(
        self, patched_client, mock_service, tmp_shared_dir, tmp_attachments_dir,
    ):
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-9",
                   "payload": {"parts": [
                       {"filename": "invoice.pdf", "mimeType": "application/pdf",
                        "body": {"size": 12, "attachmentId": "a9"}},
                   ]},
               })
        data_b64 = base64.urlsafe_b64encode(b"pdf-payload!").decode()
        _chain(mock_service, "users", "messages", "attachments", "get",
               execute_return={"data": data_b64})

        out = patched_client.download_attachment(
            "msg-9", "invoice.pdf", destination="shared",
        )
        shared_file = tmp_shared_dir / "invoice.pdf"
        assert shared_file.read_bytes() == b"pdf-payload!"
        # Not written to the private namespaced path.
        assert not (tmp_attachments_dir / "msg-9" / "invoice.pdf").exists()
        assert "shared:invoice.pdf" in out

    def test_destination_invalid_raises(self, patched_client):
        with pytest.raises(ValueError, match="destination must be"):
            patched_client.download_attachment(
                "msg-1", "x.pdf", destination="elsewhere",
            )

    def test_rejects_filename_with_path(self, patched_client):
        with pytest.raises(ValueError, match="bare filename"):
            patched_client.download_attachment("msg-1", "../evil.pdf")

    def test_shared_clash_autorenames(
        self, patched_client, mock_service, tmp_shared_dir,
    ):
        # A file with the same name already exists in the shared mount.
        (tmp_shared_dir / "invoice.pdf").write_bytes(b"existing-payload")

        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-10",
                   "payload": {"parts": [
                       {"filename": "invoice.pdf", "mimeType": "application/pdf",
                        "body": {"size": 7, "attachmentId": "a10"}},
                   ]},
               })
        data_b64 = base64.urlsafe_b64encode(b"NEW-PDF").decode()
        _chain(mock_service, "users", "messages", "attachments", "get",
               execute_return={"data": data_b64})

        out = patched_client.download_attachment(
            "msg-10", "invoice.pdf", destination="shared",
        )
        # Original is untouched; new one landed at invoice-2.pdf.
        assert (tmp_shared_dir / "invoice.pdf").read_bytes() == b"existing-payload"
        assert (tmp_shared_dir / "invoice-2.pdf").read_bytes() == b"NEW-PDF"
        assert "invoice-2.pdf" in out
        assert "shared:invoice-2.pdf" in out
        assert "already existed" in out

    def test_shared_clash_increments_suffix(
        self, patched_client, mock_service, tmp_shared_dir,
    ):
        (tmp_shared_dir / "x.txt").write_bytes(b"a")
        (tmp_shared_dir / "x-2.txt").write_bytes(b"b")
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-11",
                   "payload": {"parts": [
                       {"filename": "x.txt", "mimeType": "text/plain",
                        "body": {"size": 1, "attachmentId": "a11"}},
                   ]},
               })
        data_b64 = base64.urlsafe_b64encode(b"c").decode()
        _chain(mock_service, "users", "messages", "attachments", "get",
               execute_return={"data": data_b64})

        patched_client.download_attachment(
            "msg-11", "x.txt", destination="shared",
        )
        assert (tmp_shared_dir / "x-3.txt").read_bytes() == b"c"


class TestPurgeSharedFiles:
    def test_deletes_only_old_files(self, client, tmp_shared_dir):
        import os as _os
        import time as _time
        now = _time.time()
        old = tmp_shared_dir / "old.pdf"
        new = tmp_shared_dir / "new.pdf"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        # Force old mtime: 48h ago.
        _os.utime(old, (now - 48 * 3600, now - 48 * 3600))

        out = client.purge_shared_files(max_age_hours=24)
        assert not old.exists()
        assert new.exists()
        assert "old.pdf" in out
        assert "Deleted" in out
        assert "new.pdf" not in out

    def test_dry_run_does_not_delete(self, client, tmp_shared_dir):
        import os as _os
        import time as _time
        old = tmp_shared_dir / "stale.pdf"
        old.write_bytes(b"x")
        _os.utime(old, (_time.time() - 48 * 3600,) * 2)

        out = client.purge_shared_files(max_age_hours=24, dry_run=True)
        assert old.exists()
        assert "Would delete" in out
        assert "stale.pdf" in out

    def test_nothing_to_purge(self, client, tmp_shared_dir):
        (tmp_shared_dir / "fresh.pdf").write_bytes(b"y")
        out = client.purge_shared_files(max_age_hours=24)
        assert "No files older" in out

    def test_rejects_negative_age(self, client):
        with pytest.raises(ValueError, match=">="):
            client.purge_shared_files(max_age_hours=-1)

    def test_leaves_subdirectories_alone(self, client, tmp_shared_dir):
        import os as _os
        import time as _time
        sub = tmp_shared_dir / "subdir"
        sub.mkdir()
        (sub / "inner.txt").write_bytes(b"x")
        _os.utime(sub, (_time.time() - 48 * 3600,) * 2)
        out = client.purge_shared_files(max_age_hours=24)
        assert sub.exists()
        assert (sub / "inner.txt").exists()
        assert "No files older" in out


# ===================================================================
# Drive tools
# ===================================================================

class TestDriveList:
    def test_lists_files(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "list",
               execute_return={"files": [
                   {"id": "f1", "name": "a.txt", "size": "10",
                    "mimeType": "text/plain", "modifiedTime": "2026-04-01T00:00:00Z"},
                   {"id": "f2", "name": "b.pdf", "size": "20",
                    "mimeType": "application/pdf", "modifiedTime": "2026-04-02T00:00:00Z"},
               ]})
        out = patched_client.drive_list()
        assert "a.txt" in out
        assert "b.pdf" in out
        assert "2 files" in out

    def test_filter(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "list",
               execute_return={"files": [
                   {"id": "f1", "name": "report.pdf", "size": "10",
                    "mimeType": "application/pdf", "modifiedTime": "2026-04-01T00:00:00Z"},
                   {"id": "f2", "name": "notes.txt", "size": "20",
                    "mimeType": "text/plain", "modifiedTime": "2026-04-02T00:00:00Z"},
               ]})
        out = patched_client.drive_list("report")
        assert "report.pdf" in out
        assert "notes.txt" not in out

    def test_empty(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "list", execute_return={"files": []})
        out = patched_client.drive_list()
        assert "empty" in out


class TestDriveUpload:
    def test_uploads_to_folder(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "create",
               execute_return={
                   "id": "file-id-1", "name": "doc.pdf",
                   "size": "9", "mimeType": "application/pdf",
                   "webViewLink": "https://drive.example/doc.pdf",
               })
        content_b64 = base64.b64encode(b"pdf-bytes").decode()
        out = patched_client.drive_upload("doc.pdf", content_b64)
        assert "file-id-1" in out
        assert "doc.pdf" in out
        create_call = mock_drive_service.files.return_value.create
        meta = create_call.call_args.kwargs["body"]
        assert meta["name"] == "doc.pdf"
        assert meta["parents"] == ["folder-123"]

    def test_rejects_bad_filename(self, patched_client):
        content_b64 = base64.b64encode(b"x").decode()
        with pytest.raises(ValueError, match="bare filename"):
            patched_client.drive_upload("../evil.pdf", content_b64)

    def test_requires_exactly_one_source(self, patched_client):
        with pytest.raises(ValueError, match="exactly one"):
            patched_client.drive_upload("doc.pdf")
        with pytest.raises(ValueError, match="exactly one"):
            patched_client.drive_upload(
                "doc.pdf", content_base64="AAA", local_filename="doc.pdf",
            )

    def test_local_filename_from_shared(
        self, patched_client, mock_drive_service, tmp_shared_dir,
    ):
        (tmp_shared_dir / "report.pdf").write_bytes(b"SHARED-BYTES")
        _chain(mock_drive_service, "files", "create",
               execute_return={
                   "id": "f-shared-1", "name": "report.pdf",
                   "size": "12", "mimeType": "application/pdf",
                   "webViewLink": "https://drive.example/r",
               })
        out = patched_client.drive_upload(
            "report.pdf", local_filename="report.pdf",
        )
        assert "f-shared-1" in out
        # Verify the actual bytes from shared dir made it into the upload.
        media = mock_drive_service.files.return_value.create.call_args.kwargs[
            "media_body"
        ]
        assert media.getbytes(0, media.size()) == b"SHARED-BYTES"

    def test_local_filename_from_attachments(
        self, patched_client, mock_drive_service, tmp_attachments_dir,
    ):
        nested = tmp_attachments_dir / "msg-7"
        nested.mkdir()
        (nested / "doc.pdf").write_bytes(b"PRIVATE-BYTES")
        _chain(mock_drive_service, "files", "create",
               execute_return={
                   "id": "f-priv-1", "name": "doc.pdf",
                   "size": "13", "mimeType": "application/pdf",
                   "webViewLink": "https://drive.example/p",
               })
        out = patched_client.drive_upload(
            "doc.pdf", local_filename="msg-7/doc.pdf",
        )
        assert "f-priv-1" in out
        media = mock_drive_service.files.return_value.create.call_args.kwargs[
            "media_body"
        ]
        assert media.getbytes(0, media.size()) == b"PRIVATE-BYTES"

    def test_local_filename_missing_raises(self, patched_client):
        with pytest.raises(ValueError, match="not found"):
            patched_client.drive_upload("x.pdf", local_filename="missing.pdf")

    def test_local_filename_traversal_rejected(
        self, patched_client, tmp_path, tmp_shared_dir,
    ):
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"evil")
        with pytest.raises(ValueError, match="not found"):
            patched_client.drive_upload(
                "outside.pdf", local_filename="../outside.pdf",
            )


class TestResolveDriveFile:
    def test_finds_by_name(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "list",
               execute_return={"files": [
                   {"id": "f1", "name": "a.txt", "mimeType": "text/plain", "size": "5"},
               ]})
        file_id, name, mime, size = patched_client._resolve_drive_file("a.txt")
        assert file_id == "f1"
        assert name == "a.txt"

    def test_ambiguous_name_raises(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "list",
               execute_return={"files": [
                   {"id": "f1", "name": "x", "mimeType": "text/plain", "size": "1"},
                   {"id": "f2", "name": "x", "mimeType": "text/plain", "size": "1"},
               ]})
        with pytest.raises(ValueError, match="Ambiguous"):
            patched_client._resolve_drive_file("x")

    def test_falls_back_to_id_lookup(self, patched_client, mock_drive_service):
        # Name lookup returns nothing, then get-by-id succeeds and the file's
        # parent is the folder.
        _chain(mock_drive_service, "files", "list", execute_return={"files": []})
        _chain(mock_drive_service, "files", "get",
               execute_return={"id": "file-id-42", "name": "doc.pdf",
                               "mimeType": "application/pdf", "size": "100",
                               "parents": ["folder-123"]})
        file_id, name, mime, size = patched_client._resolve_drive_file("file-id-42")
        assert file_id == "file-id-42"
        assert name == "doc.pdf"

    def test_id_outside_folder_raises(self, patched_client, mock_drive_service):
        _chain(mock_drive_service, "files", "list", execute_return={"files": []})
        _chain(mock_drive_service, "files", "get",
               execute_return={"id": "x", "name": "y", "mimeType": "text/plain",
                               "size": "1", "parents": ["different-folder"]})
        with pytest.raises(ValueError, match="not inside folder"):
            patched_client._resolve_drive_file("x")

    def test_empty_input_raises(self, patched_client):
        with pytest.raises(ValueError, match="name or ID required"):
            patched_client._resolve_drive_file("")


class TestFetchDriveBytes:
    def test_refuses_google_native(self, patched_client):
        with pytest.raises(ValueError, match="Google-native"):
            patched_client._fetch_drive_bytes(
                "file-id", "application/vnd.google-apps.document"
            )


# ===================================================================
# list_attachments exclude_inline
# ===================================================================

def _part(filename, mime="application/pdf", size=10, aid="a1", headers=None):
    return {
        "filename": filename,
        "mimeType": mime,
        "body": {"size": size, "attachmentId": aid},
        "headers": headers or [],
    }


class TestListAttachmentsExcludeInline:
    def test_excludes_inline_parts(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-inl",
                   "payload": {"parts": [
                       _part("invoice.pdf",
                             headers=[{"name": "Content-Disposition",
                                       "value": "attachment; filename=invoice.pdf"}]),
                       _part("logo.png", mime="image/png", aid="a2",
                             headers=[{"name": "Content-Disposition",
                                       "value": "inline"},
                                      {"name": "Content-ID", "value": "<logo@x>"}]),
                   ]},
               })
        out = patched_client.list_attachments(
            "msg-inl", exclude_inline=True,
        )
        assert "invoice.pdf" in out
        assert "logo.png" not in out
        assert "disposition=attachment" in out

    def test_shows_disposition_by_default(self, patched_client, mock_service):
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-d",
                   "payload": {"parts": [
                       _part("pixel.gif", mime="image/gif",
                             headers=[{"name": "Content-Disposition",
                                       "value": "inline"}]),
                   ]},
               })
        out = patched_client.list_attachments("msg-d")
        assert "pixel.gif" in out
        assert "disposition=inline" in out


# ===================================================================
# batch_list_attachments
# ===================================================================

class TestBatchListAttachments:
    def test_collapses_many_messages(self, patched_client, mock_service):
        # Configure a side_effect that dispatches per message id.
        responses = {
            "m-a": {"id": "m-a", "payload": {"parts": [
                _part("a.pdf", headers=[{"name": "Content-Disposition",
                                         "value": "attachment"}])]}},
            "m-b": {"id": "m-b", "payload": {}},
            "m-c": {"id": "m-c", "payload": {"parts": [
                _part("c1.pdf", aid="c1",
                      headers=[{"name": "Content-Disposition",
                                "value": "attachment"}]),
                _part("c2.png", mime="image/png", aid="c2",
                      headers=[{"name": "Content-Disposition",
                                "value": "inline"}]),
            ]}},
        }
        get_mock = mock_service.users.return_value.messages.return_value.get
        get_mock.return_value.execute.side_effect = (
            lambda: responses[get_mock.call_args.kwargs["id"]]
        )

        import json as _json
        out = patched_client.batch_list_attachments(
            ["m-a", "m-b", "m-c"],
        )
        # Parse the JSON portion out of the prose header.
        body = out[out.index("{"):]
        data = _json.loads(body)
        assert len(data["m-a"]) == 1
        assert data["m-a"][0]["filename"] == "a.pdf"
        assert data["m-b"] == []
        assert len(data["m-c"]) == 2
        assert "2/3 messages with matching attachments" in out

    def test_exclude_inline_filters_per_message(
        self, patched_client, mock_service,
    ):
        responses = {
            "m-x": {"id": "m-x", "payload": {"parts": [
                _part("pixel.gif",
                      headers=[{"name": "Content-Disposition",
                                "value": "inline"}]),
            ]}},
        }
        get_mock = mock_service.users.return_value.messages.return_value.get
        get_mock.return_value.execute.side_effect = (
            lambda: responses[get_mock.call_args.kwargs["id"]]
        )

        import json as _json
        out = patched_client.batch_list_attachments(
            ["m-x"], exclude_inline=True,
        )
        data = _json.loads(out[out.index("{"):])
        assert data["m-x"] == []

    def test_rejects_empty_list(self, patched_client):
        with pytest.raises(ValueError, match="non-empty"):
            patched_client.batch_list_attachments([])


# ===================================================================
# search_email message_ids pre-filter
# ===================================================================

class TestSearchEmailPreFilter:
    def test_keeps_only_ids_with_attachment(
        self, patched_client, mock_service,
    ):
        full = {
            "m1": {
                "id": "m1",
                "labelIds": ["INBOX"],
                "snippet": "hi",
                "payload": {
                    "headers": [
                        {"name": "Date", "value": "D"},
                        {"name": "From", "value": "a@b"},
                        {"name": "Subject", "value": "S"},
                    ],
                    "parts": [
                        _part("invoice.pdf",
                              headers=[{"name": "Content-Disposition",
                                        "value": "attachment"}]),
                    ],
                },
            },
            "m2": {
                "id": "m2",
                "labelIds": ["INBOX"],
                "snippet": "",
                "payload": {
                    "headers": [
                        {"name": "Date", "value": "D"},
                        {"name": "From", "value": "a@b"},
                        {"name": "Subject", "value": "S"},
                    ],
                },
            },
        }
        get_mock = mock_service.users.return_value.messages.return_value.get
        # Two call points consume id= to dispatch in side_effect.
        get_mock.return_value.execute.side_effect = (
            lambda: full[get_mock.call_args.kwargs["id"]]
        )

        out = patched_client.search_email(
            "has:attachment",
            message_ids=["m1", "m2"],
        )
        assert "m1" in out
        # The non-matching one should be filtered out.
        assert "ID: m2" not in out


# ===================================================================
# download_attachment prefix + atomic safety
# ===================================================================

class TestDownloadAttachmentPrefix:
    def test_prefix_is_applied(
        self, patched_client, mock_service, tmp_shared_dir,
    ):
        _chain(mock_service, "users", "messages", "get",
               execute_return={
                   "id": "msg-p",
                   "payload": {"parts": [_part("bill.pdf", aid="ap")]},
               })
        _chain(mock_service, "users", "messages", "attachments", "get",
               execute_return={
                   "data": base64.urlsafe_b64encode(b"PDF").decode()
               })
        patched_client.download_attachment(
            "msg-p", "bill.pdf",
            destination="shared", prefix="msg-p_",
        )
        assert (tmp_shared_dir / "msg-p_bill.pdf").read_bytes() == b"PDF"

    def test_prefix_rejects_path_components(self, patched_client):
        with pytest.raises(ValueError, match="Invalid prefix"):
            patched_client.download_attachment(
                "m", "x.pdf", destination="shared", prefix="a/b",
            )


# ===================================================================
# _atomic_create
# ===================================================================

class TestAtomicCreate:
    def test_writes_base_filename(self, client, tmp_shared_dir):
        path = client._atomic_create(tmp_shared_dir, "a.txt", b"payload")
        assert path.name == "a.txt"
        assert path.read_bytes() == b"payload"

    def test_collision_increments(self, client, tmp_shared_dir):
        (tmp_shared_dir / "a.txt").write_bytes(b"old")
        path = client._atomic_create(tmp_shared_dir, "a.txt", b"new")
        assert path.name == "a-2.txt"
        assert path.read_bytes() == b"new"
        # Original untouched.
        assert (tmp_shared_dir / "a.txt").read_bytes() == b"old"

    def test_two_racing_writers_get_distinct_files(
        self, client, tmp_shared_dir,
    ):
        p1 = client._atomic_create(tmp_shared_dir, "r.bin", b"first")
        p2 = client._atomic_create(tmp_shared_dir, "r.bin", b"second")
        assert p1 != p2
        assert p1.read_bytes() == b"first"
        assert p2.read_bytes() == b"second"


# ===================================================================
# list_shared_files / purge_file
# ===================================================================

class TestListSharedFiles:
    def test_lists_files(self, client, tmp_shared_dir):
        import hashlib
        (tmp_shared_dir / "a.pdf").write_bytes(b"x")
        (tmp_shared_dir / "b.txt").write_bytes(b"yy")
        out = client.list_shared_files()
        assert "a.pdf" in out
        assert "b.txt" in out
        assert "[shared:a.pdf]" in out
        assert hashlib.sha256(b"x").hexdigest() in out

    def test_filter_substring(self, client, tmp_shared_dir):
        (tmp_shared_dir / "invoice.pdf").write_bytes(b"x")
        (tmp_shared_dir / "contract.docx").write_bytes(b"y")
        out = client.list_shared_files(filter="invoice")
        assert "invoice.pdf" in out
        assert "contract.docx" not in out

    def test_empty_mount(self, client, tmp_shared_dir):
        out = client.list_shared_files()
        assert "empty" in out


class TestPurgeFile:
    def test_deletes_target_file(self, client, tmp_shared_dir):
        p = tmp_shared_dir / "a.pdf"
        p.write_bytes(b"bye")
        out = client.purge_file("a.pdf")
        assert not p.exists()
        assert "Deleted a.pdf" in out

    def test_missing_file_is_reported(self, client, tmp_shared_dir):
        out = client.purge_file("ghost.pdf")
        assert "No such file" in out

    def test_rejects_path_traversal(self, client, tmp_shared_dir):
        with pytest.raises(ValueError, match="bare filename"):
            client.purge_file("../secrets.txt")


# ===================================================================
# extract_attachment_text / render_attachment_page
# ===================================================================

import json as _json_mod
from pathlib import Path as _Path

_FIXTURE_PDF = _Path(__file__).parent / "fixtures" / "sample.pdf"


def _mock_pdf_attachment(mock_service, msg_id: str, filename: str, pdf_bytes: bytes):
    """Configure the mock Gmail service to return a single PDF attachment
    for ``msg_id``. Two chains: messages.get returns the payload listing
    the attachment; messages.attachments.get returns the raw bytes."""
    _chain(
        mock_service, "users", "messages", "get",
        execute_return={
            "id": msg_id,
            "payload": {"parts": [
                {"filename": filename, "mimeType": "application/pdf",
                 "body": {"size": len(pdf_bytes), "attachmentId": "att-1"}},
            ]},
        },
    )
    data_b64 = base64.urlsafe_b64encode(pdf_bytes).decode()
    _chain(
        mock_service, "users", "messages", "attachments", "get",
        execute_return={"data": data_b64},
    )


class TestExtractAttachmentText:
    def test_text_mode_returns_both_pages(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text("msg-pdf", "sample.pdf")
        data = _json_mod.loads(out)
        assert data["page_count"] == 2
        assert data["pages_returned"] == [1, 2]
        assert data["mode"] == "text"
        assert data["truncated"] is False
        assert "Invoice total: $42.50" in data["text"]
        assert "Reference: INV-00042" in data["text"]

    def test_pages_range_selects_subset(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text(
            "msg-pdf", "sample.pdf", pages="2",
        )
        data = _json_mod.loads(out)
        assert data["pages_returned"] == [2]
        # Page 1 content must NOT leak through.
        assert "Invoice total" not in data["text"]
        assert "Reference: INV-00042" in data["text"]

    def test_pages_comma_list(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text(
            "msg-pdf", "sample.pdf", pages="1,2",
        )
        data = _json_mod.loads(out)
        assert data["pages_returned"] == [1, 2]

    def test_pages_range_hyphen(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text(
            "msg-pdf", "sample.pdf", pages="1-2",
        )
        data = _json_mod.loads(out)
        assert data["pages_returned"] == [1, 2]

    def test_out_of_range_pages_silently_dropped(
        self, patched_client, mock_service,
    ):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text(
            "msg-pdf", "sample.pdf", pages="2,99",
        )
        data = _json_mod.loads(out)
        assert data["pages_returned"] == [2]
        assert data["page_count"] == 2

    def test_invalid_pages_raises(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        with pytest.raises(ValueError, match="Invalid page"):
            patched_client.extract_attachment_text(
                "msg-pdf", "sample.pdf", pages="abc",
            )

    def test_invalid_mode_raises(self, patched_client):
        with pytest.raises(ValueError, match="mode must be one of"):
            patched_client.extract_attachment_text(
                "msg-pdf", "sample.pdf", mode="nope",
            )

    def test_size_ceiling_truncates(
        self, patched_client, mock_service, monkeypatch,
    ):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        # Shrink the ceiling so the 2-page fixture trips it on page 2.
        monkeypatch.setattr(
            patched_client, "EXTRACT_TEXT_MAX_BYTES", 50,
        )
        out = patched_client.extract_attachment_text("msg-pdf", "sample.pdf")
        data = _json_mod.loads(out)
        assert data["truncated"] is True
        # "truncated" marker is present in the text
        assert "truncated" in data["text"]
        # The page 2 body did NOT come through — only page 1 fit
        assert data["pages_returned"] == [1]

    def test_non_pdf_rejected(self, patched_client, mock_service):
        # Send a text/plain attachment through the same path — must be
        # rejected with an "error" key in the response.
        _chain(
            mock_service, "users", "messages", "get",
            execute_return={
                "id": "msg-text",
                "payload": {"parts": [
                    {"filename": "notes.txt", "mimeType": "text/plain",
                     "body": {"size": 4, "attachmentId": "att-2"}},
                ]},
            },
        )
        _chain(
            mock_service, "users", "messages", "attachments", "get",
            execute_return={"data": base64.urlsafe_b64encode(b"hi!\n").decode()},
        )
        out = patched_client.extract_attachment_text(
            "msg-text", "notes.txt",
        )
        data = _json_mod.loads(out)
        assert "error" in data
        assert "only supports PDFs" in data["error"]

    def test_missing_attachment_error(self, patched_client, mock_service):
        _chain(
            mock_service, "users", "messages", "get",
            execute_return={"id": "msg-x", "payload": {"parts": []}},
        )
        out = patched_client.extract_attachment_text("msg-x", "ghost.pdf")
        data = _json_mod.loads(out)
        assert "error" in data
        assert "not found" in data["error"]

    def test_tables_mode_runs(self, patched_client, mock_service):
        # The fixture has no real tables, but tables mode should still
        # return a well-formed JSON response with empty per-page text.
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text(
            "msg-pdf", "sample.pdf", mode="tables",
        )
        data = _json_mod.loads(out)
        assert data["mode"] == "tables"
        assert data["pages_returned"] == [1, 2]


class TestRenderAttachmentPage:
    def test_returns_jpeg_base64_for_page_1(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.render_attachment_page(
            "msg-pdf", "sample.pdf", page=1,
        )
        data = _json_mod.loads(out)
        assert data["mime_type"] == "image/jpeg"
        assert data["page"] == 1
        assert data["page_count"] == 2
        # Width should be <= the default max_width of 1200
        assert data["width"] <= 1200
        # Base64 decodes to a JPEG — magic bytes are FF D8 FF
        img_bytes = base64.standard_b64decode(data["image_base64"])
        assert img_bytes[:3] == b"\xff\xd8\xff"

    def test_png_format(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.render_attachment_page(
            "msg-pdf", "sample.pdf", page=1, format="png",
        )
        data = _json_mod.loads(out)
        assert data["mime_type"] == "image/png"
        img_bytes = base64.standard_b64decode(data["image_base64"])
        # PNG magic: 89 50 4E 47 0D 0A 1A 0A
        assert img_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    def test_respects_max_width(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.render_attachment_page(
            "msg-pdf", "sample.pdf", page=1, max_width=400,
        )
        data = _json_mod.loads(out)
        assert data["width"] <= 400
        # And height scales in proportion (aspect ratio is preserved)
        assert data["height"] > 0

    def test_page_out_of_range(self, patched_client, mock_service):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        out = patched_client.render_attachment_page(
            "msg-pdf", "sample.pdf", page=99,
        )
        data = _json_mod.loads(out)
        assert "error" in data
        assert "out of range" in data["error"]
        assert data["page_count"] == 2

    def test_invalid_page_value(self, patched_client):
        with pytest.raises(ValueError, match="1-indexed"):
            patched_client.render_attachment_page(
                "msg-pdf", "sample.pdf", page=0,
            )

    def test_invalid_format(self, patched_client):
        with pytest.raises(ValueError, match="format must be"):
            patched_client.render_attachment_page(
                "msg-pdf", "sample.pdf", page=1, format="gif",
            )

    def test_invalid_quality(self, patched_client):
        with pytest.raises(ValueError, match="quality must be"):
            patched_client.render_attachment_page(
                "msg-pdf", "sample.pdf", page=1, quality=200,
            )

    def test_invalid_max_width(self, patched_client):
        with pytest.raises(ValueError, match="max_width must be"):
            patched_client.render_attachment_page(
                "msg-pdf", "sample.pdf", page=1, max_width=10,
            )

    def test_non_pdf_rejected(self, patched_client, mock_service):
        _chain(
            mock_service, "users", "messages", "get",
            execute_return={
                "id": "msg-t",
                "payload": {"parts": [
                    {"filename": "notes.txt", "mimeType": "text/plain",
                     "body": {"size": 3, "attachmentId": "att-t"}},
                ]},
            },
        )
        _chain(
            mock_service, "users", "messages", "attachments", "get",
            execute_return={"data": base64.urlsafe_b64encode(b"hi!").decode()},
        )
        out = patched_client.render_attachment_page(
            "msg-t", "notes.txt", page=1,
        )
        data = _json_mod.loads(out)
        assert "error" in data
        assert "only supports PDFs" in data["error"]

    def test_missing_attachment_error(self, patched_client, mock_service):
        _chain(
            mock_service, "users", "messages", "get",
            execute_return={"id": "msg-x", "payload": {"parts": []}},
        )
        out = patched_client.render_attachment_page(
            "msg-x", "ghost.pdf", page=1,
        )
        data = _json_mod.loads(out)
        assert "error" in data
        assert "not found" in data["error"]


# ===================================================================
# Duplicate-filename attachment addressing (index parameter)
# ===================================================================

def _msg_with_dup_attachments():
    """A message carrying two attachments with the SAME filename but
    distinct attachmentIds — models e.g. a vendor sending a receipt and an
    itemised invoice both named 'Invoice.pdf'."""
    return {
        "id": "m-dup", "threadId": "t-dup", "labelIds": ["INBOX"], "snippet": "",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Two invoices"}],
            "parts": [
                {"filename": "Invoice.pdf", "mimeType": "application/pdf",
                 "body": {"attachmentId": "att-A", "size": 11}},
                {"filename": "Invoice.pdf", "mimeType": "application/pdf",
                 "body": {"attachmentId": "att-B", "size": 22}},
            ],
        },
    }


class TestDuplicateFilenameIndex:
    def _wire(self, mock_service, data_by_id):
        # message fetch
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = _msg_with_dup_attachments()
        # attachment fetch — return bytes keyed by the requested id
        def get_side_effect(userId, messageId, id):
            node = MagicMock()
            node.execute.return_value = {
                "data": base64.urlsafe_b64encode(data_by_id[id]).decode()
            }
            return node
        mock_service.users.return_value.messages.return_value.attachments.return_value.get.side_effect = get_side_effect

    def test_index0_is_first_match(self, patched_client, mock_service):
        self._wire(mock_service, {"att-A": b"FIRST", "att-B": b"SECOND"})
        acct, data, mime = patched_client._fetch_attachment_bytes("m-dup", "Invoice.pdf")
        assert data == b"FIRST"

    def test_index1_selects_second_match(self, patched_client, mock_service):
        self._wire(mock_service, {"att-A": b"FIRST", "att-B": b"SECOND"})
        acct, data, mime = patched_client._fetch_attachment_bytes("m-dup", "Invoice.pdf", index=1)
        assert data == b"SECOND"

    def test_index_out_of_range_raises(self, patched_client, mock_service):
        self._wire(mock_service, {"att-A": b"FIRST", "att-B": b"SECOND"})
        with pytest.raises(FileNotFoundError, match="index 2 out of range"):
            patched_client._fetch_attachment_bytes("m-dup", "Invoice.pdf", index=2)

    def test_missing_filename_still_raises(self, patched_client, mock_service):
        self._wire(mock_service, {"att-A": b"FIRST", "att-B": b"SECOND"})
        with pytest.raises(FileNotFoundError, match="not found"):
            patched_client._fetch_attachment_bytes("m-dup", "Nope.pdf")

    def test_download_passes_index(self, patched_client, mock_service, tmp_attachments_dir):
        self._wire(mock_service, {"att-A": b"FIRST", "att-B": b"SECOND"})
        out = patched_client.download_attachment("m-dup", "Invoice.pdf", index=1)
        # the saved file should contain the SECOND attachment's bytes
        import os
        saved = os.path.join(str(tmp_attachments_dir), "m-dup", "Invoice.pdf")
        assert os.path.exists(saved)
        assert open(saved, "rb").read() == b"SECOND"

    def test_list_attachments_flags_duplicates(self, patched_client, mock_service):
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = _msg_with_dup_attachments()
        out = patched_client.list_attachments("m-dup")
        assert "index=0" in out and "index=1" in out
        assert "duplicate filename" in out


# ===================================================================
# OCR plumbing through extract_attachment_text / render raw variant
# (OCR semantics themselves are covered in test_pdf_read.py)
# ===================================================================

_SCANNED_PDF = _Path(__file__).parent / "fixtures" / "scanned.pdf"


class TestExtractAttachmentTextOcr:
    def test_invalid_ocr_raises_before_fetch(self, patched_client):
        with pytest.raises(ValueError, match="ocr must be one of"):
            patched_client.extract_attachment_text(
                "msg-pdf", "sample.pdf", ocr="tesseract",
            )

    def test_scanned_attachment_default_auto_ocr(
        self, patched_client, mock_service, monkeypatch,
    ):
        # OCR is delegated to xberg — stub the client boundary; the request
        # shape is covered by test_pdf_read.py's mocked-HTTP suite.
        from mcp_server_kit import pdf as _pdf_read

        monkeypatch.setattr(
            _pdf_read, "xberg_ocr_pages",
            lambda fb, pages, *, vlm=False, concurrency=None: (
                {p: f"Total due 421.50 (page {p})" for p in pages},
                "xberg:tesseract",
            ),
        )
        pdf_bytes = _SCANNED_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-scan", "scan.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text("msg-scan", "scan.pdf")
        data = _json_mod.loads(out)
        assert data["ocr_used"] is True
        assert data["ocr_pages"] == [1, 2]
        assert data["ocr_engine"] == "xberg:tesseract"
        assert "421.50" in data["text"]

    def test_scanned_attachment_ocr_off_stays_empty(
        self, patched_client, mock_service,
    ):
        pdf_bytes = _SCANNED_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-scan", "scan.pdf", pdf_bytes)

        out = patched_client.extract_attachment_text(
            "msg-scan", "scan.pdf", ocr="off",
        )
        data = _json_mod.loads(out)
        assert "ocr_used" not in data
        assert data["text"].strip() == ""


class TestRenderAttachmentPageRaw:
    def test_raw_returns_image_bytes_and_meta(
        self, patched_client, mock_service,
    ):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        raw = patched_client.render_attachment_page_raw(
            "msg-pdf", "sample.pdf", 1,
        )
        assert raw["mime_type"] == "image/jpeg"
        assert raw["image_bytes"][:3] == b"\xff\xd8\xff"
        assert raw["page_count"] == 2
        assert raw["width"] == 1200

    def test_raw_out_of_range_error_dict(
        self, patched_client, mock_service,
    ):
        pdf_bytes = _FIXTURE_PDF.read_bytes()
        _mock_pdf_attachment(mock_service, "msg-pdf", "sample.pdf", pdf_bytes)

        raw = patched_client.render_attachment_page_raw(
            "msg-pdf", "sample.pdf", 99,
        )
        assert "out of range" in raw["error"]
        assert raw["page_count"] == 2

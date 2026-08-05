import base64
import email.utils
import hashlib
import html
import io
import mimetypes
import os
import re
import ssl
import threading
from email.message import EmailMessage
from email.mime.text import MIMEText
from pathlib import Path

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

from google_accounts_mcp import pdf_read
from google_accounts_mcp.auth import TokenStore
from google_accounts_mcp.config import (
    ATTACHMENTS_DIR,
    DB_PATH,
    DEFAULT_ACCOUNT,
    DRIVE_ACCOUNT,
    DRIVE_FOLDER_NAME,
    SHARED_DIR,
)


# Connection-level errors that mean a pooled keepalive socket has gone
# stale rather than a real API failure. ConnectionResetError /
# BrokenPipeError / socket timeouts are OSError subclasses, so OSError
# covers the socket family; ssl.SSLError and httplib2.HttpLib2Error cover
# the TLS/transport layers above it.
_TRANSIENT_HTTP_ERRORS = (OSError, ssl.SSLError, httplib2.HttpLib2Error)

# Bound for a single HTTP request (seconds). The google client caches one
# service object — and its underlying httplib2 connection — per account for
# the life of the process, so without a timeout a wedged socket hangs a
# tool call indefinitely. Chunked downloads/uploads issue many short
# requests, so this caps a single round-trip, not a whole transfer.
_HTTP_TIMEOUT = 60

# Tool handlers now run in a worker threadpool (see server.py), so several
# PDF rasterisations can run at once. Each pypdfium2 render of a high-res page
# transiently holds a large native bitmap; left unbounded, a concurrent burst
# could spike RSS past the container MemoryMax. This semaphore caps concurrent
# rasterisation/parse so peak memory stays predictable. Sequential callers
# (the common case) are unaffected. Tunable via PDF_MAX_CONCURRENCY.
_PDF_CONCURRENCY = threading.Semaphore(
    max(1, int(os.environ.get("PDF_MAX_CONCURRENCY", "4")))
)


class _RetryingHttp(httplib2.Http):
    """``httplib2.Http`` that retries a request once when a pooled
    keepalive socket has gone stale.

    Google closes idle connections server-side, so the first call to an
    account that has been quiet reuses a dead socket and fails with
    "[Errno 104] Connection reset by peer" (or a broken pipe / SSL EOF).
    The retry first drops any pooled connections so httplib2 dials a fresh
    socket. A connection reset means the request never completed, so
    re-sending is safe even for non-idempotent calls. Only connection-level
    errors are retried — HTTP error statuses are returned unchanged for the
    caller to handle.

    Thread safety: a cached service object (and the single httplib2 socket it
    holds) is shared across a session's tool calls. Tool handlers now run in a
    worker threadpool (see ``server.py``), so two calls for the *same* account
    could hit this socket concurrently — and httplib2 is not safe for that.
    A per-instance ``RLock`` serialises requests through one account's socket
    while leaving different accounts (each with their own ``_RetryingHttp``)
    free to run in parallel. ``RLock`` (not ``Lock``) because an
    ``AuthorizedHttp`` token refresh re-enters ``request`` on the same thread.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def request(self, *args, **kwargs):
        with self._lock:
            try:
                return super().request(*args, **kwargs)
            except _TRANSIENT_HTTP_ERRORS:
                for conn in list(self.connections.values()):
                    try:
                        conn.close()
                    except Exception:
                        pass
                self.connections.clear()
                return super().request(*args, **kwargs)


class GmailClient:
    def __init__(self):
        self.store = TokenStore(DB_PATH)
        self._services: dict = {}
        self._drive_service = None
        self._drive_folder_id: str | None = None

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _atomic_create(root: Path, filename: str, data: bytes) -> Path:
        """Write ``data`` to a file under ``root`` whose name does not
        collide with an existing file. Starts with ``filename`` and, on
        collision, appends ``-2``, ``-3``, ... before the extension. The
        create-then-write is atomic (O_CREAT|O_EXCL) so parallel writers
        do not overwrite each other. Callers must ensure ``filename`` is
        a bare filename (no path components)."""
        stem, ext = os.path.splitext(filename)
        root.mkdir(parents=True, exist_ok=True)
        for i in range(0, 1001):
            candidate = root / (filename if i == 0 else f"{stem}-{i + 1}{ext}")
            try:
                fd = os.open(
                    candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as fp:
                    fp.write(data)
            except Exception:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
                raise
            return candidate
        raise RuntimeError(
            f"Could not find a free filename for '{filename}' in {root} "
            f"(tried up to -1000)."
        )

    def list_shared_files(self, filter: str = "") -> str:
        """List files currently staged in the cross-MCP shared mount
        (SHARED_DIR). Use this to verify that download_attachment with
        destination='shared' landed before handing the filename over to
        notion-mcp. Only regular files at the top level are shown —
        subdirectories are skipped (nothing should create them)."""
        root = Path(SHARED_DIR)
        if not root.exists():
            return f"Shared dir {SHARED_DIR} does not exist."
        files = [p for p in root.iterdir() if p.is_file()]
        if filter:
            files = [p for p in files if filter.lower() in p.name.lower()]
        if not files:
            suffix = f" matching '{filter}'" if filter else ""
            return f"Shared mount is empty{suffix}."
        lines = [f"{len(files)} file(s) in shared mount ({SHARED_DIR}):\n"]
        for p in sorted(files):
            data = p.read_bytes()
            lines.append(
                f"  • {p.name}  ({len(data)} bytes, "
                f"sha256 {self._sha256(data)})  [shared:{p.name}]"
            )
        return "\n".join(lines)

    def purge_file(self, filename: str) -> str:
        """Delete a single file from SHARED_DIR by bare filename. Use
        this after a successful cross-MCP handoff instead of waiting for
        the daily TTL sweep. Traversal-safe: rejects any filename with
        path components."""
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            raise ValueError(
                f"Invalid filename '{filename}' — must be a bare filename."
            )
        target = Path(SHARED_DIR) / safe_name
        if not target.is_file():
            return f"No such file in shared mount: {safe_name}"
        size = target.stat().st_size
        target.unlink()
        return f"Deleted {safe_name} ({size} bytes) from {SHARED_DIR}."

    def purge_shared_files(
        self, max_age_hours: float = 24.0, dry_run: bool = False,
    ) -> str:
        """Delete files in SHARED_DIR whose mtime is older than
        ``max_age_hours``. Returns a human-readable summary. Set
        ``dry_run=True`` to list what would be deleted without touching
        the filesystem."""
        import time
        if max_age_hours < 0:
            raise ValueError("max_age_hours must be >= 0")
        root = Path(SHARED_DIR)
        if not root.exists():
            return f"Shared dir {SHARED_DIR} does not exist (nothing to purge)."
        cutoff = time.time() - max_age_hours * 3600
        # Only touch regular files — leave any subdirs alone. There are
        # none today but an unrelated tool could create them, and we'd
        # rather err on the side of leaving data alone.
        victims = [
            p for p in root.iterdir()
            if p.is_file() and p.stat().st_mtime < cutoff
        ]
        if not victims:
            return (
                f"No files older than {max_age_hours}h in {SHARED_DIR} "
                f"(dir contains {sum(1 for _ in root.iterdir())} entries)."
            )
        verb = "Would delete" if dry_run else "Deleted"
        lines = [
            f"{verb} {len(victims)} file(s) older than {max_age_hours}h "
            f"from {SHARED_DIR}:"
        ]
        for p in sorted(victims):
            age_h = (time.time() - p.stat().st_mtime) / 3600
            size = p.stat().st_size
            lines.append(f"  • {p.name}  ({size} bytes, age {age_h:.1f}h)")
            if not dry_run:
                p.unlink()
        return "\n".join(lines)

    def _read_local_bytes(self, local_filename: str) -> bytes:
        """Resolve a local_filename against SHARED_DIR then ATTACHMENTS_DIR
        and return the file bytes. Rejects traversal — the resolved path
        must stay under one of the two roots."""
        roots = [Path(SHARED_DIR).resolve(), Path(ATTACHMENTS_DIR).resolve()]
        for root in roots:
            if not root.exists():
                continue
            candidate = (root / local_filename).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate.read_bytes()
        raise ValueError(
            f"local_filename '{local_filename}' not found under SHARED_DIR "
            f"({SHARED_DIR}) or ATTACHMENTS_DIR ({ATTACHMENTS_DIR})."
        )

    def _resolve_account(self, account: str) -> str:
        if not account:
            if DEFAULT_ACCOUNT:
                return DEFAULT_ACCOUNT
            stored = self.store.list_accounts()
            if len(stored) == 1:
                return stored[0]
            raise ValueError(
                "No account specified and DEFAULT_ACCOUNT is not configured. "
                + (f"Available: {', '.join(stored)}" if stored
                   else "No accounts authorized yet — run scripts/authorize.py.")
            )
        accounts = self.store.list_accounts()
        # Exact match
        if account in accounts:
            return account
        # Partial match
        matches = [a for a in accounts if account.lower() in a.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous account '{account}', matches: {', '.join(matches)}")
        raise ValueError(f"Account '{account}' not found. Available: {', '.join(accounts)}")

    def _authorized_http(self, creds):
        """Wrap ``creds`` in an ``AuthorizedHttp`` backed by a retrying,
        timeout-bounded httplib2 transport. Cached service objects then
        survive idle-closed sockets (one transparent retry) and can never
        hang a tool call indefinitely. Token refresh is still handled by
        ``AuthorizedHttp`` exactly as the default ``credentials=`` path did."""
        return AuthorizedHttp(creds, http=_RetryingHttp(timeout=_HTTP_TIMEOUT))

    def _get_service(self, account: str):
        account = self._resolve_account(account)
        if account not in self._services:
            creds = self.store.get_credentials(account)
            if not creds:
                raise ValueError(f"No credentials for {account}")
            self._services[account] = build(
                "gmail", "v1",
                http=self._authorized_http(creds),
                static_discovery=True,
            )
        return self._services[account], account

    def list_accounts(self, filter: str = "") -> str:
        accounts = self.store.list_accounts()
        if filter:
            accounts = [a for a in accounts if filter.lower() in a.lower()]
        if not accounts:
            return "No accounts configured." if not filter else f"No accounts matching '{filter}'."
        lines = ["Connected accounts:"]
        for a in accounts:
            default = " (default)" if a == DEFAULT_ACCOUNT else ""
            lines.append(f"  • {a}{default}")
        return "\n".join(lines)

    def search_email(
        self,
        query: str,
        account: str = "",
        max_results: int = 10,
        message_ids: list[str] | None = None,
    ) -> str:
        service, acct = self._get_service(account)

        if message_ids:
            # Pre-filter mode: iterate the given IDs and keep only those
            # that match ``query`` locally. Saves callers a second round
            # trip when they already have a candidate set (e.g. from a
            # Notion row export) and want to know which ones actually
            # have attachments.
            messages = self._filter_messages_by_query(
                service, message_ids, query,
            )
            if not messages:
                return (
                    f"No messages in the provided set of {len(message_ids)} "
                    f"IDs match '{query}' (account: {acct})."
                )
        else:
            result = service.users().messages().list(
                userId="me", q=query, maxResults=max_results,
            ).execute()
            messages = result.get("messages", [])
            if not messages:
                return f"No messages found for query: {query} (account: {acct})"

        lines = [f"Search results for '{query}' ({acct}) — {len(messages)} messages:\n"]
        for msg_info in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_info["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            lines.append(f"ID: {msg['id']}")
            lines.append(f"  Date: {headers.get('Date', 'N/A')}")
            lines.append(f"  From: {headers.get('From', 'N/A')}")
            lines.append(f"  Subject: {headers.get('Subject', '(no subject)')}")
            lines.append(f"  Snippet: {html.unescape(msg.get('snippet', ''))}")
            labels = msg.get("labelIds", [])
            if labels:
                lines.append(f"  Labels: {', '.join(labels)}")
            lines.append("")
        return "\n".join(lines)

    def read_email(self, message_id: str, account: str = "") -> str:
        service, acct = self._get_service(account)
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        return self._format_message(msg, acct)

    def read_thread(self, thread_id: str, account: str = "") -> str:
        service, acct = self._get_service(account)
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()

        messages = thread.get("messages", [])
        lines = [f"Thread {thread_id} ({acct}) — {len(messages)} messages:\n"]
        for i, msg in enumerate(messages):
            lines.append(f"{'='*60}")
            lines.append(f"Message {i+1}/{len(messages)}")
            lines.append(self._format_message(msg, acct))
            lines.append("")
        return "\n".join(lines)

    def _parse_attachment_source(self, source: str) -> tuple[str, bytes]:
        """Parse a unified `scheme:value` attachment reference into
        (filename, content_bytes). Supported schemes:

          local:<path>          — file under ATTACHMENTS_DIR (path-traversal
                                  rejected)
          shared:<filename>     — file on the cross-MCP shared mount
                                  (SHARED_DIR), e.g. staged there by
                                  notion-mcp's notion_download_file with
                                  destination='shared'
          drive:<name-or-id>    — file in the shared DRIVE_FOLDER_NAME
                                  Drive folder (name lookup first, file ID
                                  fallback)

        Unknown schemes raise ValueError. The caller is responsible for
        packaging the bytes into a MIME part.
        """
        if not source or ":" not in source:
            raise ValueError(
                f"Attachment '{source}' is missing a scheme. Use "
                f"'local:<path>', 'shared:<filename>', or "
                f"'drive:<name-or-id>'."
            )
        scheme, _, value = source.partition(":")
        scheme = scheme.strip().lower()
        value = value.strip()
        if not value:
            raise ValueError(f"Attachment source '{source}' has no value after the scheme.")

        if scheme == "local":
            path = self._resolve_local_attachment(value)
            return path.name, path.read_bytes()
        if scheme == "shared":
            safe_name = os.path.basename(value)
            if not safe_name or safe_name != value:
                raise ValueError(
                    f"Invalid shared filename '{value}' — must be a bare "
                    f"filename, no path components."
                )
            path = Path(SHARED_DIR) / safe_name
            if not path.is_file():
                raise ValueError(
                    f"Attachment 'shared:{value}' not found in {SHARED_DIR}. "
                    f"List staged files with list_shared_files."
                )
            return path.name, path.read_bytes()
        if scheme == "drive":
            file_id, name, mime, _ = self._resolve_drive_file(value)
            return name, self._fetch_drive_bytes(file_id, mime)
        raise ValueError(
            f"Unknown attachment scheme '{scheme}' in '{source}'. "
            f"Supported schemes: local:, shared:, drive:."
        )

    def _collect_attachments(self, attachments: list[str]) -> list[tuple[str, bytes]]:
        """Resolve a list of `scheme:value` sources into (filename, bytes)
        tuples ready for _build_message."""
        return [self._parse_attachment_source(s) for s in attachments or []]

    def _apply_reply_headers(
        self, service, message, reply_to_message_id: str
    ) -> str | None:
        """Fetch the original message's Message-ID header, stamp In-Reply-To
        and References onto the outgoing message, and return the threadId so
        the caller can include it in the draft/send body. Returns None if
        reply_to_message_id is empty."""
        if not reply_to_message_id:
            return None
        orig = service.users().messages().get(
            userId="me", id=reply_to_message_id, format="metadata",
            metadataHeaders=["Message-ID"],
        ).execute()
        orig_headers = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
        original_msg_id = orig_headers.get("Message-ID", "")
        message["In-Reply-To"] = original_msg_id
        message["References"] = original_msg_id
        return orig.get("threadId")

    def draft_email(
        self, to: list[str], subject: str, body: str, account: str = "",
        cc: list[str] | None = None, bcc: list[str] | None = None,
        reply_to_message_id: str = "",
        attachments: list[str] | None = None,
    ) -> str:
        service, acct = self._get_service(account)
        merged = self._collect_attachments(attachments or [])
        message = self._build_message(to, subject, body, cc or [], bcc or [], merged)
        thread_id = self._apply_reply_headers(service, message, reply_to_message_id)

        draft_body: dict = {"message": {"raw": self._encode_message(message)}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = service.users().drafts().create(userId="me", body=draft_body).execute()
        attach_note = f"\n  Attachments: {', '.join(name for name, _ in merged)}" if merged else ""
        return (
            f"Draft created (account: {acct})\n"
            f"  Draft ID: {draft['id']}\n"
            f"  To: {', '.join(to)}\n"
            f"  Subject: {subject}{attach_note}"
        )

    def send_email(
        self, to: list[str], subject: str, body: str, account: str = "",
        cc: list[str] | None = None, bcc: list[str] | None = None,
        reply_to_message_id: str = "",
        attachments: list[str] | None = None,
    ) -> str:
        service, acct = self._get_service(account)
        merged = self._collect_attachments(attachments or [])
        message = self._build_message(to, subject, body, cc or [], bcc or [], merged)
        thread_id = self._apply_reply_headers(service, message, reply_to_message_id)

        send_body: dict = {"raw": self._encode_message(message)}
        if thread_id:
            send_body["threadId"] = thread_id

        sent = service.users().messages().send(userId="me", body=send_body).execute()
        attach_note = f"\n  Attachments: {', '.join(name for name, _ in merged)}" if merged else ""
        return (
            f"Email sent (account: {acct})\n"
            f"  Message ID: {sent['id']}\n"
            f"  To: {', '.join(to)}\n"
            f"  Subject: {subject}{attach_note}"
        )

    def upload_file(self, filename: str, content_base64: str) -> str:
        """Save base64 content to ATTACHMENTS_DIR/uploads/ so it can later be
        passed to send_email/draft_email via the attachments parameter."""
        # Sanitise filename — no path separators, no hidden files.
        safe_name = os.path.basename(filename).lstrip(".")
        if not safe_name or safe_name != filename:
            raise ValueError(f"Invalid filename '{filename}' — must be a bare filename, no path components.")

        try:
            data = base64.b64decode(content_base64, validate=True)
        except Exception as e:
            raise ValueError(f"Invalid base64 content: {e}") from e

        uploads_dir = Path(ATTACHMENTS_DIR) / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        save_path = self._atomic_create(uploads_dir, safe_name, data)
        actual_name = save_path.name
        rename_note = (
            ""
            if actual_name == safe_name
            else f"  Note: '{safe_name}' already existed; saved as '{actual_name}'.\n"
        )
        rel = save_path.relative_to(ATTACHMENTS_DIR)
        return (
            f"Uploaded file:\n"
            f"  Filename: {actual_name}\n"
            f"  Size: {len(data)} bytes\n"
            f"  SHA256: {self._sha256(data)}\n"
            f"  Saved to: {save_path}\n"
            f"{rename_note}"
            f"  Reference as attachment: local:{rel}"
        )

    # --- Google Drive attachment store -----------------------------------

    def _get_drive_service(self):
        if self._drive_service is None:
            if not DRIVE_ACCOUNT:
                raise ValueError(
                    "DRIVE_ACCOUNT is not configured. Set it to the account "
                    "whose Google Drive should host the shared folder to "
                    "enable the drive_* tools."
                )
            creds = self.store.get_credentials(DRIVE_ACCOUNT)
            if not creds:
                raise ValueError(
                    f"No credentials stored for Drive account '{DRIVE_ACCOUNT}'. "
                    f"Run scripts/authorize.py to connect it."
                )
            self._drive_service = build(
                "drive", "v3",
                http=self._authorized_http(creds),
                cache_discovery=False,
                static_discovery=True,
            )
        return self._drive_service

    def _get_drive_folder_id(self) -> str:
        if self._drive_folder_id:
            return self._drive_folder_id
        service = self._get_drive_service()
        escaped = DRIVE_FOLDER_NAME.replace("\\", "\\\\").replace("'", "\\'")
        q = (
            f"name = '{escaped}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        res = service.files().list(
            q=q, spaces="drive", fields="files(id, name)", pageSize=10
        ).execute()
        files = res.get("files", [])
        if files:
            self._drive_folder_id = files[0]["id"]
        else:
            meta = {
                "name": DRIVE_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = service.files().create(body=meta, fields="id").execute()
            self._drive_folder_id = folder["id"]
        return self._drive_folder_id

    def drive_upload(
        self,
        filename: str,
        content_base64: str = "",
        local_filename: str = "",
    ) -> str:
        """Upload a file into the shared Drive folder (DRIVE_FOLDER_NAME).

        Provide exactly one of ``content_base64`` or ``local_filename``.
        ``local_filename`` reads a file that was already placed on disk by
        download_attachment or upload_file — either ``<message_id>/<name>``
        under ATTACHMENTS_DIR or a bare filename under SHARED_DIR — so
        large attachments never pass through MCP parameter encoding.
        """
        safe_name = os.path.basename(filename).lstrip(".")
        if not safe_name or safe_name != filename:
            raise ValueError(
                f"Invalid filename '{filename}' — must be a bare filename, no path components."
            )
        if bool(content_base64) == bool(local_filename):
            raise ValueError(
                "Provide exactly one of content_base64 or local_filename."
            )

        if local_filename:
            data = self._read_local_bytes(local_filename)
        else:
            try:
                data = base64.b64decode(content_base64, validate=True)
            except Exception as e:
                raise ValueError(f"Invalid base64 content: {e}") from e

        service = self._get_drive_service()
        folder_id = self._get_drive_folder_id()
        mime, _ = mimetypes.guess_type(safe_name)
        media = MediaInMemoryUpload(
            data, mimetype=mime or "application/octet-stream", resumable=False
        )
        meta = {"name": safe_name, "parents": [folder_id]}
        file = service.files().create(
            body=meta, media_body=media,
            fields="id, name, size, mimeType, webViewLink",
        ).execute()
        return (
            f"Uploaded to Drive folder '{DRIVE_FOLDER_NAME}' ({DRIVE_ACCOUNT}):\n"
            f"  Filename: {file['name']}\n"
            f"  File ID: {file['id']}\n"
            f"  Size: {file.get('size', len(data))} bytes\n"
            f"  SHA256: {self._sha256(data)}\n"
            f"  MIME: {file.get('mimeType', mime or 'application/octet-stream')}\n"
            f"  Link: {file.get('webViewLink', '(not shared)')}\n"
            f"  Reference as attachment: drive:{file['name']}"
        )

    def drive_list(self, filter: str = "") -> str:
        """List files in the shared Drive folder (DRIVE_FOLDER_NAME)."""
        service = self._get_drive_service()
        folder_id = self._get_drive_folder_id()
        res = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, size, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=100,
        ).execute()
        files = res.get("files", [])
        if filter:
            files = [f for f in files if filter.lower() in f["name"].lower()]
        if not files:
            suffix = f" matching '{filter}'" if filter else ""
            return f"Drive folder '{DRIVE_FOLDER_NAME}' ({DRIVE_ACCOUNT}) is empty{suffix}."
        lines = [
            f"Drive folder '{DRIVE_FOLDER_NAME}' ({DRIVE_ACCOUNT}) — {len(files)} files:\n"
        ]
        for f in files:
            size = f.get("size", "?")
            lines.append(f"  • {f['name']}  ({f.get('mimeType', 'unknown')}, {size} bytes)")
            lines.append(f"      id: {f['id']}  modified: {f.get('modifiedTime', 'N/A')}")
        return "\n".join(lines)

    def drive_download(self, name_or_id: str, return_base64: bool = False) -> str:
        """Download a file from the Drive folder, either to
        ATTACHMENTS_DIR/drive/ on disk or inline as base64."""
        file_id, name, mime, _ = self._resolve_drive_file(name_or_id)
        content = self._fetch_drive_bytes(file_id, mime)

        if return_base64:
            encoded = base64.standard_b64encode(content).decode("ascii")
            return (
                f"Drive file ({DRIVE_ACCOUNT}):\n"
                f"  Filename: {name}\n"
                f"  File ID: {file_id}\n"
                f"  Size: {len(content)} bytes\n"
                f"  SHA256: {self._sha256(content)}\n"
                f"  MIME: {mime}\n"
                f"  Base64:\n{encoded}"
            )

        save_dir = Path(ATTACHMENTS_DIR) / "drive"
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = self._atomic_create(save_dir, name, content)
        rel = save_path.relative_to(ATTACHMENTS_DIR)
        rename_note = (
            ""
            if save_path.name == name
            else f"  Note: '{name}' already existed; saved as '{save_path.name}'.\n"
        )
        return (
            f"Downloaded from Drive ({DRIVE_ACCOUNT}):\n"
            f"  Filename: {save_path.name}\n"
            f"  File ID: {file_id}\n"
            f"  Size: {len(content)} bytes\n"
            f"  SHA256: {self._sha256(content)}\n"
            f"  MIME: {mime}\n"
            f"  Saved to: {save_path}\n"
            f"{rename_note}"
            f"  Reference as attachment: local:{rel}"
        )

    def _resolve_drive_file(self, name_or_id: str) -> tuple[str, str, str, int]:
        """Return (file_id, name, mime_type, size) for a file in the folder.

        Tries name lookup within the folder first; falls back to treating
        the input as a Drive file ID and verifying it lives in the folder."""
        if not name_or_id:
            raise ValueError("Drive file name or ID required.")
        service = self._get_drive_service()
        folder_id = self._get_drive_folder_id()

        escaped = name_or_id.replace("\\", "\\\\").replace("'", "\\'")
        q = (
            f"'{folder_id}' in parents "
            f"and name = '{escaped}' "
            f"and trashed = false"
        )
        res = service.files().list(
            q=q, fields="files(id, name, mimeType, size)", pageSize=10
        ).execute()
        files = res.get("files", [])
        if len(files) == 1:
            f = files[0]
            return f["id"], f["name"], f.get("mimeType", ""), int(f.get("size", 0) or 0)
        if len(files) > 1:
            raise ValueError(
                f"Ambiguous Drive file '{name_or_id}' — "
                f"{len(files)} files with that name in folder '{DRIVE_FOLDER_NAME}'. "
                f"Pass the file ID instead."
            )
        # Fall back to treating the string as a file ID.
        try:
            f = service.files().get(
                fileId=name_or_id,
                fields="id, name, mimeType, size, parents",
            ).execute()
        except HttpError as e:
            raise ValueError(f"Drive file '{name_or_id}' not found.") from e
        if folder_id not in (f.get("parents") or []):
            raise ValueError(
                f"Drive file '{name_or_id}' exists but is not inside folder '{DRIVE_FOLDER_NAME}'."
            )
        return f["id"], f["name"], f.get("mimeType", ""), int(f.get("size", 0) or 0)

    def _fetch_drive_bytes(self, file_id: str, mime_type: str) -> bytes:
        service = self._get_drive_service()
        if mime_type.startswith("application/vnd.google-apps"):
            # Google-native files (Docs/Sheets/Slides) need to be exported,
            # not downloaded via get_media. Refuse for now — attachment use
            # case targets user-uploaded binaries.
            raise ValueError(
                f"Drive file has Google-native type '{mime_type}' — cannot attach "
                f"directly. Export it to PDF/etc. in Drive first."
            )
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    def list_labels(self, account: str = "") -> str:
        service, acct = self._get_service(account)
        result = service.users().labels().list(userId="me").execute()
        labels = result.get("labels", [])

        system = [l for l in labels if l.get("type") == "system"]
        user = [l for l in labels if l.get("type") == "user"]

        lines = [f"Labels for {acct}:\n"]
        if system:
            lines.append("System labels:")
            for l in sorted(system, key=lambda x: x["name"]):
                lines.append(f"  • {l['name']} (id: {l['id']})")
        if user:
            lines.append("\nUser labels:")
            for l in sorted(user, key=lambda x: x["name"]):
                lines.append(f"  • {l['name']} (id: {l['id']})")
        return "\n".join(lines)

    def modify_labels(
        self, message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        account: str = "",
    ) -> str:
        service, acct = self._get_service(account)
        body: dict = {}
        if add_labels:
            body["addLabelIds"] = list(add_labels)
        if remove_labels:
            body["removeLabelIds"] = list(remove_labels)
        if not body:
            return "No label changes specified."

        service.users().messages().modify(
            userId="me", id=message_id, body=body
        ).execute()
        parts = []
        if add_labels:
            parts.append(f"added: {', '.join(add_labels)}")
        if remove_labels:
            parts.append(f"removed: {', '.join(remove_labels)}")
        return f"Labels updated for message {message_id} ({acct}): {'; '.join(parts)}"

    def archive_email(self, message_id: str, account: str = "") -> str:
        return self.modify_labels(message_id, remove_labels=["INBOX"], account=account)

    def mark_read(self, message_id: str, account: str = "") -> str:
        return self.modify_labels(message_id, remove_labels=["UNREAD"], account=account)

    def mark_unread(self, message_id: str, account: str = "") -> str:
        return self.modify_labels(message_id, add_labels=["UNREAD"], account=account)

    def list_attachments(
        self,
        message_id: str,
        account: str = "",
        exclude_inline: bool = False,
    ) -> str:
        service, acct = self._get_service(account)
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        attachments = self._find_attachments(msg.get("payload", {}))
        if exclude_inline:
            attachments = [
                a for a in attachments
                if self._part_disposition(a) == "attachment"
            ]
        if not attachments:
            suffix = " (real attachments)" if exclude_inline else ""
            return f"No attachments found{suffix} on message {message_id} ({acct})."

        # Count filename occurrences so duplicates can be flagged with the
        # per-filename index callers must pass to target a specific copy.
        from collections import Counter
        name_counts = Counter(a["filename"] for a in attachments)
        dup_seen: dict[str, int] = {}

        lines = [f"Attachments on message {message_id} ({acct}):\n"]
        for i, att in enumerate(attachments):
            name = att["filename"]
            size = att.get("body", {}).get("size", 0)
            disposition = self._part_disposition(att)
            dup_note = ""
            if name_counts[name] > 1:
                fidx = dup_seen.get(name, 0)
                dup_seen[name] = fidx + 1
                dup_note = f"  [duplicate filename — pass index={fidx} to target this one]"
            lines.append(
                f"  {i}: {name} "
                f"({att.get('mimeType', 'unknown')}, {size} bytes, "
                f"disposition={disposition}){dup_note}"
            )
        lines.append(
            f"\nUse download_attachment / extract_attachment_text / "
            f"render_attachment_page with message_id='{message_id}' and "
            f"filename to act on an attachment. When the same filename appears "
            f"more than once, add index=N (0-based, as shown above) to pick a "
            f"specific copy."
        )
        return "\n".join(lines)

    def batch_list_attachments(
        self,
        message_ids: list[str],
        account: str = "",
        exclude_inline: bool = False,
    ) -> str:
        """Return a JSON map of {message_id: [{filename, mime_type,
        size, disposition}, ...]} for a list of messages. Collapses what
        would otherwise be N round-trip list_attachments calls — use
        this when processing a candidate set (e.g. a batch exported
        from a Notion database) to find the N with real attachments.

        Messages with zero matching attachments appear with an empty
        list so callers can see the full picture. On a per-message
        error (deleted/inaccessible message), the value is a string
        error token so one bad ID doesn't poison the whole batch."""
        if not isinstance(message_ids, list) or not message_ids:
            raise ValueError("message_ids must be a non-empty list of str.")
        service, acct = self._get_service(account)
        out: dict[str, object] = {}
        for mid in message_ids:
            try:
                msg = service.users().messages().get(
                    userId="me", id=mid, format="full",
                ).execute()
                parts = self._find_attachments(msg.get("payload", {}))
                if exclude_inline:
                    parts = [
                        p for p in parts
                        if self._part_disposition(p) == "attachment"
                    ]
                out[mid] = [
                    {
                        "filename": p.get("filename"),
                        "mime_type": p.get("mimeType", "unknown"),
                        "size": p.get("body", {}).get("size", 0),
                        "disposition": self._part_disposition(p),
                    }
                    for p in parts
                ]
            except HttpError as e:
                out[mid] = f"error: {e}"
        import json as _json
        with_files = sum(
            1 for v in out.values() if isinstance(v, list) and v
        )
        return (
            f"Batch list_attachments ({acct}) — "
            f"{with_files}/{len(message_ids)} messages with matching attachments:\n"
            + _json.dumps(out, indent=2)
        )

    def _filter_messages_by_query(
        self,
        service,
        message_ids: list[str],
        query: str,
    ) -> list[dict]:
        """Local pre-filter: keep message IDs whose full payload matches
        the given ``query``. Only a small subset of Gmail's query
        grammar is understood — the currently-supported predicates are:
          • ``has:attachment``  — message has at least one real
            (non-inline) attachment
        An empty ``query`` keeps every message. Returns a list of
        ``{"id": ...}`` dicts shaped like Gmail's messages.list output
        so the caller can reuse its existing formatting loop."""
        q = (query or "").strip().lower()
        want_attachment = "has:attachment" in q
        kept: list[dict] = []
        for mid in message_ids:
            try:
                msg = service.users().messages().get(
                    userId="me", id=mid, format="full",
                ).execute()
            except HttpError:
                continue
            if want_attachment:
                parts = self._find_attachments(msg.get("payload", {}))
                real = [
                    p for p in parts
                    if self._part_disposition(p) == "attachment"
                ]
                if not real:
                    continue
            kept.append({"id": mid})
        return kept

    def _fetch_attachment_bytes(
        self,
        message_id: str,
        filename: str,
        account: str = "",
        index: int = 0,
    ) -> tuple[str, bytes, str]:
        """Fetch attachment bytes via the Gmail API.

        Returns ``(acct, file_bytes, mime_type)``. Raises
        ``FileNotFoundError`` if no part with ``filename`` exists on the
        message (error message lists available filenames) and
        ``ValueError`` if the matched part has no ``attachmentId`` (e.g.
        an inline part stored in the payload directly). Shared by
        ``download_attachment``, ``extract_attachment_text``, and
        ``render_attachment_page`` so attachment resolution stays
        consistent across tools.

        ``index`` disambiguates duplicate filenames: when an email carries
        several attachments with the *same* ``filename`` (e.g. a vendor that
        sends a receipt and an itemised invoice both named ``Invoice.pdf``),
        ``index`` selects the 0-based Nth match in attachment order. The
        default ``0`` preserves the historical first-match behaviour."""
        service, acct = self._get_service(account)
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        attachments = self._find_attachments(msg.get("payload", {}))
        match = [a for a in attachments if a["filename"] == filename]
        if not match:
            available = [a["filename"] for a in attachments]
            raise FileNotFoundError(
                f"Attachment '{filename}' not found. "
                f"Available: {', '.join(available) or 'none'}"
            )
        if index < 0 or index >= len(match):
            raise FileNotFoundError(
                f"Attachment '{filename}' index {index} out of range — "
                f"{len(match)} attachment(s) match that filename "
                f"(valid index 0..{len(match) - 1}). "
                f"Use list_attachments to see the duplicates."
            )

        part = match[index]
        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            raise ValueError(
                f"Attachment '{filename}' has no attachment ID (may be inline)."
            )

        att_data = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        file_bytes = base64.urlsafe_b64decode(att_data["data"])
        mime_type = part.get("mimeType", "application/octet-stream")
        return acct, file_bytes, mime_type

    def download_attachment(
        self,
        message_id: str,
        filename: str,
        account: str = "",
        return_base64: bool = False,
        destination: str = "private",
        prefix: str = "",
        index: int = 0,
    ) -> str:
        dest_norm = destination.strip().lower()
        if dest_norm not in ("private", "shared"):
            raise ValueError(
                f"destination must be 'private' or 'shared', got {destination!r}."
            )
        # Guard against path-escape via filename.
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            raise ValueError(
                f"Invalid filename '{filename}' — must be a bare filename."
            )
        if prefix:
            # Reject path-escape in prefix too — it ends up as a
            # literal substring on a bare filename.
            if "/" in prefix or prefix != os.path.basename(prefix):
                raise ValueError(
                    f"Invalid prefix {prefix!r} — must not contain path "
                    f"separators."
                )

        try:
            acct, file_data, _mime = self._fetch_attachment_bytes(
                message_id, filename, account, index=index,
            )
        except FileNotFoundError as e:
            return str(e)
        except ValueError as e:
            # "no attachment ID (may be inline)" — keep the old
            # return-as-string behavior so existing callers don't break.
            if "no attachment ID" in str(e):
                return str(e)
            raise

        if return_base64:
            encoded = base64.standard_b64encode(file_data).decode("ascii")
            return (
                f"Attachment ({acct}):\n"
                f"  Filename: {filename}\n"
                f"  Size: {len(file_data)} bytes\n"
                f"  SHA256: {self._sha256(file_data)}\n"
                f"  Base64:\n{encoded}"
            )

        if dest_norm == "shared":
            save_dir = Path(SHARED_DIR)
            target_name = f"{prefix}{safe_name}" if prefix else safe_name
            save_path = self._atomic_create(save_dir, target_name, file_data)
            actual_name = save_path.name
            rename_note = (
                ""
                if actual_name == target_name
                else f"  Note: '{target_name}' already existed; saved as '{actual_name}'.\n"
            )
            return (
                f"Downloaded attachment ({acct}) to shared storage:\n"
                f"  Filename: {actual_name}\n"
                f"  Size: {len(file_data)} bytes\n"
                f"  SHA256: {self._sha256(file_data)}\n"
                f"  Saved to: {save_path}\n"
                f"{rename_note}"
                f"  Reference from notion-mcp: source='shared:{actual_name}'\n"
                f"  TTL: files in shared storage are purged after 24h by "
                f"mcp-shared-purge.timer — stage to Notion within that window."
            )

        # Save to attachments directory, namespaced by message ID
        save_dir = Path(ATTACHMENTS_DIR) / message_id
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / safe_name
        save_path.write_bytes(file_data)

        return (
            f"Downloaded attachment ({acct}):\n"
            f"  Filename: {safe_name}\n"
            f"  Size: {len(file_data)} bytes\n"
            f"  SHA256: {self._sha256(file_data)}\n"
            f"  Saved to: {save_path}\n"
            f"  Reference as attachment: local:{message_id}/{safe_name}\n"
            f"  Reference as drive_upload(local_filename=...): {message_id}/{safe_name}"
        )

    # Maximum size (in bytes) of text returned by extract_attachment_text
    # before it is truncated. 200 KB fits well within typical MCP
    # response-size ceilings while covering multi-page invoices in full.
    EXTRACT_TEXT_MAX_BYTES = 200_000

    # Kept as a method alias for backward compatibility — the parser
    # itself lives in pdf_read (shared with notion-mcp).
    _parse_page_spec = staticmethod(pdf_read.parse_page_spec)

    def extract_attachment_text(
        self,
        message_id: str,
        filename: str,
        account: str = "",
        pages: str | None = None,
        mode: str = "text",
        ocr: str = "auto",
        index: int = 0,
    ) -> str:
        """Extract text from a PDF attachment without writing the bytes
        to disk. Returns a JSON document with the shape described in the
        tool docstring. Non-PDF attachments are rejected. Extraction
        semantics (modes, OCR tiers) live in :mod:`google_accounts_mcp.pdf_read`."""
        import json as _json

        # Fail fast on bad options before spending an API round-trip.
        pdf_read.validate_options(mode, ocr)

        # Reuse the same attachment-resolution path as download_attachment
        # so filename lookup errors look the same across tools.
        try:
            acct, file_bytes, mime_type = self._fetch_attachment_bytes(
                message_id, filename, account, index=index,
            )
        except FileNotFoundError as e:
            return _json.dumps({"error": str(e)}, indent=2)
        except ValueError as e:
            if "no attachment ID" in str(e):
                return _json.dumps({"error": str(e)}, indent=2)
            raise

        # PDF detection: check MIME type first, fall back to magic bytes
        # (some senders mislabel PDFs as application/octet-stream).
        is_pdf = (
            mime_type == "application/pdf"
            or filename.lower().endswith(".pdf")
            or file_bytes[:5] == b"%PDF-"
        )
        if not is_pdf:
            return _json.dumps(
                {
                    "error": (
                        f"extract_attachment_text only supports PDFs; "
                        f"'{filename}' is {mime_type}. Use download_attachment "
                        f"with destination='shared' and read the bytes directly."
                    ),
                },
                indent=2,
            )

        result = pdf_read.extract_pdf_text(
            file_bytes,
            pages=pages,
            mode=mode,
            ocr=ocr,
            max_bytes=self.EXTRACT_TEXT_MAX_BYTES,
            concurrency=_PDF_CONCURRENCY,
        )
        result["account"] = acct
        result["filename"] = filename
        return _json.dumps(result, indent=2)

    def render_attachment_page_raw(
        self,
        message_id: str,
        filename: str,
        page: int,
        account: str = "",
        max_width: int = 1200,
        format: str = "jpeg",
        quality: int = 75,
        index: int = 0,
    ) -> dict:
        """Render a single PDF page to encoded image bytes. Returns a dict
        with ``image_bytes`` (raw, NOT base64), ``mime_type``, ``width``,
        ``height``, ``page``, ``page_count``, ``account``, ``filename`` —
        or a dict with an ``error`` key. The server layer turns this into
        an MCP image content block (or base64 JSON on request)."""
        fmt = (format or "jpeg").strip().lower()
        if fmt not in ("png", "jpeg", "jpg"):
            raise ValueError(
                f"format must be 'png' or 'jpeg' — got {format!r}."
            )
        if fmt == "jpg":
            fmt = "jpeg"
        if not (1 <= quality <= 100):
            raise ValueError(f"quality must be in 1..100 — got {quality}.")
        if not (200 <= max_width <= 4000):
            raise ValueError(
                f"max_width must be in 200..4000 — got {max_width}."
            )
        if not isinstance(page, int) or page < 1:
            raise ValueError(f"page must be a 1-indexed integer — got {page!r}.")

        try:
            acct, file_bytes, mime_type = self._fetch_attachment_bytes(
                message_id, filename, account, index=index,
            )
        except FileNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            if "no attachment ID" in str(e):
                return {"error": str(e)}
            raise

        is_pdf = (
            mime_type == "application/pdf"
            or filename.lower().endswith(".pdf")
            or file_bytes[:5] == b"%PDF-"
        )
        if not is_pdf:
            return {
                "error": (
                    f"render_attachment_page only supports PDFs; "
                    f"'{filename}' is {mime_type}."
                ),
            }

        try:
            img_bytes, out_mime, out_w, out_h, page_count = (
                pdf_read.render_pdf_page_bytes(
                    file_bytes, page,
                    max_width=max_width,
                    fmt=fmt,
                    quality=quality,
                    concurrency=_PDF_CONCURRENCY,
                )
            )
        except pdf_read.PageOutOfRangeError as e:
            return {"error": str(e), "page_count": e.page_count}

        return {
            "image_bytes": img_bytes,
            "mime_type": out_mime,
            "width": out_w,
            "height": out_h,
            "page": page,
            "page_count": page_count,
            "account": acct,
            "filename": filename,
        }

    def render_attachment_page(
        self,
        message_id: str,
        filename: str,
        page: int,
        account: str = "",
        max_width: int = 1200,
        format: str = "jpeg",
        quality: int = 75,
        index: int = 0,
    ) -> str:
        """Render a single PDF page to a base64-encoded image. Returns
        JSON with keys ``image_base64``, ``mime_type``, ``width``,
        ``height``, ``page``, ``page_count``. One page per call — that
        bounds the base64 payload size."""
        import json as _json

        raw = self.render_attachment_page_raw(
            message_id, filename, page, account,
            max_width=max_width, format=format, quality=quality, index=index,
        )
        if "error" in raw:
            return _json.dumps(raw, indent=2)
        img_bytes = raw.pop("image_bytes")
        raw["image_base64"] = base64.standard_b64encode(img_bytes).decode(
            "ascii"
        )
        # Key order matches the historical response shape.
        ordered = {
            "image_base64": raw["image_base64"],
            "mime_type": raw["mime_type"],
            "width": raw["width"],
            "height": raw["height"],
            "page": raw["page"],
            "page_count": raw["page_count"],
            "account": raw["account"],
            "filename": raw["filename"],
        }
        return _json.dumps(ordered, indent=2)

    def _find_attachments(self, payload: dict) -> list[dict]:
        attachments = []
        if payload.get("filename"):
            attachments.append(payload)
        for part in payload.get("parts", []):
            attachments.extend(self._find_attachments(part))
        return attachments

    @staticmethod
    def _part_disposition(part: dict) -> str:
        """Classify a payload part as ``'inline'`` or ``'attachment'``.

        Uses the MIME ``Content-Disposition`` header if present; falls
        back to ``Content-ID`` (a ``cid:`` reference from elsewhere in
        the email body is the main signal of an inline image). Defaults
        to ``'attachment'`` when no disposition hints exist — matches
        RFC 2183 semantics and keeps the old behavior for messages
        that only set a filename."""
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in part.get("headers") or []
        }
        disp = headers.get("content-disposition", "").strip().lower()
        if disp.startswith("inline"):
            return "inline"
        if disp.startswith("attachment"):
            return "attachment"
        if headers.get("content-id"):
            return "inline"
        return "attachment"

    # --- helpers ---

    def _format_message(self, msg: dict, acct: str) -> str:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        lines = [
            f"Message ID: {msg['id']}  Thread ID: {msg.get('threadId', 'N/A')}",
            f"Account: {acct}",
            f"Date: {headers.get('Date', 'N/A')}",
            f"From: {headers.get('From', 'N/A')}",
            f"To: {headers.get('To', 'N/A')}",
        ]
        if headers.get("Cc"):
            lines.append(f"CC: {headers['Cc']}")
        lines.append(f"Subject: {headers.get('Subject', '(no subject)')}")

        label_ids = msg.get("labelIds", [])
        if label_ids:
            lines.append(f"Labels: {', '.join(label_ids)}")

        lines.append(f"\n{'—'*40}\n")
        body = self._extract_body(msg.get("payload", {}))
        lines.append(body if body else "(empty body)")
        return "\n".join(lines)

    def _extract_body(self, payload: dict) -> str:
        # Try text/plain first, fall back to text/html
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

        if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
            raw_html = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
            return self._strip_html(raw_html)

        # Multipart: recurse into parts
        parts = payload.get("parts", [])
        # Prefer text/plain
        for part in parts:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        # Fall back to text/html
        for part in parts:
            if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                raw_html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                return self._strip_html(raw_html)
        # Recurse into nested multipart
        for part in parts:
            result = self._extract_body(part)
            if result:
                return result

        # List attachments if present
        attachments = [p for p in parts if p.get("filename")]
        if attachments:
            att_lines = ["Attachments:"]
            for a in attachments:
                att_lines.append(f"  • {a['filename']} ({a.get('mimeType', 'unknown')})")
            return "\n".join(att_lines)

        return ""

    def _strip_html(self, text: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        # Collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _resolve_local_attachment(self, path_str: str) -> Path:
        """Resolve a single `local:` attachment path to an absolute Path
        under ATTACHMENTS_DIR. Rejects any path that escapes the root
        (symlinks are followed, then checked) so the tool can't be used to
        exfiltrate arbitrary host files.
        """
        base = Path(ATTACHMENTS_DIR).resolve()
        candidate = Path(path_str)
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise ValueError(
                f"Attachment path '{path_str}' is outside ATTACHMENTS_DIR ({base}). "
                f"Stage via upload_file or reference a downloaded attachment."
            )
        if not candidate.is_file():
            raise ValueError(f"Attachment '{path_str}' not found at {candidate}.")
        return candidate

    def _build_message(
        self, to: list[str], subject: str, body: str,
        cc: list[str] | None = None, bcc: list[str] | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> EmailMessage | MIMEText:
        to_header = ", ".join(to)
        cc_header = ", ".join(cc or [])
        bcc_header = ", ".join(bcc or [])

        if not attachments:
            message = MIMEText(body)
            message["To"] = to_header
            message["Subject"] = subject
            if cc_header:
                message["Cc"] = cc_header
            if bcc_header:
                message["Bcc"] = bcc_header
            return message

        # Multipart path: use modern EmailMessage API which handles
        # Content-Type/Transfer-Encoding automatically for each part.
        message = EmailMessage()
        message["To"] = to_header
        message["Subject"] = subject
        if cc_header:
            message["Cc"] = cc_header
        if bcc_header:
            message["Bcc"] = bcc_header
        message.set_content(body)
        for filename, content in attachments:
            ctype, encoding = mimetypes.guess_type(filename)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            message.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )
        return message

    def _encode_message(self, message) -> str:
        return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

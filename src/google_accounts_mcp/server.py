import functools
import hmac
import inspect
import json
import logging
import os
import time

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

from google_accounts_mcp.calendar_client import CalendarClient
from google_accounts_mcp.config import MCP_BEARER_TOKEN, PORT
from google_accounts_mcp.contacts_client import ContactsClient
from google_accounts_mcp.gmail_client import GmailClient
from google_accounts_mcp.tasks_client import TasksClient

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Multi-account Gmail + Drive + Calendar + Tasks + Contacts server.
Use search_contacts to resolve names to addresses before drafting
email; saved contacts are read/write (create/update/delete +
save_other_contact to promote autocomplete entries). File transfer uses unified
scheme references: 'local:<path>' (private store), 'shared:<filename>'
(cross-MCP mount, visible to notion-mcp), 'drive:<name-or-id>' (shared
Drive folder). Zero-copy pipeline with notion-mcp via the shared mount:
Gmail→Notion: download_attachment(destination='shared') then
notion_upload_file(source='shared:<name>'). Notion→Gmail:
notion_download_file(destination='shared') then
draft_email/send_email(attachments=['shared:<name>']). Bytes never
traverse MCP parameters that way — no size limits. Shared files may be
purged periodically (24h in the reference deployment); verify staging
with list_shared_files (returns sha256 for integrity checks, as do all
transfer tools).
PDF attachments are readable in place: extract_attachment_text gets the
text (scanned/image-only pages are OCR'd automatically; ocr='llm' routes
hard documents like handwriting to a vision model), and
render_attachment_page returns a viewable image of any page — look at
the page directly instead of giving up on a scan."""

mcp = FastMCP(
    "gmail",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)
client = GmailClient()
# CalendarClient shares the GmailClient's TokenStore so the same refresh
# tokens (now covering calendar.events scope too) are reused without a
# second DB handle.
calendar = CalendarClient(store=client.store)
# TasksClient / ContactsClient share the same TokenStore for the same reason.
tasks = TasksClient(store=client.store)
contacts = ContactsClient(store=client.store)


# --- Run blocking tool handlers off the event loop -----------------------
# The MCP SDK executes a synchronous @mcp.tool function INLINE on the single
# asyncio event loop (func_metadata.call_fn_with_arg_validation: the sync
# branch is just `return fn(...)`). Every tool here makes a blocking upstream
# call (httplib2 .execute(), which can stall up to ~120s on a stale socket),
# so one slow call would freeze the loop and stall ALL other requests — even a
# cheap ListTools/ping — until it returns, surfacing as the client's 4-minute
# "MCP server unresponsive" timeout.
#
# Wrapping mcp.tool makes each *sync* tool run in a worker thread via
# anyio.to_thread.run_sync, keeping the loop free for concurrent requests.
# functools.wraps + an explicit __signature__ preserve the introspected
# signature, so the generated tool schema is byte-for-byte unchanged (clients
# need no restart). Each call also emits a structured one-line log
# (tool / duration / outcome) for Loki/Grafana so the next stall is visible.
#
# Dedicated request logger: plain single-line output (bypasses FastMCP's
# RichHandler, which wraps lines and would split duration_ms onto a
# continuation line, breaking Loki field extraction). propagate=False keeps
# it off the root handler so the line is emitted exactly once.
req_logger = logging.getLogger("google_accounts_mcp.requests")
if not req_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    req_logger.addHandler(_h)
    req_logger.setLevel(logging.INFO)
    req_logger.propagate = False


def _rss_mib() -> int:
    """Current process RSS in MiB, read cheaply from /proc (no psutil dep).
    Lets Grafana correlate a hang/OOM with the request that triggered it."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf")
                        else 4096) // (1024 * 1024)
    except Exception:
        return -1


_orig_tool = mcp.tool


def _threaded_tool(*d_args, **d_kwargs):
    register = _orig_tool(*d_args, **d_kwargs)

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):
            return register(fn)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            outcome = "ok"
            try:
                return await anyio.to_thread.run_sync(
                    functools.partial(fn, *args, **kwargs)
                )
            except Exception:
                outcome = "error"
                raise
            finally:
                req_logger.info(
                    "tool=%s outcome=%s duration_ms=%d rss_mib=%d",
                    fn.__name__, outcome,
                    int((time.perf_counter() - start) * 1000),
                    _rss_mib(),
                )

        wrapper.__signature__ = inspect.signature(fn)
        # Register the threaded wrapper with FastMCP (the runtime calls this),
        # but return the ORIGINAL sync fn so the module-level name stays
        # directly callable — tool-to-tool calls and the unit tests invoke
        # these functions synchronously and must not get a coroutine.
        register(wrapper)
        return fn

    return decorator


mcp.tool = _threaded_tool


class BearerAuthMiddleware:
    """Pure ASGI middleware — compatible with SSE streaming (unlike BaseHTTPMiddleware)."""

    def __init__(self, app, expected_token: str):
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            # Let discovery probes through unauthenticated so MCP clients
            # don't mistake 401 for an OAuth-protected server.
            path = scope.get("path", "")
            if path.startswith("/.well-known/"):
                return await self.app(scope, receive, send)
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth.startswith("Bearer "):
                token = auth[7:]
                if hmac.compare_digest(token, self.expected_token):
                    return await self.app(scope, receive, send)
            client_host = scope.get("client", ("unknown",))[0]
            # The container liveness probe (google_accounts_mcp.healthcheck) hits /mcp
            # unauthenticated from loopback every 30s by design — its 401 is the
            # intended liveness signal, not an intrusion. Logging it at WARNING
            # floods Loki (~2880 lines/day) and trips the log-watchdog every
            # cycle, so demote the loopback case to DEBUG. Real unauthorized
            # requests arrive via Traefik from a non-loopback source and still
            # warn.
            if client_host in ("127.0.0.1", "::1"):
                logger.debug("Health probe 401 (loopback, expected)")
            else:
                logger.warning("Rejected request: invalid or missing bearer token from %s", client_host)
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


@mcp.tool()
def list_accounts(filter: str = "") -> str:
    """List all connected Gmail accounts. Optional filter matches substrings against email addresses."""
    return client.list_accounts(filter)


@mcp.tool()
def search_email(
    query: str,
    account: str = "",
    max_results: int = 10,
    message_ids: list[str] | None = None,
) -> str:
    """Search emails using Gmail search syntax (e.g. 'from:user@example.com',
    'is:unread', 'subject:hello'). Defaults to the configured
    DEFAULT_ACCOUNT (or the sole authorized account) when account is
    not specified.

    message_ids: optional pre-filter set. When provided, the tool iterates
      over these IDs instead of calling Gmail's search API and keeps only
      the ones matching ``query``. Useful when you already have a candidate
      set (e.g. exported from a Notion database) and want to avoid N blind
      list_attachments calls. Currently only ``has:attachment`` is honored
      as a local predicate — other query terms are ignored in pre-filter
      mode."""
    return client.search_email(query, account, max_results, message_ids)


@mcp.tool()
def read_email(message_id: str, account: str = "") -> str:
    """Read the full content of an email by its message ID.
    Use search_email first to find message IDs."""
    return client.read_email(message_id, account)


@mcp.tool()
def read_thread(thread_id: str, account: str = "") -> str:
    """Read all messages in an email thread by its thread ID.
    Thread IDs are shown in search results and message details."""
    return client.read_thread(thread_id, account)


@mcp.tool()
def draft_email(
    to: list[str], subject: str, body: str, account: str = "",
    cc: list[str] | None = None, bcc: list[str] | None = None,
    reply_to_message_id: str = "",
    attachments: list[str] | None = None,
) -> str:
    """Create a draft email. The draft can be reviewed and sent from Gmail.

    to / cc / bcc: lists of email addresses.
    reply_to_message_id: set to turn the draft into a reply (stamps
      In-Reply-To / References headers and preserves the thread).
    attachments: list of unified source references. Each entry is:
      - 'local:<path>'        — file under ATTACHMENTS_DIR (e.g.
        'local:uploads/report.pdf' after upload_file, or
        'local:<message_id>/<filename>' after download_attachment)
      - 'shared:<filename>'   — file on the cross-MCP shared mount, e.g.
        staged by notion_download_file(destination='shared'). Verify
        with list_shared_files.
      - 'drive:<name-or-id>'  — file in the shared DRIVE_FOLDER_NAME
        Drive folder (populated via drive_upload)

    ## Cross-MCP file transfer (Notion → Gmail)
    To attach a Notion file to a draft:
      1. notion-mcp/notion_download_file(block_id, destination='shared')
      2. draft_email(..., attachments=['shared:<filename>'])
    Bytes never traverse MCP parameters — no size limits."""
    return client.draft_email(
        to=to, subject=subject, body=body, account=account,
        cc=cc, bcc=bcc, reply_to_message_id=reply_to_message_id,
        attachments=attachments,
    )


@mcp.tool()
def send_email(
    to: list[str], subject: str, body: str, account: str = "",
    cc: list[str] | None = None, bcc: list[str] | None = None,
    reply_to_message_id: str = "",
    attachments: list[str] | None = None,
) -> str:
    """Send an email directly. Use with care — this dispatches immediately.

    to / cc / bcc: lists of email addresses.
    reply_to_message_id: set to send as a reply in an existing thread.
    attachments: list of unified source references. Each entry is:
      - 'local:<path>'        — file under ATTACHMENTS_DIR
      - 'shared:<filename>'   — file on the cross-MCP shared mount, e.g.
        staged by notion_download_file(destination='shared')
      - 'drive:<name-or-id>'  — file in the shared DRIVE_FOLDER_NAME
        Drive folder"""
    return client.send_email(
        to=to, subject=subject, body=body, account=account,
        cc=cc, bcc=bcc, reply_to_message_id=reply_to_message_id,
        attachments=attachments,
    )


@mcp.tool()
def upload_file(filename: str, content_base64: str) -> str:
    """Save a file to the local attachments directory so it can be attached to an
    outgoing email via draft_email/send_email. Pass the raw filename (no path
    components) and the file content as base64. Subject to MCP parameter size
    limits — for large files prefer the shared mount or Drive. Returns the
    attachment reference to pass to draft_email/send_email (e.g.
    'local:uploads/report.pdf') plus the sha256 of the saved bytes. Name
    collisions auto-rename (-2, -3, ...) rather than overwrite."""
    return client.upload_file(filename, content_base64)


@mcp.tool()
def drive_upload(
    filename: str,
    content_base64: str = "",
    local_filename: str = "",
) -> str:
    """Upload a file to the shared Drive folder on the configured
    DRIVE_ACCOUNT. Files persist in Drive across restarts.

    Provide exactly one of content_base64 or local_filename:
      • content_base64 — raw base64 bytes. Subject to MCP parameter size
        limits; avoid for files larger than ~20KB.
      • local_filename — reference a file already on disk. Accepts
        '<message_id>/<name>' (saved by download_attachment with
        destination='private', the default) or a bare '<name>' that lives
        in SHARED_DIR (saved by download_attachment destination='shared'
        or upload_file). No size limit — the bytes never traverse MCP.

    Returns the Drive file ID, webViewLink, and the reference to use as
    drive_attachments when sending email. The uploaded file is also
    reachable from notion-mcp via source='drive:<file_id>' on
    notion_upload_file / notion_add_file_to_row."""
    return client.drive_upload(
        filename,
        content_base64=content_base64,
        local_filename=local_filename,
    )


@mcp.tool()
def purge_shared_files(max_age_hours: float = 24.0, dry_run: bool = False) -> str:
    """Delete files from the cross-MCP shared mount (SHARED_DIR, /shared
    inside the container) whose modification time is older than
    max_age_hours. Use this to clean up attachments that were staged for
    a Notion handoff once that handoff is complete.

    A host-level systemd user timer (mcp-shared-purge.timer) already
    runs a 24h TTL sweep daily, so this tool is mainly for
    agents that want to clean up proactively at the end of a workflow
    without waiting for the timer.

    Set dry_run=True to list victims without deleting. Only regular
    files are considered — subdirectories are left alone on principle
    (nothing currently creates them, but an unrelated tool could)."""
    return client.purge_shared_files(max_age_hours, dry_run)


@mcp.tool()
def drive_list(filter: str = "") -> str:
    """List files currently in the shared 'DRIVE_FOLDER_NAME' Drive folder.
    Shows filename, MIME type, size, modification time, and file ID.
    Optional filter matches filename substrings (case-insensitive)."""
    return client.drive_list(filter)


@mcp.tool()
def drive_download(name_or_id: str, return_base64: bool = False) -> str:
    """Download a file from the shared 'DRIVE_FOLDER_NAME' Drive folder.
    Accepts either a filename (within the folder) or a Drive file ID.
    Defaults to saving into ATTACHMENTS_DIR/drive/ on disk; set
    return_base64=True to return the file content inline as base64."""
    return client.drive_download(name_or_id, return_base64)


@mcp.tool()
def list_labels(account: str = "") -> str:
    """List all Gmail labels (system and user-created) for an account."""
    return client.list_labels(account)


@mcp.tool()
def modify_labels(
    message_id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    account: str = "",
) -> str:
    """Add or remove labels from a message. Pass label IDs as lists
    (e.g. add_labels=['STARRED'], remove_labels=['INBOX', 'UNREAD'])."""
    return client.modify_labels(
        message_id,
        add_labels=add_labels,
        remove_labels=remove_labels,
        account=account,
    )


@mcp.tool()
def list_attachments(
    message_id: str,
    account: str = "",
    exclude_inline: bool = False,
) -> str:
    """List all attachments on an email by message ID.

    Returns filenames, MIME types, sizes, and a disposition hint
    (``attachment`` vs ``inline``). Inline parts are typically
    embedded logos, signatures, or tracking pixels that the sender
    never intended as user-facing files.

    Set exclude_inline=True to skip them and only see real attachments —
    useful when sweeping a mailbox for invoices or contracts and you
    want to ignore template boilerplate images.

    Use download_attachment to save a file."""
    return client.list_attachments(message_id, account, exclude_inline)


@mcp.tool()
def batch_list_attachments(
    message_ids: list[str],
    account: str = "",
    exclude_inline: bool = False,
) -> str:
    """List attachments for many messages in one round trip.

    Accepts a list of message IDs and returns a JSON map of
    ``{message_id: [{filename, mime_type, size, disposition}, ...]}``.
    Messages with no matching attachments appear with an empty list.
    Per-message errors (e.g. a deleted message) surface as a string
    value instead of a list so one bad ID does not poison the batch.

    This collapses what would otherwise be N serial list_attachments
    calls — use it when you have a candidate set (e.g. exported from a
    Notion database) and want to find the subset that actually has
    files before doing real work. Combine with exclude_inline=True to
    filter out embedded tracking pixels and logos."""
    return client.batch_list_attachments(message_ids, account, exclude_inline)


@mcp.tool()
def download_attachment(
    message_id: str,
    filename: str,
    account: str = "",
    return_base64: bool = False,
    destination: str = "private",
    prefix: str = "",
    index: int = 0,
) -> str:
    """Download an attachment from an email and save it to disk.
    Use list_attachments first to see available files.

    destination:
      • 'private' (default) — saves under ATTACHMENTS_DIR/<message_id>/.
        Reference the result from drive_upload via
        local_filename='<message_id>/<filename>'.
      • 'shared' — saves into the shared mount (/shared in the container).
        Immediately visible to notion-mcp as source='shared:<filename>'
        on notion_upload_file / notion_add_file_to_row.

    return_base64=True bypasses disk entirely and returns inline base64;
    use sparingly — large files exceed MCP parameter limits.

    prefix: optional string prepended to the saved filename when
      destination='shared'. Use this to namespace attachments in
      parallel workflows where multiple messages may have the same
      filename (e.g. ``invoice.pdf``). A common choice is the message
      ID itself: ``prefix=f"{message_id}_"``. The atomic create under
      the hood still rebalances any residual collision by appending
      ``-2``, ``-3``, ... so concurrent workers are safe.

    index: when several attachments on the message share the same
      filename, select the 0-based Nth match (in attachment order, as
      shown by list_attachments). Defaults to 0 (first match). Out-of-range
      indices return an error listing how many copies exist.

    ## Cross-MCP pipeline
    The fastest way to attach a Gmail attachment to a Notion row is:
      1. download_attachment(..., destination='shared')
      2. notion_add_file_to_row(source='shared:<filename>', ...)
    No base64, no size limits."""
    return client.download_attachment(
        message_id, filename, account, return_base64, destination, prefix,
        index=index,
    )


@mcp.tool()
def extract_attachment_text(
    message_id: str,
    filename: str,
    account: str = "",
    pages: str | None = None,
    mode: str = "text",
    ocr: str = "auto",
    index: int = 0,
) -> str:
    """Extract text from a PDF attachment server-side without writing
    the bytes to disk or returning them over MCP. Designed for agents
    running in a sandbox that can't see the shared filesystem — use
    this to read invoice line items, contract clauses, etc. directly.

    Returns a JSON document:
      {
        "text": "<extracted text>",
        "page_count": <int, full document>,
        "pages_returned": [<1-indexed page numbers actually included>],
        "mode": "text" | "layout" | "tables",
        "truncated": <bool>,
        "account": "...",
        "filename": "..."
      }
    On errors (attachment missing, non-PDF file, inline part) the JSON
    has an "error" key instead.

    Parameters:
      pages: pdftotext-style range spec. Accepts None / "" (all pages),
        "3", "1-5", "1,3,5", or mixes like "1-3,7,10-12". Pages outside
        the document range are silently dropped; the effective set is
        echoed back in "pages_returned".
      mode: 'text' (default, flowing text via pdfplumber), 'layout'
        (preserves column layout — helps with structured invoices),
        or 'tables' (pdfplumber.extract_tables → markdown tables with
        '## Page N Table K' headers).

    Response-size ceiling: 200 KB of text. If the requested pages exceed
    that, the response is truncated on a page boundary and "truncated"
    is True — a trailing "...[truncated at page N, call again with
    pages=M-L]" marker tells you exactly how to fetch the rest.

    OCR (scanned / image-only PDFs) — delegated to the fleet xberg
    service (XBERG_BASE_URL env; pages rasterised locally, sent as PNGs):
      ocr='auto' (default) — any requested page whose native text comes
        back < 20 non-whitespace chars is OCR'd (tesseract backend,
        seconds/page). Real text pages are untouched. The response gains
        "ocr_used": true, "ocr_pages": [...], "ocr_engine": "xberg:...".
        If xberg is unreachable or fails, the native-text result still
        returns, with an "ocr_error" field explaining why.
      ocr='off'   — native extraction only (old behaviour).
      ocr='force' — OCR every requested page, skip native extraction
        (use when native text is garbled, e.g. broken font encodings).
      ocr='llm'   — transcribe every requested page with a vision model
        (xberg vlm backend → LiteLLM; env: XBERG_VLM_API_KEY, optional
        XBERG_VLM_MODEL / XBERG_VLM_BASE_URL). Best for handwriting and
        messy tables; slower and metered. OCR output is markdown-shaped —
        the 'tables'/'layout' modes only shape native extraction.

    index: when several attachments share the same filename (e.g. a vendor
      sends a receipt and an itemised invoice both named 'Invoice.pdf'),
      select the 0-based Nth match in attachment order. list_attachments
      flags duplicates and shows the index to pass. Defaults to 0.

    Non-PDF attachments are rejected. For structured text attachments
    (.txt, .csv), fetch bytes via download_attachment instead."""
    return client.extract_attachment_text(
        message_id, filename, account, pages, mode, ocr, index=index,
    )


@mcp.tool()
def render_attachment_page(
    message_id: str,
    filename: str,
    page: int,
    account: str = "",
    max_width: int = 1200,
    format: str = "jpeg",
    quality: int = 75,
    return_base64: bool = False,
    index: int = 0,
):
    # No return annotation on purpose: the tool returns either an MCP Image
    # (FastMCP converts it to an image content block at the protocol layer)
    # or a JSON string, and FastMCP 1.x tries — and fails — to build a
    # pydantic structured-output schema for any annotated non-str union
    # containing Image.
    """Render a single page of a PDF attachment as a viewable image.
    Use when extract_attachment_text isn't enough — charts, stamps,
    scanned pages, layouts, visual validation. The default return is a
    real MCP image content block, so you SEE the page directly — for a
    scanned document this is often all you need (no OCR round-trip).

    Set return_base64=True to instead get the legacy JSON envelope
    {"image_base64", "mime_type", "width", "height", "page",
    "page_count", "account", "filename"} — for agents that relay the
    bytes programmatically rather than viewing them.

    On errors (attachment missing, page out of range, non-PDF) a JSON
    string with an "error" key is returned in both modes.

    One page per call — bounds the payload. For a multi-page scan sweep,
    loop over pages externally, or use extract_attachment_text with
    ocr='auto' to get machine text for all pages in one call.

    Parameters:
      page:      1-indexed page number. Out-of-range -> error JSON.
      max_width: target pixel width (200..4000). Defaults to 1200 —
        dense but comfortably under MCP payload limits as JPEG.
      format:    'jpeg' (default, smaller) or 'png' (lossless).
      quality:   JPEG quality 1..100 (default 75). Ignored for PNG.
      index:     when several attachments share the same filename, select
        the 0-based Nth match (as shown by list_attachments). Defaults to 0."""
    if return_base64:
        return client.render_attachment_page(
            message_id=message_id,
            filename=filename,
            page=page,
            account=account,
            max_width=max_width,
            format=format,
            quality=quality,
            index=index,
        )
    raw = client.render_attachment_page_raw(
        message_id=message_id,
        filename=filename,
        page=page,
        account=account,
        max_width=max_width,
        format=format,
        quality=quality,
        index=index,
    )
    if "error" in raw:
        return json.dumps(raw, indent=2)
    return Image(
        data=raw["image_bytes"],
        format="jpeg" if raw["mime_type"] == "image/jpeg" else "png",
    )


@mcp.tool()
def list_shared_files(filter: str = "") -> str:
    """List files currently staged in the cross-MCP shared mount
    (/shared in the container). Matches what notion-mcp can see via
    source='shared:<filename>'. Useful to verify a download_attachment
    landed before telling notion-mcp to attach it. Optional substring
    filter on filename."""
    return client.list_shared_files(filter)


@mcp.tool()
def purge_file(filename: str) -> str:
    """Delete a single file from the shared mount by bare filename.
    Use after a successful cross-MCP handoff instead of waiting for the
    daily TTL purge (or calling purge_shared_files with max_age_hours=0,
    which would wipe unrelated in-flight handoffs). Rejects any
    filename containing path separators."""
    return client.purge_file(filename)


@mcp.tool()
def archive_email(message_id: str, account: str = "") -> str:
    """Archive an email (removes INBOX label)."""
    return client.archive_email(message_id, account)


@mcp.tool()
def mark_read(message_id: str, account: str = "") -> str:
    """Mark an email as read."""
    return client.mark_read(message_id, account)


@mcp.tool()
def mark_unread(message_id: str, account: str = "") -> str:
    """Mark an email as unread."""
    return client.mark_unread(message_id, account)


# ===================================================================
# Calendar tools — Google Calendar v3 via calendar.events scope.
# Every tool accepts an optional `account` that falls back to
# DEFAULT_ACCOUNT. Each Gmail account has its own calendar surface;
# the same refresh token grants gmail.modify + drive.file +
# calendar.events.
# ===================================================================


@mcp.tool()
def list_calendars(account: str = "", filter: str = "") -> str:
    """List calendars visible to the account (own + subscribed).
    Shows summary, access role, primary flag, and calendar ID.
    Optional substring filter matches summary or id."""
    return calendar.list_calendars(account, filter)


@mcp.tool()
def list_events(
    calendar_id: str = "primary",
    account: str = "",
    time_min: str = "",
    time_max: str = "",
    query: str = "",
    max_results: int = 25,
    single_events: bool = True,
    show_deleted: bool = False,
    order_by: str = "",
    page_token: str = "",
    event_types: str = "",
) -> str:
    """List events on a calendar.

    time_min / time_max: RFC3339 timestamps (e.g. '2026-04-14T00:00:00+10:00').
      Use these to window results — unbounded queries over busy calendars
      are expensive.
    query: free-text match on summary, description, location, attendees.
    single_events: True (default) expands recurring events into individual
      instances — almost always what you want.
    order_by: 'startTime' (only valid with single_events=True) or 'updated'.
      Defaults to 'startTime' when expanding instances.
    page_token: pass the next_page_token from a previous call to paginate.
    event_types: comma-separated filter — default, birthday, focusTime,
      outOfOffice, workingLocation. E.g. 'default' hides birthdays from a
      scheduling view. Empty (the default) returns every type; non-default
      types are tagged in the output either way.
    """
    return calendar.list_events(
        calendar_id=calendar_id,
        account=account,
        time_min=time_min,
        time_max=time_max,
        query=query,
        max_results=max_results,
        single_events=single_events,
        show_deleted=show_deleted,
        order_by=order_by,
        page_token=page_token,
        event_types=event_types,
    )


@mcp.tool()
def get_event(event_id: str, calendar_id: str = "primary", account: str = "") -> str:
    """Read a single event's full detail by ID, including attendees,
    description, recurrence rules, conference link (if any), and organizer.
    Use list_events first to find event IDs."""
    return calendar.get_event(event_id, calendar_id, account)


@mcp.tool()
def create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    account: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
    attendees: list[str] | None = None,
    recurrence: list[str] | None = None,
    send_updates: str = "none",
    reminders_minutes: list[int] | None = None,
) -> str:
    """Create a new calendar event.

    start / end: either 'YYYY-MM-DD' for an all-day event, or an RFC3339
      dateTime like '2026-04-14T15:00:00+10:00'. Mix and match is not
      supported — if start is a date, end must be a date too.
    timezone: IANA zone name (e.g. 'Australia/Sydney'). Applied to
      dateTime values only; ignored for all-day events. If your start/end
      strings already carry an offset, this is usually unnecessary.
    attendees: list of email addresses to invite.
    recurrence: list of RRULE/RDATE/EXDATE strings (e.g.
      ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR']).
    send_updates: 'all', 'externalOnly', or 'none' (default). Controls
      whether invite emails are dispatched to attendees.
    reminders_minutes: override default reminders with a list of
      popup-reminder offsets in minutes before start (e.g. [10, 60])."""
    return calendar.create_event(
        summary=summary,
        start=start,
        end=end,
        calendar_id=calendar_id,
        account=account,
        description=description,
        location=location,
        timezone=timezone,
        attendees=attendees,
        recurrence=recurrence,
        send_updates=send_updates,
        reminders_minutes=reminders_minutes,
    )


@mcp.tool()
def update_event(
    event_id: str,
    calendar_id: str = "primary",
    account: str = "",
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "",
    attendees: list[str] | None = None,
    recurrence: list[str] | None = None,
    send_updates: str = "none",
) -> str:
    """Patch an existing event. Only fields set to a non-None value are
    sent to the API — everything else is left unchanged.

    Clear semantics:
      • description='' clears the description
      • location='' clears the location
      • attendees=[] removes all attendees
      • recurrence=[] turns a recurring event into a one-off

    start/end and timezone follow the same rules as create_event."""
    return calendar.update_event(
        event_id=event_id,
        calendar_id=calendar_id,
        account=account,
        summary=summary,
        start=start,
        end=end,
        description=description,
        location=location,
        timezone=timezone,
        attendees=attendees,
        recurrence=recurrence,
        send_updates=send_updates,
    )


@mcp.tool()
def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    account: str = "",
    send_updates: str = "none",
) -> str:
    """Delete an event. send_updates controls whether attendees are
    notified of the cancellation ('all' / 'externalOnly' / 'none')."""
    return calendar.delete_event(event_id, calendar_id, account, send_updates)


@mcp.tool()
def quick_add_event(
    text: str, calendar_id: str = "primary", account: str = "",
    send_updates: str = "none",
) -> str:
    """Create an event from a natural-language phrase using Google's
    own parser (e.g. 'Dinner with Alice tomorrow 7pm'). Fast for simple
    single-timezone events; use create_event for precise control over
    start/end, attendees, or recurrence."""
    return calendar.quick_add_event(text, calendar_id, account, send_updates)


@mcp.tool()
def move_event(
    event_id: str,
    destination_calendar_id: str,
    source_calendar_id: str = "primary",
    account: str = "",
    send_updates: str = "none",
) -> str:
    """Move an event from one calendar to another (both calendars must
    be owned by the authenticated account)."""
    return calendar.move_event(
        event_id, destination_calendar_id, source_calendar_id, account, send_updates,
    )


@mcp.tool()
def respond_to_event(
    event_id: str,
    response: str,
    calendar_id: str = "primary",
    account: str = "",
    comment: str = "",
    send_updates: str = "none",
) -> str:
    """Set the authenticated account's RSVP status on an event.

    response: accepted / declined / tentative / needsAction. Also accepts
      casual aliases (yes / no / maybe / accept / decline).
    comment: optional free-text response comment visible to the organizer.
    send_updates: whether the organizer is notified of the response."""
    return calendar.respond_to_event(
        event_id, response, calendar_id, account, comment, send_updates,
    )


@mcp.tool()
def list_instances(
    event_id: str,
    calendar_id: str = "primary",
    account: str = "",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 50,
) -> str:
    """List the instances of a recurring event (the expanded single
    occurrences). Pass the recurring event's ID — get it from a
    list_events result where 'recurringEventId' points to it, or from
    the root event itself. Window with time_min/time_max."""
    return calendar.list_instances(
        event_id, calendar_id, account, time_min, time_max, max_results,
    )


@mcp.tool()
def free_busy(
    time_min: str,
    time_max: str,
    calendar_ids: list[str] | None = None,
    account: str = "",
    timezone: str = "",
) -> str:
    """Query the free/busy state for one or more calendars over a time
    window. Returns a list of busy blocks per calendar — nothing about
    event contents, just opaque 'busy' intervals. Use this for scheduling
    logic where you don't need event detail.

    calendar_ids: defaults to ['primary']. Accepts other calendars the
      account has access to.
    time_min / time_max: RFC3339 timestamps, required."""
    return calendar.free_busy(
        time_min, time_max, calendar_ids, account, timezone,
    )


@mcp.tool()
def create_calendar(
    summary: str, account: str = "", description: str = "", timezone: str = "",
) -> str:
    """Create a new secondary calendar owned by the account (e.g. a
    dedicated 'Family' or 'Travel' calendar). timezone is an IANA name
    like 'Australia/Sydney'. Returns the new calendar's ID for use as
    calendar_id in the event tools."""
    return calendar.create_calendar(summary, account, description, timezone)


@mcp.tool()
def update_calendar(
    calendar_id: str,
    account: str = "",
    summary: str | None = None,
    description: str | None = None,
    timezone: str | None = None,
) -> str:
    """Rename a calendar or change its description/timezone. Only
    non-None fields are changed; description='' clears it."""
    return calendar.update_calendar(
        calendar_id, account, summary, description, timezone,
    )


@mcp.tool()
def delete_calendar(calendar_id: str, account: str = "") -> str:
    """Permanently delete a SECONDARY calendar and every event on it.
    Irreversible — confirm with the user before calling. The primary
    calendar is refused (use delete_event for individual events)."""
    return calendar.delete_calendar(calendar_id, account)


# ===================================================================
# Tasks tools — Google Tasks v1 via the tasks scope.
# task_list defaults to '@default' (the account's default list); every
# tool accepts an optional `account` that falls back to DEFAULT_ACCOUNT.
# ===================================================================


@mcp.tool()
def list_task_lists(account: str = "", filter: str = "") -> str:
    """List the account's Google Tasks task lists (title + ID).
    Optional substring filter matches title or id. Most accounts only
    have the default list — tools accept '@default' for it, so you
    rarely need this unless the user keeps multiple lists."""
    return tasks.list_task_lists(account, filter)


@mcp.tool()
def list_tasks(
    task_list: str = "@default",
    account: str = "",
    show_completed: bool = True,
    show_hidden: bool = False,
    due_min: str = "",
    due_max: str = "",
    max_results: int = 50,
    page_token: str = "",
    show_assigned: bool = False,
) -> str:
    """List tasks on a task list.

    show_completed: include completed tasks (True by default). Completed
      tasks the user has cleared from the UI additionally need
      show_hidden=True to appear.
    due_min / due_max: window by due date — 'YYYY-MM-DD' or RFC3339.
      Tasks without a due date are excluded when either bound is set.
    page_token: pass the next_page_token from a previous call to paginate.
    show_assigned: also include tasks assigned to the account from Google
      Docs / Chat spaces (tagged in the output; the API can read/edit but
      not create assigned tasks).
    """
    return tasks.list_tasks(
        task_list=task_list,
        account=account,
        show_completed=show_completed,
        show_hidden=show_hidden,
        due_min=due_min,
        due_max=due_max,
        max_results=max_results,
        page_token=page_token,
        show_assigned=show_assigned,
    )


@mcp.tool()
def get_task(task_id: str, task_list: str = "@default", account: str = "") -> str:
    """Read a single task's full detail (title, status, due date, notes,
    parent, completion time). Use list_tasks first to find task IDs."""
    return tasks.get_task(task_id, task_list, account)


@mcp.tool()
def create_task(
    title: str,
    task_list: str = "@default",
    account: str = "",
    notes: str = "",
    due: str = "",
    parent: str = "",
    previous: str = "",
) -> str:
    """Create a task.

    due: 'YYYY-MM-DD' (or RFC3339 — but the Tasks API only stores the
      DATE; any time component is discarded by Google).
    parent: a task ID to create this as a subtask of.
    previous: a sibling task ID to insert after (controls list order)."""
    return tasks.create_task(
        title=title,
        task_list=task_list,
        account=account,
        notes=notes,
        due=due,
        parent=parent,
        previous=previous,
    )


@mcp.tool()
def update_task(
    task_id: str,
    task_list: str = "@default",
    account: str = "",
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    status: str | None = None,
) -> str:
    """Patch a task. Only fields set to a non-None value are sent —
    everything else is left unchanged.

    Clear semantics: notes='' clears the notes, due='' clears the due date.
    status: 'completed' or 'needsAction' (reopens a completed task)."""
    return tasks.update_task(
        task_id=task_id,
        task_list=task_list,
        account=account,
        title=title,
        notes=notes,
        due=due,
        status=status,
    )


@mcp.tool()
def complete_task(task_id: str, task_list: str = "@default", account: str = "") -> str:
    """Mark a task as completed. Shortcut for
    update_task(status='completed')."""
    return tasks.complete_task(task_id, task_list, account)


@mcp.tool()
def delete_task(task_id: str, task_list: str = "@default", account: str = "") -> str:
    """Permanently delete a task. To keep it with a done checkmark,
    use complete_task instead."""
    return tasks.delete_task(task_id, task_list, account)


# ===================================================================
# Contacts tools — People API v1 (contacts + contacts.other.readonly).
# Read/write on saved contacts since 2026-07-04; the "Other contacts"
# autocomplete pool stays read-only (Google offers no write scope for
# it) — promote entries into My Contacts with save_other_contact.
# ===================================================================


@mcp.tool()
def search_contacts(
    query: str,
    account: str = "",
    max_results: int = 10,
    include_other_contacts: bool = True,
) -> str:
    """Search the account's contacts by name, email, phone number, or
    organization (prefix matching). The go-to tool for resolving a
    person's name to an email address before drafting mail.

    include_other_contacts: also search Gmail's autocomplete pool —
      people the account has emailed but never saved. Those rows are
      tagged [other]. First search per account pauses ~2s to warm
      Google's search cache."""
    return contacts.search_contacts(query, account, max_results, include_other_contacts)


@mcp.tool()
def list_contacts(
    account: str = "",
    max_results: int = 50,
    page_token: str = "",
    sort_order: str = "LAST_MODIFIED_DESCENDING",
) -> str:
    """List saved contacts. Defaults to most-recently-modified first;
    sort_order also accepts 'FIRST_NAME_ASCENDING' / 'LAST_NAME_ASCENDING'.
    Prefer search_contacts when looking for a specific person —
    list_contacts is for browsing.
    page_token: pass the next_page_token from a previous call to paginate."""
    return contacts.list_contacts(account, max_results, page_token, sort_order)


@mcp.tool()
def get_contact(resource_name: str, account: str = "") -> str:
    """Read a contact's full detail (all emails, phones, organization,
    addresses, birthday, notes) by resource name — the 'people/c…' or
    'otherContacts/c…' ID from search_contacts / list_contacts results."""
    return contacts.get_contact(resource_name, account)


@mcp.tool()
def create_contact(
    given_name: str,
    family_name: str = "",
    account: str = "",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    organization: str = "",
    job_title: str = "",
    notes: str = "",
) -> str:
    """Create a new saved contact. Check search_contacts first to avoid
    duplicates — Google does not dedupe."""
    return contacts.create_contact(
        given_name=given_name,
        family_name=family_name,
        account=account,
        emails=emails,
        phones=phones,
        organization=organization,
        job_title=job_title,
        notes=notes,
    )


@mcp.tool()
def update_contact(
    resource_name: str,
    account: str = "",
    given_name: str | None = None,
    family_name: str | None = None,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    notes: str | None = None,
) -> str:
    """Update a saved contact ('people/c…' IDs only). Only non-None
    fields are written; emails/phones REPLACE the whole list (fetch with
    get_contact first when appending). Clear semantics: emails=[] /
    phones=[] empty the lists, notes='' clears notes, organization=''
    removes the org entry. Name/org parts merge with existing values.
    For an [other]-tagged contact, run save_other_contact first."""
    return contacts.update_contact(
        resource_name=resource_name,
        account=account,
        given_name=given_name,
        family_name=family_name,
        emails=emails,
        phones=phones,
        organization=organization,
        job_title=job_title,
        notes=notes,
    )


@mcp.tool()
def delete_contact(resource_name: str, account: str = "") -> str:
    """Delete a saved contact ('people/c…' IDs only; the [other] pool
    can't be deleted). Confirm with the user first — recovery is only
    via the Google Contacts web-UI bin (~30 days)."""
    return contacts.delete_contact(resource_name, account)


@mcp.tool()
def save_other_contact(resource_name: str, account: str = "") -> str:
    """Promote an 'otherContacts/c…' entry (an [other]-tagged
    search_contacts row — Gmail's autocomplete pool) into My Contacts.
    Copies name/emails/phones and returns the new 'people/c…' contact,
    which can then be edited with update_contact."""
    return contacts.save_other_contact(resource_name, account)


def main():
    import sys

    # Emit our per-request tool logs at INFO so they reach journald → Loki.
    # basicConfig writes to stderr, so this is safe for the stdio transport
    # too (stdout must stay pure JSON-RPC).
    logging.basicConfig(level=logging.INFO)

    if "--stdio" in sys.argv:
        # Local single-client mode: the MCP client spawns this process and
        # owns it — no network surface, so no bearer token or uvicorn.
        mcp.run(transport="stdio")
        return

    import uvicorn

    if not MCP_BEARER_TOKEN:
        raise SystemExit(
            "MCP_BEARER_TOKEN is not set. Refusing to start an unauthenticated "
            "MCP server. Set it in .secrets.env and restart."
        )
    # Streamable HTTP at /mcp, stateless: every tool call is an independent
    # POST, so dropped connections and container restarts never strand a
    # client session. (Legacy SSE was removed 2026-06 once all clients
    # migrated.)
    app = BearerAuthMiddleware(mcp.streamable_http_app(), expected_token=MCP_BEARER_TOKEN)
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

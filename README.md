# google-accounts-mcp

One [MCP](https://modelcontextprotocol.io) server for all your Google
accounts — **multi-account by design**. 51 tools across six surfaces:
Gmail, a shared Google Drive folder for file handoff, a cross-MCP shared
filesystem, per-account Google Calendar (incl. calendar management),
Google Tasks, and Google Contacts (read/write on saved contacts). Every
tool takes an `account` parameter; authorize as many Google accounts as
you like and address them by name or unique substring. Built on the
[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
(FastMCP); runs as a local stdio server or a containerized Streamable
HTTP service with bearer auth. Works with any MCP client.

> Unofficial project, not affiliated with or endorsed by Google.

## Table of Contents

- [Quick Start](#quick-start)
- [Tool Reference](#tool-reference)
  - [Account Management](#account-management)
  - [Search & Read](#search--read)
  - [Drafts & Sending](#drafts--sending)
  - [Labels & Lifecycle](#labels--lifecycle)
  - [Attachments](#attachments)
  - [Drive File Store](#drive-file-store)
  - [Calendar](#calendar)
  - [Tasks](#tasks)
  - [Contacts](#contacts)
- [Authentication](#authentication)
- [Multi-Account Model](#multi-account-model)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Development](#development)
- [Testing](#testing)
- [Local stdio Mode](#local-stdio-mode)
- [Container Deployment](#container-deployment)
- [MCP Client Registration](#mcp-client-registration)

## Quick Start

One-time Google Cloud setup: create (or pick) a GCP project, enable the
**Gmail, Google Drive, Google Calendar, Google Tasks, and People** APIs,
and create an **OAuth client ID** of type *Desktop app* (APIs & Services →
Credentials). That client ID/secret is what `authorize.py` uses for the
local browser consent flow.

```bash
# Prerequisites: Python 3.12+, uv
uv sync

export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
export TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db

# Authorize one or more Google accounts (opens a browser per account;
# stores refresh tokens in the SQLite DB, chmod 600)
uv run scripts/authorize.py you@example.com second@gmail.com

# Run over stdio (what most MCP clients spawn)
uv run google-accounts-mcp --stdio

# ...or as an HTTP server (requires MCP_BEARER_TOKEN)
MCP_BEARER_TOKEN=... uv run google-accounts-mcp
# Streamable HTTP at http://0.0.0.0:8321/mcp
```

## Tool Reference

Every tool accepts an optional `account` parameter. When omitted it falls
back to `DEFAULT_ACCOUNT` (configured via env var). Partial account matches
are resolved automatically if unambiguous.

### Account Management

| Tool | Parameters | Description |
|---|---|---|
| `list_accounts` | `filter: str = ""` | List all authorized Gmail accounts. Optional substring filter. |
| `list_labels` | `account: str = ""` | List system and user labels for an account. |

### Search & Read

| Tool | Parameters | Description |
|---|---|---|
| `search_email` | `query`, `account = ""`, `max_results = 10`, `message_ids: list[str] \| None = None` | Search with Gmail query syntax (e.g. `is:unread from:alice@example.com`). When `message_ids` is supplied, the tool switches to pre-filter mode: it iterates the given IDs instead of calling Gmail's search API and keeps only the ones matching `query`. Currently only `has:attachment` is honored as a local predicate (other query terms are ignored in this mode). Useful when you already have a candidate set — e.g. exported from a Notion database — and want to find the subset with attachments without N blind round-trips. |
| `read_email` | `message_id`, `account = ""` | Fetch a full message: headers + text/plain body (falls back to stripped HTML). |
| `read_thread` | `thread_id`, `account = ""` | Fetch every message in a thread. |

### Drafts & Sending

Both `draft_email` and `send_email` take the same parameters:

| Parameter | Type | Description |
|---|---|---|
| `to` | `list[str]` | Recipient addresses. |
| `subject` | `str` | Subject line. |
| `body` | `str` | Plain-text body. |
| `account` | `str` | Sending account. Defaults to `DEFAULT_ACCOUNT`. |
| `cc` | `list[str]` | CC recipients. |
| `bcc` | `list[str]` | BCC recipients. |
| `reply_to_message_id` | `str` | Turns the message into a reply — stamps `In-Reply-To` / `References` and sets `threadId`. |
| `attachments` | `list[str]` | Unified source references — see below. |

Each `attachments` entry is a `scheme:value` string:

- `local:<path>` — file under `ATTACHMENTS_DIR`. Path may be relative
  (e.g. `local:uploads/report.pdf` after `upload_file`) or reference a
  previously downloaded attachment (`local:<message_id>/<filename>`).
  Paths outside `ATTACHMENTS_DIR` are rejected.
- `shared:<filename>` — file on the cross-MCP shared mount (`/shared`),
  e.g. staged there by notion-mcp's
  `notion_download_file(destination='shared')`. Bare filenames only;
  verify staging with `list_shared_files`.
- `drive:<name-or-id>` — file in the shared Drive
  folder. Tries name lookup first, falls back to treating the value as a
  Drive file ID; in either case the file must live inside the shared
  folder.

```python
# Example
send_email(
    to=["alice@example.com"],
    subject="Q2 report",
    body="See attached.",
    attachments=["local:uploads/q2.pdf", "drive:charts.xlsx"],
)
```

`draft_email` writes to the Drafts folder; `send_email` dispatches immediately
(use with care).

### Shared-Store Semantics

The shared mount at `/shared` (host path
`~/.local/share/containers/data/mcp-shared/`) is **read/write from both
MCP servers** and has two invariants worth knowing:

- **Filename clashes auto-rename, atomically.** If `download_attachment`
  is asked to write `invoice.pdf` into shared storage and a file with
  that name already exists, the new one lands at `invoice-2.pdf`
  (`invoice-3.pdf`, etc.). The create uses `O_CREAT | O_EXCL` so two
  concurrent writers never clobber each other, even without a prefix.
  The return value always reports the actual saved filename and the
  `source='shared:<actual-name>'` string to pass to notion-mcp, so the
  agent never has to guess.
- **Explicit namespacing via `prefix`.** In batch workflows where
  multiple messages may legitimately carry the same filename (e.g. 20
  different senders whose attachment is `invoice.pdf`), the auto-rename
  is safe but ugly. Pass `prefix=f"{message_id}_"` to
  `download_attachment` and the files land as `m1_invoice.pdf`,
  `m2_invoice.pdf`, ... instead of `invoice.pdf`, `invoice-2.pdf`,
  `invoice-3.pdf`, which is much easier to reason about when
  correlating back to the source message.
- **24h TTL.** Files in shared storage are purged 24 hours after their
  last modification by the `mcp-shared-purge.timer` user unit on the
  host. This is a safety net for forgotten handoffs, not a backup —
  stage to Notion (or rename out of the shared dir) within that
  window. Agents that want immediate cleanup after a workflow can
  call the `purge_shared_files` tool (see below).

### Cross-MCP File Handoff to notion-mcp

`google-accounts-mcp` and `notion-mcp` share a volume (`/shared` in both
containers, host path `~/.local/share/containers/data/mcp-shared/`) so
files move between servers without base64-through-MCP — in **both
directions**. Every transfer tool reports the `sha256` of the bytes it
moved, so an agent can verify integrity end-to-end without shell
access. The canonical pipeline for attaching a Gmail attachment to a
Notion row:

```python
download_attachment(
    message_id="19a1b2...",
    filename="invoice.pdf",
    destination="shared",              # writes SHARED_DIR/invoice.pdf
)
# Then from notion-mcp:
notion_add_file_to_row(
    page_id="...",
    source="shared:invoice.pdf",       # reads the same bytes
    files_property="Attachments",
)
```

And the reverse — emailing a file stored in Notion:

```python
# From notion-mcp:
notion_download_file(
    block_id="...",                    # from notion_list_files_on_page
    destination="shared",              # writes SHARED_DIR/<name>
)
# Then from this server:
draft_email(
    to=["alice@example.com"],
    subject="Contract",
    body="Attached.",
    attachments=["shared:contract.pdf"],
)
```

No size limit — the file bytes never traverse MCP parameters. For
cases where Drive persistence is also wanted, use
`drive_upload(local_filename=...)` instead of passing `content_base64`
so the bytes stay on disk end-to-end.

### Labels & Lifecycle

| Tool | Parameters | Description |
|---|---|---|
| `modify_labels` | `message_id`, `add_labels: list[str] = []`, `remove_labels: list[str] = []`, `account = ""` | Add/remove label IDs. |
| `archive_email` | `message_id`, `account = ""` | Removes `INBOX` label. |
| `mark_read` | `message_id`, `account = ""` | Removes `UNREAD`. |
| `mark_unread` | `message_id`, `account = ""` | Adds `UNREAD`. |

```python
modify_labels("msg-id", add_labels=["STARRED"], remove_labels=["INBOX", "UNREAD"])
```

### Attachments

Outgoing attachments must live under `ATTACHMENTS_DIR` — the server refuses
paths outside it to prevent exfiltration. Use `upload_file` to stage a new
file or reference a previously downloaded attachment path.

**Reading PDF attachments from a sandboxed agent.** An MCP client running
in a sandbox VM typically cannot see `ATTACHMENTS_DIR` or `SHARED_DIR` on
this server, and `return_base64=True` on a multi-MB PDF blows past most
MCP clients' parameter-size ceilings. Use `extract_attachment_text` to
pull structured text (with a 200 KB response ceiling and a `pages=` range
selector for anything bigger) and `render_attachment_page` for one-page
bitmaps when text extraction isn't enough.

| Tool | Parameters | Description |
|---|---|---|
| `upload_file` | `filename`, `content_base64` | Stages a file under `ATTACHMENTS_DIR/uploads/`. Filename must be bare (no path components). Collisions auto-rename (`-2`, `-3`, ...). Returns the ready-to-use `local:uploads/<name>` attachment reference and the `sha256` of the saved bytes. |
| `list_attachments` | `message_id`, `account = ""`, `exclude_inline = False` | Enumerate attachments on a message. Every entry carries a `disposition` hint (`attachment` vs `inline`). Set `exclude_inline=True` to skip embedded logos and tracking pixels — the default keeps them visible so you can still see what the email contains. Disposition is detected from the `Content-Disposition` header and falls back to `Content-ID` presence. **When the same filename appears more than once** (e.g. a vendor sends a receipt and an itemised invoice both named `Invoice.pdf`), each duplicate row is flagged with the 0-based `index=N` to pass to the fetch tools below. |
| `batch_list_attachments` | `message_ids: list[str]`, `account = ""`, `exclude_inline = False` | Batch variant of `list_attachments` — accepts a list of message IDs and returns a JSON map `{id: [{filename, mime_type, size, disposition}, ...]}`. Collapses N serial `list_attachments` calls into one when pre-filtering a candidate set (e.g. "of these 400 rows, which have real PDFs"). Per-message errors surface as a string value on the affected ID so one bad message doesn't poison the batch. |
| `download_attachment` | `message_id`, `filename`, `account = ""`, `return_base64 = False`, `destination = "private"`, `prefix = ""`, `index = 0` | `destination='private'` (default) saves under `ATTACHMENTS_DIR/<message_id>/`. `destination='shared'` saves under `SHARED_DIR` (the cross-MCP mount, `/shared` in the container) so `notion-mcp` can pick it up via `source='shared:<filename>'`. `return_base64=True` bypasses disk entirely; use only for small files. `prefix` is prepended to the saved filename for namespacing in parallel workflows — a common choice is `prefix=f"{message_id}_"` so two messages with `invoice.pdf` don't collide. Atomic create under the hood (`O_EXCL`) means concurrent callers are safe even without a prefix. `index` (0-based) selects among attachments **sharing the same filename** on one message — default `0` is the first match; out-of-range returns an error listing how many copies exist. |
| `extract_attachment_text` | `message_id`, `filename`, `account = ""`, `pages: str \| None = None`, `mode = "text"`, `ocr = "auto"`, `index = 0` | Extract text from a PDF attachment server-side — returns JSON with `{text, page_count, pages_returned, mode, truncated}` (+ `ocr_used`/`ocr_pages`/`ocr_engine` when OCR ran). Designed for agents in a sandbox VM that can't reach `SHARED_DIR` or `ATTACHMENTS_DIR` and hit MCP parameter-size limits on `return_base64=True`. `pages` accepts `pdftotext`-style specs (`"3"`, `"1-5"`, `"1,3,5"`, `"1-3,7"`). `mode='text'` is flowing text, `'layout'` preserves columns (pdfplumber `layout=True`), `'tables'` renders `extract_tables()` output as pipe-delimited markdown tables. Response-size ceiling is 200 KB — truncation happens on a page boundary with a trailing marker telling you which pages to fetch next. Non-PDF attachments are rejected. **OCR** (see [PDF OCR](#pdf-ocr)): `ocr='auto'` (default) OCRs any requested page with < 20 chars of native text via RapidOCR; `'off'` disables; `'force'` OCRs every page; `'llm'` transcribes pages with a vision model via the LiteLLM gateway. `index` (0-based) targets a specific copy when the filename is duplicated on the message. |
| `render_attachment_page` | `message_id`, `filename`, `page`, `account = ""`, `max_width = 1200`, `format = "jpeg"`, `quality = 75`, `return_base64 = False`, `index = 0` | Render a single PDF page via `pypdfium2` — no poppler dependency. **Default return is a real MCP image content block, so the calling model sees the page directly** (scans, charts, stamps — often no OCR needed at all). `return_base64=True` returns the legacy JSON `{image_base64, mime_type, width, height, page, page_count}` for programmatic relaying. One page per call bounds the payload. `max_width` is clamped 200..4000; JPEG at default width+quality lands around 100-200 KB. `index` (0-based) targets a specific copy when the filename is duplicated on the message. |
| `list_shared_files` | `filter: str = ""` | List files currently staged in the cross-MCP shared mount, with size and `sha256` per file. Mirrors what `notion-mcp` sees via `source='shared:<name>'`. Useful to verify a handoff landed (and its integrity) before telling `notion-mcp` to attach it. |
| `purge_file` | `filename` | Delete a single bare-filename file from `SHARED_DIR`. Traversal-safe. Use after a successful handoff instead of waiting for the 24h TTL sweep (which would wipe unrelated in-flight work if you called `purge_shared_files` with `max_age_hours=0`). |

### PDF OCR

Scanned / image-only PDFs have no text layer, so native extraction returns
nothing. `extract_attachment_text` handles this with three reading tiers
(implemented in `src/google_accounts_mcp/pdf_read.py`, duplicated verbatim
in the sibling notion-mcp project — edit both copies together). OCR is
delegated to an [xberg](https://github.com/xberg-io/xberg) server — pages are
rasterised locally (pypdfium2) and uploaded as PNGs in one multipart
`POST /extract`:

| Tier | Engine | When |
|---|---|---|
| A — native text | pdfplumber (local) | Always first (except `ocr='force'/'llm'`) |
| B — OCR | xberg `tesseract` backend (paddle-ocr selectable via env but takes minutes/dense page on CPU — measured 2026-08-03) | `ocr='auto'` on pages with < 20 chars of native text, or `ocr='force'` |
| C — VLM OCR | xberg `vlm` backend → OpenAI-compatible vision endpoint (e.g. a LiteLLM gateway) | `ocr='llm'` — handwriting, messy tables, low-quality scans |

In `ocr='auto'`, an OCR failure (xberg down, extraction error) never breaks
extraction — the native-text result is returned with an `ocr_error` field
instead. `ocr='force'`/`'llm'` propagate the error.

Env (set per deployment):

| Var | Meaning | Default |
|---|---|---|
| `XBERG_BASE_URL` | xberg endpoint | `http://xberg:8000` (container DNS; point it at your xberg server) |
| `XBERG_TIMEOUT` | Request timeout (s) | `120` |
| `XBERG_OCR_BACKEND` | Classical backend for tier B | `tesseract` |
| `XBERG_VLM_MODEL` | Gateway model alias, passed verbatim | `cheap` |
| `XBERG_VLM_BASE_URL` | Vision endpoint, resolved by the xberg *server* | `http://litellm:4000/v1` |
| `XBERG_VLM_API_KEY` | Vision-endpoint API key, sent in the per-request `vlm_config` | *(unset — `ocr='llm'` errors with setup pointer)* |

The vlm key rides per-request because xberg 1.0.8 skips provider-env key
resolution whenever `vlm_config.base_url` is overridden (verified
2026-08-03) — revisit server-side key placement if upstream fixes that.

For one-off visual questions, skip OCR entirely: `render_attachment_page`
returns a real MCP image content block by default, so the calling model
just looks at the page.

### Drive File Store

A shared Google Drive folder (`DRIVE_FOLDER_NAME` on `DRIVE_ACCOUNT`) acts as
a persistent file store for attachments that outlive a single server restart.
Files uploaded via `drive_upload` can be passed to `draft_email` /
`send_email` via the `drive_attachments` parameter — even from mailboxes
other than the Drive-owning account.

| Tool | Parameters | Description |
|---|---|---|
| `drive_upload` | `filename`, `content_base64 = ""`, `local_filename = ""` | Upload to the shared folder. Returns file ID + webViewLink. Provide exactly one of `content_base64` (raw base64, subject to MCP parameter size limits) or `local_filename` (reference an existing file — accepts `<message_id>/<name>` under `ATTACHMENTS_DIR` or a bare `<name>` under `SHARED_DIR`). `local_filename` is required for anything larger than ~20 KB because MCP parameter encoding truncates big base64 blobs. |
| `purge_shared_files` | `max_age_hours = 24.0`, `dry_run = False` | Delete files from `SHARED_DIR` older than `max_age_hours`. `dry_run=True` lists victims without deleting. Regular files only — subdirectories are left alone. Pair with a host-side cron/timer TTL sweep if you want automatic cleanup; use when an agent wants to clean up immediately after a workflow. |
| `drive_list` | `filter: str = ""` | List files in the folder. Filter matches filename substrings. |
| `drive_download` | `name_or_id`, `return_base64 = False` | Save to `ATTACHMENTS_DIR/drive/` (collision auto-rename) or return inline base64. Returns `sha256` and the `local:drive/<name>` attachment reference. Google-native files (Docs/Sheets) are refused — export first. |

### Calendar

Per-account Google Calendar read/write via the full `calendar` OAuth
scope (since 2026-07-04; events + calendarList + calendar management). Every Gmail account has
its own calendar surface — the same `account` parameter used by Gmail
tools also selects which calendar you operate on. Existing accounts
must re-run `authorize.py` after a scope change, since the OAuth consent
is fixed at grant time (old refresh tokens return `insufficientPermissions`
on calendar calls).

Why two scopes: `calendar.events` covers every `events/*` endpoint
(list / get / insert / patch / delete / move / quickAdd / instances /
freebusy), but `calendarList` is a separate surface with its own scope.
Adding `calendar.calendarlist.readonly` is the narrowest way to give
the `list_calendars` tool what it needs — still strictly less permissive
than the full `calendar` scope (no ACL changes, no calendar
create/delete, no settings).

**Time-value model**: a bare `YYYY-MM-DD` string makes an all-day event;
anything else is treated as an RFC3339 dateTime (`2026-04-14T15:00:00+10:00`
or `2026-04-14T15:00:00` + an explicit `timezone` IANA name). The
`timezone` parameter is silently dropped for date-only values because
Google Calendar rejects `timeZone` on all-day events. When your dateTime
already carries an offset, `timezone` is optional.

| Tool | Parameters | Description |
|---|---|---|
| `list_calendars` | `account = ""`, `filter = ""` | List calendars visible to the account (own + subscribed). Shows summary, access role, primary flag, and calendar ID. |
| `list_events` | `calendar_id = "primary"`, `account = ""`, `time_min = ""`, `time_max = ""`, `query = ""`, `max_results = 25`, `single_events = True`, `show_deleted = False`, `order_by = ""`, `page_token = ""` | List events on a calendar. `time_min` / `time_max` are RFC3339 timestamps. `query` is Google's free-text match on summary, description, location, attendees. `single_events=True` (the default) expands recurring events into individual instances and forces `orderBy=startTime`. Paginate via the `next_page_token` printed at the bottom of the result. |
| `get_event` | `event_id`, `calendar_id = "primary"`, `account = ""` | Read one event's full detail — attendees + response status, description, recurrence rules, conference link (if any), organizer. |
| `create_event` | `summary`, `start`, `end`, `calendar_id = "primary"`, `account = ""`, `description = ""`, `location = ""`, `timezone = ""`, `attendees: list[str] \| None = None`, `recurrence: list[str] \| None = None`, `send_updates = "none"`, `reminders_minutes: list[int] \| None = None` | Create a new event. `attendees` is a list of email addresses. `recurrence` is a list of RRULE/RDATE/EXDATE strings (e.g. `['RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR']`). `send_updates` controls whether invite emails are dispatched (`'all'`, `'externalOnly'`, `'none'`). `reminders_minutes` overrides the default reminders with one popup per offset (e.g. `[10, 60]`). |
| `update_event` | `event_id`, `calendar_id = "primary"`, `account = ""`, plus any of `summary`, `start`, `end`, `description`, `location`, `timezone`, `attendees`, `recurrence`, `send_updates` | PATCH semantics — only fields explicitly set to a non-None value are sent. `description=""` clears the description; `attendees=[]` removes all attendees; `recurrence=[]` turns a recurring event into a one-off. |
| `delete_event` | `event_id`, `calendar_id = "primary"`, `account = ""`, `send_updates = "none"` | Delete an event. `send_updates` controls cancellation notifications. |
| `quick_add_event` | `text`, `calendar_id = "primary"`, `account = ""`, `send_updates = "none"` | Create an event from a natural-language phrase using Google's own parser (e.g. `'Dinner with Alice tomorrow 7pm'`). Fast for simple events; use `create_event` for precise control. |
| `move_event` | `event_id`, `destination_calendar_id`, `source_calendar_id = "primary"`, `account = ""`, `send_updates = "none"` | Move an event from one calendar to another (both owned by the account). |
| `respond_to_event` | `event_id`, `response`, `calendar_id = "primary"`, `account = ""`, `comment = ""`, `send_updates = "none"` | Set the account's RSVP. `response` accepts `accepted` / `declined` / `tentative` / `needsAction` and casual aliases (`yes` / `no` / `maybe` / `accept` / `decline`). If the account is not yet an attendee, it's appended as one. |
| `list_instances` | `event_id`, `calendar_id = "primary"`, `account = ""`, `time_min = ""`, `time_max = ""`, `max_results = 50` | Expand a recurring event into its individual instances. Window with `time_min` / `time_max`. |
| `create_calendar` | `summary`, `account = ""`, `description = ""`, `timezone = ""` | Create a secondary calendar (e.g. 'Family'). Returns its ID for use as `calendar_id`. |
| `update_calendar` | `calendar_id`, `account = ""`, plus any of `summary`, `description`, `timezone` | Rename a calendar / change metadata. PATCH semantics; `description=""` clears. |
| `delete_calendar` | `calendar_id`, `account = ""` | Permanently delete a SECONDARY calendar and all its events. The primary calendar is refused. |
| `free_busy` | `time_min`, `time_max`, `calendar_ids: list[str] \| None = None`, `account = ""`, `timezone = ""` | Query opaque busy-block intervals across one or more calendars (defaults to `['primary']`). Returns per-calendar busy lists without event content — use for scheduling logic that doesn't need detail. |

```python
# Create a timed event with attendees and a popup reminder
create_event(
    summary="Architecture review",
    start="2026-04-15T10:00:00+10:00",
    end="2026-04-15T11:00:00+10:00",
    account="alice@example.com",
    attendees=["alice@example.com", "bob@example.com"],
    reminders_minutes=[10],
    send_updates="all",
)

# Window query
list_events(
    account="alice@example.com",
    time_min="2026-04-14T00:00:00+10:00",
    time_max="2026-04-15T00:00:00+10:00",
    query="standup",
)

# RSVP to an invite
respond_to_event(
    event_id="abc123",
    response="yes",
    account="alice@example.com",
    comment="Running 5 min late",
)
```

### Tasks

Per-account Google Tasks read/write via the `tasks` OAuth scope (the only
write scope Google offers for Tasks — there is no narrower option).
`task_list` defaults to `@default`, the API alias for the account's default
list, so single-list users never need `list_task_lists`.

**Due-date model**: the Tasks API stores only a DATE — any time component
in an RFC3339 value is discarded server-side. Tools accept a bare
`YYYY-MM-DD` and expand it to midnight UTC for the API.

| Tool | Parameters | Description |
|---|---|---|
| `list_task_lists` | `account = ""`, `filter = ""` | List the account's task lists (title + ID). Optional substring filter. |
| `list_tasks` | `task_list = "@default"`, `account = ""`, `show_completed = True`, `show_hidden = False`, `due_min = ""`, `due_max = ""`, `max_results = 50`, `page_token = ""` | List tasks. Completed tasks the user has cleared from the UI additionally need `show_hidden=True`. `due_min`/`due_max` window by due date but exclude tasks without one. Paginate via the printed `next_page_token`. |
| `get_task` | `task_id`, `task_list = "@default"`, `account = ""` | One task's full detail — title, status, due, notes, parent, completion time. |
| `create_task` | `title`, `task_list = "@default"`, `account = ""`, `notes = ""`, `due = ""`, `parent = ""`, `previous = ""` | Create a task. `parent` makes it a subtask; `previous` inserts after a sibling task ID (list ordering). |
| `update_task` | `task_id`, `task_list = "@default"`, `account = ""`, plus any of `title`, `notes`, `due`, `status` | PATCH semantics — only non-None fields are sent. `notes=""` clears notes; `due=""` clears the due date (sent as JSON null). `status` is `'completed'` or `'needsAction'` (reopening also clears the completion timestamp). |
| `complete_task` | `task_id`, `task_list = "@default"`, `account = ""` | Mark completed — sugar for the most common mutation. |
| `delete_task` | `task_id`, `task_list = "@default"`, `account = ""` | Permanently delete (vs. `complete_task`, which keeps it checked off). |

### Contacts

People API lookup + read/write on saved contacts (`contacts` +
`contacts.other.readonly` scopes; write support added 2026-07-04). The
second scope covers Google's "Other contacts" pool (people the account
has emailed but never saved — the Gmail autocomplete list), searched by
default and tagged `[other]` in results. That pool is read-only at the
API level; the supported write path is `save_other_contact`, which
copies an entry into My Contacts where it becomes editable.

**Search-cache warmup**: Google's contact search reads from a
lazily-populated cache; the first search per account per process issues a
warmup request and pauses ~2 s (per Google's documented guidance) before
the real query. Subsequent searches are immediate.

| Tool | Parameters | Description |
|---|---|---|
| `search_contacts` | `query`, `account = ""`, `max_results = 10`, `include_other_contacts = True` | Prefix-match search over names, emails, phone numbers, and organizations, across saved + other contacts. The go-to tool for "what's Alice's address?". |
| `list_contacts` | `account = ""`, `max_results = 50`, `page_token = ""`, `sort_order = "LAST_MODIFIED_DESCENDING"` | Browse saved contacts. `sort_order` also accepts `FIRST_NAME_ASCENDING` / `LAST_NAME_ASCENDING`. Paginate via the printed `next_page_token`. |
| `get_contact` | `resource_name`, `account = ""` | Full detail (all emails, phones, org, addresses, birthday, notes) by `people/c…` or `otherContacts/c…` ID from search/list results. Other-contact IDs are transparently re-prefixed for the `people.get` endpoint. |

```python
# Resolve a name before drafting
search_contacts(query="alice", account="alice@example.com")

# Capture a follow-up from an email thread
create_task(
    title="Reply to Alice re: contract",
    due="2026-07-07",
    notes="thread: <message-id>",
    account="alice@example.com",
)
```

## Authentication

**Bearer auth (HTTP mode only):** the HTTP server refuses to start without
`MCP_BEARER_TOKEN`. Every request (except `/.well-known/*` discovery
probes) must carry an `Authorization: Bearer <token>` header. Local stdio
mode (`--stdio`) has no network surface and skips bearer auth entirely.

**Google OAuth 2.0:** each Gmail account is authorized once via
`scripts/authorize.py`, which runs the installed-app OAuth flow and stores
the resulting refresh token in a SQLite DB (`tokens.db`). Access tokens are
refreshed on demand by the google-auth library — no background refresher.
If you run both a container deployment and local stdio copies, remember
the DB is a per-machine file: re-authorizing means copying the refreshed
`tokens.db` to each deployment (and restarting the container so cached
service objects drop stale credentials).

Scopes (`authorize.py` requests the full set on every account — a scope
addition therefore requires a one-time re-auth of each existing account,
since consent is fixed at grant time):

- `https://www.googleapis.com/auth/gmail.modify` — read/write/label on all
  authorized mailboxes.
- `https://www.googleapis.com/auth/drive.file` — only files the app creates
  (i.e. the shared `DRIVE_FOLDER_NAME` folder). Although granted
  everywhere, the drive tools only ever operate against `DRIVE_ACCOUNT`.
- `https://www.googleapis.com/auth/calendar` — full calendar scope
  (2026-07-04; replaced the narrower `calendar.events` +
  `calendar.calendarlist.readonly` pair when calendar management —
  create/update/delete calendar — was added; see [Calendar](#calendar)).
- `https://www.googleapis.com/auth/tasks` — Google Tasks read/write (no
  narrower write scope exists).
- `https://www.googleapis.com/auth/contacts` +
  `https://www.googleapis.com/auth/contacts.other.readonly` — read/write
  on saved contacts (2026-07-04; was `contacts.readonly`) plus read-only
  access to the "Other contacts" autocomplete pool (Google offers no
  write scope for that pool — promote entries with `save_other_contact`).

## Multi-Account Model

All tools accept `account` as an optional parameter. Resolution:

1. Empty → `DEFAULT_ACCOUNT` if set, else the sole authorized account
   (a clear error lists the options when several exist).
2. Exact match against stored accounts.
3. Case-insensitive substring match — if unique, used; if ambiguous, raises.

After `scripts/authorize.py` completes you must restart the container so the
in-memory service objects pick up the new credentials.

## Configuration

All env vars are optional unless noted. Path defaults assume the
container layout (`/data`); set them explicitly for local runs.

| Variable | Default | Description |
|---|---|---|
| `MCP_BEARER_TOKEN` | _(required in HTTP mode)_ | Bearer token clients must present. The HTTP server refuses to start if unset; stdio mode doesn't use it. |
| `GOOGLE_CLIENT_ID` | _(required)_ | OAuth 2.0 client ID. |
| `GOOGLE_CLIENT_SECRET` | _(required)_ | OAuth 2.0 client secret. |
| `TOKEN_DB_PATH` | `/data/tokens.db` | SQLite DB holding per-account refresh tokens. |
| `GOOGLE_AUTH_URL` | *(unset)* | Optional [google-auth](#token-broker-mode-optional) token broker base URL. When set, tokens come from the broker and `TOKEN_DB_PATH` / `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` are unused. |
| `GOOGLE_AUTH_BEARER` | *(unset)* | This server's bearer for the broker. Required when `GOOGLE_AUTH_URL` is set. |
| `ATTACHMENTS_DIR` | `/data/attachments` | Root for staged attachments and downloaded files. |
| `DEFAULT_ACCOUNT` | *(empty)* | Account used when a tool's `account` parameter is empty. Empty falls back to the sole authorized account. |
| `DRIVE_ACCOUNT` | *(empty)* | Account hosting the shared Drive folder. Required only for the `drive_*` tools. |
| `DRIVE_FOLDER_NAME` | `mcp-google-accounts` | Name of the shared Drive folder. |
| `PORT` | `8321` | HTTP port the server listens on. |

### Token broker mode (optional)

By default the server keeps its own refresh tokens (`TOKEN_DB_PATH`, written by
`scripts/authorize.py`) and needs the OAuth client id and secret. If you run a
google-auth token broker, set `GOOGLE_AUTH_URL` and `GOOGLE_AUTH_BEARER`
instead: the server then holds no refresh tokens or client secret and asks the
broker for short-lived access tokens. In that mode the account list is whatever
the broker shares with this server, accounts are added and re-authorised in the
broker, and a re-authorisation takes effect without restarting this server.

## Architecture

```
┌─────────────────┐   HTTP + Bearer    ┌──────────────────┐
│  Claude Code /  │ ─────────────────▶ │ google-accounts- │
│  VS Code / etc  │                    │  mcp (FastMCP)   │
└─────────────────┘                    └────────┬─────────┘
                                                │
                              ┌─────────────────┼─────────────────┐
                              ▼                 ▼                 ▼
                       ┌─────────────┐  ┌─────────────┐  ┌───────────────┐
                       │  Gmail API  │  │  Drive API  │  │  tokens.db    │
                       │  (per acct) │  │  (1 acct)   │  │  (SQLite)     │
                       └─────────────┘  └─────────────┘  └───────────────┘
```

Key design points:

- **SQLite token store** (`auth.py`) — one row per account keyed by email,
  holding the OAuth refresh token. Opened fresh per query; no long-lived
  sqlite connection.
- **Lazy service cache** — googleapiclient `Resource` objects are built on
  first use per account and cached in-memory for the life of the process.
  Credentials auto-refresh via `AuthorizedHttp`.
- **Threadpool tool offload** (`server.py`) — the MCP SDK runs synchronous
  `@mcp.tool()` handlers *inline on the event loop*, so a single blocking
  `httplib2` call would freeze every other request (the cause of the
  4-minute "server unresponsive" stalls). `mcp.tool` is wrapped so each sync
  tool is registered as an async wrapper that runs the body in a worker
  thread (`anyio.to_thread.run_sync`); the loop stays free for concurrent and
  cheap calls. The wrapper preserves the tool signature (so client schemas
  are unchanged — no restart needed) and returns the original sync function
  as the module name (so tool-to-tool calls and tests still work). Cached
  `httplib2` objects aren't thread-safe, so `_RetryingHttp` serialises one
  account's socket with a per-instance `RLock` while letting other accounts
  run in parallel. Each call logs `tool=… outcome=… duration_ms=…` to stderr
  for your log pipeline.
- **Container healthcheck** — `python -m google_accounts_mcp.healthcheck`
  does a full HTTP round-trip to `/mcp`; a wedged event loop fails the probe
  so a restart-on-unhealthy policy self-heals the container.
- **PDF resource management** — `render_attachment_page` and the OCR branch of
  `extract_attachment_text` close their `pypdfium2` document/page/bitmap
  handles in `try/finally` (PDFium native memory isn't reclaimed
  deterministically by Python's GC). A `threading.Semaphore` caps concurrent
  rasterisation/parse — set `PDF_MAX_CONCURRENCY` (default `4`) to tune. Run
  the container with `MALLOC_ARENA_MAX=2` so glibc returns freed memory to the
  OS instead of retaining it in per-thread arenas. A 108-call mixed
  extract+render stress run across three accounts holds RSS flat (~320 MiB,
  well under the 1 GB cap) with zero restarts.
- **Pure ASGI bearer middleware** — wraps the Streamable HTTP app
  (`/mcp`, stateless) and short-circuits
  unauthenticated requests with a 401, using `hmac.compare_digest` for
  constant-time comparison. `/.well-known/*` paths pass through so MCP
  clients don't confuse 401 for an OAuth-protected server.
- **Path-traversal guards** — `_resolve_attachments` rejects any path that
  resolves outside `ATTACHMENTS_DIR`, and `upload_file` / `drive_upload`
  require bare filenames with no path components.

## Development

```bash
# Syntax check before building
python3 -c "import py_compile; py_compile.compile('src/google_accounts_mcp/server.py', doraise=True)"

# Build the container image
podman build -t google-accounts-mcp .   # or: docker build
```

The Streamable HTTP transport is stateless, so a rebuild never breaks
client sessions. If tool signatures changed, restart your MCP client so it
re-fetches the schemas.

## Testing

Three tiers:

```bash
# Tier 1 — pure helpers (no API, no mocking)
uv run --extra test pytest tests/test_gmail_helpers.py -v

# Tier 2 — Gmail client logic with mocked googleapiclient
uv run --extra test pytest tests/test_gmail_client.py -v

# Tier 1 + 2 — Calendar client (pure helpers + mocked calendar service)
uv run --extra test pytest tests/test_calendar_client.py -v

# Tier 1 + 2 — Tasks / Contacts clients (pure helpers + mocked services)
uv run --extra test pytest tests/test_tasks_client.py tests/test_contacts_client.py -v

# PDF reading / OCR tiers (pdf_read module + tool plumbing; the xberg
# HTTP calls are mocked — no network)
uv run --extra test pytest tests/test_pdf_read.py -v

# All unit tests together (fast, safe, run after every code change)
uv run --extra test pytest tests/test_gmail_helpers.py tests/test_gmail_client.py tests/test_calendar_client.py tests/test_tasks_client.py tests/test_contacts_client.py tests/test_pdf_read.py tests/test_retrying_http.py

# Tier 3 Gmail — live Gmail + Drive round-trip (gated)
GMAIL_TEST_ACCOUNT=you@example.com \
  TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db \
  ATTACHMENTS_DIR=~/.local/share/google-accounts-mcp/attachments \
  GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \
  uv run --extra test pytest tests/test_integration.py -v

# Tier 3 Calendar — live Google Calendar round-trip (gated by the same
# env var). Every test creates its own event and deletes it in a finally
# block; nothing is left on the calendar on success. Events are scheduled
# 24+ hours out to stay off the visible week.
GMAIL_TEST_ACCOUNT=you@example.com \
  TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db \
  GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \
  uv run --extra test pytest tests/test_calendar_integration.py -v

# Tier 3 Tasks — live Google Tasks round-trip in a dedicated scratch task
# list (created and deleted by the module). Tier 3 Contacts — read-only
# live smoke of search/list/get (nothing to clean up by construction).
GMAIL_TEST_ACCOUNT=you@example.com \
  TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db \
  GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \
  uv run --extra test pytest tests/test_tasks_integration.py tests/test_contacts_integration.py -v
```

Integration tests create their own artifacts and clean up after
themselves — drafts (never sent) and Drive files for the Gmail suite,
test events for the Calendar suite, a scratch task list for the Tasks
suite. Nothing is left behind on success.

## Local stdio Mode

`--stdio` starts FastMCP's stdio transport: no uvicorn, no bearer token
(the client owns the spawned process; there is no network surface). This
is what most interactive MCP clients should use:

```jsonc
// e.g. Claude Desktop claude_desktop_config.json / Claude Code .mcp.json
{
  "mcpServers": {
    "google": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/google-accounts-mcp",
               "google-accounts-mcp", "--stdio"],
      "env": {
        "GOOGLE_CLIENT_ID": "...",
        "GOOGLE_CLIENT_SECRET": "...",
        "TOKEN_DB_PATH": "/home/you/.local/share/google-accounts-mcp/tokens.db",
        "ATTACHMENTS_DIR": "/home/you/.local/share/google-accounts-mcp/attachments"
      }
    }
  }
}
```

Prefer an env file over inline values where your client supports it
(`uv run --env-file ...`).

## Container Deployment (HTTP)

```bash
podman build -t google-accounts-mcp .
podman run -d --name google-accounts-mcp -p 8321:8321 -v google-data:/data \
  -e GOOGLE_CLIENT_ID=... -e GOOGLE_CLIENT_SECRET=... \
  -e MCP_BEARER_TOKEN=some-long-random-token \
  google-accounts-mcp
```

The `/data` volume persists `tokens.db` and staged attachments across
restarts. Authorize accounts by running `scripts/authorize.py` on a
machine with a browser and copying `tokens.db` into the volume (restart
the container afterwards). HTTP clients register the server as:

```json
{
  "mcpServers": {
    "google": {
      "url": "https://your-host:8321/mcp",
      "headers": {"Authorization": "Bearer <MCP_BEARER_TOKEN>"}
    }
  }
}
```

Terminate TLS at a reverse proxy — the server itself speaks plain HTTP.
Treat `tokens.db` like a password vault: whoever reads it controls every
connected Google account across all granted scopes (it is created with
mode 0600; keep the volume private).

## License

[MIT](LICENSE)

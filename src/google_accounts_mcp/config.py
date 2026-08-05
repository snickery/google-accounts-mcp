import os

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    # drive.file limits the app to files it created or opened — used for the
    # dedicated DRIVE_FOLDER_NAME folder on the DRIVE_ACCOUNT mailbox.
    "https://www.googleapis.com/auth/drive.file",
    # Full calendar scope (2026-07-04, replacing the earlier calendar.events
    # + calendar.calendarlist.readonly pair): needed for calendar management
    # (create_calendar / update_calendar / delete_calendar), which lives
    # under the calendars/* surface that no narrower scope covers. Also
    # subsumes events/* and calendarList, so the older tools are unchanged.
    "https://www.googleapis.com/auth/calendar",
    # Full read/write on Google Tasks (task lists + tasks). There is no
    # narrower write scope for Tasks — tasks.readonly is the only other
    # option and it would block create/update/complete.
    "https://www.googleapis.com/auth/tasks",
    # Full contacts scope (2026-07-04, replacing contacts.readonly): covers
    # create/update/delete on saved contacts plus everything the read-only
    # scope did. contacts.other.readonly additionally covers the "Other
    # contacts" autocomplete surface (people the account has emailed but
    # never saved), which lives behind a separate scope from saved contacts
    # — still read-only there; the write path into "other contacts" is
    # save_other_contact (copy into My Contacts), covered by `contacts`.
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]

DB_PATH = os.environ.get("TOKEN_DB_PATH", "/data/tokens.db")
ATTACHMENTS_DIR = os.environ.get("ATTACHMENTS_DIR", "/data/attachments")
# Shared filesystem exposed to other MCP servers (notion-mcp in particular)
# so attachments can be handed off without base64-through-MCP-params.
# Both containers mount the same host path at /shared.
SHARED_DIR = os.environ.get("SHARED_DIR", "/shared")
# Account used when a tool call omits `account`. Empty (the default) falls
# back to the sole authorized account, or errors when several exist.
DEFAULT_ACCOUNT = os.environ.get("DEFAULT_ACCOUNT", "")
# The single account whose Google Drive hosts the shared folder. All drive_*
# tools operate against this account's Drive regardless of which account is
# used for email operations. Required for the drive_* tools; everything else
# works without it.
DRIVE_ACCOUNT = os.environ.get("DRIVE_ACCOUNT", "")
DRIVE_FOLDER_NAME = os.environ.get("DRIVE_FOLDER_NAME", "mcp-google-accounts")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
PORT = int(os.environ.get("PORT", "8321"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")

#!/usr/bin/env python3
"""CLI helper to authorize one or more Gmail accounts and store refresh tokens.

Usage:
    uv run scripts/authorize.py alice@example.com
    uv run scripts/authorize.py alice@example.com bob@example.com carol@example.com

Requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables,
or source them from a .secrets.env file first.

Multi-account re-auth
---------------------
Pass every account you want to (re-)authorize as a positional argument. The
script loops through them in order, running a fresh OAuth flow per account,
and aborts the whole batch on the first failure so you don't silently end up
with mixed state.

Two tricks keep a batch re-auth hands-off:

1. ``prompt='select_account consent'`` is passed on the authorization URL,
   so Google always shows the account picker *and* the consent screen —
   no browser restart between accounts to clear the cached consent.

2. We do *not* use ``flow.run_local_server``. That helper hardcodes
   ``wsgiref.simple_server.WSGIServer.allow_reuse_address = False``
   (google-auth-oauthlib flow.py line 436), which means after the first
   run the local :9876 socket sits in TIME_WAIT for ~60s and the next
   bind fails with EADDRINUSE. We run our own WSGI server with
   SO_REUSEADDR + SO_REUSEPORT set explicitly so consecutive runs can
   immediately rebind.

The final identity check via ``users().getProfile`` still catches any case
where the wrong account slipped past the account picker.
"""
import socket
import sys
import wsgiref.simple_server
import wsgiref.util

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from google_accounts_mcp.auth import TokenStore
from google_accounts_mcp.config import DB_PATH, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SCOPES


_LOCAL_HOST = "localhost"
_LOCAL_PORT = 9876


class _ReusableWSGIServer(wsgiref.simple_server.WSGIServer):
    """WSGIServer subclass that force-enables address/port reuse so
    consecutive OAuth flows on the same port don't hit EADDRINUSE from
    a lingering TIME_WAIT socket left by the previous run."""

    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT is Linux-only and handles the cases SO_REUSEADDR
        # alone misses (e.g. prior socket in CLOSE_WAIT, not TIME_WAIT).
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class _SilentHandler(wsgiref.simple_server.WSGIRequestHandler):
    """WSGIRequestHandler that suppresses the default per-request stderr
    access log — keeps authorize.py output clean."""

    def log_message(self, format, *args):  # noqa: A002 - API signature
        pass


def _run_local_oauth_server(flow: InstalledAppFlow, email_addr: str) -> None:
    """Run a one-shot local WSGI server to capture the OAuth redirect for
    ``flow`` and call ``flow.fetch_token``. Replaces the library's
    ``run_local_server`` helper because of its hardcoded
    ``allow_reuse_address = False``."""
    flow.redirect_uri = f"http://{_LOCAL_HOST}:{_LOCAL_PORT}/"
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="select_account consent",
        login_hint=email_addr,
        include_granted_scopes="false",
    )

    print("Copy the URL below and open it in your LOCAL browser.")
    print("(Make sure you have SSH port forwarding: ssh -L 9876:localhost:9876 your-server)")
    print(f"IMPORTANT: Pick {email_addr} from Google's account picker.\n")
    print(auth_url)
    print()

    captured: dict[str, str] = {}

    def app(environ, start_response):
        start_response("200 OK", [("Content-type", "text/plain; charset=utf-8")])
        captured["uri"] = wsgiref.util.request_uri(environ)
        return [
            b"OAuth flow complete for google-accounts-mcp. You may close this tab.",
        ]

    server = wsgiref.simple_server.make_server(
        _LOCAL_HOST,
        _LOCAL_PORT,
        app,
        server_class=_ReusableWSGIServer,
        handler_class=_SilentHandler,
    )
    try:
        server.handle_request()
    finally:
        server.server_close()

    if "uri" not in captured:
        raise SystemExit("Timed out waiting for OAuth redirect from Google.")

    # oauthlib insists the authorization_response URL is https, even
    # though we served it over http on localhost. The library's own
    # run_local_server does the same substitution.
    authorization_response = captured["uri"].replace("http://", "https://", 1)
    flow.fetch_token(authorization_response=authorization_response)


def authorize_one(email_addr: str, store: TokenStore) -> None:
    """Run the OAuth flow for a single account and persist the refresh token."""
    client_config = {
        "installed": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

    print(f"\n=== Authorizing {email_addr} ===")
    _run_local_oauth_server(flow, email_addr)

    creds = flow.credentials

    # Verify the authenticated identity matches what was requested. The
    # forced account picker and login_hint both reduce the chance of a
    # silent wrong-account selection, but we still cross-check via the
    # Gmail profile endpoint — if someone clicks the wrong account we
    # want to hard-abort instead of writing the wrong row to tokens.db.
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    authenticated_email = profile.get("emailAddress", "")
    if authenticated_email.lower() != email_addr.lower():
        raise SystemExit(
            f"\nERROR: Requested {email_addr} but OAuth flow authenticated as "
            f"{authenticated_email}. Nothing saved for this account."
        )

    store.save_credentials(email_addr, creds.refresh_token.strip())
    print(f"Success! Refresh token stored for {email_addr}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <email> [<email> ...]")
        sys.exit(1)

    emails = sys.argv[1:]

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        print("Error: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set.")
        print("Export them or source your .secrets.env file first.")
        sys.exit(1)

    store = TokenStore(DB_PATH)

    for i, email_addr in enumerate(emails, start=1):
        print(f"\n[{i}/{len(emails)}] {email_addr}")
        authorize_one(email_addr, store)

    print(f"\nAll {len(emails)} account(s) authorized. Token database: {DB_PATH}")
    print("Remember to restart the server so cached credentials refresh.")


if __name__ == "__main__":
    main()

import os
import sqlite3
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from google_accounts_mcp.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET


class TokenStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        # Refresh tokens are stored in plaintext — keep the DB owner-only.
        # Create with 0600 (sqlite would use the umask) and tighten any
        # pre-existing DB; WAL sidecar files inherit the main DB's mode.
        fd = os.open(self.db_path, os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o600)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY,
                    refresh_token TEXT NOT NULL
                )
            """)
            conn.commit()

    def list_accounts(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT email FROM accounts ORDER BY email").fetchall()
        return [r[0] for r in rows]

    def get_credentials(self, email: str) -> Credentials | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT refresh_token FROM accounts WHERE email = ?", (email,)
            ).fetchone()
        if not row:
            return None

        # Do NOT pass scopes= here. google-auth would include it in the
        # refresh request, which makes Google reject with invalid_scope if
        # the stored refresh token was granted a narrower set than our
        # current SCOPES (e.g. an old Gmail-only token vs. the new
        # Gmail+Drive SCOPES list). Refreshing without a scope parameter
        # returns an access token scoped to whatever the refresh token
        # was originally granted, which is the behaviour we want.
        creds = Credentials(
            token=None,
            refresh_token=row[0],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
        creds.refresh(Request())
        return creds

    def save_credentials(self, email: str, refresh_token: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO accounts (email, refresh_token) VALUES (?, ?)",
                (email, refresh_token),
            )
            conn.commit()

"""Tier 3 integration tests — hit the real People API (read-only).

Skipped unless GMAIL_TEST_ACCOUNT is set. The contacts surface is
read-only by scope, so there is nothing to create or clean up — these
tests just prove the scopes, endpoints, and formatters work against
live data without asserting on any specific contact's details.

Run with:
    GMAIL_TEST_ACCOUNT=bob@example.com \\
    TOKEN_DB_PATH=~/.local/share/google-accounts-mcp/tokens.db \\
    uv run --extra test pytest tests/test_contacts_integration.py -v

Requires a refresh token issued AFTER contacts.readonly +
contacts.other.readonly were added to SCOPES.
"""
from __future__ import annotations

import os
import re

import pytest

from tests.conftest import requires_gmail


@pytest.fixture(scope="module")
def real_contacts():
    from google_accounts_mcp.contacts_client import ContactsClient
    return ContactsClient()


@pytest.fixture(scope="module")
def test_account():
    return os.environ["GMAIL_TEST_ACCOUNT"]


@requires_gmail
class TestContactsReadOnly:
    def test_list_contacts(self, real_contacts, test_account):
        out = real_contacts.list_contacts(account=test_account, max_results=5)
        # Either the account has contacts (rows carry resource IDs) or it
        # legitimately has none — both are valid live states.
        assert "contact(s) for" in out or "No saved contacts" in out

    def test_search_and_get_round_trip(self, real_contacts, test_account):
        # Search for the account itself — every Gmail account has emailed
        # or saved *someone*; fall back to a broad vowel query.
        for query in (test_account.split("@")[0][:4], "a"):
            out = real_contacts.search_contacts(query, account=test_account)
            ids = re.findall(r"id=((?:people|otherContacts)/\S+)\)", out)
            if ids:
                detail = real_contacts.get_contact(ids[0], account=test_account)
                assert "Contact:" in detail
                assert "id:" in detail
                return
        pytest.skip("account has no searchable contacts at all")

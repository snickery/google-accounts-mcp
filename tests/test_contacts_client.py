"""Tier 1 + Tier 2 tests for ContactsClient.

Tier 1 covers the person formatters. Tier 2 exercises search / list / get
against a mocked People API service — the `contacts_client` fixture
short-circuits `_get_service` and zeroes the search-cache warmup pause.

Run with:
    uv run --extra test pytest tests/test_contacts_client.py -v
"""
from __future__ import annotations

import pytest

from google_accounts_mcp.contacts_client import ContactsClient


def make_person(
    resource: str = "people/c1",
    name: str = "Alice Example",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str = "",
) -> dict:
    p: dict = {"resourceName": resource}
    if name:
        p["names"] = [{"displayName": name}]
    if emails:
        p["emailAddresses"] = [{"value": e} for e in emails]
    if phones:
        p["phoneNumbers"] = [{"value": v} for v in phones]
    if org:
        p["organizations"] = [{"name": org}]
    return p


# ===================================================================
# Tier 1 — formatters
# ===================================================================

class TestFormatPersonRow:
    def test_full_row(self):
        out = ContactsClient._format_person_row(make_person(
            emails=["alice@example.com"], phones=["+61 400 000 000"],
            org="Acme",
        ))
        assert "Alice Example" in out
        assert "<alice@example.com>" in out
        assert "ph: +61 400 000 000" in out
        assert "(Acme)" in out
        assert "id=people/c1" in out
        assert "[other]" not in out

    def test_other_tagged(self):
        out = ContactsClient._format_person_row(
            make_person(resource="otherContacts/c9"), other=True,
        )
        assert "[other]" in out
        assert "id=otherContacts/c9" in out

    def test_nameless(self):
        out = ContactsClient._format_person_row(
            {"resourceName": "people/c2", "emailAddresses": [{"value": "x@y.z"}]},
        )
        assert "(no name)" in out


class TestFormatPersonDetail:
    def test_full_detail(self):
        out = ContactsClient._format_person_detail({
            "resourceName": "people/c1",
            "names": [{"displayName": "Alice Example"}],
            "nicknames": [{"value": "Al"}],
            "emailAddresses": [
                {"value": "alice@work.com", "type": "work"},
                {"value": "alice@home.net"},
            ],
            "phoneNumbers": [{"value": "+61 400 000 000", "type": "mobile"}],
            "organizations": [{"name": "Acme", "title": "CTO"}],
            "addresses": [{"formattedValue": "1 Main St, Sydney", "type": "home"}],
            "birthdays": [{"date": {"year": 1980, "month": 7, "day": 5}}],
            "biographies": [{"value": "Met at conference"}],
            "urls": [{"value": "https://alice.example"}],
        })
        assert "Contact: Alice Example" in out
        assert "aka:     Al" in out
        assert "alice@work.com (work)" in out
        assert "alice@home.net" in out
        assert "+61 400 000 000 (mobile)" in out
        assert "Acme, CTO" in out
        assert "1 Main St, Sydney (home)" in out
        assert "1980-07-05" in out
        assert "Met at conference" in out
        assert "https://alice.example" in out

    def test_birthday_without_year(self):
        out = ContactsClient._format_person_detail({
            "resourceName": "people/c1",
            "names": [{"displayName": "A"}],
            "birthdays": [{"date": {"month": 7, "day": 5}}],
        })
        assert "bday:    07-05" in out

    def test_notes_truncated(self):
        out = ContactsClient._format_person_detail({
            "resourceName": "people/c1",
            "names": [{"displayName": "A"}],
            "biographies": [{"value": "n" * 600}],
        })
        assert "n" * 500 + "…" in out
        assert "n" * 501 not in out


# ===================================================================
# Tier 2 — mocked service
# ===================================================================

class TestSearchContacts:
    def test_merges_saved_and_other(self, contacts_client, mock_people_service):
        mock_people_service.people().searchContacts().execute.return_value = {
            "results": [{"person": make_person(emails=["alice@example.com"])}],
        }
        mock_people_service.otherContacts().search().execute.return_value = {
            "results": [{"person": make_person(
                resource="otherContacts/c9", name="Bob Other",
                emails=["bob@example.com"],
            )}],
        }
        out = contacts_client.search_contacts("example")
        assert "2 contact(s)" in out
        assert "Alice Example" in out
        assert "Bob Other" in out
        assert "[other]" in out

    def test_saved_only(self, contacts_client, mock_people_service):
        mock_people_service.people().searchContacts().execute.return_value = {
            "results": [{"person": make_person()}],
        }
        out = contacts_client.search_contacts(
            "alice", include_other_contacts=False,
        )
        assert "1 contact(s)" in out
        # The warmup hits otherContacts once with an empty query; the real
        # query must never reach the other-contacts surface.
        queries = [
            c.kwargs["query"]
            for c in mock_people_service.otherContacts().search.call_args_list
            if c.kwargs
        ]
        assert queries == [""]

    def test_warmup_runs_once_per_account(self, contacts_client, mock_people_service):
        mock_people_service.people().searchContacts().execute.return_value = {"results": []}
        mock_people_service.otherContacts().search().execute.return_value = {"results": []}
        mock_people_service.people().searchContacts.reset_mock()
        contacts_client.search_contacts("alice")
        contacts_client.search_contacts("bob")
        # 1 warmup (empty query) + 2 real queries.
        queries = [
            c.kwargs["query"]
            for c in mock_people_service.people().searchContacts.call_args_list
        ]
        assert queries == ["", "alice", "bob"]

    def test_empty_query_rejected(self, contacts_client):
        with pytest.raises(ValueError, match="must not be empty"):
            contacts_client.search_contacts("")

    def test_no_matches(self, contacts_client, mock_people_service):
        mock_people_service.people().searchContacts().execute.return_value = {"results": []}
        mock_people_service.otherContacts().search().execute.return_value = {"results": []}
        assert "No contacts matching" in contacts_client.search_contacts("zzz")


class TestListContacts:
    def test_defaults(self, contacts_client, mock_people_service):
        mock_people_service.people().connections().list().execute.return_value = {
            "connections": [make_person()],
            "totalItems": 42,
        }
        mock_people_service.people().connections().list.reset_mock()
        out = contacts_client.list_contacts()
        kwargs = mock_people_service.people().connections().list.call_args.kwargs
        assert kwargs["resourceName"] == "people/me"
        assert kwargs["sortOrder"] == "LAST_MODIFIED_DESCENDING"
        assert "pageToken" not in kwargs
        assert "1 contact(s)" in out
        assert "of 42 total" in out

    def test_pagination(self, contacts_client, mock_people_service):
        mock_people_service.people().connections().list().execute.return_value = {
            "connections": [make_person()],
            "nextPageToken": "tok-2",
        }
        out = contacts_client.list_contacts(page_token="tok-1")
        kwargs = mock_people_service.people().connections().list.call_args.kwargs
        assert kwargs["pageToken"] == "tok-1"
        assert "next_page_token: tok-2" in out

    def test_empty(self, contacts_client, mock_people_service):
        mock_people_service.people().connections().list().execute.return_value = {}
        assert "No saved contacts" in contacts_client.list_contacts()


class TestGetContact:
    def test_saved_contact(self, contacts_client, mock_people_service):
        mock_people_service.people().get().execute.return_value = make_person()
        mock_people_service.people().get.reset_mock()
        out = contacts_client.get_contact("people/c1")
        kwargs = mock_people_service.people().get.call_args.kwargs
        assert kwargs["resourceName"] == "people/c1"
        assert "sources" not in kwargs
        assert "Contact: Alice Example" in out

    def test_other_contact_prefix_swapped(self, contacts_client, mock_people_service):
        mock_people_service.people().get().execute.return_value = make_person(
            resource="people/c9", name="Bob Other",
        )
        mock_people_service.people().get.reset_mock()
        contacts_client.get_contact("otherContacts/c9")
        kwargs = mock_people_service.people().get.call_args.kwargs
        assert kwargs["resourceName"] == "people/c9"
        assert "READ_SOURCE_TYPE_OTHER_CONTACT" in kwargs["sources"]


class TestAccountResolution:
    def test_unknown_account(self, contacts_client):
        with pytest.raises(ValueError, match="not found"):
            contacts_client.list_contacts(account="nobody@example.com")


# ===================================================================
# Write surface (2026-07-04, `contacts` scope)
# ===================================================================

class TestCreateContact:
    def test_minimal(self, contacts_client, mock_people_service):
        mock_people_service.people().createContact().execute.return_value = (
            make_person(name="Alice")
        )
        mock_people_service.people().createContact.reset_mock()
        out = contacts_client.create_contact("Alice")
        kwargs = mock_people_service.people().createContact.call_args.kwargs
        assert kwargs["body"] == {"names": [{"givenName": "Alice"}]}
        assert "Contact created" in out

    def test_full(self, contacts_client, mock_people_service):
        mock_people_service.people().createContact().execute.return_value = (
            make_person()
        )
        mock_people_service.people().createContact.reset_mock()
        contacts_client.create_contact(
            "Alice", family_name="Example",
            emails=["a@x.com", "a@y.com"], phones=["+61 400 000 000"],
            organization="Acme", job_title="CTO", notes="met at conf",
        )
        kwargs = mock_people_service.people().createContact.call_args.kwargs
        assert kwargs["body"] == {
            "names": [{"givenName": "Alice", "familyName": "Example"}],
            "emailAddresses": [{"value": "a@x.com"}, {"value": "a@y.com"}],
            "phoneNumbers": [{"value": "+61 400 000 000"}],
            "organizations": [{"name": "Acme", "title": "CTO"}],
            "biographies": [{"value": "met at conf"}],
        }

    def test_empty_name_rejected(self, contacts_client):
        with pytest.raises(ValueError, match="must not be empty"):
            contacts_client.create_contact("")


class TestUpdateContact:
    def _wire_current(self, mock_people_service, current):
        mock_people_service.people().get().execute.return_value = current
        mock_people_service.people().updateContact().execute.return_value = (
            make_person()
        )
        mock_people_service.people().updateContact.reset_mock()

    def test_replace_emails_only(self, contacts_client, mock_people_service):
        self._wire_current(mock_people_service, {"etag": "e1"})
        contacts_client.update_contact("people/c1", emails=["new@x.com"])
        kwargs = mock_people_service.people().updateContact.call_args.kwargs
        assert kwargs["resourceName"] == "people/c1"
        assert kwargs["updatePersonFields"] == "emailAddresses"
        assert kwargs["body"]["etag"] == "e1"
        assert kwargs["body"]["emailAddresses"] == [{"value": "new@x.com"}]
        assert "names" not in kwargs["body"]

    def test_name_part_merges_with_existing(self, contacts_client, mock_people_service):
        self._wire_current(mock_people_service, {
            "etag": "e2",
            "names": [{"givenName": "Alice", "familyName": "Example"}],
        })
        contacts_client.update_contact("people/c1", given_name="Alicia")
        body = mock_people_service.people().updateContact.call_args.kwargs["body"]
        assert body["names"] == [{"givenName": "Alicia", "familyName": "Example"}]

    def test_clear_lists_and_org(self, contacts_client, mock_people_service):
        self._wire_current(mock_people_service, {"etag": "e3"})
        contacts_client.update_contact(
            "people/c1", emails=[], phones=[], organization="", notes="",
        )
        kwargs = mock_people_service.people().updateContact.call_args.kwargs
        assert kwargs["body"]["emailAddresses"] == []
        assert kwargs["body"]["phoneNumbers"] == []
        assert kwargs["body"]["organizations"] == []
        assert kwargs["body"]["biographies"] == []
        assert set(kwargs["updatePersonFields"].split(",")) == {
            "emailAddresses", "phoneNumbers", "organizations", "biographies",
        }

    def test_other_contact_refused(self, contacts_client):
        with pytest.raises(ValueError, match="save_other_contact"):
            contacts_client.update_contact("otherContacts/c9", emails=["x@y.z"])

    def test_no_fields_rejected(self, contacts_client, mock_people_service):
        mock_people_service.people().get().execute.return_value = {"etag": "e"}
        with pytest.raises(ValueError, match="no fields"):
            contacts_client.update_contact("people/c1")


class TestDeleteContact:
    def test_delete(self, contacts_client, mock_people_service):
        out = contacts_client.delete_contact("people/c1")
        mock_people_service.people().deleteContact.assert_called_once_with(
            resourceName="people/c1",
        )
        assert "Deleted contact people/c1" in out

    def test_other_contact_refused(self, contacts_client, mock_people_service):
        with pytest.raises(ValueError, match="cannot be deleted"):
            contacts_client.delete_contact("otherContacts/c9")
        mock_people_service.people().deleteContact.assert_not_called()


class TestSaveOtherContact:
    def test_promote(self, contacts_client, mock_people_service):
        mock_people_service.otherContacts().copyOtherContactToMyContactsGroup().execute.return_value = (
            make_person(resource="people/c77", name="Bob Other")
        )
        mock_people_service.otherContacts().copyOtherContactToMyContactsGroup.reset_mock()
        out = contacts_client.save_other_contact("otherContacts/c9")
        kwargs = (
            mock_people_service.otherContacts()
            .copyOtherContactToMyContactsGroup.call_args.kwargs
        )
        assert kwargs["resourceName"] == "otherContacts/c9"
        assert kwargs["body"] == {
            "copyMask": "names,emailAddresses,phoneNumbers",
        }
        assert "Saved to My Contacts" in out
        assert "people/c77" in out

    def test_saved_contact_refused(self, contacts_client):
        with pytest.raises(ValueError, match="otherContacts"):
            contacts_client.save_other_contact("people/c1")

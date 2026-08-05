"""Google Contacts (People API) client for gmail-mcp.

Shares the same ``TokenStore`` as ``GmailClient`` — the same OAuth refresh
token covers the ``contacts`` + ``contacts.other.readonly`` scopes (see
``config.SCOPES``). Read/write since 2026-07-04 (was read-only at the
2026-07-03 introduction): saved contacts support create / update / delete,
and an "other contact" can be promoted into My Contacts with
``save_other_contact``. The "Other contacts" pool itself stays read-only —
that is a Google-side constraint (`contacts.other.readonly` is the only
scope for it); promotion-then-edit is the supported write path.

Design notes
------------
* ``search_contacts`` queries BOTH surfaces by default: saved contacts
  (``people.searchContacts``) and "Other contacts" (``otherContacts.search``
  — people the account has emailed but never saved, i.e. Gmail's
  autocomplete pool). Rows from the latter are tagged ``[other]``.
* Google's search endpoints read from a lazily-populated cache: the first
  search on a fresh session can silently return stale/empty results. Per
  Google's own docs the fix is a warmup request (empty query) followed by a
  short pause — done once per account per process in ``_warm``.
* ``resource_name`` identifiers look like ``people/c1234...`` (saved) or
  ``otherContacts/c1234...`` (other). ``get_contact`` accepts both; the
  People API serves otherContacts resource names via ``people.get`` too.
"""
from __future__ import annotations

import time

from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from google_accounts_mcp.auth import TokenStore
from google_accounts_mcp.config import DB_PATH, DEFAULT_ACCOUNT
from google_accounts_mcp.gmail_client import _HTTP_TIMEOUT, _RetryingHttp


# Fields for list/detail views of saved contacts.
_PERSON_FIELDS = (
    "names,nicknames,emailAddresses,phoneNumbers,organizations,"
    "addresses,birthdays,biographies,urls,metadata"
)
# searchContacts supports the same masks; keep the search response lean.
_SEARCH_READ_MASK = "names,emailAddresses,phoneNumbers,organizations"
# otherContacts.search only permits these four masks — anything else 400s.
_OTHER_READ_MASK = "names,emailAddresses,phoneNumbers,metadata"

# Seconds to pause after the warmup request; Google documents "a few
# seconds" for the search cache to populate.
_WARMUP_PAUSE = 2.0


class ContactsClient:
    def __init__(self, store: TokenStore | None = None):
        self.store = store or TokenStore(DB_PATH)
        self._services: dict = {}
        self._warmed: set[str] = set()

    # ------------------------------------------------------------------
    # Account resolution (mirrors GmailClient._resolve_account)
    # ------------------------------------------------------------------
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
        if account in accounts:
            return account
        matches = [a for a in accounts if account.lower() in a.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous account '{account}', matches: {', '.join(matches)}"
            )
        raise ValueError(
            f"Account '{account}' not found. Available: {', '.join(accounts)}"
        )

    def _get_service(self, account: str):
        account = self._resolve_account(account)
        if account not in self._services:
            creds = self.store.get_credentials(account)
            if not creds:
                raise ValueError(f"No credentials for {account}")
            authed = AuthorizedHttp(creds, http=_RetryingHttp(timeout=_HTTP_TIMEOUT))
            self._services[account] = build(
                "people", "v1",
                http=authed,
                cache_discovery=False,
                static_discovery=True,
            )
        return self._services[account], account

    def _warm(self, service, acct: str) -> None:
        """Warm the search caches once per account per process."""
        if acct in self._warmed:
            return
        service.people().searchContacts(
            query="", readMask=_SEARCH_READ_MASK,
        ).execute()
        service.otherContacts().search(
            query="", readMask=_OTHER_READ_MASK,
        ).execute()
        time.sleep(_WARMUP_PAUSE)
        self._warmed.add(acct)

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------
    @staticmethod
    def _primary(items: list | None) -> dict:
        """First entry of a People API repeated field ({} when absent)."""
        return (items or [{}])[0]

    @classmethod
    def _format_person_row(cls, person: dict, other: bool = False) -> str:
        name = cls._primary(person.get("names")).get("displayName", "(no name)")
        emails = [e.get("value", "") for e in person.get("emailAddresses", [])]
        phones = [p.get("value", "") for p in person.get("phoneNumbers", [])]
        org = cls._primary(person.get("organizations")).get("name", "")
        parts = [f"  • {name}"]
        if emails:
            parts.append(f"<{', '.join(e for e in emails if e)}>")
        if phones:
            parts.append(f"ph: {', '.join(p for p in phones if p)}")
        if org:
            parts.append(f"({org})")
        if other:
            parts.append("[other]")
        parts.append(f"(id={person.get('resourceName', '')})")
        return "  ".join(parts)

    @classmethod
    def _format_person_detail(cls, person: dict) -> str:
        name = cls._primary(person.get("names")).get("displayName", "(no name)")
        lines = [
            f"Contact: {name}",
            f"  id:      {person.get('resourceName', '')}",
        ]
        nicknames = [n.get("value", "") for n in person.get("nicknames", []) if n.get("value")]
        if nicknames:
            lines.append(f"  aka:     {', '.join(nicknames)}")
        for e in person.get("emailAddresses", []):
            t = f" ({e['type']})" if e.get("type") else ""
            lines.append(f"  email:   {e.get('value', '')}{t}")
        for p in person.get("phoneNumbers", []):
            t = f" ({p['type']})" if p.get("type") else ""
            lines.append(f"  phone:   {p.get('value', '')}{t}")
        for o in person.get("organizations", []):
            title = f", {o['title']}" if o.get("title") else ""
            lines.append(f"  org:     {o.get('name', '')}{title}")
        for a in person.get("addresses", []):
            t = f" ({a['type']})" if a.get("type") else ""
            lines.append(f"  address: {a.get('formattedValue', '')}{t}")
        for b in person.get("birthdays", []):
            d = b.get("date", {})
            if d:
                y = d.get("year")
                lines.append(
                    "  bday:    "
                    + (f"{y}-" if y else "")
                    + f"{d.get('month', 0):02d}-{d.get('day', 0):02d}"
                )
        for bio in person.get("biographies", []):
            v = bio.get("value", "")
            if v:
                if len(v) > 500:
                    v = v[:500] + "…"
                lines.append(f"  notes:   {v}")
        for u in person.get("urls", []):
            if u.get("value"):
                lines.append(f"  url:     {u['value']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # search_contacts — saved contacts + (optionally) "Other contacts"
    # ------------------------------------------------------------------
    def search_contacts(
        self,
        query: str,
        account: str = "",
        max_results: int = 10,
        include_other_contacts: bool = True,
    ) -> str:
        """Prefix-match search over names, emails, phone numbers, and orgs."""
        if not query:
            raise ValueError("query must not be empty")
        service, acct = self._get_service(account)
        self._warm(service, acct)

        resp = service.people().searchContacts(
            query=query, readMask=_SEARCH_READ_MASK, pageSize=max_results,
        ).execute()
        saved = [r.get("person", {}) for r in resp.get("results", [])]

        others = []
        if include_other_contacts:
            resp = service.otherContacts().search(
                query=query, readMask=_OTHER_READ_MASK, pageSize=max_results,
            ).execute()
            others = [r.get("person", {}) for r in resp.get("results", [])]

        if not saved and not others:
            return f"No contacts matching '{query}' for {acct}."
        lines = [
            f"{len(saved) + len(others)} contact(s) matching '{query}' for {acct}:"
        ]
        for p in saved:
            lines.append(self._format_person_row(p))
        for p in others:
            lines.append(self._format_person_row(p, other=True))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # list_contacts — the saved-contacts connection list
    # ------------------------------------------------------------------
    def list_contacts(
        self,
        account: str = "",
        max_results: int = 50,
        page_token: str = "",
        sort_order: str = "LAST_MODIFIED_DESCENDING",
    ) -> str:
        service, acct = self._get_service(account)
        kwargs = {
            "resourceName": "people/me",
            "personFields": _SEARCH_READ_MASK,
            "pageSize": max_results,
            "sortOrder": sort_order,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.people().connections().list(**kwargs).execute()
        items = resp.get("connections", [])
        next_token = resp.get("nextPageToken", "")
        total = resp.get("totalItems")
        if not items:
            return f"No saved contacts for {acct}."
        header = f"{len(items)} contact(s) for {acct}"
        if total is not None:
            header += f" (of {total} total)"
        lines = [header + ":"]
        for p in items:
            lines.append(self._format_person_row(p))
        if next_token:
            lines.append(f"\n(next_page_token: {next_token})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # create_contact
    # ------------------------------------------------------------------
    def create_contact(
        self,
        given_name: str,
        family_name: str = "",
        account: str = "",
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        organization: str = "",
        job_title: str = "",
        notes: str = "",
    ) -> str:
        if not given_name:
            raise ValueError("given_name must not be empty")
        service, acct = self._get_service(account)
        body: dict = {"names": [{"givenName": given_name}]}
        if family_name:
            body["names"][0]["familyName"] = family_name
        if emails:
            body["emailAddresses"] = [{"value": e} for e in emails]
        if phones:
            body["phoneNumbers"] = [{"value": p} for p in phones]
        if organization or job_title:
            org: dict = {}
            if organization:
                org["name"] = organization
            if job_title:
                org["title"] = job_title
            body["organizations"] = [org]
        if notes:
            body["biographies"] = [{"value": notes}]
        person = service.people().createContact(body=body).execute()
        return (
            f"Contact created for {acct}\n"
            + self._format_person_detail(person)
        )

    # ------------------------------------------------------------------
    # update_contact (PATCH-like; the People API requires the current etag
    # and an explicit updatePersonFields mask)
    # ------------------------------------------------------------------
    def update_contact(
        self,
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
        """Update a saved contact. Only non-``None`` fields are written.

        ``emails=[]`` / ``phones=[]`` clear those lists; ``notes=''`` clears
        the notes; ``organization=''`` (with ``job_title`` unset or ``''``)
        removes the organization entry. Name and organization parts merge
        with the existing values, so setting ``given_name`` alone keeps the
        current family name."""
        if resource_name.startswith("otherContacts/"):
            raise ValueError(
                "Other contacts are read-only. Promote it into My Contacts "
                "first with save_other_contact, then update the returned "
                "people/* contact."
            )
        service, acct = self._get_service(account)
        current = service.people().get(
            resourceName=resource_name,
            personFields="names,emailAddresses,phoneNumbers,organizations,biographies",
        ).execute()

        body: dict = {"etag": current.get("etag", ""), "resourceName": resource_name}
        fields: list[str] = []

        if given_name is not None or family_name is not None:
            cur = self._primary(current.get("names"))
            name: dict = {}
            g = given_name if given_name is not None else cur.get("givenName", "")
            f = family_name if family_name is not None else cur.get("familyName", "")
            if g:
                name["givenName"] = g
            if f:
                name["familyName"] = f
            if not name:
                raise ValueError("cannot clear both name parts")
            body["names"] = [name]
            fields.append("names")
        if emails is not None:
            body["emailAddresses"] = [{"value": e} for e in emails]
            fields.append("emailAddresses")
        if phones is not None:
            body["phoneNumbers"] = [{"value": p} for p in phones]
            fields.append("phoneNumbers")
        if organization is not None or job_title is not None:
            cur = self._primary(current.get("organizations"))
            o = organization if organization is not None else cur.get("name", "")
            t = job_title if job_title is not None else cur.get("title", "")
            org: dict = {}
            if o:
                org["name"] = o
            if t:
                org["title"] = t
            body["organizations"] = [org] if org else []
            fields.append("organizations")
        if notes is not None:
            body["biographies"] = [{"value": notes}] if notes else []
            fields.append("biographies")

        if not fields:
            raise ValueError("update_contact called with no fields to change")

        person = service.people().updateContact(
            resourceName=resource_name,
            body=body,
            updatePersonFields=",".join(fields),
        ).execute()
        return (
            f"Contact updated for {acct}\n"
            + self._format_person_detail(person)
        )

    # ------------------------------------------------------------------
    # delete_contact
    # ------------------------------------------------------------------
    def delete_contact(self, resource_name: str, account: str = "") -> str:
        if resource_name.startswith("otherContacts/"):
            raise ValueError(
                "Other contacts cannot be deleted via the API — they are "
                "Gmail's autocomplete history, not saved contacts."
            )
        service, acct = self._get_service(account)
        service.people().deleteContact(resourceName=resource_name).execute()
        return (
            f"Deleted contact {resource_name} for {acct}. "
            "(Google Contacts keeps deletions in its web-UI bin for ~30 days.)"
        )

    # ------------------------------------------------------------------
    # save_other_contact — promote an autocomplete-pool entry to saved
    # ------------------------------------------------------------------
    def save_other_contact(self, resource_name: str, account: str = "") -> str:
        if not resource_name.startswith("otherContacts/"):
            raise ValueError(
                "save_other_contact takes an otherContacts/* resource name "
                "(from a search_contacts row tagged [other])."
            )
        service, acct = self._get_service(account)
        person = service.otherContacts().copyOtherContactToMyContactsGroup(
            resourceName=resource_name,
            body={"copyMask": "names,emailAddresses,phoneNumbers"},
        ).execute()
        return (
            f"Saved to My Contacts for {acct} — it can now be updated with "
            "update_contact\n" + self._format_person_detail(person)
        )

    # ------------------------------------------------------------------
    # get_contact
    # ------------------------------------------------------------------
    def get_contact(self, resource_name: str, account: str = "") -> str:
        service, _acct = self._get_service(account)
        kwargs = {"personFields": _PERSON_FIELDS}
        if resource_name.startswith("otherContacts/"):
            # people.get only serves people/* resource names. Other-contact
            # IDs are documented as interchangeable after a prefix swap, but
            # the OTHER_CONTACT read source must be requested explicitly (it
            # is off by default).
            resource_name = "people/" + resource_name.split("/", 1)[1]
            kwargs["sources"] = [
                "READ_SOURCE_TYPE_OTHER_CONTACT",
                "READ_SOURCE_TYPE_CONTACT",
                "READ_SOURCE_TYPE_PROFILE",
            ]
        person = service.people().get(
            resourceName=resource_name, **kwargs,
        ).execute()
        return self._format_person_detail(person)

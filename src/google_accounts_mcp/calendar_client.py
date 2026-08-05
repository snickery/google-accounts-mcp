"""Google Calendar client for gmail-mcp.

Shares the same ``TokenStore`` as ``GmailClient`` — the same OAuth refresh
token covers gmail.modify, drive.file, and calendar.events scopes (see
``config.SCOPES``). Every Gmail account in the token DB gets a per-account
calendar surface with a separate cached ``build('calendar', 'v3', ...)``
service.

Design notes
------------
* Date / datetime inputs are strings. A bare ``YYYY-MM-DD`` turns the event
  into an all-day event; anything else is treated as an RFC3339 dateTime and
  passed through to the API with an optional ``timezone`` override.
* ``update_event`` uses PATCH semantics. Only fields the caller explicitly
  provides are sent to the API. ``None`` means "leave unchanged"; an empty
  string (for string fields) or an empty list (for list fields) explicitly
  clears the field.
* The response formatters return human-readable strings, not JSON, to match
  ``GmailClient`` style. Agents get structured-enough output to act on
  (event IDs, calendar IDs, start/end) without having to parse JSON.
"""
from __future__ import annotations

import re

from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from google_accounts_mcp.auth import TokenStore
from google_accounts_mcp.config import DB_PATH, DEFAULT_ACCOUNT
from google_accounts_mcp.gmail_client import _HTTP_TIMEOUT, _RetryingHttp


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_RESPONSE_STATUS_MAP = {
    "accept": "accepted",
    "accepted": "accepted",
    "yes": "accepted",
    "decline": "declined",
    "declined": "declined",
    "no": "declined",
    "tentative": "tentative",
    "maybe": "tentative",
    "needs_action": "needsAction",
    "needsaction": "needsAction",
}


class CalendarClient:
    def __init__(self, store: TokenStore | None = None):
        self.store = store or TokenStore(DB_PATH)
        self._services: dict = {}

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
            # Same retrying, timeout-bounded transport as GmailClient so an
            # idle-closed calendar socket retries transparently instead of
            # surfacing "Connection reset by peer".
            authed = AuthorizedHttp(creds, http=_RetryingHttp(timeout=_HTTP_TIMEOUT))
            self._services[account] = build(
                "calendar", "v3",
                http=authed,
                cache_discovery=False,
                static_discovery=True,
            )
        return self._services[account], account

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _build_time_point(value: str, timezone: str = "") -> dict:
        """Build the dict Google Calendar expects for a start/end field.

        * ``YYYY-MM-DD`` -> ``{'date': value}`` (all-day).
        * Anything else  -> ``{'dateTime': value}`` plus optional timeZone.
        """
        if not value:
            raise ValueError("start/end value must not be empty")
        if _DATE_ONLY_RE.match(value):
            return {"date": value}
        out = {"dateTime": value}
        if timezone:
            out["timeZone"] = timezone
        return out

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------
    @staticmethod
    def _format_event_row(event: dict) -> str:
        eid = event.get("id", "")
        summary = event.get("summary", "(no title)")
        start = event.get("start", {})
        end = event.get("end", {})
        start_s = start.get("dateTime") or start.get("date") or "?"
        end_s = end.get("dateTime") or end.get("date") or "?"
        status = event.get("status", "")
        status_tag = f" [{status}]" if status and status != "confirmed" else ""
        etype = event.get("eventType", "")
        type_tag = f" [{etype}]" if etype and etype != "default" else ""
        return f"  • {start_s} → {end_s}  {summary}{type_tag}{status_tag}  (id={eid})"

    @staticmethod
    def _format_event_detail(event: dict) -> str:
        lines = [
            f"Event: {event.get('summary', '(no title)')}",
            f"  id:          {event.get('id', '')}",
            f"  status:      {event.get('status', '')}",
            f"  htmlLink:    {event.get('htmlLink', '')}",
        ]
        start = event.get("start", {})
        end = event.get("end", {})
        start_s = start.get("dateTime") or start.get("date") or ""
        end_s = end.get("dateTime") or end.get("date") or ""
        tz = start.get("timeZone") or end.get("timeZone") or ""
        lines.append(f"  start:       {start_s}" + (f" ({tz})" if tz else ""))
        lines.append(f"  end:         {end_s}")
        if event.get("location"):
            lines.append(f"  location:    {event['location']}")
        if event.get("description"):
            desc = event["description"]
            if len(desc) > 500:
                desc = desc[:500] + "…"
            lines.append(f"  description: {desc}")
        if event.get("recurrence"):
            lines.append(f"  recurrence:  {event['recurrence']}")
        if event.get("recurringEventId"):
            lines.append(f"  recurringEventId: {event['recurringEventId']}")
        attendees = event.get("attendees") or []
        if attendees:
            lines.append("  attendees:")
            for a in attendees:
                tag = []
                if a.get("organizer"):
                    tag.append("organizer")
                if a.get("self"):
                    tag.append("self")
                tag_s = f" [{', '.join(tag)}]" if tag else ""
                lines.append(
                    f"    - {a.get('email', '')}  "
                    f"({a.get('responseStatus', '?')}){tag_s}"
                )
        organizer = event.get("organizer", {})
        if organizer:
            lines.append(
                f"  organizer:   {organizer.get('email', '')} "
                f"{'(self)' if organizer.get('self') else ''}".rstrip()
            )
        if event.get("conferenceData"):
            uri = (
                event["conferenceData"]
                .get("entryPoints", [{}])[0]
                .get("uri", "")
            )
            if uri:
                lines.append(f"  conference:  {uri}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # list_calendars
    # ------------------------------------------------------------------
    def list_calendars(self, account: str = "", filter: str = "") -> str:
        """List calendars visible to the account (calendarList entries)."""
        service, acct = self._get_service(account)
        items = []
        page_token = None
        while True:
            resp = (
                service.calendarList()
                .list(pageToken=page_token, maxResults=250)
                .execute()
            )
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if filter:
            f = filter.lower()
            items = [
                c for c in items
                if f in (c.get("summary", "").lower())
                or f in (c.get("id", "").lower())
            ]
        if not items:
            suffix = f" matching '{filter}'" if filter else ""
            return f"No calendars found for {acct}{suffix}."
        lines = [f"{len(items)} calendar(s) for {acct}:"]
        for c in items:
            primary = " (primary)" if c.get("primary") else ""
            access = c.get("accessRole", "")
            lines.append(
                f"  • {c.get('summary', '(unnamed)')}{primary}  "
                f"[{access}]  (id={c.get('id', '')})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Calendar management (2026-07-04, full `calendar` scope) — the
    # calendars/* surface, distinct from calendarList and events/*.
    # ------------------------------------------------------------------
    def create_calendar(
        self,
        summary: str,
        account: str = "",
        description: str = "",
        timezone: str = "",
    ) -> str:
        """Create a new secondary calendar owned by the account."""
        if not summary:
            raise ValueError("summary must not be empty")
        service, acct = self._get_service(account)
        body: dict = {"summary": summary}
        if description:
            body["description"] = description
        if timezone:
            body["timeZone"] = timezone
        cal = service.calendars().insert(body=body).execute()
        return (
            f"Calendar created for {acct}\n"
            f"  summary: {cal.get('summary', '')}\n"
            f"  id:      {cal.get('id', '')}\n"
            + (f"  timeZone: {cal['timeZone']}\n" if cal.get("timeZone") else "")
        ).rstrip()

    def update_calendar(
        self,
        calendar_id: str,
        account: str = "",
        summary: str | None = None,
        description: str | None = None,
        timezone: str | None = None,
    ) -> str:
        """Patch a calendar's metadata. Only non-None fields are sent;
        empty string clears description."""
        service, acct = self._get_service(account)
        body: dict = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if timezone is not None:
            body["timeZone"] = timezone
        if not body:
            raise ValueError("update_calendar called with no fields to change")
        cal = (
            service.calendars()
            .patch(calendarId=calendar_id, body=body)
            .execute()
        )
        return (
            f"Calendar updated for {acct}\n"
            f"  summary: {cal.get('summary', '')}\n"
            f"  id:      {cal.get('id', '')}"
        )

    def delete_calendar(self, calendar_id: str, account: str = "") -> str:
        """Permanently delete a secondary calendar and every event on it.

        The primary calendar cannot be deleted (Google rejects it too, but
        we refuse up-front with a clearer message)."""
        service, acct = self._get_service(account)
        if calendar_id in ("primary", acct):
            raise ValueError(
                "Refusing to delete the primary calendar. Google only allows "
                "deleting secondary calendars; use delete_event to remove "
                "individual events."
            )
        service.calendars().delete(calendarId=calendar_id).execute()
        return f"Deleted calendar {calendar_id} for {acct} (all its events are gone)."

    # ------------------------------------------------------------------
    # list_events
    # ------------------------------------------------------------------
    def list_events(
        self,
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
        """List events from a calendar.

        ``time_min`` / ``time_max``: RFC3339 timestamps. If ``single_events``
        is True (the default) recurring events are expanded into individual
        instances, which is almost always what you want. ``order_by``
        defaults to 'startTime' when expanding instances, 'updated' otherwise.
        ``event_types``: comma-separated filter over the API's event types —
        default, birthday, focusTime, outOfOffice, workingLocation
        (e.g. 'default' hides birthdays; 'focusTime,outOfOffice' shows only
        blocks). Empty returns every type, as before.
        """
        service, acct = self._get_service(account)
        kwargs = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": single_events,
            "showDeleted": show_deleted,
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query
        if page_token:
            kwargs["pageToken"] = page_token
        if order_by:
            kwargs["orderBy"] = order_by
        elif single_events:
            kwargs["orderBy"] = "startTime"
        types = [t.strip() for t in event_types.split(",") if t.strip()]
        if types:
            kwargs["eventTypes"] = types

        resp = service.events().list(**kwargs).execute()
        items = resp.get("items", [])
        next_token = resp.get("nextPageToken", "")
        if not items:
            return f"No events on {calendar_id} for {acct}."
        lines = [f"{len(items)} event(s) on {calendar_id} for {acct}:"]
        for e in items:
            lines.append(self._format_event_row(e))
        if next_token:
            lines.append(f"\n(next_page_token: {next_token})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # get_event
    # ------------------------------------------------------------------
    def get_event(
        self, event_id: str, calendar_id: str = "primary", account: str = "",
    ) -> str:
        service, _acct = self._get_service(account)
        event = (
            service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )
        return self._format_event_detail(event)

    # ------------------------------------------------------------------
    # create_event
    # ------------------------------------------------------------------
    def create_event(
        self,
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
        """Create a new event.

        ``start`` / ``end`` accept either ``YYYY-MM-DD`` (all-day) or a full
        RFC3339 dateTime. ``timezone`` is applied to dateTime values.
        ``attendees`` is a list of email addresses. ``send_updates`` controls
        whether invite emails are dispatched: ``'all'``, ``'externalOnly'``,
        or ``'none'`` (default)."""
        service, acct = self._get_service(account)
        body: dict = {
            "summary": summary,
            "start": self._build_time_point(start, timezone),
            "end": self._build_time_point(end, timezone),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
        if recurrence:
            body["recurrence"] = list(recurrence)
        if reminders_minutes is not None:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": m} for m in reminders_minutes
                ],
            }

        event = (
            service.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates=send_updates)
            .execute()
        )
        return (
            f"Event created on {calendar_id} for {acct}\n"
            + self._format_event_detail(event)
        )

    # ------------------------------------------------------------------
    # update_event (PATCH)
    # ------------------------------------------------------------------
    def update_event(
        self,
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
        """Patch an existing event. Only fields that are not ``None`` are
        sent. Pass ``attendees=[]`` or ``recurrence=[]`` to clear those
        fields explicitly. String fields follow the same rule —
        ``description=''`` clears the description."""
        service, acct = self._get_service(account)
        body: dict = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if start is not None:
            body["start"] = self._build_time_point(start, timezone)
        if end is not None:
            body["end"] = self._build_time_point(end, timezone)
        if attendees is not None:
            body["attendees"] = [{"email": e} for e in attendees]
        if recurrence is not None:
            body["recurrence"] = list(recurrence)

        if not body:
            raise ValueError("update_event called with no fields to change")

        event = (
            service.events()
            .patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
                sendUpdates=send_updates,
            )
            .execute()
        )
        return (
            f"Event updated on {calendar_id} for {acct}\n"
            + self._format_event_detail(event)
        )

    # ------------------------------------------------------------------
    # delete_event
    # ------------------------------------------------------------------
    def delete_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        account: str = "",
        send_updates: str = "none",
    ) -> str:
        service, acct = self._get_service(account)
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates,
        ).execute()
        return f"Deleted event {event_id} on {calendar_id} for {acct}."

    # ------------------------------------------------------------------
    # quick_add_event — natural-language add ("Dinner tomorrow 7pm")
    # ------------------------------------------------------------------
    def quick_add_event(
        self, text: str, calendar_id: str = "primary", account: str = "",
        send_updates: str = "none",
    ) -> str:
        service, acct = self._get_service(account)
        event = (
            service.events()
            .quickAdd(
                calendarId=calendar_id, text=text, sendUpdates=send_updates,
            )
            .execute()
        )
        return (
            f"Quick-added event on {calendar_id} for {acct}\n"
            + self._format_event_detail(event)
        )

    # ------------------------------------------------------------------
    # move_event
    # ------------------------------------------------------------------
    def move_event(
        self,
        event_id: str,
        destination_calendar_id: str,
        source_calendar_id: str = "primary",
        account: str = "",
        send_updates: str = "none",
    ) -> str:
        service, acct = self._get_service(account)
        event = (
            service.events()
            .move(
                calendarId=source_calendar_id,
                eventId=event_id,
                destination=destination_calendar_id,
                sendUpdates=send_updates,
            )
            .execute()
        )
        return (
            f"Moved event {event_id} from {source_calendar_id} to "
            f"{destination_calendar_id} for {acct}\n"
            + self._format_event_detail(event)
        )

    # ------------------------------------------------------------------
    # respond_to_event — accept / decline / tentative
    # ------------------------------------------------------------------
    def respond_to_event(
        self,
        event_id: str,
        response: str,
        calendar_id: str = "primary",
        account: str = "",
        comment: str = "",
        send_updates: str = "none",
    ) -> str:
        """Set the authenticated account's response on an event. ``response``
        accepts accept/declined/tentative aliases (yes/no/maybe etc)."""
        key = response.strip().lower()
        if key not in _RESPONSE_STATUS_MAP:
            raise ValueError(
                f"Unknown response '{response}'. "
                f"Use one of: accepted, declined, tentative, needsAction."
            )
        status = _RESPONSE_STATUS_MAP[key]

        service, acct = self._get_service(account)
        event = (
            service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )
        attendees = event.get("attendees", [])
        # Find the attendee entry that is the authenticated user. Google
        # marks one attendee with ``"self": True`` on requests made by that
        # user. Fall back to matching the account email.
        self_att = None
        for a in attendees:
            if a.get("self") or a.get("email", "").lower() == acct.lower():
                self_att = a
                break
        if self_att is None:
            # Not yet an attendee — add the account as a responding attendee.
            self_att = {"email": acct, "self": True}
            attendees = list(attendees) + [self_att]
        self_att["responseStatus"] = status
        if comment:
            self_att["comment"] = comment

        patched = (
            service.events()
            .patch(
                calendarId=calendar_id,
                eventId=event_id,
                body={"attendees": attendees},
                sendUpdates=send_updates,
            )
            .execute()
        )
        return (
            f"Response '{status}' recorded for {acct} on event {event_id}\n"
            + self._format_event_detail(patched)
        )

    # ------------------------------------------------------------------
    # list_instances — expand a recurring event
    # ------------------------------------------------------------------
    def list_instances(
        self,
        event_id: str,
        calendar_id: str = "primary",
        account: str = "",
        time_min: str = "",
        time_max: str = "",
        max_results: int = 50,
    ) -> str:
        service, acct = self._get_service(account)
        kwargs = {
            "calendarId": calendar_id,
            "eventId": event_id,
            "maxResults": max_results,
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        resp = service.events().instances(**kwargs).execute()
        items = resp.get("items", [])
        if not items:
            return f"No instances for recurring event {event_id} on {calendar_id}."
        lines = [
            f"{len(items)} instance(s) of {event_id} on {calendar_id} for {acct}:"
        ]
        for e in items:
            lines.append(self._format_event_row(e))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # free_busy — who is busy when
    # ------------------------------------------------------------------
    def free_busy(
        self,
        time_min: str,
        time_max: str,
        calendar_ids: list[str] | None = None,
        account: str = "",
        timezone: str = "",
    ) -> str:
        service, acct = self._get_service(account)
        calendar_ids = calendar_ids or ["primary"]
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": cid} for cid in calendar_ids],
        }
        if timezone:
            body["timeZone"] = timezone
        resp = service.freebusy().query(body=body).execute()
        cals = resp.get("calendars", {})
        lines = [
            f"Free/busy for {acct} ({time_min} → {time_max}):"
        ]
        for cid, info in cals.items():
            errors = info.get("errors") or []
            busy = info.get("busy") or []
            if errors:
                lines.append(
                    f"  {cid}: ERROR "
                    + ", ".join(e.get("reason", "?") for e in errors)
                )
                continue
            if not busy:
                lines.append(f"  {cid}: free")
                continue
            lines.append(f"  {cid}: {len(busy)} busy block(s)")
            for b in busy:
                lines.append(f"    - {b.get('start', '?')} → {b.get('end', '?')}")
        return "\n".join(lines)

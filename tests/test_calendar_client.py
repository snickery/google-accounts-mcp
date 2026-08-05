"""Tier 1 + Tier 2 tests for CalendarClient.

Tier 1 covers pure helpers (time-point parsing, formatters, response alias
map). Tier 2 exercises the public methods against a mocked Google Calendar
service — the `calendar_client` fixture short-circuits `_get_service` so no
network calls happen.

Run with:
    uv run --extra test pytest tests/test_calendar_client.py -v
"""
from __future__ import annotations

import pytest

from google_accounts_mcp.calendar_client import CalendarClient, _RESPONSE_STATUS_MAP


# ===================================================================
# Tier 1 — pure helpers
# ===================================================================

class TestBuildTimePoint:
    def test_all_day_date(self):
        assert CalendarClient._build_time_point("2026-04-14") == {"date": "2026-04-14"}

    def test_datetime_without_tz(self):
        assert CalendarClient._build_time_point("2026-04-14T15:00:00+10:00") == {
            "dateTime": "2026-04-14T15:00:00+10:00",
        }

    def test_datetime_with_explicit_tz(self):
        assert CalendarClient._build_time_point(
            "2026-04-14T15:00:00", timezone="Australia/Sydney",
        ) == {
            "dateTime": "2026-04-14T15:00:00",
            "timeZone": "Australia/Sydney",
        }

    def test_timezone_ignored_for_all_day(self):
        # A date-only value is all-day; Google Calendar rejects timeZone on
        # those, so we must not include it.
        assert CalendarClient._build_time_point(
            "2026-04-14", timezone="Australia/Sydney",
        ) == {"date": "2026-04-14"}

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            CalendarClient._build_time_point("")


class TestResponseStatusMap:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("yes", "accepted"),
            ("accept", "accepted"),
            ("accepted", "accepted"),
            ("no", "declined"),
            ("decline", "declined"),
            ("declined", "declined"),
            ("maybe", "tentative"),
            ("tentative", "tentative"),
            ("needs_action", "needsAction"),
            ("needsaction", "needsAction"),
        ],
    )
    def test_aliases(self, alias, expected):
        assert _RESPONSE_STATUS_MAP[alias] == expected


class TestFormatters:
    def test_format_event_row_basic(self):
        out = CalendarClient._format_event_row({
            "id": "evt-1",
            "summary": "Standup",
            "start": {"dateTime": "2026-04-14T09:00:00+10:00"},
            "end": {"dateTime": "2026-04-14T09:15:00+10:00"},
            "status": "confirmed",
        })
        assert "Standup" in out
        assert "evt-1" in out
        assert "2026-04-14T09:00:00+10:00" in out
        # confirmed is the default — no status tag for confirmed events
        assert "[confirmed]" not in out

    def test_format_event_row_cancelled_tagged(self):
        out = CalendarClient._format_event_row({
            "id": "evt-2",
            "summary": "Cancelled meet",
            "start": {"dateTime": "2026-04-14T09:00:00+10:00"},
            "end": {"dateTime": "2026-04-14T09:15:00+10:00"},
            "status": "cancelled",
        })
        assert "[cancelled]" in out

    def test_format_event_row_all_day(self):
        out = CalendarClient._format_event_row({
            "id": "evt-3",
            "summary": "Holiday",
            "start": {"date": "2026-04-14"},
            "end": {"date": "2026-04-15"},
        })
        assert "Holiday" in out
        assert "2026-04-14" in out

    def test_format_event_detail_includes_attendees(self):
        out = CalendarClient._format_event_detail({
            "id": "evt-9",
            "summary": "Review",
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=x",
            "start": {"dateTime": "2026-04-14T10:00:00+10:00", "timeZone": "Australia/Sydney"},
            "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            "attendees": [
                {"email": "alice@example.com", "responseStatus": "accepted", "organizer": True},
                {"email": "bob@example.com", "responseStatus": "needsAction", "self": True},
            ],
            "location": "Room 3",
        })
        assert "Review" in out
        assert "alice@example.com" in out
        assert "bob@example.com" in out
        assert "organizer" in out
        assert "self" in out
        assert "Room 3" in out
        assert "Australia/Sydney" in out


# ===================================================================
# Tier 2 — mocked Google Calendar service
# ===================================================================


def _chain(mock_service, *path, execute_return=None):
    """Walk a chain like calendars().events().list().execute() and configure
    the final .execute() return value. Returns the terminal execute mock."""
    node = mock_service
    for name in path:
        node = getattr(node, name).return_value
    node.execute.return_value = execute_return
    return node.execute


class TestListCalendars:
    def test_lists_and_formats(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "calendarList", "list",
            execute_return={
                "items": [
                    {"id": "primary", "summary": "Alice", "primary": True, "accessRole": "owner"},
                    {"id": "team@example.com", "summary": "Team", "accessRole": "reader"},
                ],
            },
        )
        out = calendar_client.list_calendars()
        assert "Alice" in out
        assert "(primary)" in out
        assert "Team" in out
        assert "[reader]" in out

    def test_filter(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "calendarList", "list",
            execute_return={
                "items": [
                    {"id": "primary", "summary": "Alice", "primary": True, "accessRole": "owner"},
                    {"id": "team@example.com", "summary": "Team", "accessRole": "reader"},
                ],
            },
        )
        out = calendar_client.list_calendars(filter="team")
        assert "Team" in out
        assert "Alice" not in out

    def test_paginates(self, calendar_client, mock_calendar_service):
        # First call returns a nextPageToken; second call finishes.
        mock_calendar_service.calendarList.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "a", "summary": "A", "accessRole": "owner"}], "nextPageToken": "t1"},
            {"items": [{"id": "b", "summary": "B", "accessRole": "owner"}]},
        ]
        out = calendar_client.list_calendars()
        assert "A" in out
        assert "B" in out


class TestListEvents:
    def test_event_types_filter(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "list",
            execute_return={"items": [{
                "id": "e1", "summary": "Bday", "status": "confirmed",
                "eventType": "birthday",
                "start": {"date": "2026-08-10"}, "end": {"date": "2026-08-11"},
            }]},
        )
        out = calendar_client.list_events(event_types="birthday, focusTime")
        kwargs = mock_calendar_service.events.return_value.list.call_args.kwargs
        assert kwargs["eventTypes"] == ["birthday", "focusTime"]
        assert "[birthday]" in out

    def test_event_types_absent_by_default(self, calendar_client, mock_calendar_service):
        _chain(mock_calendar_service, "events", "list", execute_return={"items": []})
        calendar_client.list_events()
        kwargs = mock_calendar_service.events.return_value.list.call_args.kwargs
        assert "eventTypes" not in kwargs

    def test_returns_rows(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "list",
            execute_return={
                "items": [
                    {
                        "id": "e1", "summary": "Standup",
                        "start": {"dateTime": "2026-04-14T09:00:00+10:00"},
                        "end": {"dateTime": "2026-04-14T09:15:00+10:00"},
                        "status": "confirmed",
                    },
                ],
            },
        )
        out = calendar_client.list_events()
        assert "Standup" in out
        assert "e1" in out

    def test_passes_time_window_and_query(self, calendar_client, mock_calendar_service):
        _chain(mock_calendar_service, "events", "list", execute_return={"items": []})
        calendar_client.list_events(
            time_min="2026-04-14T00:00:00+10:00",
            time_max="2026-04-15T00:00:00+10:00",
            query="standup",
        )
        kwargs = mock_calendar_service.events.return_value.list.call_args.kwargs
        assert kwargs["timeMin"] == "2026-04-14T00:00:00+10:00"
        assert kwargs["timeMax"] == "2026-04-15T00:00:00+10:00"
        assert kwargs["q"] == "standup"
        # single_events defaults to True and drives orderBy=startTime.
        assert kwargs["singleEvents"] is True
        assert kwargs["orderBy"] == "startTime"

    def test_next_page_token_surfaced(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "list",
            execute_return={
                "items": [
                    {
                        "id": "e1", "summary": "s",
                        "start": {"dateTime": "2026-04-14T09:00:00+10:00"},
                        "end": {"dateTime": "2026-04-14T10:00:00+10:00"},
                    },
                ],
                "nextPageToken": "tok-xyz",
            },
        )
        out = calendar_client.list_events()
        assert "tok-xyz" in out

    def test_empty_result(self, calendar_client, mock_calendar_service):
        _chain(mock_calendar_service, "events", "list", execute_return={"items": []})
        out = calendar_client.list_events()
        assert "No events" in out


class TestGetEvent:
    def test_formats_detail(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "get",
            execute_return={
                "id": "e1", "summary": "Planning", "status": "confirmed",
                "htmlLink": "https://cal/e1",
                "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            },
        )
        out = calendar_client.get_event("e1")
        assert "Planning" in out
        assert "e1" in out


class TestCreateEvent:
    def test_minimal(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "insert",
            execute_return={
                "id": "new-1", "summary": "Lunch", "status": "confirmed",
                "htmlLink": "https://cal/new-1",
                "start": {"dateTime": "2026-04-14T12:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T13:00:00+10:00"},
            },
        )
        out = calendar_client.create_event(
            summary="Lunch",
            start="2026-04-14T12:00:00+10:00",
            end="2026-04-14T13:00:00+10:00",
        )
        kwargs = mock_calendar_service.events.return_value.insert.call_args.kwargs
        assert kwargs["calendarId"] == "primary"
        assert kwargs["sendUpdates"] == "none"
        body = kwargs["body"]
        assert body["summary"] == "Lunch"
        assert body["start"] == {"dateTime": "2026-04-14T12:00:00+10:00"}
        assert body["end"] == {"dateTime": "2026-04-14T13:00:00+10:00"}
        assert "attendees" not in body
        assert "recurrence" not in body
        assert "Lunch" in out
        assert "new-1" in out

    def test_all_day(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "insert",
            execute_return={
                "id": "new-2", "summary": "Holiday", "status": "confirmed",
                "htmlLink": "",
                "start": {"date": "2026-04-14"},
                "end": {"date": "2026-04-15"},
            },
        )
        calendar_client.create_event(
            summary="Holiday", start="2026-04-14", end="2026-04-15",
        )
        body = mock_calendar_service.events.return_value.insert.call_args.kwargs["body"]
        assert body["start"] == {"date": "2026-04-14"}
        assert body["end"] == {"date": "2026-04-15"}

    def test_full_featured(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "insert",
            execute_return={
                "id": "new-3", "summary": "Design", "status": "confirmed",
                "htmlLink": "",
                "start": {"dateTime": "2026-04-14T10:00:00", "timeZone": "Australia/Sydney"},
                "end": {"dateTime": "2026-04-14T11:00:00", "timeZone": "Australia/Sydney"},
            },
        )
        calendar_client.create_event(
            summary="Design",
            start="2026-04-14T10:00:00",
            end="2026-04-14T11:00:00",
            description="Arch review",
            location="Zoom",
            timezone="Australia/Sydney",
            attendees=["alice@example.com", "bob@example.com"],
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
            send_updates="all",
            reminders_minutes=[10, 60],
        )
        call = mock_calendar_service.events.return_value.insert.call_args
        assert call.kwargs["sendUpdates"] == "all"
        body = call.kwargs["body"]
        assert body["description"] == "Arch review"
        assert body["location"] == "Zoom"
        assert body["start"]["timeZone"] == "Australia/Sydney"
        assert body["end"]["timeZone"] == "Australia/Sydney"
        assert body["attendees"] == [
            {"email": "alice@example.com"},
            {"email": "bob@example.com"},
        ]
        assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
        assert body["reminders"] == {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
                {"method": "popup", "minutes": 60},
            ],
        }


class TestUpdateEvent:
    def test_patch_only_changed_fields(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "patch",
            execute_return={
                "id": "e1", "summary": "New title", "status": "confirmed",
                "htmlLink": "",
                "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            },
        )
        calendar_client.update_event(event_id="e1", summary="New title")
        body = mock_calendar_service.events.return_value.patch.call_args.kwargs["body"]
        assert body == {"summary": "New title"}

    def test_clear_description(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "patch",
            execute_return={
                "id": "e1", "summary": "x", "status": "confirmed", "htmlLink": "",
                "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            },
        )
        calendar_client.update_event(event_id="e1", description="")
        body = mock_calendar_service.events.return_value.patch.call_args.kwargs["body"]
        assert body == {"description": ""}

    def test_clear_attendees(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "patch",
            execute_return={
                "id": "e1", "summary": "x", "status": "confirmed", "htmlLink": "",
                "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            },
        )
        calendar_client.update_event(event_id="e1", attendees=[])
        body = mock_calendar_service.events.return_value.patch.call_args.kwargs["body"]
        assert body == {"attendees": []}

    def test_rejects_noop(self, calendar_client, mock_calendar_service):
        with pytest.raises(ValueError, match="no fields"):
            calendar_client.update_event(event_id="e1")


class TestDeleteEvent:
    def test_calls_delete(self, calendar_client, mock_calendar_service):
        _chain(mock_calendar_service, "events", "delete", execute_return=None)
        out = calendar_client.delete_event("e1")
        kwargs = mock_calendar_service.events.return_value.delete.call_args.kwargs
        assert kwargs["eventId"] == "e1"
        assert kwargs["calendarId"] == "primary"
        assert kwargs["sendUpdates"] == "none"
        assert "Deleted" in out


class TestQuickAdd:
    def test_passes_text(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "quickAdd",
            execute_return={
                "id": "qa-1", "summary": "Dinner", "status": "confirmed",
                "htmlLink": "",
                "start": {"dateTime": "2026-04-14T19:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T20:00:00+10:00"},
            },
        )
        out = calendar_client.quick_add_event("Dinner tomorrow 7pm")
        kwargs = mock_calendar_service.events.return_value.quickAdd.call_args.kwargs
        assert kwargs["text"] == "Dinner tomorrow 7pm"
        assert "Dinner" in out


class TestMoveEvent:
    def test_move(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "move",
            execute_return={
                "id": "e1", "summary": "Moved", "status": "confirmed",
                "htmlLink": "",
                "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
                "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            },
        )
        calendar_client.move_event(
            event_id="e1", destination_calendar_id="work@example.com",
        )
        kwargs = mock_calendar_service.events.return_value.move.call_args.kwargs
        assert kwargs["calendarId"] == "primary"
        assert kwargs["destination"] == "work@example.com"
        assert kwargs["eventId"] == "e1"


class TestRespondToEvent:
    def _stub_event_with_self(self):
        return {
            "id": "e1", "summary": "Review", "status": "confirmed",
            "htmlLink": "",
            "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
            "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            "attendees": [
                {"email": "carol@example.com", "responseStatus": "accepted"},
                {"email": "alice@example.com", "responseStatus": "needsAction", "self": True},
            ],
        }

    def test_accept_updates_self_attendee(self, calendar_client, mock_calendar_service):
        mock_calendar_service.events.return_value.get.return_value.execute.return_value = (
            self._stub_event_with_self()
        )
        mock_calendar_service.events.return_value.patch.return_value.execute.return_value = (
            self._stub_event_with_self()
        )
        calendar_client.respond_to_event("e1", "yes")
        body = mock_calendar_service.events.return_value.patch.call_args.kwargs["body"]
        self_entry = next(a for a in body["attendees"] if a.get("self"))
        assert self_entry["responseStatus"] == "accepted"

    def test_comment_persisted(self, calendar_client, mock_calendar_service):
        mock_calendar_service.events.return_value.get.return_value.execute.return_value = (
            self._stub_event_with_self()
        )
        mock_calendar_service.events.return_value.patch.return_value.execute.return_value = (
            self._stub_event_with_self()
        )
        calendar_client.respond_to_event("e1", "maybe", comment="Might be late")
        body = mock_calendar_service.events.return_value.patch.call_args.kwargs["body"]
        self_entry = next(a for a in body["attendees"] if a.get("self"))
        assert self_entry["responseStatus"] == "tentative"
        assert self_entry["comment"] == "Might be late"

    def test_adds_self_if_not_attendee(self, calendar_client, mock_calendar_service):
        # Event has no self attendee — account should be appended.
        mock_calendar_service.events.return_value.get.return_value.execute.return_value = {
            "id": "e1", "summary": "Review", "status": "confirmed", "htmlLink": "",
            "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
            "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
            "attendees": [
                {"email": "carol@example.com", "responseStatus": "accepted"},
            ],
        }
        mock_calendar_service.events.return_value.patch.return_value.execute.return_value = {
            "id": "e1", "summary": "Review", "status": "confirmed", "htmlLink": "",
            "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
            "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
        }
        calendar_client.respond_to_event("e1", "accept")
        body = mock_calendar_service.events.return_value.patch.call_args.kwargs["body"]
        # Original attendee plus the appended self entry.
        assert len(body["attendees"]) == 2
        self_entry = next(a for a in body["attendees"] if a.get("self"))
        assert self_entry["email"] == "alice@example.com"
        assert self_entry["responseStatus"] == "accepted"

    def test_bad_response_raises(self, calendar_client):
        with pytest.raises(ValueError, match="Unknown response"):
            calendar_client.respond_to_event("e1", "huh")


class TestListInstances:
    def test_lists(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "events", "instances",
            execute_return={
                "items": [
                    {
                        "id": "e1_1", "summary": "Weekly",
                        "start": {"dateTime": "2026-04-14T10:00:00+10:00"},
                        "end": {"dateTime": "2026-04-14T11:00:00+10:00"},
                    },
                    {
                        "id": "e1_2", "summary": "Weekly",
                        "start": {"dateTime": "2026-04-21T10:00:00+10:00"},
                        "end": {"dateTime": "2026-04-21T11:00:00+10:00"},
                    },
                ],
            },
        )
        out = calendar_client.list_instances("e1")
        assert "e1_1" in out
        assert "e1_2" in out


class TestFreeBusy:
    def test_default_primary(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "freebusy", "query",
            execute_return={
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-04-14T10:00:00+10:00",
                                "end": "2026-04-14T11:00:00+10:00",
                            },
                        ],
                    },
                },
            },
        )
        out = calendar_client.free_busy(
            time_min="2026-04-14T00:00:00+10:00",
            time_max="2026-04-15T00:00:00+10:00",
        )
        body = mock_calendar_service.freebusy.return_value.query.call_args.kwargs["body"]
        assert body["items"] == [{"id": "primary"}]
        assert "1 busy block" in out

    def test_multi_calendar(self, calendar_client, mock_calendar_service):
        _chain(
            mock_calendar_service, "freebusy", "query",
            execute_return={
                "calendars": {
                    "primary": {"busy": []},
                    "team@example.com": {"busy": []},
                },
            },
        )
        out = calendar_client.free_busy(
            time_min="2026-04-14T00:00:00+10:00",
            time_max="2026-04-15T00:00:00+10:00",
            calendar_ids=["primary", "team@example.com"],
        )
        assert "primary: free" in out
        assert "team@example.com: free" in out


# ===================================================================
# Account resolution reuses GmailClient logic via shared TokenStore
# ===================================================================

class TestResolveAccount:
    def test_default(self, calendar_client):
        assert calendar_client._resolve_account("") == "alice@example.com"

    def test_exact(self, calendar_client):
        assert (
            calendar_client._resolve_account("bob@example.com")
            == "bob@example.com"
        )

    def test_unique_partial(self, calendar_client):
        assert calendar_client._resolve_account("alice") == "alice@example.com"

    def test_ambiguous(self, calendar_client):
        with pytest.raises(ValueError, match="Ambiguous"):
            calendar_client._resolve_account("example")

    def test_missing(self, calendar_client):
        with pytest.raises(ValueError, match="not found"):
            calendar_client._resolve_account("missing@example.com")


# ===================================================================
# Calendar management (2026-07-04, full `calendar` scope)
# ===================================================================

class TestCreateCalendar:
    def test_minimal(self, calendar_client, mock_calendar_service):
        mock_calendar_service.calendars().insert().execute.return_value = {
            "id": "cal-new@group.calendar.google.com", "summary": "Family",
        }
        mock_calendar_service.calendars().insert.reset_mock()
        out = calendar_client.create_calendar("Family")
        kwargs = mock_calendar_service.calendars().insert.call_args.kwargs
        assert kwargs["body"] == {"summary": "Family"}
        assert "Calendar created" in out
        assert "cal-new@group.calendar.google.com" in out

    def test_full(self, calendar_client, mock_calendar_service):
        mock_calendar_service.calendars().insert().execute.return_value = {
            "id": "c1", "summary": "Travel", "timeZone": "Australia/Sydney",
        }
        mock_calendar_service.calendars().insert.reset_mock()
        calendar_client.create_calendar(
            "Travel", description="trips", timezone="Australia/Sydney",
        )
        kwargs = mock_calendar_service.calendars().insert.call_args.kwargs
        assert kwargs["body"] == {
            "summary": "Travel",
            "description": "trips",
            "timeZone": "Australia/Sydney",
        }

    def test_empty_summary_rejected(self, calendar_client):
        with pytest.raises(ValueError, match="must not be empty"):
            calendar_client.create_calendar("")


class TestUpdateCalendar:
    def test_patch_only_changed(self, calendar_client, mock_calendar_service):
        mock_calendar_service.calendars().patch().execute.return_value = {
            "id": "c1", "summary": "Renamed",
        }
        mock_calendar_service.calendars().patch.reset_mock()
        out = calendar_client.update_calendar("c1", summary="Renamed")
        kwargs = mock_calendar_service.calendars().patch.call_args.kwargs
        assert kwargs["calendarId"] == "c1"
        assert kwargs["body"] == {"summary": "Renamed"}
        assert "Calendar updated" in out

    def test_clear_description(self, calendar_client, mock_calendar_service):
        mock_calendar_service.calendars().patch().execute.return_value = {
            "id": "c1", "summary": "X",
        }
        mock_calendar_service.calendars().patch.reset_mock()
        calendar_client.update_calendar("c1", description="")
        kwargs = mock_calendar_service.calendars().patch.call_args.kwargs
        assert kwargs["body"] == {"description": ""}

    def test_no_fields_rejected(self, calendar_client):
        with pytest.raises(ValueError, match="no fields"):
            calendar_client.update_calendar("c1")


class TestDeleteCalendar:
    def test_delete_secondary(self, calendar_client, mock_calendar_service):
        out = calendar_client.delete_calendar("cal-2@group.calendar.google.com")
        mock_calendar_service.calendars().delete.assert_called_once_with(
            calendarId="cal-2@group.calendar.google.com",
        )
        assert "Deleted calendar" in out

    def test_primary_alias_refused(self, calendar_client, mock_calendar_service):
        with pytest.raises(ValueError, match="primary"):
            calendar_client.delete_calendar("primary")
        mock_calendar_service.calendars().delete.assert_not_called()

    def test_primary_email_id_refused(self, calendar_client, mock_calendar_service):
        # The primary calendar's real ID is the account email.
        with pytest.raises(ValueError, match="primary"):
            calendar_client.delete_calendar("alice@example.com")
        mock_calendar_service.calendars().delete.assert_not_called()

"""Tier 1 + Tier 2 tests for TasksClient.

Tier 1 covers pure helpers (due-date normalization, formatters). Tier 2
exercises the public methods against a mocked Google Tasks service — the
`tasks_client` fixture short-circuits `_get_service` so no network calls
happen.

Run with:
    uv run --extra test pytest tests/test_tasks_client.py -v
"""
from __future__ import annotations

import pytest

from google_accounts_mcp.tasks_client import TasksClient


# ===================================================================
# Tier 1 — pure helpers
# ===================================================================

class TestNormalizeDue:
    def test_bare_date_expanded(self):
        assert TasksClient._normalize_due("2026-07-05") == "2026-07-05T00:00:00.000Z"

    def test_rfc3339_passthrough(self):
        v = "2026-07-05T09:30:00+10:00"
        assert TasksClient._normalize_due(v) == v


class TestFormatters:
    def test_row_open_task_with_due(self):
        out = TasksClient._format_task_row({
            "id": "t1",
            "title": "Buy milk",
            "status": "needsAction",
            "due": "2026-07-05T00:00:00.000Z",
        })
        assert "[ ]" in out
        assert "Buy milk" in out
        assert "due 2026-07-05" in out
        assert "id=t1" in out

    def test_row_completed_task(self):
        out = TasksClient._format_task_row({
            "id": "t2", "title": "Done", "status": "completed",
        })
        assert "[x]" in out
        assert "due" not in out

    def test_row_untitled_and_subtask(self):
        out = TasksClient._format_task_row({
            "id": "t3", "status": "needsAction", "parent": "t1",
        })
        assert "(untitled)" in out
        assert "subtask of t1" in out

    def test_detail_all_fields(self):
        out = TasksClient._format_task_detail({
            "id": "t1",
            "title": "Buy milk",
            "status": "completed",
            "due": "2026-07-05T00:00:00.000Z",
            "completed": "2026-07-04T22:11:00.000Z",
            "notes": "2 litres",
            "parent": "t0",
            "updated": "2026-07-04T22:11:01.000Z",
            "webViewLink": "https://tasks.google.com/task/t1",
        })
        assert "Task: Buy milk" in out
        assert "due:       2026-07-05" in out
        assert "2 litres" in out
        assert "parent:    t0" in out
        assert "https://tasks.google.com/task/t1" in out

    def test_detail_notes_truncated(self):
        out = TasksClient._format_task_detail({
            "id": "t1", "title": "X", "status": "needsAction",
            "notes": "n" * 600,
        })
        assert "n" * 500 + "…" in out
        assert "n" * 501 not in out


# ===================================================================
# Tier 2 — mocked service
# ===================================================================

class TestListTaskLists:
    def test_lists_and_paginates(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasklists().list().execute.side_effect = [
            {"items": [{"id": "l1", "title": "My Tasks"}], "nextPageToken": "pg2"},
            {"items": [{"id": "l2", "title": "Groceries"}]},
        ]
        out = tasks_client.list_task_lists()
        assert "2 task list(s)" in out
        assert "My Tasks" in out and "Groceries" in out

    def test_filter(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasklists().list().execute.side_effect = [
            {"items": [
                {"id": "l1", "title": "My Tasks"},
                {"id": "l2", "title": "Groceries"},
            ]},
        ]
        out = tasks_client.list_task_lists(filter="grocer")
        assert "Groceries" in out
        assert "My Tasks" not in out

    def test_empty(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasklists().list().execute.side_effect = [{"items": []}]
        assert "No task lists" in tasks_client.list_task_lists()


class TestListTasks:
    def test_default_kwargs(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().list().execute.return_value = {
            "items": [{"id": "t1", "title": "A", "status": "needsAction"}],
        }
        mock_tasks_service.tasks().list.reset_mock()
        out = tasks_client.list_tasks()
        kwargs = mock_tasks_service.tasks().list.call_args.kwargs
        assert kwargs["tasklist"] == "@default"
        assert kwargs["showCompleted"] is True
        assert kwargs["showHidden"] is False
        assert "dueMin" not in kwargs and "pageToken" not in kwargs
        assert "1 task(s) on @default" in out

    def test_show_assigned_flag(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().list().execute.return_value = {
            "items": [{"id": "t1", "title": "From Docs",
                       "status": "needsAction",
                       "assignmentInfo": {"surfaceType": "CONTEXT_TYPE_DOCUMENT"}}],
        }
        mock_tasks_service.tasks().list.reset_mock()
        out = tasks_client.list_tasks(show_assigned=True)
        kwargs = mock_tasks_service.tasks().list.call_args.kwargs
        assert kwargs["showAssigned"] is True
        assert "[assigned via document]" in out

    def test_show_assigned_off_by_default(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().list().execute.return_value = {"items": []}
        mock_tasks_service.tasks().list.reset_mock()
        tasks_client.list_tasks()
        assert "showAssigned" not in mock_tasks_service.tasks().list.call_args.kwargs

    def test_due_window_normalized(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().list().execute.return_value = {"items": []}
        mock_tasks_service.tasks().list.reset_mock()
        tasks_client.list_tasks(due_min="2026-07-01", due_max="2026-07-31")
        kwargs = mock_tasks_service.tasks().list.call_args.kwargs
        assert kwargs["dueMin"] == "2026-07-01T00:00:00.000Z"
        assert kwargs["dueMax"] == "2026-07-31T00:00:00.000Z"

    def test_pagination_token_surfaced(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().list().execute.return_value = {
            "items": [{"id": "t1", "title": "A", "status": "needsAction"}],
            "nextPageToken": "tok-2",
        }
        out = tasks_client.list_tasks()
        assert "next_page_token: tok-2" in out

    def test_empty(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().list().execute.return_value = {"items": []}
        assert "No tasks" in tasks_client.list_tasks()


class TestGetTask:
    def test_get(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().get().execute.return_value = {
            "id": "t1", "title": "A", "status": "needsAction",
        }
        mock_tasks_service.tasks().get.reset_mock()
        out = tasks_client.get_task("t1", task_list="l9")
        mock_tasks_service.tasks().get.assert_called_once_with(
            tasklist="l9", task="t1",
        )
        assert "Task: A" in out


class TestCreateTask:
    def test_minimal(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().insert().execute.return_value = {
            "id": "t1", "title": "Buy milk", "status": "needsAction",
        }
        mock_tasks_service.tasks().insert.reset_mock()
        out = tasks_client.create_task("Buy milk")
        kwargs = mock_tasks_service.tasks().insert.call_args.kwargs
        assert kwargs["tasklist"] == "@default"
        assert kwargs["body"] == {"title": "Buy milk"}
        assert "parent" not in kwargs and "previous" not in kwargs
        assert "Task created" in out

    def test_full(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().insert().execute.return_value = {
            "id": "t2", "title": "Sub", "status": "needsAction",
        }
        mock_tasks_service.tasks().insert.reset_mock()
        tasks_client.create_task(
            "Sub", notes="detail", due="2026-07-05",
            parent="t1", previous="t0",
        )
        kwargs = mock_tasks_service.tasks().insert.call_args.kwargs
        assert kwargs["body"] == {
            "title": "Sub",
            "notes": "detail",
            "due": "2026-07-05T00:00:00.000Z",
        }
        assert kwargs["parent"] == "t1"
        assert kwargs["previous"] == "t0"


class TestUpdateTask:
    def test_patch_only_changed_fields(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().patch().execute.return_value = {
            "id": "t1", "title": "New", "status": "needsAction",
        }
        mock_tasks_service.tasks().patch.reset_mock()
        tasks_client.update_task("t1", title="New")
        kwargs = mock_tasks_service.tasks().patch.call_args.kwargs
        assert kwargs["body"] == {"title": "New"}

    def test_clear_due_sends_null(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().patch().execute.return_value = {
            "id": "t1", "title": "A", "status": "needsAction",
        }
        mock_tasks_service.tasks().patch.reset_mock()
        tasks_client.update_task("t1", due="")
        kwargs = mock_tasks_service.tasks().patch.call_args.kwargs
        assert kwargs["body"] == {"due": None}

    def test_set_due_normalized(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().patch().execute.return_value = {
            "id": "t1", "title": "A", "status": "needsAction",
        }
        mock_tasks_service.tasks().patch.reset_mock()
        tasks_client.update_task("t1", due="2026-08-01")
        kwargs = mock_tasks_service.tasks().patch.call_args.kwargs
        assert kwargs["body"] == {"due": "2026-08-01T00:00:00.000Z"}

    def test_reopen_clears_completed_timestamp(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().patch().execute.return_value = {
            "id": "t1", "title": "A", "status": "needsAction",
        }
        mock_tasks_service.tasks().patch.reset_mock()
        tasks_client.update_task("t1", status="needsAction")
        kwargs = mock_tasks_service.tasks().patch.call_args.kwargs
        assert kwargs["body"] == {"status": "needsAction", "completed": None}

    def test_invalid_status_rejected(self, tasks_client):
        with pytest.raises(ValueError, match="Invalid status"):
            tasks_client.update_task("t1", status="done")

    def test_no_fields_rejected(self, tasks_client):
        with pytest.raises(ValueError, match="no fields"):
            tasks_client.update_task("t1")


class TestCompleteDelete:
    def test_complete(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasks().patch().execute.return_value = {
            "id": "t1", "title": "A", "status": "completed",
        }
        mock_tasks_service.tasks().patch.reset_mock()
        out = tasks_client.complete_task("t1")
        kwargs = mock_tasks_service.tasks().patch.call_args.kwargs
        assert kwargs["body"] == {"status": "completed"}
        assert "Task completed" in out

    def test_delete(self, tasks_client, mock_tasks_service):
        out = tasks_client.delete_task("t1", task_list="l1")
        mock_tasks_service.tasks().delete.assert_called_once_with(
            tasklist="l1", task="t1",
        )
        assert "Deleted task t1" in out


class TestAccountResolution:
    def test_unknown_account(self, tasks_client):
        with pytest.raises(ValueError, match="not found"):
            tasks_client.list_task_lists(account="nobody@example.com")

    def test_substring_match(self, tasks_client, mock_tasks_service):
        mock_tasks_service.tasklists().list().execute.side_effect = [{"items": []}]
        out = tasks_client.list_task_lists(account="alice")
        assert "alice@example.com" in out

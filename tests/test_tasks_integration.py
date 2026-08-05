"""Tier 3 integration tests — hit the real Google Tasks API.

Skipped unless GMAIL_TEST_ACCOUNT is set (the same env var the Gmail
integration tests use). The whole module works inside its OWN task list
(created in a module fixture, deleted afterwards) so nothing on the
account's real lists is ever touched.

Run with:
    GMAIL_TEST_ACCOUNT=bob@example.com \\
    TOKEN_DB_PATH=~/.local/share/mcp/gmail/tokens.db \\
    uv run --extra test pytest tests/test_tasks_integration.py -v

Requires:
  * A refresh token stored for GMAIL_TEST_ACCOUNT that was issued AFTER
    the tasks scope was added to SCOPES (re-run mcp-gmail-reauth against
    the account if this is the first tasks test run).
  * GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars.
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import requires_gmail


@pytest.fixture(scope="module")
def real_tasks():
    """A fully-wired TasksClient against the real Tasks API."""
    from google_accounts_mcp.tasks_client import TasksClient
    return TasksClient()


@pytest.fixture(scope="module")
def test_account():
    return os.environ["GMAIL_TEST_ACCOUNT"]


@pytest.fixture(scope="module")
def scratch_list(real_tasks, test_account):
    """A dedicated task list for this run; deleted on teardown so the
    account is left exactly as found."""
    service, _acct = real_tasks._get_service(test_account)
    title = f"tasks-itest-{uuid.uuid4().hex[:8]}"
    tl = service.tasklists().insert(body={"title": title}).execute()
    yield tl["id"]
    try:
        service.tasklists().delete(tasklist=tl["id"]).execute()
    except Exception:
        pass


def _extract_task_id(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("id:"):
            return line.split("id:", 1)[1].strip()
    raise AssertionError(f"no task id in output:\n{output}")


@requires_gmail
class TestTasksRoundTrip:
    def test_full_lifecycle(self, real_tasks, test_account, scratch_list):
        # Create with a due date and notes.
        out = real_tasks.create_task(
            "integration test task",
            task_list=scratch_list,
            account=test_account,
            notes="created by test_tasks_integration",
            due="2027-01-15",
        )
        assert "Task created" in out
        task_id = _extract_task_id(out)

        # Read back — the Tasks API stores dates only.
        out = real_tasks.get_task(task_id, scratch_list, test_account)
        assert "integration test task" in out
        assert "2027-01-15" in out

        # Appears in the list.
        out = real_tasks.list_tasks(scratch_list, account=test_account)
        assert task_id in out
        assert "[ ]" in out

        # Patch title + clear the due date.
        out = real_tasks.update_task(
            task_id, scratch_list, test_account,
            title="renamed task", due="",
        )
        assert "renamed task" in out
        assert "2027-01-15" not in out

        # Complete, then verify the checkbox flips.
        out = real_tasks.complete_task(task_id, scratch_list, test_account)
        assert "status:    completed" in out

        # Reopen via status patch.
        out = real_tasks.update_task(
            task_id, scratch_list, test_account, status="needsAction",
        )
        assert "status:    needsAction" in out

        # Delete and confirm gone.
        out = real_tasks.delete_task(task_id, scratch_list, test_account)
        assert "Deleted task" in out
        out = real_tasks.list_tasks(scratch_list, account=test_account)
        assert task_id not in out

    def test_subtask_placement(self, real_tasks, test_account, scratch_list):
        parent_out = real_tasks.create_task(
            "parent task", task_list=scratch_list, account=test_account,
        )
        parent_id = _extract_task_id(parent_out)
        child_out = real_tasks.create_task(
            "child task", task_list=scratch_list, account=test_account,
            parent=parent_id,
        )
        child_id = _extract_task_id(child_out)
        out = real_tasks.list_tasks(scratch_list, account=test_account)
        assert f"subtask of {parent_id}" in out
        real_tasks.delete_task(child_id, scratch_list, test_account)
        real_tasks.delete_task(parent_id, scratch_list, test_account)

    def test_scratch_list_visible(self, real_tasks, test_account, scratch_list):
        out = real_tasks.list_task_lists(account=test_account, filter="tasks-itest")
        assert scratch_list in out

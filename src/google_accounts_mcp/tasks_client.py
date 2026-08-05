"""Google Tasks client for gmail-mcp.

Shares the same ``TokenStore`` as ``GmailClient`` — the same OAuth refresh
token covers the ``tasks`` scope (see ``config.SCOPES``). Every Gmail
account in the token DB gets a per-account tasks surface with a separate
cached ``build('tasks', 'v1', ...)`` service.

Design notes
------------
* ``task_list`` defaults to ``'@default'``, the API alias for the account's
  default list — most single-list users never need list_task_lists.
* The Tasks API stores ``due`` as an RFC3339 timestamp but **discards the
  time portion** — only the date is recorded and returned. We accept a bare
  ``YYYY-MM-DD`` and expand it to midnight UTC for the API.
* ``update_task`` uses PATCH semantics like ``CalendarClient.update_event``:
  ``None`` means "leave unchanged", empty string explicitly clears the field
  (for ``due`` a clear is sent as JSON null — the API rejects ``""``).
* Formatters return human-readable strings, matching the other clients.
"""
from __future__ import annotations

import re

from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from google_accounts_mcp.auth import TokenStore
from google_accounts_mcp.config import DB_PATH, DEFAULT_ACCOUNT
from google_accounts_mcp.gmail_client import _HTTP_TIMEOUT, _RetryingHttp


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_VALID_STATUSES = ("needsAction", "completed")


class TasksClient:
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
            authed = AuthorizedHttp(creds, http=_RetryingHttp(timeout=_HTTP_TIMEOUT))
            self._services[account] = build(
                "tasks", "v1",
                http=authed,
                cache_discovery=False,
                static_discovery=True,
            )
        return self._services[account], account

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_due(value: str) -> str:
        """Expand a bare YYYY-MM-DD to the RFC3339 form the API requires.

        The Tasks API only records the date — any time component is
        discarded server-side — so midnight UTC is as good as anything.
        Full RFC3339 values pass through untouched.
        """
        if _DATE_ONLY_RE.match(value):
            return f"{value}T00:00:00.000Z"
        return value

    @staticmethod
    def _format_task_row(task: dict) -> str:
        box = "[x]" if task.get("status") == "completed" else "[ ]"
        title = task.get("title") or "(untitled)"
        due = task.get("due", "")
        # Only the date part is meaningful (see _normalize_due).
        due_s = f"  due {due[:10]}" if due else ""
        parent = task.get("parent", "")
        parent_s = f"  [subtask of {parent}]" if parent else ""
        assigned = task.get("assignmentInfo", {})
        surface = (assigned.get("surfaceType") or "").replace("CONTEXT_TYPE_", "")
        assigned_s = f"  [assigned via {surface.lower()}]" if assigned else ""
        return f"  • {box} {title}{due_s}{parent_s}{assigned_s}  (id={task.get('id', '')})"

    @staticmethod
    def _format_task_detail(task: dict) -> str:
        lines = [
            f"Task: {task.get('title') or '(untitled)'}",
            f"  id:        {task.get('id', '')}",
            f"  status:    {task.get('status', '')}",
        ]
        if task.get("due"):
            lines.append(f"  due:       {task['due'][:10]} (Tasks API stores dates only)")
        if task.get("assignmentInfo"):
            info = task["assignmentInfo"]
            surface = (info.get("surfaceType") or "").replace("CONTEXT_TYPE_", "").lower()
            lines.append(f"  assigned:  via {surface or 'unknown surface'}")
        if task.get("webViewLink"):
            lines.append(f"  link:      {task['webViewLink']}")
        if task.get("completed"):
            lines.append(f"  completed: {task['completed']}")
        if task.get("notes"):
            notes = task["notes"]
            if len(notes) > 500:
                notes = notes[:500] + "…"
            lines.append(f"  notes:     {notes}")
        if task.get("parent"):
            lines.append(f"  parent:    {task['parent']}")
        if task.get("updated"):
            lines.append(f"  updated:   {task['updated']}")
        if task.get("webViewLink"):
            lines.append(f"  link:      {task['webViewLink']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # list_task_lists
    # ------------------------------------------------------------------
    def list_task_lists(self, account: str = "", filter: str = "") -> str:
        service, acct = self._get_service(account)
        items = []
        page_token = None
        while True:
            kwargs = {"maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.tasklists().list(**kwargs).execute()
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if filter:
            f = filter.lower()
            items = [
                tl for tl in items
                if f in tl.get("title", "").lower() or f in tl.get("id", "").lower()
            ]
        if not items:
            suffix = f" matching '{filter}'" if filter else ""
            return f"No task lists found for {acct}{suffix}."
        lines = [f"{len(items)} task list(s) for {acct}:"]
        for tl in items:
            lines.append(f"  • {tl.get('title', '(unnamed)')}  (id={tl.get('id', '')})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # list_tasks
    # ------------------------------------------------------------------
    def list_tasks(
        self,
        task_list: str = "@default",
        account: str = "",
        show_completed: bool = True,
        show_hidden: bool = False,
        due_min: str = "",
        due_max: str = "",
        max_results: int = 50,
        page_token: str = "",
        show_assigned: bool = False,
    ) -> str:
        service, acct = self._get_service(account)
        kwargs = {
            "tasklist": task_list,
            "maxResults": max_results,
            "showCompleted": show_completed,
            "showHidden": show_hidden,
        }
        if show_assigned:
            # Includes tasks assigned to the account from Docs/Chat spaces
            # (API can read/edit but not create these).
            kwargs["showAssigned"] = True
        if due_min:
            kwargs["dueMin"] = self._normalize_due(due_min)
        if due_max:
            kwargs["dueMax"] = self._normalize_due(due_max)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.tasks().list(**kwargs).execute()
        items = resp.get("items", [])
        next_token = resp.get("nextPageToken", "")
        if not items:
            return f"No tasks on {task_list} for {acct}."
        lines = [f"{len(items)} task(s) on {task_list} for {acct}:"]
        for t in items:
            lines.append(self._format_task_row(t))
        if next_token:
            lines.append(f"\n(next_page_token: {next_token})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # get_task
    # ------------------------------------------------------------------
    def get_task(
        self, task_id: str, task_list: str = "@default", account: str = "",
    ) -> str:
        service, _acct = self._get_service(account)
        task = service.tasks().get(tasklist=task_list, task=task_id).execute()
        return self._format_task_detail(task)

    # ------------------------------------------------------------------
    # create_task
    # ------------------------------------------------------------------
    def create_task(
        self,
        title: str,
        task_list: str = "@default",
        account: str = "",
        notes: str = "",
        due: str = "",
        parent: str = "",
        previous: str = "",
    ) -> str:
        """Insert a task. ``parent`` makes it a subtask; ``previous`` places
        it after a sibling task ID (both are insert-position query params,
        not body fields)."""
        service, acct = self._get_service(account)
        body: dict = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = self._normalize_due(due)
        kwargs = {"tasklist": task_list, "body": body}
        if parent:
            kwargs["parent"] = parent
        if previous:
            kwargs["previous"] = previous
        task = service.tasks().insert(**kwargs).execute()
        return (
            f"Task created on {task_list} for {acct}\n"
            + self._format_task_detail(task)
        )

    # ------------------------------------------------------------------
    # update_task (PATCH)
    # ------------------------------------------------------------------
    def update_task(
        self,
        task_id: str,
        task_list: str = "@default",
        account: str = "",
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        status: str | None = None,
    ) -> str:
        """Patch a task. Only non-``None`` fields are sent. ``due=''`` clears
        the due date (sent as JSON null — the API rejects an empty string);
        ``notes=''`` clears the notes. ``status`` must be 'needsAction' or
        'completed'."""
        service, acct = self._get_service(account)
        body: dict = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = self._normalize_due(due) if due else None
        if status is not None:
            if status not in _VALID_STATUSES:
                raise ValueError(
                    f"Invalid status '{status}'. "
                    f"Use one of: {', '.join(_VALID_STATUSES)}."
                )
            body["status"] = status
            if status == "needsAction":
                # Reopening requires clearing the completion timestamp too,
                # or the API rejects the patch as inconsistent.
                body["completed"] = None

        if not body:
            raise ValueError("update_task called with no fields to change")

        task = (
            service.tasks()
            .patch(tasklist=task_list, task=task_id, body=body)
            .execute()
        )
        return (
            f"Task updated on {task_list} for {acct}\n"
            + self._format_task_detail(task)
        )

    # ------------------------------------------------------------------
    # complete_task — sugar for the most common mutation
    # ------------------------------------------------------------------
    def complete_task(
        self, task_id: str, task_list: str = "@default", account: str = "",
    ) -> str:
        service, acct = self._get_service(account)
        task = (
            service.tasks()
            .patch(tasklist=task_list, task=task_id, body={"status": "completed"})
            .execute()
        )
        return (
            f"Task completed on {task_list} for {acct}\n"
            + self._format_task_detail(task)
        )

    # ------------------------------------------------------------------
    # delete_task
    # ------------------------------------------------------------------
    def delete_task(
        self, task_id: str, task_list: str = "@default", account: str = "",
    ) -> str:
        service, acct = self._get_service(account)
        service.tasks().delete(tasklist=task_list, task=task_id).execute()
        return f"Deleted task {task_id} on {task_list} for {acct}."

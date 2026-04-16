# services/tasks_service.py
"""
Google Tasks Service — create, list, and complete personal tasks per user.
Uses tasks scope.
"""

import asyncio
import logging
import re
from typing import Optional

from googleapiclient.discovery import build
from services.auth_service import _load_credentials

logger = logging.getLogger(__name__)


async def _get_tasks_service(user_id: str):
    """Return an authenticated Google Tasks API client for a specific user."""
    creds = await _load_credentials(user_id)
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


async def handle_tasks(query: str, user_id: str) -> str:
    """
    Main entry point — detect action from query and perform it.
    Actions: list, add, complete.
    """
    try:
        service = await _get_tasks_service(user_id)
    except RuntimeError as e:
        return (
            "To manage tasks, you need to connect your Google account first.\n\n"
            "Click the **Connect Google** button at the top of the chat to get started."
        )

    query_lower = query.lower()

    # Detect action
    if any(w in query_lower for w in ["show", "list", "view", "what", "my tasks", "pending", "due"]):
        return await _list_tasks(service, user_id)

    if any(w in query_lower for w in ["add", "create", "new task", "remind", "todo", "to-do"]):
        return await _add_task(service, query, user_id)

    if any(w in query_lower for w in ["complete", "done", "finish", "mark", "completed"]):
        return await _complete_task(service, query, user_id)

    # Default — list tasks
    return await _list_tasks(service, user_id)


async def _list_tasks(service, user_id: str) -> str:
    """List all pending tasks from the default task list."""
    try:
        result = await asyncio.to_thread(
            lambda: service.tasks().list(
                tasklist="@default",
                showCompleted=False,
                maxResults=10,
            ).execute()
        )

        items = result.get("items", [])
        if not items:
            return "You have no pending tasks."

        lines = ["### Your Tasks\n"]
        for i, task in enumerate(items, 1):
            title = task.get("title", "(untitled)")
            due   = task.get("due", "")
            due_str = f" — Due: {due[:10]}" if due else ""
            lines.append(f"{i}. {title}{due_str}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("[Tasks] List failed | user=%s | %s", user_id, e)
        return f"Failed to fetch tasks: {str(e)}"


async def _add_task(service, query: str, user_id: str) -> str:
    """Extract task title from query and create it."""
    # Strip action words to get the task title
    title = re.sub(
        r"(?i)^(add|create|new task|remind me to|todo|to-do|add a task|add task)\s*",
        "", query
    ).strip()

    if not title:
        return "What task would you like to add? Please provide a title."

    try:
        result = await asyncio.to_thread(
            lambda: service.tasks().insert(
                tasklist="@default",
                body={"title": title, "status": "needsAction"},
            ).execute()
        )
        logger.info("[Tasks] Task created | user=%s | title=%s", user_id, title)
        return f"Task added: **{title}**"

    except Exception as e:
        logger.error("[Tasks] Add failed | user=%s | %s", user_id, e)
        return f"Failed to add task: {str(e)}"


async def _complete_task(service, query: str, user_id: str) -> str:
    """Find a task by partial title match and mark it complete."""
    # Extract task name from query
    title_hint = re.sub(
        r"(?i)^(complete|done|finish|mark|mark as done|completed)\s*",
        "", query
    ).strip()

    if not title_hint:
        return "Which task would you like to mark as complete?"

    try:
        # List tasks to find a match
        result = await asyncio.to_thread(
            lambda: service.tasks().list(
                tasklist="@default",
                showCompleted=False,
                maxResults=20,
            ).execute()
        )

        items = result.get("items", [])
        match = next(
            (t for t in items if title_hint.lower() in t.get("title", "").lower()),
            None
        )

        if not match:
            return f"No pending task found matching '{title_hint}'."

        await asyncio.to_thread(
            lambda: service.tasks().update(
                tasklist="@default",
                task=match["id"],
                body={**match, "status": "completed"},
            ).execute()
        )

        logger.info("[Tasks] Task completed | user=%s | title=%s", user_id, match["title"])
        return f"Task marked as complete: **{match['title']}**"

    except Exception as e:
        logger.error("[Tasks] Complete failed | user=%s | %s", user_id, e)
        return f"Failed to complete task: {str(e)}"

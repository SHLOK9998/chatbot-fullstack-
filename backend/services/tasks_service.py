# services/tasks_service.py
"""
Google Tasks Service — create, list, and complete personal tasks per user.
Uses tasks scope.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.discovery import build
from services.auth_service import _load_credentials
from core.dependencies import get_llm
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

_TASK_ACTION_PROMPT = """You are a task operation extractor for a personal task manager.

User query: "{query}"

Extract the operation and relevant data. Return ONLY valid JSON in one of these formats:

For LIST (viewing tasks):
{{"operation": "list"}}

For ADD (creating a new task):
{{"operation": "add", "title": "..."}}

For COMPLETE (marking a task as done):
{{"operation": "complete", "title": "..."}}

Rules:
- For ADD: extract the actual task description as "title" (e.g., "buy milk"). Ignore conversational filler like "Please remind me to" or "I want to add a task to".
- For COMPLETE: extract the name or partial name of the task to complete as "title". Ignore conversational filler.
- If it's just asking to see tasks, return list.
- Return ONLY the JSON. No explanation, no markdown fences.

JSON:"""

async def _extract_task_action(query: str) -> dict:
    llm = get_llm()
    prompt = _TASK_ACTION_PROMPT.format(query=query)
    try:
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = response.content.strip() if hasattr(response, "content") else str(response).strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "operation" in parsed:
            logger.info("[Tasks] Extracted action: %s", parsed.get("operation"))
            return parsed
    except Exception as e:
        logger.warning("[Tasks] Action extraction failed: %s", e)
    return {}


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

    action = await _extract_task_action(query)
    operation = action.get("operation", "list").lower()

    if operation == "add":
        title = action.get("title", "")
        if not title:
            return "What task would you like to add? Try: \"Add task: review the Q3 report\""
        return await _add_task(service, title, user_id)

    elif operation == "complete":
        title = action.get("title", "")
        if not title:
            return "Which task would you like to mark as complete? Say \"complete [task name]\"."
        return await _complete_task(service, title, user_id)

    else:
        # Default — list tasks
        return await _list_tasks(service, user_id)

async def _list_tasks(service, user_id: str) -> str:
    """List all pending tasks from the default task list with formatted output."""
    try:
        result = await asyncio.to_thread(
            lambda: service.tasks().list(
                tasklist="@default",
                showCompleted=False,
                showHidden=False,
                maxResults=20,
            ).execute()
        )

        items = result.get("items", [])
        if not items:
            return "You have no pending tasks. To add one, say \"Add task: review the Q3 report\"."

        def due_key(t):
            return t.get("due", "9999")
        items.sort(key=due_key)

        lines = [f"You have {len(items)} pending task(s):\n"]

        for task in items:
            title = task.get("title", "(untitled)")
            due = task.get("due", None)
            due_str = due[:10] if due else "no due"
            lines.append(f"\n• {title} : {due_str}")

        lines.append("\nSay \"complete [task name]\" to mark a task done, or \"add task: [description]\" to add a new one.")
        return "\n".join(lines)

    except Exception as e:
        logger.error("[Tasks] List failed | user=%s | %s", user_id, e)
        return "I couldn't fetch your tasks right now. Please try again in a moment."


async def _add_task(service, title: str, user_id: str) -> str:
    """Create a task with the given title."""
    try:
        result = await asyncio.to_thread(
            lambda: service.tasks().insert(
                tasklist="@default",
                body={"title": title, "status": "needsAction"},
            ).execute()
        )
        logger.info("[Tasks] Task created | user=%s | title=%s", user_id, title)
        return f"Task added: **{title}**\n\nSay \"show my tasks\" to see all pending tasks."

    except Exception as e:
        logger.error("[Tasks] Add failed | user=%s | %s", user_id, e)
        return "I couldn't add the task right now. Please try again."


async def _complete_task(service, title_hint: str, user_id: str) -> str:
    """Find a task by partial title match and mark it complete, with disambiguation."""
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

        # Find all matches, not just the first
        matches = [t for t in items if title_hint.lower() in t.get("title", "").lower()]

        if not matches:
            return (
                f"No pending task found matching \"{title_hint}\".\n\n"
                f"Say \"show my tasks\" to see your full list."
            )

        if len(matches) > 1:
            task_list = "\n".join(f"{i+1}. {m['title']}" for i, m in enumerate(matches))
            return (
                f"I found {len(matches)} tasks matching \"{title_hint}\":\n\n{task_list}\n\n"
                f"Please be more specific — say \"complete\" followed by the exact title."
            )

        match = matches[0]
        await asyncio.to_thread(
            lambda: service.tasks().update(
                tasklist="@default",
                task=match["id"],
                body={**match, "status": "completed"},
            ).execute()
        )

        logger.info("[Tasks] Task completed | user=%s | title=%s", user_id, match["title"])
        return f"Marked as complete: **{match['title']}**"

    except Exception as e:
        logger.error("[Tasks] Complete failed | user=%s | %s", user_id, e)
        return "I couldn't update the task right now. Please try again."

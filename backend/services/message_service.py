# services/message_service.py
"""
Manages the 'messages' collection in MongoDB.

Every user message and assistant reply is saved here.

CHANGES FROM ORIGINAL:
  1. save_message() now returns (new_count, summarized_up_to) — a tuple.
     Both values come from increment_message_count() which does a single
     atomic find_one_and_update. No separate DB read needed, no race condition.

  2. get_messages_from_offset(thread_id, start) added.
     Fetches all messages from position `start` to end of thread (no upper bound).
     Used by flush_session_summary() to grab all unsummarised messages without
     needing to know the total count in advance.

MongoDB schema:
{
  "_id":       ObjectId(),
  "thread_id": "thread_07",
  "role":      "user",
  "content":   "Who is the intern?",
  "timestamp": ISODate(...)
}
"""

import logging
from datetime import datetime, timezone

from core.database import get_db
from services.thread_service import increment_message_count, get_thread, get_thread_message_count

logger = logging.getLogger(__name__)

COLLECTION            = "messages"
DEFAULT_HISTORY_LIMIT = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def save_message(thread_id: str, role: str, content: str) -> tuple[int, int]:
    """
    Save one message to MongoDB and atomically increment the thread counter.

    Returns:
        (new_message_count, summarized_up_to) — both from a single atomic DB op.
        Pass these directly to maybe_update_summary() — no extra DB read needed.

    Called twice per turn:
        _, _          = await save_message(thread_id, "user",      user_query)
        count, up_to  = await save_message(thread_id, "assistant", reply)
        # use count + up_to to decide whether to trigger summary
    """
    if not content or not content.strip():
        logger.warning("[Message] Skipping empty message | thread=%s role=%s", thread_id, role)
        # Return safe values without incrementing
        thread = await get_thread(thread_id)
        if thread:
            return thread.get("message_count", 0), thread.get("summarized_up_to", 0)
        return 0, 0

    db = get_db()

    await db[COLLECTION].insert_one({
        "thread_id": thread_id,
        "role":      role,
        "content":   content,
        "timestamp": _now(),
    })

    # Single atomic increment — returns (new_count, summarized_up_to)
    new_count, summarized_up_to = await increment_message_count(thread_id)

    logger.info(
        "[Message] Saved | thread=%s role=%s len=%d count=%d up_to=%d",
        thread_id, role, len(content), new_count, summarized_up_to,
    )
    return new_count, summarized_up_to


async def get_recent_messages(thread_id: str, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict]:
    """
    Fetch the most recent N messages, sorted oldest → newest (for LLM prompt).
    """
    db = get_db()
    cursor = db[COLLECTION].find(
        {"thread_id": thread_id},
        sort=[("timestamp", -1)],
        limit=limit,
    )
    messages = await cursor.to_list(length=limit)
    messages.reverse()
    logger.info("[Message] Loaded %d recent messages | thread=%s", len(messages), thread_id)
    return messages


async def format_history_from_db(thread_id: str, limit: int = DEFAULT_HISTORY_LIMIT) -> str:
    """
    Load recent messages and format as plain text for LLM prompt injection.

    Example output:
        User: who is the full stack intern?
        Assistant: Anand Vaghela is a Full Stack Intern...
    """
    messages = await get_recent_messages(thread_id, limit=limit)
    if not messages:
        return ""

    lines = []
    for msg in messages:
        role    = "User" if msg["role"] == "user" else "Assistant"
        content = msg.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def get_all_messages(thread_id: str) -> list[dict]:
    """Fetch ALL messages for a thread in chronological order."""
    db = get_db()
    cursor = db[COLLECTION].find({"thread_id": thread_id}, sort=[("timestamp", 1)])
    return await cursor.to_list(length=None)


async def get_messages_in_range(thread_id: str, start: int, end: int) -> list[dict]:
    """
    Fetch messages by 1-based position range.
    start=1, end=30 → first 30 messages.
    start=31, end=60 → messages 31 through 60.
    """
    db    = get_db()
    count = end - start + 1
    cursor = (
        db[COLLECTION]
        .find({"thread_id": thread_id}, sort=[("timestamp", 1)])
        .skip(max(0, start - 1))
        .limit(count)
    )
    return await cursor.to_list(length=count)


async def get_messages_from_offset(thread_id: str, start: int) -> list[dict]:
    """
    Fetch all messages from 1-based position `start` to the end of the thread.
    Used by flush_session_summary() to grab all unsummarised messages.

    Example: summarized_up_to=30, total=42 → call with start=31 → returns msgs 31-42.
    """
    if start < 1:
        start = 1

    db = get_db()
    cursor = (
        db[COLLECTION]
        .find({"thread_id": thread_id}, sort=[("timestamp", 1)])
        .skip(start - 1)
    )
    messages = await cursor.to_list(length=None)
    logger.info("[Message] Loaded %d messages from offset %d | thread=%s", len(messages), start, thread_id)
    return messages


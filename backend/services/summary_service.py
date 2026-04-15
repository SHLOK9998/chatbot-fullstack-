# services/summary_service.py
"""
Rolling conversation summary — ONE summary document per thread.

MODEL:
  - ONE MongoDB document per thread in the 'summaries' collection (upsert).
  - Every 15 rounds (= 30 messages) the LLM merges those messages into the
    existing summary. The result REPLACES the old summary (rolling upsert).
  - On session end (Ctrl+C or explicit call), all remaining unsummarised
    messages are merged in — so ZERO messages are ever left outside the summary.

REDIS CACHING (new):
  Two functions are now Redis-aware. Everything else is unchanged.

  get_thread_summary(thread_id):
    Redis key: "summary:{thread_id}"
    READ:  Redis first → MongoDB on miss → store in Redis
    This is called on EVERY LLM prompt, so caching here saves the most reads.
    The summary only changes every 30 messages or at session end.

  _upsert_summary(thread_id, text):
    Writes to MongoDB (source of truth) then immediately updates Redis.
    This is the ONLY place summary text is ever written, so Redis is always
    in sync after every write — no separate invalidation needed.

CROSS-THREAD CONTEXT:
  get_past_thread_summaries() delegates to thread_service (which has Redis cache).
  chat_service calls this to build the PREVIOUS SESSION blocks in LLM prompts.

MongoDB summaries schema:
{
  "thread_id":    "thread_3f2a1b9c",
  "summary_text": "The user asked about...",
  "updated_at":   ISODate(...),
  "created_at":   ISODate(...),
}
"""

import asyncio
import logging
from datetime import datetime, timezone

from core.database import get_db
from core.redis_client import redis_get, redis_set, redis_delete
from services.message_service import get_messages_in_range, get_messages_from_offset
from services.thread_service import (
    update_summarized_up_to,
    get_thread,
    get_past_thread_summaries as _get_past_thread_summaries,
)

logger = logging.getLogger(__name__)

COLLECTION = "summaries"

# 15 turns × 2 messages per turn = 30 messages per summary window
SUMMARY_EVERY_TURNS  = 15
MESSAGES_PER_SUMMARY = SUMMARY_EVERY_TURNS * 2   # = 30

# Redis key pattern for current thread summary
_SUMMARY_KEY = "summary:{}"          # .format(thread_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Read current thread's rolling summary ─────────────────────────────────────

async def get_thread_summary(thread_id: str) -> str:
    """
    Return the current rolling summary text for this thread.
    Returns "" if no summary exists yet (new thread, fewer than 30 messages).

    REDIS: checks cache first. Falls through to MongoDB on miss.
    After message 30 the summary exists and Redis stays warm for the rest
    of the session — saving one MongoDB read on every single LLM call.
    """
    key = _SUMMARY_KEY.format(thread_id)

    # Step 1: Redis (fast path — 0.1ms)
    cached = await redis_get(key)
    if cached is not None:
        logger.debug("[Summary] Redis HIT | thread=%s", thread_id)
        return cached

    # Step 2: MongoDB (slow path — first call only, or after a server restart)
    db  = get_db()
    doc = await db[COLLECTION].find_one({"thread_id": thread_id})
    text = doc["summary_text"] if doc else ""

    # Step 3: Store in Redis for next call (only if there is actual content)
    if text:
        await redis_set(key, text)
        logger.debug("[Summary] Redis SET | thread=%s | len=%d", thread_id, len(text))

    return text


# ── Cross-thread: all past summaries for this user ────────────────────────────

async def get_past_thread_summaries(user_id: str, current_thread_id: str) -> list[dict]:
    """
    Fetch summaries from ALL past threads for this user, excluding the current thread.
    Delegates to thread_service which has its own Redis cache.

    Returns list ordered oldest→newest:
    [{"session_num": 1, "thread_id": ..., "title": ..., "summary_text": ...}, ...]
    """
    return await _get_past_thread_summaries(user_id, current_thread_id)


# ── LLM merge ─────────────────────────────────────────────────────────────────

async def _merge_into_summary(existing_summary: str, new_messages: list[dict], llm) -> str:
    """
    Ask the LLM to merge `new_messages` into the existing rolling summary.
    Returns ONE updated summary (5-8 sentences), or existing on failure.
    """
    if not new_messages:
        return existing_summary

    conversation_block = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in new_messages
    )

    existing_section = (
        f"EXISTING SUMMARY:\n{existing_summary}"
        if existing_summary
        else "EXISTING SUMMARY:\n(none — this is the first summary for this thread)"
    )

    prompt = (
        "You maintain a rolling conversation summary for a persistent AI assistant.\n\n"
        f"{existing_section}\n\n"
        "NEW MESSAGES TO MERGE IN:\n"
        f"{conversation_block}\n\n"
        "INSTRUCTIONS:\n"
        "- Produce ONE updated summary merging the new messages into the existing summary.\n"
        "- Preserve: user goals, key decisions, names, employee details, actions taken.\n"
        "- Add: new topics, questions, information retrieved, CRUD operations done.\n"
        "- Remove: details now redundant or superseded by newer information.\n"
        "- Length: 5–8 sentences maximum.\n"
        "- Write in third-person past tense: 'The user asked about X. The assistant found Y.'\n"
        "- Do NOT include filler like 'This summary covers...' — just the facts.\n\n"
        "UPDATED SUMMARY:"
    )

    try:
        from langchain_core.messages import HumanMessage
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        merged   = (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )
        return merged if merged else existing_summary

    except Exception as e:
        logger.exception("[Summary] _merge_into_summary failed: %s", e)
        return existing_summary


# ── Upsert ────────────────────────────────────────────────────────────────────

async def _upsert_summary(thread_id: str, text: str) -> None:
    """
    Write (or overwrite) the single rolling summary for this thread.

    WRITES TO:
      1. MongoDB  — source of truth, always written first
      2. Redis    — updated immediately so next read is a cache hit

    This is the ONLY place summary text is ever written.
    Both the 15-round trigger and the session-end flush call this function,
    so Redis stays in sync automatically — no extra invalidation needed.
    """
    # MongoDB first (source of truth)
    db  = get_db()
    now = _now()
    await db[COLLECTION].update_one(
        {"thread_id": thread_id},
        {
            "$set":         {"summary_text": text, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    # Redis: update so next get_thread_summary() call is a cache hit
    await redis_set(_SUMMARY_KEY.format(thread_id), text)

    logger.info("[Summary] Upserted | thread=%s | len=%d", thread_id, len(text))


# ── Core shared logic ──────────────────────────────────────────────────────────

async def _run_summary_update(
    thread_id: str,
    messages:  list[dict],
    end_msg:   int,
    llm,
) -> None:
    """
    Shared by maybe_update_summary() and flush_session_summary():
      1. Load existing rolling summary (Redis → MongoDB).
      2. LLM merges new messages into it.
      3. Upsert result to MongoDB + Redis.
      4. Advance summarized_up_to pointer in MongoDB.
    """
    if not messages:
        logger.info("[Summary] No messages to merge | thread=%s — skipping.", thread_id)
        return

    existing = await get_thread_summary(thread_id)
    updated  = await _merge_into_summary(existing, messages, llm)

    await _upsert_summary(thread_id, updated)
    await update_summarized_up_to(thread_id, end_msg)

    logger.info(
        "[Summary] Updated | thread=%s | up_to=%d | len=%d",
        thread_id, end_msg, len(updated),
    )


# ── Public: 15-round trigger ──────────────────────────────────────────────────

async def maybe_update_summary(
    thread_id:         str,
    new_message_count: int,
    summarized_up_to:  int,
    llm,
) -> None:
    """
    Trigger a rolling summary update if 30+ new messages have accumulated
    since the last summary update.

    Called from chat_service._save_turn_to_mongodb() after every turn.
    Fires when: (new_message_count - summarized_up_to) >= 30.
    """
    unsummarised = new_message_count - summarized_up_to

    if unsummarised < MESSAGES_PER_SUMMARY:
        logger.debug(
            "[Summary] Not enough new messages (%d/%d) | thread=%s",
            unsummarised, MESSAGES_PER_SUMMARY, thread_id,
        )
        return

    start_msg = summarized_up_to + 1
    end_msg   = summarized_up_to + MESSAGES_PER_SUMMARY

    logger.info(
        "[Summary] 15-round trigger | thread=%s | window=%d–%d",
        thread_id, start_msg, end_msg,
    )

    messages = await get_messages_in_range(thread_id, start_msg, end_msg)
    await _run_summary_update(thread_id, messages, end_msg, llm)


# ── Public: session-end flush ─────────────────────────────────────────────────

async def flush_session_summary(thread_id: str, llm) -> bool:
    """
    Flush ALL unsummarised messages into the rolling summary.

    Call on session end (Ctrl+C, 'exit', FastAPI shutdown).
    Returns True if a flush was performed, False if nothing to flush.

    After this runs, _upsert_summary() has already updated Redis with the
    final summary text. chat_service.end_session() then deletes the
    past_summaries cache so the next session loads fresh data from MongoDB.
    """
    thread = await get_thread(thread_id)
    if not thread:
        logger.warning("[Summary] flush: thread '%s' not found.", thread_id)
        return False

    message_count    = thread.get("message_count", 0)
    summarized_up_to = thread.get("summarized_up_to", 0)
    unsummarised     = message_count - summarized_up_to

    if unsummarised <= 0:
        logger.info("[Summary] flush: nothing to flush | thread=%s", thread_id)
        return False

    start_msg = summarized_up_to + 1
    end_msg   = message_count

    logger.info(
        "[Summary] Session-end flush | thread=%s | window=%d–%d (%d messages)",
        thread_id, start_msg, end_msg, unsummarised,
    )

    messages = await get_messages_from_offset(thread_id, start_msg)
    await _run_summary_update(thread_id, messages, end_msg, llm)
    return True


# ── Backward-compat shims ─────────────────────────────────────────────────────

async def maybe_create_summary(thread_id: str, message_count: int, llm) -> None:
    """DEPRECATED shim — delegates to maybe_update_summary."""
    thread           = await get_thread(thread_id)
    summarized_up_to = thread.get("summarized_up_to", 0) if thread else 0
    await maybe_update_summary(thread_id, message_count, summarized_up_to, llm)


async def get_relevant_summaries(thread_id: str, query: str, top_k: int = 3) -> list[dict]:
    """DEPRECATED shim — returns current thread's rolling summary in a list."""
    text = await get_thread_summary(thread_id)
    return [{"summary_text": text}] if text else []


# ── Debug helper ──────────────────────────────────────────────────────────────

async def get_all_summaries(thread_id: str) -> list[dict]:
    """Return the summary doc for this thread (0 or 1 items). Always reads MongoDB."""
    db     = get_db()
    cursor = db[COLLECTION].find({"thread_id": thread_id})
    return await cursor.to_list(length=1)



# services/thread_service.py
"""
ChatGPT-style thread management.

DESIGN:
  - Every fresh session start creates a NEW thread via create_new_thread().
  - Thread title starts as "New Conversation" (placeholder).
    After the first user message + assistant reply, an LLM generates a
    short 4-6 word title and update_thread_title() sets it — non-blocking.
  - Past threads are never deleted — they stay in MongoDB.
  - list_threads() returns all past threads newest-first (sidebar-style).
  - set_active_thread() reopens a past thread to continue from where it left off.

REDIS CACHING (new):
  get_past_thread_summaries() now uses Redis cache.
  Redis key: "past_summaries:{user_id}"
  This $lookup aggregation is the most expensive read in the whole project
  (joins two collections) and runs on every single LLM call. Caching it
  means the $lookup only runs ONCE per session (on the first message),
  and every subsequent call is a Redis hit.

  INVALIDATION: chat_service.end_session() deletes this key after flush.
  This happens because the current thread is now a "past thread" and the
  cached list must be refreshed on the next session start.

ROLLING SUMMARY SUPPORT (per-thread):
  increment_message_count() returns (new_count, summarized_up_to) atomically.

MongoDB threads schema:
{
  "thread_id":         "thread_3f2a1b9c",
  "user_id":           "shlok",
  "title":             "Employee salary queries",
  "active":            True,
  "created_at":        ISODate(...),
  "updated_at":        ISODate(...),
  "message_count":     42,
  "summarized_up_to":  30,
}
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.database import get_db
from core.redis_client import redis_get_json, redis_set_json, redis_delete

logger = logging.getLogger(__name__)

COLLECTION = "threads"

# Redis key pattern for past thread summaries
_PAST_KEY = "past_summaries:{}"    # .format(user_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_thread_id() -> str:
    return "thread_" + uuid.uuid4().hex[:8]


# ── Create ────────────────────────────────────────────────────────────────────

async def create_new_thread(user_id: str) -> str:
    """
    Create a brand-new thread and mark it active for this user.
    All previous threads are marked active=False (they remain in DB).
    Title starts as "New Conversation" — updated after first turn.
    """
    db        = get_db()
    thread_id = _make_thread_id()

    await db[COLLECTION].update_many(
        {"user_id": user_id},
        {"$set": {"active": False}},
    )

    await db[COLLECTION].insert_one({
        "thread_id":        thread_id,
        "user_id":          user_id,
        "title":            "New Conversation",
        "active":           True,
        "created_at":       _now(),
        "updated_at":       _now(),
        "message_count":    0,
        "summarized_up_to": 0,
    })

    logger.info("[Thread] Created new thread '%s' for user '%s'", thread_id, user_id)
    return thread_id


# ── Update title ──────────────────────────────────────────────────────────────

async def update_thread_title(thread_id: str, title: str) -> None:
    """Set the LLM-generated title. Called once after first turn (background task)."""
    if not title or not title.strip():
        return
    clean = title.strip().strip('"').strip("'")
    db    = get_db()
    await db[COLLECTION].update_one(
        {"thread_id": thread_id},
        {"$set": {"title": clean, "updated_at": _now()}},
    )
    logger.info("[Thread] Title updated | thread=%s | title='%s'", thread_id, clean)


# ── Read active thread ────────────────────────────────────────────────────────

async def get_active_thread(user_id: str) -> Optional[str]:
    """Return the thread_id of the currently active thread, or None."""
    db  = get_db()
    doc = await db[COLLECTION].find_one(
        {"user_id": user_id, "active": True},
        sort=[("updated_at", -1)],
    )
    return doc["thread_id"] if doc else None


# ── Switch thread ─────────────────────────────────────────────────────────────

async def set_active_thread(user_id: str, thread_id: str) -> bool:
    """Reopen a past thread. Marks it active=True, all others active=False."""
    db     = get_db()
    target = await db[COLLECTION].find_one({"thread_id": thread_id, "user_id": user_id})
    if not target:
        logger.warning("[Thread] set_active_thread: '%s' not found for user '%s'", thread_id, user_id)
        return False

    await db[COLLECTION].update_many({"user_id": user_id}, {"$set": {"active": False}})
    await db[COLLECTION].update_one(
        {"thread_id": thread_id},
        {"$set": {"active": True, "updated_at": _now()}},
    )
    logger.info("[Thread] Switched active thread → '%s' for user '%s'", thread_id, user_id)
    return True


# ── Sidebar listing ───────────────────────────────────────────────────────────

async def list_threads(user_id: str, limit: int = 30) -> list[dict]:
    """Return all threads for a user, newest first. Always reads MongoDB."""
    db = get_db()
    cursor = db[COLLECTION].find(
        {"user_id": user_id},
        projection={
            "_id": 0, "thread_id": 1, "title": 1, "active": 1,
            "updated_at": 1, "message_count": 1, "created_at": 1,
        },
        sort=[("updated_at", -1)],
        limit=limit,
    )
    threads = await cursor.to_list(length=limit)
    logger.info("[Thread] Listed %d threads for user '%s'", len(threads), user_id)
    return threads


# ── Cross-thread summary fetch (with Redis cache) ─────────────────────────────

async def get_past_thread_summaries(
    user_id: str,
    current_thread_id: str,
) -> list[dict]:
    """
    Fetch summaries from ALL past threads for this user, excluding the current thread.

    REDIS CACHE:
      Key: "past_summaries:{user_id}"
      This expensive $lookup aggregation runs once per session (first message).
      After that, every call hits Redis instead of MongoDB.

      WHEN IS IT INVALIDATED?
        chat_service.end_session() deletes this key after flushing the session summary.
        This happens because after a session ends, the current thread becomes a past
        thread — the cached list is now stale and must be refreshed.
        Next session's first message → Redis miss → MongoDB fetches updated list.

    Returns list ordered oldest→newest, each item:
    {
        "session_num":  1,
        "thread_id":    "thread_abc123",
        "title":        "Employee salary queries",
        "summary_text": "The user asked about...",
    }
    """
    key = _PAST_KEY.format(user_id)

    # Step 1: Redis (fast path — skip $lookup entirely)
    cached = await redis_get_json(key)
    if cached is not None:
        logger.debug("[Thread] Redis HIT past_summaries | user=%s | count=%d", user_id, len(cached))
        return cached

    # Step 2: MongoDB $lookup aggregation (runs only on first call per session)
    logger.debug("[Thread] Redis MISS past_summaries | user=%s — querying MongoDB", user_id)
    db = get_db()

    pipeline = [
        {
            "$match": {
                "user_id":   user_id,
                "thread_id": {"$ne": current_thread_id},
            }
        },
        {"$sort": {"created_at": 1}},
        {
            "$lookup": {
                "from":         "summaries",
                "localField":   "thread_id",
                "foreignField": "thread_id",
                "as":           "summary_docs",
            }
        },
        {"$match": {"summary_docs.0": {"$exists": True}}},
        {
            "$project": {
                "_id":          0,
                "thread_id":    1,
                "title":        1,
                "created_at":   1,
                "summary_text": {"$arrayElemAt": ["$summary_docs.summary_text", 0]},
            }
        },
    ]

    cursor  = db[COLLECTION].aggregate(pipeline)
    results = await cursor.to_list(length=None)

    # Add 1-based session numbers
    for i, doc in enumerate(results):
        doc["session_num"] = i + 1

    # Step 3: Store in Redis (JSON list). TTL=24h as safety net.
    # This will be explicitly deleted by end_session() when the session ends.
    if results:
        await redis_set_json(key, results)
        logger.debug("[Thread] Redis SET past_summaries | user=%s | count=%d", user_id, len(results))

    logger.info(
        "[Thread] Found %d past summaries | user='%s' (excl. thread='%s')",
        len(results), user_id, current_thread_id,
    )
    return results


async def invalidate_past_summaries_cache(user_id: str) -> None:
    """
    Delete the past_summaries Redis cache for this user.

    Called by chat_service.end_session() after the session summary is flushed.
    This ensures the next session fetches fresh past summaries from MongoDB,
    which will include the summary of the session that just ended.
    """
    key = _PAST_KEY.format(user_id)
    await redis_delete(key)
    logger.info("[Thread] Invalidated past_summaries cache | user=%s", user_id)


# ── Atomic message counter ────────────────────────────────────────────────────

async def increment_message_count(thread_id: str) -> tuple[int, int]:
    """
    Atomically increment message_count by 1.
    Returns (new_message_count, summarized_up_to) — both from one DB operation.
    """
    db = get_db()

    result = await db[COLLECTION].find_one_and_update(
        {"thread_id": thread_id},
        {
            "$inc": {"message_count": 1},
            "$set": {"updated_at": _now()},
        },
        return_document=True,
    )

    if not result:
        logger.warning("[Thread] increment_message_count: thread '%s' not found.", thread_id)
        return 0, 0

    new_count        = result["message_count"]
    summarized_up_to = result.get("summarized_up_to", 0)
    logger.debug("[Thread] count=%d up_to=%d | thread=%s", new_count, summarized_up_to, thread_id)
    return new_count, summarized_up_to


async def update_summarized_up_to(thread_id: str, up_to: int) -> None:
    """Advance the summarized_up_to pointer after a summary merge."""
    db = get_db()
    await db[COLLECTION].update_one(
        {"thread_id": thread_id},
        {"$set": {"summarized_up_to": up_to, "updated_at": _now()}},
    )
    logger.info("[Thread] summarized_up_to → %d | thread=%s", up_to, thread_id)


# ── Read helpers ──────────────────────────────────────────────────────────────

async def get_thread(thread_id: str) -> Optional[dict]:
    """Fetch a single thread document by thread_id. Always reads MongoDB."""
    db = get_db()
    return await db[COLLECTION].find_one({"thread_id": thread_id})


async def get_thread_message_count(thread_id: str) -> int:
    """For admin/debug use only. In the chat flow, use save_message()'s return value."""
    doc = await get_thread(thread_id)
    return doc.get("message_count", 0) if doc else 0


# ── Delete ────────────────────────────────────────────────────────────────────

async def delete_thread(thread_id: str) -> None:
    """Delete a thread and cascade-delete all its messages and summaries."""
    db = get_db()
    await db[COLLECTION].delete_one({"thread_id": thread_id})
    await db["messages"].delete_many({"thread_id": thread_id})
    await db["summaries"].delete_many({"thread_id": thread_id})
    logger.info("[Thread] Deleted thread '%s' and cascade-deleted messages + summaries.", thread_id)


# ── Legacy shim ───────────────────────────────────────────────────────────────

async def get_or_create_thread(user_id: str, first_message: str = "") -> str:
    """Backward-compat shim. Returns the active thread or creates one if none exists."""
    tid = await get_active_thread(user_id)
    if tid:
        return tid
    return await create_new_thread(user_id)



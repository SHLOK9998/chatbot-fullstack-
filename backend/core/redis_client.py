# core/redis_client.py
"""
Redis connection manager.

Same pattern as core/database.py — one shared client for the whole app.
Redis is used to cache two things only:
  1. "summary:{thread_id}"       → current thread's rolling summary text
  2. "past_summaries:{user_id}"  → all past thread summaries (JSON list)

Everything else (messages, threads, counts) stays in MongoDB.

SETUP:
  pip install redis[asyncio]

  Local:  redis-server   (install via brew / apt / choco)
  Cloud:  redis.io/try-free  (free 30MB tier)

  Add to your .env:
    REDIS_URL=redis://localhost:6379

  If REDIS_URL is not set, Redis is disabled and all reads fall through to MongoDB.
  This means the app works perfectly without Redis — Redis only adds speed.
"""

import json
import logging
from typing import Optional
from core.config import settings
logger = logging.getLogger(__name__)

# Single shared Redis client — None until connect_redis() is called
_redis = None


async def connect_redis(url: str = None) -> None:
    """
    Called once at app startup (in main.py lifespan).
    Creates the async Redis client and pings to verify the connection.

    If url is None, reads from settings.REDIS_URL.
    If REDIS_URL is not configured, logs a warning and skips Redis silently.
    The app continues to work using MongoDB only.
    """
    global _redis

    # Import here so redis package is optional
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning("[Redis] redis[asyncio] not installed — Redis disabled. Run: pip install redis[asyncio]")
        return

    # Get URL from settings if not passed directly
    if url is None:
        try:
            
            url = getattr(settings, "REDIS_URL", None)
        except Exception:
            url = None

    if not url:
        logger.warning("[Redis] REDIS_URL not configured — Redis disabled. Add REDIS_URL to .env to enable caching.")
        return

    try:
        _redis = aioredis.from_url(url, decode_responses=True)
        await _redis.ping()
        logger.info("[Redis] Connected | url=%s", url)
    except Exception as e:
        logger.warning("[Redis] Connection failed (%s) — falling back to MongoDB only.", e)
        _redis = None


async def close_redis() -> None:
    """Called at app shutdown (in main.py lifespan)."""
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None
        logger.info("[Redis] Connection closed.")


def get_redis():
    """
    Returns the Redis client, or None if Redis is not connected.

    All callers must handle None gracefully — Redis is optional.
    If None is returned, fall through to MongoDB as normal.

    Usage:
        r = get_redis()
        if r:
            cached = await r.get("some_key")
    """
    return _redis


# ── Helpers used by summary_service and thread_service ───────────────────────

async def redis_get(key: str) -> Optional[str]:
    """Get a string value from Redis. Returns None on miss or if Redis is down."""
    r = get_redis()
    if not r:
        return None
    try:
        return await r.get(key)
    except Exception as e:
        logger.warning("[Redis] GET '%s' failed: %s", key, e)
        return None


async def redis_set(key: str, value: str, ex: int = 86400) -> None:
    """
    Set a string value in Redis with a TTL (default 24 hours).
    Silent no-op if Redis is not connected.

    ex: expiry in seconds. 86400 = 24 hours.
    The TTL is a safety net — keys are explicitly deleted on invalidation,
    but TTL ensures stale data never persists if a delete is missed.
    """
    r = get_redis()
    if not r:
        return
    try:
        await r.set(key, value, ex=ex)
    except Exception as e:
        logger.warning("[Redis] SET '%s' failed: %s", key, e)


async def redis_delete(key: str) -> None:
    """Delete a key from Redis. Silent no-op if Redis is not connected."""
    r = get_redis()
    if not r:
        return
    try:
        await r.delete(key)
    except Exception as e:
        logger.warning("[Redis] DELETE '%s' failed: %s", key, e)


async def redis_get_json(key: str) -> Optional[list]:
    """Get a JSON-encoded list from Redis. Returns None on miss."""
    raw = await redis_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("[Redis] JSON parse failed for key '%s': %s", key, e)
        return None


async def redis_set_json(key: str, value: list, ex: int = 86400) -> None:
    """Store a list as JSON in Redis."""
    try:
        await redis_set(key, json.dumps(value, default=str), ex=ex)
    except Exception as e:
        logger.warning("[Redis] JSON set failed for key '%s': %s", key, e)
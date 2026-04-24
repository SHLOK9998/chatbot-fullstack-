"""
debug_check.py — Verify Redis + MongoDB are both working correctly.

Run this BEFORE chatting to confirm your setup is healthy.
It does NOT call the LLM — it only checks connections and data.

Usage:
    python debug_check.py

What it checks:
  1. MongoDB connection + collections exist
  2. Redis connection + read/write/delete works
  3. Current active thread + message count + summarized_up_to
  4. Current thread's summary (Redis vs MongoDB — shows both)
  5. All past threads + their summaries (Redis vs MongoDB — shows both)
  6. Redis cache hit/miss status for both keys

Run it DURING a session (while uvicorn is running) to see live data.
"""

import asyncio
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main():
    print("\n" + "═" * 60)
    print("  REDIS + MONGODB HEALTH CHECK")
    print("═" * 60)

    # ── 1. Connect ────────────────────────────────────────────────
    print("\n[1] CONNECTIONS")

    try:
        from core.database import connect_db, get_db
        await connect_db()
        db = get_db()
        # Ping
        await db.command("ping")
        print("  ✓ MongoDB connected")
    except Exception as e:
        print(f"  ✗ MongoDB FAILED: {e}")
        return

    try:
        from core.redis_client import connect_redis, get_redis
        await connect_redis()
        r = get_redis()
        if r:
            await r.ping()
            print("  ✓ Redis connected")
        else:
            print("  ⚠ Redis DISABLED (REDIS_URL not set in .env) — app uses MongoDB only")
    except Exception as e:
        print(f"  ✗ Redis FAILED: {e}")

    r = get_redis()

    # ── 2. MongoDB collections ─────────────────────────────────────
    print("\n[2] MONGODB COLLECTIONS")

    for col in ["threads", "messages", "summaries"]:
        try:
            count = await db[col].count_documents({})
            print(f"  ✓ {col:<12} — {count} documents")
        except Exception as e:
            print(f"  ✗ {col} — ERROR: {e}")

    # ── 3. Active thread ───────────────────────────────────────────
    print("\n[3] ACTIVE THREAD")

    USER_ID = "shlok"

    thread_doc = await db["threads"].find_one(
        {"user_id": USER_ID, "active": True},
        sort=[("updated_at", -1)],
    )

    if not thread_doc:
        print("  ⚠ No active thread found. Start uvicorn first.")
    else:
        tid = thread_doc["thread_id"]
        print(f"  thread_id      : {tid}")
        print(f"  title          : {thread_doc.get('title', '?')}")
        print(f"  message_count  : {thread_doc.get('message_count', 0)}")
        print(f"  summarized_up_to: {thread_doc.get('summarized_up_to', 0)}")
        print(f"  created_at     : {thread_doc.get('created_at', '?')}")

        unsummarised = thread_doc.get("message_count", 0) - thread_doc.get("summarized_up_to", 0)
        print(f"  unsummarised   : {unsummarised} messages pending next trigger or flush")

        # ── 4. Current thread summary — Redis vs MongoDB ───────────
        print("\n[4] CURRENT THREAD SUMMARY")

        # MongoDB
        sum_doc = await db["summaries"].find_one({"thread_id": tid})
        mongo_summary = sum_doc["summary_text"] if sum_doc else None

        # Redis
        redis_summary = None
        if r:
            try:
                redis_summary = await r.get(f"summary:{tid}")
            except Exception:
                pass

        if mongo_summary:
            print(f"  MongoDB  : ✓ ({len(mongo_summary)} chars)")
            print(f"  Preview  : {mongo_summary[:120]}...")
        else:
            print(f"  MongoDB  : (none yet — needs {30 - unsummarised if unsummarised < 30 else 0} more messages or session end)")

        if r:
            if redis_summary:
                match = "✓ matches MongoDB" if redis_summary == mongo_summary else "⚠ DIFFERS from MongoDB"
                print(f"  Redis    : ✓ HIT ({len(redis_summary)} chars) — {match}")
            else:
                if mongo_summary:
                    print(f"  Redis    : MISS — will cache on next get_thread_summary() call")
                else:
                    print(f"  Redis    : MISS — no summary exists yet (expected for new thread)")

    # ── 5. All threads for this user ───────────────────────────────
    print("\n[5] ALL THREADS FOR USER '%s'" % USER_ID)

    all_threads = await db["threads"].find(
        {"user_id": USER_ID},
        sort=[("created_at", 1)],
    ).to_list(length=None)

    if not all_threads:
        print("  (no threads found)")
    else:
        for i, t in enumerate(all_threads, 1):
            tid_i   = t["thread_id"]
            title_i = t.get("title", "New Conversation")
            msgs_i  = t.get("message_count", 0)
            sumup_i = t.get("summarized_up_to", 0)
            active  = "← ACTIVE" if t.get("active") else ""

            # Check if this thread has a summary
            s = await db["summaries"].find_one({"thread_id": tid_i})
            has_sum = f"✓ summary ({len(s['summary_text'])} chars)" if s else "no summary"

            print(f"  Thread {i}: {title_i}")
            print(f"    id       : {tid_i} {active}")
            print(f"    messages : {msgs_i}  summarized_up_to: {sumup_i}")
            print(f"    summary  : {has_sum}")

    # ── 6. Past summaries cache — Redis vs MongoDB ─────────────────
    print("\n[6] PAST SUMMARIES CACHE (Redis vs MongoDB)")

    # What MongoDB would return via $lookup
    active_thread_id = thread_doc["thread_id"] if thread_doc else ""
    pipeline = [
        {"$match": {"user_id": USER_ID, "thread_id": {"$ne": active_thread_id}}},
        {"$sort": {"created_at": 1}},
        {"$lookup": {"from": "summaries", "localField": "thread_id",
                     "foreignField": "thread_id", "as": "summary_docs"}},
        {"$match": {"summary_docs.0": {"$exists": True}}},
        {"$project": {"_id": 0, "thread_id": 1, "title": 1,
                      "summary_text": {"$arrayElemAt": ["$summary_docs.summary_text", 0]}}},
    ]
    mongo_past = await db["threads"].aggregate(pipeline).to_list(length=None)

    print(f"  MongoDB $lookup : {len(mongo_past)} past thread(s) with summaries")
    for i, p in enumerate(mongo_past, 1):
        print(f"    Session {i}: '{p.get('title', '?')}' — {len(p.get('summary_text',''))} chars")

    if r:
        try:
            redis_past_raw = await r.get(f"past_summaries:{USER_ID}")
            if redis_past_raw:
                redis_past = json.loads(redis_past_raw)
                print(f"  Redis cache     : ✓ HIT — {len(redis_past)} past thread(s) cached")
                for item in redis_past:
                    print(f"    Session {item['session_num']}: '{item.get('title','?')}' — {len(item.get('summary_text',''))} chars")

                # Check if Redis and MongoDB agree
                if len(redis_past) == len(mongo_past):
                    print(f"  Cache status    : ✓ Redis and MongoDB agree ({len(redis_past)} sessions)")
                else:
                    print(f"  Cache status    : ⚠ MISMATCH — Redis has {len(redis_past)}, MongoDB has {len(mongo_past)}")
                    print(f"                    (expected if session just ended and cache was invalidated)")
            else:
                print(f"  Redis cache     : MISS — will be populated on first message of next session")
                if mongo_past:
                    print(f"                    (MongoDB has {len(mongo_past)} past summaries ready to cache)")
        except Exception as e:
            print(f"  Redis cache     : ERROR — {e}")

    # ── 7. Quick Redis read/write test ────────────────────────────
    print("\n[7] REDIS READ/WRITE TEST")

    if not r:
        print("  ⚠ Skipped — Redis not connected")
    else:
        try:
            test_key = "debug_test_key"
            await r.set(test_key, "hello_redis", ex=10)
            val = await r.get(test_key)
            await r.delete(test_key)
            verify = await r.get(test_key)

            if val == "hello_redis" and verify is None:
                print("  ✓ SET / GET / DELETE all working correctly")
            else:
                print(f"  ✗ Something wrong — got '{val}', after delete got '{verify}'")
        except Exception as e:
            print(f"  ✗ Test failed: {e}")

    # ── 8. Summary ────────────────────────────────────────────────
    print("\n[8] SUMMARY")

    total_threads  = len(all_threads) if all_threads else 0
    total_with_sum = len(mongo_past)
    active_title   = thread_doc.get("title", "?") if thread_doc else "none"

    print(f"  Total threads    : {total_threads}")
    print(f"  With summaries   : {total_with_sum} past + (current thread may have one)")
    print(f"  Active thread    : {active_title}")
    print(f"  Redis status     : {'connected ✓' if r else 'disabled (MongoDB only)'}")

    print("\n" + "═" * 60)
    print("  HOW TO VERIFY DURING A CHAT SESSION:")
    print("  1. Start uvicorn → run this script → see 'no summary yet'")
    print("  2. Chat for 30 messages → run this script → see summary appear")
    print("     in BOTH MongoDB and Redis")
    print("  3. Type 'exit' in app.py → run this script → see past_summaries")
    print("     Redis key is GONE (invalidated) and MongoDB has the summary")
    print("  4. Start uvicorn again → first message → run this script →")
    print("     see past_summaries Redis key is back (fetched from MongoDB)")
    print("═" * 60 + "\n")

    from core.database import close_db
    from core.redis_client import close_redis
    await close_redis()
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
    
    
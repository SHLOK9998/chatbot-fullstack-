# core/database.py
"""
MongoDB async connection manager.

This is a NEW file — it didn't exist before because ChromaDB
was just a local folder, no connection needed.

MongoDB Atlas is a remote server, so we need to:
  1. Open a connection at app startup (connect_db)
  2. Keep it alive while the app runs
  3. Close it cleanly when the app shuts down (close_db)
  4. Provide a simple way for any service to get the database (get_db)

We use 'motor' — the async MongoDB driver built for FastAPI/asyncio.
'pymongo' is the sync version; 'motor' is the async version on top of it.
"""

import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from core.config import settings

logger = logging.getLogger(__name__)

# This variable holds the single MongoDB client for the whole app.
# It starts as None and gets set when connect_db() is called at startup.
# Think of it like a single database "connection pool" — one client,
# many operations share it safely.
_client: AsyncIOMotorClient = None


async def connect_db() -> None:
    """
    Called once at app startup (inside lifespan in main.py).
    Creates the MongoDB client and verifies the connection works.

    AsyncIOMotorClient() does NOT immediately connect — it connects lazily.
    The ping command forces an actual connection so we catch errors early.
    """
    global _client

    if not settings.MONGO_URL:
        # Hard stop — if no URI is configured, the app cannot work.
        raise ValueError(
            "MONGODB_URI is not set in your .env file. "
            "Add: MONGO_URL=mongodb+srv://<user>:<pass>@cluster.xxxxx.mongodb.net/"
        )

    logger.info("Connecting to MongoDB Atlas...")

    # Create the client. serverSelectionTimeoutMS=5000 means:
    # if it can't reach Atlas in 5 seconds, throw an error instead of hanging.
    _client = AsyncIOMotorClient(
        settings.MONGO_URL,
        serverSelectionTimeoutMS=5000
    )

    # Ping the server to confirm the connection actually works.
    # Without this, a bad URI would only fail on the first real query.
    await _client.admin.command("ping")

    logger.info("MongoDB connected | database='%s'", settings.MONGO_DB_NAME)


async def close_db() -> None:
    """
    Called once at app shutdown (inside lifespan in main.py).
    Cleanly closes the connection pool so no resources are leaked.
    """
    global _client
    if _client:
        _client.close()
        _client = None
        logger.info("MongoDB connection closed.")


def get_db() -> AsyncIOMotorDatabase:
    """
    Returns the database object that all services use to access collections.

    Usage in any service file:
        from core.database import get_db
        db = get_db()
        collection = db["employee_kb"]
        await collection.find_one(...)

    This does NOT create a new connection each time — it just returns
    a reference to the already-open database from the shared _client.
    """
    if _client is None:
        raise RuntimeError(
            "MongoDB client is not initialised. "
            "Make sure connect_db() ran at startup in main.py lifespan."
        )
    # _client[db_name] gives you the database object.
    # From the database object, you access collections like: db["employee_kb"]
    return _client[settings.MONGO_DB_NAME]
# services/auth_service.py
"""
Per-user Google OAuth2 service builder.

Tokens are stored in MongoDB 'google_tokens' collection, one document per user_id.
No shared token.json — every user has their own Gmail + Calendar credentials.

MongoDB schema:
{
  "user_id":       "usr_a1b2c3d4",
  "token":         "ya29.xxx",
  "refresh_token": "1//xxx",
  "token_uri":     "https://oauth2.googleapis.com/token",
  "client_id":     "xxx.apps.googleusercontent.com",
  "client_secret": "xxx",
  "scopes":        ["https://...gmail.send", "https://...calendar"],
  "expiry":        ISODate(...)
}
"""

import logging
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.config import settings
from core.database import get_db

logger = logging.getLogger(__name__)

COLLECTION = "google_tokens"


# ── Token storage helpers ─────────────────────────────────────────────────────

async def save_user_tokens(user_id: str, creds: Credentials) -> None:
    """Persist OAuth credentials for a user to MongoDB."""
    db  = get_db()
    doc = {
        "user_id":       user_id,
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes or settings.GOOGLE_SCOPES),
        "updated_at":    datetime.now(timezone.utc),
    }
    await db[COLLECTION].update_one(
        {"user_id": user_id},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    logger.info("[GoogleAuth] Tokens saved | user=%s", user_id)


async def get_user_tokens(user_id: str) -> dict | None:
    """Load raw token document for a user from MongoDB."""
    db = get_db()
    return await db[COLLECTION].find_one({"user_id": user_id})


async def has_google_connected(user_id: str) -> bool:
    """Return True if this user has connected their Google account."""
    doc = await get_user_tokens(user_id)
    return doc is not None and bool(doc.get("refresh_token"))


# ── Credential loader ─────────────────────────────────────────────────────────

async def _load_credentials(user_id: str) -> Credentials:
    """
    Load and refresh OAuth2 credentials for a specific user.

    Raises:
        RuntimeError: If user has not connected Google account or token is invalid.
    """
    doc = await get_user_tokens(user_id)
    if not doc:
        raise RuntimeError(
            f"User '{user_id}' has not connected their Google account. "
            "Please connect via GET /auth/google/connect first."
        )

    creds = Credentials(
        token=doc.get("token"),
        refresh_token=doc.get("refresh_token"),
        token_uri=doc.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=doc.get("client_id"),
        client_secret=doc.get("client_secret"),
        scopes=doc.get("scopes", settings.GOOGLE_SCOPES),
    )

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        logger.info("[GoogleAuth] Token expired — refreshing | user=%s", user_id)
        creds.refresh(Request())
        await save_user_tokens(user_id, creds)

    if not creds.valid:
        raise RuntimeError(
            f"Google credentials for user '{user_id}' are invalid. "
            "Please reconnect via GET /auth/google/connect."
        )

    return creds


# ── Service builders ──────────────────────────────────────────────────────────

async def get_gmail_service(user_id: str):
    """Return an authenticated Gmail API client for a specific user."""
    creds = await _load_credentials(user_id)
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        logger.info("[GoogleAuth] Gmail service ready | user=%s", user_id)
        return service
    except Exception as e:
        logger.error("[GoogleAuth] Failed to build Gmail service | user=%s | %s", user_id, e)
        raise


async def get_calendar_service(user_id: str):
    """Return an authenticated Google Calendar API client for a specific user."""
    creds = await _load_credentials(user_id)
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("[GoogleAuth] Calendar service ready | user=%s", user_id)
        return service
    except Exception as e:
        logger.error("[GoogleAuth] Failed to build Calendar service | user=%s | %s", user_id, e)
        raise

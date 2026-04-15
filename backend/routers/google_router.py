# routers/google_router.py
"""
Google OAuth2 endpoints. Mounted at /auth/google in main.py.

GET  /auth/google/connect    — redirect user to Google consent screen
GET  /auth/google/callback   — Google redirects here after user approves
GET  /auth/google/status     — check if current user has connected Google
DELETE /auth/google/disconnect — revoke and remove Google tokens
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from core.auth import get_current_user, decode_token
from core.config import settings
from services.auth_service import save_user_tokens, get_user_tokens, has_google_connected

logger = logging.getLogger(__name__)
router = APIRouter()

# Frontend URL to redirect to after OAuth completes
_FRONTEND_URL = "http://localhost:3000"


def _get_flow(state: str = ""):
    """Build a Google OAuth flow instance."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(
        str(settings.GOOGLE_CREDENTIALS_FILE),
        scopes=settings.GOOGLE_SCOPES,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
    )
    if state:
        flow.state = state
    return flow


# ── GET /auth/google/connect ──────────────────────────────────────────────────

@router.get("/connect")
async def google_connect(token: str = Query(..., description="JWT access token")):
    """
    Generate Google OAuth URL and redirect to Google consent screen.
    Accepts token as query param because this is a browser redirect, not an API call.
    """
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    flow = _get_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=user_id,
    )
    logger.info("[GoogleOAuth] Redirecting to Google consent | user=%s", user_id)
    return RedirectResponse(url=auth_url)


# ── GET /auth/google/callback ─────────────────────────────────────────────────

@router.get("/callback")
async def google_callback(code: str, state: str):
    """
    Google redirects here after user approves.
    - state = user_id (set in /connect)
    - code  = authorization code to exchange for tokens
    Saves tokens to MongoDB and redirects to frontend.
    """
    user_id = state
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing state parameter.")

    try:
        flow = _get_flow(state=user_id)
        flow.fetch_token(code=code)
        creds = flow.credentials
        await save_user_tokens(user_id, creds)
        logger.info("[GoogleOAuth] Tokens saved after consent | user=%s", user_id)
    except Exception as e:
        logger.error("[GoogleOAuth] Callback failed | user=%s | %s", user_id, e)
        return RedirectResponse(url=f"{_FRONTEND_URL}/chat?google=error")

    return RedirectResponse(url=f"{_FRONTEND_URL}/chat?google=connected")


# ── GET /auth/google/status ───────────────────────────────────────────────────

@router.get("/status")
async def google_status(user_id: str = Depends(get_current_user)):
    """
    Return whether the current user has connected their Google account.
    Frontend calls this on load to show/hide the Connect button.
    """
    connected = await has_google_connected(user_id)
    return {"connected": connected, "user_id": user_id}


# ── DELETE /auth/google/disconnect ───────────────────────────────────────────

@router.delete("/disconnect")
async def google_disconnect(user_id: str = Depends(get_current_user)):
    """
    Remove the user's Google tokens from MongoDB.
    They will need to reconnect to use email/calendar features.
    """
    from core.database import get_db
    db = get_db()
    await db["google_tokens"].delete_one({"user_id": user_id})
    logger.info("[GoogleOAuth] Tokens removed | user=%s", user_id)
    return {"ok": True, "message": "Google account disconnected."}

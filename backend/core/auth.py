# core/auth.py
"""
JWT utility + FastAPI dependency for user authentication.

HOW IT WORKS:
  - On login/register: create_token(user_id) → signed JWT string
  - JWT is stored in an httpOnly cookie (browser sends it automatically)
  - On every protected request: get_current_user() reads the cookie,
    decodes the JWT, returns user_id — or raises 401 if invalid/missing

SWAGGER SUPPORT:
  Swagger UI cannot send httpOnly cookies, so get_current_user() also
  accepts the token via Authorization: Bearer <token> header.
  Priority: cookie first, then Bearer header.
  In production (real browser) the cookie is always used automatically.

SLIDING SESSION:
  - Token lifetime = JWT_EXPIRE_DAYS (default 30 days)
  - Every active request (chat message) refreshes the cookie with a new token
  - User is only logged out if:
      a) They click Logout (cookie explicitly cleared), OR
      b) They are inactive for JWT_EXPIRE_DAYS straight
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPBearer
from jose import JWTError, jwt

from core.config import settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

# Declared only so FastAPI registers BearerAuth in the OpenAPI schema
# (makes the top-right Authorize button appear in Swagger)
_bearer = HTTPBearer(auto_error=False)


def create_token(user_id: str) -> str:
    """
    Create a signed JWT containing user_id.
    Expiry = now + JWT_EXPIRE_DAYS days.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=settings.JWT_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "exp": expire,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)
    logger.debug("[Auth] Token created | user=%s | expires=%s", user_id, expire.date())
    return token


def decode_token(token: str) -> Optional[str]:
    """
    Decode and verify a JWT. Returns user_id (sub claim) or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            return None
        return user_id
    except JWTError as e:
        logger.warning("[Auth] Token decode failed: %s", e)
        return None


def _extract_token(request: Request) -> Optional[str]:
    """Read token from cookie first, then Authorization header."""
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    return token


async def get_optional_user(request: Request) -> Optional[str]:
    """
    Like get_current_user but never raises 401.
    Used for endpoints that work with or without auth (e.g. logout).
    """
    token = _extract_token(request)
    if not token:
        return None
    return decode_token(token)


async def get_current_user(request: Request) -> str:
    """
    FastAPI dependency — resolves user_id from cookie or Authorization header.
    No Cookie() parameter so Swagger never renders an extra input field.
    """
    token = _extract_token(request)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please log in.",
        )

    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please log in again.",
        )

    logger.debug("[Auth] Authenticated | user=%s", user_id)
    return user_id

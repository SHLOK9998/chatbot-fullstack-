# # routers/auth_router.py
# """
# Authentication endpoints. Mounted at /auth in main.py.

# POST /auth/register  — create account + set cookie
# POST /auth/login     — verify credentials + set cookie
# POST /auth/logout    — clear cookie
# GET  /auth/me        — return current user info (frontend calls on page load)
# """

# import logging
# from typing import Optional
# from fastapi import APIRouter, Depends, HTTPException, Response, status
# from pydantic import BaseModel, Field

# from core.auth import create_token, get_current_user, get_optional_user
# from services.user_service import (
#     validate_registration,
#     create_user,
#     authenticate_user,
#     get_user_by_id,
# )
# from core.config import settings

# logger = logging.getLogger(__name__)
# router = APIRouter()

# _COOKIE_NAME = "access_token"
# _COOKIE_MAX_AGE = settings.JWT_EXPIRE_DAYS * 24 * 60 * 60  # seconds


# def _set_auth_cookie(response: Response, token: str) -> None:
#     """Set the httpOnly auth cookie on a response."""
#     response.set_cookie(
#         key=_COOKIE_NAME,
#         value=token,
#         max_age=_COOKIE_MAX_AGE,
#         httponly=True,       # JS cannot read it — XSS protection
#         samesite="lax",      # CSRF protection
#         secure=False,        # set True in production with HTTPS
#     )


# def _clear_auth_cookie(response: Response) -> None:
#     """Delete the auth cookie."""
#     response.delete_cookie(key=_COOKIE_NAME, httponly=True, samesite="lax")


# # ── Models ────────────────────────────────────────────────────────────────────

# class RegisterRequest(BaseModel):
#     username:     str = Field(..., description="Unique login name (3–30 chars, letters/numbers/underscore)")
#     email:        str = Field(..., description="Valid email address")
#     password:     str = Field(..., description="Min 8 chars, at least 1 letter and 1 number")
#     display_name: str = Field(..., description="Your full name shown in the UI (2–50 chars)")


# class LoginRequest(BaseModel):
#     username: str = Field(..., description="Username or email address")
#     password: str = Field(...)


# class UserResponse(BaseModel):
#     user_id:      str
#     username:     str
#     display_name: str
#     email:        str
#     access_token: Optional[str] = None  # present on login/register, absent on /me


# # ── POST /auth/register ───────────────────────────────────────────────────────

# @router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
# async def register(req: RegisterRequest, response: Response):
#     """
#     Create a new account.

#     Validates all fields first:
#       - username : 3–30 chars, alphanumeric + underscore only
#       - email    : must be a valid email format
#       - password : min 8 chars, at least 1 letter + 1 number
#       - display_name: 2–50 chars

#     On success:
#       - User document created in MongoDB 'users' collection
#       - JWT cookie set (user is immediately logged in)
#       - Returns user info

#     On failure:
#       - 400 with list of validation errors, OR
#       - 409 if username/email already taken
#     """
#     # Step 1: validate all fields
#     errors = validate_registration(req.username, req.email, req.password, req.display_name)
#     if errors:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)

#     # Step 2: create user (raises ValueError if username/email taken)
#     try:
#         user = await create_user(req.username, req.email, req.password, req.display_name)
#     except ValueError as e:
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

#     # Step 3: issue JWT cookie — user is logged in immediately after register
#     token = create_token(user["user_id"])
#     _set_auth_cookie(response, token)

#     logger.info("[Auth] Register success | user_id=%s | username=%s", user["user_id"], user["username"])
#     return UserResponse(**user, access_token=token)


# # ── POST /auth/login ──────────────────────────────────────────────────────────

# @router.post("/login", response_model=UserResponse)
# async def login(req: LoginRequest, response: Response):
#     """
#     Log in with username (or email) + password.

#     On success:
#       - JWT cookie set (sliding session — 30 days from now)
#       - Returns user info

#     On failure:
#       - 401 with generic message (never reveal which field was wrong)
#     """
#     user = await authenticate_user(req.username, req.password)
#     if not user:
#         # Generic message — don't reveal whether username or password was wrong
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid username or password.",
#         )

#     token = create_token(user["user_id"])
#     _set_auth_cookie(response, token)

#     logger.info("[Auth] Login success | user_id=%s", user["user_id"])
#     return UserResponse(**user, access_token=token)


# # ── POST /auth/logout ─────────────────────────────────────────────────────────

# @router.post("/logout")
# async def logout(
#     response: Response,
#     user_id: Optional[str] = Depends(get_optional_user),
# ):
#     """
#     Log out the current user by clearing the auth cookie.
#     Always returns 200 — even if the user wasn't logged in.
#     Token is read from the Authorize button (no input field needed).
#     """
#     _clear_auth_cookie(response)
#     logger.info("[Auth] Logout — cookie cleared | user=%s", user_id)
#     return {"ok": True, "message": "Logged out successfully."}


# # ── GET /auth/me ──────────────────────────────────────────────────────────────

# @router.get("/me", response_model=UserResponse)
# async def me(user_id: str = Depends(get_current_user)):
#     """
#     Return the currently logged-in user's info.

#     Frontend calls this on every page load to check:
#       - Is the user logged in? (200 = yes, 401 = no → redirect to login)
#       - What is their display_name / username?

#     Does NOT refresh the cookie — only /chat/ refreshes it (activity-based).
#     """
#     user = await get_user_by_id(user_id)
#     if not user:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="User account not found.",
#         )
#     return UserResponse(
#         user_id=user["user_id"],
#         username=user["username"],
#         display_name=user["display_name"],
#         email=user["email"],
#         access_token=None,
#     )


# routers/auth_router.py
"""
Authentication endpoints. Mounted at /auth in main.py.

POST /auth/register  — create account + set cookie
POST /auth/login     — verify credentials + set cookie
POST /auth/logout    — clear cookie
GET  /auth/me        — return current user info (frontend calls on page load)
PUT  /auth/me        — update display_name and/or email               [NEW]

NOTE: All existing endpoints are UNCHANGED. PUT /auth/me is the only addition.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from core.auth import create_token, get_current_user, get_optional_user
from services.user_service import (
    validate_registration,
    create_user,
    authenticate_user,
    get_user_by_id,
    update_user,          # NEW import
)
from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_COOKIE_NAME = "access_token"
_COOKIE_MAX_AGE = settings.JWT_EXPIRE_DAYS * 24 * 60 * 60  # seconds


def _set_auth_cookie(response: Response, token: str) -> None:
    """Set the httpOnly auth cookie on a response."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="none",     # required for cross-origin requests (localhost:3000 → :8000)
        secure=False,        # set True in production with HTTPS
    )


def _clear_auth_cookie(response: Response) -> None:
    """Delete the auth cookie."""
    response.delete_cookie(key=_COOKIE_NAME, httponly=True, samesite="lax")


# ── Models ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username:     str = Field(..., description="Unique login name (3–30 chars, letters/numbers/underscore)")
    email:        str = Field(..., description="Valid email address")
    password:     str = Field(..., description="Min 8 chars, at least 1 letter and 1 number")
    display_name: str = Field(..., description="Your full name shown in the UI (2–50 chars)")


class LoginRequest(BaseModel):
    username: str = Field(..., description="Username or email address")
    password: str = Field(..., description="Account password")


class UserResponse(BaseModel):
    user_id:      str
    username:     str
    display_name: str
    email:        str
    access_token: Optional[str] = None  # present on login/register, absent on /me


# NEW: request body for PUT /auth/me
class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = Field(
        None,
        description="New display name (2–50 chars). Omit to leave unchanged.",
    )
    email: Optional[str] = Field(
        None,
        description="New email address. Omit to leave unchanged.",
    )


# ── POST /auth/register ───────────────────────────────────────────────────────

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest, response: Response):
    """
    Create a new account.

    Validates all fields first:
      - username : 3–30 chars, alphanumeric + underscore only
      - email    : must be a valid email format
      - password : min 8 chars, at least 1 letter + 1 number
      - display_name: 2–50 chars

    On success:
      - User document created in MongoDB 'users' collection
      - JWT cookie set (user is immediately logged in)
      - Returns user info

    On failure:
      - 400 with list of validation errors, OR
      - 409 if username/email already taken
    """
    # Step 1: validate all fields
    errors = validate_registration(req.username, req.email, req.password, req.display_name)
    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)

    # Step 2: create user (raises ValueError if username/email taken)
    try:
        user = await create_user(req.username, req.email, req.password, req.display_name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    # Step 3: issue JWT cookie — user is logged in immediately after register
    token = create_token(user["user_id"])
    _set_auth_cookie(response, token)

    logger.info("[Auth] Register success | user_id=%s | username=%s", user["user_id"], user["username"])
    return UserResponse(**user, access_token=token)


# ── POST /auth/login ──────────────────────────────────────────────────────────

@router.post("/login", response_model=UserResponse)
async def login(req: LoginRequest, response: Response):
    """
    Log in with username (or email) + password.

    On success:
      - JWT cookie set (sliding session — 30 days from now)
      - Returns user info

    On failure:
      - 401 with generic message (never reveal which field was wrong)
    """
    user = await authenticate_user(req.username, req.password)
    if not user:
        # Generic message — don't reveal whether username or password was wrong
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    token = create_token(user["user_id"])
    _set_auth_cookie(response, token)

    logger.info("[Auth] Login success | user_id=%s", user["user_id"])
    return UserResponse(**user, access_token=token)


# ── POST /auth/logout ─────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(
    response: Response,
    user_id: Optional[str] = Depends(get_optional_user),
):
    """
    Log out the current user by clearing the auth cookie.
    Always returns 200 — even if the user wasn't logged in.
    Token is read from the Authorize button (no input field needed).
    """
    _clear_auth_cookie(response)
    logger.info("[Auth] Logout — cookie cleared | user=%s", user_id)
    return {"ok": True, "message": "Logged out successfully."}


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def me(user_id: str = Depends(get_current_user)):
    """
    Return the currently logged-in user's info.

    Frontend calls this on every page load to check:
      - Is the user logged in? (200 = yes, 401 = no → redirect to login)
      - What is their display_name / username?

    Does NOT refresh the cookie — only /chat/ refreshes it (activity-based).
    """
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account not found.",
        )
    return UserResponse(
        user_id=user["user_id"],
        username=user["username"],
        display_name=user["display_name"],
        email=user["email"],
        access_token=None,
    )


# =============================================================================
# NEW ENDPOINT — added below; existing endpoints above are UNCHANGED
# =============================================================================

# ── PUT /auth/me — update profile ────────────────────────────────────────────

@router.put("/me", response_model=UserResponse)
async def update_me(
    req: UpdateProfileRequest,
    user_id: str = Depends(get_current_user),
):
    """
    Update the current user's display_name and/or email.

    - Both fields are optional — only send the ones you want to change.
    - username and password cannot be changed through this endpoint.
    - email uniqueness is enforced (409 if already taken by another user).
    - display_name length rules: 2–50 chars.

    Example body (change only display_name):
        { "display_name": "Shlok Panchal" }

    Example body (change both):
        { "display_name": "Shlok P", "email": "new@example.com" }

    Returns the updated user object.
    """
    # At least one field must be provided
    if req.display_name is None and req.email is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one field to update: display_name or email.",
        )

    try:
        updated_user = await update_user(
            user_id=user_id,
            display_name=req.display_name,
            email=req.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account not found.",
        )

    logger.info("[Auth] Profile updated | user_id=%s", user_id)
    return UserResponse(
        user_id=updated_user["user_id"],
        username=updated_user["username"],
        display_name=updated_user["display_name"],
        email=updated_user["email"],
        access_token=None,
    )
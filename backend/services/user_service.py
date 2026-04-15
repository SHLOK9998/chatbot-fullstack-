# services/user_service.py
"""
User account management — MongoDB 'users' collection.

MongoDB document schema:
{
  "user_id":      "usr_a1b2c3d4",        ← stable UUID, used everywhere as identity
  "username":     "shlok",               ← unique login name (lowercase)
  "display_name": "Shlok Panchal",       ← shown in UI
  "email":        "shlok@example.com",   ← unique, validated on register
  "password_hash": "$2b$12$...",         ← bcrypt hash, plain text never stored
  "created_at":   ISODate(...)
}

Validation rules (enforced on register):
  - username : 3–30 chars, alphanumeric + underscore only
  - email    : must contain @ and a dot after @
  - password : min 8 chars, at least 1 letter and 1 number
  - display_name: 2–50 chars, not blank

CHANGES FROM ORIGINAL:
  - update_user() added at the bottom — supports PUT /auth/me.
    All existing functions are UNCHANGED.
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from passlib.context import CryptContext

from core.database import get_db

logger = logging.getLogger(__name__)

COLLECTION = "users"

# bcrypt context — handles hashing and verification
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Validation helpers ────────────────────────────────────────────────────────

def _validate_username(username: str) -> Optional[str]:
    """Returns error message or None if valid."""
    if not username or not username.strip():
        return "Username is required."
    u = username.strip()
    if len(u) < 3:
        return "Username must be at least 3 characters."
    if len(u) > 30:
        return "Username must be 30 characters or fewer."
    if not re.fullmatch(r"[a-zA-Z0-9_]+", u):
        return "Username can only contain letters, numbers, and underscores."
    return None


def _validate_email(email: str) -> Optional[str]:
    """Returns error message or None if valid."""
    if not email or not email.strip():
        return "Email is required."
    e = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        return "Enter a valid email address."
    return None


def _validate_password(password: str) -> Optional[str]:
    """Returns error message or None if valid."""
    if not password:
        return "Password is required."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[a-zA-Z]", password):
        return "Password must contain at least one letter."
    if not re.search(r"[0-9]", password):
        return "Password must contain at least one number."
    return None


def _validate_display_name(display_name: str) -> Optional[str]:
    """Returns error message or None if valid."""
    if not display_name or not display_name.strip():
        return "Display name is required."
    d = display_name.strip()
    if len(d) < 2:
        return "Display name must be at least 2 characters."
    if len(d) > 50:
        return "Display name must be 50 characters or fewer."
    return None


def validate_registration(
    username: str,
    email: str,
    password: str,
    display_name: str,
) -> list[str]:
    """
    Run all field validations. Returns list of error strings.
    Empty list = all valid.
    """
    errors = []
    for check in [
        _validate_username(username),
        _validate_email(email),
        _validate_password(password),
        _validate_display_name(display_name),
    ]:
        if check:
            errors.append(check)
    return errors


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ── DB operations ─────────────────────────────────────────────────────────────

def _make_user_id() -> str:
    return "usr_" + uuid.uuid4().hex[:8]


async def get_user_by_username(username: str) -> Optional[dict]:
    db = get_db()
    return await db[COLLECTION].find_one({"username": username.strip().lower()})


async def get_user_by_email(email: str) -> Optional[dict]:
    db = get_db()
    return await db[COLLECTION].find_one({"email": email.strip().lower()})


async def get_user_by_id(user_id: str) -> Optional[dict]:
    db = get_db()
    return await db[COLLECTION].find_one({"user_id": user_id})


async def create_user(
    username: str,
    email: str,
    password: str,
    display_name: str,
) -> dict:
    """
    Create a new user. Assumes validation already passed.
    Returns the created user document (without password_hash).

    Raises ValueError if username or email already exists.
    """
    username_clean     = username.strip().lower()
    email_clean        = email.strip().lower()
    display_name_clean = display_name.strip()

    db = get_db()

    # Uniqueness checks
    if await db[COLLECTION].find_one({"username": username_clean}):
        raise ValueError(f"Username '{username_clean}' is already taken.")
    if await db[COLLECTION].find_one({"email": email_clean}):
        raise ValueError(f"An account with email '{email_clean}' already exists.")

    user_id = _make_user_id()
    doc = {
        "user_id":       user_id,
        "username":      username_clean,
        "display_name":  display_name_clean,
        "email":         email_clean,
        "password_hash": hash_password(password),
        "created_at":    datetime.now(timezone.utc),
    }

    await db[COLLECTION].insert_one(doc)
    logger.info("[UserService] Created user | user_id=%s | username=%s", user_id, username_clean)

    # Return safe public fields (no password_hash)
    return {
        "user_id":      user_id,
        "username":     username_clean,
        "display_name": display_name_clean,
        "email":        email_clean,
    }


async def authenticate_user(username: str, password: str) -> Optional[dict]:
    """
    Verify username + password. Returns public user dict or None if invalid.
    Accepts login by username OR email.
    """
    username_clean = username.strip().lower()

    # Try username first, then email
    user = await get_user_by_username(username_clean)
    if not user:
        user = await get_user_by_email(username_clean)

    if not user:
        logger.info("[UserService] Login failed — user not found: %s", username_clean)
        return None

    if not verify_password(password, user["password_hash"]):
        logger.info("[UserService] Login failed — wrong password: %s", username_clean)
        return None

    logger.info("[UserService] Login success | user_id=%s", user["user_id"])
    return {
        "user_id":      user["user_id"],
        "username":     user["username"],
        "display_name": user["display_name"],
        "email":        user["email"],
    }


# =============================================================================
# NEW FUNCTION — added below; all existing functions above are UNCHANGED
# =============================================================================

async def update_user(
    user_id: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[dict]:
    """
    Update a user's display_name and/or email.

    - Only the fields that are not None are updated.
    - email uniqueness is enforced across OTHER users (a user can re-submit
      their own email without triggering a conflict).
    - display_name is validated (2–50 chars).

    Returns the updated public user dict, or None if the user was not found.
    Raises ValueError if the new email is taken by a different user, or if
    display_name fails validation.
    """
    db   = get_db()
    user = await db[COLLECTION].find_one({"user_id": user_id})
    if not user:
        return None

    update_fields: dict = {}

    # ── display_name ──────────────────────────────────────────────────────────
    if display_name is not None:
        err = _validate_display_name(display_name)
        if err:
            raise ValueError(err)
        update_fields["display_name"] = display_name.strip()

    # ── email ─────────────────────────────────────────────────────────────────
    if email is not None:
        err = _validate_email(email)
        if err:
            raise ValueError(err)
        email_clean = email.strip().lower()
        # Only check uniqueness if the email is actually changing
        if email_clean != user["email"]:
            conflict = await db[COLLECTION].find_one({"email": email_clean})
            if conflict:
                raise ValueError(f"An account with email '{email_clean}' already exists.")
        update_fields["email"] = email_clean

    # Nothing to update (both were None — router already guards this, but be safe)
    if not update_fields:
        return {
            "user_id":      user["user_id"],
            "username":     user["username"],
            "display_name": user["display_name"],
            "email":        user["email"],
        }

    update_fields["updated_at"] = datetime.now(timezone.utc)

    await db[COLLECTION].update_one(
        {"user_id": user_id},
        {"$set": update_fields},
    )

    logger.info(
        "[UserService] Updated user | user_id=%s | fields=%s",
        user_id, list(update_fields.keys()),
    )

    # Return fresh values by merging
    return {
        "user_id":      user["user_id"],
        "username":     user["username"],
        "display_name": update_fields.get("display_name", user["display_name"]),
        "email":        update_fields.get("email", user["email"]),
    }


# # services/user_service.py
# """
# User account management — MongoDB 'users' collection.

# MongoDB document schema:
# {
#   "user_id":      "usr_a1b2c3d4",        ← stable UUID, used everywhere as identity
#   "username":     "shlok",               ← unique login name (lowercase)
#   "display_name": "Shlok Panchal",       ← shown in UI
#   "email":        "shlok@example.com",   ← unique, validated on register
#   "password_hash": "$2b$12$...",         ← bcrypt hash, plain text never stored
#   "created_at":   ISODate(...)
# }

# Validation rules (enforced on register):
#   - username : 3–30 chars, alphanumeric + underscore only
#   - email    : must contain @ and a dot after @
#   - password : min 8 chars, at least 1 letter and 1 number
#   - display_name: 2–50 chars, not blank
# """

# import logging
# import re
# import uuid
# from datetime import datetime, timezone
# from typing import Optional

# from passlib.context import CryptContext

# from core.database import get_db

# logger = logging.getLogger(__name__)

# COLLECTION = "users"

# # bcrypt context — handles hashing and verification
# _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# # ── Validation helpers ────────────────────────────────────────────────────────

# def _validate_username(username: str) -> Optional[str]:
#     """Returns error message or None if valid."""
#     if not username or not username.strip():
#         return "Username is required."
#     u = username.strip()
#     if len(u) < 3:
#         return "Username must be at least 3 characters."
#     if len(u) > 30:
#         return "Username must be 30 characters or fewer."
#     if not re.fullmatch(r"[a-zA-Z0-9_]+", u):
#         return "Username can only contain letters, numbers, and underscores."
#     return None


# def _validate_email(email: str) -> Optional[str]:
#     """Returns error message or None if valid."""
#     if not email or not email.strip():
#         return "Email is required."
#     e = email.strip().lower()
#     if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
#         return "Enter a valid email address."
#     return None


# def _validate_password(password: str) -> Optional[str]:
#     """Returns error message or None if valid."""
#     if not password:
#         return "Password is required."
#     if len(password) < 8:
#         return "Password must be at least 8 characters."
#     if not re.search(r"[a-zA-Z]", password):
#         return "Password must contain at least one letter."
#     if not re.search(r"[0-9]", password):
#         return "Password must contain at least one number."
#     return None


# def _validate_display_name(display_name: str) -> Optional[str]:
#     """Returns error message or None if valid."""
#     if not display_name or not display_name.strip():
#         return "Display name is required."
#     d = display_name.strip()
#     if len(d) < 2:
#         return "Display name must be at least 2 characters."
#     if len(d) > 50:
#         return "Display name must be 50 characters or fewer."
#     return None


# def validate_registration(
#     username: str,
#     email: str,
#     password: str,
#     display_name: str,
# ) -> list[str]:
#     """
#     Run all field validations. Returns list of error strings.
#     Empty list = all valid.
#     """
#     errors = []
#     for check in [
#         _validate_username(username),
#         _validate_email(email),
#         _validate_password(password),
#         _validate_display_name(display_name),
#     ]:
#         if check:
#             errors.append(check)
#     return errors


# # ── Password helpers ──────────────────────────────────────────────────────────

# def hash_password(plain: str) -> str:
#     return _pwd_ctx.hash(plain)


# def verify_password(plain: str, hashed: str) -> bool:
#     return _pwd_ctx.verify(plain, hashed)


# # ── DB operations ─────────────────────────────────────────────────────────────

# def _make_user_id() -> str:
#     return "usr_" + uuid.uuid4().hex[:8]


# async def get_user_by_username(username: str) -> Optional[dict]:
#     db = get_db()
#     return await db[COLLECTION].find_one({"username": username.strip().lower()})


# async def get_user_by_email(email: str) -> Optional[dict]:
#     db = get_db()
#     return await db[COLLECTION].find_one({"email": email.strip().lower()})


# async def get_user_by_id(user_id: str) -> Optional[dict]:
#     db = get_db()
#     return await db[COLLECTION].find_one({"user_id": user_id})


# async def create_user(
#     username: str,
#     email: str,
#     password: str,
#     display_name: str,
# ) -> dict:
#     """
#     Create a new user. Assumes validation already passed.
#     Returns the created user document (without password_hash).

#     Raises ValueError if username or email already exists.
#     """
#     username_clean    = username.strip().lower()
#     email_clean       = email.strip().lower()
#     display_name_clean = display_name.strip()

#     db = get_db()

#     # Uniqueness checks
#     if await db[COLLECTION].find_one({"username": username_clean}):
#         raise ValueError(f"Username '{username_clean}' is already taken.")
#     if await db[COLLECTION].find_one({"email": email_clean}):
#         raise ValueError(f"An account with email '{email_clean}' already exists.")

#     user_id = _make_user_id()
#     doc = {
#         "user_id":       user_id,
#         "username":      username_clean,
#         "display_name":  display_name_clean,
#         "email":         email_clean,
#         "password_hash": hash_password(password),
#         "created_at":    datetime.now(timezone.utc),
#     }

#     await db[COLLECTION].insert_one(doc)
#     logger.info("[UserService] Created user | user_id=%s | username=%s", user_id, username_clean)

#     # Return safe public fields (no password_hash)
#     return {
#         "user_id":      user_id,
#         "username":     username_clean,
#         "display_name": display_name_clean,
#         "email":        email_clean,
#     }


# async def authenticate_user(username: str, password: str) -> Optional[dict]:
#     """
#     Verify username + password. Returns public user dict or None if invalid.
#     Accepts login by username OR email.
#     """
#     username_clean = username.strip().lower()

#     # Try username first, then email
#     user = await get_user_by_username(username_clean)
#     if not user:
#         user = await get_user_by_email(username_clean)

#     if not user:
#         logger.info("[UserService] Login failed — user not found: %s", username_clean)
#         return None

#     if not verify_password(password, user["password_hash"]):
#         logger.info("[UserService] Login failed — wrong password: %s", username_clean)
#         return None

#     logger.info("[UserService] Login success | user_id=%s", user["user_id"])
#     return {
#         "user_id":      user["user_id"],
#         "username":     user["username"],
#         "display_name": user["display_name"],
#         "email":        user["email"],
#     }

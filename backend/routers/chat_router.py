# # routers/chat_router.py
# """
# Chat API router. Mounted at /chat in main.py, so full URLs are:

#   POST   /chat/           — send a message, get a reply
#   POST   /chat/session/end — flush remaining messages into rolling summary
#   GET    /chat/health     — server status + current thread info
#   DELETE /chat/reset      — legacy no-op (kept for compatibility)
# """

# import logging
# from fastapi import APIRouter, Depends, HTTPException, Response
# from pydantic import BaseModel, Field

# from core.auth import create_token, get_current_user
# from core.config import settings
# from services.chat_service import (
#     process_query,
#     end_session,
#     get_thread_list,
#     DEFAULT_USER,
# )

# logger = logging.getLogger(__name__)
# router = APIRouter()


# # ── Models ────────────────────────────────────────────────────────────────────

# class ChatRequest(BaseModel):
#     message: str = Field(..., description="User message or query.")


# class ChatResponse(BaseModel):
#     response: str


# class SessionEndResponse(BaseModel):
#     flushed: bool
#     message: str


# # ── POST /chat/ — main chat ───────────────────────────────────────────────────

# @router.post("/", response_model=ChatResponse)
# async def chat(
#     req: ChatRequest,
#     response: Response,
#     user_id: str = Depends(get_current_user),
# ) -> ChatResponse:
#     if not req.message or not req.message.strip():
#         raise HTTPException(status_code=400, detail="Message cannot be empty.")

#     logger.info("[Router] Chat | user=%s | len=%d", user_id, len(req.message))
#     reply = await process_query(req.message.strip(), user_id=user_id)
#     logger.info("[Router] Reply | user=%s | len=%d", user_id, len(reply))

#     # Sliding session — refresh cookie on every active request
#     token = create_token(user_id)
#     response.set_cookie(
#         key="access_token",
#         value=token,
#         max_age=settings.JWT_EXPIRE_DAYS * 24 * 60 * 60,
#         httponly=True,
#         samesite="lax",
#         secure=False,
#     )
#     return ChatResponse(response=reply)


# # ── POST /chat/session/end — flush summary ────────────────────────────────────

# @router.post("/session/end", response_model=SessionEndResponse)
# async def session_end(user_id: str = Depends(get_current_user)) -> SessionEndResponse:
#     """
#     Flush all unsummarised messages into the rolling summary.
#     user_id comes from the JWT cookie — no need to pass it in the body.
#     """
#     logger.info("[Router] session/end | user=%s", user_id)
#     flushed = await end_session(user_id)
#     msg = (
#         "Summary updated with remaining messages."
#         if flushed
#         else "Summary already up-to-date — nothing to flush."
#     )
#     logger.info("[Router] session/end done | user=%s | flushed=%s", user_id, flushed)
#     return SessionEndResponse(flushed=flushed, message=msg)


# # ── GET /chat/health — health check ──────────────────────────────────────────

# @router.get("/health")
# async def health(user_id: str = Depends(get_current_user)):
#     """Returns server status and current thread info for the logged-in user."""
#     from services.thread_service import get_active_thread, get_thread
#     thread_id = await get_active_thread(user_id)
#     thread    = await get_thread(thread_id) if thread_id else None
#     return {
#         "status":           "ok",
#         "user_id":          user_id,
#         "thread_id":        thread_id,
#         "message_count":    thread.get("message_count", 0)    if thread else 0,
#         "summarized_up_to": thread.get("summarized_up_to", 0) if thread else 0,
#     }


# # ── GET /chat/threads — list past threads ─────────────────────────────────────

# @router.get("/threads")
# async def list_threads(user_id: str = Depends(get_current_user)):
#     """List all past threads for the logged-in user, newest first."""
#     threads = await get_thread_list(user_id)
#     return {"threads": threads, "count": len(threads)}


# # ── DELETE /chat/reset — legacy compat ───────────────────────────────────────

# @router.delete("/reset")
# async def reset():
#     """Legacy endpoint — kept so old app.py versions don't crash."""
#     return {"status": "acknowledged"}

# routers/chat_router.py
"""
Chat API router. Mounted at /chat in main.py, so full URLs are:

  POST   /chat/                          — send a message, get a reply
  POST   /chat/session/end               — flush remaining messages into rolling summary
  POST   /chat/session/new               — explicitly start a fresh thread  [NEW]
  GET    /chat/health                    — server status + current thread info
  GET    /chat/threads                   — list all past threads (sidebar)
  GET    /chat/threads/{thread_id}/messages  — load messages of a thread   [NEW]
  POST   /chat/threads/{thread_id}/switch    — reopen & continue a past thread [NEW]
  DELETE /chat/threads/{thread_id}           — delete a thread + its messages  [NEW]
  DELETE /chat/reset                     — legacy no-op (kept for compatibility)

NOTE: All existing endpoints are UNCHANGED. The four new endpoints are
      appended at the bottom and do not touch any existing logic.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from core.auth import create_token, get_current_user
from core.config import settings
from services.chat_service import (
    process_query,
    end_session,
    get_thread_list,
    DEFAULT_USER,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., description="User message or query.")


class ChatResponse(BaseModel):
    response: str


class SessionEndResponse(BaseModel):
    flushed: bool
    message: str


# ── POST /chat/ — main chat ───────────────────────────────────────────────────

@router.post("/", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    response: Response,
    user_id: str = Depends(get_current_user),
) -> ChatResponse:
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    logger.info("[Router] Chat | user=%s | len=%d", user_id, len(req.message))
    reply = await process_query(req.message.strip(), user_id=user_id)
    logger.info("[Router] Reply | user=%s | len=%d", user_id, len(reply))

    # Sliding session — refresh cookie on every active request
    token = create_token(user_id)
    response.set_cookie(
        key="access_token",
        value=token,
        max_age=settings.JWT_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return ChatResponse(response=reply)


# ── POST /chat/session/end — flush summary ────────────────────────────────────

@router.post("/session/end", response_model=SessionEndResponse)
async def session_end(user_id: str = Depends(get_current_user)) -> SessionEndResponse:
    """
    Flush all unsummarised messages into the rolling summary.
    user_id comes from the JWT cookie — no need to pass it in the body.
    """
    logger.info("[Router] session/end | user=%s", user_id)
    flushed = await end_session(user_id)
    msg = (
        "Summary updated with remaining messages."
        if flushed
        else "Summary already up-to-date — nothing to flush."
    )
    logger.info("[Router] session/end done | user=%s | flushed=%s", user_id, flushed)
    return SessionEndResponse(flushed=flushed, message=msg)


# ── GET /chat/health — health check ──────────────────────────────────────────

@router.get("/health")
async def health(user_id: str = Depends(get_current_user)):
    """Returns server status and current thread info for the logged-in user."""
    from services.thread_service import get_active_thread, get_thread
    thread_id = await get_active_thread(user_id)
    thread    = await get_thread(thread_id) if thread_id else None
    return {
        "status":           "ok",
        "user_id":          user_id,
        "thread_id":        thread_id,
        "message_count":    thread.get("message_count", 0)    if thread else 0,
        "summarized_up_to": thread.get("summarized_up_to", 0) if thread else 0,
    }


# ── GET /chat/threads — list past threads ─────────────────────────────────────

@router.get("/threads")
async def list_threads(user_id: str = Depends(get_current_user)):
    """List all past threads for the logged-in user, newest first."""
    threads = await get_thread_list(user_id)
    return {"threads": threads, "count": len(threads)}


# ── DELETE /chat/reset — legacy compat ───────────────────────────────────────

@router.delete("/reset")
async def reset():
    """Legacy endpoint — kept so old app.py versions don't crash."""
    return {"status": "acknowledged"}


# =============================================================================
# NEW ENDPOINTS — added below; existing endpoints above are UNCHANGED
# =============================================================================

# ── POST /chat/session/new — start a fresh thread ────────────────────────────

@router.post("/session/new")
async def session_new(user_id: str = Depends(get_current_user)):
    """
    Explicitly create a new thread and make it the active one.
    Also updates the in-memory _active_threads cache in chat_service
    so the very next POST /chat/ message goes to the new thread.
    """
    from services.chat_service import initialize_session
    logger.info("[Router] session/new | user=%s", user_id)
    thread_id = await initialize_session(user_id)
    logger.info("[Router] session/new created thread=%s | user=%s", thread_id, user_id)
    return {
        "ok":        True,
        "thread_id": thread_id,
        "message":   "New thread created and set as active.",
    }


# ── GET /chat/threads/{thread_id}/messages — load thread messages ─────────────

@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    limit: int = 50,
    user_id: str = Depends(get_current_user),
):
    """
    Return the messages for a specific thread belonging to the logged-in user.

    - Verifies the thread belongs to this user before returning anything.
    - limit: how many recent messages to return (default 50, max 200).
      Pass limit=0 to get ALL messages (use with caution on long threads).

    Response shape:
        {
          "thread_id": "thread_abc123",
          "messages": [
            {"role": "user",      "content": "...", "timestamp": "..."},
            {"role": "assistant", "content": "...", "timestamp": "..."},
            ...
          ],
          "count": 12
        }
    """
    from services.thread_service import get_thread
    from services.message_service import get_recent_messages, get_all_messages

    # 1. Verify the thread exists and belongs to this user
    thread = await get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if thread.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    # 2. Fetch messages
    limit = max(0, min(limit, 200))   # clamp 0–200
    if limit == 0:
        raw_messages = await get_all_messages(thread_id)
    else:
        raw_messages = await get_recent_messages(thread_id, limit=limit)

    # Serialize — strip MongoDB _id, keep only frontend-useful fields
    # Force UTC suffix so browser correctly converts to local time (IST = UTC+5:30)
    def _ts(dt):
        if not dt: return None
        iso = dt.isoformat()
        if iso.endswith('+00:00') or iso.endswith('Z'): return iso
        return iso + 'Z'

    messages = [
        {
            "role":      m.get("role", ""),
            "content":   m.get("content", ""),
            "timestamp": _ts(m.get("timestamp")),
        }
        for m in raw_messages
    ]

    logger.info(
        "[Router] get_thread_messages | thread=%s | user=%s | count=%d",
        thread_id, user_id, len(messages),
    )
    return {
        "thread_id": thread_id,
        "messages":  messages,
        "count":     len(messages),
    }


# ── POST /chat/threads/{thread_id}/switch — reopen a past thread ──────────────

@router.post("/threads/{thread_id}/switch")
async def switch_thread(
    thread_id: str,
    user_id: str = Depends(get_current_user),
):
    """
    Switch the active thread to a past thread so the user can continue it.

    Steps performed:
      1. Verifies the thread belongs to this user.
      2. Marks all other threads as active=False.
      3. Marks this thread as active=True.

    After this call, POST /chat/ will continue the conversation in this thread.

    Returns the thread metadata (title, message_count) so the frontend
    can update its header without a separate fetch.
    """
    from services.thread_service import set_active_thread, get_thread

    # 1. Verify ownership first (set_active_thread also checks, but we want
    #    a clear 403 vs 404 distinction for the frontend)
    thread = await get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if thread.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    # 2. Switch — use chat_service.switch_to_thread so _active_threads cache is updated
    from services.chat_service import switch_to_thread
    switched = await switch_to_thread(user_id, thread_id)
    if not switched:
        raise HTTPException(status_code=500, detail="Failed to switch thread.")

    logger.info("[Router] switch_thread | thread=%s | user=%s", thread_id, user_id)
    return {
        "ok":            True,
        "thread_id":     thread_id,
        "title":         thread.get("title", "Untitled"),
        "message_count": thread.get("message_count", 0),
        "message":       "Thread switched. POST /chat/ will now continue this thread.",
    }


# ── DELETE /chat/threads/{thread_id} — delete a thread ───────────────────────

@router.delete("/threads/{thread_id}")
async def delete_thread_endpoint(
    thread_id: str,
    user_id: str = Depends(get_current_user),
):
    """
    Permanently delete a thread and all its messages and summaries.

    Safety checks:
      - Verifies the thread belongs to this user.
      - If the deleted thread was the active one, a new thread is created
        automatically so the user is never left without an active thread.

    Returns:
        { "ok": True, "new_thread_id": "..." }   — if active thread was deleted
        { "ok": True, "new_thread_id": null }     — if a non-active thread was deleted
    """
    from services.thread_service import get_thread, delete_thread, create_new_thread

    # 1. Ownership check
    thread = await get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if thread.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    was_active = thread.get("active", False)

    # 2. Delete thread + cascade (messages + summaries)
    await delete_thread(thread_id)
    logger.info("[Router] delete_thread | thread=%s | user=%s", thread_id, user_id)

    # 3. If the deleted thread was active, auto-create a replacement
    new_thread_id = None
    if was_active:
        new_thread_id = await create_new_thread(user_id)
        logger.info(
            "[Router] delete_thread auto-created replacement | new_thread=%s | user=%s",
            new_thread_id, user_id,
        )

    return {
        "ok":           True,
        "deleted":      thread_id,
        "new_thread_id": new_thread_id,
        "message": (
            "Thread deleted. A new thread has been created as the active one."
            if was_active
            else "Thread deleted."
        ),
    }
# routers/chat_router.py
"""
Chat API router. Mounted at /chat in main.py, so full URLs are:

  POST   /chat/                              — send a message, get a reply
  POST   /chat/session/end                   — flush remaining messages into rolling summary
  POST   /chat/session/new                   — explicitly start a fresh thread
  GET    /chat/health                        — server status + current thread info
  GET    /chat/threads                       — list all past threads (sidebar)
  GET    /chat/threads/{thread_id}/messages  — load messages of a thread
  POST   /chat/threads/{thread_id}/switch    — reopen & continue a past thread
  DELETE /chat/threads/{thread_id}           — delete a thread + its messages
  DELETE /chat/reset                         — legacy no-op (kept for compatibility)
  POST   /chat/upload-attachment             — upload a file attachment for the active email [NEW]
  DELETE /chat/upload-attachment             — remove a specific attachment by filename    [NEW]
"""

import base64
import logging
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File, Query
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
    logger.info("[Router] session/end | user=%s", user_id)
    flushed = await end_session(user_id)
    msg = (
        "Summary updated with remaining messages."
        if flushed
        else "Summary already up-to-date — nothing to flush."
    )
    logger.info("[Router] session/end done | user=%s | flushed=%s", user_id, flushed)
    return SessionEndResponse(flushed=flushed, message=msg)


# ── POST /chat/session/new — start a fresh thread ────────────────────────────

@router.post("/session/new")
async def session_new(user_id: str = Depends(get_current_user)):
    from services.chat_service import initialize_session
    logger.info("[Router] session/new | user=%s", user_id)
    thread_id = await initialize_session(user_id)
    logger.info("[Router] session/new created thread=%s | user=%s", thread_id, user_id)
    return {
        "ok":        True,
        "thread_id": thread_id,
        "message":   "New thread created and set as active.",
    }


# ── GET /chat/threads — list past threads ─────────────────────────────────────

@router.get("/threads")
async def list_threads(user_id: str = Depends(get_current_user)):
    threads = await get_thread_list(user_id)
    return {"threads": threads, "count": len(threads)}


# ── GET /chat/threads/{thread_id}/messages — load thread messages ─────────────

@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    limit: int = 50,
    user_id: str = Depends(get_current_user),
):
    from services.thread_service import get_thread
    from services.message_service import get_recent_messages, get_all_messages

    thread = await get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if thread.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    limit = max(0, min(limit, 200))
    if limit == 0:
        raw_messages = await get_all_messages(thread_id)
    else:
        raw_messages = await get_recent_messages(thread_id, limit=limit)

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
    return {"thread_id": thread_id, "messages": messages, "count": len(messages)}


# ── POST /chat/threads/{thread_id}/switch — reopen a past thread ──────────────

@router.post("/threads/{thread_id}/switch")
async def switch_thread(
    thread_id: str,
    user_id: str = Depends(get_current_user),
):
    from services.thread_service import set_active_thread, get_thread
    from services.chat_service import switch_to_thread

    thread = await get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if thread.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

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
    from services.thread_service import get_thread, delete_thread, create_new_thread

    thread = await get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")
    if thread.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    was_active = thread.get("active", False)
    await delete_thread(thread_id)
    logger.info("[Router] delete_thread | thread=%s | user=%s", thread_id, user_id)

    new_thread_id = None
    if was_active:
        new_thread_id = await create_new_thread(user_id)
        logger.info(
            "[Router] delete_thread auto-created replacement | new_thread=%s | user=%s",
            new_thread_id, user_id,
        )

    return {
        "ok":            True,
        "deleted":       thread_id,
        "new_thread_id": new_thread_id,
        "message": (
            "Thread deleted. A new thread has been created as the active one."
            if was_active
            else "Thread deleted."
        ),
    }


# ── DELETE /chat/reset — legacy compat ───────────────────────────────────────

@router.delete("/reset")
async def reset():
    return {"status": "acknowledged"}


# =============================================================================
# ATTACHMENT ENDPOINTS — new; no existing logic changed above
# =============================================================================

_ATTACHMENT_STATE_KEY = "email_state:{}"
_ATTACHMENT_MAX_SIZE  = 10 * 1024 * 1024   # 10 MB per file hard limit


# ── POST /chat/upload-attachment ──────────────────────────────────────────────

@router.post("/upload-attachment")
async def upload_attachment(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """
    Accept a multipart/form-data file upload and append it to the user's active
    email state in Redis under the key ``attachment_files``.

    Each entry in ``attachment_files`` is:
        { "filename": str, "data": str }   — data is base64-encoded file bytes

    Returns:
        { "ok": True, "filename": str, "size": int, "total_attachments": int }
    """
    from core.redis_client import redis_get_json, redis_set_json

    state_key = _ATTACHMENT_STATE_KEY.format(user_id)

    # Read raw bytes (guard against oversized uploads)
    file_bytes = await file.read()
    if len(file_bytes) > _ATTACHMENT_MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum allowed size is {_ATTACHMENT_MAX_SIZE // (1024*1024)} MB.",
        )

    filename   = file.filename or "attachment"
    b64_data   = base64.b64encode(file_bytes).decode("utf-8")
    size_bytes = len(file_bytes)

    # Load current email state
    state = await redis_get_json(state_key)
    if not isinstance(state, dict):
        state = {}

    # Append to attachment_files list (deduplicate by filename — last upload wins)
    existing: list = state.get("attachment_files") or []
    existing = [a for a in existing if a.get("filename") != filename]  # remove old same-name
    existing.append({"filename": filename, "data": b64_data, "size": size_bytes})
    state["attachment_files"] = existing

    await redis_set_json(state_key, state, ex=7200)

    logger.info(
        "[Router] upload-attachment | user=%s | file=%s | size=%d | total=%d",
        user_id, filename, size_bytes, len(existing),
    )
    return {
        "ok":               True,
        "filename":         filename,
        "size":             size_bytes,
        "total_attachments": len(existing),
    }


# ── DELETE /chat/upload-attachment?filename=X ────────────────────────────────

@router.delete("/upload-attachment")
async def remove_attachment(
    filename: str = Query(..., description="Filename to remove from attachment list"),
    user_id: str = Depends(get_current_user),
):
    """
    Remove a specific attachment (by filename) from the user's active email
    state in Redis.

    Returns:
        { "ok": True, "filename": str, "total_attachments": int }
    """
    from core.redis_client import redis_get_json, redis_set_json

    state_key = _ATTACHMENT_STATE_KEY.format(user_id)

    state = await redis_get_json(state_key)
    if not isinstance(state, dict):
        raise HTTPException(status_code=404, detail="No active email state found.")

    existing: list = state.get("attachment_files") or []
    before  = len(existing)
    existing = [a for a in existing if a.get("filename") != filename]

    if len(existing) == before:
        raise HTTPException(status_code=404, detail=f"Attachment '{filename}' not found.")

    state["attachment_files"] = existing
    await redis_set_json(state_key, state, ex=7200)

    logger.info(
        "[Router] remove-attachment | user=%s | file=%s | remaining=%d",
        user_id, filename, len(existing),
    )
    return {
        "ok":               True,
        "filename":         filename,
        "total_attachments": len(existing),
    }
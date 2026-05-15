# main.py  (updated — 2 lines added vs original)
"""
Application entry point.

CHANGES vs original:
  Line 1 (import):  from mcp.server import mount_mcp
  Line 2 (call):    mount_mcp(app)

  Everything else is IDENTICAL to the original main.py.
  MongoDB, Redis, ingestion, session lifecycle — all unchanged.

MCP SERVER ENDPOINTS (new):
  GET  /mcp/sse      ← MCP Inspector connects here (SSE stream)
  POST /mcp/message  ← JSON-RPC messages from client
  GET  /mcp/health   ← MCP server health check

EXISTING FASTAPI ENDPOINTS (unchanged):
  POST /chat/           ← main chatbot
  POST /chat/session/end
  GET  /chat/health
  GET  /chat/threads
  GET  /debug/state/{user_id}
  GET  /
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from core.logger import setup_logger
from core.database import connect_db, close_db
from core.redis_client import connect_redis, close_redis
from routers.chat_router import router as chat_router
from routers.auth_router import router as auth_router
from routers.google_router import router as google_router
from services.ingestion_service import initialize_knowledge_base

# ── NEW: MCP import ───────────────────────────────────────────────────────────
from server import mount_mcp

logger = logging.getLogger(__name__)


async def _background_ingest():
    """Run knowledge base ingestion in background so startup isn't blocked."""
    try:
        await asyncio.to_thread(initialize_knowledge_base)
        logger.info("Background ingestion complete.")
    except Exception as e:
        logger.error("Background ingestion failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ────────────────────────────────────────────────────────────────

    setup_logger()

    # 1. MongoDB (required)
    await connect_db()

    # 2. Redis (optional — skips silently if REDIS_URL not set)
    await connect_redis()

    # 3. Load Excel knowledge base in background (non-blocking)
    asyncio.create_task(_background_ingest())

    # ── APP RUNS HERE ──────────────────────────────────────────────────────────
    yield

    # ── SHUTDOWN ───────────────────────────────────────────────────────────────

    try:
        await close_redis()
        await close_db()
    except asyncio.CancelledError:
        pass


app = FastAPI(
    lifespan=lifespan,
    title="Personal AI Chatbot + MCP Server",
    description=(
        "RAG-powered chatbot backed by MongoDB Atlas Vector Search. "
        "Supports employee KB queries, email, and calendar management. "
        "MCP server available at /mcp/sse"
    ),
)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version="1.0.0",
        description=app.description,
        routes=app.routes,
    )
    # Add Bearer security scheme so Swagger shows the Authorize button
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Paste the access_token from /auth/login or /auth/register response",
        }
    }
    # Public routes — no lock icon, no token needed
    _public_paths = {
        "/auth/register", "/auth/login",
        "/auth/google/connect", "/auth/google/callback",
        "/mcp", "/mcp/health",
        "/debug/state/{user_id}",
        "/",
    }
    for path, methods in schema["paths"].items():
        if path in _public_paths:
            continue
        for operation in methods.values():
            operation.setdefault("security", [{"BearerAuth": []}])
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

# ── CORS — allow React dev server ────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,   # required for httpOnly cookie to be sent
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router,   prefix="/auth",         tags=["Auth"])
app.include_router(google_router, prefix="/auth/google",   tags=["Google OAuth"])
app.include_router(chat_router,   prefix="/chat",          tags=["Chat"])

# ── NEW: Mount MCP server ─────────────────────────────────────────────────────
mount_mcp(app, prefix="/mcp")


# ── Existing debug + root endpoints (unchanged) ───────────────────────────────

@app.get("/debug/state/{user_id}")
async def debug_state(user_id: str):
    from core.redis_client import get_redis
    r = get_redis()

    if not r:
        return {"source": "in-memory fallback", "redis": "not connected"}

    email_raw = await r.get(f"email_state:{user_id}")
    cal_raw   = await r.get(f"cal_state:{user_id}")

    return {
        "source":      "redis",
        "redis":       "connected",
        "email_state": email_raw,
        "cal_state":   cal_raw,
    }


@app.get("/", tags=["Health"])
async def root():
    return {
        "status":     "running",
        "docs":       "/docs",
        "chat":       "/chat/",
        "mcp_sse":    "/mcp/sse",
        "mcp_health": "/mcp/health",
    }




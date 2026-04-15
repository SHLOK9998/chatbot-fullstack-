# mcp/transport/http_handler.py
"""
Streamable HTTP Transport for MCP (spec: 2025-03-26)

REPLACES: mcp/transport/sse_handler.py

WHY STREAMABLE HTTP OVER SSE:
  Old SSE transport needed TWO endpoints:
    GET  /mcp/sse      → open persistent stream first
    POST /mcp/message  → then send messages to a different URL
  Client had to manage session IDs, keep connection alive, handle reconnects.

  Streamable HTTP uses ONE endpoint for everything:
    POST /mcp          → send any JSON-RPC request
                       → server responds with either:
                            a) plain JSON  (for fast single responses)
                            b) SSE stream  (for long-running tool calls)
  Client decides per-request whether it wants streaming or not via Accept header.
  No persistent connection. No session management. No reconnect logic needed.

HOW IT WORKS (per request):
  1. Client sends POST /mcp with JSON-RPC 2.0 body
  2. Server reads Accept header:
       Accept: application/json          → return plain JSON response
       Accept: text/event-stream         → stream response as SSE on same connection
  3. For tool calls (which can be slow — LLM + DB):
       → always stream as SSE so client gets response as it arrives
  4. For fast calls (initialize, tools/list):
       → plain JSON is fine, no streaming needed

JSON-RPC 2.0 METHODS HANDLED:
  initialize          → handshake, return server info + capabilities
  notifications/initialized → client ack after handshake (no response needed)
  tools/list          → return all 6 tool definitions
  tools/call          → run the tool, stream result back
  ping                → health check (return pong)
  notifications/*     → silently acknowledged

TOOL CALL DISPATCH:
  "email"    → run_email_tool()
  "calendar" → run_calendar_tool()
  "chat_rag" → run_chat_rag_tool()
  "db_query" → run_db_query_tool()
  "crud"     → run_crud_tool()
  "master"   → run_master_tool()

MCP INSPECTOR USAGE (after migration):
  Transport type: Streamable HTTP
  URL:            http://127.0.0.1:8000/mcp
  That's it. No SSE URL, no session setup, no second endpoint.

ENDPOINTS:
  POST /mcp          ← all JSON-RPC traffic goes here
  GET  /mcp/health   ← health check (unchanged from before)
"""

import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from schemas.tool_schemas import ALL_TOOL_SCHEMAS

logger = logging.getLogger(__name__)

router = APIRouter()

# ── MCP Protocol Version ──────────────────────────────────────────────────────
# Must match what MCP Inspector and clients expect
MCP_PROTOCOL_VERSION = "2025-03-26"

# ── Tool definitions (identical to old sse_handler, just moved here) ──────────

_TOOL_DEFINITIONS = [
    {
        "name": "email",
        "description": (
            "Send, compose, or manage emails. Handles multi-turn flows: "
            "extracts recipient/purpose → asks for missing info → shows preview "
            "→ sends. State persists in Redis between calls."
        ),
        "inputSchema": ALL_TOOL_SCHEMAS["email"],
    },
    {
        "name": "calendar",
        "description": (
            "Create or manage Google Calendar events. Handles multi-turn flows: "
            "extracts event details → resolves attendees → shows preview "
            "→ creates event. State persists in Redis between calls."
        ),
        "inputSchema": ALL_TOOL_SCHEMAS["calendar"],
    },
    {
        "name": "chat_rag",
        "description": (
            "Answer questions using the employee knowledge base (Atlas Vector Search) , general llm knowledge "
            "and conversation history. Best for: 'Who is the intern?', "
            "'Tell me about Anand', general Q&A."
        ),
        "inputSchema": ALL_TOOL_SCHEMAS["chat_rag"],
    },
    {
        "name": "db_query",
        "description": (
            "List or count employees using structured MongoDB queries. "
            "Best for: 'List all interns', 'How many employees in Surat?', "
            "'Show all Full Stack developers'. Returns ALL matching records."
        ),
        "inputSchema": ALL_TOOL_SCHEMAS["db_query"],
    },
    {
        "name": "crud",
        "description": (
            "Add, update, or delete employee records in the database. "
            "Examples: 'Add employee John, Backend Senior, Mumbai', "
            "'Update Anand's email to new@example.com', "
            "'Delete the intern from Botad'."
        ),
        "inputSchema": ALL_TOOL_SCHEMAS["crud"],
    },
    {
        "name": "master",
        "description": (
            "Universal entry point — handles ANY natural language message. "
            "Detects intent automatically and routes to email, calendar, db_query, "
            "crud, or chat/RAG. Also correctly continues active email/calendar flows. "
            "Use this when you don't know which specific tool to call."
        ),
        "inputSchema": ALL_TOOL_SCHEMAS["master"],
    },
]


# ── Tool dispatcher (identical logic to sse_handler) ─────────────────────────

async def _dispatch_tool(name: str, arguments: dict) -> str:
    """Route a tools/call request to the correct async function."""
    from tools.email_tool    import run_email_tool
    from tools.calendar_tool import run_calendar_tool
    from tools.chat_rag_tool import run_chat_rag_tool
    from tools.db_query_tool import run_db_query_tool
    from tools.crud_tool     import run_crud_tool
    from tools.master_tool   import run_master_tool

    query = arguments.get("query", "")

    dispatch = {
        "email":    run_email_tool,
        "calendar": run_calendar_tool,
        "chat_rag": run_chat_rag_tool,
        "db_query": run_db_query_tool,
        "crud":     run_crud_tool,
        "master":   run_master_tool,
    }

    fn = dispatch.get(name)
    if fn is None:
        return f"❌ Unknown tool: '{name}'. Available: {list(dispatch.keys())}"

    return await fn(query)


# ── JSON-RPC 2.0 handler ──────────────────────────────────────────────────────

async def _handle_jsonrpc(body: dict) -> dict:
    """
    Process one JSON-RPC 2.0 request.
    Returns a response dict — caller decides whether to send as JSON or SSE.

    DIFFERENCE FROM SSE VERSION:
      Old version pushed result into a queue for the SSE stream.
      New version just returns the dict — the POST handler sends it directly
      on the same HTTP connection, either as JSON or as an SSE stream.
    """
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    logger.info("[MCP] method=%s id=%s", method, req_id)

    # ── initialize ────────────────────────────────────────────────────────────
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {
                    "name":    "personal-assistant-mcp",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "tools": {"listChanged": False},
                },
            },
        }

    # ── tools/list ────────────────────────────────────────────────────────────
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _TOOL_DEFINITIONS},
        }

    # ── tools/call ────────────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result_text = await _dispatch_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            }
        except Exception as e:
            logger.exception("[MCP] tools/call error: %s", e)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"❌ Error: {e}"}],
                    "isError": True,
                },
            }

    # ── ping ──────────────────────────────────────────────────────────────────
    if method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {},
        }

    # ── notifications (no response expected — return empty sentinel) ──────────
    if method.startswith("notifications/"):
        logger.debug("[MCP] notification %s — ack only", method)
        return {}

    # ── unknown method ────────────────────────────────────────────────────────
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code":    -32601,
            "message": f"Method not found: {method}",
        },
    }


# ── SSE stream wrapper (used when client requests streaming) ──────────────────

async def _sse_response_stream(response_dict: dict) -> AsyncIterator[str]:
    """
    Wrap a single JSON-RPC response dict as an SSE stream.

    Streamable HTTP sends tool results as SSE on the same POST connection.
    Format per MCP spec:
      event: message
      data: {json}\n\n

    Followed by a final empty data to signal end of stream.
    """
    if response_dict:
        payload = json.dumps(response_dict)
        yield f"event: message\ndata: {payload}\n\n"

    # Signal end of stream — MCP spec requires this
    yield "event: message\ndata: [DONE]\n\n"


# ── Main POST /mcp endpoint ───────────────────────────────────────────────────

@router.post("")
async def mcp_endpoint(request: Request):
    """
    POST /mcp — single endpoint for ALL MCP traffic.

    This is the core of Streamable HTTP transport.
    Every JSON-RPC request comes here — initialize, tools/list, tools/call, all of it.

    RESPONSE FORMAT DECISION:
      We check the Accept header the client sent:
        application/json   → return plain JSONResponse
        text/event-stream  → return StreamingResponse with SSE format

      For tool calls specifically, we always prefer streaming because:
        - LLM calls take 1-5 seconds
        - DB queries take 100-500ms
        - Streaming lets the client show a loading state properly

      For initialize and tools/list:
        - These are instant → plain JSON is fine
        - But we still respect Accept header if client prefers streaming

    NOTIFICATION HANDLING:
      Notifications (notifications/initialized etc.) have no id and
      expect no response. We return HTTP 202 Accepted with empty body.
    """
    # Parse request body
    try:
        body = await request.json()
    except Exception as e:
        logger.error("[MCP] Failed to parse request body: %s", e)
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id":      None,
                "error":   {"code": -32700, "message": "Parse error"},
            },
        )

    method = body.get("method", "")
    logger.info("[MCP:HTTP] Received method=%s", method)

    # ── Notifications: no response body needed ────────────────────────────────
    # Per MCP spec, notifications must not be responded to.
    # Return 202 Accepted with empty body.
    if method.startswith("notifications/"):
        logger.debug("[MCP:HTTP] Notification %s — 202 Accepted", method)
        return JSONResponse(status_code=202, content={})

    # ── Process the JSON-RPC request ──────────────────────────────────────────
    response_dict = await _handle_jsonrpc(body)

    # ── Empty response (internal sentinel for notifications) ──────────────────
    if not response_dict:
        return JSONResponse(status_code=202, content={})

    # ── Determine response format from Accept header ──────────────────────────
    accept = request.headers.get("accept", "application/json")
    wants_stream = "text/event-stream" in accept

    # Tool calls always stream — LLM is slow, client deserves progress signal
    is_tool_call = method == "tools/call"

    if wants_stream or is_tool_call:
        # Stream the response as SSE on this same POST connection
        logger.debug("[MCP:HTTP] Streaming response for method=%s", method)
        return StreamingResponse(
            _sse_response_stream(response_dict),
            media_type="text/event-stream",
            headers={
                "Cache-Control":               "no-cache",
                "X-Accel-Buffering":           "no",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # Plain JSON for fast calls (initialize, tools/list, ping)
    logger.debug("[MCP:HTTP] JSON response for method=%s", method)
    return JSONResponse(
        content=response_dict,
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── Health check (unchanged from sse_handler) ─────────────────────────────────

@router.get("/health")
async def mcp_health() -> dict:
    """GET /mcp/health — confirm MCP server is running."""
    return {
        "status":   "ok",
        "server":   "personal-assistant-mcp",
        "version":  "1.0.0",
        "transport": "streamable-http",
        "protocol":  MCP_PROTOCOL_VERSION,
        "endpoint":  "POST /mcp",
        "tools":    [t["name"] for t in _TOOL_DEFINITIONS],
    }
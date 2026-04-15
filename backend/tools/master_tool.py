# mcp/tools/master_tool.py
"""
MCP Tool: master

The universal entry point — mirrors chat_service.process_query() exactly.

ROUTING (same order as process_query):
  1. Check Redis for active email flow  → handle_email_flow()
  2. Check Redis for active calendar flow → handle_calendar_flow()
  3. Detect intent from fresh message:
       email    → handle_email_flow()
       calendar → handle_calendar_flow()
       db_query → handle_db_query()
       crud     → handle_crud()
       default  → _handle_rag()
  4. Save turn to MongoDB.
  5. Return reply.

WHY USE MASTER INSTEAD OF SPECIFIC TOOLS?
  - Active flow continuity: if a user starts an email flow in one call,
    the next call must know to continue that flow. Only master (and the
    specific email/calendar tools) correctly check Redis state first.
  - Same UX as the chatbot — one tool, all capabilities.

WHY KEEP THE SPECIFIC TOOLS TOO?
  - MCP Inspector testing: you can test individual pipelines in isolation.
  - Fine-grained clients: some MCP clients may want to call specific tools
    directly without routing overhead.
  - Explicit is clearer for debugging.
"""

import logging
from utils.intent_detector import detect_intent
from services.chat_service import (
    DEFAULT_USER,
    _get_thread_id,
    _save_turn_to_mongodb,
    _handle_rag,
)
from services.email_handler    import handle_email_flow,    is_email_active
from services.calendar_handler import handle_calendar_flow, is_calendar_active
from services.db_query_service import handle_db_query
from services.crud_service     import handle_crud

logger = logging.getLogger(__name__)


async def run_master_tool(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Execute the master tool — routes to the correct handler automatically.

    Args:
        query   : Any natural language message.
        user_id : User identifier (defaults to DEFAULT_USER).

    Returns:
        The assistant reply from whichever handler processed the message.
    """
    if not query or not query.strip():
        return "I didn't catch that — could you rephrase?"

    logger.info("[MCP:master] user=%s | query=%s", user_id, query[:80])

    try:
        thread_id = await _get_thread_id(user_id)

        # ── Active flow check first (same as process_query) ───────────────────
        if await is_email_active(user_id):
            logger.info("[MCP:master] Routing → active EMAIL flow")
            reply = await handle_email_flow(query, user_id, thread_id)
            await _save_turn_to_mongodb(thread_id, query, reply)
            return reply

        if await is_calendar_active(user_id):
            logger.info("[MCP:master] Routing → active CALENDAR flow")
            reply = await handle_calendar_flow(query, user_id, thread_id)
            await _save_turn_to_mongodb(thread_id, query, reply)
            return reply

        # ── Fresh message — detect intent ─────────────────────────────────────
        intent = detect_intent(query)
        logger.info("[MCP:master] Intent = %s", intent)

        if intent == "email":
            reply = await handle_email_flow(query, user_id, thread_id)

        elif intent == "calendar":
            reply = await handle_calendar_flow(query, user_id, thread_id)

        elif intent == "db_query":
            reply = await handle_db_query(query, user_id)

        elif intent == "crud":
            reply = await handle_crud(query, user_id)

        else:
            reply = await _handle_rag(query, user_id, thread_id)

        await _save_turn_to_mongodb(thread_id, query, reply)
        logger.info("[MCP:master] reply len=%d", len(reply))
        return reply

    except Exception as e:
        logger.exception("[MCP:master] Unexpected error: %s", e)
        return f" Master tool error: {e}"
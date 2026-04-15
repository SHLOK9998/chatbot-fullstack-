# mcp/tools/email_tool.py
"""
MCP Tool: email

Wraps services/email_handler.py → handle_email_flow().

FLOW (identical to FastAPI chatbot):
  1. Load Redis state for user_id (hardcoded DEFAULT_USER for now).
  2. Pass the natural language query to handle_email_flow().
  3. The handler manages the full multi-turn state machine internally:
       extract → ask_required → ask_optional → preview → modify/send/cancel
  4. Save the turn to MongoDB (so MCP Inspector test messages are persisted).
  5. Return the reply string to the MCP client.

STATE PERSISTENCE:
  Between tool calls, all state lives in Redis under key "email_state:{user_id}".
  TTL = 2 hours.  The MCP client can call this tool repeatedly —
  each call continues exactly where the previous one left off, just like the chatbot.

MONGODB SAVE:
  Every (query, reply) pair is written to the messages collection via
  chat_service._save_turn_to_mongodb().  This means the MCP Inspector
  test messages appear in the same DB as normal chat messages.
"""

import logging
from services.chat_service import DEFAULT_USER, _get_thread_id, _save_turn_to_mongodb
from services.email_handler import handle_email_flow

logger = logging.getLogger(__name__)


async def run_email_tool(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Execute the email tool for one MCP call.

    Args:
        query   : The user's natural language message.
        user_id : User identifier (defaults to hardcoded DEFAULT_USER).

    Returns:
        The assistant reply string — the next prompt in the email flow,
        a preview card, a send confirmation, or an error message.
    """
    logger.info("[MCP:email] user=%s | query=%s", user_id, query[:80])

    try:
        # Resolve (or create) the active thread so messages are saved correctly
        thread_id = await _get_thread_id(user_id)

        # Delegate entirely to the existing handler — no logic duplication
        reply = await handle_email_flow(query, user_id, thread_id)

        # Persist turn to MongoDB (same as FastAPI path)
        await _save_turn_to_mongodb(thread_id, query, reply)

        logger.info("[MCP:email] reply len=%d", len(reply))
        return reply

    except Exception as e:
        logger.exception("[MCP:email] Unexpected error: %s", e)
        return f"Email tool error: {e}"
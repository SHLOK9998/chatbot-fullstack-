# mcp/tools/calendar_tool.py
"""
MCP Tool: calendar

Wraps services/calendar_handler.py → handle_calendar_flow().

FLOW (identical to FastAPI chatbot):
  1. Resolve active thread for user.
  2. Pass the natural language query to handle_calendar_flow().
  3. The handler manages the full multi-turn state machine internally:
       extract → ask_missing → ask_attendees → preview → modify/create/cancel
  4. Save the turn to MongoDB.
  5. Return the reply string.

STATE PERSISTENCE:
  All state lives in Redis under "cal_state:{user_id}". TTL = 2 hours.
  Multi-turn calendar flows work across consecutive MCP Inspector tool calls.
"""

import logging
from services.chat_service import DEFAULT_USER, _get_thread_id, _save_turn_to_mongodb
from services.calendar_handler import handle_calendar_flow

logger = logging.getLogger(__name__)


async def run_calendar_tool(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Execute the calendar tool for one MCP call.

    Args:
        query   : The user's natural language message.
        user_id : User identifier (defaults to DEFAULT_USER).

    Returns:
        The assistant reply — a question asking for missing fields,
        an attendee resolution prompt, a preview card, or a creation confirmation.
    """
    logger.info("[MCP:calendar] user=%s | query=%s", user_id, query[:80])

    try:
        thread_id = await _get_thread_id(user_id)
        reply = await handle_calendar_flow(query, user_id, thread_id)
        await _save_turn_to_mongodb(thread_id, query, reply)

        logger.info("[MCP:calendar] reply len=%d", len(reply))
        return reply

    except Exception as e:
        logger.exception("[MCP:calendar] Unexpected error: %s", e)
        return f" Calendar tool error: {e}"
# mcp/tools/crud_tool.py
"""
MCP Tool: crud

Wraps services/crud_service.py → handle_crud().

Handles: add / update / delete employee records.
LLM extracts operation + data from the natural language query,
then executes the MongoDB write.

Single-turn — no state machine, no Redis state.
Each call either succeeds or asks for clarification if intent is unclear.
"""

import logging
from services.chat_service import DEFAULT_USER, _get_thread_id, _save_turn_to_mongodb
from services.crud_service import handle_crud

logger = logging.getLogger(__name__)


async def run_crud_tool(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Execute the crud tool for one MCP call.

    Args:
        query   : Natural language add/update/delete instruction.
        user_id : User identifier (defaults to DEFAULT_USER).

    Returns:
        A confirmation message or an error/clarification prompt.
    """
    logger.info("[MCP:crud] user=%s | query=%s", user_id, query[:80])

    try:
        thread_id = await _get_thread_id(user_id)
        reply = await handle_crud(query, user_id)
        await _save_turn_to_mongodb(thread_id, query, reply)

        logger.info("[MCP:crud] reply len=%d", len(reply))
        return reply

    except Exception as e:
        logger.exception("[MCP:crud] Unexpected error: %s", e)
        return f" CRUD tool error: {e}"
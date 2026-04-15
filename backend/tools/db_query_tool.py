# mcp/tools/db_query_tool.py
"""
MCP Tool: db_query

Wraps services/db_query_service.py → handle_db_query().

Handles: "list all interns", "how many employees in Surat?", etc.
Uses plain MongoDB find() — NOT vector search — so all matching records
are returned (no top_k cap).

Single-turn — no state machine, no Redis state.
"""

import logging
from services.chat_service import DEFAULT_USER, _get_thread_id, _save_turn_to_mongodb
from services.db_query_service import handle_db_query

logger = logging.getLogger(__name__)


async def run_db_query_tool(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Execute the db_query tool for one MCP call.

    Args:
        query   : Natural language listing or counting query.
        user_id : User identifier (defaults to DEFAULT_USER).

    Returns:
        A formatted list or count of employees matching the query criteria.
    """
    logger.info("[MCP:db_query] user=%s | query=%s", user_id, query[:80])

    try:
        thread_id = await _get_thread_id(user_id)
        reply = await handle_db_query(query, user_id)
        await _save_turn_to_mongodb(thread_id, query, reply)

        logger.info("[MCP:db_query] reply len=%d", len(reply))
        return reply

    except Exception as e:
        logger.exception("[MCP:db_query] Unexpected error: %s", e)
        return f" DB Query tool error: {e}"
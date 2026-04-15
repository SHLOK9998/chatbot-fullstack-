# mcp/tools/chat_rag_tool.py
"""
MCP Tool: chat_rag

Wraps the RAG pipeline from services/chat_service.py → _handle_rag().

This handles:
  - Employee knowledge base queries (Atlas Vector Search)
  - General Q&A with conversation history and rolling summaries
  - Cross-thread context from past sessions

Unlike email/calendar, this is single-turn — no state machine needed.
Each call hits the RAG pipeline directly with the full context (history +
current summary + KB results) and returns a single answer.

CONTEXT CHAIN (same as FastAPI):
  past thread summaries → current summary → recent history → KB results → query
"""

import logging
from services.chat_service import DEFAULT_USER, _get_thread_id, _save_turn_to_mongodb, _handle_rag

logger = logging.getLogger(__name__)


async def run_chat_rag_tool(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Execute the chat/RAG tool for one MCP call.

    Args:
        query   : The user's natural language question.
        user_id : User identifier (defaults to DEFAULT_USER).

    Returns:
        An answer grounded in the employee knowledge base and conversation history.
        Falls back to pure LLM response if the KB has no relevant results.
    """
    logger.info("[MCP:chat_rag] user=%s | query=%s", user_id, query[:80])

    try:
        thread_id = await _get_thread_id(user_id)
        reply = await _handle_rag(query, user_id, thread_id)
        await _save_turn_to_mongodb(thread_id, query, reply)

        logger.info("[MCP:chat_rag] reply len=%d", len(reply))
        return reply

    except Exception as e:
        logger.exception("[MCP:chat_rag] Unexpected error: %s", e)
        return f"Chat/RAG tool error: {e}"
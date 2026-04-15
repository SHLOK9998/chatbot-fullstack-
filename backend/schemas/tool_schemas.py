# mcp/schemas/tool_schemas.py
"""
JSON schemas for all MCP tools.

DESIGN DECISION:
  Every tool takes a SINGLE input field: "query" (str).
  This mirrors exactly how your FastAPI chatbot works today —
  the user sends one natural-language message and the system
  figures out the rest (intent, fields, flow state).

  user_id is hardcoded to DEFAULT_USER for now (same as current FastAPI).
  When you add multi-user support later, just add user_id here.
"""

from typing import Any

# ── Shared base ───────────────────────────────────────────────────────────────

def _single_query_schema(description: str) -> dict[str, Any]:
    """All tools share this input shape — one natural language query."""
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": description,
            }
        },
        "required": ["query"],
    }


# ── Individual tool schemas ───────────────────────────────────────────────────

EMAIL_TOOL_SCHEMA = _single_query_schema(
    "Natural language email instruction. Examples: "
    "'Send an email to John about the meeting tomorrow', "
    "'Forward the project update to the team', "
    "'Reply to Priya's email about the deadline'."
)

CALENDAR_TOOL_SCHEMA = _single_query_schema(
    "Natural language calendar instruction. Examples: "
    "'Schedule a team standup tomorrow at 10am', "
    "'Create a weekly meeting with Anand every Monday at 2pm', "
    "'Set up a project kickoff meeting next Friday at 3pm with the dev team'."
)

CHAT_RAG_TOOL_SCHEMA = _single_query_schema(
    "Natural language question answered using the employee knowledge base and "
    "conversation history. Examples: "
    "'Who is the full stack intern?', "
    "'Tell me about Anand', "
    "'What is the company's address?'."
)

DB_QUERY_TOOL_SCHEMA = _single_query_schema(
    "Natural language query to LIST or COUNT employees from the database. Examples: "
    "'List all interns', "
    "'How many employees are in Surat?', "
    "'Show all Backend developers', "
    "'Give me all full stack employees'."
)

CRUD_TOOL_SCHEMA = _single_query_schema(
    "Natural language instruction to ADD, UPDATE, or DELETE an employee record. Examples: "
    "'Add employee John Doe, Backend Senior, Mumbai, john@example.com', "
    "'Update Anand's phone number to 9876543210', "
    "'Delete the intern from Botad', "
    "'Change Priya's role to Lead'."
)

MASTER_TOOL_SCHEMA = _single_query_schema(
    "Universal entry point — send ANY natural language message here. "
    "The master tool detects intent and routes to the correct handler automatically. "
    "Supports: email sending, calendar scheduling, employee queries, "
    "employee CRUD operations, and general Q&A. "
    "Examples: 'Send email to John about the standup', "
    "'List all interns in Surat', 'Who is Anand?', "
    "'Schedule a meeting tomorrow at 3pm', "
    "'Add new employee Ravi, Backend Junior, Ahmedabad'."
)

# ── Tool registry (used by server.py) ─────────────────────────────────────────

ALL_TOOL_SCHEMAS: dict[str, dict] = {
    "email":    EMAIL_TOOL_SCHEMA,
    "calendar": CALENDAR_TOOL_SCHEMA,
    "chat_rag": CHAT_RAG_TOOL_SCHEMA,
    "db_query": DB_QUERY_TOOL_SCHEMA,
    "crud":     CRUD_TOOL_SCHEMA,
    "master":   MASTER_TOOL_SCHEMA,
}
# # mcp_server.py
# """
# FastMCP Server — registers all 5 tools and exposes them via Streamable HTTP.

# MCP endpoint:    http://127.0.0.1:8000/mcp/
# MCP Inspector:   npx @modelcontextprotocol/inspector http://127.0.0.1:8000/mcp/

# SCHEMA CHANGE:
#   send_email now only takes user_id + user_message.
#   All email fields (recipient, purpose, cc, bcc, tone) are extracted from
#   user_message automatically by email_extract.py — no structured input needed.
# """

# import logging
# from fastmcp import FastMCP
# from tools.email_tool    import send_email           as _send_email
# from tools.calendar_tool import create_calendar_event as _create_event
# from tools.employee_tool import search_employees_kb   as _search
# from tools.employee_tool import query_employee_db     as _query
# from tools.employee_tool import manage_employee       as _manage

# logger = logging.getLogger(__name__)

# mcp = FastMCP(
#     name="Personal Assistant MCP",
#     instructions=(
#         "Personal AI assistant with email, calendar, and employee tools. "
#         "Use the right tool based on the user's request."
#     ),
# )


# # ── Tool 1: Send Email ────────────────────────────────────────────────────────

# @mcp.tool()
# async def send_email(user_id: str, user_message: str) -> str:
#     """
#     Compose and send an email on behalf of the user via Gmail.

#     Use when the user wants to send, write, compose, or draft any email.
#     The tool handles everything automatically: extracting the recipient and
#     purpose from the message, asking for anything missing, generating content,
#     showing a preview, and sending after the user confirms.

#     Supports sending to: specific people by name, role groups (e.g. all interns),
#     departments, multiple recipients, or everyone.

#     Args:
#         user_id     : the user's identifier (always required)
#         user_message: the user's exact message (always required)
#     """
#     return await _send_email.ainvoke({
#         "user_id":      user_id,
#         "user_message": user_message,
#     })


# # ── Tool 2: Create Calendar Event ─────────────────────────────────────────────

# @mcp.tool()
# async def create_calendar_event(
#     user_id: str,
#     user_message: str,
#     title: str = None,
#     start_time: str = None,
#     action: str = None,
# ) -> str:
#     """
#     Create a Google Calendar event on behalf of the user.

#     Use when the user wants to schedule, create, or manage a calendar event,
#     meeting, or appointment.

#     Args:
#         user_id      : the user's identifier (always required)
#         user_message : the user's latest message (always required)
#         title        : event title if already known (optional)
#         start_time   : when the event happens, e.g. "tomorrow at 3pm" (optional)
#         action       : "confirm" / "cancel" / "modify: <instruction>" (optional)
#     """
#     return await _create_event.ainvoke({
#         "user_id":      user_id,
#         "user_message": user_message,
#         "title":        title,
#         "start_time":   start_time,
#         "action":       action,
#     })


# # ── Tool 3: Search Employee Knowledge Base ────────────────────────────────────

# @mcp.tool()
# async def search_employees_kb(query: str) -> str:
#     """
#     Search the employee knowledge base using semantic similarity.

#     Use when the user asks about a specific person, their role, email,
#     location, or any general question answerable from employee data.

#     Args:
#         query: the user's question or search phrase
#     """
#     return await _search.ainvoke({"query": query})


# # ── Tool 4: Query Employee Database ──────────────────────────────────────────

# @mcp.tool()
# async def query_employee_db(query: str, user_id: str = "default") -> str:
#     """
#     Query employee database for lists, counts, or filtered results.

#     Use when the user wants to list or count employees by role, department,
#     or location (e.g. "list all interns", "how many in Surat").

#     Args:
#         query  : the listing or counting question
#         user_id: the user's identifier
#     """
#     return await _query.ainvoke({"query": query, "user_id": user_id})


# # ── Tool 5: Manage Employee ───────────────────────────────────────────────────

# @mcp.tool()
# async def manage_employee(query: str, user_id: str = "default") -> str:
#     """
#     Add, update, or delete an employee record.

#     Use when the user wants to add a new employee, update someone's details,
#     or delete an employee from the database.

#     Args:
#         query  : the add/update/delete instruction
#         user_id: the user's identifier
#     """
#     return await _manage.ainvoke({"query": query, "user_id": user_id})


# logger.info("[MCPServer] FastMCP ready — 5 tools registered.")
# services/db_query_service.py
"""
Handles the 'db_query' intent — structured listing and counting of employees.

WHEN IS THIS CALLED?
  "list all interns"                    → find all where position = Intern
  "show all Full Stack employees"       → find all where role = Full Stack
  "how many employees are in Surat?"   → find + count where address = Surat
  "give me all Backend developers"     → find all where role = Backend

WHY NOT VECTOR SEARCH?
  Vector search returns top_k MOST SIMILAR documents (e.g. top 5 or 15).
  If you have 12 interns and ask "list all interns", vector search may return
  only 5 — the rest are silently missed.
  Plain MongoDB find() returns ALL matching documents with 100% accuracy.
  That's exactly what listing / counting queries require.

FLOW:
  1. Fetch distinct role/position/address values from MongoDB (schema grounding)
  2. LLM reads the query + the real DB values → extracts filter JSON
  3. Validate extracted values case-insensitively against the real DB values
     (FIX: old version used exact-match, so "full stack" ≠ "Full Stack" → silent fail)
  4. Build MongoDB filter with case-insensitive regex
  5. Run find() → all matching employees
  6. LLM formats results into a clean readable response
"""

import json
import logging
import asyncio
import re

from core.database import get_db
from core.dependencies import get_llm
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


# ── Step 0: Fetch live schema values from MongoDB ─────────────────────────────

async def _get_schema_values() -> dict:
    """
    Pull the actual distinct values for role, position, and address from the DB.
    Passing these to the LLM grounds it in reality — it cannot invent values
    that don't exist in the database.
    """
    db         = get_db()
    collection = db["employee_kb"]

    roles     = await collection.distinct("metadata.role")
    positions = await collection.distinct("metadata.position")
    addresses = await collection.distinct("metadata.address")

    # Filter out empty strings so the LLM doesn't see blank options
    return {
        "roles":     [r for r in roles     if r],
        "positions": [p for p in positions if p],
        "addresses": [a for a in addresses if a],
    }


# ── Step 1: LLM filter extraction ─────────────────────────────────────────────

_FILTER_PROMPT = """You are a database query filter extractor.

Convert the user's request into a JSON filter for a MongoDB employee database.

DATABASE SCHEMA
Fields: name, role, position, address, email, contact

Actual values currently in the database:

Roles     : {roles}
Positions : {positions}
Addresses : {addresses}

User query: "{query}"

RULES
1. Only extract filters that are clearly mentioned or strongly implied.
2. NEVER invent a value that is not in the lists above.
3. Match the user's words to the CLOSEST value in the lists above.
4. If no filter applies (e.g. "list all employees"), return {{}}.
5. Return ONLY valid JSON — no explanation, no markdown fences.

EXAMPLES
Query: "list all interns"          → {{"position": "Intern"}}
Query: "show employees in Surat"   → {{"address": "Surat"}}
Query: "all full stack developers" → {{"role": "Full Stack"}}
Query: "list all employees"        → {{}}

JSON:"""


async def _extract_filters(query: str) -> dict:
    """
    Ask the LLM to extract structured filter fields from the user's query.
    Then validate every extracted value against the real DB schema values
    using case-insensitive comparison.

    FIX vs old version:
      Old code did `if value in schema["roles"]` — exact match only.
      "full stack" would fail to match "Full Stack" → filter silently dropped
      → query returned ALL employees instead of filtered ones.

      New code does a case-insensitive scan and returns the DB-canonical casing.
      So "full stack" → validated as "Full Stack" (the real DB value).
    """
    schema = await _get_schema_values()

    prompt = _FILTER_PROMPT.format(
        query=query,
        roles=schema["roles"],
        positions=schema["positions"],
        addresses=schema["addresses"],
    )

    llm = get_llm()

    try:
        response = await asyncio.to_thread(
            llm.invoke,
            [HumanMessage(content=prompt)],
        )

        raw = response.content.strip() if hasattr(response, "content") else str(response).strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")

        parsed = json.loads(raw)

        if not isinstance(parsed, dict):
            return {}

        validated = {}

        for key, value in parsed.items():
            value_str = str(value).strip()

            if key == "role":
                # Case-insensitive lookup → return DB-canonical casing
                match = next(
                    (r for r in schema["roles"] if r.lower() == value_str.lower()),
                    None,
                )
                if match:
                    validated["role"] = match
                else:
                    logger.info("[DBQuery] role '%s' not in DB schema — dropped.", value_str)

            elif key == "position":
                match = next(
                    (p for p in schema["positions"] if p.lower() == value_str.lower()),
                    None,
                )
                if match:
                    validated["position"] = match
                else:
                    logger.info("[DBQuery] position '%s' not in DB schema — dropped.", value_str)

            elif key == "address":
                match = next(
                    (a for a in schema["addresses"] if a.lower() == value_str.lower()),
                    None,
                )
                if match:
                    validated["address"] = match
                else:
                    logger.info("[DBQuery] address '%s' not in DB schema — dropped.", value_str)

            elif key in ("name", "email", "contact"):
                # Free-text fields — no schema list to validate against, accept as-is
                if value_str:
                    validated[key] = value_str

        logger.info("[DBQuery] Extracted and validated filters: %s", validated)
        return validated

    except Exception as e:
        logger.warning("[DBQuery] Filter extraction failed: %s", e)
        return {}

# ── Step 2: Build MongoDB filter ──────────────────────────────────────────────

def _build_mongo_filter(filters: dict) -> dict:
    """
    Convert the validated filter dict into a MongoDB query filter.
    Uses case-insensitive regex so minor casing differences in stored data
    don't cause missed matches.

    Example:
      Input:  {"role": "Full Stack", "position": "Intern"}
      Output: {
                "metadata.role":     {"$regex": "Full Stack", "$options": "i"},
                "metadata.position": {"$regex": "Intern",     "$options": "i"}
              }
    """
    field_map = {
        "role":     "metadata.role",
        "position": "metadata.position",
        "address":  "metadata.address",
        "name":     "metadata.name",
        "email":    "metadata.email",
        "contact":  "metadata.contact",
    }

    mongo_filter = {}
    for key, value in filters.items():
        if key in field_map and value:
            mongo_filter[field_map[key]] = {
                "$regex":   str(value),
                "$options": "i",
            }

    return mongo_filter


# ── Step 3: Format results with LLM ──────────────────────────────────────────

async def _format_results(query: str, employees: list[dict]) -> str:
    if not employees:
        return "I couldn't find any employees matching that criteria."

    emp_lines = []
    for e in employees:
        m = e.get("metadata", {})
        parts = [f"- **{m.get('name','?')}**"]
        role_pos = " ".join(filter(None, [m.get('role',''), m.get('position','')]))
        if role_pos.strip():
            parts.append(role_pos)
        if m.get('address'):
            parts.append(m['address'])
        if m.get('email'):
            parts.append(m['email'])
        if m.get('contact'):
            parts.append(f"📞 {m['contact']}")
        emp_lines.append(" | ".join(parts))
    employees_text = "\n".join(emp_lines)

    format_prompt = (
        f'User question: "{query}"\n\n'
        f"Database results ({len(employees)} employees found):\n"
        f"{employees_text}\n\n"
        "Write a clean, concise, and well-formatted response to the user.\n"
        "- If they asked 'how many', lead with the count then list them.\n"
        "- If they asked for a list, present it clearly with all available details.\n"
        "- Include name, role/position, location, email, and contact for each person.\n"
        "- Do not add any information not present in the data above.\n"
        "- Do not say 'based on the database results' or similar filler phrases."
    )

    llm = get_llm()

    try:
        response = await asyncio.to_thread(
            llm.invoke,
            [HumanMessage(content=format_prompt)],
        )
        return response.content.strip() if hasattr(response, "content") else str(response).strip()

    except Exception as e:
        logger.exception("[DBQuery] Response formatting failed: %s", e)
        # Graceful fallback — still return useful data even if LLM fails
        return f"Found {len(employees)} employees:\n{employees_text}"


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_db_query(query: str, user_id: str) -> str:
    """
    Main handler for the db_query intent.

    Flow:
      1. Fetch live schema values from MongoDB
      2. LLM extracts + we validate filters against real DB values
      3. Build MongoDB regex filter
      4. find() on employee_kb — returns ALL matches (no top_k cap)
      5. LLM formats the results into a readable response
    """
    logger.info("[DBQuery] Handling query: '%s'", query[:80])

    # Steps 1 + 2: extract and validate filters
    filters      = await _extract_filters(query)
    mongo_filter = _build_mongo_filter(filters)

    logger.info("[DBQuery] MongoDB filter: %s", mongo_filter)

    try:
        db         = get_db()
        collection = db["employee_kb"]

        # Exclude embedding vector from results — large array, not needed for display
        projection = {"embedding": 0}

        cursor    = collection.find(mongo_filter, projection)
        employees = await cursor.to_list(length=500)

        logger.info(
            "[DBQuery] Found %d employees | filter=%s",
            len(employees), mongo_filter,
        )

    except Exception as e:
        logger.exception("[DBQuery] MongoDB find failed: %s", e)
        return "Sorry, I couldn't query the employee database right now. Please try again."

    return await _format_results(query, employees)


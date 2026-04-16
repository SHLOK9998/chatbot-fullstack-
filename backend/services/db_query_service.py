# services/db_query_service.py
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
    db         = get_db()
    collection = db["employee_kb"]

    departments = await collection.distinct("metadata.department")
    positions   = await collection.distinct("metadata.position")
    addresses   = await collection.distinct("metadata.address")
    names       = await collection.distinct("metadata.name")

    return {
        "departments": sorted([d for d in departments if d]),
        "positions":   sorted([p for p in positions   if p]),
        "addresses":   sorted([a for a in addresses   if a]),
        "names":       sorted([n for n in names       if n]),
    }


# ── Step 1: LLM filter extraction ─────────────────────────────────────────────

_FILTER_PROMPT = """You are a strict database filter extractor for a MongoDB employee database.

The database has these EXACT values right now:

Departments : {departments}
Positions   : {positions}
Addresses   : {addresses}
Names       : {names}

User query: "{query}"

Your job: return a JSON object with ONLY the filters that apply.
Allowed keys: "department", "position", "address", "name"

STRICT RULES:
1. You MUST only use values from the lists above — copy them EXACTLY as shown.
2. If the user mentions a department (e.g. "AIML", "devops", "full stack"), set "department" to the EXACT matching value from the Departments list.
3. If the user mentions a position (e.g. "intern", "developer"), set "position" to the EXACT matching value from the Positions list.
4. If the user mentions a city/location, set "address" to the EXACT matching value from the Addresses list.
5. If the user mentions a specific person's name, set "name" to the EXACT matching value from the Names list.
6. If no filter applies (e.g. "list all employees"), return {{}}.
7. Return ONLY valid JSON — no explanation, no markdown, no extra text.

EXAMPLES (using hypothetical values):
Query: "list all interns"                    → {{"position": "Intern"}}
Query: "show AIML employees"                 → {{"department": "AIML"}}
Query: "who is working in AIML"              → {{"department": "AIML"}}
Query: "AIML interns"                        → {{"department": "AIML", "position": "Intern"}}
Query: "employees in Surat"                  → {{"address": "Surat"}}
Query: "devops interns in Ahmedabad"         → {{"department": "DevOps", "position": "Intern", "address": "Ahmedabad"}}
Query: "list all employees"                  → {{}}

JSON:"""


async def _extract_filters(query: str, schema: dict) -> dict:
    prompt = _FILTER_PROMPT.format(
        query=query,
        departments=schema["departments"],
        positions=schema["positions"],
        addresses=schema["addresses"],
        names=schema["names"],
    )

    llm = get_llm()

    try:
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = response.content.strip() if hasattr(response, "content") else str(response).strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")

        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}

        validated = {}
        key_to_schema = {
            "department": "departments",
            "position":   "positions",
            "address":    "addresses",
            "name":       "names",
        }

        for key, value in parsed.items():
            if key not in key_to_schema:
                continue
            value_str   = str(value).strip()
            schema_list = schema[key_to_schema[key]]
            match = next(
                (item for item in schema_list if item.lower() == value_str.lower()),
                None,
            )
            if match:
                validated[key] = match
            else:
                logger.warning(
                    "[DBQuery] '%s' value '%s' not found in DB schema — dropped. "
                    "Available: %s", key, value_str, schema_list
                )

        logger.info("[DBQuery] Validated filters: %s", validated)
        return validated

    except Exception as e:
        logger.warning("[DBQuery] Filter extraction failed: %s", e)
        return {}


# ── Step 2: Build MongoDB filter ──────────────────────────────────────────────

def _build_mongo_filter(filters: dict) -> dict:
    field_map = {
        "department": "metadata.department",
        "position":   "metadata.position",
        "address":    "metadata.address",
        "name":       "metadata.name",
        "email":      "metadata.email",
        "contact":    "metadata.contact",
    }

    mongo_filter = {}
    for key, value in filters.items():
        if key in field_map and value:
            mongo_filter[field_map[key]] = {
                "$regex":   f"^{re.escape(value)}$",
                "$options": "i",
            }

    return mongo_filter


# ── Step 3: Format results (no LLM — direct, accurate) ───────────────────────

def _format_results(employees: list[dict]) -> str:
    if not employees:
        return "I couldn't find any employees matching that criteria."

    count = len(employees)
    lines = [f"Found {count} employee{'s' if count != 1 else ''}:\n"]

    for i, e in enumerate(employees, 1):
        m = e.get("metadata", {})
        parts = [f"{i}. {m.get('name', '?')}"]
        dept_pos = " | ".join(filter(None, [m.get("department", ""), m.get("position", "")]))
        if dept_pos:
            parts.append(dept_pos)
        if m.get("address"):
            parts.append(m["address"])
        if m.get("email"):
            parts.append(m["email"])
        if m.get("contact"):
            parts.append(m["contact"])
        lines.append(" | ".join(parts))

    return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_db_query(query: str, user_id: str) -> str:
    logger.info("[DBQuery] Handling query: '%s'", query[:80])

    schema       = await _get_schema_values()
    filters      = await _extract_filters(query, schema)
    mongo_filter = _build_mongo_filter(filters)

    logger.info("[DBQuery] MongoDB filter: %s", mongo_filter)

    try:
        db         = get_db()
        collection = db["employee_kb"]
        projection = {"embedding": 0}

        cursor    = collection.find(mongo_filter, projection)
        employees = await cursor.to_list(length=500)

        logger.info("[DBQuery] Found %d employees | filter=%s", len(employees), mongo_filter)

    except Exception as e:
        logger.exception("[DBQuery] MongoDB find failed: %s", e)
        return "Sorry, I couldn't query the employee database right now. Please try again."

    return _format_results(employees)

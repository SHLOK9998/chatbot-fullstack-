# services/db_query_service.py
import json
import logging
import asyncio
import re

from core.database import get_db
from core.dependencies import get_llm
from core.redis_client import get_redis
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


# ── Step 0: Fetch live schema values from MongoDB (cached in Redis) ───────────

async def _get_schema_values() -> dict:
    # Try Redis cache first (avoids 4 distinct() calls per query)
    r = get_redis()
    if r:
        try:
            cached = await r.get("db_schema_cache")
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    db         = get_db()
    collection = db["employee_kb"]

    departments = await collection.distinct("metadata.department")
    positions   = await collection.distinct("metadata.position")
    addresses   = await collection.distinct("metadata.address")
    names       = await collection.distinct("metadata.name")

    schema = {
        "departments": sorted([d for d in departments if d]),
        "positions":   sorted([p for p in positions   if p]),
        "addresses":   sorted([a for a in addresses   if a]),
        "names":       sorted([n for n in names       if n]),
    }

    # Cache for 10 minutes
    if r:
        try:
            await r.set("db_schema_cache", json.dumps(schema), ex=600)
        except Exception:
            pass

    return schema


async def invalidate_schema_cache():
    """Delete cached schema values. Call after employee add/update/delete."""
    r = get_redis()
    if r:
        try:
            await r.delete("db_schema_cache")
        except Exception:
            pass


# ── Step 1: LLM filter extraction ─────────────────────────────────────────────

_FILTER_PROMPT = """You are a strict database filter and field extractor for a MongoDB employee database.

The database has these EXACT values right now:

Departments : {departments}
Positions   : {positions}
Addresses   : {addresses}
Names       : {names}

User query: "{query}"

Return a JSON object with two keys:
1. "filters" — only the filter conditions that apply (allowed keys: "department", "position", "address", "name")
2. "fields"  — list of fields the user wants to see (allowed values: "name", "department", "position", "address", "email", "contact")

STRICT RULES:
1. For filters, only use EXACT values from the lists above.
2. If no filter applies, set "filters" to {{}}.
3. For fields: if the user asks for specific fields (e.g. "only names and emails", "name and contact"), list only those.
   If the user asks for "all details" or doesn't specify fields, set "fields" to ["name", "department", "position", "address", "email", "contact"].
4. Return ONLY valid JSON — no explanation, no markdown.

EXAMPLES:
Query: "list all interns"                           → {{"filters": {{"position": "Intern"}}, "fields": ["name", "department", "position", "address", "email", "contact"]}}
Query: "AIML interns only name and email"           → {{"filters": {{"department": "AIML", "position": "Intern"}}, "fields": ["name", "email"]}}
Query: "name and contact of all employees"          → {{"filters": {{}}, "fields": ["name", "contact"]}}
Query: "all details of devops interns"              → {{"filters": {{"department": "DevOps", "position": "Intern"}}, "fields": ["name", "department", "position", "address", "email", "contact"]}}
Query: "name and email and contact of AIML interns" → {{"filters": {{"department": "AIML", "position": "Intern"}}, "fields": ["name", "email", "contact"]}}

JSON:"""


ALL_FIELDS = ["name", "department", "position", "address", "email", "contact"]


async def _extract_filters(query: str, schema: dict) -> tuple[dict, list[str]]:
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

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        raw = m.group(0) if m else "{}"

        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}, ALL_FIELDS

        # --- validate filters ---
        raw_filters = parsed.get("filters", {})
        key_to_schema = {
            "department": "departments",
            "position":   "positions",
            "address":    "addresses",
            "name":       "names",
        }
        validated = {}
        for key, value in raw_filters.items():
            if key not in key_to_schema:
                continue
            value_str   = str(value).strip()
            schema_list = schema[key_to_schema[key]]
            match = next((item for item in schema_list if item.lower() == value_str.lower()), None)
            if match:
                validated[key] = match
            else:
                validated[key] = value_str
                logger.warning(
                    "[DBQuery] '%s' value '%s' not found in DB schema — keeping it to return 0 results. "
                    "Available: %s", key, value_str, schema_list
                )

        # --- validate fields ---
        raw_fields = parsed.get("fields", ALL_FIELDS)
        fields = [f for f in raw_fields if f in ALL_FIELDS] or ALL_FIELDS

        logger.info("[DBQuery] Validated filters: %s | fields: %s", validated, fields)
        return validated, fields

    except Exception as e:
        logger.warning("[DBQuery] Filter extraction failed: %s", e)
        return {}, ALL_FIELDS


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


# ── Step 3: Build structured data for LLM formatting ─────────────────────────

def _build_employee_data(employees: list[dict], fields: list[str]) -> list[dict]:
    result = []
    for e in employees:
        m = e.get("metadata", {})
        result.append({f: m.get(f, "") for f in fields if m.get(f)})
    return result


# ── Step 4: LLM formats the final conversational response ─────────────────────

_FORMAT_PROMPT = """You are a helpful assistant. Answer the user's query conversationally based on the data below.

User query: "{query}"

Data ({count} result(s)):
{data}

RULES:
- Only mention the fields present in the data, nothing else.
- Be concise and natural. Use a numbered list if there are multiple results.
- If data is empty, say no matching employees were found.
- Do NOT add any fields that are not in the data."""


async def _llm_format_response(query: str, employees: list[dict], fields: list[str]) -> str:
    if not employees:
        return "I couldn't find any employees matching that criteria."

    data = _build_employee_data(employees, fields)
    llm  = get_llm()

    prompt = _FORMAT_PROMPT.format(
        query=query,
        count=len(data),
        data=json.dumps(data, indent=2),
    )

    try:
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        return response.content.strip() if hasattr(response, "content") else str(response).strip()
    except Exception as e:
        logger.warning("[DBQuery] LLM formatting failed: %s", e)
        # fallback: plain list
        lines = [f"Found {len(data)} employee(s):"]
        for i, emp in enumerate(data, 1):
            lines.append(f"{i}. " + " | ".join(f"{k}: {v}" for k, v in emp.items()))
        return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_db_query(query: str, user_id: str) -> str:
    logger.info("[DBQuery] Handling query: '%s'", query[:80])

    schema              = await _get_schema_values()
    filters, fields     = await _extract_filters(query, schema)
    mongo_filter        = _build_mongo_filter(filters)

    logger.info("[DBQuery] MongoDB filter: %s | fields: %s", mongo_filter, fields)

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

    return await _llm_format_response(query, employees, fields)

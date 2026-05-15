# services/crud_service.py
import json
import logging
import asyncio
import re
from datetime import datetime, timezone

from core.database import get_db
from core.dependencies import get_llm
from services.embedding_service import EmbeddingService
from services.db_query_service import invalidate_schema_cache

logger = logging.getLogger(__name__)

# Lazy singleton — avoids creating EmbeddingService at import time
_embedding_service = None
def _get_embedding_service() -> EmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service

_REQUIRED_ADD_FIELDS = ["name", "email", "department", "position", "contact"]

# ── Multi-turn state for incomplete ADD operations ────────────────────────────
# Maps user_id → { "data": {...}, "attempts": int }
# Cleared once the employee is successfully added or the user abandons.
_pending_add: dict[str, dict] = {}

# Max follow-up turns before auto-abandoning a stuck flow
_MAX_PENDING_ATTEMPTS = 4

def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Cancellation keywords ──────────────────────────────────────────────────────

_CANCEL_PHRASES = {"cancel", "stop", "abort", "quit", "exit", "never mind", "nevermind", "forget it", "leave it"}

def _is_cancel_intent(query: str) -> bool:
    q = query.strip().lower()
    return any(phrase in q for phrase in _CANCEL_PHRASES)


# ── Action extraction prompt ───────────────────────────────────────────────────

_ACTION_EXTRACT_PROMPT = """You are a database operation extractor for an employee management system.

Employee fields and their meaning:
- name: first name
- middle_name: middle name (optional)
- lastname: last/family name
- department: team or division (e.g. AIML, DevOps, Backend, Frontend, Full Stack, HR, Finance, QA, Design)
- position: job level or role title (e.g. Intern, Junior Developer, Senior Developer, Manager, Lead, Employee, Team Lead, CTO, CEO)
- address: physical location or city
- email: email address (contains @)
- contact: phone number (digits only, typically 10 digits)
- employee_no: employee ID number
- slackid: Slack username
- github: GitHub username
- linkedin: LinkedIn profile

IMPORTANT — department vs position:
- department = WHICH TEAM they belong to (AIML, DevOps, HR, etc.)
- position = WHAT THEIR ROLE IS (Intern, Developer, Manager, etc.)
- If the user says "Backend Developer", department="Backend" and position="Developer"
- If the user says "AIML Intern", department="AIML" and position="Intern"
- If the user says "Senior Software Engineer in DevOps", department="DevOps" and position="Senior Software Engineer"
- A standalone word like "Intern", "Manager", "Employee" is ALWAYS position, never department

User query: "{query}"

Extract the operation and data. Return ONLY valid JSON in one of these formats:

For ADD (new employee):
{{"operation": "add", "data": {{"name": "...", "middle_name": "...", "lastname": "...", "department": "...", "position": "...", "address": "...", "email": "...", "contact": "...", "employee_no": "...", "slackid": "...", "github": "...", "linkedin": "..."}}}}

For UPDATE (change existing employee):
{{"operation": "update", "find_by": {{"name": "..."}}, "update_fields": {{"department": "...", "contact": "...", "address": "...", "email": "..."}}}}

For DELETE (remove employee):
{{"operation": "delete", "find_by": {{"name": "..."}}}}

Rules:
- For ADD: include only fields the user mentioned. Do NOT invent values.
- For UPDATE: find_by identifies the employee (usually by name), update_fields has only changed fields.
- For DELETE: find_by identifies which employee to delete.
- Return ONLY the JSON. No explanation, no markdown fences.

JSON:"""


async def _extract_action(query: str) -> dict:
    llm    = get_llm()
    prompt = _ACTION_EXTRACT_PROMPT.format(query=query)
    try:
        from langchain_core.messages import HumanMessage
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = response.content.strip() if hasattr(response, "content") else str(response).strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "operation" in parsed:
            logger.info("[CRUD] Extracted action: %s", parsed.get("operation"))
            return parsed
    except Exception as e:
        logger.warning("[CRUD] Action extraction failed: %s", e)
    return {}


def _build_content_text(data: dict) -> str:
    name       = data.get('name', 'Unknown')
    middle     = data.get('middle_name', '')
    lastname   = data.get('lastname', '')
    department = data.get('department', '')
    position   = data.get('position', '')
    address    = data.get('address', '')
    email      = data.get('email', '')
    contact    = data.get('contact', '')
    slackid    = data.get('slackid', '')
    github     = data.get('github', '')
    linkedin   = data.get('linkedin', '')
    return (
        f"Employee profile: {name} {middle} {lastname}.\n"
        f"Role: {position} in the {department} department.\n"
        f"Location: {address}.\n"
        f"Contact: Email is {email}, phone is {contact}.\n"
        f"Online: Slack @{slackid}, GitHub @{github}, LinkedIn @{linkedin}.\n"
        f"Keywords: {department} {position} {address} {name}"
    ).strip()


def _check_required_add_fields(data: dict) -> list[str]:
    """Return list of missing required fields for ADD operation."""
    missing = []
    for field in _REQUIRED_ADD_FIELDS:
        val = data.get(field, "")
        if not val or not str(val).strip():
            missing.append(field)
    return missing


async def _add_employee(data: dict) -> str:
    missing = _check_required_add_fields(data)
    if missing:
        field_labels = {
            "name":       "Full Name",
            "email":      "Email Address",
            "department": "Department (e.g. AIML, DevOps, Full Stack)",
            "position":   "Position / Role (e.g. Intern, Senior Developer, Manager)",
            "contact":    "Contact Number",
        }
        missing_display = ", ".join(field_labels.get(f, f) for f in missing)
        return (
            f"❌ Cannot add employee — the following required fields are missing: **{missing_display}**.\n\n"
            f"Please provide: name, email, department, position/role, and contact number.\n"
            f'Example: "Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210"'
        )

    db         = get_db()
    collection = db["employee_kb"]

    existing = await collection.find_one(
        {"metadata.name": {"$regex": f"^{re.escape(data['name'])}$", "$options": "i"}}
    )
    if existing:
        return f"⚠️ An employee named '{data['name']}' already exists. Use update if you want to modify their data."

    content = _build_content_text(data)
    try:
        embedding = await _get_embedding_service().get_embedding(content)
    except Exception as e:
        logger.error("[CRUD] Embedding failed for new employee: %s", e)
        embedding = []

    doc = {
        "content":    content,
        "embedding":  embedding,
        "metadata":   data,
        "source":     "chat_crud",
        "created_at": _now(),
    }

    await collection.insert_one(doc)
    await invalidate_schema_cache()
    logger.info("[CRUD] Added new employee: %s", data.get("name"))

    name       = data.get("name")
    department = data.get("department", "")
    position   = data.get("position", "")
    email      = data.get("email", "")
    contact    = data.get("contact", "")
    address    = data.get("address", "")

    summary = f"✅ Employee **{name}** has been added successfully.\n"
    summary += f"- Department: {department} | Position: {position}\n"
    summary += f"- Email: {email}\n"
    summary += f"- Contact: {contact}\n"
    if address:
        summary += f"- Location: {address}\n"
    return summary.strip()


async def _update_employee(find_by: dict, update_fields: dict) -> str:
    if not find_by or not update_fields:
        return "❌ Cannot update — missing search criteria or update fields."

    db         = get_db()
    collection = db["employee_kb"]

    search_filter = {}
    for key, value in find_by.items():
        search_filter[f"metadata.{key}"] = {"$regex": str(value), "$options": "i"}

    existing = await collection.find_one(search_filter)
    if not existing:
        find_desc = ", ".join(f"{k}={v}" for k, v in find_by.items())
        return f"❌ No employee found matching: {find_desc}"

    current_metadata = existing.get("metadata", {})
    current_metadata.update(update_fields)

    new_content = _build_content_text(current_metadata)
    try:
        new_embedding = await _get_embedding_service().get_embedding(new_content)
    except Exception as e:
        logger.error("[CRUD] Re-embedding failed for update: %s", e)
        new_embedding = existing.get("embedding", [])

    set_payload = {"content": new_content, "embedding": new_embedding}
    for key, value in update_fields.items():
        set_payload[f"metadata.{key}"] = value

    await collection.update_one({"_id": existing["_id"]}, {"$set": set_payload})
    await invalidate_schema_cache()

    name               = current_metadata.get("name", "Employee")
    updated_fields_str = ", ".join(f"{k}={v}" for k, v in update_fields.items())
    logger.info("[CRUD] Updated employee '%s': %s", name, updated_fields_str)
    return f"✅ Updated '{name}': {updated_fields_str}"


async def _delete_employee(find_by: dict) -> str:
    if not find_by:
        return "❌ Cannot delete — no employee identifier provided."

    db         = get_db()
    collection = db["employee_kb"]

    search_filter = {}
    for key, value in find_by.items():
        search_filter[f"metadata.{key}"] = {"$regex": str(value), "$options": "i"}

    existing = await collection.find_one(search_filter)
    if not existing:
        find_desc = ", ".join(f"{k}={v}" for k, v in find_by.items())
        return f"❌ No employee found matching: {find_desc}"

    name = existing.get("metadata", {}).get("name", "Unknown")
    await collection.delete_one({"_id": existing["_id"]})
    await invalidate_schema_cache()

    logger.info("[CRUD] Deleted employee: %s", name)
    return f"✅ Employee '{name}' has been deleted from the database."


# ── Improved missing-fields extraction prompt ─────────────────────────────────

_MISSING_FIELDS_PROMPT = """Extract employee information from this message.

Message: "{query}"

Field definitions:
- name: person's first name
- middle_name: middle name (optional)
- lastname: last/family name
- department: TEAM or DIVISION they belong to (e.g. AIML, DevOps, Backend, Frontend, HR, Finance, QA)
- position: JOB TITLE or ROLE LEVEL (e.g. Intern, Junior Developer, Senior Developer, Manager, Employee, Team Lead, Engineer)
- address: city or physical location
- email: email address (must contain @ and .)
- contact: phone number (10 digits, only digits)
- employee_no: employee ID
- slackid: Slack username
- github: GitHub username
- linkedin: LinkedIn URL or username

IMPORTANT rules for department vs position:
- A word like "Intern", "Manager", "Employee", "Developer", "Engineer" is ALWAYS position
- A word like "AIML", "DevOps", "Backend", "Frontend", "HR", "Finance" is ALWAYS department
- "Backend Developer" → department=Backend, position=Developer
- "AIML Intern" → department=AIML, position=Intern
- "Senior Software Engineer" → position=Senior Software Engineer (no department implied)

Currently collected data: {existing}
Extract ONLY the NEW fields present in the message above that are missing from the collected data.

Return ONLY a JSON object with fields found. Return {{}} if nothing new is found.
No explanation, no markdown fences.

JSON:"""


async def _extract_missing_fields(query: str, existing: dict) -> dict:
    """Extract employee fields from a follow-up message with awareness of what's already collected."""
    llm = get_llm()
    existing_summary = {k: v for k, v in existing.items() if v and str(v).strip()}
    prompt = _MISSING_FIELDS_PROMPT.format(query=query, existing=json.dumps(existing_summary))
    try:
        from langchain_core.messages import HumanMessage
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = response.content.strip() if hasattr(response, "content") else str(response).strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception as e:
        logger.warning("[CRUD] Missing field extraction failed: %s", e)
    return {}


def _format_missing_prompt(missing: list[str], partial: dict) -> str:
    """Build a friendly, context-aware message asking for missing fields."""
    field_labels = {
        "name":       "Full Name",
        "email":      "Email Address",
        "department": "Department (e.g. AIML, DevOps, Full Stack, HR)",
        "position":   "Position / Role (e.g. Intern, Senior Developer, Manager)",
        "contact":    "Contact Number (10-digit phone)",
    }
    # Build a summary of what we already have
    collected = {k: v for k, v in partial.items() if v and str(v).strip() and k in _REQUIRED_ADD_FIELDS}
    collected_display = ", ".join(f"{k}={v}" for k, v in collected.items()) if collected else "nothing yet"

    missing_display = "\n".join(f"  - {field_labels.get(f, f)}" for f in missing)
    return (
        f"Got it. Still need the following to add the employee:\n"
        f"{missing_display}\n\n"
        f"(Already collected: {collected_display})\n\n"
        f'You can say "cancel" to stop at any time.'
    )


# ── Public entry point ─────────────────────────────────────────────────────────

async def handle_crud(query: str, user_id: str) -> str:
    logger.info("[CRUD] Handling query: '%s'", query[:80])

    # ── Handle cancellation in any state ──────────────────────────────────────
    if _is_cancel_intent(query):
        if user_id in _pending_add:
            del _pending_add[user_id]
            return "Cancelled. The employee add operation has been discarded."
        # Not in a flow — fall through to normal handling (might be an unrelated query)

    # ── Resume a pending ADD if we're waiting for missing fields ──────────────
    if user_id in _pending_add:
        state   = _pending_add[user_id]
        partial  = state["data"]
        attempts = state.get("attempts", 0) + 1

        # Safety valve — abandon after too many failed attempts
        if attempts > _MAX_PENDING_ATTEMPTS:
            del _pending_add[user_id]
            return (
                "I've tried several times but couldn't collect all the required details. "
                "The add operation has been cancelled.\n\n"
                "Please try again with all details in one message, for example:\n"
                '"Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210"'
            )

        new_fields = await _extract_missing_fields(query, partial)
        logger.info("[CRUD] Pending add — extracted new fields: %s (attempt %d)", new_fields, attempts)

        # Merge new fields into partial — only overwrite with non-empty values
        merged_any = False
        for k, v in new_fields.items():
            if v and str(v).strip():
                partial[k] = v
                merged_any = True

        state["attempts"] = attempts
        still_missing = _check_required_add_fields(partial)

        if still_missing:
            _pending_add[user_id] = state

            # If we made no progress and it's not the first attempt, give a hint
            if not merged_any and attempts >= 2:
                hint = (
                    "\n\nTip: I'm having trouble extracting the information. "
                    "Try sending the missing details clearly, for example:\n"
                    '"position: Senior Developer, department: Backend"'
                )
            else:
                hint = ""

            return _format_missing_prompt(still_missing, partial) + hint

        # All fields collected — proceed to add
        del _pending_add[user_id]
        return await _add_employee(partial)

    # ── Normal path ───────────────────────────────────────────────────────────
    action = await _extract_action(query)

    if not action:
        return (
            "I couldn't understand what change you want to make. Please be more specific.\n"
            "Examples:\n"
            '- "Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210"\n'
            '- "Update Anand\'s phone number to 9876543210"\n'
            '- "Delete employee Priya"'
        )

    operation = action.get("operation", "").lower()

    if operation == "add":
        data = action.get("data", {})
        if not data:
            return (
                "❌ Please provide employee details to add.\n"
                "Required: name, email, department, position/role, contact number.\n"
                'Example: "Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210"'
            )

        missing = _check_required_add_fields(data)
        if missing:
            # Store partial data and ask for what's missing
            _pending_add[user_id] = {"data": data, "attempts": 0}
            logger.info("[CRUD] Partial add stored for user=%s | missing=%s", user_id, missing)
            return _format_missing_prompt(missing, data)

        return await _add_employee(data)

    elif operation == "update":
        find_by       = action.get("find_by", {})
        update_fields = action.get("update_fields", {})
        return await _update_employee(find_by, update_fields)

    elif operation == "delete":
        find_by = action.get("find_by", {})
        return await _delete_employee(find_by)

    else:
        logger.warning("[CRUD] Unknown operation '%s'", operation)
        return "I understood you want to change employee data, but couldn't determine if you want to add, update, or delete. Please rephrase."


# services/crud_service.py
import json
import logging
import asyncio
import re
from datetime import datetime, timezone

from core.database import get_db
from core.dependencies import get_llm
from services.embedding_service import EmbeddingService
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)
embedding_service = EmbeddingService()

_REQUIRED_ADD_FIELDS = ["name", "email", "role", "position", "contact"]

def _now() -> datetime:
    return datetime.now(timezone.utc)


_ACTION_EXTRACT_PROMPT = """You are a database operation extractor for an employee management system.

Employee fields: name, role, position (designation), address, email, contact, employee_no, slack, github, linkedin

User query: "{query}"

Extract the operation and data. Return ONLY valid JSON in one of these formats:

For ADD (new employee):
{{"operation": "add", "data": {{"name": "...", "middle_name": "...", "surname": "...", "role": "...", "position": "...", "address": "...", "email": "...", "contact": "...", "employee_no": "...", "slack": "...", "github": "...", "linkedin": "..."}}}}

For UPDATE (change existing employee):
{{"operation": "update", "find_by": {{"name": "..."}}, "update_fields": {{"role": "...", "contact": "...", "address": "...", "email": "..."}}}}

For DELETE (remove employee):
{{"operation": "delete", "find_by": {{"name": "..."}}}}

Rules:
- For ADD: include all fields the user mentioned. Leave out fields not mentioned (do not invent them).
- For UPDATE: find_by identifies the employee (usually by name), update_fields has only changed fields.
- For DELETE: find_by identifies which employee to delete.
- Return ONLY the JSON. No explanation, no markdown fences.

JSON:"""


async def _extract_action(query: str) -> dict:
    llm    = get_llm()
    prompt = _ACTION_EXTRACT_PROMPT.format(query=query)
    try:
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
    return (
        f"{data.get('name', 'Unknown')} is a "
        f"{data.get('role', '')} {data.get('position', '')} "
        f"located in {data.get('address', '')}. "
        f"Email: {data.get('email', '')}. "
        f"Contact: {data.get('contact', '')}."
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
            "name":     "Full Name",
            "email":    "Email Address",
            "role":     "Role (e.g. Full Stack, Backend, Frontend)",
            "position": "Designation/Position (e.g. Intern, Senior, Lead)",
            "contact":  "Contact Number",
        }
        missing_display = ", ".join(field_labels.get(f, f) for f in missing)
        return (
            f"❌ Cannot add employee — the following required fields are missing: **{missing_display}**.\n\n"
            f"Please provide: name, email, role, designation/position, and contact number.\n"
            f"Example: \"Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210\""
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
        embedding = await embedding_service.get_embedding(content)
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
    logger.info("[CRUD] Added new employee: %s", data.get("name"))

    name     = data.get("name")
    role     = data.get("role", "")
    position = data.get("position", "")
    email    = data.get("email", "")
    contact  = data.get("contact", "")
    address  = data.get("address", "")

    summary = f"✅ Employee **{name}** has been added successfully.\n"
    summary += f"- Role: {role} {position}\n"
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
        new_embedding = await embedding_service.get_embedding(new_content)
    except Exception as e:
        logger.error("[CRUD] Re-embedding failed for update: %s", e)
        new_embedding = existing.get("embedding", [])

    set_payload = {"content": new_content, "embedding": new_embedding}
    for key, value in update_fields.items():
        set_payload[f"metadata.{key}"] = value

    await collection.update_one({"_id": existing["_id"]}, {"$set": set_payload})

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

    logger.info("[CRUD] Deleted employee: %s", name)
    return f"✅ Employee '{name}' has been deleted from the database."


# ── Public entry point ─────────────────────────────────────────────────────────

async def handle_crud(query: str, user_id: str) -> str:
    logger.info("[CRUD] Handling query: '%s'", query[:80])

    action = await _extract_action(query)

    if not action:
        return (
            "I couldn't understand what change you want to make. Please be more specific.\n"
            "Examples:\n"
            "- \"Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210\"\n"
            "- \"Update Anand's phone number to 9876543210\"\n"
            "- \"Delete employee Priya\""
        )

    operation = action.get("operation", "").lower()

    if operation == "add":
        data = action.get("data", {})
        if not data:
            return (
                "❌ Please provide employee details to add.\n"
                "Required: name, email, role, designation/position, contact number.\n"
                "Example: \"Add employee John Doe, john@example.com, Backend, Senior Developer, 9876543210\""
            )
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

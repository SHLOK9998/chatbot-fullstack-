"""
services/calendar_task/cal_modifier.py

Calendar Event Modifier — upgraded with MongoDB attendee name resolution.

WHAT CHANGED vs. OLD VERSION:
  1. When the LLM adds an attendee by name (no email), _resolve_attendee_name()
     tries MongoDB KB lookup (same 3-pass strategy as ask_attendees).
     If still not found, stores email=None and records the name in
     data["_modifier_missing"] so calendar_flow_service can ask the user.
  2. Returns a 2-tuple now: (updated_data, ask_question)
     ask_question is a LLM-generated question if names couldn't be resolved,
     else None — flow proceeds to preview.
  3. parse_user_time() dict-unpacking fix is preserved from the old version.
  4. All other role-edit logic (time, title, location, etc.) is UNCHANGED.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage

from core.dependencies import get_llm
from utils.time_parser import parse_user_time

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_FAKE_LOCALS = {
    "all", "toall", "everyone", "team", "staff", "interns", "employees",
    "intern", "employee", "null", "none", "example", "test", "noreply", "no-reply",
}


def _is_valid_email(email: str) -> bool:
    if not email or not isinstance(email, str):
        return False
    e = email.strip()
    if not _EMAIL_RE.fullmatch(e):
        return False
    return e.split("@")[0].lower() not in _FAKE_LOCALS


_MODIFY_PROMPT = """
You are editing an existing calendar event draft based on user instructions.

Current event data:
{event_json}

User instruction: "{instruction}"

Your task:
- Apply the instruction precisely to the relevant role(s).
- If the user changes the time/date, update start_time with the EXACT natural language phrase they used.
- If the user changes the title or purpose, update those fields.
- If the user changes the location, update location.
- If the user adds/removes attendees:
  - For additions: add to attendees list as {{"name": "<name or email>", "email": "<email if given else null>"}}
  - For removals: remove the matching attendee by name or email.
  - IMPORTANT: If adding a person by name only (no email given), set email to null — do NOT invent emails.
- If the user changes recurrence, update recurrence role with natural language (e.g. "every Monday").
- Leave all other fields unchanged.

Return ONLY valid JSON with the complete updated event fields:
{{
  "title": "...",
  "purpose": "...",
  "start_time": "<natural language phrase or keep existing ISO if unchanged>",
  "location": "...",
  "description": "...",
  "recurrence": "...",
  "attendees": [{{"name": "...", "email": "..."}}]
}}

If a role is unchanged, copy it exactly from the current event data.
Return JSON only — no markdown, no extra text.
"""


def _parse_time_safe(raw_time: str, existing_iso: Optional[str] = None) -> Optional[str]:
    if not raw_time or not str(raw_time).strip():
        return existing_iso
    try:
        result = parse_user_time(str(raw_time).strip())
        if result.get("success") and result.get("datetime"):
            return result["datetime"]
    except Exception as e:
        logger.warning("[CalModifier] parse_user_time error for '%s': %s", raw_time, e)
    return existing_iso


async def _resolve_attendee_name(name: str) -> Optional[str]:
    """
    Try to find an email for an attendee name via MongoDB KB.
    Same 3-pass strategy as ask_attendees._mongo_lookup_name().
    """
    from services.calendar_task.ask_attendees import _mongo_lookup_name
    return await _mongo_lookup_name(name)


async def _generate_missing_question(missing_names: list[str]) -> str:
    """LLM-generated question for names whose emails couldn't be found."""
    names_str = ", ".join(missing_names)
    prompt = f"""You are a calendar scheduling assistant.
You need to ask the user for email addresses of: {names_str}
Their emails could not be found in the contact database.

Generate ONE short, friendly question asking for their email(s).
Use the actual name(s). Return only the question text.
"""
    llm = get_llm()
    try:
        resp = await asyncio.to_thread(llm.invoke, prompt)
        q = (getattr(resp, "content", "") or "").strip()
        if q:
            return q
    except Exception:
        pass
    return f"I couldn't find the email address(es) for {names_str}. Could you share them?"


async def modify_event(data: dict, instruction: str) -> tuple[dict, Optional[str]]:
    """
    Apply a user modification instruction to the current event data.

    Args:
        data        : current event data dict
        instruction : the user's change instruction

    Returns:
        (updated_data, ask_question)
        ask_question is not None when attendees were added by name but not resolved.
    """
    logger.info("[CalModifier] Instruction: %s", instruction[:80])

    current = {
        "title":       data.get("title") or data.get("purpose"),
        "purpose":     data.get("purpose"),
        "start_time":  data.get("start_time"),
        "location":    data.get("location"),
        "description": data.get("description"),
        "recurrence":  data.get("recurrence"),
        "attendees":   data.get("attendees") or [],
    }

    llm = get_llm()
    prompt = _MODIFY_PROMPT.format(
        event_json=json.dumps(current, ensure_ascii=False, default=str),
        instruction=instruction,
    )

    updated: dict = {}
    try:
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = (getattr(resp, "content", "") or "").strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        try:
            updated = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, flags=re.S)
            updated = json.loads(m.group()) if m else {}
        if not isinstance(updated, dict):
            updated = {}
    except Exception as e:
        logger.error("[CalModifier] LLM call failed: %s", e)
        return data, None

    # Title / purpose
    if updated.get("title"):
        data["title"] = str(updated["title"]).strip()
    if updated.get("purpose"):
        data["purpose"] = str(updated["purpose"]).strip()

    # Start time — only update if the LLM changed it AND it parses to a different ISO
    new_time_raw = updated.get("start_time")
    existing_iso = data.get("start_time")
    if new_time_raw:
        new_time_str = str(new_time_raw).strip()
        # If LLM returned the existing ISO unchanged, skip re-parsing to avoid corruption
        if new_time_str == str(existing_iso):
            pass  # unchanged — keep as-is
        else:
            iso = _parse_time_safe(new_time_str, existing_iso)
            if iso and iso != existing_iso:
                data["start_time"] = iso

    # Location / description / recurrence
    if "location"    in updated: data["location"]    = updated["location"]
    if "description" in updated: data["description"] = updated["description"]
    if "recurrence"  in updated: data["recurrence"]  = updated["recurrence"]

    # Attendees — resolve names via MongoDB
    new_attendees = updated.get("attendees")
    if new_attendees is not None and isinstance(new_attendees, list):
        clean: list[dict] = []
        missing_names: list[str] = []

        for a in new_attendees:
            if not isinstance(a, dict):
                continue
            name  = str(a.get("name")  or "").strip()
            email = str(a.get("email") or "").strip()

            # Normalize null-like email strings
            if email.lower() in ("null", "none", ""):
                email = ""

            if _is_valid_email(email):
                clean.append({"name": name, "email": email})
            elif name:
                # No valid email — try MongoDB lookup
                found = await _resolve_attendee_name(name)
                if found and _is_valid_email(found):
                    clean.append({"name": name, "email": found})
                    logger.info("[CalModifier] Resolved '%s' → %s", name, found)
                else:
                    clean.append({"name": name, "email": None})
                    missing_names.append(name)
                    logger.warning("[CalModifier] Could not resolve '%s'", name)

        data["attendees"] = clean
        data["missing_fields"] = []

        if missing_names:
            ask_question = await _generate_missing_question(missing_names)
            return data, ask_question

    data["missing_fields"] = []
    return data, None

# """
# services/calendar_task/cal_modifier.py

# Calendar Event Modifier — upgraded with MongoDB attendee name resolution.

# WHAT CHANGED vs. OLD VERSION:
#   1. When the LLM adds an attendee by name (no email), _resolve_attendee_name()
#      tries MongoDB KB lookup (same 3-pass strategy as ask_attendees).
#      If still not found, stores email=None and records the name in
#      data["_modifier_missing"] so calendar_flow_service can ask the user.
#   2. Returns a 2-tuple now: (updated_data, ask_question)
#      ask_question is a LLM-generated question if names couldn't be resolved,
#      else None — flow proceeds to preview.
#   3. parse_user_time() dict-unpacking fix is preserved from the old version.
#   4. All other role-edit logic (time, title, location, etc.) is UNCHANGED.
# """

# import asyncio
# import json
# import logging
# import re
# from typing import Optional

# from langchain_core.messages import HumanMessage

# from core.dependencies import get_llm
# from utils.time_parser import parse_user_time

# logger = logging.getLogger(__name__)

# _EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# _FAKE_LOCALS = {
#     "all", "toall", "everyone", "team", "staff", "interns", "employees",
#     "intern", "employee", "null", "none", "example", "test", "noreply", "no-reply",
# }


# def _is_valid_email(email: str) -> bool:
#     if not email or not isinstance(email, str):
#         return False
#     e = email.strip()
#     if not _EMAIL_RE.fullmatch(e):
#         return False
#     return e.split("@")[0].lower() not in _FAKE_LOCALS


# _MODIFY_PROMPT = """
# You are editing an existing calendar event draft based on user instructions.

# Current event data:
# {event_json}

# User instruction: "{instruction}"

# Your task:
# - Apply the instruction precisely to the relevant role(s).
# - If the user changes the time/date, update start_time with the EXACT natural language phrase they used.
# - If the user changes the title or purpose, update those fields.
# - If the user changes the location, update location.
# - If the user adds/removes attendees:
#   - For additions: add to attendees list as {{"name": "<name or email>", "email": "<email if given else null>"}}
#   - For removals: remove the matching attendee by name or email.
#   - IMPORTANT: If adding a person by name only (no email given), set email to null — do NOT invent emails.
# - If the user changes recurrence, update recurrence role with natural language (e.g. "every Monday").
# - Leave all other fields unchanged.

# Return ONLY valid JSON with the complete updated event fields:
# {{
#   "title": "...",
#   "purpose": "...",
#   "start_time": "<natural language phrase or keep existing ISO if unchanged>",
#   "location": "...",
#   "description": "...",
#   "recurrence": "...",
#   "attendees": [{{"name": "...", "email": "..."}}]
# }}

# If a role is unchanged, copy it exactly from the current event data.
# Return JSON only — no markdown, no extra text.
# """


# def _parse_time_safe(raw_time: str, existing_iso: Optional[str] = None) -> Optional[str]:
#     if not raw_time or not str(raw_time).strip():
#         return existing_iso
#     try:
#         result = parse_user_time(str(raw_time).strip())
#         if result.get("success") and result.get("datetime"):
#             return result["datetime"]
#     except Exception as e:
#         logger.warning("[CalModifier] parse_user_time error for '%s': %s", raw_time, e)
#     return existing_iso


# async def _resolve_attendee_name(name: str) -> Optional[str]:
#     """
#     Try to find an email for an attendee name via MongoDB KB.
#     Same 3-pass strategy as ask_attendees._mongo_lookup_name().
#     """
#     from services.calendar_task.ask_attendees import _mongo_lookup_name
#     return await _mongo_lookup_name(name)


# async def _generate_missing_question(missing_names: list[str]) -> str:
#     """LLM-generated question for names whose emails couldn't be found."""
#     names_str = ", ".join(missing_names)
#     prompt = f"""You are a calendar scheduling assistant.
# You need to ask the user for email addresses of: {names_str}
# Their emails could not be found in the contact database.

# Generate ONE short, friendly question asking for their email(s).
# Use the actual name(s). Return only the question text.
# """
#     llm = get_llm()
#     try:
#         resp = await asyncio.to_thread(llm.invoke, prompt)
#         q = (getattr(resp, "content", "") or "").strip()
#         if q:
#             return q
#     except Exception:
#         pass
#     return f"I couldn't find the email address(es) for {names_str}. Could you share them?"


# async def modify_event(data: dict, instruction: str) -> tuple[dict, Optional[str]]:
#     """
#     Apply a user modification instruction to the current event data.

#     Args:
#         data        : current event data dict
#         instruction : the user's change instruction

#     Returns:
#         (updated_data, ask_question)
#         ask_question is not None when attendees were added by name but not resolved.
#     """
#     logger.info("[CalModifier] Instruction: %s", instruction[:80])

#     current = {
#         "title":       data.get("title") or data.get("purpose"),
#         "purpose":     data.get("purpose"),
#         "start_time":  data.get("start_time"),
#         "location":    data.get("location"),
#         "description": data.get("description"),
#         "recurrence":  data.get("recurrence"),
#         "attendees":   data.get("attendees") or [],
#     }

#     llm = get_llm()
#     prompt = _MODIFY_PROMPT.format(
#         event_json=json.dumps(current, ensure_ascii=False, default=str),
#         instruction=instruction,
#     )

#     updated: dict = {}
#     try:
#         resp = llm.invoke([HumanMessage(content=prompt)])
#         raw = (getattr(resp, "content", "") or "").strip()
#         raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
#         try:
#             updated = json.loads(raw)
#         except Exception:
#             m = re.search(r"\{.*\}", raw, flags=re.S)
#             updated = json.loads(m.group()) if m else {}
#         if not isinstance(updated, dict):
#             updated = {}
#     except Exception as e:
#         logger.error("[CalModifier] LLM call failed: %s", e)
#         return data, None

#     # Title / purpose
#     if updated.get("title"):
#         data["title"] = str(updated["title"]).strip()
#     if updated.get("purpose"):
#         data["purpose"] = str(updated["purpose"]).strip()

#     # Start time
#     new_time_raw = updated.get("start_time")
#     existing_iso = data.get("start_time")
#     if new_time_raw:
#         new_time_str = str(new_time_raw).strip()
#         if new_time_str != str(existing_iso):
#             iso = _parse_time_safe(new_time_str, existing_iso)
#             if iso and iso != existing_iso:
#                 data["start_time"] = iso

#     # Location / description / recurrence
#     if "location"    in updated: data["location"]    = updated["location"]
#     if "description" in updated: data["description"] = updated["description"]
#     if "recurrence"  in updated: data["recurrence"]  = updated["recurrence"]

#     # Attendees — resolve names via MongoDB
#     new_attendees = updated.get("attendees")
#     if new_attendees is not None and isinstance(new_attendees, list):
#         clean: list[dict] = []
#         missing_names: list[str] = []

#         for a in new_attendees:
#             if not isinstance(a, dict):
#                 continue
#             name  = str(a.get("name")  or "").strip()
#             email = str(a.get("email") or "").strip()

#             # Normalize null-like email strings
#             if email.lower() in ("null", "none", ""):
#                 email = ""

#             if _is_valid_email(email):
#                 clean.append({"name": name, "email": email})
#             elif name:
#                 # No valid email — try MongoDB lookup
#                 found = await _resolve_attendee_name(name)
#                 if found and _is_valid_email(found):
#                     clean.append({"name": name, "email": found})
#                     logger.info("[CalModifier] Resolved '%s' → %s", name, found)
#                 else:
#                     clean.append({"name": name, "email": None})
#                     missing_names.append(name)
#                     logger.warning("[CalModifier] Could not resolve '%s'", name)

#         data["attendees"] = clean
#         data["missing_fields"] = []

#         if missing_names:
#             ask_question = await _generate_missing_question(missing_names)
#             return data, ask_question

#     data["missing_fields"] = []
#     return data, None
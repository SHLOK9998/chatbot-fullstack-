"""
services/calendar_task/calendar_extract.py

KEY FIX: LLM prompt now includes "attendees" role.

Old version had no attendees in the extraction prompt, so names like
"shlok and yash" in "schedule meeting with shlok and yash tomorrow 3pm"
were completely ignored. Now they are captured as raw_attendees and
passed to ask_attendees for MongoDB resolution.
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

_EXTRACT_PROMPT = """
You are a smart calendar scheduling assistant.

Conversation history (for reference resolution ONLY):
{memory}

User message: "{message}"

Extract a single calendar event from the USER MESSAGE ONLY.
Use conversation history ONLY to resolve explicit references like "it", "that meeting", "the same time" — never to fill in title, attendees, or time unless the user's current message explicitly mentions or references them.

Return ONLY valid JSON:
{{
  "title"      : "<short event name or null>",
  "purpose"    : "<brief purpose or same as title or null>",
  "start_time" : "<exact date/time phrase the user used in THIS message, e.g. 'tomorrow at 3pm', or null>",
  "location"   : "<place or null>",
  "description": "<extra notes or null>",
  "recurrence" : "<recurrence pattern e.g. 'every Monday', or null>",
  "attendees"  : ["<name, email, or role exactly as user wrote in THIS message — only people the user explicitly mentioned inviting now>"]
}}

Rules:
- title/purpose: infer from the CURRENT message only. Do NOT borrow titles from history.
- start_time: EXACT phrase the user said in THIS message. Do NOT convert to ISO. Null if not mentioned.
- attendees: only people explicitly mentioned in THIS message as invitees. If no one mentioned → [].
- recurrence: only if user explicitly mentioned a repeating pattern in THIS message.
- Unknown fields → null. Return JSON only, no markdown.
"""

async def _get_thread_history(user_id: str, thread_id: Optional[str] = None) -> str:
    try:
        from services.message_service import format_history_from_db
        from services.thread_service import get_active_thread
        if not thread_id:
            thread_id = await get_active_thread(user_id)
        if not thread_id:
            return ""
        return await format_history_from_db(thread_id, limit=4)
    except Exception as e:
        logger.warning("[CalExtract] Thread history failed: %s", e)
        return ""

async def extract_calendar_fields(
    user_message: str,
    user_id: str,
    thread_id: Optional[str] = None,
) -> dict:
    """
    Extract calendar event fields from the user's message.

    Returns:
        title, purpose, start_time (ISO), location, description, recurrence,
        raw_attendees (list[str] — names/emails/roles as typed by user),
        missing_fields (list[str])

    raw_attendees are NOT resolved to emails yet — ask_attendees handles that.
    """
    logger.info("[CalExtract] user=%s msg='%s'", user_id, user_message[:80])

    memory = await _get_thread_history(user_id, thread_id)
    prompt = _EXTRACT_PROMPT.format(memory=memory or "(empty)", message=user_message)

    llm = get_llm()
    fields: dict = {}
    try:
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = (getattr(resp, "content", "") or "").strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        fields = json.loads(raw)
        if not isinstance(fields, dict):
            fields = {}
    except Exception as e:
        logger.warning("[CalExtract] parse failed: %s", e)
        fields = {}

    # Parse start_time → ISO
    start_iso: Optional[str] = None
    raw_time = fields.get("start_time")
    if raw_time and str(raw_time).strip().lower() not in ("null", "none", ""):
        r = parse_user_time(str(raw_time).strip())
        if r["success"]:
            start_iso = r["datetime"]
            logger.info("[CalExtract] time '%s' → %s", raw_time, start_iso)
        else:
            logger.warning("[CalExtract] cannot parse time: '%s'", raw_time)

    title = (fields.get("title") or fields.get("purpose") or "").strip()
    title = None if not title or title.lower() in ("null", "none") else title

    # Raw attendee strings — names/emails/roles as user wrote them
    raw_att = fields.get("attendees") or []
    if not isinstance(raw_att, list):
        raw_att = []
    raw_attendees = [str(a).strip() for a in raw_att if str(a).strip()]

    missing: list[str] = []
    if not start_iso:
        missing.append("start_time")
    if not title:
        missing.append("title")

    logger.info("[CalExtract] done | missing=%s raw_attendees=%s", missing, raw_attendees)
    return {
        "title":          title,
        "purpose":        (_n(fields.get("purpose")) or title),
        "start_time":     start_iso,
        "location":       _n(fields.get("location")),
        "description":    _n(fields.get("description")),
        "recurrence":     _n(fields.get("recurrence")),
        "raw_attendees":  raw_attendees,
        "missing_fields": missing,
    }


def _n(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return None if not s or s.lower() in ("null", "none") else s
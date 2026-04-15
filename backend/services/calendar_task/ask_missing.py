"""services/calendar_task/ask_missing.py — MongoDB thread history."""
import json, logging, re
from typing import Optional
from langchain_core.messages import HumanMessage
from core.dependencies import get_llm
from utils.time_parser import parse_user_time

logger = logging.getLogger(__name__)

async def _get_thread_history(user_id: str, thread_id: Optional[str] = None) -> str:
    try:
        from services.message_service import format_history_from_db
        from services.thread_service import get_active_thread
        if not thread_id:
            thread_id = await get_active_thread(user_id)
        if not thread_id:
            return ""
        return await format_history_from_db(thread_id, limit=8)
    except Exception:
        return ""

_ASK_PROMPT = """
You are a Google Calendar scheduling assistant collecting missing event details.

Conversation history (for resolving explicit references like "it" or "that meeting" ONLY):
{memory}

Current event data:
{event_data}

Fields still missing: {missing}

User just replied: "{user_reply}"

Tasks:
1. Extract any missing fields from the user's reply ONLY.
   - start_time: EXACT date/time phrase as user wrote it in this reply. Do NOT convert to ISO.
     Do NOT borrow time from history unless the user explicitly said "same time" or similar.
   - title: meaningful event name from THIS reply only. Do NOT infer from history unless user
     said "same as before" or used a pronoun like "it" that clearly refers to a prior event.
2. List extracted fields in "filled_fields".
3. If any fields still missing, write a natural follow-up question.
4. If ALL filled, set "next_question" to null.

Return ONLY this JSON:
{{
  "filled_fields": {{"start_time": "<phrase or null>", "title": "<title or null>"}},
  "next_question": "<question or null>"
}}
"""

async def ask_required(
    data: dict,
    user_reply: str,
    user_id: str = "",
    thread_id: Optional[str] = None,
) -> tuple[str | None, dict]:
    if not data:
        data = {}
    missing = list(data.get("missing_fields", []))
    if not missing:
        return None, data
    if not user_reply or not user_reply.strip():
        return _make_q(missing), data

    memory = await _get_thread_history(user_id, thread_id) if user_id else ""
    snap = {k: v for k, v in data.items() if k not in ("missing_fields", "attendees", "attendee_stage", "_thread_id", "raw_attendees")}
    prompt = _ASK_PROMPT.format(memory=memory or "(empty)", event_data=json.dumps(snap, default=str), missing=missing, user_reply=user_reply)

    llm = get_llm()
    result = {}
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = (getattr(resp, "content", "") or "").strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        try:
            result = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, flags=re.S)
            result = json.loads(m.group()) if m else {}
    except Exception as e:
        logger.error("[AskMissing] LLM error: %s", e)
        return _make_q(missing), data

    filled = result.get("filled_fields") or {}
    if not isinstance(filled, dict):
        filled = {}

    if "start_time" in missing:
        rt = filled.get("start_time")
        if rt and str(rt).strip().lower() not in ("null", "none", ""):
            parsed = parse_user_time(str(rt).strip())
            if parsed["success"]:
                data["start_time"] = parsed["datetime"]

    if "title" in missing:
        tv = filled.get("title")
        if tv and str(tv).strip().lower() not in ("null", "none", ""):
            t = str(tv).strip()
            data["title"] = t
            data["purpose"] = data.get("purpose") or t

    still = []
    if not data.get("start_time"):
        still.append("start_time")
    if not (data.get("title") or data.get("purpose")):
        still.append("title")
    data["missing_fields"] = still

    if not still:
        return None, data

    q = result.get("next_question")
    if not q or str(q).strip().lower() in ("null", "none", ""):
        q = _make_q(still)
    return str(q).strip(), data

def _make_q(missing: list[str]) -> str:
    if "start_time" in missing and "title" in missing:
        return "What's the event about and when would you like to schedule it?"
    if "start_time" in missing:
        return "When would you like to schedule this event?"
    if "title" in missing:
        return "What is this event about?"
    return "Could you provide more details?"
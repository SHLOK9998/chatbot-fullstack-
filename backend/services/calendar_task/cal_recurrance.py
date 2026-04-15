"""
services/calendar_task/cal_recurrance.py
-----------------------------------------
Natural Language Recurrence → Google Calendar RRULE Converter.

Flow:
  1. Try LLM to interpret the recurrence phrase (main path)
  2. If LLM fails or returns invalid result, apply rule-based fallback
  3. If still unresolvable, return None (event treated as one-time)

Examples:
  "every Monday"        → "RRULE:FREQ=WEEKLY;BYDAY=MO"
  "daily"               → "RRULE:FREQ=DAILY"
  "every 2 weeks"       → "RRULE:FREQ=WEEKLY;INTERVAL=2"
  "every weekday"       → "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
  "first Monday monthly"→ "RRULE:FREQ=MONTHLY;BYDAY=1MO"
"""

import logging
from langchain_core.messages import HumanMessage
from core.dependencies import get_llm

logger = logging.getLogger(__name__)

# Rule-based fallback for ultra-common phrases
_FALLBACK_MAP = {
    "daily"        : "RRULE:FREQ=DAILY",
    "every day"    : "RRULE:FREQ=DAILY",
    "weekly"       : "RRULE:FREQ=WEEKLY",
    "every week"   : "RRULE:FREQ=WEEKLY",
    "monthly"      : "RRULE:FREQ=MONTHLY",
    "every month"  : "RRULE:FREQ=MONTHLY",
    "yearly"       : "RRULE:FREQ=YEARLY",
    "annually"     : "RRULE:FREQ=YEARLY",
    "every monday" : "RRULE:FREQ=WEEKLY;BYDAY=MO",
    "every tuesday": "RRULE:FREQ=WEEKLY;BYDAY=TU",
    "every wednesday":"RRULE:FREQ=WEEKLY;BYDAY=WE",
    "every thursday": "RRULE:FREQ=WEEKLY;BYDAY=TH",
    "every friday" : "RRULE:FREQ=WEEKLY;BYDAY=FR",
    "every saturday": "RRULE:FREQ=WEEKLY;BYDAY=SA",
    "every sunday" : "RRULE:FREQ=WEEKLY;BYDAY=SU",
    "every weekday": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekdays"     : "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "every weekend": "RRULE:FREQ=WEEKLY;BYDAY=SA,SU",
}

_LLM_PROMPT = """You are a recurrence pattern converter for Google Calendar.

Convert the natural language recurrence description below into a valid Google Calendar RRULE.

Examples:
  every Monday           → RRULE:FREQ=WEEKLY;BYDAY=MO
  daily                  → RRULE:FREQ=DAILY
  every 2 weeks          → RRULE:FREQ=WEEKLY;INTERVAL=2
  every weekday          → RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
  monthly                → RRULE:FREQ=MONTHLY
  every year             → RRULE:FREQ=YEARLY
  first Monday of month  → RRULE:FREQ=MONTHLY;BYDAY=1MO
  last Friday of month   → RRULE:FREQ=MONTHLY;BYDAY=-1FR

Rules:
  - Reply ONLY with the RRULE string starting exactly with "RRULE:"
  - No extra text, no markdown, no explanation.
  - If you cannot determine the correct RRULE, reply exactly: NOT_FOUND

Recurrence: {text}
"""


async def convert_recurrence(recurrence_str: str | None) -> str | None:
    """
    Convert a natural-language recurrence phrase to a Google Calendar RRULE.

    Args:
        recurrence_str: e.g. "every Monday", "daily", "every 2 weeks"

    Returns:
        RRULE string (e.g. "RRULE:FREQ=WEEKLY;BYDAY=MO") or None
    """
    if not recurrence_str:
        return None

    text = recurrence_str.strip().lower()
    logger.info("[CalRecurrence] Converting: '%s'", text)

    # 1. Rule-based fast path — no LLM needed for common phrases
    for key, rule in _FALLBACK_MAP.items():
        if key in text:
            logger.info("[CalRecurrence] Fallback match '%s' → %s", key, rule)
            return rule

    # 2. Try LLM for less common patterns
    rrule = await _llm_convert(text)
    if rrule:
        logger.info("[CalRecurrence] LLM result: %s", rrule)
        return rrule

    logger.warning("[CalRecurrence] Could not convert recurrence: '%s'", recurrence_str)
    return None


async def _llm_convert(text: str) -> str | None:
    """Ask LLM to produce a valid RRULE string (non-blocking)."""
    import asyncio
    try:
        llm = get_llm()
        prompt = _LLM_PROMPT.format(text=text)
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        result = (getattr(resp, "content", "") or "").strip()
        if result.startswith("RRULE:"):
            return result
        logger.debug("[CalRecurrence] LLM returned non-RRULE: '%s'", result)
    except Exception as e:
        logger.error("[CalRecurrence] LLM error: %s", e)
    return None
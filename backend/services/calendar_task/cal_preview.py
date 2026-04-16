"""
services/calendar_task/cal_preview.py
---------------------------------------
Calendar Event Preview — Single Event.

Responsibilities:
  build_preview(data)            → formatted preview string shown to the user
  detect_user_choice(user_reply) → classifies reply as: confirm / modify / cancel
"""

import json
import logging
import re
import asyncio
from datetime import datetime

from langchain_core.messages import HumanMessage
from core.dependencies import get_llm

logger = logging.getLogger(__name__)


# ── Preview builder ───────────────────────────────────────────────────────────

def build_preview(data: dict) -> str:
    """
    Build a clean, readable preview card for a single calendar event.
    Prompts user to Confirm / Modify / Cancel.
    """
    title       = data.get("title") or data.get("purpose") or "(untitled)"
    start_time  = data.get("start_time") or ""
    location    = data.get("location") or "—"
    description = data.get("description") or "—"
    recurrence  = data.get("recurrence") or "None (one-time event)"

    # Format start_time from ISO to human-readable
    start_display = _format_datetime(start_time)

    # Format attendees as a simple comma-separated string for the table cell
    attendees_list = data.get("attendees") or []
    if attendees_list:
        parts = []
        for a in attendees_list:
            name  = (a.get("name") or "").strip()
            email = (a.get("email") or "").strip()
            if name and email:
                parts.append(f"{name} ({email})")
            elif email:
                parts.append(email)
            elif name:
                parts.append(name)
        attendees_cell = ", ".join(parts) if parts else "None"
    else:
        attendees_cell = "None"

    preview = (
        f"### Calendar Event Preview\n\n"
        f"| Field | Details |\n"
        f"|---|---|\n"
        f"| **Title** | {title} |\n"
        f"| **When** | {start_display} |\n"
        f"| **Location** | {location} |\n"
        f"| **Description** | {description} |\n"
        f"| **Recurrence** | {recurrence} |\n"
        f"| **Attendees** | {attendees_cell} |\n\n"
        f"---\n\n"
        f"**What would you like to do?**\n\n"
        f"- **Confirm** — create the event\n"
        f"- **Modify** — change time, title, location, attendees, etc.\n"
        f"- **Cancel** — discard this event"
    )

    logger.info("[CalPreview] Preview built for event='%s'", title)
    return preview


def _format_datetime(iso_str: str) -> str:
    """Convert ISO datetime to a friendly display string."""
    if not iso_str:
        return "(no time set)"
    try:
        dt = datetime.fromisoformat(str(iso_str))
        return dt.strftime("%A, %d %B %Y at %I:%M %p")
    except Exception:
        return str(iso_str)


# ── Action detector ───────────────────────────────────────────────────────────

_DETECT_PROMPT = """
You are a calendar scheduling assistant.
The user just saw a calendar event preview and replied.

User reply: "{reply}"

Classify the user's intent:
- "confirm" → user wants to create/save the event
- "modify"  → user wants to change something
- "cancel"  → user wants to discard the event

If action is "modify", restate the user's instruction clearly and concisely.

Return ONLY this JSON:
{{
  "action"     : "<confirm|modify|cancel>",
  "instruction": "<modification instruction or empty string>"
}}
"""


async def detect_user_choice(user_reply: str) -> tuple[str, str]:
    """
    Detect what the user wants to do after seeing the event preview.

    Args:
        user_reply: the user's reply to the preview

    Returns:
        (action, instruction)
        action      : "confirm" | "modify" | "cancel"
        instruction : modification instruction text (if action == "modify"), else ""
    """
    prompt = _DETECT_PROMPT.format(reply=user_reply)
    llm = get_llm()

    try:
        resp = await asyncio.to_thread(llm.invoke, prompt)
        raw = (getattr(resp, "content", "") or "").strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")

        result = {}
        try:
            result = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, flags=re.S)
            if m:
                result = json.loads(m.group())

        action = str(result.get("action") or "modify").strip().lower()
        if action not in ("confirm", "modify", "cancel"):
            action = "modify"

        instruction = str(result.get("instruction") or user_reply).strip()

        logger.info("[CalPreview] Detected action=%s | instruction='%s'", action, instruction[:60])
        return action, instruction

    except Exception as e:
        logger.error("[CalPreview] detect_user_choice failed: %s", e)
        # Default to modify with the raw reply as the instruction
        return "modify", user_reply.strip()

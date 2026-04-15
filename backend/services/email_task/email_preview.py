# services/email_task/email_preview_dynamic.py
"""
Dynamic Email preview using LLM-based intent detection.

Renders a readable preview card of the drafted email and detects
the user's intended action (Send / Modify / Cancel) using natural language understanding.
"""

import logging
import asyncio
from typing import Tuple

from core.dependencies import get_llm  # your LLM wrapper

logger = logging.getLogger(__name__)
llm = get_llm()


def build_preview(data: dict) -> str:
    """
    Build a formatted preview string to show the user.

    Returns a multi-line string with all email fields clearly laid out,
    followed by the action prompt.
    """
    emails = data.get("to_email") or []
    if isinstance(emails, str):
        emails = [emails] if emails else []
    to_email = ", ".join(emails)

    to_name  = data.get("to_name") or ""
    cc       = ", ".join(data.get("cc") or []) or "—"
    bcc      = ", ".join(data.get("bcc") or []) or "—"
    subject  = data.get("subject") or "(no subject)"
    body     = data.get("body") or "(no body)"

    # Only show "Name <email>" format for exactly one recipient with a simple name.
    # For groups or multiple recipients show just the email list to avoid
    # confusing output like "all interns <hr1@c.com, hr2@c.com>".
    if len(emails) == 1 and to_name and "@" not in to_name and len(to_name.split()) <= 3:
        recipient = f"{to_name} <{to_email}>"
    else:
        recipient = to_email or "—"

    preview = (
        f" **Email Preview**\n"
        f"{'─' * 40}\n"
        f"**To      :** {recipient}\n"
        f"**CC      :** {cc}\n"
        f"**BCC     :** {bcc}\n"
        f"**Subject :** {subject}\n"
        f"{'─' * 40}\n"
        f"{body}\n"
        f"{'─' * 40}\n\n"
        f"What would you like to do?\n"
        f"  **Send** — confirm sending the email\n"
        f"  **Modify** — describe what to change (e.g. 'make it shorter', 'change tone to casual')\n"
        f"  **Cancel** — cancel sending the email"
    )

    logger.info("[EmailPreview] Preview built for recipient='%s'", to_email)
    return preview

async def detect_user_choice_llm(user_reply: str) -> tuple[str, str]:
    """
    Detect user's choice after preview — send / cancel / modify.

    Returns:
        action      — "send", "cancel", or "modify"
        instruction — if modify, the user's modification text; else ""
    """
    prompt = f"""
You are an intelligent email assistant.
Decide what the user wants to do after seeing their email preview.

User reply: "{user_reply}"

Classify the intent:
- If they clearly approve or want to send → respond: send
- If they want to cancel → respond: cancel
- If they request any change (tone, content, subject, etc.) → respond: modify

When responding, use this format strictly:
ACTION: <send|cancel|modify>
INSTRUCTION: <if modify, restate the user's instruction clearly; else empty>
"""

    try:
        # Run LLM call safely in background
        resp = await asyncio.to_thread(llm.invoke, prompt)
        content = getattr(resp, "content", str(resp)).strip()
        logger.debug("[EmailPreview] LLM raw response: %s", content)

        # Try to parse structured output
        import re
        match = re.search(r"ACTION:\s*(\w+)", content, re.I)
        action = match.group(1).lower() if match else "modify"

        instr_match = re.search(r"INSTRUCTION:\s*(.+)", content, re.I | re.S)
        instruction = instr_match.group(1).strip() if instr_match else user_reply.strip()

        # Fallbacks
        if action not in ["send", "cancel", "modify"]:
            action = "modify"
        if action == "modify" and not instruction:
            instruction = user_reply.strip()

        return action, instruction

    except Exception as e:
        logger.error("[EmailPreview] detect_user_choice_llm failed: %s", e)
        return "modify", user_reply.strip()
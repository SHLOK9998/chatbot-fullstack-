"""
services/email_task/email_content.py

WHAT FIXED vs. UPLOADED VERSION:
  1. generate_email_content() now accepts to_name: Optional[str] parameter.
  2. Body prompt now has an explicit, unambiguous greeting rule:
       - If to_name is provided → use it ("Dear {to_name}" or "Hi {to_name}")
       - If NOT provided → use a generic greeting ("Dear Team", "Hi there")
       - NEVER invent a name that was not explicitly provided
     Old rule was "if a name is available in history, use it" — the LLM would
     hallucinate a name when none was found in history (random "Dear Mr. Johnson").
  3. All other logic, prompts, and formatting rules are UNCHANGED.
"""

import re
import logging
import asyncio
from typing import Optional, Tuple

from core.dependencies import get_llm
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)
llm = get_llm()


async def _get_history_text(user_id: str, thread_id: Optional[str] = None) -> str:
    """Load recent conversation from MongoDB thread (replaces LangChain session memory)."""
    try:
        from services.message_service import format_history_from_db
        from services.thread_service import get_active_thread
        if not thread_id:
            thread_id = await get_active_thread(user_id)
        if not thread_id:
            return ""
        return await format_history_from_db(thread_id, limit=20)
    except Exception as e:
        logger.warning("[EmailContent] Could not load history from MongoDB: %s", e)
        return ""


async def generate_email_content(
    purpose: str,
    tone: str = "neutral",
    location: Optional[str] = None,
    context: str = "",
    recipient_count: int = 1,
    user_id: str = "default",
    thread_id: Optional[str] = None,
    to_name: Optional[str] = None,       # ← NEW: explicit recipient name
) -> Tuple[str, str]:
    """
    Generate an email subject and body using the LLM.

    Args:
        to_name: recipient name to use in the greeting (e.g. "Shlok", "HR Team").
                 If None, a generic greeting is used. NEVER invented by LLM.

    Returns: (subject, body)
    """
    try:
        history_text = await _get_history_text(user_id, thread_id)

        # ── Build greeting instruction ────────────────────────────────────────
        # Explicit rule prevents LLM from hallucinating a name.
        if to_name and to_name.strip():
            greeting_rule = (
                f'- Greeting: Address the email to "{to_name.strip()}". '
                f'Use their name naturally (e.g. "Dear {to_name.strip()}," or "Hi {to_name.strip()},").'
            )
        else:
            greeting_rule = (
                "- Greeting: Use a generic greeting such as 'Dear Team,' or 'Hi there,' "
                "— do NOT invent or guess any recipient name."
            )

        # ── BODY PROMPT ───────────────────────────────────────────────────────
        body_prompt = f"""
You are an AI email composition assistant.

Conversation history (for reference):
{history_text}

Task:
Write a {tone} email for the following purpose: "{purpose}"

Rules:
{greeting_rule}
- Keep the content directly relevant to the purpose.
- Do NOT invent unrelated names, roles, or extra storylines.
- Include a greeting, main message, and a polite closing.
- Write naturally in 2–3 short paragraphs.
- Avoid placeholders (like [name], [date], [location]).
- Mention the location only if explicitly provided: {location if location else "None"}.
- The email will be sent to {recipient_count} recipient(s); use an appropriate greeting.
- If the purpose implies appreciation, request, or notice — reflect that tone clearly.
- Do not include the subject line here.
- Be concise, factual, and consistent with user intent.
- Do not make table or plan or timetable unless the user explicitly asked.
- Formatting: Use strictly GitHub Flavored Markdown.
- Tables: If the content includes schedules, lists, or structured data, use a Markdown table.
- Structure: Ensure exactly one blank line before and after any table.

Context from user or prior chat (for guidance):
{context}
"""

        logger.info("[EmailContent] Generating email body...")
        body_response = await asyncio.to_thread(llm.invoke, body_prompt)
        body_text = getattr(body_response, "content", str(body_response)).strip()

        # Clean up unwanted model patterns
        body_text = re.sub(r"(?i)^subject\s*:\s*", "", body_text)
        body_text = re.sub(r"```(?:text|markdown)?", "", body_text).strip("`\n ")

        # ── SUBJECT PROMPT ────────────────────────────────────────────────────
        subject_prompt = f"""
Generate a short, relevant subject line (max 8 words) for the email below.

Rules:
- The subject must clearly reflect the purpose: "{purpose}"
- Do NOT invent any fictional names or positions.
- Avoid generic phrases like 'Important Update' — be specific but concise.
- Do NOT include words like 'Subject:' or punctuation at the end.

Email body:
{body_text}
"""
        logger.info("[EmailContent] Generating subject line...")
        subject_response = await asyncio.to_thread(llm.invoke, subject_prompt)
        subject_text = getattr(subject_response, "content", str(subject_response)).strip()
        subject_text = re.sub(r"(?i)\bsubject\s*:\s*", "", subject_text).strip()

        # Fallbacks
        if not subject_text:
            subject_text = purpose if purpose else "Email Update"
        if not body_text:
            body_text = (
                f"Dear {to_name.strip()},\n\n" if to_name
                else "Dear Team,\n\n"
            ) + f"This email is regarding {purpose}.\n\nBest regards,"

        logger.info("[EmailContent] Email generation completed.")
        return subject_text.strip(), body_text.strip()

    except Exception as e:
        logger.exception("[EmailContent] Error generating email content")
        return "Email Update", "Sorry, I couldn't generate the email content."
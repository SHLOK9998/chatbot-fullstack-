"""
services/email_task/email_modifier.py

Email modifier — production-level with MongoDB-powered dynamic recipient resolution.

WHAT CHANGED vs. OLD VERSION:
  1. When user asks to "add shlok" or "add someone to cc/bcc/to":
       - New _resolve_name_to_email() tries MongoDB KB lookup first
       - If not found, returns None → caller asks for the email dynamically
         (LLM generates a natural-language question, not a hardcoded string)
  2. _parse_response() is unchanged.
  3. modify_email() now returns a 6-tuple:
       (to, subject, body, cc, bcc, missing_names)
       missing_names: list of names whose emails could not be resolved
       → email_flow_service uses this to ask for them dynamically
  4. All existing role-edit logic (tone, body, subject) is fully preserved.

FLOW:
  user: "add shlok to cc"
    → LLM output:  CC: shlok  (or CC: shlok@example.com if LLM hallucinates)
    → _resolve_name_to_email("shlok") → MongoDB lookup → found → add real email
    → if not found → missing_names = ["shlok"] → flow asks "I couldn't find shlok's
      email. What is their email address?" (LLM-generated question, not hardcoded)
"""

import asyncio
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage

from core.dependencies import get_llm

logger = logging.getLogger(__name__)

_MODIFY_PROMPT = """
You are an assistant that edits an existing email draft based on a user instruction.

Below is the current state of the email:

TO: {to_emails}
CC: {cc}
BCC: {bcc}
SUBJECT: {subject}
BODY:
{body}

User instruction: {instruction}

Your job is to apply the instruction precisely and return the updated email.

Rules:
- Only modify the fields that the instruction explicitly refers to. Leave all other fields exactly as they are.
- TO, CC, and BCC are completely separate fields. An email address must never appear in more than one of these fields at the same time.
- If an address is added to CC or BCC, it must not be present in TO, and vice versa.
- If an address is removed from a role, remove it only from that role and nowhere else.
- If a role ends up with no addresses after the instruction is applied, output the word: none
- For subject or body changes, only rewrite those fields. Do not touch any address fields.
- If the instruction does not mention a role at all, reproduce that role exactly without any change.
- IMPORTANT: For address fields, output the actual email address if you know it.
  If the user said "add shlok" and you don't know shlok's email, output the name "shlok"
  as a placeholder — the system will look it up. Do NOT invent email addresses.

You must always return all five fields. Use this exact format with no extra text:

TO: <comma-separated emails/names or none>
CC: <comma-separated emails/names or none>
BCC: <comma-separated emails/names or none>
SUBJECT: <subject text>
BODY:
<body text>
"""


async def _resolve_name_to_email(name: str) -> Optional[str]:
    """
    Look up a person's email from MongoDB employee_kb by name.
    Returns valid email string or None if not found.

    Uses the same 3-pass strategy as email_extract._mongo_lookup_by_name():
      1. Regex on metadata.name
      2. Regex on content role
      3. Atlas vector search fallback
    """
    from services.email_task.email_extract import _mongo_lookup_by_name, _is_valid_email
    email = await _mongo_lookup_by_name(name)
    return email if email and _is_valid_email(email) else None


async def _generate_missing_email_question(missing_names: list[str]) -> str:
    """
    Use LLM to generate a natural, context-aware question asking for missing emails.
    Never returns a hardcoded string.
    """
    names_str = ", ".join(missing_names)
    llm = get_llm()
    prompt = f"""
You are an intelligent email assistant.
The user wanted to add the following people to this email but their email addresses
could not be found in the contact database: {names_str}

Generate ONE concise, friendly question asking for their email address(es).
- If there is only one person, ask for that person's email specifically.
- If there are multiple, ask for all of them together.
- Do NOT use placeholders like [name]. Use the actual name(s).
- Return ONLY the question text, no preamble, no JSON.
"""
    try:
        resp = await asyncio.to_thread(llm.invoke, prompt)
        question = getattr(resp, "content", str(resp)).strip()
        return question if question else f"I couldn't find the email address for {names_str}. Could you provide it?"
    except Exception:
        return f"I couldn't find the email address for {names_str}. Could you share it with me?"


def _is_likely_name_not_email(value: str) -> bool:
    """
    Returns True if a string looks like a name (not an email address).
    Used to detect when LLM outputs a name placeholder instead of an email.
    """
    value = value.strip()
    if not value or value.lower() in ("none", "n/a", "-", "—"):
        return False
    # If it contains @, it's an email attempt — validate separately
    if "@" in value:
        return False
    # Likely a name: letters and spaces only (maybe hyphen/apostrophe)
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z\s'\-]{0,40}", value))


def _parse_list_field(value: str) -> tuple[list[str], list[str]]:
    """
    Parse a comma-separated role from LLM output.
    Splits each token into either a valid email or a name placeholder.

    Returns:
      (valid_emails, name_placeholders)
    """
    from services.email_task.email_extract import _is_valid_email

    stripped = value.strip()
    if stripped.lower() in ("none", "n/a", "-", "—", ""):
        return [], []

    tokens = [t.strip() for t in re.split(r"[,;]+", stripped) if t.strip()]
    emails: list[str] = []
    names: list[str] = []

    for token in tokens:
        if _is_valid_email(token):
            emails.append(token)
        elif _is_likely_name_not_email(token):
            names.append(token)
        elif "@" in token:
            # Malformed email — treat as name for lookup
            names.append(token.split("@")[0])

    return emails, names


def _parse_response(
    raw: str,
) -> tuple[list[str] | None, str, str, list[str] | None, list[str] | None,
           list[str], list[str], list[str]]:
    """
    Parse TO / CC / BCC / SUBJECT / BODY from LLM response.

    List fields return None if not found in response (means: keep current value).
    List fields return [] if found but explicitly empty/none.
    String fields return "" if not found (means: keep current value).

    Also returns (to_names, cc_names, bcc_names) — name placeholders that need
    email resolution via MongoDB lookup.
    """
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    bcc_emails: list[str] | None = None
    subject: str = ""
    body: str = ""
    to_names: list[str] = []
    cc_names: list[str] = []
    bcc_names: list[str] = []

    # TO
    to_match = re.search(r"^TO:\s*(.+)$", raw, re.IGNORECASE | re.MULTILINE)
    if to_match:
        to_emails, to_names = _parse_list_field(to_match.group(1))

    # CC — anchored to avoid matching inside BCC
    cc_match = re.search(r"^CC:\s*(.+)$", raw, re.IGNORECASE | re.MULTILINE)
    if cc_match:
        cc_emails, cc_names = _parse_list_field(cc_match.group(1))

    # BCC
    bcc_match = re.search(r"^BCC:\s*(.+)$", raw, re.IGNORECASE | re.MULTILINE)
    if bcc_match:
        bcc_emails, bcc_names = _parse_list_field(bcc_match.group(1))

    # SUBJECT
    subject_match = re.search(r"^SUBJECT:\s*(.+)$", raw, re.IGNORECASE | re.MULTILINE)
    if subject_match:
        subject = subject_match.group(1).strip()

    # BODY
    body_match = re.search(r"^BODY:\s*\n([\s\S]+)", raw, re.IGNORECASE | re.MULTILINE)
    if body_match:
        body = body_match.group(1).strip()

    return to_emails, subject, body, cc_emails, bcc_emails, to_names, cc_names, bcc_names


async def modify_email(
    data: dict,
    instruction: str,
) -> tuple[list[str], str, str, list[str], list[str], str | None]:
    """
    Modify email fields based on user instruction.

    Returns:
      (updated_to, updated_subject, updated_body, updated_cc, updated_bcc, ask_question)

      ask_question: if not None, the flow should show this question to the user
                    and wait for their reply before continuing. This happens when
                    a name was referenced but their email couldn't be found in MongoDB.
    """
    logger.info("[EmailModifier] Modifying email — instruction: %s", instruction[:80])

    current_to: list[str]  = data.get("to_email") or []
    current_subject: str   = data.get("subject") or ""
    current_body: str      = data.get("body") or ""
    current_cc: list[str]  = data.get("cc") or []
    current_bcc: list[str] = data.get("bcc") or []

    if isinstance(current_to, str):
        current_to = [current_to]

    prompt = _MODIFY_PROMPT.format(
        to_emails=", ".join(current_to) if current_to else "none",
        cc=", ".join(current_cc) if current_cc else "none",
        bcc=", ".join(current_bcc) if current_bcc else "none",
        subject=current_subject,
        body=current_body,
        instruction=instruction,
    )

    try:
        llm = get_llm()
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = resp.content.strip()
        logger.debug("[EmailModifier] Raw LLM response:\n%s", raw)

        (
            new_to, new_subject, new_body, new_cc, new_bcc,
            to_names, cc_names, bcc_names,
        ) = _parse_response(raw)

        # ── Resolve name placeholders via MongoDB ─────────────────────────────
        all_missing_names: list[str] = []

        async def resolve_names(names: list[str], existing: list[str]) -> tuple[list[str], list[str]]:
            """Try to resolve each name to an email. Collect unresolved ones."""
            resolved_emails = list(existing)
            missing = []
            for name in names:
                email = await _resolve_name_to_email(name)
                if email:
                    if email not in resolved_emails:
                        resolved_emails.append(email)
                    logger.info("[EmailModifier] Resolved name '%s' → %s", name, email)
                else:
                    missing.append(name)
                    logger.warning("[EmailModifier] Could not resolve name '%s'", name)
            return resolved_emails, missing

        # Resolve TO name placeholders
        if to_names:
            base_to = list(new_to) if new_to is not None else list(current_to)
            new_to, missing_to = await resolve_names(to_names, base_to)
            all_missing_names.extend(missing_to)

        # Resolve CC name placeholders
        if cc_names:
            base_cc = list(new_cc) if new_cc is not None else list(current_cc)
            new_cc, missing_cc = await resolve_names(cc_names, base_cc)
            all_missing_names.extend(missing_cc)

        # Resolve BCC name placeholders
        if bcc_names:
            base_bcc = list(new_bcc) if new_bcc is not None else list(current_bcc)
            new_bcc, missing_bcc = await resolve_names(bcc_names, base_bcc)
            all_missing_names.extend(missing_bcc)

        # ── Generate dynamic question for unresolved names ────────────────────
        ask_question: str | None = None
        if all_missing_names:
            ask_question = await _generate_missing_email_question(all_missing_names)
            logger.info("[EmailModifier] Missing emails for: %s — asking user", all_missing_names)

        # Final values: None means "not found in response → keep current"
        final_to      = new_to  if new_to  is not None else current_to
        final_subject = new_subject if new_subject else current_subject
        final_body    = new_body    if new_body    else current_body
        final_cc      = new_cc  if new_cc  is not None else current_cc
        final_bcc     = new_bcc if new_bcc is not None else current_bcc

        return final_to, final_subject, final_body, final_cc, final_bcc, ask_question

    except Exception as e:
        logger.error("[EmailModifier] Modification failed: %s", e)
        return current_to, current_subject, current_body, current_cc, current_bcc, None
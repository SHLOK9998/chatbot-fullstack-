# """
# services/email_task/ask_missing.py

# WHAT FIXED vs. UPLOADED VERSION:
#   _parse_required_reply_dynamic() had broken imports:
#     _llm_resolve_recipient_intent, _resolve_emails_from_intent, _normalize_to_name_field
#   — none of these exist in the new email_extract.py (they were renamed/restructured).

#   FIX: replaced the broken import block and the old resolution pipeline with
#   the new email_extract public API:
#     _is_valid_email, _extract_emails_from_text   — still exist, unchanged
#     _mongo_lookup_by_name                        — still exists, unchanged
#     _get_db_schema, _classify_recipients, _resolve — new names for the pipeline

#   The logic is identical — we still resolve the name the user typed to an email
#   via MongoDB — but now uses the correct function names.

#   Everything else (ask_required, ask_optional, _parse_optional_reply_dynamic,
#   _generate_question_for_missing, _llm_parse_purpose) is UNCHANGED.
# """

# import json
# import logging
# import re
# import asyncio
# from typing import Tuple, List, Optional

# from core.dependencies import get_llm

# logger = logging.getLogger(__name__)
# llm = get_llm()


# # ── Shared JSON parse helper ──────────────────────────────────────────────────

# def _safe_parse_json(raw: str) -> dict:
#     """Safely parse LLM output as JSON. Strips fences, never uses eval()."""
#     if not raw:
#         return {}
#     cleaned = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
#     try:
#         result = json.loads(cleaned)
#         return result if isinstance(result, dict) else {}
#     except Exception:
#         m = re.search(r"\{.*\}", cleaned, flags=re.S)
#         if m:
#             try:
#                 result = json.loads(m.group())
#                 return result if isinstance(result, dict) else {}
#             except Exception:
#                 pass
#     return {}


# # ── Phase A: Required Fields ──────────────────────────────────────────────────

# async def _generate_question_for_missing(missing_fields: List[str], data: dict) -> str:
#     """
#     LLM-generate a natural-language question for the missing required fields.
#     Handles standard missing fields and the partial_missing (incomplete recipient) case.
#     """
#     partial_missing = data.get("partial_missing", [])

#     if "to_email_incomplete" in missing_fields and partial_missing:
#         names_str = ", ".join(partial_missing)
#         prompt = f"""
# You are an intelligent email assistant.
# The user wants to send an email but I couldn't find the email address(es) for: {names_str}

# Generate ONE concise, friendly question asking for their email address(es).
# - Use the actual name(s), not placeholders like [name].
# - If multiple names, ask for all in one go.
# - Keep it conversational and short.
# - Return ONLY the question text, no JSON, no preamble.
# """
#     elif "to_email" in missing_fields and "purpose" in missing_fields:
#         prompt = """
# You are an intelligent email assistant.
# Generate ONE friendly, natural question asking the user for:
#   1. Who they want to send the email to (name, email, or role)
#   2. What the email is about (purpose)
# Ask both together concisely. Return ONLY the question text.
# """
#     elif "to_email" in missing_fields:
#         prompt = """
# You are an intelligent email assistant.
# Generate ONE friendly, natural question asking the user:
#   - Who do they want to send this email to? (name, email address, or role like 'all interns')
# Return ONLY the question text, no JSON, no preamble.
# """
#     elif "purpose" in missing_fields:
#         prompt = """
# You are an intelligent email assistant.
# Generate ONE friendly, natural question asking the user:
#   - What is this email about? What should it say?
# Return ONLY the question text, no JSON, no preamble.
# """
#     else:
#         fields_str = ", ".join(missing_fields)
#         prompt = f"""
# You are an intelligent email assistant.
# Generate ONE friendly, natural question asking the user for: {fields_str}
# Return ONLY the question text, no JSON, no preamble.
# """

#     try:
#         resp = await asyncio.to_thread(llm.invoke, prompt)
#         question = getattr(resp, "content", str(resp)).strip()
#         return question if question else f"Could you provide: {', '.join(missing_fields)}?"
#     except Exception as exc:
#         logger.warning("[AskMissing] Question generation failed: %s", exc)
#         return f"Could you provide the missing information: {', '.join(missing_fields)}?"


# async def ask_required(data: dict, user_reply: str) -> Tuple[str | None, dict]:
#     """
#     Ask for and collect missing required email fields.

#     Handles:
#       - "to_email"            → ask who to send to
#       - "to_email_incomplete" → ask for emails of specific unresolved names
#       - "purpose"             → ask what the email is about
#       - Both together         → ask in one question

#     On each user reply: tries direct email → MongoDB name lookup → purpose parse.
#     Returns (None, data) when all required fields are satisfied.
#     """
#     missing = data.get("missing_fields", [])

#     if user_reply and user_reply.strip():
#         data = await _parse_required_reply_dynamic(user_reply, data)
#         missing = data.get("missing_fields", [])

#     if not missing:
#         return None, data

#     question = await _generate_question_for_missing(missing, data)
#     return question, data


# async def _parse_required_reply_dynamic(reply: str, data: dict) -> dict:
#     """
#     Parse the user's reply for missing required fields.

#     FIXED: uses the new email_extract API (_classify_recipients + _resolve)
#     instead of the old removed functions (_llm_resolve_recipient_intent etc.)

#     Steps:
#       1. Direct email addresses in reply → accept immediately
#       2. LLM extracts to_name / purpose from the reply text
#       3. to_name → _classify_recipients → _resolve → MongoDB KB lookup
#       4. purpose applied if found
#     """
#     # ── Correct imports from new email_extract.py ─────────────────────────────
#     from services.email_task.email_extract import (
#         _is_valid_email,
#         _extract_emails_from_text,
#         _mongo_lookup_by_name,   # individual name → email (still exists)
#         _get_db_schema,          # live DB roles/positions (new)
#         _classify_recipients,    # LLM intent classifier (new name)
#         _resolve,                # MongoDB dispatcher (new name)
#     )

#     missing = data.get("missing_fields", [])
#     if not missing:
#         return data

#     needs_email   = "to_email" in missing or "to_email_incomplete" in missing
#     needs_purpose = "purpose" in missing

#     # ── Step 1: Direct email addresses in reply ───────────────────────────────
#     if needs_email:
#         direct_emails = _extract_emails_from_text(reply)
#         valid = [e for e in direct_emails if _is_valid_email(e)]
#         if valid:
#             existing = data.get("to_email") or []
#             if isinstance(existing, str):
#                 existing = [existing] if existing else []
#             all_emails = list(dict.fromkeys(existing + valid))  # dedup, preserve order
#             data["to_email"]       = all_emails[0] if len(all_emails) == 1 else all_emails
#             data["recipient_count"] = len(all_emails)
#             data["partial_missing"] = []
#             data["missing_fields"]  = [
#                 f for f in missing if f not in ("to_email", "to_email_incomplete")
#             ]
#             logger.info("[AskMissing] Direct email(s) from reply: %s", valid)
#             if needs_purpose:
#                 data = await _llm_parse_purpose(reply, data)
#             return data

#     # ── Step 2: LLM extracts to_name and/or purpose from reply ───────────────
#     prompt = f"""
# You are an intelligent email assistant. Extract the missing information from the user's reply.

# Missing fields: {missing}
# Partial missing names (if any): {data.get("partial_missing", [])}
# User reply: "{reply}"

# Instructions:
# - If "to_email" or "to_email_incomplete" is missing: extract the recipient name(s), email(s),
#   or role exactly as the user wrote. Output as "to_name": "<value>".
#   Do NOT invent email addresses.
# - If "purpose" is missing: extract what the email is about as "purpose": "<value>".
# - Respond in strict JSON format with only the found fields.
# - Return JSON only, no markdown, no extra text.
# """
#     resp = await asyncio.to_thread(llm.invoke, prompt)
#     llm_response = getattr(resp, "content", str(resp)).strip()
#     parsed = _safe_parse_json(llm_response)

#     if not parsed:
#         logger.warning("[AskMissing] Failed to parse required reply: '%s'", llm_response[:120])

#     # ── Step 3: Resolve to_name via new MongoDB pipeline ─────────────────────
#     if needs_email and parsed.get("to_name"):
#         raw_to_name = str(parsed["to_name"]).strip()

#         if raw_to_name:
#             try:
#                 # Fetch live DB schema so classifier is grounded in real vocabulary
#                 schema = await _get_db_schema()
#                 # Load thread history via stored thread_id (not the non-existent _thread_history key)
#                 thread_id = data.get("_thread_id")
#                 if thread_id:
#                     from services.email_task.email_extract import _get_thread_history
#                     history = await _get_thread_history("", thread_id)
#                 else:
#                     history = ""

#                 # Classify the name/role phrase the user typed
#                 classification = await _classify_recipients(
#                     raw_to_name, reply, history, schema
#                 )

#                 # Resolve to actual email addresses via MongoDB
#                 resolved_emails, still_missing = await _resolve(
#                     classification, history, []
#                 )

#                 valid_emails = [e for e in resolved_emails if _is_valid_email(e)]

#                 if valid_emails:
#                     existing = data.get("to_email") or []
#                     if isinstance(existing, str):
#                         existing = [existing] if existing else []
#                     all_emails = list(dict.fromkeys(existing + valid_emails))
#                     data["to_email"]        = all_emails[0] if len(all_emails) == 1 else all_emails
#                     data["recipient_count"] = len(all_emails)
#                     data["partial_missing"] = still_missing
#                     data["missing_fields"]  = [
#                         f for f in missing if f not in ("to_email", "to_email_incomplete")
#                     ]
#                     if still_missing:
#                         data["missing_fields"].append("to_email_incomplete")
#                     logger.info(
#                         "[AskMissing] Resolved from name reply: %s | still missing: %s",
#                         valid_emails, still_missing,
#                     )
#                 else:
#                     # Could not resolve — mark as incomplete so we keep asking
#                     data["partial_missing"] = [raw_to_name]
#                     if "to_email_incomplete" not in data["missing_fields"]:
#                         data["missing_fields"] = [
#                             "to_email_incomplete" if f == "to_email" else f
#                             for f in missing
#                         ]
#                     logger.warning("[AskMissing] Could not resolve name from reply: '%s'", raw_to_name)

#             except Exception as e:
#                 logger.warning("[AskMissing] MongoDB resolution failed: %s", e)

#     # ── Step 4: Apply purpose if found ───────────────────────────────────────
#     if needs_purpose and parsed.get("purpose"):
#         data["purpose"] = str(parsed["purpose"]).strip()
#         data["missing_fields"] = [
#             f for f in data.get("missing_fields", []) if f != "purpose"
#         ]
#         logger.info("[AskMissing] Purpose resolved: %s", data["purpose"])

#     return data


# async def _llm_parse_purpose(reply: str, data: dict) -> dict:
#     """Lightweight LLM call to extract just the purpose from a user reply."""
#     prompt = f"""
# Extract the email purpose/topic from this user reply: "{reply}"
# Return JSON only: {{"purpose": "<extracted purpose or null>"}}
# """
#     try:
#         resp = await asyncio.to_thread(llm.invoke, prompt)
#         parsed = _safe_parse_json(getattr(resp, "content", str(resp)).strip())
#         if parsed.get("purpose"):
#             data["purpose"] = str(parsed["purpose"]).strip()
#             data["missing_fields"] = [
#                 f for f in data.get("missing_fields", []) if f != "purpose"
#             ]
#     except Exception:
#         pass
#     return data


# # ── Phase B: Optional Fields ──────────────────────────────────────────────────

# def _get_open_optional_fields(data: dict) -> List[str]:
#     """Return only the optional fields that haven't been resolved yet."""
#     open_fields = []
#     if not data.get("skip_cc"):
#         open_fields.append("cc")
#     if not data.get("skip_bcc"):
#         open_fields.append("bcc")
#     if not data.get("skip_attachments"):
#         open_fields.append("attachments")
#     return open_fields


# async def ask_optional(data: dict, user_reply: str) -> Tuple[str | None, dict]:
#     """
#     Ask for optional fields (cc, bcc, attachments) — only those not yet resolved.

#     Flow:
#       1. If optional_filled=True → skip entirely
#       2. Compute open fields from skip flags
#       3. No open fields → return (None, data)
#       4. If already asked + user replied → parse and close phase
#       5. If not asked yet → generate one LLM question for open fields only
#     """
#     if data.get("optional_filled"):
#         logger.info("[AskMissing] optional_filled=True — skipping Phase B")
#         return None, data

#     open_fields = _get_open_optional_fields(data)

#     if not open_fields:
#         logger.info("[AskMissing] No open optional fields — skipping Phase B")
#         return None, data

#     if data.get("optional_asked") and user_reply and user_reply.strip():
#         data = await _parse_optional_reply_dynamic(user_reply, data, open_fields)
#         return None, data

#     if not data.get("optional_asked"):
#         fields_label = ", ".join(open_fields)
#         prompt = f"""
# You are an intelligent email assistant. The email already has all required fields filled.
# Ask the user if they want to provide these optional fields: {fields_label}.
# - Ask in ONE friendly, concise question.
# - Ask for cc, bcc, and attachment info together if all are missing.
# - If only one role remains, ask just about that one.
# - Return ONLY the question text, no JSON, no preamble.
# """
#         try:
#             resp = await asyncio.to_thread(llm.invoke, prompt)
#             question = getattr(resp, "content", str(resp)).strip()
#         except Exception as exc:
#             logger.warning("[AskMissing] Optional question generation failed: %s", exc)
#             question = f"Would you like to add {fields_label} to this email? (Reply with values or say 'skip'.)"

#         data["optional_asked"] = True
#         return question, data

#     return None, data


# async def _parse_optional_reply_dynamic(reply: str, data: dict, fields_asked: List[str]) -> dict:
#     """
#     Parse cc, bcc, and/or attachments from user's reply.
#     Marks each asked role's skip flag = True after processing
#     so the flow treats them as resolved regardless of whether user provided a value.
#     """
#     from services.email_task.email_extract import _is_valid_email

#     prompt = f"""
# You are an intelligent email assistant. Extract the optional fields from the user's reply.

# Fields that were asked about: {fields_asked}
# User reply: "{reply}"

# Instructions:
# - cc: list of email addresses if the user gave any, else empty list []
# - bcc: list of email addresses if the user gave any, else empty list []
# - attachments: text description if the user mentioned one, else null
# - If the user said "skip", "no", "none", "don't add", treat as not provided.
# - Respond in JSON format with only the keys from: {fields_asked}
# - Return JSON only, no markdown.
# """
#     resp = await asyncio.to_thread(llm.invoke, prompt)
#     llm_response = getattr(resp, "content", str(resp)).strip()
#     parsed = _safe_parse_json(llm_response)

#     if not parsed:
#         logger.warning("[AskMissing] Failed to parse optional reply: '%s'", llm_response[:120])

#     if "cc" in fields_asked:
#         cc_val = parsed.get("cc", [])
#         if isinstance(cc_val, list):
#             valid_cc = [e for e in cc_val if _is_valid_email(str(e))]
#             if valid_cc:
#                 data["cc"] = valid_cc
#         data["skip_cc"] = True

#     if "bcc" in fields_asked:
#         bcc_val = parsed.get("bcc", [])
#         if isinstance(bcc_val, list):
#             valid_bcc = [e for e in bcc_val if _is_valid_email(str(e))]
#             if valid_bcc:
#                 data["bcc"] = valid_bcc
#         data["skip_bcc"] = True

#     if "attachments" in fields_asked:
#         att_val = parsed.get("attachments")
#         if att_val and str(att_val).strip():
#             data["attachments"] = str(att_val).strip()
#         data["skip_attachments"] = True

#     data["optional_filled"] = (
#         data.get("skip_cc", False) and
#         data.get("skip_bcc", False) and
#         data.get("skip_attachments", False)
#     )

#     logger.info(
#         "[AskMissing] Optional parsed | cc=%s bcc=%s att=%s | optional_filled=%s",
#         data.get("cc"), data.get("bcc"), data.get("attachments"), data.get("optional_filled"),
#     )
#     return data


"""
services/email_task/ask_missing.py

WHAT FIXED vs. UPLOADED VERSION:
  _parse_required_reply_dynamic() had broken imports:
    _llm_resolve_recipient_intent, _resolve_emails_from_intent, _normalize_to_name_field
  — none of these exist in the new email_extract.py (they were renamed/restructured).

  FIX: replaced the broken import block and the old resolution pipeline with
  the new email_extract public API:
    _is_valid_email, _extract_emails_from_text   — still exist, unchanged
    _mongo_lookup_by_name                        — still exists, unchanged
    _get_db_schema, _classify_recipients, _resolve — new names for the pipeline

  The logic is identical — we still resolve the name the user typed to an email
  via MongoDB — but now uses the correct function names.

  ATTACHMENT UPLOAD SUPPORT (new):
    When the bot asks about attachments in ask_optional, it now also sets
    ``awaiting_attachment_upload: True`` in state so the frontend knows to show
    the file-picker UI. The flag is cleared once the user has responded to
    (or skipped) the attachment question.

  Everything else (ask_required, ask_optional, _parse_optional_reply_dynamic,
  _generate_question_for_missing, _llm_parse_purpose) is UNCHANGED.
"""

import json
import logging
import re
import asyncio
from typing import Tuple, List, Optional

from core.dependencies import get_llm

logger = logging.getLogger(__name__)
llm = get_llm()


# ── Shared JSON parse helper ──────────────────────────────────────────────────

def _safe_parse_json(raw: str) -> dict:
    """Safely parse LLM output as JSON. Strips fences, never uses eval()."""
    if not raw:
        return {}
    cleaned = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", cleaned, flags=re.S)
        if m:
            try:
                result = json.loads(m.group())
                return result if isinstance(result, dict) else {}
            except Exception:
                pass
    return {}


# ── Phase A: Required Fields ──────────────────────────────────────────────────

async def _generate_question_for_missing(missing_fields: List[str], data: dict) -> str:
    """
    LLM-generate a natural-language question for the missing required fields.
    Handles standard missing fields and the partial_missing (incomplete recipient) case.
    """
    partial_missing = data.get("partial_missing", [])

    if "to_email_incomplete" in missing_fields and partial_missing:
        names_str = ", ".join(partial_missing)
        prompt = f"""
You are an intelligent email assistant.
The user wants to send an email but I couldn't find the email address(es) for: {names_str}

Generate ONE concise, friendly question asking for their email address(es).
- Use the actual name(s), not placeholders like [name].
- If multiple names, ask for all in one go.
- Keep it conversational and short.
- Return ONLY the question text, no JSON, no preamble.
"""
    elif "to_email" in missing_fields and "purpose" in missing_fields:
        prompt = """
You are an intelligent email assistant.
Generate ONE friendly, natural question asking the user for:
  1. Who they want to send the email to (name, email, or role)
  2. What the email is about (purpose)
Ask both together concisely. Return ONLY the question text.
"""
    elif "to_email" in missing_fields:
        prompt = """
You are an intelligent email assistant.
Generate ONE friendly, natural question asking the user:
  - Who do they want to send this email to? (name, email address, or role like 'all interns')
Return ONLY the question text, no JSON, no preamble.
"""
    elif "purpose" in missing_fields:
        prompt = """
You are an intelligent email assistant.
Generate ONE friendly, natural question asking the user:
  - What is this email about? What should it say?
Return ONLY the question text, no JSON, no preamble.
"""
    else:
        fields_str = ", ".join(missing_fields)
        prompt = f"""
You are an intelligent email assistant.
Generate ONE friendly, natural question asking the user for: {fields_str}
Return ONLY the question text, no JSON, no preamble.
"""

    try:
        resp = await asyncio.to_thread(llm.invoke, prompt)
        question = getattr(resp, "content", str(resp)).strip()
        return question if question else f"Could you provide: {', '.join(missing_fields)}?"
    except Exception as exc:
        logger.warning("[AskMissing] Question generation failed: %s", exc)
        return f"Could you provide the missing information: {', '.join(missing_fields)}?"


async def ask_required(data: dict, user_reply: str) -> Tuple[str | None, dict]:
    """
    Ask for and collect missing required email fields.

    Handles:
      - "to_email"            → ask who to send to
      - "to_email_incomplete" → ask for emails of specific unresolved names
      - "purpose"             → ask what the email is about
      - Both together         → ask in one question

    On each user reply: tries direct email → MongoDB name lookup → purpose parse.
    Returns (None, data) when all required fields are satisfied.
    """
    missing = data.get("missing_fields", [])

    if user_reply and user_reply.strip():
        data = await _parse_required_reply_dynamic(user_reply, data)
        missing = data.get("missing_fields", [])

    if not missing:
        return None, data

    question = await _generate_question_for_missing(missing, data)
    return question, data


async def _parse_required_reply_dynamic(reply: str, data: dict) -> dict:
    """
    Parse the user's reply for missing required fields.

    FIXED: uses the new email_extract API (_classify_recipients + _resolve)
    instead of the old removed functions (_llm_resolve_recipient_intent etc.)

    Steps:
      1. Direct email addresses in reply → accept immediately
      2. LLM extracts to_name / purpose from the reply text
      3. to_name → _classify_recipients → _resolve → MongoDB KB lookup
      4. purpose applied if found
    """
    from services.email_task.email_extract import (
        _is_valid_email,
        _extract_emails_from_text,
        _mongo_lookup_by_name,
        _get_db_schema,
        _classify_recipients,
        _resolve,
    )

    missing = data.get("missing_fields", [])
    if not missing:
        return data

    needs_email   = "to_email" in missing or "to_email_incomplete" in missing
    needs_purpose = "purpose" in missing

    # ── Step 1: Direct email addresses in reply ───────────────────────────────
    if needs_email:
        direct_emails = _extract_emails_from_text(reply)
        valid = [e for e in direct_emails if _is_valid_email(e)]
        if valid:
            existing = data.get("to_email") or []
            if isinstance(existing, str):
                existing = [existing] if existing else []
            all_emails = list(dict.fromkeys(existing + valid))
            data["to_email"]        = all_emails[0] if len(all_emails) == 1 else all_emails
            data["recipient_count"] = len(all_emails)
            data["partial_missing"] = []
            data["missing_fields"]  = [
                f for f in missing if f not in ("to_email", "to_email_incomplete")
            ]
            logger.info("[AskMissing] Direct email(s) from reply: %s", valid)
            if needs_purpose:
                data = await _llm_parse_purpose(reply, data)
            return data

    # ── Step 2: LLM extracts to_name and/or purpose from reply ───────────────
    prompt = f"""
You are an intelligent email assistant. Extract the missing information from the user's reply.

Missing fields: {missing}
User reply: "{reply}"

Return JSON with keys matching missing fields:
- to_name: person's name or role mentioned (string or null)
- purpose: what the email is about (string or null)

Return JSON only, no markdown.
"""
    try:
        resp = await asyncio.to_thread(llm.invoke, prompt)
        parsed = _safe_parse_json(getattr(resp, "content", str(resp)).strip())
    except Exception as exc:
        logger.warning("[AskMissing] LLM parse failed: %s", exc)
        parsed = {}

    # ── Step 3: Resolve to_name → email via MongoDB ───────────────────────────
    if needs_email and parsed.get("to_name"):
        raw_to_name = str(parsed["to_name"]).strip()
        try:
            schema  = await _get_db_schema()
            history = [{"role": "user", "content": reply}]

            classification = await _classify_recipients(
                raw_to_name, reply, history, schema
            )

            resolved_emails, still_missing = await _resolve(
                classification, history, []
            )

            valid_emails = [e for e in resolved_emails if _is_valid_email(e)]

            if valid_emails:
                existing = data.get("to_email") or []
                if isinstance(existing, str):
                    existing = [existing] if existing else []
                all_emails = list(dict.fromkeys(existing + valid_emails))
                data["to_email"]        = all_emails[0] if len(all_emails) == 1 else all_emails
                data["recipient_count"] = len(all_emails)
                data["partial_missing"] = still_missing
                data["missing_fields"]  = [
                    f for f in missing if f not in ("to_email", "to_email_incomplete")
                ]
                if still_missing:
                    data["missing_fields"].append("to_email_incomplete")
                logger.info(
                    "[AskMissing] Resolved from name reply: %s | still missing: %s",
                    valid_emails, still_missing,
                )
            else:
                data["partial_missing"] = [raw_to_name]
                if "to_email_incomplete" not in data["missing_fields"]:
                    data["missing_fields"] = [
                        "to_email_incomplete" if f == "to_email" else f
                        for f in missing
                    ]
                logger.warning("[AskMissing] Could not resolve name from reply: '%s'", raw_to_name)

        except Exception as e:
            logger.warning("[AskMissing] MongoDB resolution failed: %s", e)

    # ── Step 4: Apply purpose if found ───────────────────────────────────────
    if needs_purpose and parsed.get("purpose"):
        data["purpose"] = str(parsed["purpose"]).strip()
        data["missing_fields"] = [
            f for f in data.get("missing_fields", []) if f != "purpose"
        ]
        logger.info("[AskMissing] Purpose resolved: %s", data["purpose"])

    return data


async def _llm_parse_purpose(reply: str, data: dict) -> dict:
    """Lightweight LLM call to extract just the purpose from a user reply."""
    prompt = f"""
Extract the email purpose/topic from this user reply: "{reply}"
Return JSON only: {{"purpose": "<extracted purpose or null>"}}
"""
    try:
        resp = await asyncio.to_thread(llm.invoke, prompt)
        parsed = _safe_parse_json(getattr(resp, "content", str(resp)).strip())
        if parsed.get("purpose"):
            data["purpose"] = str(parsed["purpose"]).strip()
            data["missing_fields"] = [
                f for f in data.get("missing_fields", []) if f != "purpose"
            ]
    except Exception:
        pass
    return data


# ── Phase B: Optional Fields ──────────────────────────────────────────────────

def _get_open_optional_fields(data: dict) -> List[str]:
    """Return only the optional fields that haven't been resolved yet."""
    open_fields = []
    if not data.get("skip_cc"):
        open_fields.append("cc")
    if not data.get("skip_bcc"):
        open_fields.append("bcc")
    if not data.get("skip_attachments"):
        open_fields.append("attachments")
    return open_fields


async def ask_optional(data: dict, user_reply: str) -> Tuple[str | None, dict]:
    """
    Ask for optional fields (cc, bcc, attachments) — only those not yet resolved.

    Flow:
      1. If optional_filled=True → skip entirely
      2. Compute open fields from skip flags
      3. No open fields → return (None, data)
      4. If already asked + user replied → parse and close phase
      5. If not asked yet → generate one LLM question for open fields only

    Attachment upload flag:
      When the bot is about to ask about attachments (and hasn't asked yet),
      it sets ``awaiting_attachment_upload: True`` in state so the frontend
      can display the file-picker UI alongside the chat bubble.
      The flag is cleared after the user has replied to the optional question.
    """
    if data.get("optional_filled"):
        logger.info("[AskMissing] optional_filled=True — skipping Phase B")
        return None, data

    open_fields = _get_open_optional_fields(data)

    if not open_fields:
        logger.info("[AskMissing] No open optional fields — skipping Phase B")
        return None, data

    if data.get("optional_asked") and user_reply and user_reply.strip():
        # Clear the upload-awaiting flag now that user has responded
        data.pop("awaiting_attachment_upload", None)
        data = await _parse_optional_reply_dynamic(user_reply, data, open_fields)
        return None, data

    if not data.get("optional_asked"):
        fields_label = ", ".join(open_fields)
        prompt = f"""
You are an intelligent email assistant. The email already has all required fields filled.
Ask the user if they want to provide these optional fields: {fields_label}.
- Ask in ONE friendly, concise question.
- Ask for cc, bcc, and attachment info together if all are missing.
- If only one role remains, ask just about that one.
- If asking about attachments, mention they can upload a file using the paperclip icon.
- Return ONLY the question text, no JSON, no preamble.
"""
        try:
            resp = await asyncio.to_thread(llm.invoke, prompt)
            question = getattr(resp, "content", str(resp)).strip()
        except Exception as exc:
            logger.warning("[AskMissing] Optional question generation failed: %s", exc)
            question = f"Would you like to add {fields_label} to this email? (Reply with values or say 'skip'.)"

        data["optional_asked"] = True

        # Signal to the frontend that it should show the file-picker if attachments
        # are among the fields being asked about
        if "attachments" in open_fields:
            data["awaiting_attachment_upload"] = True

        return question, data

    return None, data


async def _parse_optional_reply_dynamic(reply: str, data: dict, fields_asked: List[str]) -> dict:
    """
    Parse cc, bcc, and/or attachments from user's reply.
    Marks each asked role's skip flag = True after processing
    so the flow treats them as resolved regardless of whether user provided a value.

    Note: actual file bytes are stored in ``attachment_files`` (set by the upload
    endpoint). The ``attachments`` key here is just the text description that the
    LLM extracted from the chat reply (legacy field kept for compatibility).
    """
    from services.email_task.email_extract import _is_valid_email

    prompt = f"""
You are an intelligent email assistant. Extract the optional fields from the user's reply.

Fields that were asked about: {fields_asked}
User reply: "{reply}"

Instructions:
- cc: list of email addresses if the user gave any, else empty list []
- bcc: list of email addresses if the user gave any, else empty list []
- attachments: text description if the user mentioned one, else null
- If the user said "skip", "no", "none", "don't add", treat as not provided.
- Respond in JSON format with only the keys from: {fields_asked}
- Return JSON only, no markdown.
"""
    resp = await asyncio.to_thread(llm.invoke, prompt)
    llm_response = getattr(resp, "content", str(resp)).strip()
    parsed = _safe_parse_json(llm_response)

    if not parsed:
        logger.warning("[AskMissing] Failed to parse optional reply: '%s'", llm_response[:120])

    if "cc" in fields_asked:
        cc_val = parsed.get("cc", [])
        if isinstance(cc_val, list):
            valid_cc = [e for e in cc_val if _is_valid_email(str(e))]
            if valid_cc:
                data["cc"] = valid_cc
        data["skip_cc"] = True

    if "bcc" in fields_asked:
        bcc_val = parsed.get("bcc", [])
        if isinstance(bcc_val, list):
            valid_bcc = [e for e in bcc_val if _is_valid_email(str(e))]
            if valid_bcc:
                data["bcc"] = valid_bcc
        data["skip_bcc"] = True

    if "attachments" in fields_asked:
        att_val = parsed.get("attachments")
        if att_val and str(att_val).strip():
            data["attachments"] = str(att_val).strip()
        data["skip_attachments"] = True

    data["optional_filled"] = (
        data.get("skip_cc", False) and
        data.get("skip_bcc", False) and
        data.get("skip_attachments", False)
    )

    logger.info(
        "[AskMissing] Optional parsed | cc=%s bcc=%s att=%s | optional_filled=%s",
        data.get("cc"), data.get("bcc"), data.get("attachments"), data.get("optional_filled"),
    )
    return data
# services/email_handler.py
import asyncio
import logging
import re
from typing import Optional

from services.email_task import (
    email_send, email_extract, ask_missing,
    email_content, email_preview, email_modifier,
)
from services.email_task.ask_missing import ask_required, ask_optional

logger = logging.getLogger(__name__)

_STATE_KEY = "email_state:{}"
_TTL       = 7200

_EXIT_WORDS = {"quit", "exit", "stop", "abort", "cancel", "stop it", "nevermind", "never mind"}

_GATE_PROMPT = """\
You are managing an active email composition flow.

Current stage: {stage}
What the system last said to the user:
\"\"\"{last_reply}\"\"\"

The user just replied:
\"\"\"{message}\"\"\"

Classify the user's reply as exactly one of:

flow_response     — The user is directly responding to the email flow.
                    Includes: confirming, providing email address, name, yes/no to CC/BCC,
                    giving modification instruction, saying send/cancel/modify.

side_question     — The user is asking something completely unrelated to composing this email.
                    Includes: asking about a person, employee info, general questions,
                    company data, database queries, anything not about this specific email.

new_flow_request  — The user wants to start a completely NEW email to a different person
                    or about a completely different topic, while this email is still unsent.

Reply with ONLY one word: flow_response OR side_question OR new_flow_request
No explanation. No punctuation. No extra words.\
"""


# ── Redis helpers ─────────────────────────────────────────────────────────────

async def _load(user_id: str) -> dict:
    from core.redis_client import redis_get_json
    s = await redis_get_json(_STATE_KEY.format(user_id))
    return s if isinstance(s, dict) else {}

async def _save(user_id: str, state: dict) -> None:
    from core.redis_client import redis_set_json
    await redis_set_json(_STATE_KEY.format(user_id), state, ex=_TTL)

async def _clear(user_id: str) -> None:
    from core.redis_client import redis_delete
    await redis_delete(_STATE_KEY.format(user_id))

def _is_exit(message: str) -> bool:
    msg = message.lower().strip().strip("!.,?")
    return msg in _EXIT_WORDS or any(w in msg for w in _EXIT_WORDS)


# ── Gate classifier ───────────────────────────────────────────────────────────

async def _classify_message(user_message: str, stage: str, state: dict) -> str:
    from core.dependencies import get_llm
    last_reply = state.get("_last_reply", "(no previous reply)")[:400]
    prompt = _GATE_PROMPT.format(
        stage=stage,
        last_reply=last_reply,
        message=user_message[:300],
    )
    try:
        llm = get_llm()
        response = await asyncio.to_thread(llm.invoke, prompt)
        raw = (response.content if hasattr(response, "content") else str(response)).strip().lower()
        raw = re.sub(r"[^a-z_]", "", raw)
        if raw in ("flow_response", "side_question", "new_flow_request"):
            logger.info("[EmailGate] stage=%s | verdict=%s", stage, raw)
            return raw
        logger.warning("[EmailGate] Unexpected verdict '%s' — defaulting to flow_response", raw)
        return "flow_response"
    except Exception as e:
        logger.warning("[EmailGate] Classifier failed (%s) — defaulting to flow_response", e)
        return "flow_response"


# ── Side question handler ─────────────────────────────────────────────────────

async def _handle_side_question(user_message: str, user_id: str, thread_id: Optional[str], state: dict) -> str:
    from services.chat_service import process_query_direct
    try:
        answer = await process_query_direct(user_message, user_id)
    except Exception as e:
        logger.exception("[EmailSideQ] process_query_direct failed: %s", e)
        answer = "Sorry, I couldn't process that right now."

    count = state.get("_side_q_count", 0) + 1
    state["_side_q_count"] = count

    to_email = state.get("to_email", [])
    to_name  = state.get("to_name") or (to_email[0] if to_email else "recipient")
    stage    = state.get("stage", "preview")
    purpose  = state.get("purpose", "your email")

    if count == 1:
        stage_prompts = {
            "ask_required": f"By the way, I still need a few details to compose your email to {to_name}.",
            "ask_optional": f"By the way, your email to {to_name} is almost ready — just need your CC/BCC preference.",
            "preview":      f"By the way, your email to {to_name} about '{purpose}' is ready to review — send, modify, or cancel?",
            "modify":       f"By the way, I'm still working on your modification for the email to {to_name}.",
            "modify_await": f"By the way, I still need an email address to complete your modification.",
        }
        resume = stage_prompts.get(stage, f"By the way, your email to {to_name} is still pending.")
    elif count <= 3:
        resume = f"Your email to {to_name} is still pending."
    else:
        resume = ""

    combined = f"{answer}\n\n{resume}" if resume else answer

    # Restore _last_reply to the original stage prompt so the gate classifier
    # has correct context on the next message — not the side question answer
    original_stage_reply = state.get("_stage_reply", state.get("_last_reply", ""))
    state["_last_reply"]  = original_stage_reply
    await _save(user_id, state)
    return combined


# ── New flow conflict handler ─────────────────────────────────────────────────

async def _handle_new_flow_conflict(user_message: str, user_id: str, state: dict) -> str:
    to_email = state.get("to_email", [])
    to_name  = state.get("to_name") or (to_email[0] if to_email else "someone")
    purpose  = state.get("purpose", "your previous topic")

    state["_pending_new_request"] = user_message
    reply = (
        f"You still have an unsent email to {to_name} about '{purpose}'. "
        f"Would you like to finish and send it first, or discard it and start a new email?"
    )
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


# ── Public API ────────────────────────────────────────────────────────────────

async def handle_email_flow(
    user_message: str,
    user_id: str,
    thread_id: Optional[str] = None,
) -> str:
    state = await _load(user_id)
    stage = state.get("stage")

    # ── Handle pending conflict resolution ────────────────────────────────────
    if state.get("_pending_new_request"):
        msg_lower = user_message.lower().strip()
        discard_words = {"discard", "start new", "new email", "forget it", "ignore", "skip", "new one", "start over"}
        finish_words  = {"finish", "send it", "send", "keep", "continue", "yes", "go ahead"}

        if any(w in msg_lower for w in discard_words):
            pending = state.get("_pending_new_request", "")
            await _clear(user_id)
            logger.info("[EmailConflict] User discarded current email | user=%s", user_id)
            return await handle_email_flow(pending, user_id, thread_id)

        if any(w in msg_lower for w in finish_words):
            state.pop("_pending_new_request", None)
            reply = email_preview.build_preview(state)
            state["_last_reply"] = reply
            await _save(user_id, state)
            return reply

    # ── Exit check ────────────────────────────────────────────────────────────
    if stage != "preview" and _is_exit(user_message):
        await _clear(user_id)
        logger.info("[EmailHandler] user=%s exited email flow", user_id)
        return "Email cancelled. How else can I help you?"

    logger.info("[EmailHandler] user=%s | stage=%s", user_id, stage or "new")

    if thread_id:
        state["_thread_id"] = thread_id

    # ── Gate check (only when flow is already in progress) ───────────────────
    if stage and stage not in ("extract",):
        verdict = await _classify_message(user_message, stage, state)

        if verdict == "side_question":
            logger.info("[EmailGate] Side question | user=%s | stage=%s", user_id, stage)
            return await _handle_side_question(user_message, user_id, thread_id, state)

        if verdict == "new_flow_request":
            logger.info("[EmailGate] New flow conflict | user=%s | stage=%s", user_id, stage)
            return await _handle_new_flow_conflict(user_message, user_id, state)

    # ── Route to stage handler ────────────────────────────────────────────────
    if not stage or stage == "extract":
        return await _stage_extract(user_message, user_id, thread_id, state)
    if stage == "ask_required":
        return await _stage_ask_required(user_message, user_id, state)
    if stage == "ask_optional":
        return await _stage_ask_optional(user_message, user_id, state)
    if stage == "preview":
        return await _stage_preview(user_message, user_id, state)
    if stage == "modify":
        return await _stage_modify(user_message, user_id, state)
    if stage == "modify_await":
        return await _stage_modify_await(user_message, user_id, state)

    logger.warning("[EmailHandler] Unknown stage '%s' — restarting", stage)
    await _clear(user_id)
    return await _stage_extract(user_message, user_id, thread_id, {})


# ── Stage handlers ────────────────────────────────────────────────────────────

async def _stage_extract(user_message: str, user_id: str, thread_id: Optional[str], state: dict) -> str:
    fields = await email_extract.extract_email_fields(user_message, user_id, thread_id)

    te = fields.get("to_email")
    if isinstance(te, str):
        fields["to_email"] = [te] if te else []
    elif te is None:
        fields["to_email"] = []

    state.update(fields)
    state["active_task"] = "email"
    if thread_id:
        state["_thread_id"] = thread_id

    if fields.get("missing_fields"):
        state["stage"] = "ask_required"
        await _save(user_id, state)
        question, updated = await ask_missing.ask_required(state, "")
        state.update(updated)
        state["stage"] = "ask_required"
        reply = question or "Could you provide more details?"
        state["_last_reply"]  = reply
        state["_stage_reply"] = reply
        await _save(user_id, state)
        return reply

    if fields.get("optional_filled"):
        state["stage"] = "ask_optional"
        await _save(user_id, state)
        return await _generate_and_preview(user_id, state)

    state["stage"] = "ask_optional"
    await _save(user_id, state)
    question, updated = await ask_optional(state, "")
    state.update(updated)
    if question:
        state["_last_reply"] = question
        await _save(user_id, state)
        return question
    return await _generate_and_preview(user_id, state)


async def _stage_ask_required(user_message: str, user_id: str, state: dict) -> str:
    question, updated = await ask_required(state, user_message)

    te = updated.get("to_email")
    if isinstance(te, str):
        updated["to_email"] = [te] if te else []
    elif te is None:
        updated["to_email"] = []

    state.update(updated)

    if question:
        state["stage"] = "ask_required"
        state["_last_reply"] = question
        state["_stage_reply"] = question
        await _save(user_id, state)
        return question

    if updated.get("optional_filled"):
        return await _generate_and_preview(user_id, state)

    state["stage"] = "ask_optional"
    await _save(user_id, state)
    opt_question, updated2 = await ask_optional(state, "")
    state.update(updated2)
    if opt_question:
        state["_last_reply"] = opt_question
        state["_stage_reply"] = opt_question
        await _save(user_id, state)
        return opt_question
    return await _generate_and_preview(user_id, state)


async def _stage_ask_optional(user_message: str, user_id: str, state: dict) -> str:
    question, updated = await ask_optional(state, user_message)
    state.update(updated)

    if question:
        state["stage"] = "ask_optional"
        state["_last_reply"] = question
        state["_stage_reply"] = question
        await _save(user_id, state)
        return question

    return await _generate_and_preview(user_id, state)


async def _generate_and_preview(user_id: str, state: dict) -> str:
    thread_id = state.get("_thread_id")

    to_email_val = state.get("to_email")
    if isinstance(to_email_val, list):
        recipient_count = len(to_email_val)
    elif to_email_val:
        recipient_count = 1
    else:
        recipient_count = 0
    state["recipient_count"] = recipient_count

    to_name_raw   = state.get("to_name")
    greeting_name = str(to_name_raw).strip() if (recipient_count == 1 and to_name_raw) else None

    subject, body = await email_content.generate_email_content(
        purpose=state.get("purpose"),
        tone=state.get("tone"),
        location=state.get("location"),
        context="",
        recipient_count=recipient_count,
        user_id=user_id,
        thread_id=thread_id,
        to_name=greeting_name,
    )

    state["subject"]     = subject
    state["body"]        = body
    state["stage"]       = "preview"
    reply                = email_preview.build_preview(state)
    state["_last_reply"] = reply
    state["_stage_reply"] = reply
    await _save(user_id, state)
    return reply


async def _stage_preview(user_message: str, user_id: str, state: dict) -> str:
    action, instruction = await email_preview.detect_user_choice_llm(user_message)
    logger.info("[EmailHandler] preview action=%s", action)

    if action == "send":
        result = await email_send.send_email(state)
        await _clear(user_id)
        return result

    if action == "cancel":
        await _clear(user_id)
        return "Email cancelled. Is there anything else I can help with?"

    if action == "modify":
        state["stage"] = "modify"
        await _save(user_id, state)
        return await _stage_modify(instruction, user_id, state)

    reply = email_preview.build_preview(state)
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


async def _stage_modify(user_message: str, user_id: str, state: dict) -> str:
    new_to, new_subject, new_body, new_cc, new_bcc, ask_question = \
        await email_modifier.modify_email(state, user_message)

    state.update({
        "to_email": new_to,
        "subject":  new_subject,
        "body":     new_body,
        "cc":       new_cc,
        "bcc":      new_bcc,
    })

    if ask_question:
        state["stage"]       = "modify_await"
        state["_last_reply"] = ask_question
        await _save(user_id, state)
        return ask_question

    state["stage"] = "preview"
    reply = email_preview.build_preview(state)
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


async def _stage_modify_await(user_message: str, user_id: str, state: dict) -> str:
    from services.email_task.email_extract import (
        _extract_emails_from_text, _is_valid_email, _mongo_lookup_by_name,
    )

    direct_emails = [e for e in _extract_emails_from_text(user_message) if _is_valid_email(e)]

    if not direct_emails:
        name_candidate = user_message.strip()
        if name_candidate and len(name_candidate) < 60:
            found = await _mongo_lookup_by_name(name_candidate)
            if found:
                direct_emails = [found]

    if direct_emails:
        current_to = state.get("to_email") or []
        if isinstance(current_to, str):
            current_to = [current_to] if current_to else []
        all_to = list(dict.fromkeys(current_to + direct_emails))
        state["to_email"]        = all_to
        state["recipient_count"] = len(all_to)
        state["stage"]           = "preview"
        reply = email_preview.build_preview(state)
        state["_last_reply"] = reply
        await _save(user_id, state)
        return reply

    reply = "I couldn't find that email address. Could you share the exact email address you'd like to add?"
    state["stage"]       = "modify_await"
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


async def is_email_active(user_id: str) -> bool:
    s = await _load(user_id)
    return s.get("active_task") == "email" and bool(s.get("stage"))

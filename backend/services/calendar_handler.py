# services/calendar_handler.py
import asyncio
import logging
import re
from typing import Optional

from services.calendar_task import (
    calendar_extract, ask_missing, ask_attendees,
    cal_preview, cal_modifier, set_calendar,
)

logger = logging.getLogger(__name__)

_STATE_KEY = "cal_state:{}"
_TTL       = 7200

_EXIT_WORDS = {"quit", "exit", "stop", "abort", "cancel", "stop it", "nevermind", "never mind"}

_GATE_PROMPT = """\
You are managing an active calendar event creation flow.

Current stage: {stage}
What the system last said to the user:
\"\"\"{last_reply}\"\"\"

The user just replied:
\"\"\"{message}\"\"\"

Classify the user's reply as exactly one of:

flow_response     — The user is directly responding to the calendar flow.
                    Includes: confirming the event, cancelling, providing title, date, time,
                    attendee name or email, giving a modification instruction for this event.

side_question     — The user is asking something completely unrelated to scheduling this event.
                    Includes: asking about a person, employee info, general questions,
                    company data, database queries, anything not about this specific calendar event.

new_flow_request  — The user wants to create a completely NEW calendar event on a different
                    topic or with different people, while this event is still not created.

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
            logger.info("[CalGate] stage=%s | verdict=%s", stage, raw)
            return raw
        logger.warning("[CalGate] Unexpected verdict '%s' — defaulting to flow_response", raw)
        return "flow_response"
    except Exception as e:
        logger.warning("[CalGate] Classifier failed (%s) — defaulting to flow_response", e)
        return "flow_response"


# ── Side question handler ─────────────────────────────────────────────────────

async def _handle_side_question(user_message: str, user_id: str, thread_id: Optional[str], state: dict) -> str:
    from services.chat_service import process_query_direct
    try:
        answer = await process_query_direct(user_message, user_id)
    except Exception as e:
        logger.exception("[CalSideQ] process_query_direct failed: %s", e)
        answer = "Sorry, I couldn't process that right now."

    count = state.get("_side_q_count", 0) + 1
    state["_side_q_count"] = count

    title      = state.get("title") or "your event"
    start_time = state.get("start_time", "")
    stage      = state.get("stage", "preview")
    time_part  = f" at {start_time}" if start_time else ""

    if count == 1:
        stage_prompts = {
            "ask_missing":   f"By the way, I still need a few details to create '{title}'{time_part}.",
            "ask_attendees": f"By the way, I'm still collecting attendees for '{title}'{time_part}.",
            "preview":       f"By the way, your calendar event '{title}'{time_part} is ready — confirm, modify, or cancel?",
            "modify_await":  f"By the way, I still need an email address to complete your event modification.",
        }
        resume = stage_prompts.get(stage, f"By the way, your calendar event '{title}' is still pending.")
    elif count <= 3:
        resume = f"📅 Calendar event '{title}' still pending."
    else:
        resume = ""

    combined = f"{answer}\n\n{resume}" if resume else answer
    state["_last_reply"] = combined
    await _save(user_id, state)
    return combined


# ── New flow conflict handler ─────────────────────────────────────────────────

async def _handle_new_flow_conflict(user_message: str, user_id: str, state: dict) -> str:
    title      = state.get("title") or "your current event"
    start_time = state.get("start_time", "")
    time_part  = f" at {start_time}" if start_time else ""

    state["_pending_new_request"] = user_message
    reply = (
        f"You still have a pending calendar event: '{title}'{time_part}. "
        f"Would you like to finish creating it first, or discard it and create a new event?"
    )
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


# ── Public API ────────────────────────────────────────────────────────────────

async def handle_calendar_flow(
    user_message: str,
    user_id: str,
    thread_id: Optional[str] = None,
) -> str:
    state = await _load(user_id)
    stage = state.get("stage")

    # ── Handle pending conflict resolution ────────────────────────────────────
    if state.get("_pending_new_request"):
        msg_lower     = user_message.lower().strip()
        discard_words = {"discard", "new event", "forget it", "ignore", "skip", "new one", "start over", "create new"}
        finish_words  = {"finish", "confirm", "keep", "continue", "yes", "go ahead", "create it"}

        if any(w in msg_lower for w in discard_words):
            pending = state.get("_pending_new_request", "")
            await _clear(user_id)
            logger.info("[CalConflict] User discarded current event | user=%s", user_id)
            return await handle_calendar_flow(pending, user_id, thread_id)

        if any(w in msg_lower for w in finish_words):
            state.pop("_pending_new_request", None)
            reply = cal_preview.build_preview(state)
            state["_last_reply"] = reply
            await _save(user_id, state)
            return reply

    # ── Exit check ────────────────────────────────────────────────────────────
    if stage != "preview" and _is_exit(user_message):
        await _clear(user_id)
        logger.info("[CalHandler] user=%s exited calendar flow", user_id)
        return "✅ Calendar event creation cancelled. How else can I help?"

    logger.info("[CalHandler] user=%s | stage=%s", user_id, stage or "new")

    if thread_id:
        state["_thread_id"] = thread_id

    # ── Gate check (only when flow is already in progress) ───────────────────
    if stage and stage not in ("extract",):
        verdict = await _classify_message(user_message, stage, state)

        if verdict == "side_question":
            logger.info("[CalGate] Side question | user=%s | stage=%s", user_id, stage)
            return await _handle_side_question(user_message, user_id, thread_id, state)

        if verdict == "new_flow_request":
            logger.info("[CalGate] New flow conflict | user=%s | stage=%s", user_id, stage)
            return await _handle_new_flow_conflict(user_message, user_id, state)

    # ── Route to stage handler ────────────────────────────────────────────────
    if not stage or stage == "extract":
        return await _stage_extract(user_message, user_id, thread_id, state)
    if stage == "ask_missing":
        return await _stage_ask_missing(user_message, user_id, state)
    if stage == "ask_attendees":
        return await _stage_ask_attendees(user_message, user_id, state)
    if stage == "preview":
        return await _stage_preview(user_message, user_id, state)
    if stage == "modify_await":
        return await _stage_modify_await(user_message, user_id, state)
    if stage == "create":
        return await _stage_create(user_id, state)

    logger.warning("[CalHandler] Unknown stage '%s' — restarting", stage)
    await _clear(user_id)
    return await _stage_extract(user_message, user_id, thread_id, {})


# ── Stage handlers ────────────────────────────────────────────────────────────

async def _stage_extract(user_message: str, user_id: str, thread_id: Optional[str], state: dict) -> str:
    fields = await calendar_extract.extract_calendar_fields(user_message, user_id, thread_id)

    state.update(fields)
    state["active_task"]       = "calendar"
    state["_original_message"] = user_message
    if thread_id:
        state["_thread_id"] = thread_id

    if fields.get("missing_fields"):
        state["stage"] = "ask_missing"
        await _save(user_id, state)
        question, updated = await ask_missing.ask_required(state, "", user_id, thread_id)
        state.update(updated)
        state["stage"] = "ask_missing"
        reply = question or "Could you provide more details about the event?"
        state["_last_reply"] = reply
        await _save(user_id, state)
        return reply

    return await _enter_attendees_stage(user_id, state, original_message=user_message)


async def _stage_ask_missing(user_message: str, user_id: str, state: dict) -> str:
    thread_id = state.get("_thread_id")
    question, updated = await ask_missing.ask_required(state, user_message, user_id, thread_id)
    state.update(updated)

    if question:
        state["stage"]       = "ask_missing"
        state["_last_reply"] = question
        await _save(user_id, state)
        return question

    original_message = state.get("_original_message", "")
    return await _enter_attendees_stage(user_id, state, original_message=original_message)


async def _enter_attendees_stage(user_id: str, state: dict, original_message: str = "") -> str:
    state["stage"]          = "ask_attendees"
    state["attendee_stage"] = "on_extract"
    thread_id               = state.get("_thread_id")

    await _save(user_id, state)

    question, updated = await ask_attendees.handle_attendees(
        state, original_message, user_id, thread_id
    )
    state.update(updated)

    if question:
        state["stage"]       = "ask_attendees"
        state["_last_reply"] = question
        await _save(user_id, state)
        return question

    return await _enter_preview_stage(user_id, state)


async def _stage_ask_attendees(user_message: str, user_id: str, state: dict) -> str:
    thread_id = state.get("_thread_id")

    question, updated = await ask_attendees.handle_attendees(
        state, user_message, user_id, thread_id
    )
    state.update(updated)

    if question:
        state["stage"]       = "ask_attendees"
        state["_last_reply"] = question
        await _save(user_id, state)
        return question

    return await _enter_preview_stage(user_id, state)


async def _enter_preview_stage(user_id: str, state: dict) -> str:
    state["stage"] = "preview"
    reply = cal_preview.build_preview(state)
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


async def _stage_preview(user_message: str, user_id: str, state: dict) -> str:
    action, instruction = await cal_preview.detect_user_choice(user_message)
    logger.info("[CalHandler] preview action=%s", action)

    if action == "confirm":
        state["stage"] = "create"
        await _save(user_id, state)
        return await _stage_create(user_id, state)

    if action == "cancel":
        await _clear(user_id)
        return "✅ Event cancelled. Let me know if you'd like to schedule something else!"

    if action == "modify":
        updated_data, ask_question = await cal_modifier.modify_event(state, instruction)
        state.update(updated_data)

        if ask_question:
            state["stage"]       = "modify_await"
            state["_last_reply"] = ask_question
            await _save(user_id, state)
            return ask_question

        state["stage"] = "preview"
        reply = cal_preview.build_preview(state)
        state["_last_reply"] = reply
        await _save(user_id, state)
        return reply

    reply = cal_preview.build_preview(state)
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


async def _stage_modify_await(user_message: str, user_id: str, state: dict) -> str:
    from services.calendar_task.ask_attendees import (
        _mongo_lookup_name, _is_valid_email, _extract_emails, _generate_missing_q,
    )

    attendees: list[dict] = list(state.get("attendees") or [])
    still = [a for a in attendees if not a.get("email")]

    if not still:
        return await _enter_preview_stage(user_id, state)

    person      = still[0]
    person_name = person.get("name", "that person")

    direct = [e for e in _extract_emails(user_message) if _is_valid_email(e)]
    if direct:
        idx = attendees.index(person)
        attendees[idx]["email"] = direct[0]
        state["attendees"] = attendees
        remaining = [a for a in attendees if not a.get("email")]
        if remaining:
            state["stage"] = "modify_await"
            reply = await _generate_missing_q(remaining[0]["name"])
            state["_last_reply"] = reply
            await _save(user_id, state)
            return reply
        state["missing_fields"] = []
        return await _enter_preview_stage(user_id, state)

    candidate = user_message.strip()
    if candidate and len(candidate) < 60:
        found = await _mongo_lookup_name(candidate)
        if found and _is_valid_email(found):
            idx = attendees.index(person)
            attendees[idx]["email"] = found
            state["attendees"] = attendees
            remaining = [a for a in attendees if not a.get("email")]
            if remaining:
                state["stage"] = "modify_await"
                reply = await _generate_missing_q(remaining[0]["name"])
                state["_last_reply"] = reply
                await _save(user_id, state)
                return reply
            state["missing_fields"] = []
            return await _enter_preview_stage(user_id, state)

    state["stage"] = "modify_await"
    reply = await _generate_missing_q(person_name)
    state["_last_reply"] = reply
    await _save(user_id, state)
    return reply


async def _stage_create(user_id: str, state: dict) -> str:
    result = await set_calendar.create_event(state, user_id)
    await _clear(user_id)
    return result


async def is_calendar_active(user_id: str) -> bool:
    s = await _load(user_id)
    return s.get("active_task") == "calendar" and bool(s.get("stage"))

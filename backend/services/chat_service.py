# services/chat_service.py
import asyncio
import logging

from core.dependencies import get_llm, get_openai_llm
from utils.intent_detector import detect_intent_async

logger = logging.getLogger(__name__)

DEFAULT_USER = "default_user"

_active_threads: dict[str, str] = {}


# ── Session lifecycle ─────────────────────────────────────────────────────────

async def initialize_session(user_id: str = DEFAULT_USER) -> str:
    from services.thread_service import create_new_thread
    thread_id = await create_new_thread(user_id)
    _active_threads[user_id] = thread_id
    logger.info("[Chat] Session initialized | user=%s | thread=%s", user_id, thread_id)
    return thread_id


async def end_session(user_id: str = DEFAULT_USER) -> bool:
    from services.thread_service import get_active_thread, invalidate_past_summaries_cache
    from services.summary_service import flush_session_summary
    thread_id = _active_threads.get(user_id) or await get_active_thread(user_id)
    if not thread_id:
        return False

    llm     = get_llm()
    flushed = await flush_session_summary(thread_id, llm)
    await invalidate_past_summaries_cache(user_id)

    logger.info("[Chat] Session ended | user=%s | thread=%s | flushed=%s", user_id, thread_id, flushed)
    _active_threads.pop(user_id, None)
    return flushed


# ── Thread helpers ────────────────────────────────────────────────────────────

async def switch_to_thread(user_id: str, thread_id: str) -> bool:
    from services.thread_service import set_active_thread
    ok = await set_active_thread(user_id, thread_id)
    if ok:
        _active_threads[user_id] = thread_id
    return ok


async def get_thread_list(user_id: str) -> list[dict]:
    from services.thread_service import list_threads
    return await list_threads(user_id)


# ── System Prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    return (
        'You are "Personal Assistant" — a smart, reliable, and friendly AI assistant.\n'
        "You help with answering questions, sending and reading emails, managing calendar events, "
        "managing personal tasks, and searching through employee and company data.\n\n"
        "RESPONSE FORMATTING RULES:\n"
        "- For simple factual answers (a name, a date, a number): respond in 1-2 sentences.\n"
        "- For explanations, how-to questions, or multi-part topics: use bullet points, numbered steps, "
        "and headers where helpful. Structure the response clearly.\n"
        "- For code questions: always use code blocks.\n"
        "- Never use emojis.\n"
        "- Match the depth of the answer to the complexity of the question.\n"
    )


# ── Resolve active thread ─────────────────────────────────────────────────────

async def _get_thread_id(user_id: str) -> str:
    from services.thread_service import get_active_thread, create_new_thread
    if user_id in _active_threads:
        return _active_threads[user_id]
    thread_id = await get_active_thread(user_id)
    if thread_id:
        _active_threads[user_id] = thread_id
        return thread_id
    thread_id = await create_new_thread(user_id)
    _active_threads[user_id] = thread_id
    return thread_id


# ── LLM title generation (background task) ───────────────────────────────────

async def _generate_and_set_title(thread_id: str, user_message: str, assistant_reply: str) -> None:
    from langchain_core.messages import HumanMessage
    from services.thread_service import update_thread_title
    prompt = (
        "Generate a short title (4-6 words, no punctuation, no quotes) for a "
        "conversation that started with:\n\n"
        f"User: {user_message[:300]}\n"
        f"Assistant: {assistant_reply[:300]}\n\n"
        "Rules:\n"
        "- 4 to 6 words maximum\n"
        "- No punctuation, no quotes, no full stop\n"
        "- Capture the topic, not the tone\n"
        "- Examples: 'Backend intern locations Surat', 'Anand salary details', "
        "'Calendar event next Monday'\n\n"
        "Title:"
    )
    try:
        llm      = get_llm()
        response = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        title    = (response.content.strip() if hasattr(response, "content") else str(response).strip())
        title    = title.strip('"').strip("'").strip()
        if title:
            await update_thread_title(thread_id, title)
            logger.info("[Chat] Title generated | thread=%s | title='%s'", thread_id, title)
    except Exception as e:
        logger.warning("[Chat] Title generation failed (non-critical): %s", e)


# ── Cross-thread past context builder ────────────────────────────────────────

async def _get_past_context(user_id: str, current_thread_id: str) -> str:
    from services.summary_service import get_past_thread_summaries
    past = await get_past_thread_summaries(user_id, current_thread_id)
    if not past:
        return ""
    blocks = []
    for item in past:
        session_num  = item["session_num"]
        title        = item.get("title", "Previous Session")
        summary_text = item.get("summary_text", "").strip()
        if summary_text:
            blocks.append(f"PREVIOUS SESSION {session_num} ({title}):\n{summary_text}")
    return "\n\n".join(blocks)


# ── RAG handler ───────────────────────────────────────────────────────────────

async def _handle_rag(query: str, user_id: str, thread_id: str) -> str:
    from langchain_core.messages import SystemMessage
    from services.summary_service import get_thread_summary
    from services.message_service import format_history_from_db
    from services.mongo_rag_service import search_employees
    logger.info("[RAG] Handling query: '%s'", query[:80])
    llm = get_openai_llm()

    past_context = await _get_past_context(user_id, thread_id)

    try:
        current_summary = await get_thread_summary(thread_id)
    except Exception as e:
        logger.exception("[RAG] Failed to load current summary: %s", e)
        current_summary = ""

    history_text = await format_history_from_db(thread_id, limit=20)

    try:
        kb_results = await search_employees(query, top_k=5)
        kb_results = [r for r in kb_results if r.get("score", 0) >= 0.75]
        kb_context = "\n\n".join(
            f"[{r.get('metadata', {}).get('name', 'Employee')} | "
            f"dept: {r.get('metadata', {}).get('department', '?')} | "
            f"position: {r.get('metadata', {}).get('position', '?')}]\n{r['content']}"
            for r in kb_results
        ) if kb_results else ""
    except Exception as e:
        logger.exception("[RAG] KB search failed: %s", e)
        kb_context = ""

    parts = [_build_system_prompt()]
    if past_context:
        parts.append("\n\n--- MEMORY FROM PREVIOUS SESSIONS ---\n" + past_context)
    parts.append(
        "\n\nCURRENT SESSION SUMMARY:\n"
        + (current_summary or "(no summary yet)")
    )
    parts.append(
        "\n\nRECENT CONVERSATION:\n" + (history_text or "(no history yet)")
    )
    parts.append(
        "\n\nEMPLOYEE KNOWLEDGE BASE:\n" + (kb_context or "(no relevant data found)")
    )
    parts.append(
        "\n\nUSER QUESTION:\n" + query
        + "\n\n--- INSTRUCTIONS ---\n"
        "- Use the EMPLOYEE KNOWLEDGE BASE for questions about specific people's details.\n"
        "- For general knowledge questions, answer from your own knowledge — do NOT mention employees.\n"
        "- NEVER hallucinate any name, email, phone, or detail not explicitly in the KB entries above.\n"
        "- ONLY mention a person if their KB entry is shown above.\n"
        "- If the answer is not in the KB, say exactly: 'I don't have that information.'\n"
        "- If the user asks for one specific detail (e.g. email, phone, address), return ONLY that detail.\n"
        "- If the user asks an open question (explain, describe, how does, list), give a full structured answer.\n"
        "- Match answer length to question complexity. Short questions get short answers. "
        "Open questions get structured, detailed answers with bullet points or numbered steps where appropriate.\n"
        "\nFINAL ANSWER:"
    )

    system_content = "".join(parts)
    try:
        response = await asyncio.to_thread(llm.invoke, [SystemMessage(content=system_content)])
        answer = (response.content.strip() if hasattr(response, "content") else str(response).strip())
        weak_signals = ["i don't know", "not enough context", "no relevant"]
        if not answer or (any(sig in answer.lower() for sig in weak_signals) and not kb_context):
            logger.info("[RAG] Weak answer with no KB — falling back to pure LLM.")
            return await _handle_llm_chat(query, user_id, thread_id)
        return answer
    except Exception as e:
        logger.exception("[RAG] LLM call failed: %s", e)
        return await _handle_llm_chat(query, user_id, thread_id)


# ── Pure LLM fallback ─────────────────────────────────────────────────────────

async def _handle_llm_chat(query: str, user_id: str, thread_id: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage
    from services.summary_service import get_thread_summary
    from services.message_service import format_history_from_db
    try:
        llm             = get_openai_llm()
        past_context    = await _get_past_context(user_id, thread_id)
        current_summary = await get_thread_summary(thread_id)
        history_text    = await format_history_from_db(thread_id, limit=20)

        parts = [_build_system_prompt()]
        if past_context:
            parts.append("\n\n--- MEMORY FROM PREVIOUS SESSIONS ---\n" + past_context)
        parts.append(
            "\n\nCURRENT SESSION SUMMARY:\n" + (current_summary or "(no summary yet)")
        )
        parts.append(
            "\n\nRECENT CONVERSATION:\n" + (history_text or "(no history yet)")
        )

        messages = [
            SystemMessage(content="".join(parts)),
            HumanMessage(content=(
                query + "\n\n"
                "[Format your response appropriately: use bullet points for lists, "
                "numbered steps for sequences, code blocks for code, "
                "and headers for multi-section answers. Plain prose for simple questions.]"
            )),
        ]

        response = await asyncio.to_thread(llm.invoke, messages)
        answer   = (response.content.strip() if hasattr(response, "content") else str(response).strip())
        logger.info("[Chat] Pure LLM response generated.")
        return answer

    except Exception as e:
        logger.exception("[Chat] Pure LLM failed: %s", e)
        return "Sorry, something went wrong. Please try again."


# ── Direct query handler (skips active flow check) ───────────────────────────

async def process_query_direct(query: str, user_id: str = DEFAULT_USER) -> str:
    """
    Answer a query by routing directly to the correct handler.
    SKIPS the active flow check — called only from email/calendar side question handlers
    to avoid looping back into the same handler.
    """
    from services.db_query_service import handle_db_query
    from services.crud_service import handle_crud
    from services.gmail_read_service import handle_gmail_read
    from services.tasks_service import handle_tasks
    if not query or not query.strip():
        return "I didn't catch that — could you rephrase?"

    logger.info("[Chat:Direct] user=%s | query=%s", user_id, query[:80])

    thread_id = await _get_thread_id(user_id)
    intent    = await detect_intent_async(query)
    logger.info("[Chat:Direct] intent=%s", intent)

    if intent == "db_query":
        return await handle_db_query(query, user_id)
    if intent == "crud":
        return await handle_crud(query, user_id)
    if intent == "email_read":
        return await handle_gmail_read(query, user_id)
    if intent == "tasks":
        return await handle_tasks(query, user_id)
    # email_send/calendar intents fall through to RAG to avoid nested flows
    return await _handle_rag(query, user_id, thread_id)


# ── Save turn + trigger summary ───────────────────────────────────────────────

async def _save_turn_to_mongodb(thread_id: str, query: str, answer: str) -> None:
    from services.message_service import save_message
    from services.summary_service import maybe_update_summary
    try:
        await save_message(thread_id, "user", query)
        new_count, summarized_up_to = await save_message(thread_id, "assistant", answer)

        llm = get_llm()
        await maybe_update_summary(thread_id, new_count, summarized_up_to, llm)

        if new_count == 2:
            asyncio.create_task(_generate_and_set_title(thread_id, query, answer))
            logger.info("[Chat] Title generation task scheduled | thread=%s", thread_id)

        logger.info(
            "[Chat] Turn saved | thread=%s | count=%d | summarized_up_to=%d",
            thread_id, new_count, summarized_up_to,
        )
    except Exception as e:
        logger.exception("[Chat] Failed to save turn: %s", e)


# ── Main entry point ──────────────────────────────────────────────────────────

async def process_query(query: str, user_id: str = DEFAULT_USER) -> str:
    if not query or not query.strip():
        logger.warning("[Chat] Empty query received.")
        return "I didn't catch that — could you rephrase?"

    logger.info("[Chat] Query from '%s': %s", user_id, query[:120])

    thread_id = await _get_thread_id(user_id)
    logger.info("[Chat] thread_id='%s'", thread_id)

    from services.email_handler    import handle_email_flow,    is_email_active
    from services.calendar_handler import handle_calendar_flow, is_calendar_active
    from services.crud_service     import _pending_add, _is_cancel_intent
    from services.db_query_service import handle_db_query
    from services.crud_service     import handle_crud
    from services.gmail_read_service import handle_gmail_read
    from services.tasks_service    import handle_tasks

    if await is_email_active(user_id):
        logger.info("[Router] Continuing active EMAIL flow.")
        reply = await handle_email_flow(query, user_id, thread_id)
        await _save_turn_to_mongodb(thread_id, query, reply)
        return reply

    if await is_calendar_active(user_id):
        logger.info("[Router] Continuing active CALENDAR flow.")
        reply = await handle_calendar_flow(query, user_id, thread_id)
        await _save_turn_to_mongodb(thread_id, query, reply)
        return reply

    # Resume a pending add-employee flow before running intent detection.
    # Also route cancel messages directly so the CRUD handler can clear state.
    if user_id in _pending_add or (
        _is_cancel_intent(query) and user_id in _pending_add
    ):
        logger.info("[Router] Resuming pending CRUD add flow for user=%s", user_id)
        reply = await handle_crud(query, user_id)
        await _save_turn_to_mongodb(thread_id, query, reply)
        return reply

    intent = await detect_intent_async(query)
    logger.info("[Router] Intent = %s", intent)

    if intent == "email_send":
        reply = await handle_email_flow(query, user_id, thread_id)
    elif intent == "calendar":
        reply = await handle_calendar_flow(query, user_id, thread_id)
    elif intent == "email_read":
        reply = await handle_gmail_read(query, user_id)
    elif intent == "tasks":
        reply = await handle_tasks(query, user_id)
    elif intent == "db_query":
        reply = await handle_db_query(query, user_id)
    elif intent == "crud":
        reply = await handle_crud(query, user_id)
    else:
        reply = await _handle_rag(query, user_id, thread_id)

    await _save_turn_to_mongodb(thread_id, query, reply)
    return reply



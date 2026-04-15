# tests/test_chatbot.py
"""
Integration tests for all chatbot services.

Covers:
  - RAG (employee knowledge base queries)
  - db_query (list/count employees)
  - CRUD (add / update / delete employee)
  - Email flow (full flow + side question gate)
  - Calendar flow (full flow + side question gate)

Run:
    pytest tests/test_chatbot.py -v

Requirements:
  - Server must be running: uvicorn main:app --reload
  - MongoDB Atlas + Redis must be reachable
  - .env must have valid GROQ_API_KEY, GEMINI_API_KEY, MONGO_URL
"""

import pytest
import httpx
import asyncio

BASE_URL = "http://127.0.0.1:8000"
USER_ID  = "test_user_pytest"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def chat(message: str, user_id: str = USER_ID) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{BASE_URL}/chat/",
            json={"message": message, "user_id": user_id},
        )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()["response"]


async def clear_state(user_id: str = USER_ID) -> None:
    """Hit debug endpoint to inspect (and implicitly confirm) state is visible."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.get(f"{BASE_URL}/debug/state/{user_id}")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def reset_state():
    """Cancel any active flow before and after every test."""
    # pre-test: send cancel to flush any leftover state
    try:
        await chat("cancel", user_id=USER_ID)
    except Exception:
        pass
    yield
    try:
        await chat("cancel", user_id=USER_ID)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 — RAG: single employee lookup
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rag_employee_lookup():
    """
    RAG path: intent=default → _handle_rag() → search_employees() → LLM answer.
    Verifies the response is non-empty and not a hallucinated refusal.
    """
    reply = await chat("What is Anand's email address?")

    assert reply and len(reply) > 10, "RAG returned empty response"

    hard_refusals = ["i cannot help", "i am unable", "as an ai language model"]
    assert not any(p in reply.lower() for p in hard_refusals), (
        f"RAG returned unexpected refusal: {reply}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 — db_query: list all interns
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_query_list_interns():
    """
    db_query path: intent=db_query → handle_db_query() → MongoDB find() → LLM format.
    Expects a non-empty formatted list response.
    """
    reply = await chat("List all interns")

    assert reply and len(reply) > 10, "db_query returned empty response"
    # Response should not be a generic error
    assert "something went wrong" not in reply.lower(), (
        f"db_query returned error: {reply}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 — CRUD: add → verify → delete
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_crud_add_and_delete():
    """
    CRUD path: add employee → confirm via db_query → delete employee.
    Verifies _add_employee() and _delete_employee() both succeed.
    """
    # ADD — use explicit key=value format so the LLM maps every field correctly
    add_reply = await chat(
        "Add new employee: name=TestBot Alpha, email=testbot@example.com, "
        "role=Backend, position=Intern, contact=9000000001"
    )
    assert "✅" in add_reply or "added" in add_reply.lower(), (
        f"CRUD add did not confirm success: {add_reply}"
    )

    # Brief wait for MongoDB write to propagate
    await asyncio.sleep(1)

    # VERIFY via db_query
    list_reply = await chat("List all Backend interns")
    assert "testbot" in list_reply.lower() or "alpha" in list_reply.lower(), (
        f"Newly added employee not found in db_query: {list_reply}"
    )

    # DELETE
    del_reply = await chat("Delete employee TestBot Alpha")
    assert "✅" in del_reply or "deleted" in del_reply.lower(), (
        f"CRUD delete did not confirm success: {del_reply}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 — CRUD: missing required fields returns helpful error
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_crud_add_missing_fields():
    """
    CRUD add with incomplete data must return a clear error listing
    missing required fields — not a crash or silent failure.
    """
    reply = await chat("Add employee John")  # missing email, role, position, contact

    assert (
        "❌" in reply
        or "missing" in reply.lower()
        or "required" in reply.lower()
        or "provide" in reply.lower()
    ), f"CRUD missing-fields check did not return expected error: {reply}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5 — Email flow: full happy path (cancel at preview)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_email_flow_full():
    """
    Email flow: extract → ask_optional (CC/BCC) → preview → cancel.
    No real email is sent. Verifies stage transitions and clean teardown.
    """
    # Trigger with enough info to skip ask_required
    r1 = await chat("Send an email to test@example.com about project update")
    assert r1 and len(r1) > 5, f"Email flow start returned empty: {r1}"

    # Answer CC/BCC question (ask_optional stage)
    r2 = await chat("No CC or BCC needed")
    assert r2 and len(r2) > 5, f"Email ask_optional stage returned empty: {r2}"

    # Cancel at preview — no real send
    r3 = await chat("Cancel")
    assert "cancel" in r3.lower() or "✅" in r3, (
        f"Email cancel did not confirm cancellation: {r3}"
    )

    # Flow must be cleared — next message should not mention a pending email
    r4 = await chat("Hello")
    assert "pending" not in r4.lower(), (
        f"Email flow not cleared after cancel: {r4}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6 — Email side question gate
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_email_side_question_gate():
    """
    Gate logic: email flow active at preview stage →
    unrelated question → gate=side_question →
    process_query_direct() answers it → resume prompt appended.
    """
    # Reach preview stage
    await chat("Send email to test@example.com about leave request")
    await chat("No CC or BCC")

    # Ask a completely unrelated side question
    side_reply = await chat("What is 2 + 2?")

    assert side_reply and len(side_reply) > 5, "Side question got empty reply"

    # Must include a resume reminder about the pending email
    resume_keywords = ["email", "pending", "still", "by the way"]
    assert any(kw in side_reply.lower() for kw in resume_keywords), (
        f"Side question reply missing email resume prompt: {side_reply}"
    )

    # Clean up
    await chat("Cancel")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7 — Calendar flow: full happy path (cancel at preview)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_calendar_flow_full():
    """
    Calendar flow: extract → ask_attendees → preview → cancel.
    No real Google Calendar event is created.
    """
    r1 = await chat("Schedule a team meeting tomorrow at 3pm")
    assert r1 and len(r1) > 5, f"Calendar flow start returned empty: {r1}"

    # Respond to attendee question
    r2 = await chat("No attendees needed")
    assert r2 and len(r2) > 5, f"Calendar attendee stage returned empty: {r2}"

    # Cancel at preview
    r3 = await chat("Cancel")
    assert "cancel" in r3.lower() or "✅" in r3, (
        f"Calendar cancel did not confirm: {r3}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8 — Calendar side question gate
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_calendar_side_question_gate():
    """
    Gate logic: calendar flow active at preview stage →
    db_query side question → gate=side_question →
    process_query_direct() answers it → resume prompt appended.
    """
    # Reach preview stage.
    # Do NOT mention any names in the initial message — the LLM will extract
    # them as raw_attendees and ask_attendees will try to resolve their emails.
    await chat("Schedule a standup meeting tomorrow at 10am")

    # ask_yn fires: "Would you like to add any attendees?"
    # Reply with a hard "no" — matches ^(no|nope|nah|skip|none|...) regex in ask_attendees
    await chat("no")

    # Now at preview stage — ask a side question (db_query intent)
    side_reply = await chat("List all interns")

    assert side_reply and len(side_reply) > 5, "Calendar side question got empty reply"

    # Gate must have fired: side_question → process_query_direct → resume prompt appended
    resume_keywords = ["calendar", "event", "pending", "still", "by the way", "standup", "meeting"]
    assert any(kw in side_reply.lower() for kw in resume_keywords), (
        f"Calendar side question reply missing resume prompt: {side_reply}"
    )

    # Clean up
    await chat("Cancel")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9 — Health check
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_endpoint():
    """Verify /chat/health returns status=ok with expected fields."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE_URL}/chat/health",
            params={"user_id": USER_ID},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "thread_id" in data
    assert "message_count" in data





# (venv) PS C:\Users\shlok\OneDrive\Desktop\fastmcp> pytest tests/test_chatbot.py -v
# ================================================ test session starts =================================================
# platform win32 -- Python 3.12.2, pytest-9.0.3, pluggy-1.6.0 -- C:\Users\shlok\OneDrive\Desktop\fastmcp\venv\Scripts\python.exe
# cachedir: .pytest_cache
# rootdir: C:\Users\shlok\OneDrive\Desktop\fastmcp
# configfile: pytest.ini
# plugins: anyio-4.12.1, langsmith-0.7.3, asyncio-1.3.0
# asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
# collected 9 items                                                                                                     

# tests/test_chatbot.py::test_rag_employee_lookup PASSED                                                          [ 11%]
# tests/test_chatbot.py::test_db_query_list_interns PASSED                                                        [ 22%]
# tests/test_chatbot.py::test_crud_add_and_delete PASSED                                                          [ 33%]
# tests/test_chatbot.py::test_crud_add_missing_fields PASSED                                                      [ 44%]
# tests/test_chatbot.py::test_email_flow_full PASSED                                                              [ 55%]
# tests/test_chatbot.py::test_email_side_question_gate PASSED                                                     [ 66%]
# tests/test_chatbot.py::test_calendar_flow_full PASSED                                                           [ 77%]
# tests/test_chatbot.py::test_calendar_side_question_gate PASSED                                                  [ 88%]
# tests/test_chatbot.py::test_health_endpoint PASSED                                                              [100%]

# =========================================== 9 passed in 396.85s (0:06:36) ============================================ 
# (venv) PS C:\Users\shlok\OneDrive\Desktop\fastmcp> 
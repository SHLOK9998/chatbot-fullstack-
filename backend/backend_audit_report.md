# Backend Audit Report — Full Analysis & Fixes

> Codebase: FastAPI + MongoDB Atlas + Redis + Gemini Embeddings + Google APIs  
> Files reviewed: `main.py`, `ingestion_service.py`, `embedding_service.py`, `crud_service.py`, `db_query_service.py`, `gmail_read_service.py`, `tasks_service.py`, `chat_service.py`, `mongo_rag_service.py`, `intent_detector.py`

---

## 1. Slow Startup

### Root Cause
In `main.py` (lifespan), startup does four sequential blocking things:

```python
await connect_db()         # 1. MongoDB connect
await connect_redis()      # 2. Redis connect
await asyncio.to_thread(initialize_knowledge_base)  # 3. Full ingestion
await initialize_session(DEFAULT_USER)              # 4. Session + summary flush
```

The **bottleneck is Step 3**. `initialize_knowledge_base()` opens a *brand new* `pymongo.MongoClient` on every startup (in `_get_sync_db()`), then:
- Fetches the manifest from MongoDB (sync call)
- If the Excel changed → reads Excel → calls **Gemini batch embed** for all rows → upserts N documents one-by-one in a Python `for` loop
- Even on a "hash match / skip" path, the `_get_sync_db()` call creates a fresh TCP connection to Atlas, which has a cold-start cost (~500ms–2s depending on Atlas region latency)

Additionally, `EmbeddingService` is module-level instantiated in both `ingestion_service.py` and `crud_service.py`, which initialises `GoogleGenerativeAIEmbeddings` at import time (triggers network/auth setup).

### Fix

**a) Make ingestion truly lazy and non-blocking at startup:**

```python
# main.py — lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logger()
    await connect_db()
    await connect_redis()
    await initialize_session(DEFAULT_USER)
    # Fire ingestion in background AFTER app is ready to serve
    asyncio.create_task(_background_ingest())
    yield
    await end_session(DEFAULT_USER)
    await close_redis()
    await close_db()

async def _background_ingest():
    try:
        await asyncio.to_thread(initialize_knowledge_base)
        logger.info("Background ingestion complete.")
    except Exception as e:
        logger.error("Background ingestion failed: %s", e)
```

This makes the app ready to accept requests within 1–2 seconds. Ingestion happens in parallel.

**b) Reuse the sync pymongo client instead of recreating it:**

```python
# ingestion_service.py
_sync_client = None
_sync_db_handle = None

def _get_sync_db():
    global _sync_client, _sync_db_handle
    if _sync_client is None:
        import pymongo
        _sync_client = pymongo.MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
        _sync_db_handle = _sync_client[settings.MONGO_DB_NAME]
    return _sync_client, _sync_db_handle
```

**c) Lazy-init embeddings (already partly done — just don't instantiate at module level):**

```python
# Instead of module-level:
embedding_service = EmbeddingService()

# Use a singleton getter:
_embedding_service = None
def get_embedding_service():
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
```

**Expected result:** Startup drops from ~10–20s to ~2–3s. Ingestion completes in the background without blocking first requests.

---

## 2. Ingestion & Embedding Pipeline (RAG Response Quality)

### Root Cause

**a) Content text is a flat, repetitive sentence:**
```python
content = (
    f"{name} {middle_name} {lastname} is a "
    f"{position} in the {department} department. "
    f"For contact, reach them at {email} or {contact}. ..."
)
```
This template produces nearly identical sentence structures across employees, which compresses the embedding space — similar vectors for semantically different employees. The model can't distinguish well.

**b) Only one document per employee.** If the user asks "who is the AIML intern from Surat?", the single document must carry department + position + address all at once. If any field is empty or slightly differently named, retrieval fails.

**c) `top_k=5` with `numCandidates=50` is too tight.** For a small KB (< 200 employees), this is fine, but the `score >= 0.75` threshold in `chat_service.py` is very aggressive and silently drops valid results.

**d) No chunking strategy.** For future-proofing (PDF, Slack history, documents), the current per-row ingestion has no chunking.

### Fix

**a) Richer, structured content text with keyword injection:**

```python
def _build_content_text(row: dict) -> str:
    name = f"{row.get('name','')} {row.get('middle_name','')} {row.get('lastname','')}".strip()
    dept = row.get('department', '')
    pos  = row.get('position', '')
    addr = row.get('address', '')
    email = row.get('email', '')
    contact = row.get('contact', '')
    slackid = row.get('slackid', '')
    github  = row.get('github', '')

    # Multi-perspective content — helps semantic retrieval from different query angles
    return (
        f"Employee profile: {name}.\n"
        f"Role: {pos} in the {dept} department.\n"
        f"Location: {addr}.\n"
        f"Contact: Email is {email}, phone is {contact}.\n"
        f"Online: Slack @{slackid}, GitHub @{github}.\n"
        f"Keywords: {dept} {pos} {addr} {name}"
    )
```

**b) Raise `numCandidates` and lower the score threshold:**

```python
# mongo_rag_service.py
vector_search_stage = {
    "$vectorSearch": {
        "index":         "employee_vector_index",
        "path":          "embedding",
        "queryVector":   query_vector,
        "numCandidates": max(top_k * 20, 100),  # was top_k * 10
        "limit":         top_k,
    }
}

# chat_service.py — lower threshold for better recall
kb_results = [r for r in kb_results if r.get("score", 0) >= 0.60]  # was 0.75
```

**c) Add a `text` index for hybrid search fallback:**

```python
# Run once in a setup script or admin endpoint:
await db["employee_kb"].create_index([
    ("metadata.name", "text"),
    ("metadata.department", "text"),
    ("metadata.position", "text"),
    ("content", "text"),
])
```

Then in `mongo_rag_service.py`, if vector search returns 0 results, fallback to text search:

```python
if not results:
    logger.info("[RAG] Vector returned nothing — falling back to text search")
    cursor = collection.find(
        {"$text": {"$search": query}},
        {"score": {"$meta": "textScore"}, "content": 1, "metadata": 1}
    ).sort([("score", {"$meta": "textScore"})]).limit(top_k)
    results = await cursor.to_list(length=top_k)
```

**d) Re-embed on content change only** (already done via MD5 manifest — good, keep it).

---

## 3. DB Query — Better Approach

### Root Cause

`handle_db_query` in `db_query_service.py` does **3 sequential LLM/DB calls** before returning a result:

```
1. _get_schema_values()  → 4 × MongoDB distinct() calls
2. _extract_filters()    → 1 × LLM call (Groq)
3. collection.find()     → MongoDB query
```

**Problems:**
- `distinct()` on large collections is slow and uncached — called on every query
- LLM filter extraction adds ~1–2s latency even for simple queries like "list all AIML interns"
- `cursor.to_list(length=500)` fetches all 500 docs into memory before formatting — wasteful for large teams
- `_format_results()` outputs a flat numbered list, not useful for follow-up actions

### Fix

**a) Cache schema values in Redis (TTL = 10 minutes):**

```python
async def _get_schema_values() -> dict:
    r = get_redis()
    if r:
        cached = await r.get("db_schema_cache")
        if cached:
            return json.loads(cached)

    db = get_db()
    collection = db["employee_kb"]
    departments = await collection.distinct("metadata.department")
    positions   = await collection.distinct("metadata.position")
    addresses   = await collection.distinct("metadata.address")
    names       = await collection.distinct("metadata.name")

    schema = {
        "departments": sorted([d for d in departments if d]),
        "positions":   sorted([p for p in positions   if p]),
        "addresses":   sorted([a for a in addresses   if a]),
        "names":       sorted([n for n in names       if n]),
    }

    if r:
        await r.set("db_schema_cache", json.dumps(schema), ex=600)
    return schema
```

**b) Invalidate cache when employee is added/updated/deleted** (in `crud_service.py`):

```python
async def _invalidate_schema_cache():
    r = get_redis()
    if r:
        await r.delete("db_schema_cache")
```

**c) Use projection + pagination instead of fetching all 500 docs:**

```python
cursor = collection.find(mongo_filter, projection).limit(50)  # cap at 50
employees = await cursor.to_list(length=50)

if len(employees) == 50:
    # Optionally count total
    total = await collection.count_documents(mongo_filter)
    footer = f"\n\n_Showing 50 of {total} employees. Ask for more or narrow by department/location._"
else:
    footer = ""
```

**d) Richer formatted output with grouped display:**

```python
def _format_results(employees: list[dict], footer: str = "") -> str:
    if not employees:
        return "No employees found matching that criteria."

    count = len(employees)
    lines = [f"**{count} employee{'s' if count != 1 else ''} found:**\n"]

    # Group by department for cleaner reading
    from collections import defaultdict
    by_dept = defaultdict(list)
    for e in employees:
        dept = e.get("metadata", {}).get("department", "Other")
        by_dept[dept].append(e)

    for dept, emps in sorted(by_dept.items()):
        lines.append(f"\n**{dept}** ({len(emps)})")
        for e in emps:
            m = e.get("metadata", {})
            name = m.get("name", "?")
            pos  = m.get("position", "")
            email = m.get("email", "")
            contact = m.get("contact", "")
            row = f"- {name}"
            if pos:    row += f" | {pos}"
            if email:  row += f" | {email}"
            if contact: row += f" | {contact}"
            lines.append(row)

    return "\n".join(lines) + footer
```

---

## 4. Add New Employee — Rigid Flow

### Root Cause

`handle_crud()` → `_extract_action()` → `_add_employee()` does everything in one shot. If the user says `"Add John"`, it:
1. Extracts partial data (only name)
2. Hits `_check_required_add_fields()` — finds 4 missing fields
3. Returns a hard error message

There is **no conversational collection** — the user must provide all fields in a single message or be rejected. There is also no confirmation before writing to the database.

### Fix

Implement a multi-turn collection flow using Redis state, similar to how `email_handler.py` and `calendar_handler.py` work.

**a) Add a `crud_state` in Redis for in-progress employee additions:**

```python
# In crud_service.py

REQUIRED_FIELDS = ["name", "email", "department", "position", "contact"]
FIELD_PROMPTS = {
    "name":       "What is the employee's full name?",
    "email":      "What is their email address?",
    "department": "Which department? (e.g. AIML, DevOps, Full Stack, Backend)",
    "position":   "What is their position? (e.g. Intern, Developer, Senior Developer)",
    "contact":    "What is their contact number?",
}

async def _get_crud_state(user_id: str) -> dict:
    r = get_redis()
    if r:
        raw = await r.get(f"crud_state:{user_id}")
        if raw:
            return json.loads(raw)
    return {}

async def _set_crud_state(user_id: str, state: dict):
    r = get_redis()
    if r:
        await r.set(f"crud_state:{user_id}", json.dumps(state), ex=600)

async def _clear_crud_state(user_id: str):
    r = get_redis()
    if r:
        await r.delete(f"crud_state:{user_id}")

async def is_crud_active(user_id: str) -> bool:
    state = await _get_crud_state(user_id)
    return bool(state.get("operation") == "add" and not state.get("confirmed"))
```

**b) Update `handle_crud` to start a flow:**

```python
async def handle_crud(query: str, user_id: str) -> str:
    # Check if we're mid-collection
    state = await _get_crud_state(user_id)

    if state.get("operation") == "add":
        return await _continue_add_flow(query, user_id, state)

    action = await _extract_action(query)
    if not action:
        return "I couldn't understand the change you want. Try: \"Add new employee\", \"Update Anand's contact\", or \"Delete employee John\"."

    operation = action.get("operation", "").lower()

    if operation == "add":
        data = action.get("data", {})
        missing = _check_required_add_fields(data)
        if not missing:
            # All fields present — ask for confirmation
            await _set_crud_state(user_id, {"operation": "add", "data": data, "confirmed": False})
            return _format_add_preview(data)
        else:
            # Start collection flow
            await _set_crud_state(user_id, {"operation": "add", "data": data, "confirmed": False})
            next_field = missing[0]
            return (
                f"Starting employee addition. I have some details already.\n"
                f"{FIELD_PROMPTS[next_field]}"
            )
    # ... update/delete remain unchanged
```

**c) Flow continuation:**

```python
async def _continue_add_flow(query: str, user_id: str, state: dict) -> str:
    data = state.get("data", {})

    # Check if awaiting confirmation
    if state.get("awaiting_confirmation"):
        if any(w in query.lower() for w in ["yes", "confirm", "ok", "sure", "add", "go ahead"]):
            await _clear_crud_state(user_id)
            return await _add_employee(data)
        elif any(w in query.lower() for w in ["no", "cancel", "stop", "abort"]):
            await _clear_crud_state(user_id)
            return "Employee addition cancelled."
        else:
            return _format_add_preview(data) + "\n\nReply **yes** to confirm or **no** to cancel."

    # Fill in the next missing field from user input
    missing = _check_required_add_fields(data)
    if missing:
        field = missing[0]
        data[field] = query.strip()
        state["data"] = data
        remaining = _check_required_add_fields(data)
        if remaining:
            await _set_crud_state(user_id, state)
            return FIELD_PROMPTS[remaining[0]]
        else:
            state["awaiting_confirmation"] = True
            await _set_crud_state(user_id, state)
            return _format_add_preview(data)

def _format_add_preview(data: dict) -> str:
    return (
        f"Here's a summary of the new employee:\n\n"
        f"- **Name:** {data.get('name')}\n"
        f"- **Email:** {data.get('email')}\n"
        f"- **Department:** {data.get('department')}\n"
        f"- **Position:** {data.get('position')}\n"
        f"- **Contact:** {data.get('contact')}\n"
        f"- **Address:** {data.get('address', 'Not provided')}\n\n"
        f"Reply **yes** to add, or **no** to cancel."
    )
```

Also add `is_crud_active` check in `chat_service.py`'s `process_query` routing, similar to email/calendar flow.

---

## 5. Email — Shows Only 5 Fixed Unread Emails

### Root Cause

In `gmail_read_service.py`:

```python
results = await asyncio.to_thread(
    _fetch_emails, service, gmail_query, max_results=5  # hardcoded 5
)
```

And `_fetch_emails()` always uses `maxResults=max_results` with **no support for pagination** or dynamic count.

The intent parser does recognise `"from <name>"` but uses a very naive regex that breaks on email addresses, multi-word names, and quoted names. Also, there's no support for `"emails about <topic>"`, `"emails with attachment"`, `"starred emails"`, etc.

### Fix

**a) Make `max_results` dynamic based on intent:**

```python
async def handle_gmail_read(query: str, user_id: str) -> str:
    ...
    # Parse how many the user wants
    count_match = re.search(r"\b(\d+)\s*(email|mail|message)s?\b", query_lower)
    max_results = int(count_match.group(1)) if count_match else 10  # default 10, not 5

    # Cap at 25 to avoid huge API bills
    max_results = min(max_results, 25)
```

**b) Expand Gmail query building:**

```python
def _build_gmail_query(query_lower: str) -> tuple[str, str]:
    """Returns (gmail_query_string, human_readable_label)"""

    if "unread" in query_lower:
        return "is:unread", "Unread emails"

    if "today" in query_lower:
        return "newer_than:1d", "Today's emails"

    if "this week" in query_lower:
        return "newer_than:7d", "This week's emails"

    if "attachment" in query_lower or "attached" in query_lower:
        return "has:attachment", "Emails with attachments"

    if "starred" in query_lower or "important" in query_lower:
        return "is:starred", "Starred emails"

    # from: support — handles email addresses AND names
    from_match = re.search(r"\bfrom\s+([^\s,]+(?:\s+[^\s,]+)*)", query_lower)
    if from_match:
        sender = from_match.group(1).strip()
        return f"from:{sender}", f"Emails from {sender}"

    # about/subject support
    about_match = re.search(r"\b(?:about|subject|regarding|re:?)\s+(.+)", query_lower)
    if about_match:
        topic = about_match.group(1).strip().split()[0]  # first word as subject keyword
        return f"subject:{topic}", f"Emails about {topic}"

    # replied to / response check
    if any(w in query_lower for w in ["reply", "replied", "response", "responded"]):
        name_match = re.search(r"(?:from|by)\s+(\w+)", query_lower)
        if name_match:
            return f"from:{name_match.group(1)} is:inbox", f"Replies from {name_match.group(1)}"
        return "is:inbox newer_than:7d", "Recent inbox emails"

    return "is:inbox", "Inbox emails"
```

**c) Improve the output format:**

```python
def _format_emails(emails: list[dict], label: str) -> str:
    if not emails:
        return f"No emails found for: {label}."

    lines = [f"**{label}** ({len(emails)} found)\n"]
    for i, msg in enumerate(emails, 1):
        sender  = msg.get("from", "Unknown")
        subject = msg.get("subject", "(no subject)")
        date    = msg.get("date", "")
        snippet = msg.get("snippet", "")
        # Trim date to just "May 10" style
        date_short = date[:16] if date else ""
        lines.append(
            f"**{i}. {subject}**\n"
            f"From: {sender}\n"
            f"Date: {date_short}\n"
            f"_{snippet}_\n"
        )
    return "\n---\n".join(lines)
```

---

## 6. Tasks — Unformatted and Bad Flow

### Root Cause

In `tasks_service.py`:

- `_list_tasks()` returns a flat numbered list with no status, no priority, no due date formatting
- `_add_task()` strips action words with a rough regex — easily fails on "add a task to review the Q3 report by Friday"
- `_complete_task()` does a naive `in` substring match — "complete buy" would match "buy milk" but also "buy apples"
- No due date support when adding tasks — the Google Tasks API supports `due` (RFC 3339) but it's never set
- No task list selection — always uses `@default`
- Error messages are raw exception strings shown to user

### Fix

**a) Better task listing with formatted output:**

```python
async def _list_tasks(service, user_id: str) -> str:
    try:
        result = await asyncio.to_thread(
            lambda: service.tasks().list(
                tasklist="@default",
                showCompleted=False,
                showHidden=False,
                maxResults=20,
            ).execute()
        )
        items = result.get("items", [])
        if not items:
            return "You have no pending tasks. To add one, say \"Add task: review the Q3 report\"."

        # Sort by due date if available
        def due_key(t):
            return t.get("due", "9999")
        items.sort(key=due_key)

        lines = [f"**Your tasks** ({len(items)} pending)\n"]
        today = datetime.now(timezone.utc).date()

        for i, task in enumerate(items, 1):
            title = task.get("title", "(untitled)")
            due   = task.get("due", "")
            notes = task.get("notes", "")

            if due:
                due_date = datetime.fromisoformat(due.replace("Z", "+00:00")).date()
                days_left = (due_date - today).days
                if days_left < 0:
                    due_str = f"⚠ Overdue ({due_date.strftime('%b %d')})"
                elif days_left == 0:
                    due_str = "Due today"
                elif days_left == 1:
                    due_str = "Due tomorrow"
                else:
                    due_str = f"Due {due_date.strftime('%b %d')}"
            else:
                due_str = "No due date"

            lines.append(f"{i}. **{title}**\n   {due_str}")
            if notes:
                lines.append(f"   _{notes[:80]}_")

        lines.append("\nSay \"complete [task name]\" to mark a task done, or \"add task: [description]\" to add a new one.")
        return "\n".join(lines)

    except Exception as e:
        logger.error("[Tasks] List failed | user=%s | %s", user_id, e)
        return "I couldn't fetch your tasks right now. Please try again in a moment."
```

**b) Better task addition with due date parsing:**

```python
from utils.time_parser import parse_time  # already exists in codebase

async def _add_task(service, query: str, user_id: str) -> str:
    # Strip action prefix
    title = re.sub(
        r"(?i)^(add\s+a?\s*task:?\s*|create\s+a?\s*(task|to-?do):?\s*|remind\s+me\s+to\s*|note:?\s*)",
        "", query
    ).strip()

    if not title:
        return "What task would you like to add? Try: \"Add task: review the Q3 report by Friday\""

    # Extract due date from title if present
    due_body = None
    due_match = re.search(r"\bby\s+(.+)$", title, re.IGNORECASE)
    if due_match:
        date_str = due_match.group(1).strip()
        parsed = parse_time(date_str)  # your existing time parser
        if parsed:
            due_body = parsed.strftime("%Y-%m-%dT00:00:00.000Z")
            title = title[:due_match.start()].strip()

    body = {"title": title, "status": "needsAction"}
    if due_body:
        body["due"] = due_body

    try:
        await asyncio.to_thread(
            lambda: service.tasks().insert(tasklist="@default", body=body).execute()
        )
        due_note = f" (due: {due_match.group(1)})" if due_match else ""
        return f"Task added: **{title}**{due_note}\n\nSay \"show my tasks\" to see all pending tasks."
    except Exception as e:
        logger.error("[Tasks] Add failed | user=%s | %s", user_id, e)
        return "I couldn't add the task right now. Please try again."
```

**c) Better task completion with disambiguation:**

```python
async def _complete_task(service, query: str, user_id: str) -> str:
    title_hint = re.sub(
        r"(?i)^(complete|done|finish|mark\s+(as\s+)?(done|complete|finished))\s*",
        "", query
    ).strip()

    if not title_hint:
        return "Which task would you like to mark as complete? Say \"complete [task name]\"."

    try:
        result = await asyncio.to_thread(
            lambda: service.tasks().list(
                tasklist="@default", showCompleted=False, maxResults=20
            ).execute()
        )
        items = result.get("items", [])

        # Find all matches, not just the first
        matches = [t for t in items if title_hint.lower() in t.get("title", "").lower()]

        if not matches:
            return (
                f"No pending task found matching \"{title_hint}\".\n\n"
                f"Say \"show my tasks\" to see your full list."
            )

        if len(matches) > 1:
            task_list = "\n".join(f"{i+1}. {m['title']}" for i, m in enumerate(matches))
            return (
                f"I found {len(matches)} tasks matching \"{title_hint}\":\n\n{task_list}\n\n"
                f"Please be more specific — say \"complete\" followed by the exact title."
            )

        match = matches[0]
        await asyncio.to_thread(
            lambda: service.tasks().update(
                tasklist="@default",
                task=match["id"],
                body={**match, "status": "completed"},
            ).execute()
        )
        return f"Marked as complete: **{match['title']}**"

    except Exception as e:
        logger.error("[Tasks] Complete failed | user=%s | %s", user_id, e)
        return "I couldn't update the task right now. Please try again."
```

---

## 7. Simple/Normal Replies Are Too Short and Unformatted

### Root Cause

In `chat_service.py`, the system prompt says:

```python
"Always be clear, concise, and helpful in your replies.\n"
"IMPORTANT: Never use emojis in any response. Plain text only.\n"
```

And the RAG prompt adds:

```python
"- Answer ONLY what the user asked. Do not add unrequested details.\n"
"- Be concise. One sentence if possible.\n"  # ← this is the culprit
```

The `_handle_llm_chat` fallback inherits the "be concise" system prompt, causing even open-ended questions like "explain JWT tokens" or "what are best practices for REST APIs" to get a single-sentence answer.

There is also **no instruction to use Markdown formatting** — no bullet points, no headers, no code blocks — even when the question clearly warrants it.

### Fix

**a) Split the system prompt into a base (always on) and a focused layer (RAG-specific):**

```python
# chat_service.py

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

def _build_rag_instructions() -> str:
    return (
        "--- INSTRUCTIONS ---\n"
        "- Use the EMPLOYEE KNOWLEDGE BASE for questions about specific people's details.\n"
        "- For general knowledge questions, answer from your own knowledge — do NOT mention employees.\n"
        "- NEVER hallucinate any name, email, phone, or detail not in the KB entries above.\n"
        "- If the answer is not in the KB, say: 'I don't have that information.'\n"
        "- If the user asks for one specific field (email, phone), return just that field.\n"
        "- If the user asks an open question (explain, describe, how does, list), give a full structured answer.\n"
    )
```

**b) Remove the "one sentence" restriction entirely.** Replace with context-aware guidance:

```python
# In _handle_rag(), replace:
"- Be concise. One sentence if possible.\n"

# With:
"- Match answer length to question complexity. Short questions get short answers. "
"Open questions get structured, detailed answers with bullet points or numbered steps where appropriate.\n"
```

**c) For `_handle_llm_chat`, add an explicit formatting nudge:**

```python
messages = [
    SystemMessage(content="".join(parts)),
    HumanMessage(content=(
        query + "\n\n"
        "[Format your response appropriately: use bullet points for lists, "
        "numbered steps for sequences, code blocks for code, "
        "and headers for multi-section answers. Plain prose for simple questions.]"
    )),
]
```

---

## Summary Table

| # | Issue | Root Cause | Fix Complexity |
|---|-------|-----------|----------------|
| 1 | Slow startup | Sync ingestion + new MongoClient on every boot | Medium — background task + singleton client |
| 2 | Poor RAG quality | Flat content text, aggressive 0.75 score threshold | Medium — richer text template + hybrid search fallback |
| 3 | DB query latency | 4 distinct() calls uncached + no pagination | Easy — Redis cache for schema + result cap |
| 4 | Rigid employee add | Single-shot extraction, no conversational flow | Medium-High — Redis state machine (same pattern as email/calendar) |
| 5 | Email: fixed 5 results | Hardcoded `max_results=5`, limited query parsing | Easy — dynamic count + expanded query builder |
| 6 | Tasks: bad formatting | Flat list, no due dates, naive regex, raw error strings | Medium — structured output + time parser + disambiguation |
| 7 | Short generic replies | "One sentence if possible" in system prompt | Easy — split system prompt, remove length restriction |

---

*All changes are backward-compatible. No schema migrations required. Redis dependency remains optional — all fixes degrade gracefully if Redis is unavailable.*

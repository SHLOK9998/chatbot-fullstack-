# utils/intent_detector.py
import logging
from langchain_groq import ChatGroq
from core.config import settings

logger = logging.getLogger(__name__)

_classifier_llm = ChatGroq(
    temperature=0,
    model_name=settings.MODEL_NAME,
    groq_api_key=settings.GROQ_API_KEY,
)

_INTENT_PROMPT = """You are a strict intent classifier for a personal AI assistant.
Classify the user's message into EXACTLY ONE of these intents:

1. email_send
   → User explicitly wants to SEND, COMPOSE, WRITE, or FORWARD an email to someone.
   → Examples: "send email to John", "write an email to HR", "compose a mail to my boss"
   → NOT email_send: reading inbox, checking received emails, searching emails.

2. email_read
   → User wants to READ, CHECK, SEARCH, or VIEW emails they have RECEIVED.
   → Examples: "show my emails", "check my inbox", "any unread emails", "did John reply", "latest emails from HR"
   → NOT email_read: sending or composing a new email.

3. calendar
   → User explicitly wants to SCHEDULE, CREATE, SET UP, or ADD a calendar event/meeting/reminder.
   → Examples: "schedule a meeting", "create an event", "add to calendar", "book a meeting for Monday"
   → NOT calendar: just mentioning a date, asking about someone's availability.

4. tasks
   → User wants to CREATE, ADD, VIEW, LIST, or COMPLETE a personal to-do task.
   → Examples: "add a task", "create a to-do", "show my tasks", "mark task as done", "what tasks do I have"
   → NOT tasks: employee records, calendar events, emails.
   → Key rule: "add a task" = tasks. "add an employee" = crud. Never confuse these.

5. db_query
   → User wants a LIST, COUNT, or GROUP of EMPLOYEES from the company database.
   → Examples: "list all employees", "how many interns", "show all developers", "count employees in Surat"
   → Must be asking for MULTIPLE employees or a count — not a single person's details.
   → NOT db_query: personal tasks, emails, calendar, single person lookup.

6. crud
   → User wants to ADD, UPDATE, MODIFY, or DELETE a specific EMPLOYEE RECORD in the database.
   → Examples: "add new employee", "update Anand's salary", "delete employee John", "change Shlok's role"
   → Must target an EMPLOYEE RECORD specifically with a clear action word.
   → NOT crud: personal tasks ("add a task"), emails, calendar events.

7. default
   → Everything else:
   → Questions about a specific person's details ("what is Anand's phone number?")
   → General knowledge questions ("how does React work?")
   → Conversation, greetings, thanks
   → Anything not clearly matching 1-6 above

Rules:
- When in doubt → default
- "add a task" or "my tasks" or "to-do" → tasks (NEVER crud)
- "show my emails" or "check inbox" or "unread" → email_read (NEVER email_send)
- "send email" or "write email" or "compose" → email_send (NEVER email_read)
- Single person info queries → default
- Never guess. Never ask questions.
- Return ONLY one word: email_send, email_read, calendar, tasks, db_query, crud, default

User message: {query}

Intent:
"""


def detect_intent(query: str) -> str:
    if not query or not query.strip():
        return "default"

    clean_query = query.strip()
    logger.info("[IntentDetector] Classifying: '%s'", clean_query[:100])

    try:
        prompt   = _INTENT_PROMPT.format(query=clean_query)
        response = _classifier_llm.invoke(prompt)
        raw      = (response.content if hasattr(response, "content") else str(response)).strip().lower()
        raw      = raw.strip(".,!?\"' ")

        valid = {"email_send", "email_read", "calendar", "tasks", "db_query", "crud", "default"}
        if raw in valid:
            logger.info("[IntentDetector] Intent = %s", raw)
            return raw

        logger.warning("[IntentDetector] Unexpected output '%s' — fallback to default", raw)
        return "default"

    except Exception as e:
        logger.exception("[IntentDetector] Failed — fallback to default: %s", e)
        return "default"

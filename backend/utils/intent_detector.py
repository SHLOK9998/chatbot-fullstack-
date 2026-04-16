# utils/intent_detector.py
import asyncio
import logging
from langchain_openai import ChatOpenAI
from core.config import settings

logger = logging.getLogger(__name__)

# Use the stronger OpenAI-compatible model for intent detection —
# it makes fewer misclassifications on ambiguous queries like
# "list something" which Groq/llama confuses with tasks intent.
_classifier_llm = ChatOpenAI(
    model=settings.OPENAI_MODEL_NAME,
    api_key=settings.API_FOR_OPENAI,
    base_url="https://api.groq.com/openai/v1",
    temperature=0,
)

_INTENT_PROMPT = """You are a strict intent classifier for a personal AI assistant.
Classify the user's message into EXACTLY ONE of these intents:

1. email_send
   → User explicitly wants to SEND, COMPOSE, WRITE, or FORWARD an email to someone.
   → Examples: "send email to John", "write an email to HR", "compose a mail to my boss"
   → NOT email_send: reading inbox, checking received emails, searching emails.

2. email_read
   → User wants to READ, CHECK, SEARCH, or VIEW emails they have RECEIVED.
   → Examples: "show my emails", "check my inbox", "any unread emails", "did John reply"
   → NOT email_read: sending or composing a new email.

3. calendar
   → User explicitly wants to SCHEDULE, CREATE, SET UP, or ADD a calendar event/meeting/reminder.
   → Examples: "schedule a meeting", "create an event", "add to calendar", "book a meeting for Monday"
   → Examples: "book a slot", "set up a call", "arrange a meeting", "fix a time for standup", "block time for"
   → NOT calendar: just mentioning a date, asking about someone's availability.

4. tasks
   → User wants to CREATE, ADD, VIEW, LIST, or COMPLETE their OWN PERSONAL to-do tasks.
   → Examples: "add a task", "create a to-do", "show my tasks", "mark task as done", "what tasks do I have"
   → Examples: "remind me to call mom", "note to self: buy milk", "don't forget to submit report"
   → CRITICAL: "tasks" is ONLY for the user's own personal to-do list — NOT for listing employees, people, or company data.
   → NOT tasks: "list all interns", "list employees", "show me the devops team", "list something about employees/people/company"
   → Key rule: "add a task" = tasks. "add an employee" = crud. "list interns" = db_query. Never confuse these.

5. db_query
   → User wants to LIST, COUNT, or find out WHO is in a department/role/location from the company database.
   → Examples: "list all employees", "how many interns", "show all developers", "count employees in Surat"
   → Examples: "who is working in AIML", "who are the devops interns", "show me full stack team"
   → Examples: "give me list of AIML interns", "employees in Ahmedabad", "how many people in devops"
   → Examples: "list out all interns", "show me all employees", "list something" (when about people/employees)
   → Use this whenever the user asks about a GROUP of people or employees, even phrased as "list", "who is", or "who are".
   → NOT db_query: personal tasks, emails, calendar, single specific person's contact/email/details.

6. crud
   → User wants to ADD, UPDATE, MODIFY, or DELETE a specific EMPLOYEE RECORD in the database.
   → Examples: "add new employee", "update Anand's salary", "delete employee John", "change Shlok's role"
   → Must target an EMPLOYEE RECORD specifically with a clear action word.
   → NOT crud: personal tasks ("add a task"), emails, calendar events.

7. default
   → Everything else:
   → Questions about ONE specific named person's details ("what is Anand's phone number?", "show me Shlok's email")
   → General knowledge questions ("how does React work?")
   → Conversation, greetings, thanks
   → Anything not clearly matching 1-6 above
   → NOT default: any question asking about a group, department, or role — those are db_query

CONFLICT RESOLUTION — when in doubt between two intents, use these rules:
- "list/show/give me" + employees/interns/team/department/people → db_query (NOT tasks)
- "my tasks" / "add a task" / "to-do" / "todo" → tasks (NOT db_query)
- "list all X" where X is a type of employee or department → db_query
- "show my emails" / "check inbox" / "unread" → email_read (NOT email_send)
- "send email" / "write email" / "compose" → email_send (NOT email_read)
- Single named person info queries ("what is Anand's email") → default
- Never guess. Never ask questions.
- Return ONLY one word: email_send, email_read, calendar, tasks, db_query, crud, default

User message: {query}

Intent:
"""


async def detect_intent_async(query: str) -> str:
    """Async version — runs the LLM call in a thread to avoid blocking the event loop."""
    if not query or not query.strip():
        return "default"

    clean_query = query.strip()
    logger.info("[IntentDetector] Classifying: '%s'", clean_query[:100])

    try:
        prompt   = _INTENT_PROMPT.format(query=clean_query)
        response = await asyncio.to_thread(_classifier_llm.invoke, prompt)
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


def detect_intent(query: str) -> str:
    """
    Sync wrapper kept for compatibility with callers that cannot await.
    Internally runs the async version via asyncio.run() only if no loop is running,
    otherwise schedules it properly.
    """
    if not query or not query.strip():
        return "default"

    clean_query = query.strip()
    logger.info("[IntentDetector] Classifying (sync): '%s'", clean_query[:100])

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

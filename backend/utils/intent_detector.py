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

1. email
   → User explicitly wants to SEND, COMPOSE, WRITE, or FORWARD an email/message to someone.
   → Keywords: "send email", "write email", "email to", "mail to", "send a message to"
   → NOT email: asking for someone's email address, asking about emails received.

2. calendar
   → User explicitly wants to SCHEDULE, CREATE, SET UP, or ADD a calendar event/meeting/reminder.
   → Keywords: "schedule", "create event", "set meeting", "add to calendar", "book a meeting"
   → NOT calendar: just mentioning a time/date, asking about someone's schedule, planning without booking.

3. db_query
   → User wants a LIST, COUNT, or GROUP of employees based on criteria.
   → Keywords: "list all", "show all", "how many", "count", "give me all", "find all employees"
   → Must be asking for MULTIPLE employees or a count — not a single person's details.

4. crud
   → User wants to ADD, UPDATE, MODIFY, or DELETE a specific employee record.
   → Keywords: "add employee", "add new employee", "update [name]'s", "change [name]'s", "delete employee", "remove employee"
   → Must have a clear action word (add/update/delete/remove/change) targeting an employee record.
   → NOT crud: if no employee name or clear action is present.

5. default
   → Everything else:
   → Questions about a specific person ("what is Anand's phone number?")
   → General knowledge questions
   → Conversation / greetings
   → Asking for someone's email/contact/details (this is RAG, not crud)
   → Coding, documents, company info
   → Anything not clearly matching 1-4 above

Rules:
- When in doubt → default
- A question mark (?) almost always means default unless it's clearly "list all X" or "how many X"
- Single person info queries → default (handled by RAG)
- Never guess. Never ask questions.
- Return ONLY one word: email, calendar, db_query, crud, default

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

        # Strip any punctuation the LLM might add
        raw = raw.strip(".,!?\"' ")

        valid = {"email", "calendar", "db_query", "crud", "default"}
        if raw in valid:
            logger.info("[IntentDetector] Intent = %s", raw)
            return raw

        logger.warning("[IntentDetector] Unexpected output '%s' — fallback to default", raw)
        return "default"

    except Exception as e:
        logger.exception("[IntentDetector] Failed — fallback to default: %s", e)
        return "default"

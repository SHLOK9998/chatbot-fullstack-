"""
services/calendar_task/ask_attendees.py

MongoDB-powered attendee collection. All bugs from previous version fixed.

BUGS FIXED:

  BUG 1 — attendees never resolved from initial message:
    calendar_extract had no "attendees" role in its prompt, so names like
    "shlok and yash" were ignored. Now extract returns raw_attendees list.
    ask_attendees receives raw_attendees and resolves them immediately.

  BUG 2 — double LLM classification losing targets:
    When stage=ask_yn and user typed names, _classify_intent returned
    "has_attendees" then called _do_collect which re-ran _classify_attendee_mention
    on the same text without event context → LLM returned has_attendees=False
    → targets=[] → attendees=None.
    FIX: targets are now passed as a direct argument to _do_collect, never
    re-classified. The only time classification runs is when we genuinely
    need to parse a new reply from the user.

  BUG 3 — "cancel" exit word conflict:
    is_exit_request("cancel it") = True because "cancel" is in EXIT_WORDS.
    But "cancel it" is a legitimate response at the preview stage.
    FIX: calendar_flow_service skips is_exit_request when stage == "preview".

FLOW:
  on_extract   → called once with raw_attendees from calendar_extract
                 → resolve immediately via MongoDB
                 → all resolved : done (no question asked, straight to preview)
                 → some missing : resolve_N (ask for email)
                 → none given   : ask_yn

  ask_yn       → "Would you like to add any attendees?"
                 → no           : done
                 → yes          : ask for names/emails
                 → typed inline : parse + resolve

  collect      → user provided names/emails/roles in reply
                 → resolve via MongoDB → done or resolve_N

  resolve_N    → ask for email of person N (LLM-generated question)
                 → got email or name→lookup → next unresolved or done

  done         → (None, data) — flow moves to preview
"""

import asyncio
import json
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage
from core.database import get_db
from core.dependencies import get_llm

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_FAKE_LOCALS = {
    "all", "toall", "everyone", "team", "staff", "interns", "employees",
    "intern", "employee", "worker", "workers", "developer", "developers",
    "null", "none", "example", "test", "noreply", "no-reply", "user",
}


# ── Email helpers ─────────────────────────────────────────────────────────────

def _is_valid_email(email: str) -> bool:
    if not email or not isinstance(email, str):
        return False
    e = email.strip()
    if not _EMAIL_RE.fullmatch(e):
        return False
    return e.split("@")[0].lower() not in _FAKE_LOCALS


def _extract_emails(text: str) -> list[str]:
    if not text:
        return []
    seen, out = set(), []
    for e in _EMAIL_RE.findall(text):
        e = e.strip()
        if _is_valid_email(e) and e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ── MongoDB KB lookups ────────────────────────────────────────────────────────

async def _mongo_lookup_name(name: str) -> Optional[str]:
    """
    Find a person's email from employee_kb.
    Pass 1: metadata.name regex
    Pass 2: content text regex
    Pass 3: vector search with fuzzy 3-char prefix token match (handles typos)
    """
    if not name or not name.strip():
        return None

    col = get_db()["employee_kb"]
    name_clean = name.strip()

    # Pass 1 — metadata.name
    try:
        pat = re.compile(re.escape(name_clean), re.IGNORECASE)
        doc = await col.find_one({"metadata.name": {"$regex": pat}})
        if doc:
            email = (doc.get("metadata") or {}).get("email") or ""
            if not email:
                email = next(iter(_extract_emails(doc.get("content", ""))), "")
            if _is_valid_email(email):
                logger.info("[AskAttendees] name-regex '%s' → %s", name_clean, email)
                return email
    except Exception as e:
        logger.warning("[AskAttendees] name-regex failed '%s': %s", name_clean, e)

    # Pass 2 — content text
    try:
        pat2 = re.compile(re.escape(name_clean), re.IGNORECASE)
        doc = await col.find_one({"content": {"$regex": pat2}})
        if doc:
            email = (doc.get("metadata") or {}).get("email") or ""
            if not email:
                email = next(iter(_extract_emails(doc.get("content", ""))), "")
            if _is_valid_email(email):
                logger.info("[AskAttendees] content-regex '%s' → %s", name_clean, email)
                return email
    except Exception as e:
        logger.warning("[AskAttendees] content-regex failed '%s': %s", name_clean, e)

    # Pass 3 — vector search with exact name matching (no loose prefix guessing)
    try:
        from services.mongo_rag_service import search_employees
        results = await search_employees(name_clean, top_k=8)
        name_lower = name_clean.lower()
        name_parts = name_lower.split()
        for r in results:
            meta = r.get("metadata") or {}
            rname = str(meta.get("name") or "").lower().strip()
            if not rname:
                continue
            rname_parts = rname.split()
            # Require: full name exact match OR first name exact match
            # Never accept a 3-char prefix — that's what caused wrong-person emails
            full_match = (rname == name_lower)
            first_name_match = (
                name_parts and rname_parts and
                name_parts[0] == rname_parts[0] and
                len(name_parts[0]) >= 4  # avoid single-char / very short first names
            )
            if full_match or first_name_match:
                email = meta.get("email") or ""
                if not email:
                    email = next(iter(_extract_emails(r.get("content", ""))), "")
                if _is_valid_email(email):
                    logger.info("[AskAttendees] vector '%s' → %s (matched '%s')", name_clean, email, rname)
                    return email
    except Exception as e:
        logger.warning("[AskAttendees] vector failed '%s': %s", name_clean, e)

    return None


async def _mongo_group_lookup(label: str) -> list[str]:
    """
    Resolve a role/team/dept label to a list of emails from employee_kb.
    Pass 1: $or regex on metadata role fields
    Pass 2: Atlas vector search filtered by label tokens

    IMPORTANT: Common English words (stopwords) are excluded from label_tokens
    so words like "add", "all", "the", "employees" don't match every document.
    """
    col = get_db()["employee_kb"]
    emails_out: list[str] = []
    seen: set = set()
    role_fields = [
        "metadata.role", "metadata.position", "metadata.department",
        "metadata.team", "metadata.designation", "metadata.dept", "metadata.group",
    ]

    # Words that appear in almost every document and must not be used as filters
    _STOPWORDS = {
        "add", "all", "the", "and", "for", "with", "who", "are", "was",
        "has", "have", "been", "from", "that", "this", "they", "their",
        "our", "your", "its", "not", "also", "any", "can", "will", "may",
        "include", "invite", "please", "some", "both", "other", "new",
        "employees", "employee", "members", "member", "team", "staff",
        "people", "person", "user", "users", "worker", "workers",
    }

    label_tokens = [
        w for w in re.sub(r"[^a-z ]", " ", label.lower()).split()
        if len(w) >= 3 and w not in _STOPWORDS
    ]

    def _add(meta: dict, content: str = "") -> None:
        email = meta.get("email") or ""
        if not email:
            email = next(iter(_extract_emails(content)), "")
        if _is_valid_email(email) and email not in seen:
            seen.add(email)
            emails_out.append(email)

    # Only run the lookup if we have meaningful tokens
    if not label_tokens:
        logger.warning("[AskAttendees] group '%s' has no meaningful tokens — skipping", label)
        return []

    pat = re.compile(re.escape(label), re.IGNORECASE)
    or_conds = [{f: {"$regex": pat}} for f in role_fields]
    or_conds.append({"content": {"$regex": pat}})
    try:
        async for doc in col.find({"$or": or_conds}):
            _add(doc.get("metadata") or {}, doc.get("content", ""))
    except Exception as e:
        logger.warning("[AskAttendees] group-regex '%s': %s", label, e)

    for token in label_tokens:
        if token == label.lower():
            continue
        try:
            pat_t = re.compile(r"\b" + re.escape(token) + r"\b", re.IGNORECASE)
            async for doc in col.find({"$or": [{f: {"$regex": pat_t}} for f in role_fields]}):
                _add(doc.get("metadata") or {}, doc.get("content", ""))
        except Exception:
            pass

    try:
        from services.mongo_rag_service import search_employees
        for r in await search_employees(label, top_k=40):
            meta = r.get("metadata") or {}
            meta_str = " ".join(str(v) for v in meta.values()).lower()
            # Require ALL meaningful tokens to match (not just any) to avoid false positives
            if label_tokens and all(tok in meta_str for tok in label_tokens):
                _add(meta, r.get("content", ""))
    except Exception as e:
        logger.warning("[AskAttendees] group-vector '%s': %s", label, e)

    logger.info("[AskAttendees] group '%s' → %d emails", label, len(emails_out))
    return emails_out


# ── Thread history ────────────────────────────────────────────────────────────

async def _get_thread_history(user_id: str, thread_id: Optional[str] = None) -> str:
    try:
        from services.message_service import format_history_from_db
        from services.thread_service import get_active_thread
        if not thread_id:
            thread_id = await get_active_thread(user_id)
        if not thread_id:
            return ""
        return await format_history_from_db(thread_id, limit=8)
    except Exception:
        return ""


def _find_in_history(name: str, history: str) -> Optional[str]:
    """
    Find an email for a name in conversation history.
    Searches for the email that appears CLOSEST and to the RIGHT of the name
    occurrence — never just the first email on the line — to avoid assigning
    Person A's email to Person B when they appear on the same line.
    """
    if not history:
        return None
    name_pat = re.escape(name)
    # Find all occurrences of the name and grab the nearest email that follows it
    for m in re.finditer(name_pat, history, flags=re.I):
        # Look at up to 80 chars after the name for an email
        after = history[m.end():m.end() + 80]
        hits = _extract_emails(after)
        if hits:
            return hits[0]
    return None


# ── DB schema ─────────────────────────────────────────────────────────────────

async def _get_schema() -> dict:
    try:
        col = get_db()["employee_kb"]
        return {
            "roles":     [r for r in await col.distinct("metadata.role")     if r],
            "positions": [p for p in await col.distinct("metadata.position") if p],
        }
    except Exception:
        return {"roles": [], "positions": []}


# ── Raw string → target classification ───────────────────────────────────────

async def _classify_raw_strings(raw_list: list[str], schema: dict) -> list[dict]:
    """
    Takes a list of cleaned candidate strings (names/emails/roles) and classifies
    each as type "email", "name", or "group".

    Expects strings that have already been cleaned of action words like
    "add", "invite", "include" by the caller (_do_collect_from_reply).
    """
    if not raw_list:
        return []

    targets: list[dict] = []
    remaining: list[str] = []
    for s in raw_list:
        if _is_valid_email(s):
            targets.append({"type": "email", "value": s})
        else:
            remaining.append(s)

    if not remaining:
        return targets

    prompt = f"""Classify each item as a person name, role/group, or email.

Available roles in DB: {schema.get("roles", [])}
Available positions in DB: {schema.get("positions", [])}

Items to classify: {json.dumps(remaining)}

Return ONLY a JSON array (one entry per item, same order):
[
  {{"type": "name",  "value": "<person name — keep exact spelling for fuzzy lookup>"}},
  {{"type": "group", "value": "<closest matching role/position from DB vocabulary>"}},
  {{"type": "email", "value": "<email address>"}}
]

Rules:
- "name"  : a single person's name (even if misspelled).
- "group" : a role, position, department, or team reference.
  Use the closest DB vocabulary match. Only classify as group if the item
  clearly refers to a category of people, not an individual.
- "email" : a valid email address.
- When in doubt between "name" and "group", prefer "name".
- Return the array only, no markdown.
"""
    llm = get_llm()
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = (getattr(resp, "content", "") or "").strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n\r\t")
        result = json.loads(raw)
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("type") in ("name", "group", "email") and item.get("value"):
                    targets.append({"type": item["type"], "value": str(item["value"]).strip()})
        return targets
    except Exception as e:
        logger.warning("[AskAttendees] classify_raw failed: %s", e)
        for s in remaining:
            targets.append({"type": "name", "value": s})
        return targets


# ── Core resolver ─────────────────────────────────────────────────────────────

async def _resolve_targets(
    targets: list[dict],
    user_id: str,
    thread_id: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """
    Resolve classified targets to attendee dicts.
    Returns (resolved, unresolved).
    resolved   : [{"name": ..., "email": ...}] — all have valid email
    unresolved : [{"name": ..., "email": None}] — need to ask user
    """
    history = await _get_thread_history(user_id, thread_id)
    resolved: list[dict] = []
    unresolved: list[dict] = []
    seen: set = set()

    for t in targets:
        t_type  = t.get("type")
        t_value = str(t.get("value") or "").strip()
        if not t_value:
            continue

        if t_type == "email":
            if _is_valid_email(t_value) and t_value not in seen:
                seen.add(t_value)
                resolved.append({"name": t_value, "email": t_value})

        elif t_type == "name":
            email = _find_in_history(t_value, history)
            if not email:
                email = await _mongo_lookup_name(t_value)
            if email and _is_valid_email(email) and email not in seen:
                seen.add(email)
                resolved.append({"name": t_value, "email": email})
            else:
                unresolved.append({"name": t_value, "email": None})

        elif t_type == "group":
            for email in await _mongo_group_lookup(t_value):
                if email not in seen:
                    seen.add(email)
                    resolved.append({"name": email, "email": email})

    return resolved, unresolved


async def _generate_missing_q(name: str) -> str:
    """LLM generates a friendly question asking for this person's email."""
    llm = get_llm()
    try:
        resp = await asyncio.to_thread(
            llm.invoke,
            f"You are a calendar assistant. Ask the user for the email address of '{name}' "
            f"in one short friendly sentence. Use the actual name. Return only the question."
        )
        q = (getattr(resp, "content", "") or "").strip()
        if q:
            return q
    except Exception:
        pass
    return f"I couldn't find {name}'s email. Could you share it?"


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_attendees(
    data: dict,
    user_reply: str,
    user_id: str,
    thread_id: Optional[str] = None,
) -> tuple[str | None, dict]:
    """
    Smart attendee collection. Called from calendar_flow_service on every turn
    while stage == "ask_attendees".

    Stage progression:
      on_extract → ask_yn → collect → resolve_N → done

    data["attendee_stage"] tracks the sub-stage.
    """
    stage = data.get("attendee_stage", "on_extract")
    logger.info("[AskAttendees] stage=%s reply='%s'", stage, (user_reply or "")[:60])

    # ── on_extract ────────────────────────────────────────────────────────────
    # Called once right after calendar_extract with the original user message.
    # Resolves raw_attendees extracted by calendar_extract immediately.
    if stage == "on_extract":
        raw_attendees = data.pop("raw_attendees", []) or []

        if raw_attendees:
            logger.info("[AskAttendees] Resolving %d raw attendees from extract: %s", len(raw_attendees), raw_attendees)
            schema = await _get_schema()
            targets = await _classify_raw_strings(raw_attendees, schema)
            resolved, unresolved = await _resolve_targets(targets, user_id, thread_id)

            all_attendees = resolved + unresolved
            data["attendees"] = all_attendees

            if unresolved:
                idx = all_attendees.index(unresolved[0])
                data["attendee_stage"] = f"resolve_{idx}"
                q = await _generate_missing_q(unresolved[0]["name"])
                return q, data

            # All resolved → skip the yes/no question entirely
            data["attendee_stage"] = "done"
            return None, data

        # No attendees in initial message → ask yes/no
        data["attendee_stage"] = "ask_yn"
        return "Would you like to add any attendees to this event?", data

    # ── ask_yn ────────────────────────────────────────────────────────────────
    if stage == "ask_yn":
        if not user_reply or not user_reply.strip():
            return "Would you like to add any attendees to this event?", data

        # Classify intent: no / yes / inline names
        reply_lower = user_reply.lower().strip()

        # Fast "no" check
        if re.search(r"^(no|nope|nah|skip|none|don'?t|not|without)\b", reply_lower):
            data.update({"attendees": [], "attendee_stage": "done"})
            return None, data

        # Check if user typed names/emails directly (not just a confirmation word)
        has_direct_emails = bool(_extract_emails(user_reply))
        _CONFIRM_WORDS = {
            "yes", "sure", "okay", "ok", "yeah", "yep", "add", "invite",
            "please", "thanks", "thank", "great", "fine", "go", "ahead",
            "do", "it", "that", "sounds", "good", "yup",
        }
        non_confirm_words = [w for w in reply_lower.split() if w not in _CONFIRM_WORDS]
        # Need ≥2 non-filler words, or at least one long word (real names are usually ≥4 chars)
        has_names_inline = (
            len(non_confirm_words) >= 2 or
            any(len(w) >= 4 for w in non_confirm_words)
        ) and not re.search(r"^(yes|sure|okay|ok|yeah|yep|yup)\s*$", reply_lower)

        if has_direct_emails or has_names_inline:
            # User provided names/emails inline — resolve them directly
            data["attendee_stage"] = "collect"
            return await _do_collect_from_reply(user_reply, data, user_id, thread_id)

        # Pure "yes" → ask for names
        data["attendee_stage"] = "collect"
        return "Please share the names, email addresses, or roles of the attendees.", data

    # ── collect ───────────────────────────────────────────────────────────────
    if stage == "collect":
        return await _do_collect_from_reply(user_reply, data, user_id, thread_id)

    # ── resolve_N ─────────────────────────────────────────────────────────────
    if stage.startswith("resolve_"):
        try:
            idx = int(stage.split("_", 1)[1])
        except (IndexError, ValueError):
            data["attendee_stage"] = "done"
            return None, data

        attendees: list[dict] = list(data.get("attendees") or [])
        if idx >= len(attendees):
            data["attendee_stage"] = "done"
            return None, data

        person_name = attendees[idx].get("name", "that person")

        # Try direct email first
        direct = _extract_emails(user_reply)
        if direct:
            attendees[idx]["email"] = direct[0]
            logger.info("[AskAttendees] got email for '%s': %s", person_name, direct[0])
        else:
            # User might have typed another name — try MongoDB
            candidate = user_reply.strip()
            if candidate and len(candidate) < 60:
                found = await _mongo_lookup_name(candidate)
                if found:
                    attendees[idx]["email"] = found
                    logger.info("[AskAttendees] resolved name '%s' → %s", candidate, found)
                else:
                    return await _generate_missing_q(person_name), data
            else:
                return await _generate_missing_q(person_name), data

        data["attendees"] = attendees
        still = [a for a in attendees if not a.get("email")]
        if still:
            next_idx = attendees.index(still[0])
            data["attendee_stage"] = f"resolve_{next_idx}"
            return await _generate_missing_q(still[0]["name"]), data

        data["attendee_stage"] = "done"
        return None, data

    # ── done ─────────────────────────────────────────────────────────────────
    return None, data


async def _do_collect_from_reply(
    reply: str,
    data: dict,
    user_id: str,
    thread_id: Optional[str],
) -> tuple[str | None, dict]:
    """
    Parse attendees from a user reply string, resolve via MongoDB, update data.

    APPROACH:
      1. Strip action/filler words ("add", "invite", "include", "also", "please", etc.)
         from the reply BEFORE any classification — these cause misclassification
         e.g. "add shlok" → "add shlok" classified as group → matches all employees.
      2. Split the cleaned reply on "and" / "," into individual candidate strings.
      3. Pull out direct email addresses first (no LLM needed).
      4. Pass the remaining candidate strings to _classify_raw_strings (one call).
      5. Resolve via MongoDB.
    """
    existing: list[dict] = list(data.get("attendees") or [])
    existing_emails = {a["email"] for a in existing if a.get("email")}
    existing_names = {a.get("name", "").lower() for a in existing}

    # Step 1: extract direct emails from raw reply
    direct_emails = _extract_emails(reply)
    for e in direct_emails:
        if e not in existing_emails:
            existing.append({"name": e, "email": e})
            existing_emails.add(e)

    # Step 2: strip action/filler words and split into candidate strings
    # These words appear as prefixes and confuse the classifier / group lookup
    _ACTION_WORDS = {
        "add", "invite", "include", "also", "please", "and", "with",
        "the", "to", "for", "a", "an", "some", "both",
    }
    reply_no_email = _EMAIL_RE.sub("", reply).strip()
    # Split on "and", commas, semicolons
    raw_parts = [p.strip() for p in re.split(r"\band\b|[,;]", reply_no_email, flags=re.I) if p.strip()]

    # For each part, strip leading/trailing action words token by token
    candidates: list[str] = []
    for part in raw_parts:
        tokens = part.split()
        # Strip leading action words
        while tokens and tokens[0].lower() in _ACTION_WORDS:
            tokens = tokens[1:]
        # Strip trailing action words
        while tokens and tokens[-1].lower() in _ACTION_WORDS:
            tokens = tokens[:-1]
        cleaned = " ".join(tokens).strip()
        if cleaned:
            candidates.append(cleaned)

    if not candidates:
        data["attendees"] = existing
        data["attendee_stage"] = "done"
        return None, data

    # Step 3: classify cleaned candidates (names vs groups vs emails)
    schema = await _get_schema()
    targets = await _classify_raw_strings(candidates, schema)

    # Step 4: resolve
    resolved, unresolved = await _resolve_targets(targets, user_id, thread_id)

    for a in resolved:
        if a["email"] not in existing_emails:
            existing.append(a)
            existing_emails.add(a["email"])

    for u in unresolved:
        if u["name"].lower() not in existing_names:
            existing.append(u)
            existing_names.add(u["name"].lower())

    data["attendees"] = existing

    still = [a for a in existing if not a.get("email")]
    if still:
        idx = existing.index(still[0])
        data["attendee_stage"] = f"resolve_{idx}"
        return await _generate_missing_q(still[0]["name"]), data

    data["attendee_stage"] = "done"
    return None, data
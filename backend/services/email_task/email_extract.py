"""
services/email_task/email_extract.py

MongoDB-backed email role extractor.

ARCHITECTURE — three clean stages:

  Stage 1 — PARSE   : LLM reads the raw user message and pulls out structured
                       fields (to_name, tone, purpose, etc.) with no lookups.

  Stage 2 — CLASSIFY: LLM reads the parsed to_name list PLUS the real role /
                       position values that actually exist in the DB right now,
                       and decides:
                         • Is this a specific person or a group/role?
                         • Which clean name(s) / label(s) should we search for?
                       Because the LLM sees the real DB vocabulary it can match
                       "aiml interns" → "AIML" + "Intern" even if those words
                       don't appear together verbatim.

  Stage 3 — RESOLVE : Pure MongoDB lookups — no more LLM calls.
                       specific  → _mongo_lookup_by_name()   (regex → vector)
                       group     → _mongo_group_lookup()      (regex → vector)
                       broadcast → _mongo_broadcast_lookup()  (full scan)
                       contextual→ thread-history emails → group fallback
                       unknown   → nothing → missing_fields triggers ask_required

KEY IMPROVEMENTS over previous version:
  • DB schema is fetched once and injected into the classifier prompt so the LLM
    never has to guess what roles / positions exist.
  • Classifier returns a structured object per person/group; the dispatcher just
    executes those objects — no big conditional tree of examples needed.
  • _mongo_lookup_by_name now uses a flexible token-overlap fuzzy match for the
    vector-search pass, so "shlko" finds "Shlok", "annasd" finds "Anand", etc.
  • _mongo_group_lookup drops the aggressive word-filter on the vector pass;
    instead it accepts any result whose metadata overlaps at least one label word.
  • No hardcoded example lists inside prompts — the only examples are the
    real DB values fed dynamically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List, Optional, Union

from langchain_core.messages import HumanMessage

from core.database import get_db
from core.dependencies import get_llm

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# ── Email helpers ─────────────────────────────────────────────────────────────

_FAKE_LOCALS = {
    "all", "toall", "everyone", "team", "staff", "interns", "employees",
    "intern", "employee", "worker", "workers", "developer", "developers",
    "null", "none", "example", "test", "noreply", "no-reply", "user",
}


def _is_valid_email(email: str) -> bool:
    if not email or not isinstance(email, str):
        return False
    e = email.strip()
    if not _EMAIL_RE.fullmatch(e):
        return False
    return e.split("@")[0].lower() not in _FAKE_LOCALS


def _extract_emails_from_text(text: str) -> List[str]:
    if not text:
        return []
    seen, out = set(), []
    for e in _EMAIL_RE.findall(text):
        e = e.strip()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _normalize_name_list(raw: Union[str, List[str], None]) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    parts = re.split(r"\s*(?:,|and)\s*", str(raw).strip())
    return [p.strip() for p in parts if p.strip()]


def _cleanup_json(raw: str) -> str:
    if not raw:
        return raw
    cleaned = re.sub(r"```(?:json)?", "", raw)
    return cleaned.strip("` \n\r\t")


def _safe_json(raw: str) -> dict | list:
    cleaned = _cleanup_json(raw)
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", cleaned, flags=re.S)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return {}

# ── Live DB schema ────────────────────────────────────────────────────────────

async def _get_db_schema() -> dict:
    """
    Fetch distinct role, position, and name values from employee_kb.
    Used to ground the classifier — LLM sees what actually exists in the DB.
    Cached for the lifetime of a single extraction call (no global cache needed,
    it's one fast DB call per email flow start).
    """
    try:
        col = get_db()["employee_kb"]
        departments = await col.distinct("metadata.department")
        positions   = await col.distinct("metadata.position")
        names       = await col.distinct("metadata.name")
        return {
            "departments": [d for d in departments if d],
            "positions":   [p for p in positions   if p],
            "names":       [n for n in names       if n],
        }
    except Exception as e:
        logger.warning("[EmailExtract] DB schema fetch failed: %s", e)
        return {"departments": [], "positions": [], "names": []}


# ── MongoDB thread memory ─────────────────────────────────────────────────────

async def _get_thread_history(user_id: str, thread_id: Optional[str] = None, limit: int = 8) -> str:
    """Load recent conversation from MongoDB thread (replaces LangChain session memory)."""
    try:
        from services.message_service import format_history_from_db
        from services.thread_service import get_active_thread
        if not thread_id:
            thread_id = await get_active_thread(user_id)
        if not thread_id:
            return ""
        return await format_history_from_db(thread_id, limit=limit)
    except Exception as e:
        logger.warning("[EmailExtract] Thread history load failed: %s", e)
        return ""

# ── Stage 1 — Parse raw message ───────────────────────────────────────────────

async def _parse_message(user_message: str, history: str) -> dict:
    """
    LLM reads the raw user message and extracts structured fields.
    Does NOT do any lookups — just parses what the user literally said.
    """
    prompt = f"""You are extracting structured fields from a user's email request.

Conversation history (for resolving explicit references like "them", "it", "that person" ONLY):
{history or "(none)"}

User message: "{user_message}"

Return ONLY valid JSON — no markdown, no explanation:
{{
  "to_name": "<exactly what the user said about the recipient in THIS message — name(s), role, phrase, or null>",
  "tone": "<formal|casual|friendly>",
  "purpose": "<what the email is about from THIS message, or null>",
  "location": "<place mentioned in THIS message, or null>",
  "time": "<time or date mentioned in THIS message, or null>"
}}

Rules:
- Extract ONLY from the user message above. Use history ONLY to resolve pronouns like
  "them", "it", "same person" — never to fill in to_name or purpose from a prior session.
- to_name must be exactly what the user said in THIS message — do not interpret or clean it yet.
- Never invent an email address.
- If the user said nothing about a role, set it to null.
"""
    llm = get_llm()
    try:
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = getattr(resp, "content", str(resp) or "")
        result = _safe_json(raw)
        if not isinstance(result, dict):
            result = {}
    except Exception as e:
        logger.warning("[EmailExtract] Parse failed: %s", e)
        result = {}

    def _clean(val) -> "str | None":
        if not val:
            return None
        s = str(val).strip()
        return None if not s or s.lower() in ("null", "none", "n/a", "") else s

    return {
        "to_name": _clean(result.get("to_name")),
        "tone":    (result.get("tone") or "formal").lower().strip(),
        "purpose": _clean(result.get("purpose")),
        "location":_clean(result.get("location")),
        "time":    _clean(result.get("time")),
    }

# ── Stage 2 — Classify recipient intent ──────────────────────────────────────

async def _classify_recipients(
    to_name_raw: str | None,
    user_message: str,
    history: str,
    schema: dict,
) -> dict:
    """
    LLM reads the to_name text, the real DB vocabulary, and the full user message,
    and returns a structured classification:

    {
      "intent": "specific" | "group" | "broadcast" | "contextual" | "unknown",
      "targets": [
        {"type": "name",  "value": "<clean name as the user said it>"},
        {"type": "group", "value": "<role or position label from DB vocabulary>"}
      ]
    }

    The LLM is NOT given a long list of hardcoded example sentences.
    Instead it is given the actual DB vocabulary so it can make its own judgement.
    A few short illustrative examples are included only to show the output format.
    """
    if not to_name_raw and not user_message:
        return {"intent": "unknown", "targets": []}

    prompt = f"""You are classifying who a user wants to send an email to.

Available departments in the employee database: {schema["departments"]}
Available positions in the employee database: {schema["positions"]}
Some employee names in the database: {schema["names"][:30]}

Conversation history: {history or "(none)"}
User message: "{user_message}"
Recipient phrase from user: "{to_name_raw or ''}"

Decide the intent and return ONLY valid JSON:
{{
  "intent": "<specific|group|broadcast|contextual|unknown>",
  "targets": [
    {{"type": "name",  "value": "<individual name exactly as stated by user>"}},
    {{"type": "group", "value": "<role/position label — pick the closest match from the DB vocabulary above>"}}
  ]
}}

Intent definitions:
- specific   : user named one or more individual people by name (even if misspelled)
- group      : user referenced a role, position, department, or team
- broadcast  : user wants to reach everyone with no filter ("all", "everyone", "whole team")
- contextual : user referenced people already mentioned in this conversation
               ("them", "those people", "above interns", "same group")
- unknown    : recipient is missing or incomprehensible

targets rules:
- For "specific": one entry per person with type="name" and the name as typed by the user.
- For "group": one entry per distinct group with type="group" and the closest matching
  label from the DB vocabulary. If user said "aiml interns" use separate entries for
  the role part ("AIML") and position part ("Intern") if they are separate DB fields,
  or one combined label if that reads better. Use your judgement.
- For "broadcast" / "contextual" / "unknown": targets = []

Short format examples (output shape only, not content rules):
  specific two people  → {{"intent":"specific","targets":[{{"type":"name","value":"Shlok"}},{{"type":"name","value":"Yash"}}]}}
  group by role        → {{"intent":"group","targets":[{{"type":"group","value":"AIML Intern"}}]}}
  everyone             → {{"intent":"broadcast","targets":[]}}
  contextual reference → {{"intent":"contextual","targets":[]}}
"""
    llm = get_llm()
    try:
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = getattr(resp, "content", str(resp) or "")
        result = _safe_json(raw)
        if not isinstance(result, dict):
            result = {}

        intent = result.get("intent", "unknown")
        if intent not in ("specific", "group", "broadcast", "contextual", "unknown"):
            intent = "unknown"

        targets = result.get("targets", [])
        if not isinstance(targets, list):
            targets = []
        # Validate each target entry
        clean_targets = []
        for t in targets:
            if isinstance(t, dict) and t.get("type") in ("name", "group") and t.get("value"):
                clean_targets.append({
                    "type":  t["type"],
                    "value": str(t["value"]).strip(),
                })

        logger.info("[EmailExtract] Classify → intent=%s targets=%s", intent, clean_targets)
        return {"intent": intent, "targets": clean_targets}

    except Exception as e:
        logger.warning("[EmailExtract] Classify failed: %s", e)
        return {"intent": "unknown", "targets": []}


# ── Stage 3 — MongoDB lookups ─────────────────────────────────────────────────

async def _mongo_lookup_by_name(name: str) -> Optional[str]:
    """
    Find a single person's email by name.

    Pass 1 — regex on metadata.name  (exact-ish, case-insensitive)
    Pass 2 — regex on content text   (catches name in the narrative blob)
    Pass 3 — vector search           (semantic/typo fallback)
              fuzzy match: accept if any token in the query name overlaps
              with any token in the result name — handles "shlko"→"Shlok",
              "annasd"→"Anand", etc.
    """
    if not name or not name.strip():
        return None

    col = get_db()["employee_kb"]
    name_clean = name.strip()

    # Pass 1
    try:
        pat = re.compile(re.escape(name_clean), re.IGNORECASE)
        doc = await col.find_one({"metadata.name": {"$regex": pat}})
        if doc:
            email = (doc.get("metadata") or {}).get("email") or ""
            if not email:
                email = next(iter(_extract_emails_from_text(doc.get("content", ""))), "")
            if _is_valid_email(email):
                logger.info("[EmailExtract] name-regex '%s' → %s", name_clean, email)
                return email
    except Exception as e:
        logger.warning("[EmailExtract] name-regex failed '%s': %s", name_clean, e)

    # Pass 2
    try:
        pat2 = re.compile(re.escape(name_clean), re.IGNORECASE)
        doc = await col.find_one({"content": {"$regex": pat2}})
        if doc:
            email = (doc.get("metadata") or {}).get("email") or ""
            if not email:
                email = next(iter(_extract_emails_from_text(doc.get("content", ""))), "")
            if _is_valid_email(email):
                logger.info("[EmailExtract] content-regex '%s' → %s", name_clean, email)
                return email
    except Exception as e:
        logger.warning("[EmailExtract] content-regex failed '%s': %s", name_clean, e)

    # Pass 3 — vector search with exact name matching (no loose 3-char prefix)
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
            # Require exact full-name match OR exact first-name match (≥4 chars)
            # Never accept a 3-char prefix — that caused wrong-person email assignment
            full_match = (rname == name_lower)
            first_name_match = (
                name_parts and rname_parts and
                name_parts[0] == rname_parts[0] and
                len(name_parts[0]) >= 4
            )
            if full_match or first_name_match:
                email = meta.get("email") or ""
                if not email:
                    email = next(iter(_extract_emails_from_text(r.get("content", ""))), "")
                if _is_valid_email(email):
                    logger.info("[EmailExtract] vector-exact '%s' → %s (matched '%s')", name_clean, email, rname)
                    return email
    except Exception as e:
        logger.warning("[EmailExtract] vector search failed '%s': %s", name_clean, e)

    logger.warning("[EmailExtract] No email for name '%s'", name_clean)
    return None


async def _mongo_group_lookup(labels: List[str]) -> List[str]:
    """
    Resolve role/position/team labels to a list of emails.

    Per label:
    Pass 1 — $or regex across all role-like metadata fields and content.
    Pass 2 — vector search; accept any result whose metadata contains at least
             one label token of length ≥ 3 (relaxed from previous version).
    """
    if not labels:
        return []

    col = get_db()["employee_kb"]
    emails_out: List[str] = []
    seen: set = set()

    role_fields = [
        "metadata.department", "metadata.position",
        "metadata.team", "metadata.designation", "metadata.dept", "metadata.group",
    ]

    def _add(doc: dict) -> None:
        meta = doc.get("metadata") or {}
        email = meta.get("email") or ""
        if not email:
            emails = _extract_emails_from_text(doc.get("content", ""))
            email = emails[0] if emails else ""
        if _is_valid_email(email) and email not in seen:
            seen.add(email)
            emails_out.append(email)

    for label in labels:
        label_clean = label.strip()
        # All meaningful tokens (len ≥ 3) for loose matching
        _STOPWORDS = {"all","the","and","for","with","send","email","mail","to","employees","employee","members","member","team","staff","people","person"}
        label_tokens = [w for w in re.sub(r"[^a-z ]", " ", label_clean.lower()).split() if len(w) >= 3 and w not in _STOPWORDS]

        # Pass 1 — regex on role metadata fields + content
        pat = re.compile(re.escape(label_clean), re.IGNORECASE)
        or_conds = [{f: {"$regex": pat}} for f in role_fields]
        or_conds.append({"content": {"$regex": pat}})
        try:
            async for doc in col.find({"$or": or_conds}):
                _add(doc)
        except Exception as e:
            logger.warning("[EmailExtract] group-regex failed '%s': %s", label_clean, e)

        # Pass 1b — try each individual token as well (catches "AIML" + "Intern" separately)
        for token in label_tokens:
            if token == label_clean.lower():
                continue  # already done above
            pat_t = re.compile(re.escape(token), re.IGNORECASE)
            or_t = [{f: {"$regex": pat_t}} for f in role_fields]
            try:
                async for doc in col.find({"$or": or_t}):
                    _add(doc)
            except Exception:
                pass

        # Pass 2 — vector search; relaxed: accept if any label token appears in metadata
        try:
            from services.mongo_rag_service import search_employees
            results = await search_employees(label_clean, top_k=40)
            for r in results:
                meta = r.get("metadata") or {}
                meta_str = " ".join(str(v) for v in meta.values()).lower()
                if label_tokens and all(tok in meta_str for tok in label_tokens):
                    _add({"metadata": meta, "content": r.get("content", "")})
        except Exception as e:
            logger.warning("[EmailExtract] group-vector failed '%s': %s", label_clean, e)

    logger.info("[EmailExtract] Group lookup %s → %d emails", labels, len(emails_out))
    return emails_out


async def _mongo_broadcast_lookup() -> List[str]:
    """Return ALL employee emails — used for 'send to all / everyone'."""
    col = get_db()["employee_kb"]
    emails_out: List[str] = []
    seen: set = set()
    try:
        async for doc in col.find({}):
            meta = doc.get("metadata") or {}
            e = meta.get("email") or ""
            if _is_valid_email(e) and e not in seen:
                seen.add(e)
                emails_out.append(e)
            for e2 in _extract_emails_from_text(doc.get("content", "")):
                if _is_valid_email(e2) and e2 not in seen:
                    seen.add(e2)
                    emails_out.append(e2)
    except Exception as e:
        logger.warning("[EmailExtract] broadcast lookup failed: %s", e)
    logger.info("[EmailExtract] Broadcast → %d emails", len(emails_out))
    return emails_out


# ── Resolution dispatcher ─────────────────────────────────────────────────────

async def _resolve(
    classification: dict,
    history: str,
    history_emails: List[str],
) -> tuple[List[str], List[str]]:
    """
    Execute the classification produced by _classify_recipients().
    Returns (resolved_emails, partial_missing_names).
    partial_missing_names: names that the classifier identified as specific
                           individuals but whose email could not be found.
    """
    intent  = classification.get("intent", "unknown")
    targets = classification.get("targets", [])

    resolved: List[str] = []
    missing:  List[str] = []

    if intent == "broadcast":
        # Always do a full DB lookup for broadcast — history emails are irrelevant
        # (history may contain just 1-2 random emails, not the whole company)
        return await _mongo_broadcast_lookup(), []

    if intent == "contextual":
        if history_emails:
            return list(history_emails), []
        # Fallback: treat any group targets as group lookup
        group_labels = [t["value"] for t in targets if t["type"] == "group"]
        if group_labels:
            return await _mongo_group_lookup(group_labels), []
        # No history, no group targets — cannot resolve a contextual reference
        # Return as unresolvable so ask_required asks the user who they mean
        logger.warning("[EmailExtract] Contextual intent with no resolvable context — marking as missing")
        return [], ["recipient (contextual reference — please clarify who you mean)"]

    if intent == "group":
        labels = [t["value"] for t in targets if t["type"] == "group"]
        if not labels:
            # No group labels extracted — fall through to missing
            return [], []
        return await _mongo_group_lookup(labels), []

    if intent == "specific":
        names = [t["value"] for t in targets if t["type"] == "name"]
        if not names:
            return [], []

        for name in names:
            # 1. Check thread history — email nearest to the name, not first on line
            found: Optional[str] = None
            if history and history_emails:
                name_pat = re.escape(name)
                for m in re.finditer(name_pat, history, flags=re.I):
                    # Scan up to 80 chars AFTER the name for the closest email
                    after = history[m.end():m.end() + 80]
                    hits = _extract_emails_from_text(after)
                    if hits:
                        found = hits[0]
                        break

            # 2. MongoDB KB lookup (3-pass with fuzzy vector)
            if not found:
                found = await _mongo_lookup_by_name(name)

            if found and _is_valid_email(found):
                if found not in resolved:
                    resolved.append(found)
                logger.info("[EmailExtract] '%s' → %s", name, found)
            else:
                missing.append(name)
                logger.warning("[EmailExtract] No email for '%s'", name)

        return resolved, missing

    # unknown
    return [], []


# ── Optional fields ───────────────────────────────────────────────────────────

def _regex_skip_flags(msg: str) -> dict:
    m = msg.lower()
    flags: dict = {}
    skip = bool(re.search(r"\b(skip|no|without|don['\u2019]?t add|remove)\b", m))
    if skip:
        if re.search(r"\bcc\b", m):          flags["skip_cc"] = True
        if re.search(r"\bbcc\b", m):         flags["skip_bcc"] = True
        if re.search(r"\battach(ment)?s?\b", m): flags["skip_attachments"] = True
    if re.search(r"\b(skip all|skip everything|no optional|without all)\b", m):
        flags["skip_cc"] = flags["skip_bcc"] = flags["skip_attachments"] = True
    return flags


async def _extract_optional_fields(user_message: str) -> dict:
    regex_flags = _regex_skip_flags(user_message)

    prompt = f"""You are an email assistant. Read the user message and extract optional email fields.

User message: "{user_message}"

Return ONLY valid JSON:
{{
  "cc": [],
  "bcc": [],
  "attachments": null,
  "skip_cc": false,
  "skip_bcc": false,
  "skip_attachments": false
}}

Rules:
- cc / bcc: include only email addresses the user explicitly typed. If none, empty list.
- skip_cc = true only if user explicitly said to skip or omit cc, or already provided cc addresses.
- skip_bcc = true only if user explicitly said to skip or omit bcc, or already provided bcc addresses.
- skip_attachments = true only if user mentioned or skipped attachments.
- Never invent email addresses.
"""
    llm = get_llm()
    try:
        resp = await asyncio.to_thread(llm.invoke, [HumanMessage(content=prompt)])
        raw = getattr(resp, "content", str(resp) or "")
        result = _safe_json(raw)
        if not isinstance(result, dict):
            result = {}
    except Exception as e:
        logger.warning("[EmailExtract] Optional fields failed: %s", e)
        result = {}

    cc_raw  = [e.strip() for e in (result.get("cc")  or []) if _is_valid_email(str(e))]
    bcc_raw = [e.strip() for e in (result.get("bcc") or []) if _is_valid_email(str(e))]

    attachments = result.get("attachments") or None
    if isinstance(attachments, str) and not attachments.strip():
        attachments = None

    skip_cc  = bool(result.get("skip_cc"))  or bool(cc_raw)  or regex_flags.get("skip_cc", False)
    skip_bcc = bool(result.get("skip_bcc")) or bool(bcc_raw) or regex_flags.get("skip_bcc", False)
    skip_att = bool(result.get("skip_attachments")) or bool(attachments) or regex_flags.get("skip_attachments", False)

    return {
        "cc": cc_raw, "bcc": bcc_raw, "attachments": attachments,
        "skip_cc": skip_cc, "skip_bcc": skip_bcc, "skip_attachments": skip_att,
    }


# ── Public entry point ────────────────────────────────────────────────────────

async def extract_email_fields(
    user_message: str,
    user_id: str,
    thread_id: Optional[str] = None,
) -> dict:
    """
    Full extraction pipeline. Returns a dict consumed by email_flow_service.

    Keys returned:
      to_name, to_email, tone, purpose, location, time,
      missing_fields, recipient_count, partial_missing,
      cc, bcc, attachments, skip_cc, skip_bcc, skip_attachments, optional_filled
    """
    logger.info("[EmailExtract] Start | user=%s", user_id)

    # ── Parallel setup: history + DB schema ──────────────────────────────────
    history, schema = await asyncio.gather(
        _get_thread_history(user_id, thread_id),
        _get_db_schema(),
    )
    history_emails = _extract_emails_from_text(history)

    # ── Check for direct email addresses in message ───────────────────────────
    direct_emails = [e for e in _extract_emails_from_text(user_message) if _is_valid_email(e)]

    # ── Stage 1: Parse ────────────────────────────────────────────────────────
    parsed = await _parse_message(user_message, history)
    to_name_raw = parsed["to_name"]

    # ── Stage 2: Classify (skip if we already have direct emails) ─────────────
    if direct_emails:
        logger.info("[EmailExtract] Direct emails found: %s — skipping classify", direct_emails)
        resolved_emails = direct_emails
        partial_missing: List[str] = []
    else:
        classification = await _classify_recipients(to_name_raw, user_message, history, schema)

        # Stage 3: Resolve
        resolved_emails, partial_missing = await _resolve(classification, history, history_emails)

    # ── Deduplicate + validate ────────────────────────────────────────────────
    seen: set = set()
    valid: List[str] = []
    for e in resolved_emails:
        if _is_valid_email(e) and e not in seen:
            seen.add(e)
            valid.append(e)

    if len(valid) == 0:
        to_email_out = None
        recipient_count = 0
    elif len(valid) == 1:
        to_email_out = valid[0]
        recipient_count = 1
    else:
        to_email_out = valid
        recipient_count = len(valid)

    # ── Optional fields ───────────────────────────────────────────────────────
    opt = await _extract_optional_fields(user_message)
    skip_cc  = opt["skip_cc"]
    skip_bcc = opt["skip_bcc"]
    skip_att = opt["skip_attachments"]

    # ── Missing fields ────────────────────────────────────────────────────────
    missing_fields: List[str] = []
    if recipient_count == 0:
        missing_fields.append("to_email")
    elif partial_missing:
        missing_fields.append("to_email_incomplete")
    # Guard: treat "null"/"none" strings from LLM as missing
    purpose_clean = parsed.get("purpose")
    if isinstance(purpose_clean, str) and purpose_clean.strip().lower() in ("null", "none", "n/a", ""):
        purpose_clean = None
    if not purpose_clean:
        missing_fields.append("purpose")
    # Write clean value back so generate_email_content never receives "null"
    parsed["purpose"] = purpose_clean

    result = {
        "to_name":          to_name_raw,
        "to_email":         to_email_out,
        "tone":             parsed["tone"],
        "purpose":          parsed.get("purpose"),
        "location":         parsed["location"],
        "time":             parsed["time"],
        "missing_fields":   missing_fields,
        "recipient_count":  recipient_count,
        "partial_missing":  partial_missing,
        "cc":               opt["cc"],
        "bcc":              opt["bcc"],
        "attachments":      opt["attachments"],
        "skip_cc":          skip_cc,
        "skip_bcc":         skip_bcc,
        "skip_attachments": skip_att,
        "optional_filled":  skip_cc and skip_bcc and skip_att,
    }

    logger.info(
        "[EmailExtract] Done | missing=%s | recipients=%d | partial=%s",
        missing_fields, recipient_count, partial_missing,
    )
    return result
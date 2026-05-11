# services/gmail_read_service.py
"""
Gmail Read Service — fetch and search received emails per user.
Uses gmail.readonly scope.
"""

import asyncio
import logging
import base64
import re
from typing import Optional

from services.auth_service import get_gmail_service

logger = logging.getLogger(__name__)


async def handle_gmail_read(query: str, user_id: str) -> str:
    """
    Main entry point — parse the user's query and fetch relevant emails.
    Returns a formatted markdown string of results.
    """
    try:
        service = await get_gmail_service(user_id)
    except RuntimeError as e:
        return (
            "To read emails, you need to connect your Google account first.\n\n"
            "Click the **Connect Google** button at the top of the chat to get started."
        )

    query_lower = query.lower()

    # Parse how many the user wants (e.g. "show 15 emails")
    count_match = re.search(r"\b(\d+)\s*(email|mail|message)s?\b", query_lower)
    max_results = int(count_match.group(1)) if count_match else 10
    max_results = min(max_results, 25)  # cap to avoid huge API bills

    # Build Gmail search query from user intent
    gmail_query, label = _build_gmail_query(query_lower)

    try:
        results = await asyncio.to_thread(
            _fetch_emails, service, gmail_query, max_results=max_results
        )
    except Exception as e:
        logger.error("[GmailRead] Fetch failed | user=%s | %s", user_id, e)
        return f"Failed to fetch emails: {str(e)}"

    if not results:
        return f"No emails found for: {label}."

    # Format as markdown
    lines = [f"**{label}** ({len(results)} found)\n"]
    for i, msg in enumerate(results, 1):
        sender  = msg.get("from", "Unknown")
        subject = msg.get("subject", "(no subject)")
        date    = msg.get("date", "")
        snippet = msg.get("snippet", "")
        date_short = date[:16] if date else ""
        lines.append(
            f"**{i}. {subject}**\n"
            f"From: {sender}\n"
            f"Date: {date_short}\n"
            f"_{snippet}_\n"
        )

    return "\n---\n".join(lines)


def _build_gmail_query(query_lower: str) -> tuple:
    """Returns (gmail_query_string, human_readable_label)."""

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
        topic = about_match.group(1).strip().split()[0]
        return f"subject:{topic}", f"Emails about {topic}"

    # replied to / response check
    if any(w in query_lower for w in ["reply", "replied", "response", "responded"]):
        name_match = re.search(r"(?:from|by)\s+(\w+)", query_lower)
        if name_match:
            return f"from:{name_match.group(1)} is:inbox", f"Replies from {name_match.group(1)}"
        return "is:inbox newer_than:7d", "Recent inbox emails"

    return "is:inbox", "Inbox emails"


def _fetch_emails(service, gmail_query: str, max_results: int = 5) -> list[dict]:
    """Synchronous Gmail API call — run in thread."""
    result = service.users().messages().list(
        userId="me",
        q=gmail_query,
        maxResults=max_results,
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        emails.append({
            "from":    headers.get("From", "Unknown"),
            "subject": headers.get("Subject", "(no subject)"),
            "date":    headers.get("Date", ""),
            "snippet": detail.get("snippet", "")[:150],
        })

    return emails

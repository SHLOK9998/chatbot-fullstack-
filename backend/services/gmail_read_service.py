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

    # Determine what the user wants
    query_lower = query.lower()

    # Build Gmail search query from user intent
    if "unread" in query_lower:
        gmail_query = "is:unread"
        label = "Unread emails"
    elif "today" in query_lower:
        gmail_query = "newer_than:1d"
        label = "Today's emails"
    elif "from" in query_lower:
        # Extract name/email after "from"
        match = re.search(r"from\s+([a-zA-Z0-9@._\s]+)", query_lower)
        sender = match.group(1).strip() if match else ""
        gmail_query = f"from:{sender}" if sender else "is:inbox"
        label = f"Emails from {sender}" if sender else "Inbox emails"
    elif any(w in query_lower for w in ["reply", "replied", "response"]):
        gmail_query = "is:inbox newer_than:7d"
        label = "Recent inbox emails"
    else:
        gmail_query = "is:inbox"
        label = "Inbox emails"

    try:
        results = await asyncio.to_thread(
            _fetch_emails, service, gmail_query, max_results=5
        )
    except Exception as e:
        logger.error("[GmailRead] Fetch failed | user=%s | %s", user_id, e)
        return f"Failed to fetch emails: {str(e)}"

    if not results:
        return f"No emails found for: {label}."

    # Format as markdown
    lines = [f"### {label}\n"]
    for i, msg in enumerate(results, 1):
        sender  = msg.get("from", "Unknown")
        subject = msg.get("subject", "(no subject)")
        date    = msg.get("date", "")
        snippet = msg.get("snippet", "")
        lines.append(
            f"**{i}. {subject}**\n"
            f"From: {sender} | {date}\n"
            f"{snippet}\n"
        )

    return "\n---\n".join(lines)


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

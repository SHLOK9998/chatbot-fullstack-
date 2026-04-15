# services/google_auth_service.py
"""
Google OAuth2 service builder.
Shared by both gmail_service.py and calendar_service.py.

Uses a single token.json that contains scopes for both Gmail and Calendar.
If the token is missing or expired it refreshes automatically via the
stored refresh_token.  If it cannot refresh it raises RuntimeError so
the caller can surface a helpful error to the user.
"""

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.config import settings

logger = logging.getLogger(__name__)


def _load_credentials() -> Credentials:
    """
    Load OAuth2 credentials from token.json.
    Refreshes automatically if the access token is expired.

    Returns:
        Valid google.oauth2.credentials.Credentials object.

    Raises:
        RuntimeError: If token is missing, invalid, or cannot be refreshed.
    """
    token_path: Path = settings.GOOGLE_TOKEN_FILE
    # creds_path: Path = settings.GOOGLE_CREDENTIALS_FILE

    if not token_path.exists():
        raise RuntimeError(
            f"Google token file not found at '{token_path}'. "
            "Please run the OAuth consent flow once to generate it."
        )

    creds = Credentials.from_authorized_user_file(str(token_path), settings.GOOGLE_SCOPES)

    if creds.expired and creds.refresh_token:
        logger.info("Google OAuth token expired — refreshing automatically.")
        creds.refresh(Request())

        # Persist the refreshed token back to disk
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Refreshed token saved to '%s'", token_path)

    if not creds.valid:
        raise RuntimeError(
            "Google credentials are invalid and could not be refreshed. "
            f"Delete '{token_path}' and re-run the OAuth consent flow."
        )

    return creds

def get_gmail_service():
    """Return an authenticated Gmail API client (v1)."""
    logger.debug("Building Gmail API service client.")
    creds = _load_credentials()
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        logger.info("Gmail API service client ready.")
        return service
    except Exception as e:
        logger.error(f"Failed to initialize Gmail service: {e}")
        raise

def get_calendar_service():
    """Return an authenticated Google Calendar API client (v3)."""
    logger.debug("Building Google Calendar API service client.")
    creds = _load_credentials()
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google Calendar API service client ready.")
        return service
    except Exception as e:
        logger.error(f"Failed to initialize Calendar service: {e}")
        raise
    
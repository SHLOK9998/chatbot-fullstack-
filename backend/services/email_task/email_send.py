# services/email_task/email_send.py
"""
Email sender.

Builds a MIME email and sends it using the Gmail API client
obtained from auth_service (get_gmail_service).

Handles:
  - to, cc, bcc
  - subject and body
  - converts Markdown (including tables) to styled HTML
  - sender address from config (GMAIL_SENDER)
"""

import base64
import logging
import markdown2
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.config import settings
from services.auth_service import get_gmail_service

logger = logging.getLogger(__name__)


async def send_email(data: dict) -> str:
    """
    Send the email using the Gmail API.

    Args:
        data: State data dict with to_email, subject, body, cc, bcc.

    Returns:
        Success or error message string.
    """
    logger.info("[EmailSend] Sending email to '%s'", data.get("to_email"))

    try:
        # Build the MIME message
        message = _build_mime_message(data)

        # Encode to base64 as required by Gmail API
        raw_bytes = message.as_bytes()
        encoded   = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

        # Get authenticated Gmail service
        service = get_gmail_service()

        # Send via Gmail API
        result = service.users().messages().send(
            userId="me",
            body={"raw": encoded}
        ).execute()

        message_id = result.get("id", "unknown")
        logger.info("[EmailSend] Email sent successfully. Message ID: %s", message_id)
        return f"✅ Email sent successfully to {data.get('to_email')}!"

    except Exception as e:
        logger.error("[EmailSend] Failed to send email: %s", e)
        return f"❌ Failed to send email: {str(e)}"


def _build_mime_message(data: dict) -> MIMEMultipart:
    """Build a sanitized MIMEMultipart email from the state data dict."""

    msg = MIMEMultipart("alternative")  # allow both plain + html
    msg["From"] = settings.GMAIL_SENDER or "me"

    # --- Normalize helper ---
    def normalize_emails(value):
        if not value:
            return None
        if isinstance(value, list):
            clean = [email.strip().replace("\u200b", "") for email in value if email.strip()]
            return ", ".join(clean)
        elif isinstance(value, str):
            return value.strip().replace("\u200b", "")
        return None

    # --- Assign recipients ---
    to_emails = normalize_emails(data.get("to_email"))
    if not to_emails:
        raise ValueError("Missing valid 'To' recipients.")

    msg["To"] = to_emails

    # Only set CC/BCC headers when there are actual addresses.
    # Setting msg["Cc"] = None causes Python's email module to write
    # "Cc: None" literally in the headers in some versions.
    cc_val  = normalize_emails(data.get("cc"))
    bcc_val = normalize_emails(data.get("bcc"))
    if cc_val:
        msg["Cc"] = cc_val
    if bcc_val:
        msg["Bcc"] = bcc_val

    msg["Subject"] = (data.get("subject") or "(no subject)").strip()

    # --- Convert Markdown to HTML (with table styling) ---
    body = data.get("body", "")
    logger.info("[EmailSend] Converting Markdown to HTML for sending...")

    # Convert markdown → HTML (tables, fenced code blocks, etc.)
    html_content = markdown2.markdown(
        body,
        extras=["tables", "fenced-code-blocks", "strike", "underline"]
    )

    # Basic email-safe CSS styling
    style_html = f"""
    <html>
        <head>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    color: #333;
                    line-height: 1.6;
                    margin: 10px 20px;
                }}
                table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin: 20px 0;
                    font-size: 14px;
                }}
                th, td {{
                    border: 1px solid #dddddd;
                    text-align: left;
                    padding: 8px;
                }}
                th {{
                    background-color: #f2f2f2;
                }}
                tr:nth-child(even) {{
                    background-color: #f9f9f9;
                }}
            </style>
        </head>
        <body>
            {html_content}
        </body>
    </html>
    """

    # --- Attach plain text and HTML versions ---
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(style_html, "html", "utf-8"))

    return msg
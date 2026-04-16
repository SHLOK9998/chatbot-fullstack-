# # services/email_task/email_send.py
# """
# Email sender.

# Builds a MIME email and sends it using the Gmail API client
# obtained from auth_service (get_gmail_service).

# Handles:
#   - to, cc, bcc
#   - subject and body
#   - converts Markdown (including tables) to styled HTML
#   - sender address from config (GMAIL_SENDER)
# """

# import base64
# import logging
# import markdown2
# from email.mime.multipart import MIMEMultipart
# from email.mime.text import MIMEText

# from core.config import settings
# from services.auth_service import get_gmail_service

# logger = logging.getLogger(__name__)


# async def send_email(data: dict, user_id: str) -> str:
#     """
#     Send the email using the Gmail API for the given user.
#     """
#     logger.info("[EmailSend] Sending email | user=%s | to=%s", user_id, data.get("to_email"))

#     try:
#         message  = _build_mime_message(data, user_id)
#         raw_bytes = message.as_bytes()
#         encoded   = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

#         service = await get_gmail_service(user_id)

#         result = service.users().messages().send(
#             userId="me",
#             body={"raw": encoded}
#         ).execute()

#         to_val = data.get('to_email')
#         to_display = ", ".join(to_val) if isinstance(to_val, list) else str(to_val)
#         logger.info("[EmailSend] Sent | user=%s | msg_id=%s", user_id, message_id)
#         return f"Email sent successfully to {to_display}!"

#     except RuntimeError as e:
#         logger.warning("[EmailSend] Google not connected | user=%s", user_id)
#         return (
#             "To send emails, you need to connect your Google account first.\n\n"
#             "Click the **Connect Google** button at the top of the chat to get started."
#         )
#     except Exception as e:
#         logger.error("[EmailSend] Failed | user=%s | %s", user_id, e)
#         return f"Failed to send email: {str(e)}"


# def _build_mime_message(data: dict, user_id: str) -> MIMEMultipart:
#     """Build a sanitized MIMEMultipart email from the state data dict."""

#     msg = MIMEMultipart("alternative")
#     # Use the user's own Gmail address as From (fetched from token or fallback)
#     msg["From"] = data.get("_sender_email") or settings.GMAIL_SENDER or "me"

#     # --- Normalize helper ---
#     def normalize_emails(value):
#         if not value:
#             return None
#         if isinstance(value, list):
#             clean = [email.strip().replace("\u200b", "") for email in value if email.strip()]
#             return ", ".join(clean)
#         elif isinstance(value, str):
#             return value.strip().replace("\u200b", "")
#         return None

#     # --- Assign recipients ---
#     to_emails = normalize_emails(data.get("to_email"))
#     if not to_emails:
#         raise ValueError("Missing valid 'To' recipients.")

#     msg["To"] = to_emails

#     # Only set CC/BCC headers when there are actual addresses.
#     # Setting msg["Cc"] = None causes Python's email module to write
#     # "Cc: None" literally in the headers in some versions.
#     cc_val  = normalize_emails(data.get("cc"))
#     bcc_val = normalize_emails(data.get("bcc"))
#     if cc_val:
#         msg["Cc"] = cc_val
#     if bcc_val:
#         msg["Bcc"] = bcc_val

#     msg["Subject"] = (data.get("subject") or "(no subject)").strip()

#     # --- Convert Markdown to HTML (with table styling) ---
#     body = data.get("body", "")
#     logger.info("[EmailSend] Converting Markdown to HTML for sending...")

#     # Convert markdown → HTML (tables, fenced code blocks, etc.)
#     html_content = markdown2.markdown(
#         body,
#         extras=["tables", "fenced-code-blocks", "strike", "underline"]
#     )

#     # Basic email-safe CSS styling
#     style_html = f"""
#     <html>
#         <head>
#             <style>
#                 body {{
#                     font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
#                     color: #333;
#                     line-height: 1.6;
#                     margin: 10px 20px;
#                 }}
#                 table {{
#                     border-collapse: collapse;
#                     width: 100%;
#                     margin: 20px 0;
#                     font-size: 14px;
#                 }}
#                 th, td {{
#                     border: 1px solid #dddddd;
#                     text-align: left;
#                     padding: 8px;
#                 }}
#                 th {{
#                     background-color: #f2f2f2;
#                 }}
#                 tr:nth-child(even) {{
#                     background-color: #f9f9f9;
#                 }}
#             </style>
#         </head>
#         <body>
#             {html_content}
#         </body>
#     </html>
#     """

#     # --- Attach plain text and HTML versions ---
#     msg.attach(MIMEText(body, "plain", "utf-8"))
#     msg.attach(MIMEText(style_html, "html", "utf-8"))

#     return msg



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
  - file attachments via data["attachment_files"] (list of {filename, data} base64-encoded)
"""

import base64
import logging
import markdown2
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import mimetypes

from core.config import settings
from services.auth_service import get_gmail_service

logger = logging.getLogger(__name__)


async def send_email(data: dict, user_id: str) -> str:
    """
    Send the email using the Gmail API for the given user.
    """
    logger.info("[EmailSend] Sending email | user=%s | to=%s", user_id, data.get("to_email"))

    try:
        message  = _build_mime_message(data, user_id)
        raw_bytes = message.as_bytes()
        encoded   = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

        service = await get_gmail_service(user_id)

        result = service.users().messages().send(
            userId="me",
            body={"raw": encoded}
        ).execute()

        message_id = result.get("id", "unknown")
        to_val = data.get('to_email')
        to_display = ", ".join(to_val) if isinstance(to_val, list) else str(to_val)

        attachment_files = data.get("attachment_files") or []
        att_count = len(attachment_files)
        att_note = f" with {att_count} attachment{'s' if att_count != 1 else ''}" if att_count else ""

        logger.info("[EmailSend] Sent | user=%s | msg_id=%s", user_id, message_id)
        return f"Email sent successfully to {to_display}{att_note}!"

    except RuntimeError as e:
        logger.warning("[EmailSend] Google not connected | user=%s", user_id)
        return (
            "To send emails, you need to connect your Google account first.\n\n"
            "Click the **Connect Google** button at the top of the chat to get started."
        )
    except Exception as e:
        logger.error("[EmailSend] Failed | user=%s | %s", user_id, e)
        return f"Failed to send email: {str(e)}"


def _build_mime_message(data: dict, user_id: str) -> MIMEMultipart:
    """Build a sanitized MIMEMultipart email from the state data dict."""

    # Use "mixed" to support both alternative text/html body AND file attachments.
    # For emails without attachments this is still valid; all clients handle it correctly.
    msg = MIMEMultipart("mixed")
    msg["From"] = data.get("_sender_email") or settings.GMAIL_SENDER or "me"

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

    cc_val  = normalize_emails(data.get("cc"))
    bcc_val = normalize_emails(data.get("bcc"))
    if cc_val:
        msg["Cc"] = cc_val
    if bcc_val:
        msg["Bcc"] = bcc_val

    msg["Subject"] = (data.get("subject") or "(no subject)").strip()

    # --- Build the text/html body as an "alternative" sub-part ---
    body = data.get("body", "")
    logger.info("[EmailSend] Converting Markdown to HTML for sending...")

    html_content = markdown2.markdown(
        body,
        extras=["tables", "fenced-code-blocks", "strike", "underline"]
    )

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

    # Nest plain + html as an "alternative" sub-part inside the "mixed" container
    body_part = MIMEMultipart("alternative")
    body_part.attach(MIMEText(body, "plain", "utf-8"))
    body_part.attach(MIMEText(style_html, "html", "utf-8"))
    msg.attach(body_part)

    # --- Attach uploaded files ---
    attachment_files = data.get("attachment_files") or []
    for att in attachment_files:
        filename  = att.get("filename", "attachment")
        file_data = att.get("data", "")   # base64-encoded string

        try:
            file_bytes = base64.b64decode(file_data)
        except Exception as exc:
            logger.warning("[EmailSend] Could not decode attachment '%s': %s", filename, exc)
            continue

        # Guess MIME type from filename; fall back to octet-stream
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type:
            main_type, sub_type = mime_type.split("/", 1)
        else:
            main_type, sub_type = "application", "octet-stream"

        part = MIMEBase(main_type, sub_type)
        part.set_payload(file_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=filename,
        )
        msg.attach(part)
        logger.info("[EmailSend] Attached file '%s' (%d bytes)", filename, len(file_bytes))

    return msg
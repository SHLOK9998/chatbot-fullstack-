"""
services/calendar_task/set_calendar.py

Calendar Event Builder & Creator.

WHAT CHANGED vs. OLD VERSION:
  1. get_memory().save_context() call removed — persistence is handled
     automatically by chat_service.save_message() on every turn.
  2. All event building + Google Calendar API logic is UNCHANGED.
"""

import logging
from datetime import datetime, timedelta

from services.auth_service import get_calendar_service
from services.calendar_task.cal_recurrance import convert_recurrence

logger = logging.getLogger(__name__)

_DEFAULT_DURATION_HOURS = 1
_TIMEZONE = "Asia/Kolkata"


async def build_event_body(data: dict) -> dict:
    """
    Assemble a Google Calendar API event body from event state data.
    """
    start_field = data.get("start_time")
    if isinstance(start_field, dict):
        start_iso = start_field.get("datetime")
    else:
        start_iso = start_field

    if not start_iso:
        raise ValueError("[SetCalendar] Cannot build event: start_time is missing.")

    end_field = data.get("end_time")
    if isinstance(end_field, dict):
        end_iso = end_field.get("datetime")
    else:
        end_iso = end_field

    try:
        start_dt = datetime.fromisoformat(str(start_iso))
        if not end_iso:
            end_dt  = start_dt + timedelta(hours=_DEFAULT_DURATION_HOURS)
            end_iso = end_dt.isoformat()
        else:
            end_dt  = datetime.fromisoformat(str(end_iso))
            end_iso = end_dt.isoformat()
    except Exception as e:
        logger.warning("[SetCalendar] Could not compute end_time: %s", e)
        end_iso = start_iso

    attendees = []
    for a in data.get("attendees") or []:
        if a.get("email"):
            attendees.append({
                "email":       a["email"],
                "displayName": a.get("name", ""),
            })

    event_body = {
        "summary":     data.get("title") or data.get("purpose") or "New Event",
        "description": data.get("description") or data.get("purpose") or "",
        "location":    data.get("location") or "",
        "start": {"dateTime": str(start_iso), "timeZone": _TIMEZONE},
        "end":   {"dateTime": str(end_iso),   "timeZone": _TIMEZONE},
        "attendees": attendees,
    }

    recurrence_str = data.get("recurrence")
    if recurrence_str:
        rrule = await convert_recurrence(recurrence_str)
        if rrule:
            event_body["recurrence"] = [rrule]

    logger.info(
        "[SetCalendar] Event body ready: summary='%s' | start=%s | attendees=%d",
        event_body["summary"], start_iso, len(attendees),
    )
    return event_body


async def create_event(data: dict, user_id: str) -> str:
    """
    Create the calendar event via the Google Calendar API for the given user.
    """
    summary = data.get("title") or data.get("purpose") or "New Event"
    logger.info("[SetCalendar] Creating event: '%s' | user=%s", summary, user_id)

    try:
        event_body = await build_event_body(data)
        service    = await get_calendar_service(user_id)

        result = service.events().insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="all",
        ).execute()

        event_id        = result.get("id", "")
        event_link      = result.get("htmlLink", "")
        created_summary = result.get("summary", summary)

        logger.info("[SetCalendar] Event created | user=%s | id=%s", user_id, event_id)

        msg = f"Event **{created_summary}** created successfully!"
        if event_link:
            msg += f"\n[View in Google Calendar]({event_link})"

        invited = [a["email"] for a in (data.get("attendees") or []) if a.get("email")]
        if invited:
            msg += f"\nInvites sent to: {', '.join(invited)}"

        return msg

    except RuntimeError as e:
        logger.warning("[SetCalendar] Google not connected | user=%s", user_id)
        return (
            "To create calendar events, you need to connect your Google account first.\n\n"
            "Click the **Connect Google** button at the top of the chat to get started."
        )
    except ValueError as e:
        logger.error("[SetCalendar] Validation error | user=%s | %s", user_id, e)
        return f"Could not create event: {str(e)}"
    except Exception as e:
        logger.exception("[SetCalendar] API call failed | user=%s", user_id)
        return f"Failed to create the event: {str(e)}"
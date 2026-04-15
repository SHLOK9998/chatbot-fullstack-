from datetime import datetime 
import re
import dateparser

def parse_user_time(time_str: str):
    if not time_str:
        return {"success": False, "datetime": None, "time_missing": True}

    now = datetime.now()
    parsed = dateparser.parse(
        time_str,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "RETURN_AS_TIMEZONE_AWARE": False
        }
    )

    if not parsed:
        return {"success": False, "datetime": None, "time_missing": True}

    # Check if specific time was mentioned
    time_indicators = [
        ":", "am", "pm", "noon", "midnight", "hour", "minute", "min",
        "morning", "evening", "afternoon", "tonight"
    ]
    has_time = any(re.search(rf"\b{ind}\b", time_str.lower()) for ind in time_indicators)

    return {
        "success": True,
        "datetime": parsed.strftime("%Y-%m-%dT%H:%M:%S"),
        "time_missing": not has_time
    }
    
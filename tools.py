import datetime
from contextvars import ContextVar

from calendar_auth import get_calendar_service
from langchain_core.tools import tool

# ---------------------------------------------------------------------------
# Per-request user identity
# ---------------------------------------------------------------------------

current_user_id: ContextVar[int] = ContextVar("current_user_id")

TIMEZONE = "Asia/Singapore"
TZ_OFFSET = datetime.timezone(datetime.timedelta(hours=8))

BIRTHDAY_TAG = "#birthday"

_DEFAULT_START_HOUR = 9
_DEFAULT_DURATION_H = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _svc():
    return get_calendar_service(current_user_id.get())


def _build_datetimes(
    date_str: str,
    start_time: str = None,
    end_time: str = None,
) -> tuple[str, str]:
    """
    Return (start_iso, end_iso) with +08:00 offset.
    date_str   : YYYY-MM-DD
    start_time : HH:MM  (optional, defaults to 09:00)
    end_time   : HH:MM  (optional, defaults to start + 1 h)
    """
    date = datetime.date.fromisoformat(date_str)

    sh, sm = map(int, start_time.split(":")) if start_time else (_DEFAULT_START_HOUR, 0)
    start_dt = datetime.datetime(date.year, date.month, date.day, sh, sm, tzinfo=TZ_OFFSET)

    if end_time:
        eh, em = map(int, end_time.split(":"))
        end_dt = datetime.datetime(date.year, date.month, date.day, eh, em, tzinfo=TZ_OFFSET)
    else:
        end_dt = start_dt + datetime.timedelta(hours=_DEFAULT_DURATION_H)

    fmt = "%Y-%m-%dT%H:%M:%S+08:00"
    return start_dt.strftime(fmt), end_dt.strftime(fmt)


def _fmt_activity(e: dict) -> str:
    """One-line summary for an activity event."""
    start = e["start"].get("dateTime", e["start"].get("date", ""))
    end   = e["end"].get("dateTime",   e["end"].get("date", ""))
    try:
        start_dt = datetime.datetime.fromisoformat(start)
        end_dt   = datetime.datetime.fromisoformat(end)
        time_str = f"{start_dt.strftime('%b %d, %H:%M')} → {end_dt.strftime('%H:%M')}"
    except Exception:
        time_str = start
    return f"- [{e['id']}] {e.get('summary', '(no title)')} | {time_str}"


# ---------------------------------------------------------------------------
# Activity tools  (replaces separate event + task tools)
# ---------------------------------------------------------------------------

@tool
def add_activity(
    title: str,
    date: str = None,
    start_time: str = None,
    end_time: str = None,
    description: str = "",
    location: str = "",
) -> str:
    """
    Add a new activity (anything on the calendar — meeting, task, errand, workout, etc.).
    Args:
        title:       Activity title.
        date:        Date in YYYY-MM-DD format (optional; defaults to today).
        start_time:  Start time as HH:MM in 24-hour SGT (optional; defaults to 09:00).
        end_time:    End time as HH:MM in 24-hour SGT (optional; defaults to start + 1 hour).
        description: Optional notes or details.
        location:    Optional location string.
    Returns confirmation with the activity ID.
    """
    try:
        service  = _svc()
        date_str = date or datetime.date.today().isoformat()
        start_iso, end_iso = _build_datetimes(date_str, start_time, end_time)

        event = {
            "summary":     title,
            "description": description,
            "location":    location,
            "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
            "end":   {"dateTime": end_iso,   "timeZone": TIMEZONE},
        }
        created  = service.events().insert(calendarId="primary", body=event).execute()
        start_dt = datetime.datetime.fromisoformat(start_iso)
        end_dt   = datetime.datetime.fromisoformat(end_iso)
        time_str = f"{start_dt.strftime('%b %d, %H:%M')} → {end_dt.strftime('%H:%M')}"
        return f"✅ Activity added: '{title}' | {time_str}\nID: {created['id']}"
    except Exception as ex:
        return f"Error adding activity: {ex}"


def _day_bounds(date_str: str) -> tuple[str, str]:
    """
    Return (timeMin, timeMax) in UTC covering the full SGT calendar day for date_str.
    SGT is UTC+8, so a full SGT day runs from UTC-8h to UTC+16h of that date.
    """
    d = datetime.date.fromisoformat(date_str)
    day_start = datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ_OFFSET)
    day_end   = day_start + datetime.timedelta(days=1)
    return day_start.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), \
           day_end.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@tool
def list_activities(days_ahead: int = 7, specific_date: str = None) -> str:
    """
    List activities (excludes birthdays).
    Args:
        days_ahead:    How many days into the future to look (default: 7). Ignored if specific_date is set.
        specific_date: Optional. Show only activities on this exact date (YYYY-MM-DD).
                       Use this when the user asks about a specific day, e.g. "what's on 10 Sep?".
    Returns a formatted list of activities.
    """
    try:
        service = _svc()

        if specific_date:
            time_min, time_max = _day_bounds(specific_date)
            label = f"on {specific_date}"
        else:
            time_min = datetime.datetime.utcnow().isoformat() + "Z"
            time_max = (datetime.datetime.utcnow() + datetime.timedelta(days=days_ahead)).isoformat() + "Z"
            label = f"in the next {days_ahead} days"

        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        activities = [
            e for e in result.get("items", [])
            if BIRTHDAY_TAG not in e.get("description", "")
        ]

        if not activities:
            return f"No activities found {label}."

        lines = [f"📅 Activities {label}:"]
        for e in activities:
            lines.append(_fmt_activity(e))
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing activities: {ex}"


@tool
def search_activities(query: str, days_ahead: int = 30, specific_date: str = None) -> str:
    """
    Search for activities matching a keyword (excludes birthdays).
    Args:
        query:         Keyword to search (e.g. 'gym', 'meeting', 'Alice').
        days_ahead:    How far ahead to search (default: 30 days). Ignored if specific_date is set.
        specific_date: Optional. Restrict search to this exact date (YYYY-MM-DD).
    Returns matching activities.
    """
    try:
        service = _svc()

        if specific_date:
            time_min, time_max = _day_bounds(specific_date)
            label = f"matching '{query}' on {specific_date}"
        else:
            time_min = datetime.datetime.utcnow().isoformat() + "Z"
            time_max = (datetime.datetime.utcnow() + datetime.timedelta(days=days_ahead)).isoformat() + "Z"
            label = f"matching '{query}'"

        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        activities = [
            e for e in result.get("items", [])
            if BIRTHDAY_TAG not in e.get("description", "")
        ]

        if not activities:
            return f"No activities found {label}."

        lines = [f"🔍 Activities {label}:"]
        for e in activities:
            lines.append(_fmt_activity(e))
        return "\n".join(lines)
    except Exception as ex:
        return f"Error searching activities: {ex}"


@tool
def update_activity(
    event_id: str,
    title: str = None,
    date: str = None,
    start_time: str = None,
    end_time: str = None,
    description: str = None,
    location: str = None,
) -> str:
    """
    Update an existing activity by its ID.
    Use list_activities or search_activities to find IDs first.
    Args:
        event_id:    The activity ID.
        title:       New title (optional).
        date:        New date YYYY-MM-DD (optional).
        start_time:  New start time HH:MM SGT (optional).
        end_time:    New end time HH:MM SGT (optional).
        description: New notes/description (optional).
        location:    New location (optional).
    """
    try:
        service = _svc()
        event   = service.events().get(calendarId="primary", eventId=event_id).execute()

        if BIRTHDAY_TAG in event.get("description", ""):
            return "That's a birthday — use update_birthday instead."

        if title is not None:
            event["summary"] = title
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location

        if date or start_time or end_time:
            # Read existing start/end to fill in any unspecified fields
            existing_start = datetime.datetime.fromisoformat(
                event["start"].get("dateTime", event["start"].get("date"))
            )
            existing_end = datetime.datetime.fromisoformat(
                event["end"].get("dateTime", event["end"].get("date"))
            )
            resolved_date  = date       or existing_start.strftime("%Y-%m-%d")
            resolved_start = start_time or existing_start.strftime("%H:%M")
            resolved_end   = end_time   or existing_end.strftime("%H:%M")
            new_start, new_end = _build_datetimes(resolved_date, resolved_start, resolved_end)
            event["start"] = {"dateTime": new_start, "timeZone": TIMEZONE}
            event["end"]   = {"dateTime": new_end,   "timeZone": TIMEZONE}

        updated = service.events().update(
            calendarId="primary", eventId=event_id, body=event
        ).execute()
        return f"✅ Activity updated: '{updated['summary']}' (ID: {updated['id']})"
    except Exception as ex:
        return f"Error updating activity: {ex}"


@tool
def delete_activity(event_id: str) -> str:
    """
    Delete an activity by its ID.
    Use list_activities or search_activities to find IDs first.
    Args:
        event_id: The activity ID to delete.
    """
    try:
        service = _svc()
        event   = service.events().get(calendarId="primary", eventId=event_id).execute()

        if BIRTHDAY_TAG in event.get("description", ""):
            return "That's a birthday — use delete_birthday instead."

        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return "✅ Activity deleted."
    except Exception as ex:
        return f"Error deleting activity: {ex}"


# ---------------------------------------------------------------------------
# Birthday tools  (unchanged)
# ---------------------------------------------------------------------------

@tool
def add_birthday(name: str, birth_date: str, note: str = "") -> str:
    """
    Add a yearly recurring birthday event to the primary calendar.
    Args:
        name:       Person's name, e.g. 'Alice'.
        birth_date: Date in YYYY-MM-DD format, e.g. '1990-07-15'.
        note:       Optional note (gift ideas, etc.).
    """
    try:
        service = _svc()
        event = {
            "summary":     f"🎂 {name}'s Birthday",
            "description": f"{BIRTHDAY_TAG}\n{note}".strip(),
            "start": {"date": birth_date},
            "end":   {"date": birth_date},
            "recurrence": ["RRULE:FREQ=YEARLY"],
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email",  "minutes": 24 * 60},
                    {"method": "popup",  "minutes": 24 * 60},
                ],
            },
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"✅ Birthday added for {name} on {birth_date} (repeats yearly). ID: {created['id']}"
    except Exception as ex:
        return f"Error adding birthday: {ex}"


@tool
def list_birthdays(days_ahead: int = 365, specific_date: str = None) -> str:
    """
    List upcoming birthdays.
    Args:
        days_ahead:    How many days into the future to look (default: 365). Ignored if specific_date is set.
        specific_date: Optional. Show only birthdays that fall on this month and day (YYYY-MM-DD).
                       The year is ignored — use this when the user asks e.g. "whose birthday is on 17 May?".
    Returns a formatted list of birthdays.
    """
    try:
        service = _svc()
        now    = datetime.datetime.utcnow().isoformat() + "Z"
        future = (datetime.datetime.utcnow() + datetime.timedelta(days=days_ahead)).isoformat() + "Z"

        # For a specific date we search a generous 2-year window and filter by month+day
        if specific_date:
            future = (datetime.datetime.utcnow() + datetime.timedelta(days=730)).isoformat() + "Z"

        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=future,
            q=BIRTHDAY_TAG,
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        ).execute()

        events = [e for e in result.get("items", []) if BIRTHDAY_TAG in e.get("description", "")]

        if specific_date:
            target = datetime.date.fromisoformat(specific_date)
            events = [
                e for e in events
                if datetime.date.fromisoformat(e["start"].get("date", "9999-01-01")).month == target.month
                and datetime.date.fromisoformat(e["start"].get("date", "9999-01-01")).day   == target.day
            ]
            label = f"on {target.strftime('%B %d')}"
        else:
            label = f"in the next {days_ahead} days"

        if not events:
            return f"No birthdays found {label}."

        lines = [f"🎂 Birthdays {label}:"]
        for e in events:
            lines.append(f"- [{e['id']}] {e.get('summary','(no title)')} | {e['start'].get('date','?')}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing birthdays: {ex}"


@tool
def search_birthday(name: str = None, specific_date: str = None) -> str:
    """
    Search for a birthday by name and/or specific date.
    Args:
        name:          Name to search for (partial match). Optional if specific_date is provided.
        specific_date: Optional. Find birthdays falling on this month and day (YYYY-MM-DD).
                       The year portion is ignored — only month and day are matched.
    At least one of name or specific_date must be provided.
    """
    try:
        if not name and not specific_date:
            return "Please provide a name or a specific date to search."

        service = _svc()
        now    = datetime.datetime.utcnow().isoformat() + "Z"
        future = (datetime.datetime.utcnow() + datetime.timedelta(days=730)).isoformat() + "Z"

        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=future,
            q=name if name else BIRTHDAY_TAG,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()

        events = [e for e in result.get("items", []) if BIRTHDAY_TAG in e.get("description", "")]

        # Filter by name if provided
        if name:
            events = [e for e in events if name.lower() in e.get("summary", "").lower()]

        # Filter by month+day if provided (year-agnostic)
        if specific_date:
            target = datetime.date.fromisoformat(specific_date)
            events = [
                e for e in events
                if datetime.date.fromisoformat(e["start"].get("date", "9999-01-01")).month == target.month
                and datetime.date.fromisoformat(e["start"].get("date", "9999-01-01")).day   == target.day
            ]

        if not events:
            parts = []
            if name:          parts.append(f"name '{name}'")
            if specific_date:
                target = datetime.date.fromisoformat(specific_date)
                parts.append(f"date {target.strftime('%B %d')}")
            return f"No birthday found for {' and '.join(parts)}."

        label_parts = []
        if name:          label_parts.append(f"'{name}'")
        if specific_date:
            target = datetime.date.fromisoformat(specific_date)
            label_parts.append(target.strftime('%B %d'))

        lines = [f"🔍 Birthday results for {' / '.join(label_parts)}:"]
        for e in events:
            lines.append(f"- [{e['id']}] {e.get('summary','(no title)')} | {e['start'].get('date','?')}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error searching birthday: {ex}"


@tool
def update_birthday(
    event_id: str,
    name: str = None,
    birth_date: str = None,
    note: str = None,
) -> str:
    """
    Update an existing birthday.
    Args:
        event_id:   Event ID to update.
        name:       New name (optional).
        birth_date: New date YYYY-MM-DD (optional).
        note:       New note (optional).
    """
    try:
        service = _svc()
        event   = service.events().get(calendarId="primary", eventId=event_id).execute()

        if name:
            event["summary"] = f"🎂 {name}'s Birthday"
        if birth_date:
            event["start"] = {"date": birth_date}
            event["end"]   = {"date": birth_date}
        if note is not None:
            event["description"] = f"{BIRTHDAY_TAG}\n{note}".strip()

        updated = service.events().update(
            calendarId="primary", eventId=event_id, body=event
        ).execute()
        return f"✅ Birthday updated: {updated['summary']} (ID: {updated['id']})"
    except Exception as ex:
        return f"Error updating birthday: {ex}"


@tool
def delete_birthday(event_id: str) -> str:
    """
    Delete a birthday by its ID.
    Args:
        event_id: The event ID to delete.
    """
    try:
        service = _svc()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"✅ Birthday deleted."
    except Exception as ex:
        return f"Error deleting birthday: {ex}"
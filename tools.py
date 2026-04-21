import datetime
from calendar_auth import get_calendar_service, get_credentials
from googleapiclient.discovery import build
from langchain_core.tools import tool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_tasks_service():
    """Build a Google Tasks API service using the same credentials."""
    creds = get_credentials()
    return build("tasks", "v1", credentials=creds)


def _find_calendar_id(service, keyword: str) -> str | None:
    """Return the calendarId whose summary contains *keyword* (case-insensitive)."""
    calendars = service.calendarList().list().execute().get("items", [])
    for cal in calendars:
        if keyword.lower() in cal.get("summary", "").lower():
            return cal["id"]
    return None


def _fmt_event(e: dict) -> str:
    """One-line summary for a calendar event."""
    start = e["start"].get("dateTime", e["start"].get("date"))
    end   = e["end"].get("dateTime",   e["end"].get("date"))
    if "date" in e["start"]:
        end_date = datetime.datetime.fromisoformat(end) - datetime.timedelta(days=1)
        end = end_date.date().isoformat()
    time_str = start if start == end else f"{start} → {end}"
    return f"- [{e['id']}] {e.get('summary', '(no title)')} | {time_str}"


def _resolve_task_list(tasks_svc, name_or_id: str) -> str:
    """Return the task list ID for a given name, or pass through '@default' / raw IDs."""
    if name_or_id in ("@default", "") or name_or_id.startswith("MTAw"):
        return name_or_id
    result = tasks_svc.tasklists().list(maxResults=20).execute()
    for tl in result.get("items", []):
        if name_or_id.lower() == tl.get("title", "").lower():
            return tl["id"]
    return "@default"


# ---------------------------------------------------------------------------
# Calendar event tools
# ---------------------------------------------------------------------------

@tool
def list_events(days_ahead: int = 7) -> str:
    """
    List upcoming Google Calendar events.
    Args:
        days_ahead: How many days into the future to look (default: 7).
    Returns a formatted list of upcoming events.
    """
    try:
        service = get_calendar_service()
        now    = datetime.datetime.utcnow().isoformat() + "Z"
        future = (datetime.datetime.utcnow() + datetime.timedelta(days=days_ahead)).isoformat() + "Z"

        events_result = service.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=future,
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])
        if not events:
            return f"No upcoming events in the next {days_ahead} days."

        output = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            end   = e["end"].get("dateTime",   e["end"].get("date"))
            if "date" in e["start"]:
                end_date = datetime.datetime.fromisoformat(end) - datetime.timedelta(days=1)
                end = end_date.date().isoformat()
            time_str = start if start == end else f"{start} → {end}"
            output.append(f"- [{e['id']}] {e['summary']} | {time_str}")

        return "\n".join(output)
    except Exception as ex:
        return f"Error listing events: {ex}"


@tool
def create_event(
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    location: str = "",
) -> str:
    """
    Create a new event on Google Calendar.
    Args:
        summary:        Event title.
        start_datetime: Start time in ISO 8601 format, e.g. '2025-04-20T09:00:00+08:00'.
        end_datetime:   End time in ISO 8601 format, e.g. '2025-04-20T10:00:00+08:00'.
        description:    Optional event description.
        location:       Optional location string.
    Returns a confirmation with the event link.
    """
    try:
        service = get_calendar_service()
        event = {
            "summary":     summary,
            "location":    location,
            "description": description,
            "start": {"dateTime": start_datetime, "timeZone": "Asia/Singapore"},
            "end":   {"dateTime": end_datetime,   "timeZone": "Asia/Singapore"},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"✅ Event created: {created['summary']}\nLink: {created.get('htmlLink')}"
    except Exception as ex:
        return f"Error creating event: {ex}"


@tool
def update_event(
    event_id: str,
    summary: str = None,
    start_datetime: str = None,
    end_datetime: str = None,
    description: str = None,
    location: str = None,
) -> str:
    """
    Update an existing Google Calendar event by its ID.
    Use list_events or search_events to find event IDs first.
    Args:
        event_id:       The event ID.
        summary:        New title (optional).
        start_datetime: New start time ISO 8601 (optional).
        end_datetime:   New end time ISO 8601 (optional).
        description:    New description (optional).
        location:       New location (optional).
    """
    try:
        service = get_calendar_service()
        event   = service.events().get(calendarId="primary", eventId=event_id).execute()

        if summary:
            event["summary"] = summary
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location
        if start_datetime:
            event["start"] = {"dateTime": start_datetime, "timeZone": "Asia/Singapore"}
        if end_datetime:
            event["end"]   = {"dateTime": end_datetime,   "timeZone": "Asia/Singapore"}

        updated = service.events().update(
            calendarId="primary", eventId=event_id, body=event
        ).execute()
        return f"✅ Event updated: {updated['summary']}\nLink: {updated.get('htmlLink')}"
    except Exception as ex:
        return f"Error updating event: {ex}"


@tool
def delete_event(event_id: str) -> str:
    """
    Delete a Google Calendar event by its ID.
    Use list_events or search_events to find event IDs first.
    Args:
        event_id: The event ID to delete.
    """
    try:
        service = get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"✅ Event {event_id} deleted successfully."
    except Exception as ex:
        return f"Error deleting event: {ex}"


@tool
def search_events(query: str, days_ahead: int = 30) -> str:
    """
    Search for events matching a query string.
    Args:
        query:      Keyword to search (e.g. 'meeting', 'John').
        days_ahead: How far ahead to search (default: 30 days).
    Returns matching events.
    """
    try:
        service = get_calendar_service()
        now    = datetime.datetime.utcnow().isoformat() + "Z"
        future = (datetime.datetime.utcnow() + datetime.timedelta(days=days_ahead)).isoformat() + "Z"

        events_result = service.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=future,
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        events = events_result.get("items", [])
        if not events:
            return f"No events found matching '{query}'."

        output = [f"🔍 Results for '{query}':"]
        for e in events:
            output.append(_fmt_event(e))
        return "\n".join(output)
    except Exception as ex:
        return f"Error searching events: {ex}"


# ---------------------------------------------------------------------------
# Birthday tools
# ---------------------------------------------------------------------------

BIRTHDAY_TAG = "#birthday"


@tool
def add_birthday(
    name: str,
    birth_date: str,
    note: str = "",
) -> str:
    """
    Add a yearly recurring birthday event to the primary calendar.
    Args:
        name:       Person's name, e.g. 'Alice'.
        birth_date: Date in YYYY-MM-DD format, e.g. '1990-07-15'.
        note:       Optional note (gift ideas, etc.).
    Returns confirmation with event link.
    """
    try:
        service = get_calendar_service()
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
        return (
            f"✅ Birthday added for {name} on {birth_date} (repeats yearly).\n"
            f"Link: {created.get('htmlLink')}"
        )
    except Exception as ex:
        return f"Error adding birthday: {ex}"


@tool
def list_birthdays(days_ahead: int = 365) -> str:
    """
    List all birthday events from the primary calendar.
    Args:
        days_ahead: How many days into the future to look (default: 365).
    Returns a formatted list of upcoming birthdays.
    """
    try:
        service = get_calendar_service()
        now    = datetime.datetime.utcnow().isoformat() + "Z"
        future = (datetime.datetime.utcnow() + datetime.timedelta(days=days_ahead)).isoformat() + "Z"

        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=future,
            q=BIRTHDAY_TAG,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()

        events = [e for e in result.get("items", []) if BIRTHDAY_TAG in e.get("description", "")]
        if not events:
            return "No upcoming birthdays found."

        lines = ["🎂 Upcoming Birthdays:"]
        for e in events:
            date  = e["start"].get("date", "?")
            title = e.get("summary", "(no title)")
            lines.append(f"- [{e['id']}] {title} | {date}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing birthdays: {ex}"


@tool
def search_birthday(name: str) -> str:
    """
    Search for a birthday event by person's name.
    Args:
        name: Name to search for (partial match works).
    Returns matching birthday events.
    """
    try:
        service = get_calendar_service()
        now    = datetime.datetime.utcnow().isoformat() + "Z"
        future = (datetime.datetime.utcnow() + datetime.timedelta(days=730)).isoformat() + "Z"

        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=future,
            q=name,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        events = [
            e for e in result.get("items", [])
            if BIRTHDAY_TAG in e.get("description", "")
            and name.lower() in e.get("summary", "").lower()
        ]

        if not events:
            return f"No birthday found for '{name}'."

        lines = [f"🔍 Birthday search results for '{name}':"]
        lines += [_fmt_event(e) for e in events]
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
    Update an existing birthday event.
    Use list_birthdays or search_birthday to find the event ID first.
    Args:
        event_id:   Event ID to update.
        name:       New name (optional).
        birth_date: New date YYYY-MM-DD (optional).
        note:       New note (optional).
    Returns confirmation with updated event link.
    """
    try:
        service = get_calendar_service()
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
        return (
            f"✅ Birthday updated: {updated['summary']}\n"
            f"Link: {updated.get('htmlLink')}"
        )
    except Exception as ex:
        return f"Error updating birthday: {ex}"


@tool
def delete_birthday(event_id: str) -> str:
    """
    Delete a birthday event by its ID.
    Use list_birthdays or search_birthday to find the event ID first.
    Args:
        event_id: The event ID to delete.
    Returns confirmation message.
    """
    try:
        service = get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"✅ Birthday event {event_id} deleted."
    except Exception as ex:
        return f"Error deleting birthday: {ex}"


# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------

@tool
def add_task(
    title: str,
    due_date: str = None,
    notes: str = "",
    task_list: str = "@default",
) -> str:
    """
    Create a new task in Google Tasks.
    Args:
        title:     Task title.
        due_date:  Due date in YYYY-MM-DD format (optional).
        notes:     Additional notes (optional).
        task_list: Task list name or '@default' (default).
    Returns confirmation with task details.
    """
    try:
        tasks_svc = _get_tasks_service()
        list_id   = _resolve_task_list(tasks_svc, task_list)

        task_body: dict = {"title": title, "notes": notes}
        if due_date:
            task_body["due"] = f"{due_date}T00:00:00.000Z"

        created = tasks_svc.tasks().insert(tasklist=list_id, body=task_body).execute()
        due_str = f" | Due: {due_date}" if due_date else ""
        return f"✅ Task created: '{created['title']}'{due_str}\nID: {created['id']}"
    except Exception as ex:
        return f"Error adding task: {ex}"


@tool
def list_tasks(
    task_list: str = "@default",
    show_completed: bool = False,
) -> str:
    """
    List tasks from a Google Tasks list.
    Args:
        task_list:      Task list name or '@default'.
        show_completed: Include completed tasks (default: False).
    Returns a formatted list of tasks.
    """
    try:
        tasks_svc = _get_tasks_service()
        list_id   = _resolve_task_list(tasks_svc, task_list)

        result = tasks_svc.tasks().list(
            tasklist=list_id,
            showCompleted=show_completed,
            showHidden=show_completed,
            maxResults=100,
        ).execute()

        tasks = result.get("items", [])
        if not tasks:
            return "No tasks found."

        lines = [f"📋 Tasks in '{task_list}':"]
        for t in tasks:
            status = "✅" if t.get("status") == "completed" else "⬜"
            due    = f" | Due: {t['due'][:10]}" if t.get("due") else ""
            notes  = f"\n    📝 {t['notes']}" if t.get("notes") else ""
            lines.append(f"{status} [{t['id']}] {t['title']}{due}{notes}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing tasks: {ex}"


@tool
def search_tasks(
    query: str,
    task_list: str = "@default",
) -> str:
    """
    Search for tasks by keyword in their title or notes.
    Args:
        query:     Keyword to search.
        task_list: Task list name or '@default'.
    Returns matching tasks.
    """
    try:
        tasks_svc = _get_tasks_service()
        list_id   = _resolve_task_list(tasks_svc, task_list)

        result = tasks_svc.tasks().list(
            tasklist=list_id,
            showCompleted=True,
            showHidden=True,
            maxResults=100,
        ).execute()

        q       = query.lower()
        matches = [
            t for t in result.get("items", [])
            if q in t.get("title", "").lower() or q in t.get("notes", "").lower()
        ]

        if not matches:
            return f"No tasks found matching '{query}'."

        lines = [f"🔍 Tasks matching '{query}':"]
        for t in matches:
            status = "✅" if t.get("status") == "completed" else "⬜"
            due    = f" | Due: {t['due'][:10]}" if t.get("due") else ""
            lines.append(f"{status} [{t['id']}] {t['title']}{due}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error searching tasks: {ex}"


@tool
def update_task(
    task_id: str,
    title: str = None,
    due_date: str = None,
    notes: str = None,
    mark_completed: bool = False,
    task_list: str = "@default",
) -> str:
    """
    Update an existing task.
    Use list_tasks or search_tasks to find the task ID first.
    Args:
        task_id:        ID of the task to update.
        title:          New title (optional).
        due_date:       New due date YYYY-MM-DD (optional).
        notes:          New notes (optional).
        mark_completed: Set True to mark the task as done (optional).
        task_list:      Task list name or '@default'.
    Returns confirmation message.
    """
    try:
        tasks_svc = _get_tasks_service()
        list_id   = _resolve_task_list(tasks_svc, task_list)
        task      = tasks_svc.tasks().get(tasklist=list_id, task=task_id).execute()

        if title is not None:
            task["title"] = title
        if notes is not None:
            task["notes"] = notes
        if due_date is not None:
            task["due"] = f"{due_date}T00:00:00.000Z"
        if mark_completed:
            task["status"]    = "completed"
            task["completed"] = datetime.datetime.utcnow().isoformat() + "Z"

        updated    = tasks_svc.tasks().update(tasklist=list_id, task=task_id, body=task).execute()
        status_str = " (marked completed)" if mark_completed else ""
        return f"✅ Task updated: '{updated['title']}'{status_str}"
    except Exception as ex:
        return f"Error updating task: {ex}"


@tool
def delete_task(
    task_id: str,
    task_list: str = "@default",
) -> str:
    """
    Delete a task by its ID.
    Use list_tasks or search_tasks to find the task ID first.
    Args:
        task_id:   ID of the task to delete.
        task_list: Task list name or '@default'.
    Returns confirmation message.
    """
    try:
        tasks_svc = _get_tasks_service()
        list_id   = _resolve_task_list(tasks_svc, task_list)
        tasks_svc.tasks().delete(tasklist=list_id, task=task_id).execute()
        return f"✅ Task {task_id} deleted."
    except Exception as ex:
        return f"Error deleting task: {ex}"


@tool
def list_task_lists() -> str:
    """
    List all available Google Task lists for the user.
    Returns all task list names and IDs.
    """
    try:
        tasks_svc = _get_tasks_service()
        result    = tasks_svc.tasklists().list(maxResults=20).execute()
        lists     = result.get("items", [])
        if not lists:
            return "No task lists found."
        lines = ["📚 Your Task Lists:"]
        for tl in lists:
            lines.append(f"- [{tl['id']}] {tl['title']}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing task lists: {ex}"
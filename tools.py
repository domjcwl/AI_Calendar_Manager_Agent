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


def _svc():
    return get_calendar_service(current_user_id.get())


def _iso_now() -> str:
    return datetime.datetime.now(TZ_OFFSET).isoformat()


def _fmt_event(e: dict) -> str:
    """Format a single event as a readable summary line."""
    start = e["start"].get("dateTime", e["start"].get("date", ""))
    end   = e["end"].get("dateTime",   e["end"].get("date", ""))
    try:
        start_dt   = datetime.datetime.fromisoformat(start)
        end_dt     = datetime.datetime.fromisoformat(end)
        is_all_day = "dateTime" not in e["start"]
        if is_all_day:
            display_end = end_dt - datetime.timedelta(days=1)
            time_str = (
                start_dt.strftime("%b %d")
                if start_dt.date() == display_end.date()
                else f"{start_dt.strftime('%b %d')} → {display_end.strftime('%b %d')}"
            )
        elif start_dt.date() == end_dt.date():
            time_str = f"{start_dt.strftime('%b %d, %H:%M')} → {end_dt.strftime('%H:%M')}"
        else:
            time_str = f"{start_dt.strftime('%b %d, %H:%M')} → {end_dt.strftime('%b %d, %H:%M')}"
    except Exception:
        time_str = start

    lines = [f"• [{e['id']}] {e.get('summary', '(no title)')} | {time_str}"]
    if e.get("location"):
        lines.append(f"  📍 {e['location']}")
    attendees = e.get("attendees", [])
    if attendees:
        names = ", ".join(a.get("displayName", a.get("email", "")) for a in attendees[:3])
        extra = f" +{len(attendees) - 3} more" if len(attendees) > 3 else ""
        lines.append(f"  👥 {names}{extra}")
    if e.get("recurrence"):
        rule = e["recurrence"][0].replace("RRULE:FREQ=", "").capitalize()
        lines.append(f"  🔁 {rule}")
    if e.get("status") == "cancelled":
        lines.append("  ❌ CANCELLED")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

@tool
def list_events(
    start_time: str = None,
    end_time: str = None,
    full_text: str = None,
    calendar_id: str = "primary",
    include_all_calendars: bool = False,
    page_size: int = 50,
    order_by: str = "startTime",
    time_zone: str = TIMEZONE,
    event_type_filter: list[str] = None,
) -> str:
    """
    List calendar events matching the given criteria.
    Args:
        start_time:            Start of the time window (ISO 8601, e.g. "2025-06-01T08:00:00+08:00"). Defaults to now.
        end_time:              End of the time window (ISO 8601).
        full_text:             Free-text search across title, description, location, and attendees.
        calendar_id:           A specific calendar to query (default: "primary"). Ignored when include_all_calendars=True.
        include_all_calendars: When True, queries EVERY calendar the user has (primary, holidays,
                               shared, birthdays, etc.) and merges the results. Use this for any
                               general "what do I have?" query so holidays and other calendar
                               events are never missed. Default: False.
        page_size:             Max results per calendar (default: 50).
        order_by:              "startTime" (default) or "lastModified".
        time_zone:             IANA timezone name (default: Asia/Singapore).
        event_type_filter:     Restrict to specific event types: "default", "birthday",
                               "outOfOffice", "focusTime", "workingLocation", "fromGmail".
                               If omitted, all types are returned.
    Returns a combined, chronological list of events across all queried calendars.
    """
    try:
        service = _svc()

        # Resolve the list of calendars to query
        if include_all_calendars:
            cal_list = service.calendarList().list().execute()
            cal_ids  = [
                c["id"] for c in cal_list.get("items", [])
                if c.get("selected", True)          # respect the user's visibility settings
                and c.get("accessRole") != "none"   # skip calendars with no access
            ]
        else:
            cal_ids = [calendar_id]

        time_min = start_time if start_time else _iso_now()

        all_items: list[dict] = []
        for cal_id in cal_ids:
            params: dict = {
                "calendarId":   cal_id,
                "maxResults":   page_size,
                "singleEvents": True,
                "orderBy":      "startTime",
                "timeZone":     time_zone,
                "timeMin":      time_min,
            }
            if end_time:
                params["timeMax"] = end_time
            if full_text:
                params["q"] = full_text
            if event_type_filter:
                params["eventTypes"] = event_type_filter
            # When eventTypes is omitted the API returns all event types —
            # that includes birthdays, holidays, working-location blocks, etc.

            try:
                result = service.events().list(**params).execute()
                all_items.extend(result.get("items", []))
            except Exception:
                pass  # skip calendars we can't read (e.g. broken shared calendars)

        # Deduplicate by event ID (recurring instances can appear across calendars)
        seen: set[str] = set()
        unique: list[dict] = []
        for e in all_items:
            if e["id"] not in seen:
                seen.add(e["id"])
                unique.append(e)

        # Re-sort the merged list by start time
        unique.sort(key=lambda e: e["start"].get("dateTime", e["start"].get("date", "")))

        if not unique:
            label = f"matching '{full_text}'" if full_text else "in that window"
            return f"No events found {label}."

        lines = [f"📅 {len(unique)} event{'s' if len(unique) != 1 else ''}:"]
        for e in unique:
            lines.append(_fmt_event(e))
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing events: {ex}"


@tool
def get_event(event_id: str, calendar_id: str = "primary") -> str:
    """
    Get full details of a single calendar event by its ID.
    Use this when the user asks for notes, attendees, location, or any detail about a specific event.
    Args:
        event_id:    The event ID (obtained from list_events).
        calendar_id: The calendar the event belongs to (default: "primary").
    Returns complete event details.
    """
    try:
        service = _svc()
        e       = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        start = e["start"].get("dateTime", e["start"].get("date", ""))
        end   = e["end"].get("dateTime",   e["end"].get("date", ""))
        try:
            start_dt   = datetime.datetime.fromisoformat(start)
            end_dt     = datetime.datetime.fromisoformat(end)
            is_all_day = "dateTime" not in e["start"]
            if is_all_day:
                display_end = end_dt - datetime.timedelta(days=1)
                time_str = (
                    start_dt.strftime("%A, %B %d %Y")
                    if start_dt.date() == display_end.date()
                    else f"{start_dt.strftime('%A, %B %d %Y')} → {display_end.strftime('%B %d')}"
                )
            elif start_dt.date() == end_dt.date():
                time_str = f"{start_dt.strftime('%A, %B %d %Y, %H:%M')} → {end_dt.strftime('%H:%M')}"
            else:
                time_str = f"{start_dt.strftime('%A, %B %d %Y, %H:%M')} → {end_dt.strftime('%A, %B %d, %H:%M')}"
        except Exception:
            time_str = start

        _RSVP = {"accepted": "✅", "declined": "❌", "tentative": "❓", "needsAction": "⏳"}

        lines = [
            f"📋 <b>{e.get('summary', '(no title)')}</b>",
            f"🕐 {time_str}",
        ]
        if e.get("location"):
            lines.append(f"📍 {e['location']}")
        desc = (e.get("description") or "").strip()
        if desc:
            lines.append(f"📝 {desc}")
        attendees = e.get("attendees", [])
        if attendees:
            lines.append(f"👥 Attendees ({len(attendees)}):")
            for a in attendees:
                icon = _RSVP.get(a.get("responseStatus", ""), "")
                name = a.get("displayName", a.get("email", ""))
                lines.append(f"   {icon} {name}")
        if e.get("recurrence"):
            rule = e["recurrence"][0].replace("RRULE:FREQ=", "").capitalize()
            lines.append(f"🔁 Repeats: {rule}")
        if e.get("hangoutLink"):
            lines.append(f"📹 Meet: {e['hangoutLink']}")
        reminders = e.get("reminders", {})
        if not reminders.get("useDefault") and reminders.get("overrides"):
            mins = reminders["overrides"][0]["minutes"]
            lines.append(f"🔔 Reminder: {mins} min before")
        if e.get("status") == "cancelled":
            lines.append("❌ This event has been cancelled.")
        lines.append(f"\n<i>ID: {e['id']}</i>")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error getting event: {ex}"


@tool
def list_calendars(page_size: int = 100) -> str:
    """
    List all calendars on the user's calendar list.
    Use when the user asks "what calendars do I have?" or to find a non-primary calendar ID.
    Args:
        page_size: Max calendars to return (default: 100).
    Returns a list of calendar names and their IDs.
    """
    try:
        service = _svc()
        result  = service.calendarList().list(maxResults=page_size).execute()
        items   = result.get("items", [])

        if not items:
            return "No calendars found."

        lines = [f"📆 {len(items)} calendar{'s' if len(items) != 1 else ''}:"]
        for cal in items:
            primary_tag = " (primary)" if cal.get("primary") else ""
            access      = cal.get("accessRole", "")
            lines.append(f"• {cal.get('summary', '(unnamed)')} — ID: {cal['id']}{primary_tag} [{access}]")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error listing calendars: {ex}"


@tool
def suggest_time(
    attendee_emails: list[str],
    start_time: str,
    end_time: str,
    duration_minutes: int = 30,
    start_hour: str = "09:00",
    end_hour: str = "18:00",
    exclude_weekends: bool = False,
    max_suggestions: int = 5,
    time_zone: str = TIMEZONE,
) -> str:
    """
    Find available time slots across one or more calendars.
    Use to answer "when are we free?", "find a 1-hour slot this week", "is John free on Monday?".
    Pass "primary" in attendee_emails to include the current user's own calendar.
    Args:
        attendee_emails:  Calendars/emails to check, e.g. ["primary"] or ["primary", "alice@gmail.com"].
        start_time:       Start of the search window (ISO 8601).
        end_time:         End of the search window (ISO 8601).
        duration_minutes: Required slot length in minutes (default: 30).
        start_hour:       Earliest time of day to suggest, HH:MM (default: "09:00").
        end_hour:         Latest time of day to suggest, HH:MM (default: "18:00").
        exclude_weekends: Skip Saturday and Sunday (default: False).
        max_suggestions:  How many slots to return (default: 5).
        time_zone:        IANA timezone name (default: Asia/Singapore).
    Returns a list of available time windows.
    """
    try:
        service = _svc()

        # Resolve "primary" to the actual calendar email
        primary_email = service.calendars().get(calendarId="primary").execute().get("id", "primary")
        resolved = [primary_email if e == "primary" else e for e in attendee_emails]

        fb_result = service.freebusy().query(body={
            "timeMin":  start_time,
            "timeMax":  end_time,
            "timeZone": time_zone,
            "items":    [{"id": email} for email in resolved],
        }).execute()

        # Merge all busy periods across all calendars
        busy: list[tuple[datetime.datetime, datetime.datetime]] = []
        for cal_data in fb_result.get("calendars", {}).values():
            for period in cal_data.get("busy", []):
                busy.append((
                    datetime.datetime.fromisoformat(period["start"]),
                    datetime.datetime.fromisoformat(period["end"]),
                ))
        busy.sort(key=lambda x: x[0])

        # Walk the window day by day, finding free slots
        window_start = datetime.datetime.fromisoformat(start_time)
        window_end   = datetime.datetime.fromisoformat(end_time)
        slot_delta   = datetime.timedelta(minutes=duration_minutes)
        sh, sm = map(int, start_hour.split(":"))
        eh, em = map(int, end_hour.split(":"))

        suggestions: list[tuple[datetime.datetime, datetime.datetime]] = []
        day = window_start.date()

        while day <= window_end.date() and len(suggestions) < max_suggestions:
            if exclude_weekends and day.weekday() >= 5:
                day += datetime.timedelta(days=1)
                continue

            day_open  = datetime.datetime(day.year, day.month, day.day, sh, sm, tzinfo=window_start.tzinfo)
            day_close = datetime.datetime(day.year, day.month, day.day, eh, em, tzinfo=window_start.tzinfo)
            day_open  = max(day_open,  window_start)
            day_close = min(day_close, window_end)

            cursor = day_open
            for bs, be in busy:
                if cursor + slot_delta <= bs:
                    gap_end = min(bs, day_close)
                    while cursor + slot_delta <= gap_end and len(suggestions) < max_suggestions:
                        suggestions.append((cursor, cursor + slot_delta))
                        cursor += slot_delta
                if be > cursor:
                    cursor = be
            while cursor + slot_delta <= day_close and len(suggestions) < max_suggestions:
                suggestions.append((cursor, cursor + slot_delta))
                cursor += slot_delta

            day += datetime.timedelta(days=1)

        if not suggestions:
            return (
                f"No {duration_minutes}-minute slots found between {start_time} and {end_time} "
                f"within {start_hour}–{end_hour}. Try a wider window or shorter duration."
            )

        who = ", ".join(attendee_emails)
        lines = [f"🟢 {len(suggestions)} available {duration_minutes}-min slot{'s' if len(suggestions) != 1 else ''} for {who}:"]
        for s, e in suggestions:
            lines.append(f"  • {s.strftime('%a %b %d, %H:%M')} → {e.strftime('%H:%M')}")
        return "\n".join(lines)
    except Exception as ex:
        return f"Error suggesting times: {ex}"


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@tool
def create_event(
    summary: str,
    start_time: str,
    end_time: str,
    time_zone: str = TIMEZONE,
    description: str = "",
    location: str = "",
    attendee_emails: list[str] = None,
    all_day: bool = False,
    recurrence: list[str] = None,
    color_id: str = None,
    add_google_meet: bool = False,
    calendar_id: str = "primary",
) -> str:
    """
    Create a new calendar event.
    Args:
        summary:           Event title (required).
        start_time:        Start time in ISO 8601 (required), e.g. "2025-06-01T09:00:00+08:00".
                           For all-day events use midnight UTC: "2025-06-01T00:00:00Z".
        end_time:          End time in ISO 8601 (required).
        time_zone:         IANA timezone (default: Asia/Singapore).
        description:       Notes or details (optional). Can contain HTML.
        location:          Location as free-form text (optional).
        attendee_emails:   List of attendee email addresses to invite (optional).
        all_day:           True for an all-day event (optional, default False).
        recurrence:        RRULE strings for recurring events, e.g. ["RRULE:FREQ=WEEKLY"] (optional).
        color_id:          Color ID "1"–"11": 1=Lavender, 2=Sage, 3=Grape, 4=Flamingo, 5=Banana,
                           6=Tangerine, 7=Peacock, 8=Graphite, 9=Blueberry, 10=Basil, 11=Tomato.
        add_google_meet:   Create a Google Meet link (optional, default False).
        calendar_id:       Calendar to add the event to (default: "primary").
    Returns confirmation with the new event ID.
    """
    try:
        service = _svc()
        body: dict = {
            "summary":  summary,
            "start":    {"date": start_time[:10]} if all_day else {"dateTime": start_time, "timeZone": time_zone},
            "end":      {"date": end_time[:10]}   if all_day else {"dateTime": end_time,   "timeZone": time_zone},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendee_emails:
            body["attendees"] = [{"email": em} for em in attendee_emails]
        if recurrence:
            body["recurrence"] = recurrence
        if color_id:
            body["colorId"] = color_id
        if add_google_meet:
            body["conferenceData"] = {"createRequest": {"requestId": f"meet-{summary[:20]}"}}

        created = service.events().insert(
            calendarId=calendar_id,
            body=body,
            conferenceDataVersion=1 if add_google_meet else 0,
            sendUpdates="all" if attendee_emails else "none",
        ).execute()

        start = created["start"].get("dateTime", created["start"].get("date", ""))
        end   = created["end"].get("dateTime",   created["end"].get("date", ""))
        try:
            s_dt = datetime.datetime.fromisoformat(start)
            e_dt = datetime.datetime.fromisoformat(end)
            time_str = (
                s_dt.strftime("%b %d")
                if all_day
                else (
                    f"{s_dt.strftime('%b %d, %H:%M')} → {e_dt.strftime('%H:%M')}"
                    if s_dt.date() == e_dt.date()
                    else f"{s_dt.strftime('%b %d, %H:%M')} → {e_dt.strftime('%b %d, %H:%M')}"
                )
            )
        except Exception:
            time_str = start

        extras = []
        if attendee_emails:
            extras.append(f"📨 Invites sent to: {', '.join(attendee_emails)}")
        if recurrence:
            extras.append(f"🔁 Recurring: {recurrence[0]}")
        if created.get("hangoutLink"):
            extras.append(f"📹 Meet: {created['hangoutLink']}")
        extra_str = ("\n" + "\n".join(extras)) if extras else ""

        return f"✅ Event created: '{summary}' | {time_str}\nID: {created['id']}{extra_str}"
    except Exception as ex:
        return f"Error creating event: {ex}"


@tool
def update_event(
    event_id: str,
    summary: str = None,
    start_time: str = None,
    end_time: str = None,
    description: str = None,
    location: str = None,
    added_attendee_emails: list[str] = None,
    removed_attendee_emails: list[str] = None,
    color_id: str = None,
    add_google_meet: bool = False,
    calendar_id: str = "primary",
) -> str:
    """
    Update an existing calendar event. Only the fields you supply are changed.
    Args:
        event_id:                 The event ID to update (required).
        summary:                  New title (optional).
        start_time:               New start time in ISO 8601 (optional).
        end_time:                 New end time in ISO 8601 (optional).
        description:              New description/notes (optional).
        location:                 New location (optional).
        added_attendee_emails:    Email addresses to add as attendees (optional).
        removed_attendee_emails:  Email addresses to remove from attendees (optional).
        color_id:                 New color ID "1"–"11" (optional).
        add_google_meet:          Add a Google Meet link (optional).
        calendar_id:              Calendar the event belongs to (default: "primary").
    Returns confirmation with the updated event title.
    """
    try:
        service = _svc()
        patch: dict = {}

        if summary     is not None: patch["summary"]     = summary
        if description is not None: patch["description"] = description
        if location    is not None: patch["location"]    = location
        if color_id    is not None: patch["colorId"]     = color_id

        if start_time:
            patch["start"] = {"dateTime": start_time, "timeZone": TIMEZONE}
        if end_time:
            patch["end"]   = {"dateTime": end_time,   "timeZone": TIMEZONE}

        if added_attendee_emails or removed_attendee_emails:
            existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            current  = existing.get("attendees", [])
            if removed_attendee_emails:
                current = [a for a in current if a.get("email") not in removed_attendee_emails]
            if added_attendee_emails:
                current_emails = {a["email"] for a in current}
                current += [{"email": em} for em in added_attendee_emails if em not in current_emails]
            patch["attendees"] = current

        if add_google_meet:
            patch["conferenceData"] = {"createRequest": {"requestId": f"meet-{event_id[:20]}"}}

        updated = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=patch,
            conferenceDataVersion=1 if add_google_meet else 0,
            sendUpdates="all" if (added_attendee_emails or removed_attendee_emails) else "none",
        ).execute()

        return f"✅ Event updated: '{updated.get('summary', event_id)}' (ID: {updated['id']})"
    except Exception as ex:
        return f"Error updating event: {ex}"


@tool
def delete_event(event_id: str, calendar_id: str = "primary") -> str:
    """
    Permanently delete a calendar event by its ID. This action cannot be undone.
    Always confirm with the user before calling this tool.
    Args:
        event_id:    The event ID to delete (required).
        calendar_id: Calendar the event belongs to (default: "primary").
    Returns confirmation of deletion.
    """
    try:
        service = _svc()
        # Fetch title first so the confirmation message is meaningful
        try:
            title = service.events().get(
                calendarId=calendar_id, eventId=event_id
            ).execute().get("summary", "(untitled)")
        except Exception:
            title = event_id

        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates="none",
        ).execute()
        return f"✅ '{title}' deleted."
    except Exception as ex:
        return f"Error deleting event: {ex}"


@tool
def respond_to_event(
    event_id: str,
    response_status: str,
    calendar_id: str = "primary",
    response_comment: str = "",
) -> str:
    """
    RSVP to a calendar event — accept, decline, or mark as tentative.
    Use for: "accept the meeting with Jane", "decline tomorrow's standup", "tentatively accept Friday's event".
    Args:
        event_id:         The event ID to respond to (required).
        response_status:  "accepted", "declined", or "tentative" (required).
        calendar_id:      Calendar the event belongs to (default: "primary").
        response_comment: Optional message to include with the response.
    Returns confirmation of the RSVP status.
    """
    if response_status not in ("accepted", "declined", "tentative"):
        return "response_status must be 'accepted', 'declined', or 'tentative'."
    try:
        service     = _svc()
        user_email  = service.calendars().get(calendarId="primary").execute().get("id", "")
        event       = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        attendees = event.get("attendees", [])
        matched   = False
        for a in attendees:
            if a.get("email") == user_email or a.get("self"):
                a["responseStatus"] = response_status
                if response_comment:
                    a["comment"] = response_comment
                matched = True
                break

        if not matched:
            # User not explicitly in attendees list — add them
            entry = {"email": user_email, "responseStatus": response_status, "self": True}
            if response_comment:
                entry["comment"] = response_comment
            attendees.append(entry)

        updated = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"attendees": attendees},
            sendUpdates="all",
        ).execute()

        icons = {"accepted": "✅", "declined": "❌", "tentative": "❓"}
        label = {"accepted": "Accepted", "declined": "Declined", "tentative": "Tentatively accepted"}
        return f"{icons[response_status]} {label[response_status]}: '{updated.get('summary', event_id)}'"
    except Exception as ex:
        return f"Error responding to event: {ex}"
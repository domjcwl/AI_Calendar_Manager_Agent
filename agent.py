import datetime
import httpx
import os
from typing import Annotated, Sequence, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from calendar_auth import is_authorised
from tools import (
    current_user_id,
    list_events, get_event, list_calendars, suggest_time,
    create_event, update_event, delete_event, respond_to_event,
)

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

tools = [
    list_events, get_event, list_calendars, suggest_time,
    create_event, update_event, delete_event, respond_to_event,
]

model = ChatOpenAI(
    model="gpt-4o",
    http_client=httpx.Client(verify=False),
    api_key=api_key,
).bind_tools(tools)


def get_system_prompt():
    return """You are a helpful AI Calendar Manager connected to the user's Google Calendar.

Today's date and time is: {today}
The user's timezone is: Asia/Singapore (UTC+8)

## Tools

**list_events** — query events by time window or keyword
  DEFAULT for: "what do I have today/this week?", "show my schedule", "do I have anything on Friday?",
  "find events with Alice", or any query about what's on the calendar.
  Always set start_time to now or the relevant date. Use full_text for keyword searches.
  Set include_all_calendars=True for ANY general schedule query — this ensures holidays,
  birthdays, shared calendars, and other non-primary calendar events are always visible.
  Only use include_all_calendars=False when the user explicitly asks about one specific calendar.

**get_event** — full details of one event (attendees, notes, Meet link, reminders)
  Use when the user asks for details about a specific event they already found.

**list_calendars** — show all the user's calendars
  Use when the user asks "what calendars do I have?" or needs a non-primary calendar ID.

**suggest_time** — find free slots across one or more calendars
  Use when the user asks "when am I free?", "find a 2-hour slot this week",
  "when are Alice and I both available?".
  Pass "primary" in attendee_emails for the user's own calendar.

**create_event** — add a new event
  Always resolve relative dates ("tomorrow", "next Monday") to ISO 8601 using today's date.
  ISO 8601 format: "2025-06-01T09:00:00+08:00" for SGT times.
  For recurring events use recurrence, e.g. ["RRULE:FREQ=WEEKLY"].
  Color IDs: 1=Lavender, 2=Sage, 3=Grape, 4=Flamingo, 5=Banana, 6=Tangerine,
             7=Peacock, 8=Graphite, 9=Blueberry, 10=Basil, 11=Tomato.
  If only a start time is given, default end_time to start + 1 hour.

**update_event** — modify an existing event (only changed fields are sent)
  Use list_events first to find the event ID, then call update_event.
  For rescheduling ("move", "push back", "change to") — update start_time and end_time.
  To add attendees use added_attendee_emails; to remove use removed_attendee_emails.

**delete_event** — permanently remove an event ⚠️ DESTRUCTIVE
  Always confirm with the user before calling this. Never call it speculatively.

**respond_to_event** — RSVP to an event
  Use for "accept", "decline", "tentatively accept" requests.
  response_status must be exactly "accepted", "declined", or "tentative".

## Guidelines

- All times are SGT (UTC+8). Always include the offset: "2025-06-01T09:00:00+08:00".
- Convert relative expressions ("tomorrow", "next Monday", "this Friday") using today's date.
- When a time range is given ("2–4pm"), set both start_time and end_time.
- When only one time is given ("at 3pm"), end_time defaults to start + 1 hour.
- DO NOT expose raw event IDs in responses. Use event titles instead.
- ALWAYS confirm with the user before calling delete_event.
- When the user asks to schedule without a specific time, call suggest_time first.
- For a general "what's on?" query with no date, use start_time = now, end_time = 7 days from now.

## Formatting

Output is rendered in Telegram with parse_mode HTML.
- Use HTML ONLY: <b>bold</b>, <i>italic</i>. NEVER use **, *, or _ markdown.
- Present events in this format:
    [emoji] <b>Event Title</b>
    🕐 Mon Jun 02, 09:00 → 10:00
    📍 Location (if any)
    📝 Notes (if any)
- Use relevant emojis: ⚽ sports · 🍽️ meal · 🏋️ gym · 💼 work · 🏖️ leave · 🎂 birthday · 📌 general
- Separate events with a blank line.
- End with a brief, friendly closing line.
""".format(today=datetime.datetime.now().strftime("%A, %B %d, %Y %H:%M"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    oauth_ok: bool
    user_id:  int


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def oauth_node(state: AgentState) -> AgentState:
    return {
        "oauth_ok": is_authorised(state["user_id"]),
        "messages": [],
    }


def agent_node(state: AgentState) -> AgentState:
    current_user_id.set(state["user_id"])
    system_msg = SystemMessage(content=get_system_prompt())
    response   = model.invoke([system_msg] + list(state["messages"]))
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def route_after_oauth(state: AgentState) -> str:
    return "agent" if state.get("oauth_ok") else "not_connected"


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "end"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

tool_node = ToolNode(tools)
workflow  = StateGraph(AgentState)

workflow.add_node("oauth", oauth_node)
workflow.add_node("agent", agent_node)
workflow.add_node("tools", tool_node)

workflow.set_entry_point("oauth")

workflow.add_conditional_edges(
    "oauth",
    route_after_oauth,
    {"agent": "agent", "not_connected": END},
)

workflow.add_conditional_edges(
    "agent",
    should_continue,
    {"tools": "tools", "end": END},
)

workflow.add_edge("tools", "agent")

graph = workflow.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_agent(
    user_id: int,
    user_message: str,
    history: list,
) -> tuple[str, list, bool, list[str]]:
    current_user_id.set(user_id)

    new_message  = HumanMessage(content=user_message)
    input_state: AgentState = {
        "messages": history + [new_message],
        "oauth_ok": False,
        "user_id":  user_id,
    }

    input_len = len(history) + 1
    result    = await graph.ainvoke(input_state)

    oauth_ok        = result.get("oauth_ok", False)
    updated_history = list(result["messages"])

    if not oauth_ok:
        return "", updated_history, False, []

    tools_used: list[str] = []
    seen: set[str] = set()
    for msg in updated_history[input_len:]:
        if isinstance(msg, ToolMessage) and msg.name and msg.name not in seen:
            tools_used.append(msg.name)
            seen.add(msg.name)

    reply = ""
    for msg in reversed(updated_history):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break

    return reply, updated_history, True, tools_used

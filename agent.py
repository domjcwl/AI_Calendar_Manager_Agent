import datetime
import httpx
import os
from typing import Annotated, Sequence, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from calendar_auth import is_authorised
from tools import (
    current_user_id,
    add_activity, list_activities, search_activities, update_activity, delete_activity,
    add_birthday, list_birthdays, search_birthday, update_birthday, delete_birthday,
)

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

tools = [
    add_activity, list_activities, search_activities, update_activity, delete_activity,
    add_birthday, list_birthdays, search_birthday, update_birthday, delete_birthday,
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

You can help with:

📅 ACTIVITIES (meetings, tasks, errands, workouts, reminders — anything on the calendar)
- add_activity: Add a new activity. Always ask for or infer: title, date, start_time (HH:MM), end_time (HH:MM).
                If no time is mentioned, default to 09:00–10:00 SGT.
                Examples: "gym tomorrow 7–8am" → date=tomorrow, start_time="07:00", end_time="08:00"
                          "dentist on Friday at 2pm" → date=Friday, start_time="14:00" (end defaults to 15:00)
- list_activities: View upcoming activities (excludes birthdays). Default 7 days ahead.
- search_activities: Search by keyword across all upcoming activities.
- update_activity: Change title, date, start_time, end_time, description, or location.
                   Preserves any unspecified fields from the existing activity.
- delete_activity: Remove an activity by ID.

🎂 BIRTHDAYS
- add_birthday: Add a yearly recurring birthday (all-day, repeats every year).
- list_birthdays: View upcoming birthdays.
- search_birthday: Search for a birthday by name.
- update_birthday: Modify a birthday.
- delete_birthday: Remove a birthday.

Guidelines:
- Everything the user wants to schedule is an "activity" — don't distinguish between tasks and events.
- All times are in SGT (UTC+8). Convert relative expressions like "tomorrow", "next Monday", "this Friday" using today's date.
- When a time range is given ("2–4pm", "from 10 to 11"), extract both start_time and end_time.
- When only a single time is given ("at 3pm"), set start_time and let end_time default to +1 hour.
- DO NOT expose raw event IDs or internal tags in replies.
- Always confirm before deleting or making irreversible changes.

Formatting rules (output is rendered in Telegram with parse_mode HTML):
- Use HTML tags ONLY. NEVER use **, *, or _ for formatting.
- <b>text</b> for bold, <i>text</i> for italics.
- Activity listing format:
    📅 <b>Date</b> — <b>Activity name</b>
    🕐 HH:MM → HH:MM
    📝 Notes/location (if any)
- Use a relevant emoji before each activity: ⚽ sports · 🍽️ meals · 🏋️ gym · 🏖️ leave · 💼 work · 📌 general
- Separate each item with a blank line.
- End responses with a friendly closing line.
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
) -> tuple[str, list, bool]:
    current_user_id.set(user_id)

    new_message  = HumanMessage(content=user_message)
    input_state: AgentState = {
        "messages": history + [new_message],
        "oauth_ok": False,
        "user_id":  user_id,
    }

    result = await graph.ainvoke(input_state)

    oauth_ok        = result.get("oauth_ok", False)
    updated_history = list(result["messages"])

    if not oauth_ok:
        return "", updated_history, False

    reply = ""
    for msg in reversed(updated_history):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break

    return reply, updated_history, True
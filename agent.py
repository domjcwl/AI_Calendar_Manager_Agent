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
    add_birthday, list_birthdays, search_birthday, update_birthday, delete_birthday,
    add_task, list_task_lists, search_tasks, update_task, delete_task, list_tasks,
    list_events, create_event, update_event, delete_event, search_events,
)

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

tools = [
    add_birthday, list_birthdays, search_birthday, update_birthday, delete_birthday,
    add_task, list_task_lists, search_tasks, update_task, delete_task, list_tasks,
    list_events, create_event, update_event, delete_event, search_events,
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

You can:
- list_events: View upcoming events
- create_event: Add a new event
- update_event: Modify an existing event by ID
- delete_event: Remove an event by ID
- search_events: Search for events by keyword
- add_birthday: Add a new birthday
- list_birthdays: view upcoming birthdays
- search_birthday: search for a birthday by keyword
- update_birthday: Modify an existing birthday
- delete_birthday: Remove a birthday
- add_task: Add a task
- list_task_lists: Discover and view all task list names (e.g. "My Tasks", "Work")
- search_tasks: search for task by keywords
- update_task: Modify an existing task
- delete_task: Remove a task
- list_tasks: view upcoming tasks
- Answer from your own in built knowledge

Guidelines:
- Convert relative times like "tomorrow at 3pm" to ISO 8601 using today's date.
- DO NOT include links with sensitive IDs or secrets in your replies.
- When updating or deleting a birthday/task/event, always double confirm their intent before executing tool call.
- Be concise and always confirm irreversible actions like deletions.

Formatting rules (IMPORTANT):
- Use HTML tags only — the output is rendered in Telegram with parse_mode HTML.
- NEVER use ** or * or _ for formatting under any circumstances.
- Use <b>text</b> for bold, <i>text</i> for italics. Never use * or _ for formatting.
- When listing events, use this structure for each item:
    📅 <b>Date</b> — <b>Event name</b>
    🕐 Time (if applicable)
    📝 Extra details (if any)
- Add a relevant emoji before each event type: 🎂 for birthdays, ⚽ for sports, 🍽️ for meals/lunch, 🏖️ for leave/holidays, 📌 for general events.
- Separate each event with a blank line for readability.
- End responses with a friendly closing line.
""".format(today=datetime.datetime.now().strftime("%A, %B %d, %Y %H:%M"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages:  Annotated[Sequence[BaseMessage], add_messages]
    oauth_ok:  bool
    user_id:   int   # Telegram user ID — threaded through every turn


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def oauth_node(state: AgentState) -> AgentState:
    """
    Check whether valid Google credentials exist for this user.
    Sets oauth_ok=True if authorised, False otherwise.
    """
    return {
        "oauth_ok": is_authorised(state["user_id"]),
        "messages": [],
    }


def agent_node(state: AgentState) -> AgentState:
    # Make the user's identity available to tools via context var
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
    """
    Run one turn of the agent for a specific Telegram user.

    Returns:
        (reply_text, updated_history, oauth_ok)
        reply_text – empty string when oauth_ok is False
        oauth_ok   – False means the user has not connected Google Calendar yet
    """
    # Set context var immediately so any synchronous pre-graph code is covered too
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
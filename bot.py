import asyncio
import logging
import os
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from agent import run_agent
from calendar_auth import is_authorised, start_device_flow, poll_device_flow

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Keep the last N messages in memory to prevent context bloat and slow calls.
MAX_HISTORY = 20

# In-memory store: { user_id: [BaseMessage, ...] }
user_histories: dict[int, list] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trim_history(history: list) -> list:
    return history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history


def markdown_to_html(text: str) -> str:
    """Convert any stray **bold** or *italic* markdown to HTML tags."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'_(.+?)_',       r'<i>\1</i>', text)
    return text


def split_message(text: str, limit: int = 4096) -> list[str]:
    """
    Split a message to respect Telegram's 4096-character limit.
    Prefers splitting on paragraph breaks, then line breaks, then hard-cuts.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)
    return chunks


def tools_footnote(tools_used: list[str]) -> str:
    if not tools_used:
        return ""
    labels = " · ".join(f"<code>{t}</code>" for t in tools_used)
    return f"\n\n<i>🔧 {labels}</i>"


async def _keep_typing(chat_id: int, bot, stop: asyncio.Event) -> None:
    """Refresh Telegram's typing indicator every 4 s until stop is set."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=4)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Static text
# ---------------------------------------------------------------------------

GREETING = (
    "👋 <b>Hello! I'm your AI Calendar Manager.</b>\n\n"
    "I can read and manage your Google Calendar through natural conversation.\n\n"
    "To get started, send /connect to link your Google account.\n"
    "You'll be given a short code to enter on Google's website — "
    "works from any browser, including your phone.\n\n"
    "Send /help to see what I can do."
)

HELP_TEXT = (
    "<b>What I can do:</b>\n\n"
    "📅 <b>View your schedule</b>\n"
    "  • 'What do I have this week?'\n"
    "  • 'Do I have anything on Friday afternoon?'\n"
    "  • 'Find all my meetings with Alice'\n\n"
    "➕ <b>Create events</b>\n"
    "  • 'Add gym tomorrow at 7am for 1 hour'\n"
    "  • 'Schedule a weekly standup every Monday at 9am'\n"
    "  • 'Book a team lunch on Friday at noon with bob@work.com'\n\n"
    "✏️ <b>Update &amp; reschedule</b>\n"
    "  • 'Move my dentist to next Thursday at 2pm'\n"
    "  • 'Add Alice to the Friday meeting'\n"
    "  • 'Change the project review title to Q2 Review'\n\n"
    "🔍 <b>Find free time</b>\n"
    "  • 'When am I free this week for a 2-hour block?'\n"
    "  • 'Find a 30-minute slot on Thursday between 10am and 5pm'\n\n"
    "✅ <b>RSVP to events</b>\n"
    "  • 'Accept the meeting with Jane'\n"
    "  • 'Decline tomorrow\'s standup'\n\n"
    "<b>Commands</b>\n"
    "/connect — Link your Google Calendar\n"
    "/status  — Check your connection status\n"
    "/clear   — Reset conversation history\n"
    "/help    — Show this message"
)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text(GREETING, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_authorised(user_id):
        await update.message.reply_text(
            "✅ <b>Google Calendar is connected.</b>\n\n"
            "Just send me a message — try <i>'What do I have today?'</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "🔗 <b>Google Calendar is not connected.</b>\n\n"
            "Send /connect to link your account.",
            parse_mode="HTML",
        )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text(
        "🗑️ Conversation history cleared. Fresh start!",
        parse_mode="HTML",
    )


async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_authorised(user_id):
        await update.message.reply_text(
            "✅ <b>Your Google Calendar is already connected!</b>\n\n"
            "Just send me a message to get started.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "⏳ Starting Google authorisation — one moment…",
        parse_mode="HTML",
    )

    try:
        flow = start_device_flow()
    except Exception as e:
        logging.error(f"Device flow start error for user {user_id}: {e}")
        await update.message.reply_text(
            f"❌ Could not start authorisation.\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    user_code        = flow["user_code"]
    verification_url = flow["verification_url"]
    device_code      = flow["device_code"]
    expires_in       = flow.get("expires_in", 1800)
    interval         = flow.get("interval", 5)

    await update.message.reply_text(
        f"🔐 <b>Connect Google Calendar</b>\n\n"
        f"<b>Step 1</b> — Open this link:\n"
        f'<a href="{verification_url}">{verification_url}</a>\n\n'
        f"<b>Step 2</b> — Enter this code:\n"
        f"<b><code>{user_code}</code></b>\n\n"
        f"⏱️ Code expires in {expires_in // 60} minutes.\n\n"
        f"I'll let you know as soon as access is approved.",
        parse_mode="HTML",
    )

    asyncio.create_task(
        _wait_for_oauth(
            update=update,
            user_id=user_id,
            device_code=device_code,
            interval=interval,
            expires_in=expires_in,
        )
    )


async def _wait_for_oauth(
    update: Update,
    user_id: int,
    device_code: str,
    interval: int,
    expires_in: int,
):
    creds = await poll_device_flow(user_id, device_code, interval, expires_in)
    if creds:
        await update.message.reply_text(
            "✅ <b>Google Calendar connected!</b>\n\n"
            "You're all set. Try: <i>'What do I have this week?'</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "⏰ Authorisation timed out or was denied.\n"
            "Send /connect to try again.",
            parse_mode="HTML",
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Unknown command. Send /help to see available commands.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_text = update.message.text.strip()

    if not user_text:
        return

    if user_id not in user_histories:
        user_histories[user_id] = []

    # Start typing indicator and keep it alive for the duration of the agent call
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _keep_typing(update.effective_chat.id, context.bot, stop_typing)
    )

    try:
        reply, updated_history, oauth_ok, tools_used = await run_agent(
            user_id, user_text, user_histories[user_id]
        )
        user_histories[user_id] = _trim_history(updated_history)

    except Exception as e:
        logging.error(f"Agent error for user {user_id}: {e}", exc_info=True)
        stop_typing.set()
        typing_task.cancel()
        await update.message.reply_text(
            "❌ Something went wrong processing your request.\n"
            "Please try again, or send /clear to reset if the issue persists.",
            parse_mode="HTML",
        )
        return

    finally:
        stop_typing.set()
        typing_task.cancel()

    if not oauth_ok:
        await update.message.reply_text(
            "🔗 <b>Your Google Calendar isn't connected yet.</b>\n\n"
            "Send /connect to link your account — it only takes a minute.",
            parse_mode="HTML",
        )
        return

    if not reply:
        await update.message.reply_text(
            "✅ Done! Let me know if there's anything else.",
            parse_mode="HTML",
        )
        return

    chunks = split_message(markdown_to_html(reply))
    for i, chunk in enumerate(chunks):
        text = chunk + (tools_footnote(tools_used) if i == len(chunks) - 1 else "")
        await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("status",  status_command))
    app.add_handler(CommandHandler("clear",   clear))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()
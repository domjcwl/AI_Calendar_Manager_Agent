import logging
import asyncio
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

# In-memory store: { user_id: [BaseMessage, ...] }
user_histories: dict[int, list] = {}

GREETING = (
    "👋 <b>Hello! I'm your AI Calendar Manager.</b>\n\n"
    "To get started, I need access to your Google Calendar.\n\n"
    "Send /connect to link your Google account. "
    "You'll be given a short code to enter on Google's website — "
    "you can do this from any browser, including your phone.\n\n"
    "Send /help at any time to see what I can do."
)

HELP_TEXT = (
    "<b>What I can do:</b>\n\n"
    "📅 <b>Events</b> — 'What do I have this week?', 'Schedule a meeting tomorrow at 2pm'\n"
    "🎂 <b>Birthdays</b> — 'Add Alice's birthday on July 15', 'When is John's birthday?'\n"
    "✅ <b>Tasks</b> — 'Add a task to buy groceries', 'Show my tasks'\n\n"
    "<b>Commands</b>\n"
    "/connect — Link your Google Calendar\n"
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


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text("🗑️ Conversation history cleared!")


async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Start the Device Authorization Flow so the user can authorise from any browser.
    No local server or redirect URL is required.
    """
    if is_authorised():
        await update.message.reply_text(
            "✅ Your Google Calendar is already connected! Just send me a message.",
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
        logging.error(f"Device flow start error: {e}")
        await update.message.reply_text(
            f"❌ Auth error: <code>{e}</code>",
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
        f"1️⃣  Open this URL in any browser:\n"
        f"<code>{verification_url}</code>\n\n"
        f"2️⃣  Enter this code when prompted:\n"
        f"<b><code>{user_code}</code></b>\n\n"
        f"⏱️ This code expires in {expires_in // 60} minutes.\n\n"
        f"I'll notify you as soon as you've approved access.",
        parse_mode="HTML",
    )

    # Poll in the background so the bot stays responsive
    asyncio.create_task(
        _wait_for_oauth(
            update=update,
            device_code=device_code,
            interval=interval,
            expires_in=expires_in,
        )
    )


async def _wait_for_oauth(
    update: Update,
    device_code: str,
    interval: int,
    expires_in: int,
):
    """Background task: poll until approved or expired, then notify the user."""
    creds = await poll_device_flow(device_code, interval, expires_in)

    if creds:
        await update.message.reply_text(
            "✅ <b>Google Calendar connected successfully!</b>\n\n"
            "You're all set. Try: <i>'What do I have this week?'</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "⏰ Authorisation timed out or was denied.\n"
            "Send /connect to try again.",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

def markdown_to_html(text: str) -> str:
    """Convert any stray **bold** or *italic* markdown to HTML tags."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'_(.+?)_',       r'<i>\1</i>', text)
    return text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_text = update.message.text.strip()

    if not user_text:
        return

    if user_id not in user_histories:
        user_histories[user_id] = []

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        reply, updated_history, oauth_ok = await run_agent(
            user_text, user_histories[user_id]
        )
        user_histories[user_id] = updated_history

        if not oauth_ok:
            await update.message.reply_text(
                "🔗 Your Google Calendar isn't connected yet.\n\n"
                "Send /connect to link your account — it only takes a minute!",
                parse_mode="HTML",
            )
            return

        if reply:
            for chunk in split_message(reply):
                await update.message.reply_text(
                    markdown_to_html(chunk), parse_mode="HTML"
                )
        else:
            await update.message.reply_text(
                "⚙️ Done! Let me know if you need anything else."
            )

    except Exception as e:
        logging.error(f"Agent error for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ Something went wrong. Please try again or send /clear to reset."
        )


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Split long messages to respect Telegram's 4096-character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("clear",   clear))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()
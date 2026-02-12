#!/usr/bin/env python3
"""
Telegram → Signal Bridge (Multi-Group, Queued, Passcode-Protected)

Features:
  - Async message queue for instant user feedback
  - Retry logic (3 attempts with backoff)
  - Failure notification back to user with original message
  - Multiple Signal groups support
  - Passcode authentication + group selection

Flow:
  1. /login <passcode>  → Authenticate
  2. /groups             → List available Signal groups
  3. /join <group>       → Select which group to forward to
  4. Send any message    → Instantly queued, forwarded in background
  5. User gets ✅ or ❌ notification
"""

import os
import sys
import json
import base64
import asyncio
import logging
import time
import sqlite3
from dataclasses import dataclass, field
import aiohttp
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─── Configuration ───────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://signal-api:8080")
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "")
PASSCODE = os.environ.get("BOT_PASSCODE", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Queue settings
QUEUE_WORKERS = int(os.environ.get("QUEUE_WORKERS", "5"))  # parallel workers
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))  # retry attempts
RETRY_DELAY_BASE = float(
    os.environ.get("RETRY_DELAY", "2.0")
)  # seconds between retries
SEND_TIMEOUT = int(os.environ.get("SEND_TIMEOUT", "30"))  # seconds per request

# ─── Signal Groups Config ───────────────────────────────────────────────────

SIGNAL_GROUPS: dict[str, str] = {}


def load_groups_config():
    global SIGNAL_GROUPS
    raw = os.environ.get("SIGNAL_GROUPS", "")
    if raw:
        try:
            SIGNAL_GROUPS = json.loads(raw)
            logger.info(
                f"Loaded {len(SIGNAL_GROUPS)} group(s): {', '.join(SIGNAL_GROUPS.keys())}"
            )
        except json.JSONDecodeError as e:
            logger.error(f"Invalid SIGNAL_GROUPS JSON: {e}")
            sys.exit(1)
    else:
        single = os.environ.get("SIGNAL_GROUP_ID", "")
        if single:
            SIGNAL_GROUPS["default"] = single
        else:
            logger.error("No groups configured. Set SIGNAL_GROUPS or SIGNAL_GROUP_ID.")
            sys.exit(1)


# ─── User Data ───────────────────────────────────────────────────────────────

users_data: dict[int, dict] = {}
DATA_FILE = "/app/data/users_data.json"

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger("bridge")

# ─── Queue System ────────────────────────────────────────────────────────────

# Database for dashboard
DB_PATH = "/app/data/bridge_logs.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            user_id INTEGER,
            user_name TEXT,
            group_name TEXT,
            message_preview TEXT,
            has_attachment INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            attempts INTEGER DEFAULT 0,
            error TEXT,
            delivered_at REAL
        )
    """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON message_logs(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON message_logs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_group ON message_logs(group_name)")
    conn.commit()
    conn.close()


def db_insert_message(user_id, user_name, group_name, preview, has_attachment=False):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "INSERT INTO message_logs (timestamp, user_id, user_name, group_name, message_preview, has_attachment, status) VALUES (?, ?, ?, ?, ?, ?, 'queued')",
            (
                time.time(),
                user_id,
                user_name,
                group_name,
                preview[:200],
                int(has_attachment),
            ),
        )
        log_id = cur.lastrowid
        conn.commit()
        conn.close()
        return log_id
    except Exception as e:
        logger.error(f"DB insert error: {e}")
        return None


def db_update_message(log_id, status, attempts=1, error=None):
    if not log_id:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        delivered = time.time() if status == "sent" else None
        conn.execute(
            "UPDATE message_logs SET status=?, attempts=?, error=?, delivered_at=? WHERE id=?",
            (status, attempts, error, delivered, log_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB update error: {e}")


@dataclass
class QueuedMessage:
    """A message waiting to be sent to Signal."""

    chat_id: int  # Telegram chat to notify
    group_id: str  # Signal group ID
    group_name: str  # friendly name
    text: str  # message text
    base64_attachments: list[str] | None  # optional attachments
    original_text: str  # original user message (for failure notice)
    queued_at: float = field(default_factory=time.time)
    attempts: int = 0
    db_log_id: int | None = None  # SQLite log row ID


# Global queue and stats
message_queue: asyncio.Queue = None
stats = {
    "queued": 0,
    "sent": 0,
    "failed": 0,
    "retried": 0,
}

# Shared aiohttp session
http_session: aiohttp.ClientSession = None
bot_instance: Bot = None


async def send_to_signal(msg: QueuedMessage) -> bool:
    """Send a message to Signal via the REST API. Returns True on success."""
    url = f"{SIGNAL_API_URL}/v2/send"
    payload = {
        "message": msg.text,
        "number": SIGNAL_PHONE_NUMBER,
        "recipients": [msg.group_id],
    }
    if msg.base64_attachments:
        payload["base64_attachments"] = msg.base64_attachments

    try:
        async with http_session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT)
        ) as resp:
            if resp.status == 200 or resp.status == 201:
                return True
            body = await resp.text()
            logger.error(f"Signal API returned {resp.status}: {body}")
            return False
    except asyncio.TimeoutError:
        logger.error("Signal API request timed out")
        return False
    except Exception as e:
        logger.error(f"Signal API error: {e}")
        return False


async def notify_user(chat_id: int, text: str):
    """Send a notification to the Telegram user."""
    try:
        await bot_instance.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Failed to notify user {chat_id}: {e}")


async def queue_worker(worker_id: int):
    """Worker that processes messages from the queue."""
    logger.info(f"Queue worker #{worker_id} started")

    while True:
        msg = await message_queue.get()

        try:
            msg.attempts += 1
            success = await send_to_signal(msg)

            if success:
                stats["sent"] += 1
                db_update_message(msg.db_log_id, "sent", msg.attempts)
                logger.info(
                    f"[Worker #{worker_id}] ✅ Sent to [{msg.group_name}] "
                    f"(attempt {msg.attempts}, queue size: {message_queue.qsize()})"
                )
                await notify_user(msg.chat_id, f"✅ Sent to [{msg.group_name}]")

            elif msg.attempts < MAX_RETRIES:
                # Retry with backoff
                stats["retried"] += 1
                delay = RETRY_DELAY_BASE * msg.attempts
                logger.warning(
                    f"[Worker #{worker_id}] Retry {msg.attempts}/{MAX_RETRIES} "
                    f"for [{msg.group_name}] in {delay}s"
                )
                await asyncio.sleep(delay)
                await message_queue.put(msg)  # re-queue

            else:
                # All retries exhausted
                stats["failed"] += 1
                db_update_message(
                    msg.db_log_id, "failed", msg.attempts, "Timed out after all retries"
                )
                logger.error(
                    f"[Worker #{worker_id}] ❌ Failed after {MAX_RETRIES} attempts "
                    f"for [{msg.group_name}]"
                )
                fail_text = (
                    f"❌ Failed to deliver to [{msg.group_name}] after {MAX_RETRIES} attempts.\n\n"
                    f"Your original message:\n"
                    f"─────────────────\n"
                    f"{msg.original_text}\n"
                    f"─────────────────\n\n"
                    f"You can copy and send it directly on Signal."
                )
                await notify_user(msg.chat_id, fail_text)

        except Exception as e:
            logger.error(f"[Worker #{worker_id}] Unexpected error: {e}")
            stats["failed"] += 1
            db_update_message(msg.db_log_id, "failed", msg.attempts, str(e))
        finally:
            message_queue.task_done()


async def enqueue_message(
    chat_id: int,
    group_id: str,
    group_name: str,
    text: str,
    original_text: str,
    user_id: int = 0,
    user_name: str = "Unknown",
    base64_attachments: list[str] | None = None,
):
    """Add a message to the send queue and log to DB."""
    # Log to database
    log_id = db_insert_message(
        user_id=user_id,
        user_name=user_name,
        group_name=group_name,
        preview=original_text,
        has_attachment=base64_attachments is not None,
    )

    msg = QueuedMessage(
        chat_id=chat_id,
        group_id=group_id,
        group_name=group_name,
        text=text,
        base64_attachments=base64_attachments,
        original_text=original_text,
        db_log_id=log_id,
    )
    await message_queue.put(msg)
    stats["queued"] += 1
    logger.debug(
        f"Queued message for [{group_name}], queue size: {message_queue.qsize()}"
    )


# ─── Persistence ─────────────────────────────────────────────────────────────


def load_users():
    global users_data
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                raw = json.load(f)
                users_data = {int(k): v for k, v in raw.items()}
                logger.info(f"Loaded {len(users_data)} user(s)")
    except Exception as e:
        logger.warning(f"Could not load data file: {e}")


def save_users():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w") as f:
            json.dump({str(k): v for k, v in users_data.items()}, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save data file: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def validate_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not SIGNAL_PHONE_NUMBER:
        missing.append("SIGNAL_PHONE_NUMBER")
    if not PASSCODE:
        missing.append("BOT_PASSCODE")
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def is_authenticated(user_id: int) -> bool:
    return user_id in users_data


def has_group(user_id: int) -> bool:
    return is_authenticated(user_id) and users_data[user_id].get("group") is not None


def get_user_group_id(user_id: int) -> str | None:
    if not has_group(user_id):
        return None
    group_name = users_data[user_id]["group"]
    return SIGNAL_GROUPS.get(group_name)


def build_message_text(update: Update) -> tuple[str, str]:
    """Build message text. Returns (formatted_text, original_text)."""
    msg = update.message

    if msg.text:
        return msg.text, msg.text
    elif msg.caption:
        return msg.caption, msg.caption
    elif msg.sticker:
        t = f"[Sticker: {msg.sticker.emoji or '🏷️'}]"
        return t, t
    elif msg.voice:
        return "[Voice message]", "[Voice message]"
    elif msg.video_note:
        return "[Video note]", "[Video note]"
    elif msg.contact:
        t = f"[Contact: {msg.contact.first_name} {msg.contact.phone_number}]"
        return t, t
    elif msg.location:
        t = f"[Location: {msg.location.latitude}, {msg.location.longitude}]"
        return t, t
    elif msg.photo:
        return "[Photo]", "[Photo]"
    elif msg.video:
        return "[Video]", "[Video]"
    elif msg.document:
        t = f"[File: {msg.document.file_name or 'unknown'}]"
        return t, t
    elif msg.audio:
        t = f"[Audio: {msg.audio.title or 'unknown'}]"
        return t, t
    elif msg.animation:
        return "[GIF]", "[GIF]"
    elif msg.poll:
        t = f"[Poll: {msg.poll.question}]"
        return t, t
    else:
        return "[Unsupported message type]", "[Unsupported message type]"


async def download_attachment(update: Update) -> str | None:
    msg = update.message
    file_obj = None

    try:
        if msg.photo:
            file_obj = await msg.photo[-1].get_file()
        elif msg.document:
            file_obj = await msg.document.get_file()
        elif msg.video:
            file_obj = await msg.video.get_file()
        elif msg.audio:
            file_obj = await msg.audio.get_file()
        elif msg.voice:
            file_obj = await msg.voice.get_file()
        elif msg.animation:
            file_obj = await msg.animation.get_file()

        if file_obj:
            data = await file_obj.download_as_bytearray()
            return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        logger.warning(f"Could not download attachment: {e}")

    return None


def format_groups_list() -> str:
    lines = ["📋 Available groups:\n"]
    for name in sorted(SIGNAL_GROUPS.keys()):
        lines.append(f"  • {name}")
    lines.append("\nUse /join <group_name> to select one.")
    return "\n".join(lines)


# ─── Command Handlers ────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if has_group(user_id):
        group_name = users_data[user_id]["group"]
        await update.message.reply_text(
            f"✅ You're authenticated and sending to: {group_name}\n\n"
            "Just send me any message and I'll forward it.\n\n"
            "Commands:\n"
            "/groups  — List available groups\n"
            "/switch <group> — Change your group\n"
            "/status  — Check your status\n"
            "/stats   — Queue statistics\n"
            "/logout  — Revoke your access"
        )
    elif is_authenticated(user_id):
        await update.message.reply_text(
            "✅ You're authenticated but haven't joined a group yet.\n\n"
            f"{format_groups_list()}"
        )
    else:
        await update.message.reply_text(
            "👋 Welcome! This bot forwards your messages to a Signal group.\n\n"
            "Step 1: /login <passcode>\n"
            "Step 2: /groups → /join <group_name>\n"
            "Step 3: Send messages!"
        )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if is_authenticated(user_id):
        await update.message.reply_text("✅ You're already authenticated!")
        return

    if not context.args:
        await update.message.reply_text("❌ Usage: /login <passcode>")
        return

    entered = " ".join(context.args)

    if entered == PASSCODE:
        display_name = user.full_name or user.username or str(user_id)
        users_data[user_id] = {"name": display_name, "group": None}
        save_users()
        logger.info(f"User authenticated: {display_name} (ID: {user_id})")

        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Welcome, {display_name}!\n\n"
                f"Now choose a group:\n\n{format_groups_list()}"
            ),
        )
    else:
        logger.warning(f"Failed login: {user.full_name} (ID: {user_id})")
        await update.message.reply_text("❌ Wrong passcode.")


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Authenticate first: /login <passcode>")
        return

    current = users_data[user_id].get("group")
    text = format_groups_list()
    if current:
        text += f"\n\n✅ Currently in: {current}"
    await update.message.reply_text(text)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Authenticate first: /login <passcode>")
        return

    if not context.args:
        await update.message.reply_text(
            f"❌ Usage: /join <group_name>\n\n{format_groups_list()}"
        )
        return

    group_name = " ".join(context.args).strip().lower()
    matched = None
    for name in SIGNAL_GROUPS:
        if name.lower() == group_name:
            matched = name
            break

    if not matched:
        await update.message.reply_text(
            f'❌ Group "{group_name}" not found.\n\n{format_groups_list()}'
        )
        return

    users_data[user_id]["group"] = matched
    save_users()
    logger.info(f"User {users_data[user_id]['name']} joined group: {matched}")
    await update.message.reply_text(
        f"✅ Now sending to: {matched}\n\nSend me any message!"
    )


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_join(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("❌ Not authenticated. Use /login <passcode>")
        return

    data = users_data[user_id]
    group = data.get("group")
    if group:
        await update.message.reply_text(
            f"✅ Authenticated as: {data['name']}\n"
            f"📬 Forwarding to: {group}\n"
            f"📊 Queue size: {message_queue.qsize()}"
        )
    else:
        await update.message.reply_text(
            f"✅ Authenticated as: {data['name']}\n"
            f"⚠️ No group selected. Use /join <group_name>"
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show queue statistics."""
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Authenticate first: /login <passcode>")
        return

    await update.message.reply_text(
        f"📊 Queue Stats\n\n"
        f"  Pending:  {message_queue.qsize()}\n"
        f"  Queued:   {stats['queued']}\n"
        f"  Sent:     {stats['sent']}\n"
        f"  Retried:  {stats['retried']}\n"
        f"  Failed:   {stats['failed']}\n"
        f"  Workers:  {QUEUE_WORKERS}"
    )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in users_data:
        name = users_data.pop(user_id)["name"]
        save_users()
        logger.info(f"User logged out: {name} (ID: {user_id})")
        await update.message.reply_text(
            "👋 Logged out. Use /login <passcode> to re-authenticate."
        )
    else:
        await update.message.reply_text("You're not logged in.")


# ─── Message Handler ─────────────────────────────────────────────────────────


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all non-command messages — queue for Signal delivery."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id

    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Use /login <passcode>")
        return

    if not has_group(user_id):
        await update.message.reply_text(
            f"⚠️ Join a group first.\n\n{format_groups_list()}"
        )
        return

    group_id = get_user_group_id(user_id)
    group_name = users_data[user_id]["group"]

    if not group_id:
        await update.message.reply_text(
            f'❌ Group "{group_name}" no longer configured. Use /groups'
        )
        return

    # Build message
    text, original_text = build_message_text(update)
    attachment = await download_attachment(update)
    attachments = [attachment] if attachment else None

    # Queue it — user gets instant response
    user_name = users_data[user_id].get("name", "Unknown")
    await enqueue_message(
        chat_id=user_id,
        group_id=group_id,
        group_name=group_name,
        text=text,
        original_text=original_text,
        user_id=user_id,
        user_name=user_name,
        base64_attachments=attachments,
    )

    pending = message_queue.qsize()
    if pending > 10:
        await update.message.reply_text(f"📨 Queued! ({pending} messages ahead)")
    else:
        await update.message.reply_text("📨 Queued!")


# ─── Startup / Shutdown ─────────────────────────────────────────────────────


async def post_init(app):
    """Called after the Application is initialized — start queue workers."""
    global message_queue, http_session, bot_instance

    message_queue = asyncio.Queue()
    http_session = aiohttp.ClientSession()
    bot_instance = app.bot

    # Start worker tasks
    for i in range(QUEUE_WORKERS):
        asyncio.create_task(queue_worker(i))

    logger.info(f"Started {QUEUE_WORKERS} queue workers")


async def post_shutdown(app):
    """Clean up on shutdown."""
    if http_session:
        await http_session.close()
    logger.info("Shutdown complete")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    validate_config()
    load_users()
    load_groups_config()
    init_db()

    logger.info("Starting Telegram → Signal bridge (queued, multi-group)...")
    logger.info(f"Signal API: {SIGNAL_API_URL}")
    logger.info(f"Signal number: {SIGNAL_PHONE_NUMBER}")
    logger.info(f"Groups: {list(SIGNAL_GROUPS.keys())}")
    logger.info(f"Workers: {QUEUE_WORKERS}, Retries: {MAX_RETRIES}")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("logout", cmd_logout))

    # Messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    logger.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

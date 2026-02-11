#!/usr/bin/env python3
"""
Telegram → Signal Bridge (Passcode-Protected)

Users DM this bot, authenticate with a passcode, then anything they
send gets forwarded to a configured Signal group via signal-cli-rest-api.
"""

import os
import sys
import json
import base64
import logging
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─── Configuration (from environment variables) ─────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://signal-api:8080")
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "")
SIGNAL_GROUP_ID = os.environ.get("SIGNAL_GROUP_ID", "")
PASSCODE = os.environ.get("BOT_PASSCODE", "")
FORWARD_PREFIX = os.environ.get("FORWARD_PREFIX", "[Telegram]")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Store authenticated user IDs (persisted to disk so it survives restarts)
authenticated_users: dict[int, str] = {}  # user_id → display_name
AUTH_FILE = "/app/data/authenticated_users.json"

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger("bridge")

# ─── Persistence ─────────────────────────────────────────────────────────────


def load_authenticated_users():
    """Load authenticated users from disk (survives restarts)."""
    global authenticated_users
    try:
        os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, "r") as f:
                data = json.load(f)
                authenticated_users = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(authenticated_users)} authenticated user(s)")
    except Exception as e:
        logger.warning(f"Could not load auth file: {e}")


def save_authenticated_users():
    """Save authenticated users to disk."""
    try:
        os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
        with open(AUTH_FILE, "w") as f:
            json.dump({str(k): v for k, v in authenticated_users.items()}, f)
    except Exception as e:
        logger.warning(f"Could not save auth file: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def validate_config():
    """Ensure required env vars are set."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not SIGNAL_PHONE_NUMBER:
        missing.append("SIGNAL_PHONE_NUMBER")
    if not SIGNAL_GROUP_ID:
        missing.append("SIGNAL_GROUP_ID")
    if not PASSCODE:
        missing.append("BOT_PASSCODE")
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def send_to_signal(text: str, base64_attachments: list[str] | None = None):
    """Send a message to the configured Signal group."""
    url = f"{SIGNAL_API_URL}/v2/send"
    payload = {
        "message": text,
        "number": SIGNAL_PHONE_NUMBER,
        "recipients": [SIGNAL_GROUP_ID],
    }
    if base64_attachments:
        payload["base64_attachments"] = base64_attachments

    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info("Message forwarded to Signal successfully.")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send to Signal: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        return False


def build_message_text(update: Update) -> str:
    """Build a human-readable forwarded message string."""
    msg = update.message
    sender = msg.from_user
    sender_name = (
        authenticated_users.get(sender.id, sender.full_name) if sender else "Unknown"
    )

    parts = [f"{FORWARD_PREFIX} {sender_name}:"]

    if msg.text:
        parts.append(msg.text)
    elif msg.caption:
        parts.append(msg.caption)
    elif msg.sticker:
        parts.append(f"[Sticker: {msg.sticker.emoji or '🏷️'}]")
    elif msg.voice:
        parts.append("[Voice message]")
    elif msg.video_note:
        parts.append("[Video note]")
    elif msg.contact:
        parts.append(f"[Contact: {msg.contact.first_name} {msg.contact.phone_number}]")
    elif msg.location:
        parts.append(f"[Location: {msg.location.latitude}, {msg.location.longitude}]")
    elif msg.photo:
        parts.append("[Photo]")
    elif msg.video:
        parts.append("[Video]")
    elif msg.document:
        parts.append(f"[File: {msg.document.file_name or 'unknown'}]")
    elif msg.audio:
        parts.append(f"[Audio: {msg.audio.title or 'unknown'}]")
    elif msg.animation:
        parts.append("[GIF]")
    elif msg.poll:
        parts.append(f"[Poll: {msg.poll.question}]")
    else:
        parts.append("[Unsupported message type]")

    return "\n".join(parts)


async def download_attachment(update: Update) -> str | None:
    """Download a photo/document/video from Telegram and return base64."""
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


# ─── Command Handlers ────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = update.effective_user.id

    if user_id in authenticated_users:
        await update.message.reply_text(
            "✅ You're already authenticated!\n\n"
            "Just send me any message and I'll forward it to the Signal group.\n\n"
            "Commands:\n"
            "/status — Check your auth status\n"
            "/logout — Revoke your access"
        )
    else:
        await update.message.reply_text(
            "👋 Welcome! This bot forwards messages to a Signal group.\n\n"
            "To get started, authenticate with the passcode:\n"
            "/login <passcode>\n\n"
            "Example: /login mysecretcode"
        )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /login <passcode> command."""
    user = update.effective_user
    user_id = user.id

    if user_id in authenticated_users:
        await update.message.reply_text("✅ You're already authenticated!")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Please provide the passcode.\n" "Usage: /login <passcode>"
        )
        return

    entered = " ".join(context.args)

    if entered == PASSCODE:
        display_name = user.full_name or user.username or str(user_id)
        authenticated_users[user_id] = display_name
        save_authenticated_users()
        logger.info(f"User authenticated: {display_name} (ID: {user_id})")

        # Delete the login message so the passcode isn't visible in chat history
        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Welcome, {display_name}! You're now authenticated.\n\n"
                "Send me any message (text, photo, file, etc.) and "
                "I'll forward it to the Signal group."
            ),
        )
    else:
        logger.warning(f"Failed login attempt by {user.full_name} (ID: {user_id})")
        await update.message.reply_text("❌ Wrong passcode. Try again.")


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logout command."""
    user_id = update.effective_user.id

    if user_id in authenticated_users:
        name = authenticated_users.pop(user_id)
        save_authenticated_users()
        logger.info(f"User logged out: {name} (ID: {user_id})")
        await update.message.reply_text(
            "👋 You've been logged out. Use /login <passcode> to re-authenticate."
        )
    else:
        await update.message.reply_text("You're not logged in.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    user_id = update.effective_user.id

    if user_id in authenticated_users:
        await update.message.reply_text(
            f"✅ Authenticated as: {authenticated_users[user_id]}\n"
            "All your messages are being forwarded to the Signal group."
        )
    else:
        await update.message.reply_text(
            "❌ Not authenticated.\n" "Use /login <passcode> to get started."
        )


# ─── Message Handler ─────────────────────────────────────────────────────────


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all non-command messages from authenticated users."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id

    # Check authentication
    if user_id not in authenticated_users:
        await update.message.reply_text(
            "🔒 You need to authenticate first.\n" "Use /login <passcode>"
        )
        return

    # Build the message and forward to Signal
    text = build_message_text(update)
    attachment = await download_attachment(update)
    attachments = [attachment] if attachment else None

    success = send_to_signal(text, base64_attachments=attachments)

    if success:
        await update.message.reply_text("✅ Forwarded to Signal!")
    else:
        await update.message.reply_text("⚠️ Failed to forward. Check the bot logs.")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    validate_config()
    load_authenticated_users()

    logger.info("Starting Telegram → Signal bridge (passcode-protected)...")
    logger.info(f"Signal API: {SIGNAL_API_URL}")
    logger.info(f"Signal number: {SIGNAL_PHONE_NUMBER}")
    logger.info(f"Signal group: {SIGNAL_GROUP_ID}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("status", cmd_status))

    # Forward all other messages (text, photos, files, etc.)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    logger.info("Bot is polling for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

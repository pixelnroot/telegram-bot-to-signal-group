#!/usr/bin/env python3
"""
Telegram → Signal Bridge (Multi-Group, Passcode-Protected)

Flow:
  1. /start            → Welcome message
  2. /login <passcode> → Authenticate (single shared passcode)
  3. /groups           → List available Signal groups
  4. /join <group>     → Select which Signal group to forward to
  5. Send any message  → Forwarded to the user's assigned Signal group
  6. /switch <group>   → Change to a different group
  7. /logout           → Revoke access
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

# ─── Configuration ───────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://signal-api:8080")
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "")
PASSCODE = os.environ.get("BOT_PASSCODE", "")
FORWARD_PREFIX = os.environ.get("FORWARD_PREFIX", "[Telegram]")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ─── Signal Groups Config ───────────────────────────────────────────────────
# Define your groups in SIGNAL_GROUPS env var as JSON:
#   SIGNAL_GROUPS={"general":"group.abc123","alerts":"group.def456","dev":"group.ghi789"}
#
# Users will type /join general, /join alerts, etc.

SIGNAL_GROUPS: dict[str, str] = {}  # name → group_id


def load_groups_config():
    """Load group mappings from SIGNAL_GROUPS env var."""
    global SIGNAL_GROUPS
    raw = os.environ.get("SIGNAL_GROUPS", "")
    if raw:
        try:
            SIGNAL_GROUPS = json.loads(raw)
            logger.info(
                f"Loaded {len(SIGNAL_GROUPS)} Signal group(s): {', '.join(SIGNAL_GROUPS.keys())}"
            )
        except json.JSONDecodeError as e:
            logger.error(f"Invalid SIGNAL_GROUPS JSON: {e}")
            sys.exit(1)
    else:
        # Fallback: single group from SIGNAL_GROUP_ID
        single = os.environ.get("SIGNAL_GROUP_ID", "")
        if single:
            SIGNAL_GROUPS["default"] = single
            logger.info("Using single group mode (SIGNAL_GROUP_ID)")
        else:
            logger.error("No groups configured. Set SIGNAL_GROUPS or SIGNAL_GROUP_ID.")
            sys.exit(1)


# ─── User Data ───────────────────────────────────────────────────────────────
# Structure:
# {
#     user_id: {
#         "name": "John Doe",
#         "group": "general"     ← currently assigned group (None if not yet joined)
#     }
# }

users_data: dict[int, dict] = {}
DATA_FILE = "/app/data/users_data.json"

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger("bridge")

# ─── Persistence ─────────────────────────────────────────────────────────────


def load_users():
    """Load user data from disk."""
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
    """Save user data to disk."""
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w") as f:
            json.dump({str(k): v for k, v in users_data.items()}, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save data file: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def validate_config():
    """Ensure required env vars are set."""
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
    """Get the Signal group ID for a user's assigned group."""
    if not has_group(user_id):
        return None
    group_name = users_data[user_id]["group"]
    return SIGNAL_GROUPS.get(group_name)


def send_to_signal(
    group_id: str, text: str, base64_attachments: list[str] | None = None
) -> bool:
    """Send a message to a specific Signal group."""
    url = f"{SIGNAL_API_URL}/v2/send"
    payload = {
        "message": text,
        "number": SIGNAL_PHONE_NUMBER,
        "recipients": [group_id],
    }
    if base64_attachments:
        payload["base64_attachments"] = base64_attachments

    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info(f"Message forwarded to Signal group {group_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send to Signal: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        return False


def build_message_text(update: Update) -> str:
    """Build a forwarded message string with sender info."""
    msg = update.message
    sender = msg.from_user
    user_id = sender.id if sender else 0
    sender_name = users_data.get(user_id, {}).get(
        "name", sender.full_name if sender else "Unknown"
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
    """Download attachment from Telegram and return base64."""
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
    """Format available groups as a readable list."""
    lines = ["📋 Available groups:\n"]
    for name in sorted(SIGNAL_GROUPS.keys()):
        lines.append(f"  • {name}")
    lines.append("\nUse /join <group_name> to select one.")
    return "\n".join(lines)


# ─── Command Handlers ────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
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
            "Step 1: Authenticate\n"
            "  /login <passcode>\n\n"
            "Step 2: Choose a group\n"
            "  /groups — see available groups\n"
            "  /join <group_name> — select your group\n\n"
            "Step 3: Send messages!\n"
            "  Anything you send will be forwarded to your Signal group."
        )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /login <passcode>."""
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
        users_data[user_id] = {
            "name": display_name,
            "group": None,
        }
        save_users()
        logger.info(f"User authenticated: {display_name} (ID: {user_id})")

        # Delete the login message so passcode isn't visible
        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Welcome, {display_name}! You're now authenticated.\n\n"
                "Now choose a group to send messages to:\n\n"
                f"{format_groups_list()}"
            ),
        )
    else:
        logger.warning(f"Failed login attempt by {user.full_name} (ID: {user_id})")
        await update.message.reply_text("❌ Wrong passcode. Try again.")


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /groups — list available Signal groups."""
    user_id = update.effective_user.id

    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Authenticate first: /login <passcode>")
        return

    current = users_data[user_id].get("group")
    text = format_groups_list()
    if current:
        text += f"\n\n✅ You're currently in: {current}"

    await update.message.reply_text(text)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /join <group_name> — assign user to a Signal group."""
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

    # Case-insensitive lookup
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
        f"✅ You're now sending to: {matched}\n\n"
        "Send me any message and I'll forward it to this Signal group!"
    )


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /switch <group_name> — change to a different group."""
    # Same logic as /join
    await cmd_join(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show current auth & group status."""
    user_id = update.effective_user.id

    if not is_authenticated(user_id):
        await update.message.reply_text("❌ Not authenticated. Use /login <passcode>")
        return

    data = users_data[user_id]
    group = data.get("group")

    if group:
        await update.message.reply_text(
            f"✅ Authenticated as: {data['name']}\n" f"📬 Forwarding to: {group}"
        )
    else:
        await update.message.reply_text(
            f"✅ Authenticated as: {data['name']}\n"
            f"⚠️ No group selected yet. Use /join <group_name>"
        )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logout — remove user."""
    user_id = update.effective_user.id

    if user_id in users_data:
        name = users_data.pop(user_id)["name"]
        save_users()
        logger.info(f"User logged out: {name} (ID: {user_id})")
        await update.message.reply_text(
            "👋 You've been logged out. Use /login <passcode> to re-authenticate."
        )
    else:
        await update.message.reply_text("You're not logged in.")


# ─── Message Handler ─────────────────────────────────────────────────────────


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all non-command messages — forward to Signal."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id

    # Not authenticated
    if not is_authenticated(user_id):
        await update.message.reply_text(
            "🔒 You need to authenticate first.\n" "Use /login <passcode>"
        )
        return

    # Authenticated but no group selected
    if not has_group(user_id):
        await update.message.reply_text(
            f"⚠️ You haven't joined a group yet.\n\n{format_groups_list()}"
        )
        return

    # Get the Signal group ID
    group_id = get_user_group_id(user_id)
    if not group_id:
        group_name = users_data[user_id]["group"]
        await update.message.reply_text(
            f'❌ Group "{group_name}" is no longer configured. Use /groups to pick another.'
        )
        return

    # Build and forward
    text = build_message_text(update)
    attachment = await download_attachment(update)
    attachments = [attachment] if attachment else None

    success = send_to_signal(group_id, text, base64_attachments=attachments)

    group_name = users_data[user_id]["group"]
    if success:
        await update.message.reply_text(f"✅ Forwarded to [{group_name}]")
    else:
        await update.message.reply_text("⚠️ Failed to forward. Check the bot logs.")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    validate_config()
    load_users()
    load_groups_config()

    logger.info("Starting Telegram → Signal bridge (multi-group)...")
    logger.info(f"Signal API: {SIGNAL_API_URL}")
    logger.info(f"Signal number: {SIGNAL_PHONE_NUMBER}")
    logger.info(f"Available groups: {list(SIGNAL_GROUPS.keys())}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logout", cmd_logout))

    # Forward all non-command messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    logger.info("Bot is polling for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

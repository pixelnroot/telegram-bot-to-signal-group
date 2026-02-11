# Telegram → Signal Bridge (Multi-Group)

A Telegram bot where users authenticate with a passcode, choose a Signal group, and then anything they send gets forwarded to that group.

## User Flow

```
/start                  → Welcome & instructions
/login secretcode       → Authenticate
/groups                 → See available Signal groups
/join general           → Select a group to forward to
Send any message        → Forwarded to "general" Signal group
/switch alerts          → Change to a different group
/logout                 → Revoke access
```

Messages appear in Signal as:

```
[Telegram] John Doe:
Hello everyone, here's the update...
```

---

## Setup Guide

### Step 1: Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **bot token**

### Step 2: Configure

Create a `.env` file in the project folder:

```env
# ── Telegram ─────────────────────────────────────────────────────────
# Get this from @BotFather on Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# ── Signal ───────────────────────────────────────────────────────────
# Your Signal phone number in international format
SIGNAL_PHONE_NUMBER=+14155551234

# ── Groups ───────────────────────────────────────────────────────────
# Map of friendly group names → Signal group IDs (JSON format)
# Users will type /join <name> to select their group
# Get group IDs from: curl http://localhost:9922/v1/groups/+YOUR_NUMBER
SIGNAL_GROUPS={"general":"group.abc123","alerts":"group.def456","dev-team":"group.ghi789"}

# ── Authentication ───────────────────────────────────────────────────
# Shared passcode users must enter via /login
BOT_PASSCODE=my-secret-passcode-123

# ── Optional ─────────────────────────────────────────────────────────
FORWARD_PREFIX=[Telegram]
LOG_LEVEL=INFO
```

### Step 3: Start Signal API & Link Your Account

```bash
docker compose up -d signal-api
```

Wait ~10 seconds, then open in your browser:

```
http://localhost:9922/v1/qrcodelink?device_name=telegram-bridge
```

Scan the QR code in Signal app → Settings → Linked Devices → +

### Step 4: Get Your Signal Group IDs

```bash
curl -s http://localhost:9922/v1/groups/+14155551234 | python3 -m json.tool
```

Each group has an `"id"` field like `group.xxxxxxxx`. Map them in your `.env`:

```env
SIGNAL_GROUPS={"general":"group.abc123","alerts":"group.def456"}
```

### Step 5: Launch Everything

```bash
docker compose up -d
```

Check logs:

```bash
docker compose logs -f bridge
```

### Step 6: Test It

1. Open a DM with your bot on Telegram
2. `/login your-passcode`
3. `/groups` to see available groups
4. `/join general` to pick a group
5. Send a message — it appears in the Signal group!

---

## Bot Commands

| Command             | Description                           |
| ------------------- | ------------------------------------- |
| `/start`            | Welcome message & instructions        |
| `/login <passcode>` | Authenticate with the shared passcode |
| `/groups`           | List all available Signal groups      |
| `/join <group>`     | Select which group to forward to      |
| `/switch <group>`   | Change to a different group           |
| `/status`           | Show your auth & group status         |
| `/logout`           | Revoke your access                    |

---

## Configuration

| Variable              | Required | Description                                        |
| --------------------- | -------- | -------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | Yes      | Bot token from @BotFather                          |
| `SIGNAL_PHONE_NUMBER` | Yes      | Signal number in international format              |
| `SIGNAL_GROUPS`       | Yes      | JSON map of group names → Signal group IDs         |
| `BOT_PASSCODE`        | Yes      | Shared passcode for `/login`                       |
| `FORWARD_PREFIX`      | No       | Prefix for messages (default: `[Telegram]`)        |
| `SIGNAL_API_URL`      | No       | Signal API URL (default: `http://signal-api:8080`) |
| `LOG_LEVEL`           | No       | `DEBUG` / `INFO` / `WARNING` / `ERROR`             |

---

## Security Notes

- The `/login` message is auto-deleted after successful authentication so the passcode isn't visible
- User data (auth + group assignment) is persisted to disk in `bridge-data/`
- Users can only forward to groups defined in `SIGNAL_GROUPS`
- Group names are case-insensitive (`/join General` = `/join general`)

---

## Troubleshooting

**"Failed to forward" errors** → Check `docker compose logs signal-api` and verify your number is linked.

**Group ID not found** → Run `curl http://localhost:9922/v1/receive/+YOUR_NUMBER` once to sync, then list groups again.

**Adding new groups** → Update `SIGNAL_GROUPS` in `.env` and restart: `docker compose restart bridge`

---

## License

MIT

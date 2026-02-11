# Telegram → Signal Bridge (Passcode-Protected)

A Telegram bot where users DM the bot, authenticate with a passcode, and then anything they send gets forwarded to a Signal group.

## How It Works

```
User DMs the bot → /login <passcode> → sends messages → bridge.py → Signal group
```

### User Flow

1. User opens a DM with the bot on Telegram
2. Types `/login mysecretcode` to authenticate
3. Now any message they send (text, photo, file, etc.) gets forwarded to the Signal group
4. Messages appear in Signal as: `[Telegram] John Doe: Hello everyone!`

### Bot Commands

| Command         | Description                         |
| --------------- | ----------------------------------- |
| `/start`        | Show welcome message & instructions |
| `/login <code>` | Authenticate with the passcode      |
| `/logout`       | Revoke your access                  |
| `/status`       | Check if you're authenticated       |

---

## Setup Guide

### Step 1: Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **bot token**

### Step 2: Configure

Create a `.env` file in the project folder and paste the following, then fill in your values:

```env
# ── Telegram ─────────────────────────────────────────────────────────
# Get this from @BotFather on Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# ── Signal ───────────────────────────────────────────────────────────
# Your Signal phone number in international format
SIGNAL_PHONE_NUMBER=+14155551234

# The Signal group ID (get it from the API — see Step 4)
SIGNAL_GROUP_ID=group.xxxxxxxxxxxxxxxx

# ── Authentication ───────────────────────────────────────────────────
# Passcode that users must enter via /login to use the bot
BOT_PASSCODE=my-secret-passcode-123

# ── Optional ─────────────────────────────────────────────────────────
# Prefix prepended to every forwarded message
FORWARD_PREFIX=[Telegram]

# Logging level: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=INFO
```

### Step 3: Start Signal API & Link Your Account

```bash
docker compose up -d signal-api
```

Open `http://localhost:8080/v1/qrcodelink?device_name=telegram-bridge` in your browser, then scan the QR code in Signal (Settings → Linked Devices → +).

### Step 4: Get Your Signal Group ID

```bash
curl -s http://localhost:8080/v1/groups/+14155551234 | python3 -m json.tool
```

Find the `"id"` field (e.g. `group.xxxxxxxx`) and add it to `.env` as `SIGNAL_GROUP_ID`.

### Step 5: Launch Everything

```bash
docker compose up -d
```

Check logs: `docker compose logs -f bridge`

### Step 6: Test It

1. Open a DM with your bot on Telegram
2. Send `/login your-passcode-here`
3. Send a test message — it should appear in your Signal group!

---

## Configuration

| Variable              | Required | Description                                        |
| --------------------- | -------- | -------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | Yes      | Bot token from @BotFather                          |
| `SIGNAL_PHONE_NUMBER` | Yes      | Signal number in international format              |
| `SIGNAL_GROUP_ID`     | Yes      | Signal group ID (e.g. `group.xxxxxxxx`)            |
| `BOT_PASSCODE`        | Yes      | Passcode users must enter via `/login`             |
| `FORWARD_PREFIX`      | No       | Prefix for messages (default: `[Telegram]`)        |
| `SIGNAL_API_URL`      | No       | Signal API URL (default: `http://signal-api:8080`) |
| `LOG_LEVEL`           | No       | `DEBUG` / `INFO` / `WARNING` / `ERROR`             |

---

## Security Notes

- The passcode is deleted from chat after a successful login (the bot deletes the message)
- Authenticated users are saved to disk (`bridge-data/` volume), so they survive restarts
- Users can `/logout` to revoke their own access
- All forwarded messages include the sender's Telegram display name

---

## Troubleshooting

**"Failed to forward" errors** → Check `docker compose logs signal-api` and make sure your number is linked.

**Group ID not found** → Run `curl http://localhost:8080/v1/receive/+YOUR_NUMBER` once to sync, then list groups again.

**Bot not responding** → Make sure `docker compose ps` shows both containers running.

---

## License

MIT

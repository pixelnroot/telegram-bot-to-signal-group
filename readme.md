# Telegram → Signal Bridge

A production-ready Telegram bot that forwards messages to Signal groups with passcode authentication, async message queue, retry logic, and a full admin dashboard.

## Features

- 🔐 **Passcode authentication** — users must login before sending
- 📬 **Multi-group support** — users choose which Signal group to forward to
- ⚡ **Async message queue** — 5 parallel workers, instant user feedback
- 🔄 **Auto-retry** — 3 attempts with backoff on failure
- 📨 **Failure recovery** — users get their original message back if delivery fails
- 📊 **Admin dashboard** — real-time web panel with stats, logs, and user management
- 🐳 **Fully Dockerized** — one command to deploy everything

## Architecture

```
Telegram User → Bot → Queue (5 workers) → signal-cli-rest-api → Signal Group
                                              ↓
                                    Admin Dashboard (Flask)
                                    ├── Stats & Charts
                                    ├── Message Logs
                                    └── User Management
```

---

## User Flow

```
/start                  → Welcome & instructions
/login secretcode       → Authenticate
/groups                 → See available Signal groups
/join general           → Select a group to forward to
Send any message        → Instantly queued, forwarded in background
/switch alerts          → Change to a different group
/stats                  → View queue statistics
/logout                 → Revoke access
```

### Bot Commands

| Command             | Description                           |
| ------------------- | ------------------------------------- |
| `/start`            | Welcome message & instructions        |
| `/login <passcode>` | Authenticate with the shared passcode |
| `/groups`           | List all available Signal groups      |
| `/join <group>`     | Select which group to forward to      |
| `/switch <group>`   | Change to a different group           |
| `/status`           | Show your auth & group status         |
| `/stats`            | Show queue statistics                 |
| `/logout`           | Revoke your access                    |

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
# Get group IDs from: curl http://localhost:9922/v1/receive/+YOUR_NUMBER
# Then: curl http://localhost:9922/v1/groups/+YOUR_NUMBER
SIGNAL_GROUPS={"general":"PEjqrb...base64id...","alerts":"abc123...base64id..."}

# ── Authentication ───────────────────────────────────────────────────
# Shared passcode users must enter via /login
BOT_PASSCODE=relay-spark-11

# ── Queue Settings ───────────────────────────────────────────────────
QUEUE_WORKERS=5
SEND_TIMEOUT=30
MAX_RETRIES=3

# ── Admin Dashboard ──────────────────────────────────────────────────
# Password for the web admin panel
ADMIN_PASSWORD=your-admin-password-here

# Secret key for Flask sessions (change to something random)
FLASK_SECRET=change-me-to-random-string-abc123

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
http://your-server-ip:9922/v1/qrcodelink?device_name=telegram-bridge
```

Scan the QR code in Signal app → Settings → Linked Devices → +

### Step 4: Get Your Signal Group IDs

```bash
# Sync groups from Signal
curl -s http://localhost:9922/v1/receive/+YOUR_NUMBER

# List groups
curl -s http://localhost:9922/v1/groups/+YOUR_NUMBER | python3 -m json.tool
```

Copy the group IDs and add them to `SIGNAL_GROUPS` in your `.env`.

### Step 5: Build & Launch Everything

```bash
docker compose build
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

## Admin Dashboard

The bridge includes a web-based admin panel for monitoring and management.

### Access

- **Direct:** `http://your-server-ip:8765`
- **Via domain:** Set up nginx (see below)

### Dashboard Pages

**📊 Overview**

- Total messages, sent, failed, pending counts
- Today's statistics
- Success rate percentage
- 24-hour activity chart (sent vs failed per hour)
- Group breakdown table
- Recent messages feed

**📨 Message Logs**

- Full searchable message history
- Filter by status (sent, failed, queued)
- Filter by group
- Text search across messages and usernames
- Pagination for large datasets

**👥 User Management**

- All authenticated users with stats
- Messages sent count per user
- Last active timestamp
- Group assignment
- Remove users directly from the panel

### Nginx Setup (Optional)

To access the dashboard via a domain:

```bash
# Copy the nginx config
sudo cp nginx-dashboard.conf /etc/nginx/sites-available/bridge-admin

# Edit and change the domain
sudo nano /etc/nginx/sites-available/bridge-admin
# Change: server_name bridge-admin.yourdomain.com;

# Enable the site
sudo ln -s /etc/nginx/sites-available/bridge-admin /etc/nginx/sites-enabled/

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

For HTTPS, add SSL with certbot:

```bash
sudo certbot --nginx -d bridge-admin.yourdomain.com
```

---

## Configuration Reference

| Variable              | Required | Default                  | Description                            |
| --------------------- | -------- | ------------------------ | -------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | Yes      | —                        | Bot token from @BotFather              |
| `SIGNAL_PHONE_NUMBER` | Yes      | —                        | Signal number in international format  |
| `SIGNAL_GROUPS`       | Yes      | —                        | JSON map of group names → Signal IDs   |
| `BOT_PASSCODE`        | Yes      | —                        | Shared passcode for `/login`           |
| `ADMIN_PASSWORD`      | Yes      | `admin123`               | Web dashboard login password           |
| `FLASK_SECRET`        | Yes      | `change-me...`           | Flask session secret key               |
| `QUEUE_WORKERS`       | No       | `5`                      | Parallel send workers                  |
| `SEND_TIMEOUT`        | No       | `30`                     | Seconds per Signal API request         |
| `MAX_RETRIES`         | No       | `3`                      | Retry attempts before failing          |
| `FORWARD_PREFIX`      | No       | `[Telegram]`             | Prefix for forwarded messages          |
| `SIGNAL_API_URL`      | No       | `http://signal-api:8080` | Signal API URL                         |
| `LOG_LEVEL`           | No       | `INFO`                   | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Server Commands Cheat Sheet

```bash
# Start everything
docker compose up -d

# Rebuild after code changes
docker compose build
docker compose up -d --force-recreate

# View bridge logs
docker compose logs -f bridge

# View dashboard logs
docker compose logs -f dashboard

# Quick stats from server
echo "Sent:    $(docker compose logs bridge 2>/dev/null | grep -c '✅ Sent')"
echo "Failed:  $(docker compose logs bridge 2>/dev/null | grep -c '❌ Failed')"

# Restart without losing data
docker compose up -d --force-recreate

# Check all services
docker compose ps
```

---

## Troubleshooting

**"Failed to forward" errors** → Check `docker compose logs signal-api` and verify your number is linked.

**Group ID not found** → Run `curl http://localhost:9922/v1/receive/+YOUR_NUMBER` once to sync, then list groups again.

**Signal API timeouts** → Make sure you're using `MODE=json-rpc` in docker-compose.yml. Check with `curl http://localhost:9922/v1/about`.

**Dashboard not loading** → Check `docker compose logs dashboard` and ensure port 8765 is open.

**Adding new groups** → Update `SIGNAL_GROUPS` in `.env` and run `docker compose up -d --force-recreate`.

**Bot conflict errors** → Only one instance can poll the same bot token. Run `docker compose down` first, then `curl "https://api.telegram.org/botYOUR_TOKEN/deleteWebhook?drop_pending_updates=true"`, wait 10s, then start again.

---

## Security Notes

- The `/login` message is auto-deleted after successful authentication
- User data and message logs are persisted in `bridge-data/` volume
- The admin dashboard is password-protected
- Group names are case-insensitive
- Signal API has no built-in auth — keep port 9922 internal or add nginx basic auth

---

## License

MIT

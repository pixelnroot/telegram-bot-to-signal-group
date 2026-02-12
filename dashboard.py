#!/usr/bin/env python3
"""
Telegram → Signal Bridge — Admin Dashboard

A Flask web dashboard for monitoring and managing the bridge.
Features:
  - Login with admin password
  - Real-time overview (sent, failed, retried, pending)
  - Message log viewer with filters
  - User management (view, remove)
  - Group stats breakdown
  - Live auto-refresh
"""

import os
import json
import time
import sqlite3
import requests
from datetime import datetime
from functools import wraps
from flask import (
    Flask,
    render_template_string,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    flash,
)

# ─── Configuration ───────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.environ.get("FLASK_SECRET", "change-me-to-something-random")
DB_PATH = os.environ.get("DB_PATH", "/app/data/bridge_logs.db")
USERS_FILE = os.environ.get("USERS_FILE", "/app/data/users_data.json")
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://signal-api:8080")
SIGNAL_PHONE_NUMBER = os.environ.get("SIGNAL_PHONE_NUMBER", "")

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ─── Database ────────────────────────────────────────────────────────────────


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
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
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_timestamp ON message_logs(timestamp)
    """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_status ON message_logs(status)
    """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_group ON message_logs(group_name)
    """
    )
    conn.commit()
    conn.close()


# ─── Auth ────────────────────────────────────────────────────────────────────


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


# ─── Helpers ─────────────────────────────────────────────────────────────────


def load_users():
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_users(data):
    try:
        with open(USERS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def time_ago(ts):
    if not ts:
        return "N/A"
    diff = time.time() - ts
    if diff < 60:
        return f"{int(diff)}s ago"
    elif diff < 3600:
        return f"{int(diff/60)}m ago"
    elif diff < 86400:
        return f"{int(diff/3600)}h ago"
    else:
        return f"{int(diff/86400)}d ago"


# ─── Routes ──────────────────────────────────────────────────────────────────


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Wrong password", "error")
    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    conn = get_db()

    # Overall stats
    total = conn.execute("SELECT COUNT(*) FROM message_logs").fetchone()[0]
    sent = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='sent'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='failed'"
    ).fetchone()[0]
    retried = (
        conn.execute(
            "SELECT SUM(attempts) FROM message_logs WHERE attempts > 1"
        ).fetchone()[0]
        or 0
    )
    pending = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='queued'"
    ).fetchone()[0]

    # Today stats
    today_start = time.mktime(
        datetime.now().replace(hour=0, minute=0, second=0).timetuple()
    )
    today_sent = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='sent' AND timestamp > ?",
        (today_start,),
    ).fetchone()[0]
    today_failed = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='failed' AND timestamp > ?",
        (today_start,),
    ).fetchone()[0]

    # Per group stats
    group_stats = conn.execute(
        """
        SELECT group_name,
               COUNT(*) as total,
               SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
        FROM message_logs
        GROUP BY group_name
        ORDER BY total DESC
    """
    ).fetchall()

    # Hourly chart data (last 24h)
    hourly = []
    for i in range(24, 0, -1):
        h_start = time.time() - (i * 3600)
        h_end = time.time() - ((i - 1) * 3600)
        h_sent = conn.execute(
            "SELECT COUNT(*) FROM message_logs WHERE status='sent' AND timestamp BETWEEN ? AND ?",
            (h_start, h_end),
        ).fetchone()[0]
        h_failed = conn.execute(
            "SELECT COUNT(*) FROM message_logs WHERE status='failed' AND timestamp BETWEEN ? AND ?",
            (h_start, h_end),
        ).fetchone()[0]
        hour_label = datetime.fromtimestamp(h_start).strftime("%H:%M")
        hourly.append({"hour": hour_label, "sent": h_sent, "failed": h_failed})

    # Recent messages
    recent = conn.execute(
        """
        SELECT * FROM message_logs ORDER BY timestamp DESC LIMIT 20
    """
    ).fetchall()

    # Users
    users = load_users()
    total_users = len(users)

    conn.close()

    success_rate = round((sent / total * 100), 1) if total > 0 else 0

    return render_template_string(
        DASHBOARD_HTML,
        total=total,
        sent=sent,
        failed=failed,
        retried=retried,
        pending=pending,
        today_sent=today_sent,
        today_failed=today_failed,
        group_stats=group_stats,
        hourly=json.dumps(hourly),
        recent=recent,
        total_users=total_users,
        success_rate=success_rate,
        time_ago=time_ago,
    )


@app.route("/messages")
@login_required
def messages():
    conn = get_db()

    # Filters
    status = request.args.get("status", "all")
    group = request.args.get("group", "all")
    search = request.args.get("search", "")
    page = int(request.args.get("page", 1))
    per_page = 50

    query = "SELECT * FROM message_logs WHERE 1=1"
    params = []

    if status != "all":
        query += " AND status = ?"
        params.append(status)
    if group != "all":
        query += " AND group_name = ?"
        params.append(group)
    if search:
        query += " AND (message_preview LIKE ? OR user_name LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    # Count
    count = conn.execute(
        query.replace("SELECT *", "SELECT COUNT(*)"), params
    ).fetchone()[0]

    # Paginate
    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([per_page, (page - 1) * per_page])

    logs = conn.execute(query, params).fetchall()

    # Get unique groups for filter
    groups = conn.execute(
        "SELECT DISTINCT group_name FROM message_logs ORDER BY group_name"
    ).fetchall()

    total_pages = max(1, (count + per_page - 1) // per_page)
    conn.close()

    return render_template_string(
        MESSAGES_HTML,
        logs=logs,
        groups=groups,
        status=status,
        group=group,
        search=search,
        page=page,
        total_pages=total_pages,
        count=count,
        time_ago=time_ago,
    )


@app.route("/users")
@login_required
def users():
    users = load_users()
    conn = get_db()

    user_stats = []
    for uid, data in users.items():
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM message_logs WHERE user_id = ?", (int(uid),)
        ).fetchone()[0]
        last_msg = conn.execute(
            "SELECT timestamp FROM message_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1",
            (int(uid),),
        ).fetchone()
        user_stats.append(
            {
                "id": uid,
                "name": data.get("name", "Unknown"),
                "group": data.get("group", "None"),
                "messages": msg_count,
                "last_active": last_msg[0] if last_msg else None,
            }
        )

    user_stats.sort(key=lambda x: x["messages"], reverse=True)
    conn.close()

    return render_template_string(USERS_HTML, users=user_stats, time_ago=time_ago)


@app.route("/users/remove/<user_id>", methods=["POST"])
@login_required
def remove_user(user_id):
    users = load_users()
    if user_id in users:
        name = users[user_id].get("name", "Unknown")
        del users[user_id]
        save_users(users)
        flash(f"Removed user: {name}", "success")
    return redirect(url_for("users"))


@app.route("/messages/delete/<int:msg_id>", methods=["POST"])
@login_required
def delete_message(msg_id):
    """Delete a sent message from Signal using remote-delete."""
    conn = get_db()
    msg = conn.execute("SELECT * FROM message_logs WHERE id = ?", (msg_id,)).fetchone()

    if not msg:
        flash("Message not found", "error")
        conn.close()
        return redirect(url_for("messages"))

    if msg["status"] != "sent":
        flash("Can only delete sent messages", "error")
        conn.close()
        return redirect(url_for("messages"))

    signal_ts = msg["signal_timestamp"]
    group_id = msg["group_id"]

    if not signal_ts:
        flash(
            "Cannot delete: no Signal timestamp recorded (message sent before this feature was added)",
            "error",
        )
        conn.close()
        return redirect(url_for("messages"))

    if not group_id:
        flash("Cannot delete: no group ID recorded", "error")
        conn.close()
        return redirect(url_for("messages"))

    # Call Signal API remote-delete endpoint
    try:
        url = f"{SIGNAL_API_URL}/v1/messages/{SIGNAL_PHONE_NUMBER}"
        payload = {
            "recipients": [group_id],
            "timestamp": signal_ts,
        }
        resp = requests.delete(url, json=payload, timeout=30)

        if resp.status_code in (200, 201, 204):
            conn.execute(
                "UPDATE message_logs SET status='deleted' WHERE id=?", (msg_id,)
            )
            conn.commit()
            flash("Message deleted from Signal group", "success")
        else:
            flash(f"Signal API error ({resp.status_code}): {resp.text}", "error")
    except Exception as e:
        flash(f"Delete failed: {str(e)}", "error")

    conn.close()
    return redirect(url_for("messages"))


@app.route("/api/stats")
@login_required
def api_stats():
    """JSON endpoint for live refresh."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM message_logs").fetchone()[0]
    sent = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='sent'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='failed'"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM message_logs WHERE status='queued'"
    ).fetchone()[0]
    users = load_users()
    conn.close()
    return jsonify(
        {
            "total": total,
            "sent": sent,
            "failed": failed,
            "pending": pending,
            "users": len(users),
            "success_rate": round((sent / total * 100), 1) if total > 0 else 0,
        }
    )


# ─── HTML Templates ──────────────────────────────────────────────────────────

BASE_STYLE = """
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e1e4e8; }
    a { color: #58a6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }

    .nav { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 24px; display: flex; align-items: center; gap: 24px; }
    .nav-brand { font-size: 18px; font-weight: 700; color: #fff; }
    .nav-links { display: flex; gap: 16px; }
    .nav-links a { color: #8b949e; font-size: 14px; padding: 6px 12px; border-radius: 6px; }
    .nav-links a:hover, .nav-links a.active { color: #fff; background: #21262d; text-decoration: none; }
    .nav-right { margin-left: auto; }

    .container { max-width: 1200px; margin: 0 auto; padding: 24px; }

    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; }
    .stat-card .label { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
    .stat-card .value { font-size: 32px; font-weight: 700; }
    .stat-card .sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
    .stat-green .value { color: #3fb950; }
    .stat-red .value { color: #f85149; }
    .stat-blue .value { color: #58a6ff; }
    .stat-yellow .value { color: #d29922; }
    .stat-purple .value { color: #bc8cff; }

    .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; margin-bottom: 24px; }
    .card h2 { font-size: 16px; margin-bottom: 16px; color: #f0f6fc; }

    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; padding: 10px 12px; border-bottom: 1px solid #30363d; }
    td { padding: 10px 12px; border-bottom: 1px solid #21262d; font-size: 14px; }
    tr:hover { background: #1c2128; }

    .badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
    .badge-sent { background: #1b3d2f; color: #3fb950; }
    .badge-failed { background: #3d1b1b; color: #f85149; }
    .badge-queued { background: #2d2a1b; color: #d29922; }
    .badge-deleted { background: #2d1b3d; color: #bc8cff; }
    .badge-group { background: #1b2d3d; color: #58a6ff; }

    .filters { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
    .filters select, .filters input { background: #0d1117; border: 1px solid #30363d; color: #e1e4e8; padding: 8px 12px; border-radius: 6px; font-size: 14px; }
    .filters button { background: #238636; color: #fff; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }
    .filters button:hover { background: #2ea043; }

    .btn { display: inline-block; padding: 6px 14px; border-radius: 6px; font-size: 13px; cursor: pointer; border: 1px solid #30363d; background: #21262d; color: #e1e4e8; }
    .btn:hover { background: #30363d; text-decoration: none; }
    .btn-danger { background: #3d1b1b; border-color: #f85149; color: #f85149; }
    .btn-danger:hover { background: #5a1e1e; }

    .pagination { display: flex; gap: 8px; justify-content: center; margin-top: 16px; }
    .pagination a, .pagination span { padding: 6px 12px; border-radius: 6px; font-size: 14px; }
    .pagination .current { background: #238636; color: #fff; }

    .chart-container { height: 200px; display: flex; align-items: flex-end; gap: 4px; padding: 10px 0; }
    .chart-bar-group { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; }
    .chart-bar { width: 100%; border-radius: 3px 3px 0 0; min-height: 2px; transition: height 0.3s; }
    .chart-bar.sent { background: #3fb950; }
    .chart-bar.failed { background: #f85149; }
    .chart-label { font-size: 10px; color: #8b949e; margin-top: 4px; }

    .flash { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
    .flash-success { background: #1b3d2f; color: #3fb950; border: 1px solid #238636; }
    .flash-error { background: #3d1b1b; color: #f85149; border: 1px solid #f85149; }

    .login-container { display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .login-box { background: #161b22; border: 1px solid #30363d; border-radius: 16px; padding: 40px; width: 380px; }
    .login-box h1 { text-align: center; margin-bottom: 8px; font-size: 24px; }
    .login-box p { text-align: center; color: #8b949e; margin-bottom: 24px; font-size: 14px; }
    .login-box input { width: 100%; padding: 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 8px; color: #e1e4e8; font-size: 16px; margin-bottom: 16px; }
    .login-box button { width: 100%; padding: 12px; background: #238636; border: none; border-radius: 8px; color: #fff; font-size: 16px; cursor: pointer; font-weight: 600; }
    .login-box button:hover { background: #2ea043; }

    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    @media (max-width: 768px) { .two-col { grid-template-columns: 1fr; } }

    .live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #3fb950; margin-right: 6px; animation: pulse 2s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

    .msg-preview { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #8b949e; }
</style>
"""

LOGIN_HTML = (
    """
<!DOCTYPE html>
<html>
<head><title>Bridge Admin — Login</title>"""
    + BASE_STYLE
    + """</head>
<body>
<div class="login-container">
    <div class="login-box">
        <h1>🔐 Bridge Admin</h1>
        <p>Telegram → Signal Bridge Dashboard</p>
        {% for msg in get_flashed_messages() %}
        <div class="flash flash-error">{{ msg }}</div>
        {% endfor %}
        <form method="POST">
            <input type="password" name="password" placeholder="Admin password" autofocus>
            <button type="submit">Login</button>
        </form>
    </div>
</div>
</body>
</html>
"""
)

DASHBOARD_HTML = (
    """
<!DOCTYPE html>
<html>
<head>
    <title>Bridge Admin — Dashboard</title>
    <meta http-equiv="refresh" content="30">
    """
    + BASE_STYLE
    + """
</head>
<body>
<nav class="nav">
    <div class="nav-brand">📡 Bridge Admin</div>
    <div class="nav-links">
        <a href="/" class="active">Dashboard</a>
        <a href="/messages">Messages</a>
        <a href="/users">Users</a>
    </div>
    <div class="nav-right">
        <span class="live-dot"></span>Live
        <a href="/logout" class="btn" style="margin-left:12px;">Logout</a>
    </div>
</nav>

<div class="container">
    <div class="stats-grid">
        <div class="stat-card stat-blue">
            <div class="label">Total Messages</div>
            <div class="value">{{ total }}</div>
            <div class="sub">All time</div>
        </div>
        <div class="stat-card stat-green">
            <div class="label">Sent</div>
            <div class="value">{{ sent }}</div>
            <div class="sub">{{ today_sent }} today</div>
        </div>
        <div class="stat-card stat-red">
            <div class="label">Failed</div>
            <div class="value">{{ failed }}</div>
            <div class="sub">{{ today_failed }} today</div>
        </div>
        <div class="stat-card stat-yellow">
            <div class="label">Pending</div>
            <div class="value">{{ pending }}</div>
            <div class="sub">In queue</div>
        </div>
        <div class="stat-card stat-purple">
            <div class="label">Users</div>
            <div class="value">{{ total_users }}</div>
            <div class="sub">Authenticated</div>
        </div>
        <div class="stat-card">
            <div class="label">Success Rate</div>
            <div class="value" style="color:{% if success_rate > 95 %}#3fb950{% elif success_rate > 80 %}#d29922{% else %}#f85149{% endif %}">{{ success_rate }}%</div>
            <div class="sub">Delivery rate</div>
        </div>
    </div>

    <div class="two-col">
        <div class="card">
            <h2>📊 Last 24 Hours</h2>
            <div class="chart-container" id="chart"></div>
        </div>
        <div class="card">
            <h2>📋 Group Breakdown</h2>
            <table>
                <tr><th>Group</th><th>Total</th><th>Sent</th><th>Failed</th></tr>
                {% for g in group_stats %}
                <tr>
                    <td><span class="badge badge-group">{{ g.group_name }}</span></td>
                    <td>{{ g.total }}</td>
                    <td style="color:#3fb950">{{ g.sent }}</td>
                    <td style="color:#f85149">{{ g.failed }}</td>
                </tr>
                {% endfor %}
                {% if not group_stats %}
                <tr><td colspan="4" style="color:#8b949e;text-align:center;padding:20px;">No data yet</td></tr>
                {% endif %}
            </table>
        </div>
    </div>

    <div class="card">
        <h2>🕐 Recent Messages</h2>
        <table>
            <tr><th>Time</th><th>User</th><th>Group</th><th>Message</th><th>Status</th></tr>
            {% for m in recent %}
            <tr>
                <td>{{ time_ago(m.timestamp) }}</td>
                <td>{{ m.user_name }}</td>
                <td><span class="badge badge-group">{{ m.group_name }}</span></td>
                <td class="msg-preview">{{ m.message_preview }}</td>
                <td><span class="badge badge-{{ m.status }}">{{ m.status }}</span></td>
            </tr>
            {% endfor %}
            {% if not recent %}
            <tr><td colspan="5" style="color:#8b949e;text-align:center;padding:20px;">No messages yet</td></tr>
            {% endif %}
        </table>
        {% if recent %}
        <div style="text-align:center;margin-top:12px;">
            <a href="/messages" class="btn">View All Messages →</a>
        </div>
        {% endif %}
    </div>
</div>

<script>
const hourly = {{ hourly|safe }};
const chart = document.getElementById('chart');
const maxVal = Math.max(...hourly.map(h => h.sent + h.failed), 1);
hourly.forEach(h => {
    const group = document.createElement('div');
    group.className = 'chart-bar-group';

    const sentBar = document.createElement('div');
    sentBar.className = 'chart-bar sent';
    sentBar.style.height = (h.sent / maxVal * 160) + 'px';
    sentBar.title = h.sent + ' sent';

    const failBar = document.createElement('div');
    failBar.className = 'chart-bar failed';
    failBar.style.height = (h.failed / maxVal * 160) + 'px';
    failBar.title = h.failed + ' failed';

    const label = document.createElement('div');
    label.className = 'chart-label';
    label.textContent = h.hour;

    group.appendChild(failBar);
    group.appendChild(sentBar);
    group.appendChild(label);
    chart.appendChild(group);
});
</script>
</body>
</html>
"""
)

MESSAGES_HTML = (
    """
<!DOCTYPE html>
<html>
<head><title>Bridge Admin — Messages</title>"""
    + BASE_STYLE
    + """</head>
<body>
<nav class="nav">
    <div class="nav-brand">📡 Bridge Admin</div>
    <div class="nav-links">
        <a href="/">Dashboard</a>
        <a href="/messages" class="active">Messages</a>
        <a href="/users">Users</a>
    </div>
    <div class="nav-right"><a href="/logout" class="btn">Logout</a></div>
</nav>

<div class="container">
    {% for msg in get_flashed_messages(category_filter=['success']) %}
    <div class="flash flash-success">{{ msg }}</div>
    {% endfor %}

    <div class="card">
        <h2>📨 Message Logs ({{ count }} total)</h2>
        <form class="filters" method="GET">
            <select name="status">
                <option value="all" {% if status=='all' %}selected{% endif %}>All Status</option>
                <option value="sent" {% if status=='sent' %}selected{% endif %}>✅ Sent</option>
                <option value="failed" {% if status=='failed' %}selected{% endif %}>❌ Failed</option>
                <option value="queued" {% if status=='queued' %}selected{% endif %}>⏳ Queued</option>
                <option value="deleted" {% if status=='deleted' %}selected{% endif %}>🗑️ Deleted</option>
            </select>
            <select name="group">
                <option value="all" {% if group=='all' %}selected{% endif %}>All Groups</option>
                {% for g in groups %}
                <option value="{{ g.group_name }}" {% if group==g.group_name %}selected{% endif %}>{{ g.group_name }}</option>
                {% endfor %}
            </select>
            <input type="text" name="search" placeholder="Search messages..." value="{{ search }}">
            <button type="submit">Filter</button>
        </form>

        <table>
            <tr><th>Time</th><th>User</th><th>Group</th><th>Message</th><th>Attempts</th><th>Status</th><th>Action</th></tr>
            {% for m in logs %}
            <tr>
                <td>{{ time_ago(m.timestamp) }}</td>
                <td>{{ m.user_name }}</td>
                <td><span class="badge badge-group">{{ m.group_name }}</span></td>
                <td class="msg-preview" title="{{ m.message_preview }}">{{ m.message_preview }}</td>
                <td>{{ m.attempts }}</td>
                <td>
                    <span class="badge badge-{{ m.status }}">{{ m.status }}</span>
                    {% if m.error %}<br><small style="color:#f85149">{{ m.error }}</small>{% endif %}
                </td>
                <td>
                    {% if m.status == 'sent' and m.signal_timestamp %}
                    <form method="POST" action="/messages/delete/{{ m.id }}" style="display:inline" onsubmit="return confirm('Delete this message from Signal?')">
                        <button type="submit" class="btn btn-danger">🗑️ Unsend</button>
                    </form>
                    {% elif m.status == 'deleted' %}
                    <span style="color:#bc8cff;font-size:12px;">Deleted</span>
                    {% else %}
                    <span style="color:#8b949e;font-size:12px;">—</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            {% if not logs %}
            <tr><td colspan="7" style="color:#8b949e;text-align:center;padding:20px;">No messages match your filters</td></tr>
            {% endif %}
        </table>

        {% if total_pages > 1 %}
        <div class="pagination">
            {% if page > 1 %}<a href="?status={{ status }}&group={{ group }}&search={{ search }}&page={{ page-1 }}" class="btn">← Prev</a>{% endif %}
            <span class="current">Page {{ page }} / {{ total_pages }}</span>
            {% if page < total_pages %}<a href="?status={{ status }}&group={{ group }}&search={{ search }}&page={{ page+1 }}" class="btn">Next →</a>{% endif %}
        </div>
        {% endif %}
    </div>
</div>
</body>
</html>
"""
)

USERS_HTML = (
    """
<!DOCTYPE html>
<html>
<head><title>Bridge Admin — Users</title>"""
    + BASE_STYLE
    + """</head>
<body>
<nav class="nav">
    <div class="nav-brand">📡 Bridge Admin</div>
    <div class="nav-links">
        <a href="/">Dashboard</a>
        <a href="/messages">Messages</a>
        <a href="/users" class="active">Users</a>
    </div>
    <div class="nav-right"><a href="/logout" class="btn">Logout</a></div>
</nav>

<div class="container">
    {% for msg in get_flashed_messages(category_filter=['success']) %}
    <div class="flash flash-success">{{ msg }}</div>
    {% endfor %}

    <div class="card">
        <h2>👥 Authenticated Users ({{ users|length }})</h2>
        <table>
            <tr><th>Name</th><th>Telegram ID</th><th>Group</th><th>Messages Sent</th><th>Last Active</th><th>Action</th></tr>
            {% for u in users %}
            <tr>
                <td><strong>{{ u.name }}</strong></td>
                <td style="color:#8b949e">{{ u.id }}</td>
                <td><span class="badge badge-group">{{ u.group or 'None' }}</span></td>
                <td>{{ u.messages }}</td>
                <td>{{ time_ago(u.last_active) }}</td>
                <td>
                    <form method="POST" action="/users/remove/{{ u.id }}" style="display:inline" onsubmit="return confirm('Remove {{ u.name }}?')">
                        <button type="submit" class="btn btn-danger">Remove</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            {% if not users %}
            <tr><td colspan="6" style="color:#8b949e;text-align:center;padding:20px;">No users yet</td></tr>
            {% endif %}
        </table>
    </div>
</div>
</body>
</html>
"""
)

# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, debug=False)

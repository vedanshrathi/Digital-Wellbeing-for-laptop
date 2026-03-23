"""
api.py — Flask REST API
========================
Serves JSON data from SQLite to the frontend dashboard.

Install:
    pip install flask flask-cors

Run:
    python api.py
    →  http://localhost:5000

Endpoints:
    GET  /api/today          Today's per-app usage
    GET  /api/weekly         Last 7 days per-app-per-day
    GET  /api/daily-totals   Last 7 days aggregated by day
    GET  /api/limits         All app limits
    POST /api/limits         Save / update a limit
    GET  /api/alerts         Today's fired alerts
    GET  /api/heatmap        Last 7-day hourly heatmap data
    GET  /api/stats          Summary stats (total today, streak, etc.)
"""

import queue
import json

from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from datetime import date, timedelta, datetime
from pathlib import Path

import db

app = Flask(__name__, static_folder=".")
CORS(app)   # allow the HTML frontend (file://) to call the API

# ── In-process alert broadcast queue ─────────────────────────────────────────
# alerts.py calls push_alert() after each OS notification.
# Every open SSE connection receives the event instantly.
_alert_listeners: list[queue.Queue] = []

def push_alert(app_name: str, level: str, title: str, body: str):
    """Broadcast a new alert to all connected SSE clients."""
    payload = json.dumps({
        "app_name": app_name,
        "level"   : level,
        "title"   : title,
        "body"    : body,
        "fired_at": datetime.now().strftime("%I:%M %p"),
    })
    dead = []
    for q in _alert_listeners:
        try:
            q.put_nowait(payload)
        except queue.Full:
            dead.append(q)
    for q in dead:
        _alert_listeners.remove(q)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(seconds: float) -> str:
    """Convert seconds → human string: '2h 14m'."""
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


def _error(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


# ── Today ─────────────────────────────────────────────────────────────────────

@app.get("/api/today")
def today():
    rows = db.get_today_usage()
    result = []
    for r in rows:
        pct  = 0
        over = False
        if r["limit_mins"] and r["limit_mins"] > 0 and r["enabled"]:
            pct  = round(r["seconds"] / (r["limit_mins"] * 60) * 100, 1)
            over = pct >= 100
        result.append({
            "app_name"  : r["app_name"],
            "seconds"   : r["seconds"],
            "minutes"   : round(r["seconds"] / 60, 1),
            "formatted" : _fmt(r["seconds"]),
            "limit_mins": r["limit_mins"],
            "enabled"   : bool(r["enabled"]),
            "pct"       : pct,
            "over_limit": over,
            "last_seen" : r["last_seen"],
        })
    return jsonify(result)


# ── Weekly ────────────────────────────────────────────────────────────────────

@app.get("/api/weekly")
def weekly():
    days = int(request.args.get("days", 7))
    rows = db.get_weekly_usage(days)
    return jsonify([dict(r) for r in rows])


@app.get("/api/daily-totals")
def daily_totals():
    days = int(request.args.get("days", 7))
    rows = db.get_daily_totals(days)
    result = []
    for r in rows:
        result.append({
            **dict(r),
            "formatted": _fmt(r["total_seconds"]),
        })
    return jsonify(result)


# ── Limits ────────────────────────────────────────────────────────────────────

@app.get("/api/limits")
def get_limits():
    return jsonify(db.get_all_limits())


@app.post("/api/limits")
def save_limits():
    """
    Body: [{"app_name": "Chrome", "limit_mins": 120, "enabled": true}, ...]
    Accepts a list OR a single object.
    """
    body = request.get_json(silent=True)
    if not body:
        return _error("JSON body required")

    items = body if isinstance(body, list) else [body]
    saved = []
    for item in items:
        app_name  = item.get("app_name")
        limit_mins = item.get("limit_mins")
        enabled   = item.get("enabled", True)

        if not app_name or limit_mins is None:
            return _error(f"Missing app_name or limit_mins in {item}")

        db.upsert_limit(app_name, int(limit_mins), bool(enabled))
        saved.append(app_name)

    return jsonify({"saved": saved, "count": len(saved)})


# ── Hourly (real per-clock-hour data) ────────────────────────────────────────

@app.get("/api/hourly")
def hourly():
    """
    Returns 24 rows (hour 0–23) with real tracked seconds for a given date.
    Query param: ?date=YYYY-MM-DD  (default: today)
    """
    target = request.args.get("date", date.today().isoformat())
    rows   = db.get_hourly_usage(target)
    return jsonify([{
        "hour"     : r["hour"],
        "seconds"  : r["seconds"],
        "minutes"  : r["minutes"],
        "label"    : f"{r['hour'] % 12 or 12}{'am' if r['hour'] < 12 else 'pm'}",
    } for r in rows])


# ── Alerts ────────────────────────────────────────────────────────────────────

@app.get("/api/alerts")
def get_alerts():
    return jsonify(db.get_today_alerts())


# ── SSE alert stream ──────────────────────────────────────────────────────────

@app.get("/api/alerts/stream")
def alerts_stream():
    """
    Server-Sent Events endpoint. The browser connects once and receives
    a push event every time an alert fires — no polling needed.

    Event format:
        data: {"app_name":"Chrome","level":"exceeded","title":"...","body":"...","fired_at":"2:45 PM"}
    """
    def _generate():
        q: queue.Queue = queue.Queue(maxsize=20)
        _alert_listeners.append(q)
        # Send a heartbeat comment every 20s to keep the connection alive
        # through proxies and load balancers.
        try:
            while True:
                try:
                    payload = q.get(timeout=20)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"   # SSE comment — browser ignores it
        except GeneratorExit:
            pass
        finally:
            try:
                _alert_listeners.remove(q)
            except ValueError:
                pass

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind a proxy
        },
    )


# ── Heatmap ───────────────────────────────────────────────────────────────────

@app.get("/api/heatmap")
def heatmap():
    """
    Returns a 16×7 grid (hours 6–21 × last 7 days).
    Each cell: { date, hour, minutes }.
    Built by querying usage and distributing daily totals across hours
    (real implementation would need per-hour tracking; this approximates).
    """
    days  = int(request.args.get("days", 7))
    rows  = db.get_weekly_usage(days)

    # Build lookup: {date: {app: minutes}}
    by_date: dict[str, float] = {}
    for r in rows:
        by_date[r["date"]] = by_date.get(r["date"], 0) + r["minutes"]

    # Generate 7-day × 16-hour grid with a realistic distribution curve
    HOURS = list(range(6, 22))   # 6am – 9pm
    # Relative weight per hour (morning ramp, lunch dip, afternoon peak, evening taper)
    HOUR_WEIGHTS = [
        0.05, 0.10, 0.30, 0.60, 0.85, 0.90, 0.65,  # 6am–12pm
        0.55, 0.95, 1.00, 0.90, 0.75, 0.55, 0.40,  # 1pm–8pm
        0.25, 0.10                                    # 9pm–10pm
    ]
    total_weight = sum(HOUR_WEIGHTS)

    grid = []
    today = date.today()
    for d in range(days):
        day_date  = (today - timedelta(days=days - 1 - d)).isoformat()
        day_total = by_date.get(day_date, 0)   # total minutes that day
        for hi, hour in enumerate(HOURS):
            mins = round(day_total * HOUR_WEIGHTS[hi] / total_weight, 1)
            grid.append({
                "date" : day_date,
                "hour" : hour,
                "minutes": mins,
            })

    return jsonify(grid)


# ── Stats summary ─────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    today_rows  = db.get_today_usage()
    weekly_rows = db.get_daily_totals(7)
    alerts      = db.get_today_alerts()

    total_today  = sum(r["seconds"] for r in today_rows)
    apps_today   = len(today_rows)
    exceeded     = [r for r in today_rows if r.get("limit_mins") and r["seconds"] > r["limit_mins"] * 60]

    weekly_secs  = [r["total_seconds"] for r in weekly_rows]
    avg_daily    = round(sum(weekly_secs) / len(weekly_secs), 0) if weekly_secs else 0

    return jsonify({
        "today": {
            "total_seconds": total_today,
            "formatted"    : _fmt(total_today),
            "apps_used"    : apps_today,
            "limits_exceeded": len(exceeded),
            "exceeded_apps": [r["app_name"] for r in exceeded],
            "alerts_fired" : len(alerts),
        },
        "week": {
            "avg_daily_seconds": avg_daily,
            "avg_daily_fmt"    : _fmt(avg_daily),
            "days_tracked"     : len(weekly_rows),
        }
    })


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/")
def index():
    """Serve the dashboard HTML if it's in the same folder."""
    html = Path(__file__).parent / "digital-wellbeing.html"
    if html.exists():
        return send_from_directory(str(html.parent), "digital-wellbeing.html")
    return "<h2>Place digital-wellbeing.html next to api.py to serve the dashboard here.</h2>"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    print("\n  Wellbeing API running →  http://localhost:5000\n")
    print("  SSE stream  →  http://localhost:5000/api/alerts/stream\n")
    # threaded=True is required — SSE holds a connection open per browser tab,
    # which would block all other requests in single-threaded mode.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)

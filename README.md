# Digital Wellbeing — Setup Guide

## Files

```
wellbeing/
├── db.py           # SQLite schema + all query helpers
├── tracker.py      # Background daemon — polls active window → writes to DB
├── alerts.py       # OS notification engine (80% warn / 100% exceeded)
├── api.py          # Flask REST API — serves JSON to the dashboard
├── seed.py         # One-time demo data generator
├── requirements.txt
└── digital-wellbeing.html   ← copy the frontend file here
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install flask

# Linux
sudo apt install xdotool libnotify-bin

# macOS
pip install pyobjc-framework-Cocoa

# Windows
pip install pywin32 psutil win10toast
```

### 2. Seed demo data (optional)

```bash
python seed.py
```

### 3. Start the API server

```bash
python api.py
# → http://localhost:5000
```

### 4. Start the tracker (separate terminal)

```bash
python tracker.py
```

### 5. Open the dashboard

Place `digital-wellbeing.html` in the same folder and visit:
```
http://localhost:5000
```

---

## Background Mode

Use the supervisor when you want the app to keep running after the editor or
terminal is closed:

```bash
python wellbeing_service.py start
python wellbeing_service.py status
python wellbeing_service.py stop
```

`start` launches both `api.py` and `tracker.py` in the background and restarts
them if either process exits. `stop` is the explicit way to shut them down.
Logs are written under `.wellbeing-runtime/`.

When an enabled app limit reaches 100%, the tracker closes that foreground app.
If you raise the limit, disable the limit, or set the limit to `0`, the app is
allowed again the next time it is opened.

## API Reference

| Method | Endpoint          | Description                        |
|--------|-------------------|------------------------------------|
| GET    | `/api/today`      | Per-app usage for today            |
| GET    | `/api/weekly`     | Per-app-per-day for last 7 days    |
| GET    | `/api/daily-totals` | Total per day (for line chart)   |
| GET    | `/api/limits`     | All app limits                     |
| POST   | `/api/limits`     | Save/update limits (JSON body)     |
| GET    | `/api/alerts`     | Today's fired alerts               |
| GET    | `/api/heatmap`    | Hour×day grid for heatmap          |
| GET    | `/api/stats`      | Summary: total time, streaks, etc. |

### POST /api/limits example

```bash
curl -X POST http://localhost:5000/api/limits \
  -H "Content-Type: application/json" \
  -d '[
    {"app_name": "Chrome",  "limit_mins": 120, "enabled": true},
    {"app_name": "Slack",   "limit_mins": 90,  "enabled": true},
    {"app_name": "VS Code", "limit_mins": 180, "enabled": false}
  ]'
```

---

## SQLite Schema

```sql
-- One row per (app, day) — tracker upserts seconds
CREATE TABLE usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name  TEXT    NOT NULL,
    date      TEXT    NOT NULL,       -- "YYYY-MM-DD"
    seconds   INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,                   -- "YYYY-MM-DDTHH:MM:SS"
    UNIQUE(app_name, date)
);

-- User-defined daily caps
CREATE TABLE limits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name    TEXT    NOT NULL UNIQUE,
    limit_mins  INTEGER NOT NULL DEFAULT 60,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT
);

-- Notification log (prevents duplicate alerts)
CREATE TABLE alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name  TEXT NOT NULL,
    date      TEXT NOT NULL,
    level     TEXT NOT NULL,    -- 'warn' | 'exceeded'
    fired_at  TEXT NOT NULL
);
```

---

## Run as a Background Service

### Linux (systemd)

Create `/etc/systemd/system/wellbeing-tracker.service`:

```ini
[Unit]
Description=Wellbeing Screen Tracker
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 /path/to/wellbeing/tracker.py
Restart=always
Environment=DISPLAY=:0

[Install]
WantedBy=graphical-session.target
```

```bash
systemctl --user enable wellbeing-tracker
systemctl --user start wellbeing-tracker
```

### macOS (launchd)

Create `~/Library/LaunchAgents/com.wellbeing.tracker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>com.wellbeing.tracker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/wellbeing/tracker.py</string>
  </array>
  <key>RunAtLoad</key>   <true/>
  <key>KeepAlive</key>   <true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.wellbeing.tracker.plist
```

### Windows (Task Scheduler)

```powershell
schtasks /create /tn "WellbeingTracker" /tr "python C:\path\tracker.py" /sc onlogon /ru %USERNAME%
```

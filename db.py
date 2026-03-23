"""
db.py — SQLite Schema & Connection Helpers
==========================================
Single source of truth for the database.
All other modules import from here.
"""

import sqlite3
from pathlib import Path
from datetime import date, timedelta

# ── Path ──────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "wellbeing.db"


# ── Connection factory ────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """
    Return a connection with:
      - Row factory  → rows accessible as dicts (row['col'])
      - WAL journal  → safe concurrent reads from Flask + writes from tracker
      - Foreign keys → enforced
    """
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
-- ── usage ──────────────────────────────────────────────────────────────────
-- One row per (app, day). Tracker upserts seconds into this table.
CREATE TABLE IF NOT EXISTS usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name  TEXT    NOT NULL,
    date      TEXT    NOT NULL,        -- ISO-8601  "YYYY-MM-DD"
    seconds   INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,                    -- ISO-8601  "YYYY-MM-DDTHH:MM:SS"
    UNIQUE(app_name, date)
);

-- ── usage_hourly ─────────────────────────────────────────────────────────────
-- One row per (app, date, hour). Tracks real seconds used per clock hour.
CREATE TABLE IF NOT EXISTS usage_hourly (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name  TEXT    NOT NULL,
    date      TEXT    NOT NULL,        -- ISO-8601  "YYYY-MM-DD"
    hour      INTEGER NOT NULL,        -- 0–23
    seconds   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(app_name, date, hour)
);

-- ── limits ──────────────────────────────────────────────────────────────────
-- User-defined daily caps per app.
CREATE TABLE IF NOT EXISTS limits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name     TEXT    NOT NULL UNIQUE,
    limit_mins   INTEGER NOT NULL DEFAULT 60,   -- 0 = no limit
    enabled      INTEGER NOT NULL DEFAULT 1,    -- 1 = active
    updated_at   TEXT                            -- ISO-8601
);

-- ── alerts ──────────────────────────────────────────────────────────────────
-- Every notification fired is logged here (avoids repeat alerts).
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name   TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    level      TEXT    NOT NULL,   -- 'warn' | 'exceeded'
    fired_at   TEXT    NOT NULL    -- ISO-8601
);
"""


def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    with get_connection() as con:
        con.executescript(SCHEMA)
    print(f"[db] Initialised  →  {DB_PATH}")


# ── Query helpers (used by api.py) ────────────────────────────────────────────

def get_today_usage() -> list[dict]:
    """Return all app rows for today, sorted by seconds desc."""
    today = date.today().isoformat()
    with get_connection() as con:
        rows = con.execute("""
            SELECT
                u.app_name,
                u.seconds,
                ROUND(u.seconds / 60.0, 1)  AS minutes,
                u.last_seen,
                l.limit_mins,
                l.enabled
            FROM usage u
            LEFT JOIN limits l USING (app_name)
            WHERE u.date = ?
            ORDER BY u.seconds DESC
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


def get_weekly_usage(days: int = 7) -> list[dict]:
    """Return per-app-per-day totals for the last N days."""
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with get_connection() as con:
        rows = con.execute("""
            SELECT
                app_name,
                date,
                seconds,
                ROUND(seconds / 60.0, 1) AS minutes
            FROM usage
            WHERE date >= ?
            ORDER BY date, seconds DESC
        """, (start,)).fetchall()
    return [dict(r) for r in rows]


def get_daily_totals(days: int = 7) -> list[dict]:
    """Return total seconds per day for the last N days."""
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with get_connection() as con:
        rows = con.execute("""
            SELECT
                date,
                SUM(seconds)                AS total_seconds,
                ROUND(SUM(seconds)/60.0, 1) AS total_minutes
            FROM usage
            WHERE date >= ?
            GROUP BY date
            ORDER BY date
        """, (start,)).fetchall()
    return [dict(r) for r in rows]


def get_hourly_usage(target_date: str = None) -> list[dict]:
    """
    Return total seconds per hour for a given date (default: today).
    Returns 24 rows (one per hour 0–23), filling missing hours with 0.
    """
    if target_date is None:
        target_date = date.today().isoformat()
    with get_connection() as con:
        rows = con.execute("""
            SELECT hour, SUM(seconds) AS seconds
            FROM usage_hourly
            WHERE date = ?
            GROUP BY hour
            ORDER BY hour
        """, (target_date,)).fetchall()
    by_hour = {r["hour"]: r["seconds"] for r in rows}
    return [{"hour": h, "seconds": by_hour.get(h, 0), "minutes": round(by_hour.get(h, 0) / 60, 1)} for h in range(24)]


def get_all_limits() -> list[dict]:
    with get_connection() as con:
        rows = con.execute("""
            SELECT app_name, limit_mins, enabled, updated_at
            FROM limits
            ORDER BY app_name
        """).fetchall()
    return [dict(r) for r in rows]


def upsert_limit(app_name: str, limit_mins: int, enabled: bool):
    """Insert or update a limit row."""
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as con:
        con.execute("""
            INSERT INTO limits (app_name, limit_mins, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(app_name)
            DO UPDATE SET
                limit_mins = excluded.limit_mins,
                enabled    = excluded.enabled,
                updated_at = excluded.updated_at
        """, (app_name, limit_mins, int(enabled), now))


def get_today_alerts() -> list[dict]:
    today = date.today().isoformat()
    with get_connection() as con:
        rows = con.execute("""
            SELECT app_name, level, fired_at
            FROM alerts
            WHERE date = ?
            ORDER BY fired_at DESC
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


def already_alerted(app_name: str, level: str) -> bool:
    """Check if we already fired this level alert today (avoid spam)."""
    today = date.today().isoformat()
    with get_connection() as con:
        row = con.execute("""
            SELECT 1 FROM alerts
            WHERE app_name=? AND date=? AND level=?
            LIMIT 1
        """, (app_name, today, level)).fetchone()
    return row is not None


def log_alert(app_name: str, level: str):
    from datetime import datetime
    today = date.today().isoformat()
    now   = datetime.now().isoformat(timespec="seconds")
    with get_connection() as con:
        con.execute("""
            INSERT OR IGNORE INTO alerts (app_name, date, level, fired_at)
            VALUES (?, ?, ?, ?)
        """, (app_name, today, level, now))

"""
tracker.py — Background App Usage Tracker
==========================================
Polls the active window every 2 seconds, accumulates seconds in memory,
and flushes to SQLite every 10 seconds.

Platform support:
  Linux   →  xdotool         (sudo apt install xdotool)
  macOS   →  pyobjc-Cocoa    (pip install pyobjc-framework-Cocoa)
  Windows →  pywin32 + psutil (pip install pywin32 psutil)

Run:
    python tracker.py

Stop with Ctrl-C (flushes data before exit).
"""

import sys
import time
import signal
import logging
import platform
import subprocess
from datetime import date, datetime
from pathlib import Path

from db import init_db, get_connection, DB_PATH
from alerts import check_and_fire_alerts

# ── Config ────────────────────────────────────────────────────────────────────

POLL_INTERVAL  = 2    # seconds — how often we sample the active window
FLUSH_INTERVAL = 10   # seconds — how often we write to SQLite

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")

OS = platform.system()

# ── Active window detection (platform-specific) ───────────────────────────────

def get_active_app() -> str:
    """
    Return the name of the currently focused application.
    Falls back to 'Unknown' on any error.
    """
    try:
        if OS == "Linux":
            return _linux_active()
        elif OS == "Darwin":
            return _macos_active()
        elif OS == "Windows":
            return _windows_active()
        else:
            return "Unknown"
    except Exception as exc:
        log.debug("Window probe error: %s", exc)
        return "Unknown"


def _linux_active() -> str:
    """Use xdotool to read the active window name."""
    win_id = subprocess.check_output(
        ["xdotool", "getactivewindow"],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()

    title = subprocess.check_output(
        ["xdotool", "getwindowname", win_id],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()

    # Strip document names like "file.py — Visual Studio Code"
    for sep in (" – ", " - ", " | ", " — "):
        if sep in title:
            return title.split(sep)[-1].strip()

    return title.split()[0] if title else "Unknown"


def _macos_active() -> str:
    """Use AppKit (pyobjc) to read the frontmost application."""
    from AppKit import NSWorkspace  # type: ignore
    ws  = NSWorkspace.sharedWorkspace()
    app = ws.frontmostApplication()
    return app.localizedName() if app else "Unknown"


def _windows_active() -> str:
    """Use win32gui + psutil to get the active process name."""
    import win32gui       # type: ignore
    import win32process   # type: ignore
    import psutil         # type: ignore

    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        name = psutil.Process(pid).name()
        return name.replace(".exe", "").replace(".EXE", "")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # Fall back to window title first word
        title = win32gui.GetWindowText(hwnd)
        return title.split()[0] if title else "Unknown"


# ── In-memory buffer ──────────────────────────────────────────────────────────

class SessionBuffer:
    """
    Accumulates active seconds per (app, hour) in RAM between DB flushes.

    Structure:
        { ("Chrome", 14): 24, ("VS Code", 14): 18, ... }
        key = (app_name, hour_0_to_23)
    """

    def __init__(self):
        self._data: dict[tuple[str, int], int] = {}
        self._last_flush: float = time.monotonic()

    def add(self, app: str, secs: int = POLL_INTERVAL):
        hour = datetime.now().hour          # capture the current clock hour
        key  = (app, hour)
        self._data[key] = self._data.get(key, 0) + secs

    def due(self) -> bool:
        return (time.monotonic() - self._last_flush) >= FLUSH_INTERVAL

    def flush(self):
        if not self._data:
            self._last_flush = time.monotonic()
            return

        today = date.today().isoformat()
        now   = datetime.now().isoformat(timespec="seconds")

        # Aggregate daily totals from the hourly buffer for the usage table
        daily: dict[str, int] = {}
        for (app, _hour), secs in self._data.items():
            daily[app] = daily.get(app, 0) + secs

        with get_connection() as con:
            # 1. Upsert daily totals (existing behaviour)
            con.executemany("""
                INSERT INTO usage (app_name, date, seconds, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_name, date) DO UPDATE SET
                    seconds   = seconds + excluded.seconds,
                    last_seen = excluded.last_seen
            """, [(app, today, secs, now) for app, secs in daily.items()])

            # 2. Upsert per-hour breakdown into usage_hourly (NEW)
            con.executemany("""
                INSERT INTO usage_hourly (app_name, date, hour, seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_name, date, hour) DO UPDATE SET
                    seconds = seconds + excluded.seconds
            """, [(app, today, hour, secs) for (app, hour), secs in self._data.items()])

        log.debug(
            "DB flush — %d app(s)  [%s]",
            len(daily),
            ", ".join(f"{a}:{s}s" for a, s in daily.items()),
        )

        self._data.clear()
        self._last_flush = time.monotonic()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    init_db()
    log.info("Tracker started  |  OS=%s  |  DB=%s", OS, DB_PATH)
    log.info("Poll every %ds, flush every %ds. Press Ctrl-C to stop.", POLL_INTERVAL, FLUSH_INTERVAL)

    buf     = SessionBuffer()
    running = True

    def _stop(sig, _frame):
        nonlocal running
        log.info("Signal %s — shutting down…", sig)
        running = False

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    last_app = ""
    while running:
        app = get_active_app()

        # Log app switches at INFO level so the console is readable
        if app != last_app:
            log.info("Active app: %s", app)
            last_app = app

        buf.add(app)

        if buf.due():
            buf.flush()
            check_and_fire_alerts()

        time.sleep(POLL_INTERVAL)

    # Always flush remaining data on clean exit
    log.info("Final flush before exit…")
    buf.flush()
    log.info("Tracker stopped.")


if __name__ == "__main__":
    run()

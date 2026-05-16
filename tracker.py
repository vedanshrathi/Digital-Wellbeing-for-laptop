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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from db import init_db, get_connection, get_limit_status, DB_PATH
from alerts import check_and_fire_alerts

# ── Config ────────────────────────────────────────────────────────────────────

POLL_INTERVAL  = 2    # seconds — how often we sample the active window
FLUSH_INTERVAL = 10   # seconds — how often we write to SQLite
ENFORCE_LIMITS = True
ENFORCEMENT_NOTICE_INTERVAL = 30
ENFORCEMENT_GRACE_SECONDS = 3

ENFORCEMENT_EXEMPT_APPS = {
    "unknown",
    "system",
    "idle",
    "explorer",
    "finder",
    "python",
    "pythonw",
    "powershell",
    "pwsh",
    "cmd",
    "conhost",
    "windowsterminal",
    "terminal",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")

OS = platform.system()
_last_enforcement_notice: dict[str, float] = {}


@dataclass(frozen=True)
class ActiveApp:
    name: str
    pid: int | None = None

# ── Active window detection (platform-specific) ───────────────────────────────

def get_active_app() -> str:
    """Return the name of the currently focused application."""
    return get_active_app_info().name


def get_active_app_info() -> ActiveApp:
    """
    Return the name and process id of the currently focused application.
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
            return ActiveApp("Unknown")
    except Exception as exc:
        log.debug("Window probe error: %s", exc)
        return ActiveApp("Unknown")


def _linux_active() -> ActiveApp:
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

    pid = None
    try:
        raw_pid = subprocess.check_output(
            ["xdotool", "getwindowpid", win_id],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        pid = int(raw_pid) if raw_pid else None
    except Exception:
        pid = None

    # Strip document names like "file.py — Visual Studio Code"
    for sep in (" – ", " - ", " | ", " — "):
        if sep in title:
            return ActiveApp(title.split(sep)[-1].strip(), pid)

    return ActiveApp(title.split()[0] if title else "Unknown", pid)


def _macos_active() -> ActiveApp:
    """Use AppKit (pyobjc) to read the frontmost application."""
    from AppKit import NSWorkspace  # type: ignore
    ws  = NSWorkspace.sharedWorkspace()
    app = ws.frontmostApplication()
    if not app:
        return ActiveApp("Unknown")
    return ActiveApp(app.localizedName(), app.processIdentifier())


def _windows_active() -> ActiveApp:
    """Use win32gui + psutil to get the active process name."""
    import win32gui       # type: ignore
    import win32process   # type: ignore
    import psutil         # type: ignore

    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        name = psutil.Process(pid).name()
        return ActiveApp(name.replace(".exe", "").replace(".EXE", ""), pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # Fall back to window title first word
        title = win32gui.GetWindowText(hwnd)
        return ActiveApp(title.split()[0] if title else "Unknown", pid)


# ── In-memory buffer ──────────────────────────────────────────────────────────

def _normalize_app_name(app_name: str) -> str:
    return app_name.strip().lower().replace(".exe", "")


def _limit_block(app_name: str, pending_seconds: int = 0) -> dict | None:
    if not ENFORCE_LIMITS or _normalize_app_name(app_name) in ENFORCEMENT_EXEMPT_APPS:
        return None

    status = get_limit_status(app_name)
    limit_mins = status.get("limit_mins")
    enabled = bool(status.get("enabled"))
    if not enabled or not limit_mins or limit_mins <= 0:
        return None

    used_seconds = int(status.get("seconds") or 0) + pending_seconds
    limit_seconds = int(limit_mins) * 60
    if used_seconds < limit_seconds:
        return None

    return {
        "used_seconds": used_seconds,
        "limit_seconds": limit_seconds,
        "limit_mins": int(limit_mins),
    }


def _enforce_limit(active: ActiveApp, block: dict):
    now = time.monotonic()
    last_notice = _last_enforcement_notice.get(active.name, 0)
    if now - last_notice >= ENFORCEMENT_NOTICE_INTERVAL:
        log.warning(
            "Limit active: closing %s (used=%ds, limit=%ds)",
            active.name,
            block["used_seconds"],
            block["limit_seconds"],
        )
        _last_enforcement_notice[active.name] = now

    if active.pid is None:
        return

    try:
        if OS == "Windows":
            _close_windows_app(active.pid, active.name)
        elif OS in {"Linux", "Darwin"}:
            _terminate_pid(active.pid)
    except Exception as exc:
        log.warning("Could not close %s after limit reached: %s", active.name, exc)


def _terminate_pid(pid: int):
    import os

    if pid == os.getpid():
        return
    os.kill(pid, signal.SIGTERM)


def _close_windows_app(pid: int, app_name: str):
    import os
    import win32con       # type: ignore
    import win32gui       # type: ignore
    import win32process   # type: ignore
    import psutil         # type: ignore

    if pid == os.getpid():
        return

    hwnd = win32gui.GetForegroundWindow()
    _, foreground_pid = win32process.GetWindowThreadProcessId(hwnd)
    if foreground_pid == pid:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(ENFORCEMENT_GRACE_SECONDS)

    app_name_lower = app_name.lower().replace(".exe", "")
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['pid'] == os.getpid():
                continue
            proc_name = proc.info['name']
            if proc_name and proc_name.lower().replace(".exe", "") == app_name_lower:
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


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

    def pending_seconds_for(self, app: str) -> int:
        return sum(secs for (name, _hour), secs in self._data.items() if name == app)

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
        active = get_active_app_info()
        app = active.name

        # Log app switches at INFO level so the console is readable
        if app != last_app:
            log.info("Active app: %s", app)
            last_app = app

        block = _limit_block(app)
        if block:
            _enforce_limit(active, block)
            time.sleep(POLL_INTERVAL)
            continue

        buf.add(app)
        block = _limit_block(app, buf.pending_seconds_for(app))
        if block:
            buf.flush()
            check_and_fire_alerts()
            _enforce_limit(active, block)
            time.sleep(POLL_INTERVAL)
            continue

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

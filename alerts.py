"""
alerts.py — OS + Browser Notification Engine
=============================================
Fires system notifications when an app hits 80% or 100% of its daily limit,
AND pushes the alert to the browser dashboard via api.push_alert().

Each (app, day, level) pair fires at most once — no spam.
"""

import platform
import subprocess
import logging
from db import get_today_usage, get_all_limits, already_alerted, log_alert

log = logging.getLogger("alerts")
OS  = platform.system()


# ── OS notification dispatch ──────────────────────────────────────────────────

def _os_notify(title: str, body: str):
    """Send a native OS desktop notification."""
    try:
        if OS == "Linux":
            subprocess.run(
                ["notify-send", "--urgency=normal", "--icon=dialog-warning", title, body],
                check=False
            )
        elif OS == "Darwin":
            script = f'display notification "{body}" with title "{title}" sound name "Ping"'
            subprocess.run(["osascript", "-e", script], check=False)
        elif OS == "Windows":
            from win10toast import ToastNotifier  # type: ignore
            ToastNotifier().show_toast(title, body, duration=6, threaded=True)
        else:
            log.warning("OS notifications not supported on %s", OS)
    except FileNotFoundError as e:
        log.debug("Notification backend missing: %s", e)
    except Exception as e:
        log.warning("OS notification failed: %s", e)


# ── Browser push via SSE ──────────────────────────────────────────────────────

def _browser_notify(app_name: str, level: str, title: str, body: str):
    """Push alert to any open dashboard tabs via the SSE queue in api.py."""
    try:
        import api as _api
        _api.push_alert(app_name, level, title, body)
    except Exception as e:
        log.debug("Browser push failed: %s", e)


# ── Combined notify ───────────────────────────────────────────────────────────

def notify(app_name: str, level: str, title: str, body: str):
    """Fire both OS and browser notifications."""
    _os_notify(title, body)
    _browser_notify(app_name, level, title, body)


# ── Main alert check ──────────────────────────────────────────────────────────

def check_and_fire_alerts():
    """
    Compare today's usage against each app's limit.
    Fire a 'warn' alert at 80% and an 'exceeded' alert at 100%.
    """
    usage  = {row["app_name"]: row for row in get_today_usage()}
    limits = get_all_limits()

    for lim in limits:
        app_name   = lim["app_name"]
        limit_mins = lim["limit_mins"]
        enabled    = bool(lim["enabled"])

        if not enabled or limit_mins <= 0:
            continue

        row = usage.get(app_name)
        if not row:
            continue

        used_mins = row["seconds"] / 60
        pct       = used_mins / limit_mins * 100
        limit_str = f"{limit_mins} min"
        used_str  = f"{int(used_mins)} min"

        # ── 100% exceeded ────────────────────────────────────────────────────
        if pct >= 100 and not already_alerted(app_name, "exceeded"):
            title = f"⏱ {app_name} — Time limit reached"
            body  = f"You've used {used_str} of your {limit_str} daily limit."
            notify(app_name, "exceeded", title, body)
            log_alert(app_name, "exceeded")
            log.info("ALERT exceeded: %s  used=%s  limit=%s", app_name, used_str, limit_str)

        # ── 80% warning ──────────────────────────────────────────────────────
        elif 80 <= pct < 100 and not already_alerted(app_name, "warn"):
            remaining = int(limit_mins - used_mins)
            title = f"⚠ {app_name} — {int(pct)}% of daily limit"
            body  = f"{remaining} min remaining out of {limit_str}."
            notify(app_name, "warn", title, body)
            log_alert(app_name, "warn")
            log.info("ALERT warn: %s  pct=%.0f%%  remaining=%d min", app_name, pct, remaining)

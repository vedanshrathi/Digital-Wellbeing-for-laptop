"""
Background supervisor for the Digital Wellbeing app.

Usage:
    python wellbeing_service.py start
    python wellbeing_service.py status
    python wellbeing_service.py stop

The supervisor detaches from the launching terminal/editor and keeps api.py and
tracker.py running until stop is requested.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".wellbeing-runtime"
STATE_FILE = RUNTIME_DIR / "service-state.json"
STOP_FILE = RUNTIME_DIR / "stop"

SERVICES = {
    "api": BASE_DIR / "api.py",
    "tracker": BASE_DIR / "tracker.py",
}


def _ensure_runtime_dir():
    RUNTIME_DIR.mkdir(exist_ok=True)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict):
    _ensure_runtime_dir()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _terminate_pid(pid: int | None):
    if not pid or not _pid_running(pid):
        return
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        for child in children:
            child.terminate()
        proc.terminate()
        gone, alive = psutil.wait_procs([proc, *children], timeout=8)
        for proc in alive:
            proc.kill()
        return
    except Exception:
        pass

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)


def _creation_flags(detached: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if detached:
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return flags


def _process_kwargs(detached: bool = False) -> dict:
    kwargs = {
        "cwd": str(BASE_DIR),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _creation_flags(detached)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _start_service(name: str, script: Path) -> subprocess.Popen:
    log_path = RUNTIME_DIR / f"{name}.log"
    log = log_path.open("a", encoding="utf-8", buffering=1)
    log.write(f"\n[{_now()}] starting {name}\n")
    return subprocess.Popen(
        [sys.executable, "-u", str(script)],
        stdout=log,
        stderr=subprocess.STDOUT,
        **_process_kwargs(),
    )


def supervise():
    _ensure_runtime_dir()
    STOP_FILE.unlink(missing_ok=True)

    children: dict[str, subprocess.Popen] = {}
    try:
        while not STOP_FILE.exists():
            for name, script in SERVICES.items():
                proc = children.get(name)
                if proc is None or proc.poll() is not None:
                    children[name] = _start_service(name, script)

            _write_state({
                "supervisor_pid": os.getpid(),
                "started_at": _read_state().get("started_at") or _now(),
                "updated_at": _now(),
                "services": {name: proc.pid for name, proc in children.items()},
            })
            time.sleep(5)
    finally:
        for proc in children.values():
            _terminate_pid(proc.pid)
        STOP_FILE.unlink(missing_ok=True)
        STATE_FILE.unlink(missing_ok=True)


def start():
    _ensure_runtime_dir()
    state = _read_state()
    if _pid_running(state.get("supervisor_pid")):
        print(f"Already running. Supervisor PID: {state['supervisor_pid']}")
        return

    STOP_FILE.unlink(missing_ok=True)
    log = (RUNTIME_DIR / "supervisor.log").open("a", encoding="utf-8", buffering=1)
    proc = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve()), "supervise"],
        stdout=log,
        stderr=subprocess.STDOUT,
        **_process_kwargs(detached=True),
    )
    _write_state({
        "supervisor_pid": proc.pid,
        "started_at": _now(),
        "updated_at": _now(),
        "services": {},
    })
    print(f"Started Digital Wellbeing in the background. Supervisor PID: {proc.pid}")


def stop():
    state = _read_state()
    supervisor_pid = state.get("supervisor_pid")
    if not _pid_running(supervisor_pid):
        STATE_FILE.unlink(missing_ok=True)
        print("Digital Wellbeing is not running.")
        return

    _ensure_runtime_dir()
    STOP_FILE.write_text(_now(), encoding="utf-8")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _pid_running(supervisor_pid):
        time.sleep(0.5)

    if _pid_running(supervisor_pid):
        _terminate_pid(supervisor_pid)
    STATE_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    print("Stopped Digital Wellbeing.")


def status():
    state = _read_state()
    supervisor_pid = state.get("supervisor_pid")
    if not _pid_running(supervisor_pid):
        print("Digital Wellbeing is not running.")
        return

    print(f"Digital Wellbeing is running. Supervisor PID: {supervisor_pid}")
    for name, pid in state.get("services", {}).items():
        state_text = "running" if _pid_running(pid) else "restarting"
        print(f"  {name}: {pid} ({state_text})")


def main():
    parser = argparse.ArgumentParser(description="Manage Digital Wellbeing background service.")
    parser.add_argument("command", choices=["start", "stop", "status", "restart", "supervise"])
    args = parser.parse_args()

    if args.command == "start":
        start()
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        status()
    elif args.command == "restart":
        stop()
        start()
    elif args.command == "supervise":
        supervise()


if __name__ == "__main__":
    main()

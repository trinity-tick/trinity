"""
Trinity Windows Service / Watchdog
==================================
Monitors the three Trinity runtime processes and restarts any that die:

* ``trinity-api``    — http://127.0.0.1:8001/health   (python -m trinity.api.server --port 8001)
* ``trinity-mcp``    — TCP :8000 SSE transport        (python -m trinity.mcp.server --mode sse --port 8000)
* ``collector``      — daemon PID file + liveness      (python -m trinity.collector start)

The watchdog runs one probe cycle every 30 seconds and spawns missing
processes with the **system Python** (``sys.executable``), cwd
``C:\\Users\\Administrator\\trinity``. A 60-second restart-protection window
prevents crash-looping: a process restarted less than 60 s ago is not pulled
up again. All activity is appended to
``C:\\Users\\Administrator\\.trinity\\logs\\service.log``.

Execution modes
---------------
1. pywin32 installed, launched by the Service Control Manager
   (``sc create TrinityService binPath= "\\"<python.exe>\\" \\"scripts\\trinity_service.py\\""``):
   the ``__main__`` block calls ``servicemanager.Initialize()`` /
   ``PrepareToHostSingle()`` / ``StartServiceCtrlDispatcher()``.
2. pywin32 installed, manual control:
   ``python scripts\\trinity_service.py install|start|stop|remove``
   (via ``win32serviceutil.HandleCommandLine``).
3. pywin32 NOT installed: pure-Python foreground watchdog fallback with the
   same probe/restart logic plus a daemonization hint (register a scheduled
   task). Also available as ``python scripts\\trinity_service.py --foreground``.
4. ``--check`` runs a single probe cycle and prints status (diagnostics).
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:  # pywin32 — used for the native Windows service
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    HAS_PYWIN32 = True
except ImportError:  # pragma: no cover — depends on host
    HAS_PYWIN32 = False

# ── constants ────────────────────────────────────────────────────────────
TRINITY_HOME = Path(r"C:\Users\Administrator\trinity")
LOG_DIR = Path(r"C:\Users\Administrator\.trinity\logs")
SERVICE_LOG = LOG_DIR / "service.log"

API_HEALTH_URL = "http://127.0.0.1:8001/health"
MCP_HOST = "127.0.0.1"
MCP_PORT = 8000
COLLECTOR_PID_FILE = TRINITY_HOME / "data" / "collector.pid"

POLL_INTERVAL_SECONDS = 30
RESTART_PROTECTION_SECONDS = 60

SERVICE_NAME = "TrinityService"
SERVICE_DISPLAY_NAME = "Trinity Watchdog (api/mcp/collector)"
SERVICE_DESCRIPTION = (
    "Probes trinity-api (:8001/health), trinity-mcp SSE (:8000) and the "
    "collector every 30s and restarts missing processes using the system Python."
)

# process spec: name -> (module args, probe callable)
PROCESSES = {
    "trinity-api": ["trinity.api.server", "--port", "8001"],
    "trinity-mcp": ["trinity.mcp.server", "--mode", "sse", "--port", "8000"],
    "collector": ["trinity.collector", "start"],
}


def _make_logger() -> logging.Logger:
    """Logger writing to the service log file (plus stderr for console modes)."""
    logger = logging.getLogger("trinity.service")
    if logger.handlers:  # already configured
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(SERVICE_LOG, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] cannot open service log {SERVICE_LOG}: {exc}", file=sys.stderr)
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


# ── liveness helpers ─────────────────────────────────────────────────────
def _read_pid(path: Path):
    """Read a positive PID from a pid file, or None."""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _is_process_alive(pid: int) -> bool:
    """Windows-friendly liveness check (ctypes OpenProcess, tasklist fallback)."""
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── watchdog ─────────────────────────────────────────────────────────────
class TrinityWatchdog:
    """Probe + restart logic shared by the native service and the fallback loop."""

    def __init__(self, interval: int = POLL_INTERVAL_SECONDS, log: logging.Logger = None):
        self.interval = interval
        self.log = log or _make_logger()
        self._last_restart: dict = {}  # name -> monotonic timestamp

    # probes ---------------------------------------------------------------
    def probe_api(self) -> bool:
        """HTTP GET /health — any HTTP response (even 401/403) means 'up'."""
        try:
            with urllib.request.urlopen(API_HEALTH_URL, timeout=5) as resp:
                self.log.debug("api health probe: HTTP %s", resp.status)
                return True
        except urllib.error.HTTPError as exc:
            # server responded with an error status — still up
            self.log.debug("api health probe: HTTP %s (server up)", exc.code)
            return True
        except Exception:  # noqa: BLE001 — refused / timeout / DNS => down
            return False

    def probe_mcp(self) -> bool:
        """TCP connect to the SSE port."""
        try:
            with socket.create_connection((MCP_HOST, MCP_PORT), timeout=5):
                return True
        except Exception:  # noqa: BLE001
            return False

    def probe_collector(self) -> bool:
        """Collector is up iff its pid file exists and the process is alive."""
        pid = _read_pid(COLLECTOR_PID_FILE)
        if pid is None:
            return False
        return _is_process_alive(pid)

    # spawning -------------------------------------------------------------
    def spawn(self, name: str, args: list) -> bool:
        """Start a missing process with the system Python, detached from us."""
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = (
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            proc = subprocess.Popen(
                [sys.executable, "-m", *args],
                cwd=str(TRINITY_HOME),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._last_restart[name] = time.monotonic()
            self.log.info("spawned %s (pid=%s): python -m %s", name, proc.pid, " ".join(args))
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.error("failed to spawn %s: %s", name, exc)
            return False

    def ensure_running(self, name: str, args: list, probe) -> bool:
        """Probe one service; restart it when down and not in the protection window."""
        if probe():
            return True
        age = time.monotonic() - self._last_restart.get(name, 0.0)
        if age < RESTART_PROTECTION_SECONDS:
            self.log.warning(
                "%s is down but was restarted %.0fs ago — restart protection active (%.0fs)",
                name, age, RESTART_PROTECTION_SECONDS - age,
            )
            return False
        self.log.warning("%s is DOWN — restarting", name)
        return self.spawn(name, args)

    # cycle -----------------------------------------------------------------
    def run_once(self) -> dict:
        """Run one full probe + restart cycle. Returns {name: up_or_restarted}."""
        status = {}
        for name, args in PROCESSES.items():
            probe = {
                "trinity-api": self.probe_api,
                "trinity-mcp": self.probe_mcp,
                "collector": self.probe_collector,
            }[name]
            status[name] = self.ensure_running(name, args, probe)
        return status

    def run_forever(self, stop_wait) -> None:
        """Loop every ``interval`` seconds until ``stop_wait(seconds)`` returns True.

        ``stop_wait`` must block up to the given seconds and return True when a
        stop was requested (win32 event wait in service mode, threading.Event
        in the fallback loop).
        """
        self.log.info(
            "Trinity watchdog started (interval=%ss, restart protection=%ss)",
            self.interval, RESTART_PROTECTION_SECONDS,
        )
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                self.log.exception("watchdog cycle error: %s", exc)
            try:
                if stop_wait(self.interval):
                    self.log.info("stop requested — exiting watchdog loop")
                    return
            except KeyboardInterrupt:
                self.log.info("interrupted — exiting watchdog loop")
                return


# ── native Windows service (pywin32) ─────────────────────────────────────
if HAS_PYWIN32:

    class TrinityService(win32serviceutil.ServiceFramework):
        """Windows service hosting the Trinity watchdog loop."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._watchdog = TrinityWatchdog(log=_make_logger())

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)

            def _wait(seconds):
                return (
                    win32event.WaitForSingleObject(self._stop_event, int(seconds * 1000))
                    == win32event.WAIT_OBJECT_0
                )

            self._watchdog.run_forever(_wait)
            win32event.SetEvent(self._stop_event)


# ── CLI / fallback ───────────────────────────────────────────────────────
def _daemon_hint() -> str:
    return (
        "\n"
        "[!] pywin32 is not installed — cannot register a native Windows service.\n"
        "    Running the pure-Python watchdog in the foreground (Ctrl+C to stop).\n"
        "    To run it in the background, register a scheduled task (as Administrator):\n"
        f'        schtasks /Create /TN "{SERVICE_NAME}" /TR "\\"{sys.executable}\\" \\"{Path(__file__).resolve()}\\" --foreground" /SC ONSTART /RU SYSTEM /RL HIGHEST /F\n'
        f'    Or install pywin32 and use: pip install pywin32  (then re-run the installer .bat)\n'
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="trinity_service.py",
        description="Trinity Windows service / watchdog (api :8001, mcp SSE :8000, collector).",
    )
    parser.add_argument(
        "--foreground", action="store_true",
        help="Run the watchdog loop in the foreground (scheduled-task / no-pywin32 mode).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Run a single probe cycle and print UP/DOWN status, then exit.",
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL_SECONDS,
        help=f"probe interval in seconds (default {POLL_INTERVAL_SECONDS})",
    )
    opts, _rest = parser.parse_known_args(argv)

    if opts.check:
        wd = TrinityWatchdog(interval=opts.interval)
        status = wd.run_once()
        for name, ok in status.items():
            print(f"{name}: {'UP' if ok else 'DOWN'}")
        return 0

    if opts.foreground or not HAS_PYWIN32:
        print(_daemon_hint() if not HAS_PYWIN32 else
              "[i] Running the watchdog in the foreground (Ctrl+C to stop).")
        stop = threading.Event()
        wd = TrinityWatchdog(interval=opts.interval)
        wd.run_forever(lambda seconds: stop.wait(seconds))
        return 0

    # pywin32 installed: delegate to win32serviceutil (install/start/stop/remove)
    return int(win32serviceutil.HandleCommandLine(TrinityService) or 0)


if __name__ == "__main__":
    if HAS_PYWIN32 and len(sys.argv) <= 1:
        # Launched directly by the Service Control Manager (sc create binPath= ...).
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(TrinityService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        sys.exit(main())

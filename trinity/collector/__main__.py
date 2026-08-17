"""
Trinity Collector CLI
=====================
用法: python -m trinity.collector start|stop|status|restart

将主动采集器作为独立守护进程管理（启动 / 停止 / 状态查看）。
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ── 路径常量 ──────────────────────────────────────────────────────────────
_TRINITY_HOME = Path(__file__).resolve().parent.parent.parent
_PID_FILE = _TRINITY_HOME / "data" / "collector.pid"
_LOG_FILE = _TRINITY_HOME / "data" / "collector.log"
_DAEMON_MODULE = "trinity.collector.daemon"


def _get_pid() -> int | None:
    """从 PID 文件读取守护进程 PID。"""
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        return pid if pid > 0 else None
    except (ValueError, OSError):
        return None


def _is_process_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否存活（Windows 兼容）。

    优先用 ctypes OpenProcess/GetExitCodeProcess 直接查询（不派生子进程、
    不依赖 tasklist 输出格式）；任何异常回退到原 tasklist 方案。
    """
    if sys.platform == "win32":
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
        except Exception:
            pass
        try:
            # 回退：使用 tasklist 检查进程是否存在
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return f"{pid}" in result.stdout and "python" in result.stdout.lower()
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _kill_process(pid: int) -> bool:
    """终止指定 PID 的进程。"""
    if sys.platform == "win32":
        try:
            # 先尝试优雅终止（不强制）
            subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
            time.sleep(1.0)
            if not _is_process_alive(pid):
                return True
            # 如果仍存活，强制终止
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
            time.sleep(1.0)
            return not _is_process_alive(pid)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2.0)
            if not _is_process_alive(pid):
                return True
            os.kill(pid, signal.SIGKILL)
            time.sleep(1.0)
            return not _is_process_alive(pid)
        except OSError:
            return False


def cmd_start(args: argparse.Namespace) -> int:
    """启动守护进程。"""
    # 检查是否已在运行
    existing_pid = _get_pid()
    if existing_pid and _is_process_alive(existing_pid):
        print(f"[WARN] Collector daemon is already running (PID: {existing_pid})")
        print(f"       Use 'python -m trinity.collector stop' to stop it first.")
        return 1

    # 清理残留 PID 文件
    if _PID_FILE.exists():
        _PID_FILE.unlink()

    # 启动子进程
    python_exe = sys.executable
    daemon_script = str(Path(__file__).resolve().parent / "daemon.py")

    if sys.platform == "win32":
        # Windows: 使用 CREATE_NO_WINDOW | DETACHED_PROCESS 脱离父进程
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        proc = subprocess.Popen(
            [python_exe, daemon_script],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    else:
        # Linux/macOS: 使用 start_new_session 脱离终端
        proc = subprocess.Popen(
            [python_exe, daemon_script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    # 等待 PID 文件写入（最多 5 秒）
    for _ in range(50):
        pid = _get_pid()
        if pid and _is_process_alive(pid):
            print(f"[OK] Collector daemon started (PID: {pid})")
            print(f"     PID file: {_PID_FILE}")
            print(f"     Log file: {_LOG_FILE}")
            return 0
        time.sleep(0.1)

    print("[ERROR] Collector daemon failed to start (no PID file after 5s)")
    print(f"        Check log: {_LOG_FILE}")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    """停止守护进程。"""
    pid = _get_pid()
    if pid is None:
        print("[WARN] No PID file found — collector daemon is not running.")
        return 0

    if not _is_process_alive(pid):
        print(f"[WARN] PID file exists but process {pid} is not alive — cleaning up.")
        _PID_FILE.unlink()
        return 0

    print(f"Stopping collector daemon (PID: {pid})...")
    if _kill_process(pid):
        print("[OK] Collector daemon stopped.")
        if _PID_FILE.exists():
            _PID_FILE.unlink()
        return 0
    else:
        print(f"[ERROR] Failed to stop process {pid}")
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    """查看守护进程状态。"""
    pid = _get_pid()

    print("=" * 60)
    print("  Trinity Collector Daemon — Status")
    print("=" * 60)

    if pid is None:
        print("  Status:   STOPPED (no PID file)")
        print(f"  PID file: {_PID_FILE} (not found)")
        return 0

    alive = _is_process_alive(pid)
    status_str = "RUNNING" if alive else "STALE (process not found)"

    print(f"  Status:   {status_str}")
    print(f"  PID:      {pid} ({'alive' if alive else 'dead'})")
    print(f"  PID file: {_PID_FILE}")

    if alive:
        # 显示最近日志（健康检查）
        print("-" * 60)
        print("  Recent log tail:")
        try:
            if _LOG_FILE.exists():
                text = _LOG_FILE.read_text(encoding="utf-8").strip()
                if text:
                    lines = text.splitlines()
                    for line in lines[-8:]:
                        print(f"    {line}")
                else:
                    print("    (log file empty)")
            else:
                print(f"    (log file not found: {_LOG_FILE})")
        except Exception as e:
            print(f"    (failed to read log: {e})")

    print("=" * 60)
    return 0 if alive else 1


def cmd_restart(args: argparse.Namespace) -> int:
    """重启守护进程。"""
    print("Restarting collector daemon...")
    ret = cmd_stop(args)
    time.sleep(1.0)
    ret2 = cmd_start(args)
    return ret2


def cmd_sync(args: argparse.Namespace) -> int:
    """执行 Marvis 对话同步（oneshot 或 daemon 模式）。"""
    import logging as _logging

    from trinity.bridges.auto_syncer import (
        ConversationScanner,
        BidirectionalSyncDaemon,
    )

    scanner = ConversationScanner()

    if args.daemon:
        interval = args.interval or 60
        print(f"[INFO] Starting bidirectional sync daemon (interval={interval}s)...")
        print(f"       Forward:  Marvis conversations → Trinity memory pool")
        print(f"       Reverse:  Trinity insights → data/trinity_insights.json")
        print(f"       State file: {scanner.state_file}")
        print(f"       User dir:   {scanner.user_dir}")
        print(f"       Press Ctrl+C to stop.")
        daemon = BidirectionalSyncDaemon(scanner, interval=interval)
        try:
            daemon.run_forever()
        except KeyboardInterrupt:
            daemon.stop()
            print("\n[OK] Bidirectional daemon stopped.")
        return 0

    # Oneshot mode
    print(f"[INFO] Running one-shot sync...")
    print(f"       State file: {scanner.state_file}")
    print(f"       User dir:   {scanner.user_dir}")

    stats = scanner.scan_and_sync()

    print(f"\n{'='*50}")
    print(f"  Sync Complete")
    print(f"{'='*50}")
    print(f"  Users scanned:   {stats['users_scanned']}")
    print(f"  Convs scanned:   {stats['convs_scanned']}")
    print(f"  Convs skipped:   {stats['convs_skipped']}")
    print(f"  Convs synced:    {stats['convs_synced']}")
    print(f"  Errors:          {stats['errors']}")
    print(f"  Last sync ts:    {stats['new_last_sync_ts'][:19] if stats['new_last_sync_ts'] else '(none)'}")
    print(f"{'='*50}")
    return 0 if stats["errors"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(
        description="Trinity Collector Daemon — manage the active collector process",
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    sub.add_parser("start", help="Start collector daemon (background)")
    sub.add_parser("stop", help="Stop collector daemon")
    sub.add_parser("status", help="Show daemon status and health")
    sub.add_parser("restart", help="Restart collector daemon")

    # sync 子命令
    sync_parser = sub.add_parser("sync", help="Sync Marvis conversations to Trinity")
    sync_parser.add_argument(
        "--daemon", action="store_true",
        help="Run as daemon with periodic polling",
    )
    sync_parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling interval in seconds (daemon mode, default: 60)",
    )

    args = parser.parse_args()

    if args.command == "start":
        return cmd_start(args)
    elif args.command == "stop":
        return cmd_stop(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "restart":
        return cmd_restart(args)
    elif args.command == "sync":
        return cmd_sync(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())

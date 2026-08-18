"""
Trinity Collector Daemon — 独立守护进程核心
============================================
将 CollectorManager 包装为完全独立的守护进程：
  - 脱离父进程生命周期（CREATE_NO_WINDOW + DETACHED_PROCESS）
  - 内置 Watchdog 线程，扫描器异常退出时自动重启
  - PID 文件管理（data/collector.pid）
  - 结构化日志输出到 data/collector.log
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── 项目根路径解析 ────────────────────────────────────────────────────────
_TRINITY_HOME = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _TRINITY_HOME / "data"
_PID_FILE = _DATA_DIR / "collector.pid"
_LOG_FILE = _DATA_DIR / "collector.log"

# 守护进程以脚本方式被拉起（python daemon.py），cwd 不会进入 sys.path，
# 这里显式把项目根注入 sys.path，避免解析到 site-packages 里的旧版 trinity。
if str(_TRINITY_HOME) not in sys.path:
    sys.path.insert(0, str(_TRINITY_HOME))

# ── 守护进程配置 ──────────────────────────────────────────────────────────
WATCHDOG_INTERVAL = 5           # 看门狗检查间隔（秒）
MAX_RESTART_COUNT = 10          # 一小时内最大自动重启次数
RESTART_COOLDOWN_WINDOW = 3600  # 重启计数窗口（秒）
RESTART_BACKOFF_BASE = 1.0      # 重启冷却基础秒数
RESTART_BACKOFF_MAX = 60.0      # 重启冷却上限秒数

# ── Logger 配置 ───────────────────────────────────────────────────────────

def _setup_logging(log_path: Path) -> logging.Logger:
    """配置守护进程日志：同时写入文件和 stderr。"""
    logger = logging.getLogger("trinity.collector.daemon")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [trinity.collector] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件 handler
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # stderr handler（保留控制台可见性，子进程时 stderr 可能被重定向）
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


logger = _setup_logging(_LOG_FILE)


# ── CollectorDaemon ────────────────────────────────────────────────────────

class CollectorDaemon:
    """Trinity 主动采集器守护进程。

    特性：
      - 独立进程运行，脱离父进程生命周期
      - 内置 Watchdog 监控 BackgroundScanner 线程健康
      - 异常退出时自动重启（带指数退避）
      - PID 文件管理，支持优雅停止
    """

    def __init__(self):
        self._manager: object = None          # CollectorManager 实例
        self._manager_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._restart_count = 0
        self._restart_window_start = 0.0
        self._state = "initialized"

    # ── PID 文件管理 ──────────────────────────────────────────────────

    def _write_pid(self):
        """将当前进程 PID 写入 PID 文件。"""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        _PID_FILE.write_text(str(pid))
        logger.info("PID file written: %s (pid=%d)", _PID_FILE, pid)

    def _remove_pid(self):
        """删除 PID 文件。"""
        try:
            if _PID_FILE.exists():
                _PID_FILE.unlink()
                logger.info("PID file removed: %s", _PID_FILE)
        except OSError as e:
            logger.warning("Failed to remove PID file: %s", e)

    # ── 信号处理 ──────────────────────────────────────────────────────

    def _setup_signal_handlers(self):
        """注册信号处理器，实现优雅停机。"""
        signal.signal(signal.SIGINT, self._on_shutdown_signal)
        signal.signal(signal.SIGTERM, self._on_shutdown_signal)
        # Windows 特有信号
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._on_shutdown_signal)

    def _on_shutdown_signal(self, signum, frame):
        """收到终止信号时触发优雅停机。"""
        sig_name = getattr(signal, "Signals", {}).get(signum, f"SIG({signum})")
        logger.info("Received signal %s, initiating graceful shutdown...", sig_name)
        self._shutdown_event.set()

    # ── 采集器生命周期 ────────────────────────────────────────────────

    def _start_collector(self):
        """启动 CollectorManager 及其内部 BackgroundScanner。"""
        from trinity.memory.active_collector import CollectorManager

        with self._manager_lock:
            self._manager = CollectorManager()
            self._manager.start()
            self._state = "running"
            logger.info(
                "CollectorManager started: %d connectors, scanner interval=%.1fs",
                len(self._manager._connectors),
                self._manager._scanner.scan_interval,
            )

    def _stop_collector(self):
        """停止 CollectorManager 并 flush 缓冲区。"""
        with self._manager_lock:
            if self._manager is not None:
                try:
                    self._manager.stop()
                    logger.info("CollectorManager stopped gracefully")
                except Exception as e:
                    logger.error("Error stopping CollectorManager: %s", e)
                finally:
                    self._manager = None
                    self._state = "stopped"

    def _is_collector_healthy(self) -> bool:
        """检查采集器是否健康运行。

        判定标准：
          1. _manager 存在且 _scanner 存在
          2. BackgroundScanner 的 _thread 存活
          3. _stop_event 未被设置
        """
        with self._manager_lock:
            mgr = self._manager
        if mgr is None:
            return False
        scanner = getattr(mgr, "_scanner", None)
        if scanner is None:
            return False

        stop_event = getattr(scanner, "_stop_event", None)
        if stop_event and stop_event.is_set():
            return False

        thread = getattr(scanner, "_thread", None)
        if thread is None:
            return False

        return thread.is_alive()

    def _get_scanner_stats(self) -> dict:
        """获取扫描器统计信息快照。"""
        with self._manager_lock:
            mgr = self._manager
        if mgr is None:
            return {"state": "no_manager"}
        try:
            stats = mgr.statistics()
            return stats
        except Exception:
            return {"state": "error", "error": traceback.format_exc()}

    # ── Watchdog ──────────────────────────────────────────────────────

    def _watchdog_loop(self):
        """看门狗线程主循环：监控采集器健康并自动重启。"""
        logger.info("Watchdog started (interval=%ds, max_restarts=%d/%.0fs)",
                    WATCHDOG_INTERVAL, MAX_RESTART_COUNT, RESTART_COOLDOWN_WINDOW)

        while not self._shutdown_event.is_set():
            try:
                if not self._is_collector_healthy():
                    logger.warning(
                        "Watchdog: collector unhealthy (state=%s), "
                        "attempting restart...",
                        self._state,
                    )
                    self._restart_collector()
            except Exception as e:
                logger.error("Watchdog: check failed: %s", e)

            self._shutdown_event.wait(WATCHDOG_INTERVAL)

        logger.info("Watchdog stopped")

    def _restart_collector(self):
        """尝试重启采集器，包含指数退避和重启上限保护。"""
        now = time.time()

        # 重置计数窗口
        if now - self._restart_window_start > RESTART_COOLDOWN_WINDOW:
            self._restart_count = 0
            self._restart_window_start = now

        self._restart_count += 1

        if self._restart_count > MAX_RESTART_COUNT:
            logger.critical(
                "Watchdog: max restart count (%d) exceeded in %.0fs window — "
                "giving up. Manual intervention required.",
                MAX_RESTART_COUNT, RESTART_COOLDOWN_WINDOW,
            )
            self._state = "failed"
            self._shutdown_event.set()
            return

        # 指数退避
        backoff = min(
            RESTART_BACKOFF_BASE * (2 ** (self._restart_count - 1)),
            RESTART_BACKOFF_MAX,
        )
        logger.info(
            "Watchdog: restart attempt %d/%d, backing off %.1fs...",
            self._restart_count, MAX_RESTART_COUNT, backoff,
        )

        # 先停止旧的（如果存在）
        self._stop_collector()
        time.sleep(backoff)

        # 启动新的
        try:
            self._start_collector()
            logger.info(
                "Watchdog: collector restarted successfully "
                "(attempt %d/%d)",
                self._restart_count, MAX_RESTART_COUNT,
            )
            # 启动成功后重置计数
            self._restart_count = 0
        except Exception as e:
            logger.error(
                "Watchdog: restart attempt %d failed: %s\n%s",
                self._restart_count, e, traceback.format_exc(),
            )

    # ── 心跳循环 ──────────────────────────────────────────────────────

    def _heartbeat_loop(self):
        """心跳主循环：定期输出采集器统计到日志。"""
        heartbeat_interval = 30  # 每 30 秒输出一次心跳
        last_heartbeat = 0.0

        while not self._shutdown_event.is_set():
            elapsed = time.time() - last_heartbeat
            if elapsed >= heartbeat_interval:
                stats = self._get_scanner_stats()
                scanner = stats.get("scanner", {})
                collector = stats.get("collector", {})
                # 2026-08-18: DSH 结构层事件源统计（空转可见化）
                dsh = stats.get("dsh_events") or {}

                logger.info(
                    "Heartbeat: state=%s | events_captured=%d flushed=%d "
                    "scanner_cycles=%d scanner_errors=%d | dsh: seen=%d emitted=%d last_id=%s",
                    self._state,
                    collector.get("events_captured", 0),
                    collector.get("events_flushed", 0),
                    scanner.get("scan_cycles", 0),
                    scanner.get("errors", 0),
                    dsh.get("events_seen", 0),
                    dsh.get("events_emitted", 0),
                    dsh.get("last_id", "-"),
                )
                last_heartbeat = time.time()

            self._shutdown_event.wait(min(5.0, heartbeat_interval))

    # ── 入口 ──────────────────────────────────────────────────────────

    def run(self):
        """启动守护进程（阻塞调用，直到收到 shutdown 信号）。"""
        logger.info("=" * 60)
        logger.info("Trinity Collector Daemon v8.4.0 starting...")
        logger.info("PID: %d | PID file: %s | Log: %s",
                    os.getpid(), _PID_FILE, _LOG_FILE)

        self._write_pid()
        atexit.register(self._remove_pid)

        self._setup_signal_handlers()

        # 启动采集器
        try:
            self._start_collector()
        except Exception as e:
            logger.critical("Failed to start collector: %s\n%s", e, traceback.format_exc())
            self._remove_pid()
            sys.exit(1)

        # 启动看门狗
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="trinity-collector-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

        # 进入心跳主循环（阻塞）
        try:
            self._heartbeat_loop()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received, shutting down...")
        finally:
            self._shutdown_event.set()
            self._stop_collector()

            if self._watchdog_thread and self._watchdog_thread.is_alive():
                self._watchdog_thread.join(timeout=5.0)

            self._remove_pid()
            logger.info("Trinity Collector Daemon stopped. Goodbye.")
            logger.info("=" * 60)

    # ── 静态工具方法 ──────────────────────────────────────────────────

    @staticmethod
    def get_pid() -> Optional[int]:
        """读取 PID 文件获取守护进程 PID。"""
        if not _PID_FILE.exists():
            return None
        try:
            return int(_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            return None

    @staticmethod
    def get_log_tail(lines: int = 10) -> str:
        """读取日志文件末尾若干行。"""
        if not _LOG_FILE.exists():
            return "(log file not found)"
        try:
            text = _LOG_FILE.read_text(encoding="utf-8").strip()
            if not text:
                return "(log file empty)"
            all_lines = text.splitlines()
            return "\n".join(all_lines[-lines:])
        except Exception as e:
            return f"(failed to read log: {e})"


# ── 直接执行入口 ──────────────────────────────────────────────────────────

def main():
    """守护进程入口：由 CLI start 命令通过子进程调用。"""
    daemon = CollectorDaemon()
    daemon.run()


if __name__ == "__main__":
    main()

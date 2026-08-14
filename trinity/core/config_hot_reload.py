# -*- coding: utf-8 -*-
"""
Trinity Core — Configuration Hot Reload (P1-6).

Monitors configuration files for changes and triggers callbacks when
modifications are detected. Supports JSON/YAML configs with atomic
reload and validation.

Usage::

    from trinity.core.config_hot_reload import ConfigWatcher, HotReloadConfig

    config = HotReloadConfig("config/settings.json")
    config.on_change(lambda new_cfg: print("Config updated:", new_cfg))

    watcher = ConfigWatcher()
    watcher.watch(config, interval_sec=2.0)
    watcher.start()
    ...
    watcher.stop()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ── Config File Abstraction ───────────────────────────────────────────────


class ReloadFormatError(Exception):
    """Raised when config file cannot be parsed after reload."""
    pass


class ConfigValidationError(Exception):
    """Raised when reloaded config fails validation."""
    pass


@dataclass
class HotReloadConfig:
    """A configuration file monitored for changes.

    Attributes:
        path: Absolute path to config file.
        content: Current parsed config dict.
        format: File format (json/yaml).
        last_modified: OS mtime at last read.
        callbacks: Registered change listeners.
        validators: Pre-reload validation functions.
    """

    path: str
    content: Dict[str, Any] = field(default_factory=dict)
    format: str = "json"
    last_modified: float = 0.0
    callbacks: List[Callable[[Dict[str, Any]], None]] = field(default_factory=list)
    validators: List[Callable[[Dict[str, Any]], bool]] = field(default_factory=list)
    _error_count: int = 0
    _reload_count: int = 0

    # ── File I/O ────────────────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        """Load config from disk.

        Returns:
            Parsed config dict. Empty dict if file missing.
        """
        if not os.path.exists(self.path):
            logger.warning("Config file not found: %s", self.path)
            return {}

        try:
            self.last_modified = os.path.getmtime(self.path)
        except OSError:
            self.last_modified = time.time()

        with open(self.path, "r", encoding="utf-8") as f:
            raw = f.read()

        if self.format == "json":
            self.content = json.loads(raw)
        elif self.format == "yaml":
            self.content = self._parse_yaml(raw)
        else:
            raise ValueError(f"Unsupported format: {self.format}")

        return self.content

    def _parse_yaml(self, raw: str) -> Dict[str, Any]:
        """Parse YAML with fallback to JSON."""
        try:
            import yaml
            return yaml.safe_load(raw) or {}
        except ImportError:
            logger.warning("PyYAML not installed, attempting JSON fallback")
            return json.loads(raw)

    def save(self) -> None:
        """Persist current content to disk."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if self.format == "json":
            raw = json.dumps(self.content, indent=2, ensure_ascii=False)
        elif self.format == "yaml":
            try:
                import yaml
                raw = yaml.safe_dump(self.content, allow_unicode=True)
            except ImportError:
                raw = json.dumps(self.content, indent=2)
        else:
            raw = json.dumps(self.content)

        with open(self.path, "w", encoding="utf-8") as f:
            f.write(raw)
        self.last_modified = os.path.getmtime(self.path)

    # ── Change Detection ────────────────────────────────────────────

    def has_changed(self) -> bool:
        """Check if file has been modified since last load.

        Returns:
            True if mtime differs from last_modified.
        """
        if not os.path.exists(self.path):
            return False
        try:
            current_mtime = os.path.getmtime(self.path)
            return abs(current_mtime - self.last_modified) > 0.001
        except OSError:
            return False

    def reload(self) -> bool:
        """Reload config from disk if changed.

        Validates new config before accepting. Runs callbacks on success.

        Returns:
            True if config was reloaded.
        """
        if not self.has_changed():
            return False

        try:
            new_content = self._read_and_parse()
        except Exception as e:
            self._error_count += 1
            logger.error("Config reload parse error [%s]: %s", self.path, e)
            raise ReloadFormatError(f"Parse error for {self.path}: {e}")

        # Validation
        for validator in self.validators:
            try:
                if not validator(new_content):
                    raise ConfigValidationError(f"Validator rejected config {self.path}")
            except Exception as e:
                self._error_count += 1
                logger.error("Config validation failed [%s]: %s", self.path, e)
                raise

        # Accept new config
        old = dict(self.content)
        self.content = new_content
        self.last_modified = os.path.getmtime(self.path)
        self._reload_count += 1

        # Notify callbacks
        for cb in self.callbacks:
            try:
                cb(new_content)
            except Exception as e:
                logger.error("Config callback error [%s]: %s", self.path, e)

        logger.info("Config reloaded [%s] (reload #%d)", self.path, self._reload_count)
        return True

    def _read_and_parse(self) -> Dict[str, Any]:
        """Read file and parse without updating state."""
        with open(self.path, "r", encoding="utf-8") as f:
            raw = f.read()

        if self.format == "json":
            return json.loads(raw)
        elif self.format == "yaml":
            return self._parse_yaml(raw)
        raise ValueError(f"Unsupported format: {self.format}")

    # ── Callback Management ─────────────────────────────────────────

    def on_change(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback for config changes.

        Args:
            callback: Function receiving the new config dict.
        """
        self.callbacks.append(callback)

    def add_validator(self, validator: Callable[[Dict[str, Any]], bool]) -> None:
        """Add a pre-reload validator.

        Args:
            validator: Function returning True if config is valid.
        """
        self.validators.append(validator)

    # ── Get/Set ─────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dot-notation key."""
        keys = key.split(".")
        node = self.content
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
                if node is None:
                    return default
            else:
                return default
        return node

    def set(self, key: str, value: Any) -> None:
        """Set a config value by dot-notation key."""
        keys = key.split(".")
        node = self.content
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value

    # ── Statistics ──────────────────────────────────────────────────

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "reload_count": self._reload_count,
            "error_count": self._error_count,
            "last_modified": self.last_modified,
            "callbacks": len(self.callbacks),
            "validators": len(self.validators),
        }


# ── Config Watcher ────────────────────────────────────────────────────────


class ConfigWatcher:
    """Background thread that polls config files for changes.

    Watches one or more HotReloadConfig instances and triggers reload
    when file modification is detected.

    Usage::

        watcher = ConfigWatcher()
        watcher.watch(config1, interval_sec=2.0)
        watcher.watch(config2, interval_sec=5.0)  # Different polling rate
        watcher.start()
    """

    def __init__(self):
        self._configs: Dict[str, HotReloadConfig] = {}   # path -> config
        self._intervals: Dict[str, float] = {}            # path -> poll interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._watch_count: int = 0
        self._reload_count: int = 0
        self._error_count: int = 0

    def watch(
        self,
        config: HotReloadConfig,
        interval_sec: float = 2.0,
        auto_load: bool = True,
    ) -> None:
        """Register a config for watching.

        Args:
            config: HotReloadConfig instance.
            interval_sec: Polling interval in seconds.
            auto_load: Load config from disk immediately.
        """
        with self._lock:
            if auto_load and not config.content:
                try:
                    config.load()
                except Exception as e:
                    logger.warning("Initial load failed for %s: %s", config.path, e)

            self._configs[config.path] = config
            self._intervals[config.path] = interval_sec
            self._watch_count += 1
            logger.info("Watching config: %s (interval=%.1fs)", config.path, interval_sec)

    def unwatch(self, path: str) -> bool:
        """Stop watching a config.

        Args:
            path: Config file path.

        Returns:
            True if config was being watched.
        """
        with self._lock:
            removed = self._configs.pop(path, None) is not None
            self._intervals.pop(path, None)
            if removed:
                self._watch_count -= 1
            return removed

    def start(self, daemon: bool = True) -> None:
        """Start the background watcher thread.

        Args:
            daemon: Run as daemon thread.
        """
        if self._thread and self._thread.is_alive():
            logger.warning("Watcher already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watcher_loop,
            daemon=daemon,
            name="config-watcher",
        )
        self._thread.start()
        logger.info("ConfigWatcher started (%d configs)", self._watch_count)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the watcher thread gracefully."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("ConfigWatcher stopped")

    def _watcher_loop(self) -> None:
        """Main watcher loop — polls each config at its interval."""
        last_check: Dict[str, float] = {}

        while not self._stop_event.is_set():
            with self._lock:
                paths = list(self._configs.keys())

            now = time.time()
            for path in paths:
                with self._lock:
                    config = self._configs.get(path)
                    if config is None:
                        continue
                    interval = self._intervals.get(path, 2.0)

                last = last_check.get(path, 0.0)
                if now - last < interval:
                    continue

                last_check[path] = now

                try:
                    if config.has_changed():
                        config.reload()
                        self._reload_count += 1
                except Exception as e:
                    self._error_count += 1
                    logger.error("Watcher reload error [%s]: %s", path, e)

            # Sleep with early wake
            min_interval = min(self._intervals.values()) if self._intervals else 2.0
            self._stop_event.wait(timeout=min(min_interval, 1.0))

    def reload_all(self) -> Dict[str, bool]:
        """Force-reload all watched configs.

        Returns:
            Dict mapping path → success.
        """
        results = {}
        with self._lock:
            for path, config in self._configs.items():
                try:
                    ok = config.reload()
                    results[path] = ok
                except Exception as e:
                    results[path] = False
                    logger.error("Force reload failed [%s]: %s", path, e)
        return results

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            config_stats = {p: c.stats for p, c in self._configs.items()}
        return {
            "watched_configs": self._watch_count,
            "reload_count": self._reload_count,
            "error_count": self._error_count,
            "running": self._thread is not None and self._thread.is_alive(),
            "configs": config_stats,
        }


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module self-test."""
    import tempfile
    results: Dict[str, Any] = {"module": "trinity.core.config_hot_reload", "tests": {}}

    # Test 1: HotReloadConfig load/save JSON
    try:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)

        cfg = HotReloadConfig(path=path, format="json")
        cfg.content = {"server": {"host": "0.0.0.0", "port": 8000}, "debug": True}
        cfg.save()
        assert os.path.exists(path)

        cfg2 = HotReloadConfig(path=path, format="json")
        cfg2.load()
        assert cfg2.content["server"]["port"] == 8000
        os.remove(path)
        results["tests"]["config_load_save_json"] = "PASS"
    except Exception as e:
        results["tests"]["config_load_save_json"] = f"FAIL: {e}"
        try:
            os.remove(path)
        except Exception:
            pass

    # Test 2: Dot-notation get/set
    try:
        cfg = HotReloadConfig(path="/tmp/test.json", format="json")
        cfg.content = {"a": {"b": {"c": 42}}}
        assert cfg.get("a.b.c") == 42
        cfg.set("a.b.d", 99)
        assert cfg.get("a.b.d") == 99
        assert cfg.get("x.y.z", "default") == "default"
        results["tests"]["dot_notation"] = "PASS"
    except Exception as e:
        results["tests"]["dot_notation"] = f"FAIL: {e}"

    # Test 3: Change detection
    try:
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        os.write(fd, json.dumps({"version": 1}).encode())
        os.close(fd)

        cfg = HotReloadConfig(path=path, format="json")
        cfg.load()
        assert not cfg.has_changed()

        time.sleep(0.05)
        with open(path, "w") as f:
            json.dump({"version": 2}, f)

        assert cfg.has_changed()
        cfg.reload()
        assert cfg.content["version"] == 2
        os.remove(path)
        results["tests"]["change_detection"] = "PASS"
    except Exception as e:
        results["tests"]["change_detection"] = f"FAIL: {e}"
        try:
            os.remove(path)
        except Exception:
            pass

    # Test 4: Validator
    try:
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        os.write(fd, json.dumps({"version": 1}).encode())
        os.close(fd)

        cfg = HotReloadConfig(path=path, format="json")
        cfg.load()
        cfg.add_validator(lambda c: c.get("version", 0) > 0)

        with open(path, "w") as f:
            json.dump({"version": -1}, f)

        try:
            cfg.reload()
            results["tests"]["validator"] = "FAIL: should have rejected"
        except ConfigValidationError:
            results["tests"]["validator"] = "PASS"
        os.remove(path)
    except Exception as e:
        results["tests"]["validator"] = f"FAIL: {e}"
        try:
            os.remove(path)
        except Exception:
            pass

    # Test 5: Callback notification
    try:
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        os.write(fd, json.dumps({"version": 1}).encode())
        os.close(fd)

        notified = []
        cfg = HotReloadConfig(path=path, format="json")
        cfg.load()
        cfg.on_change(lambda c: notified.append(c["version"]))

        with open(path, "w") as f:
            json.dump({"version": 3}, f)

        cfg.reload()
        assert len(notified) == 1
        assert notified[0] == 3
        os.remove(path)
        results["tests"]["callback_notification"] = "PASS"
    except Exception as e:
        results["tests"]["callback_notification"] = f"FAIL: {e}"
        try:
            os.remove(path)
        except Exception:
            pass

    # Test 6: ConfigWatcher watch/start/stop
    try:
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        os.write(fd, json.dumps({"version": 1}).encode())
        os.close(fd)

        watcher = ConfigWatcher()
        cfg = HotReloadConfig(path=path, format="json")
        watcher.watch(cfg, interval_sec=0.1)
        assert watcher.stats["watched_configs"] == 1

        watcher.start()
        time.sleep(0.3)
        watcher.stop()
        assert watcher.stats["running"] == False
        os.remove(path)
        results["tests"]["config_watcher"] = "PASS"
    except Exception as e:
        results["tests"]["config_watcher"] = f"FAIL: {e}"
        try:
            os.remove(path)
        except Exception:
            pass

    # Test 7: Unwatch
    try:
        watcher2 = ConfigWatcher()
        cfg = HotReloadConfig(path="/tmp/a.json", format="json")
        watcher2.watch(cfg, auto_load=False)
        assert watcher2.unwatch("/tmp/a.json")
        assert watcher2.stats["watched_configs"] == 0
        results["tests"]["unwatch"] = "PASS"
    except Exception as e:
        results["tests"]["unwatch"] = f"FAIL: {e}"

    # Test 8: Stats
    try:
        s = cfg.stats
        assert "reload_count" in s
        results["tests"]["config_stats"] = "PASS"
    except Exception as e:
        results["tests"]["config_stats"] = f"FAIL: {e}"

    passed = sum(1 for v in results["tests"].values() if "PASS" in str(v))
    total = len(results["tests"])
    results["summary"] = f"{passed}/{total} PASS"
    return results


if __name__ == "__main__":
    import sys
    result = self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if all("PASS" in str(v) for v in result["tests"].values()) else 1)

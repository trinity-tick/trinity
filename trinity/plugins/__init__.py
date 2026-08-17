"""
Trinity Plugin Registry
=======================
Safe plugin discovery and loading.

Each plugin is a Python module living in the plugin directory:

* ``TRINITY_PLUGIN_DIR`` environment variable — discovery directory
  (default: ``C:\\Users\\Administrator\\trinity\\plugins``).
* A valid plugin module **must** expose ``plugin_meta()`` returning a dict::

      def plugin_meta():
          return {"name": "my_plugin", "version": "1.0.0", "description": "..."}

* Optional hooks: ``install(engine=None)`` and ``uninstall()``.

The registry never crashes on a broken plugin: import errors are caught,
logged via ``logging.getLogger("trinity.plugins")`` and recorded in
:attr:`PluginRegistry.failures` so the rest of the system keeps working.

Usage::

    from trinity.plugins import PluginRegistry

    reg = PluginRegistry()                 # or PluginRegistry(plugin_dir=...)
    reg.load_all()
    for meta in reg.list():
        print(meta["name"], meta["version"])
    reg.install_all(engine)                # calls install(engine) on each plugin
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.plugins")

# Default discovery directory: <repo root>/plugins
DEFAULT_PLUGIN_DIR = str(Path(__file__).resolve().parent.parent.parent / "plugins")


class PluginError(RuntimeError):
    """Raised when a plugin module is structurally invalid (e.g. no plugin_meta)."""


class PluginRegistry:
    """Discovers, loads and manages Trinity plugin modules (safe loading)."""

    def __init__(self, plugin_dir: Optional[str] = None) -> None:
        self.plugin_dir = Path(
            plugin_dir or os.environ.get("TRINITY_PLUGIN_DIR") or DEFAULT_PLUGIN_DIR
        )
        self._modules: Dict[str, ModuleType] = {}
        self._failures: Dict[str, str] = {}

    # ── discovery ────────────────────────────────────────────────────────
    def discover(self) -> List[Path]:
        """Return plugin module paths (``*.py``) in the plugin directory."""
        if not self.plugin_dir.is_dir():
            logger.debug("plugin dir %s does not exist — nothing to discover", self.plugin_dir)
            return []
        paths = [
            p
            for p in self.plugin_dir.glob("*.py")
            if p.name != "__init__.py" and not p.name.startswith("_")
        ]
        return sorted(paths)

    # ── loading ──────────────────────────────────────────────────────────
    def _load_module(self, path: Path) -> Optional[ModuleType]:
        """Load a single plugin module. Returns the module or None on failure."""
        stem = path.stem
        module_name = f"trinity_plugin_{stem}"
        mod: Optional[ModuleType] = None
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot create import spec for {path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            if not callable(getattr(mod, "plugin_meta", None)):
                raise PluginError(f"plugin '{stem}' must expose plugin_meta() -> dict")
            meta = mod.plugin_meta()
            if not isinstance(meta, dict) or not meta.get("name"):
                raise PluginError(f"plugin '{stem}': plugin_meta() must return {{'name', 'version', 'description'}}")
            return mod
        except Exception as exc:  # noqa: BLE001 — safe loading: never crash on a bad plugin
            logger.warning("failed to load plugin %s: %s", path, exc)
            self._failures[stem] = f"{type(exc).__name__}: {exc}"
            if mod is not None:
                sys.modules.pop(module_name, None)
            return None

    def load_all(self) -> Dict[str, ModuleType]:
        """(Re)discover and load every plugin. Returns {plugin name: module}.

        Broken modules are skipped and reported in :attr:`failures`.
        """
        self._modules.clear()
        self._failures.clear()
        for path in self.discover():
            mod = self._load_module(path)
            if mod is None:
                continue
            try:
                meta = mod.plugin_meta()
                self._modules[meta["name"]] = mod
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to read plugin_meta of %s: %s", path, exc)
                self._failures[path.stem] = f"{type(exc).__name__}: {exc}"
        return self._modules

    # ── access ───────────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[ModuleType]:
        """Return the loaded plugin module by name (or None)."""
        return self._modules.get(name)

    def list(self) -> List[Dict[str, str]]:
        """Return metadata for every loaded plugin: [{name, version, description}]."""
        result: List[Dict[str, str]] = []
        for name, mod in self._modules.items():
            try:
                meta = dict(mod.plugin_meta())
                meta.setdefault("name", name)
                meta.setdefault("version", "unknown")
                meta.setdefault("description", "")
                result.append(meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("plugin '%s' plugin_meta() raised: %s", name, exc)
        return result

    @property
    def failures(self) -> Dict[str, str]:
        """{module stem: error message} for modules that failed to load."""
        return dict(self._failures)

    # ── lifecycle hooks ──────────────────────────────────────────────────
    def install(self, name: str, engine: Any = None) -> bool:
        """Call ``install(engine)`` on the named plugin. True if the hook ran."""
        mod = self._modules.get(name)
        if mod is None:
            logger.warning("cannot install unknown plugin '%s'", name)
            return False
        hook = getattr(mod, "install", None)
        if not callable(hook):
            logger.info("plugin '%s' has no install() hook — skipped", name)
            return False
        try:
            hook(engine)
            logger.info("plugin '%s' installed", name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin '%s' install() raised: %s", name, exc)
            return False

    def uninstall(self, name: str) -> bool:
        """Call ``uninstall()`` on the named plugin. True if the hook ran."""
        mod = self._modules.get(name)
        if mod is None:
            logger.warning("cannot uninstall unknown plugin '%s'", name)
            return False
        hook = getattr(mod, "uninstall", None)
        if not callable(hook):
            logger.info("plugin '%s' has no uninstall() hook — skipped", name)
            return False
        try:
            hook()
            logger.info("plugin '%s' uninstalled", name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin '%s' uninstall() raised: %s", name, exc)
            return False

    def install_all(self, engine: Any = None) -> Dict[str, bool]:
        """Call ``install(engine)`` on every loaded plugin. {name: success}."""
        return {name: self.install(name, engine) for name in list(self._modules)}


__all__ = ["PluginRegistry", "PluginError", "DEFAULT_PLUGIN_DIR", "logger"]

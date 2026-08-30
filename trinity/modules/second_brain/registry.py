"""
# status: frozen (2026-09 EXECUTION 163: 冻结归档，不计维护面)
#   runtime path; engine.py facade (P0 refactor) already solved the monolith by
#   re-exporting 52 classes from split engine_* files, so lazy loading is
#   optional future optimization, not current need)
Lazy Module Registry for SecondBrain
=====================================
代替直接 import 全部122个模块，使用代理对象按需实例化。

这解决了 engine.py 9693 行单文件的启动性能问题。
"""

from __future__ import annotations

import importlib
import time
from typing import Any, Callable, Dict, Optional, Type


class LazyModule:
    """Proxy that lazily instantiates a module on first access."""

    def __init__(self, name: str, import_path: str, class_name: str,
                 init_kwargs: Optional[Dict] = None):
        self._name = name
        self._import_path = import_path
        self._class_name = class_name
        self._init_kwargs = init_kwargs or {}
        self._instance = None
        self._loaded = False
        self._load_time_ms = 0.0

    def _load(self):
        if self._loaded:
            return
        t0 = time.time()
        try:
            module = importlib.import_module(self._import_path)
            cls = getattr(module, self._class_name)
            self._instance = cls(**self._init_kwargs)
        except (ImportError, AttributeError) as e:
            # Fallback: try loading from engine.py
            from trinity.modules.second_brain.engine import SecondBrainV636
            tmp = SecondBrainV636()
            if hasattr(tmp, self._name):
                self._instance = getattr(tmp, self._name)
            else:
                raise RuntimeError(f"Cannot lazy-load {self._name}: {e}")
        self._load_time_ms = (time.time() - t0) * 1000
        self._loaded = True

    def __getattr__(self, name):
        self._load()
        return getattr(self._instance, name)

    def __call__(self, *args, **kwargs):
        self._load()
        return self._instance(*args, **kwargs)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def load_time_ms(self) -> float:
        return self._load_time_ms


class ModuleRegistry:
    """Central registry for all Trinity modules.

    Supports lazy loading, dependency injection, and diagnostics.
    """

    def __init__(self):
        self._modules: Dict[str, LazyModule] = {}
        self._guardian_chain = None
        self._retrieval = None

    def register(self, name: str, import_path: str, class_name: str,
                 **init_kwargs):
        """Register a module for lazy loading."""
        self._modules[name] = LazyModule(
            name=name,
            import_path=import_path,
            class_name=class_name,
            init_kwargs=init_kwargs,
        )

    def get(self, name: str) -> Any:
        """Get a module instance, loading if necessary."""
        if name not in self._modules:
            raise KeyError(f"Module not registered: {name}. "
                           f"Available: {list(self._modules.keys())}")
        return self._modules[name]

    def loaded_modules(self) -> Dict[str, float]:
        """Return names and load times of loaded modules."""
        return {
            name: mod.load_time_ms
            for name, mod in self._modules.items()
            if mod.is_loaded
        }

    def unload(self, name: str):
        """Force unload a module (next access re-instantiates)."""
        if name in self._modules:
            mod = self._modules[name]
            mod._instance = None
            mod._loaded = False
            mod._load_time_ms = 0.0

    def diagnostics(self) -> Dict:
        total = len(self._modules)
        loaded = len(self.loaded_modules())
        return {
            "total_registered": total,
            "loaded": loaded,
            "lazy_pending": total - loaded,
            "loaded_modules": self.loaded_modules(),
        }

    def __contains__(self, name: str) -> bool:
        return name in self._modules

    def __getitem__(self, name: str) -> Any:
        return self.get(name)

    def __len__(self) -> int:
        return len(self._modules)


# Singleton registry
_registry: Optional[ModuleRegistry] = None


def get_registry() -> ModuleRegistry:
    """Get the global module registry singleton."""
    global _registry
    if _registry is None:
        _registry = ModuleRegistry()
    return _registry


def reset_registry():
    """Reset the registry (for testing)."""
    global _registry
    _registry = None

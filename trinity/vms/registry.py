"""
Trinity VMS — Global Backend Registry.

Allows hot-swapping of storage backends, search engines, identity providers,
auditors, task brokers, and compression engines at runtime without restart.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from threading import RLock

logger = logging.getLogger(__name__)


class VMRegistry:
    """Thread-safe global registry for VMS provider backends.

    Each provider type (memory_store, identity_provider, auditor, etc.)
    maintains a dict of {name: instance}, with a concept of a "default"
    backend that is used when no name is specified.

    Usage::

        reg = VMRegistry()
        reg.register("memory_store", "sqlite", SQLiteAdapter(db_path="..."))
        store = reg.get("memory_store")            # → sqlite (default)
        store = reg.get("memory_store", "memory")  # → memory backend
    """

    def __init__(self):
        self._lock = RLock()
        self._backends: Dict[str, Dict[str, Any]] = {}      # provider_type → {name → instance}
        self._defaults: Dict[str, str] = {}                 # provider_type → default_name
        self._aliases: Dict[str, Dict[str, str]] = {}       # provider_type → {alias → name}

    # ── Registration ──────────────────────────────────────────────────

    def register(
        self,
        provider_type: str,
        name: str,
        instance: Any,
        set_default: bool = False,
        aliases: Optional[List[str]] = None,
    ) -> None:
        """Register a backend instance.

        Parameters
        ----------
        provider_type : str
            One of memory_store / identity_provider / auditor /
            task_broker / search_engine / compression_engine.
        name : str
            Unique name for this backend (e.g. "sqlite", "postgres", "memory").
        instance : Any
            The backend instance (must satisfy the corresponding Protocol).
        set_default : bool
            If True, mark this as the default backend for the provider_type.
        aliases : Optional[List[str]]
            Additional short names that can be used to reference this backend.
        """
        with self._lock:
            if provider_type not in self._backends:
                self._backends[provider_type] = {}
                self._aliases[provider_type] = {}

            self._backends[provider_type][name] = instance

            if aliases:
                for alias in aliases:
                    self._aliases[provider_type][alias] = name

            if set_default or provider_type not in self._defaults:
                self._defaults[provider_type] = name
                logger.debug("VMS: default %s → %s", provider_type, name)

    # ── Retrieval ─────────────────────────────────────────────────────

    def get(
        self,
        provider_type: str,
        name: Optional[str] = None,
    ) -> Optional[Any]:
        """Retrieve a backend instance.

        Parameters
        ----------
        provider_type : str
            The provider type to look up.
        name : Optional[str]
            Backend name or alias.  If None, returns the default.
        """
        with self._lock:
            pool = self._backends.get(provider_type, {})
            if not pool:
                return None

            resolved = name
            if resolved is None:
                resolved = self._defaults.get(provider_type)
                if resolved is None or resolved not in pool:
                    # Fallback to first registered
                    return next(iter(pool.values())) if pool else None
            else:
                # Resolve alias
                resolved = self._aliases.get(provider_type, {}).get(resolved, resolved)

            return pool.get(resolved)

    def list_backends(self, provider_type: str) -> List[str]:
        """Return list of registered backend names for a provider type."""
        with self._lock:
            return list(self._backends.get(provider_type, {}).keys())

    def get_default(self, provider_type: str) -> Optional[str]:
        """Return the name of the current default backend."""
        with self._lock:
            return self._defaults.get(provider_type)

    # ── Hot-swap ──────────────────────────────────────────────────────

    def switch_backend(self, provider_type: str, name: str) -> bool:
        """Hot-switch the default backend for a provider type.

        Parameters
        ----------
        provider_type : str
            Provider type to switch.
        name : str
            Name or alias of the backend to switch to.

        Returns
        -------
        bool : True if the switch succeeded.
        """
        with self._lock:
            # Resolve alias
            resolved = self._aliases.get(provider_type, {}).get(name, name)

            pool = self._backends.get(provider_type, {})
            if resolved not in pool:
                logger.warning("VMS: cannot switch %s → %s (not registered)",
                               provider_type, name)
                return False

            old = self._defaults.get(provider_type)
            self._defaults[provider_type] = resolved
            logger.info("VMS: switched %s from %s → %s", provider_type, old, resolved)
            return True

    # ── Introspection ─────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Return a summary of all registered backends and defaults."""
        with self._lock:
            return {
                ptype: {
                    "backends": list(pool.keys()),
                    "default": self._defaults.get(ptype),
                    "aliases": {
                        a: n for a, n in self._aliases.get(ptype, {}).items()
                    },
                }
                for ptype, pool in self._backends.items()
            }

    def unregister(self, provider_type: str, name: str) -> bool:
        """Remove a backend from the registry."""
        with self._lock:
            pool = self._backends.get(provider_type, {})
            if name not in pool:
                return False
            del pool[name]
            # Clear default if we just removed it
            if self._defaults.get(provider_type) == name:
                del self._defaults[provider_type]
                if pool:
                    self._defaults[provider_type] = next(iter(pool.keys()))
            # Clear aliases pointing to this name
            alias_map = self._aliases.get(provider_type, {})
            to_remove = [a for a, n in alias_map.items() if n == name]
            for a in to_remove:
                del alias_map[a]
            return True


# ── Module-level singleton ──────────────────────────────────────────────

_global_registry: Optional[VMRegistry] = None


def get_registry() -> VMRegistry:
    """Return the module-level VMS Registry singleton."""
    global _global_registry
    if _global_registry is None:
        _global_registry = VMRegistry()
    return _global_registry

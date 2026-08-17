"""
Example Trinity Plugin
======================
A minimal, well-behaved plugin demonstrating the plugin contract:

* ``plugin_meta()``  — required; returns {name, version, description}.
* ``install(engine)`` — optional; logs "plugin installed" and attaches a
  callback to the engine when the engine supports hooks (``plugin_hooks``
  dict or a plain attribute).
* ``uninstall()``     — optional; logs the removal.
"""

import logging

logger = logging.getLogger("trinity.plugins.example")

_PLUGIN_NAME = "example"
_PLUGIN_VERSION = "1.0.0"
_PLUGIN_DESCRIPTION = (
    "Example plugin: logs installation and attaches a callback to the engine."
)


def plugin_meta() -> dict:
    """Required: metadata describing this plugin."""
    return {
        "name": _PLUGIN_NAME,
        "version": _PLUGIN_VERSION,
        "description": _PLUGIN_DESCRIPTION,
    }


def _on_ingest(payload=None):
    """Callback attached to the engine (demonstrates engine hook wiring)."""
    logger.debug("example plugin on_ingest callback: %r", payload)
    return payload


def install(engine=None):
    """Optional: called by PluginRegistry.install(engine) / install_all(engine).

    Prints and logs "plugin installed", then attaches a callback to the
    engine: into ``engine.plugin_hooks`` when the engine keeps such a dict,
    otherwise as a plain ``engine.example_callback`` attribute.
    """
    message = f"plugin installed: {_PLUGIN_NAME} v{_PLUGIN_VERSION}"
    print(f"[plugin:{_PLUGIN_NAME}] {message}")
    logger.info("%s (engine=%r)", message, engine)

    if engine is not None:
        hooks = getattr(engine, "plugin_hooks", None)
        if isinstance(hooks, dict):
            hooks[f"{_PLUGIN_NAME}.on_ingest"] = _on_ingest
        try:
            setattr(engine, "example_callback", _on_ingest)
        except Exception:  # noqa: BLE001 — read-only engine objects are fine
            pass
    return _on_ingest


def uninstall():
    """Optional: called by PluginRegistry.uninstall(name)."""
    message = f"plugin uninstalled: {_PLUGIN_NAME}"
    print(f"[plugin:{_PLUGIN_NAME}] {message}")
    logger.info(message)
    return message

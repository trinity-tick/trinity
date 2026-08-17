"""Tests for trinity.plugins (PluginRegistry — discovery, loading, safe isolation)."""

import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.plugins import PluginError, PluginRegistry  # noqa: E402


def _write(path, content):
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_discover_and_load(tmp_path):
    _write(
        tmp_path / "good_plugin.py",
        '''
        def plugin_meta():
            return {"name": "good", "version": "1.0.0", "description": "good plugin"}

        def install(engine=None):
            return "installed-good"

        def uninstall():
            return "uninstalled-good"
        ''',
    )
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    modules = reg.load_all()
    assert isinstance(modules, dict)
    assert "good" in modules
    mod = reg.get("good")
    assert mod is not None
    assert mod.plugin_meta()["version"] == "1.0.0"
    metas = reg.list()
    assert any(m["name"] == "good" and m["version"] == "1.0.0" for m in metas)


def test_import_error_plugin_does_not_crash(tmp_path):
    _write(tmp_path / "bad_plugin.py", 'raise RuntimeError("boom at import")\n')
    _write(
        tmp_path / "ok_plugin.py",
        '''
        def plugin_meta():
            return {"name": "ok", "version": "0.1.0", "description": "fine"}
        ''',
    )
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    modules = reg.load_all()
    # the good plugin still loads; the bad one is isolated in .failures
    assert "ok" in modules
    assert "bad_plugin" in reg.failures
    assert "boom" in reg.failures["bad_plugin"]


def test_syntax_error_plugin_isolated(tmp_path):
    _write(tmp_path / "syntax_bad.py", "def broken(:\n")
    _write(
        tmp_path / "fine.py",
        'def plugin_meta():\n    return {"name": "fine", "version": "1", "description": "ok"}\n',
    )
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    modules = reg.load_all()
    assert "fine" in modules
    assert "syntax_bad" in reg.failures


def test_missing_plugin_meta_rejected(tmp_path):
    _write(tmp_path / "no_meta.py", "x = 1\n")
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    assert reg.load_all() == {}
    assert "no_meta" in reg.failures
    assert reg.get("no_meta") is None


def test_plugin_dir_from_env(tmp_path, monkeypatch):
    _write(
        tmp_path / "env_plugin.py",
        'def plugin_meta():\n    return {"name": "envp", "version": "1", "description": "from env"}\n',
    )
    monkeypatch.setenv("TRINITY_PLUGIN_DIR", str(tmp_path))
    reg = PluginRegistry()  # no explicit dir → env var wins
    assert "envp" in reg.load_all()


def test_missing_default_dir_is_empty():
    reg = PluginRegistry(plugin_dir="__definitely_not_a_real_dir__")
    assert reg.load_all() == {}
    assert reg.list() == []
    assert reg.failures == {}


def test_install_uninstall_and_engine_callback(tmp_path, capsys):
    _write(
        tmp_path / "cb_plugin.py",
        '''
        def plugin_meta():
            return {"name": "cb", "version": "1.0.0", "description": "callback plugin"}

        def install(engine=None):
            print("plugin installed")
            hooks = getattr(engine, "plugin_hooks", None)
            if isinstance(hooks, dict):
                hooks["cb.on_ingest"] = lambda payload: payload
            return "installed"

        def uninstall():
            return "uninstalled"
        ''',
    )

    class FakeEngine:
        def __init__(self):
            self.plugin_hooks = {}

    engine = FakeEngine()
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    reg.load_all()
    assert reg.install("cb", engine) is True
    assert "cb.on_ingest" in engine.plugin_hooks
    assert "plugin installed" in capsys.readouterr().out
    assert reg.uninstall("cb") is True


def test_install_unknown_plugin_returns_false(tmp_path):
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    reg.load_all()
    assert reg.install("nope", engine=None) is False
    assert reg.uninstall("nope") is False


def test_install_all(tmp_path):
    _write(
        tmp_path / "a.py",
        '''
        def plugin_meta():
            return {"name": "a", "version": "1", "description": "a"}
        def install(engine=None):
            return "a-installed"
        ''',
    )
    _write(
        tmp_path / "b.py",
        '''
        def plugin_meta():
            return {"name": "b", "version": "1", "description": "b"}
        def install(engine=None):
            return "b-installed"
        ''',
    )
    reg = PluginRegistry(plugin_dir=str(tmp_path))
    reg.load_all()
    result = reg.install_all(engine=None)
    assert result == {"a": True, "b": True}


def test_plugin_error_type_exported():
    assert issubclass(PluginError, RuntimeError)

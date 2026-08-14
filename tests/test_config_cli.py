"""Tests for scripts/trinity_config_cli.py (--show / --apply / interactive)."""

import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import trinity_config_cli as cli  # noqa: E402

PLACEHOLDER = "${TRINITY_PG_PASSWORD:-postgres}"


def _apply(tmp_path, payload, extra=None):
    cfg = tmp_path / "trinity.yaml"
    env = tmp_path / ".env"
    argv = ["--apply", json.dumps(payload), "--config", str(cfg), "--env", str(env)]
    argv += extra or []
    rc = cli.main(argv)
    return rc, cfg, env


# ── --apply ──────────────────────────────────────────────────────────────
def test_apply_placeholder_not_plaintext(tmp_path):
    secret = "s3cr3t-pw-42"
    payload = {
        "pg_host": "127.0.0.1",
        "pg_user": "postgres",
        "pg_password": secret,
        "api_port": 8001,
        "cache_backend": "memory",
        "rate_limit_rate": 30,
        "rate_limit_burst": 60,
    }
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 0
    yaml_text = cfg.read_text(encoding="utf-8")
    # yaml carries the placeholder, never the plaintext password
    assert PLACEHOLDER in yaml_text
    assert "pg_password: " + PLACEHOLDER in yaml_text
    assert secret not in yaml_text
    # the real password goes to the env file only (outside git)
    env_text = env.read_text(encoding="utf-8")
    assert f"TRINITY_PG_PASSWORD={secret}" in env_text


def test_apply_empty_password_uses_placeholder(tmp_path):
    payload = {"pg_password": "", "cache_backend": "off"}
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 0
    assert PLACEHOLDER in cfg.read_text(encoding="utf-8")
    env_text = env.read_text(encoding="utf-8")
    assert "TRINITY_PG_PASSWORD=" not in env_text
    assert "TRINITY_PG_HOST=127.0.0.1" in env_text  # non-secret values still written


def test_apply_null_password(tmp_path):
    payload = {"pg_password": None}
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 0
    assert PLACEHOLDER in cfg.read_text(encoding="utf-8")
    assert "TRINITY_PG_PASSWORD=" not in env.read_text(encoding="utf-8")


def test_apply_rejects_bad_cache_backend(tmp_path):
    payload = {"cache_backend": "banana"}
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 1
    assert not cfg.exists()
    assert not env.exists()


def test_apply_rejects_bad_rate_limit(tmp_path):
    payload = {"rate_limit_rate": 0}
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 1
    assert not cfg.exists()


def test_apply_rejects_invalid_json(tmp_path):
    rc, cfg, env = _apply(tmp_path, "not json {")
    assert rc == 1
    assert not cfg.exists()


def test_apply_values_land_in_yaml(tmp_path):
    payload = {
        "pg_host": "10.0.0.5",
        "pg_port": 5433,
        "pg_db": "t2",
        "pg_user": "alice",
        "api_port": 9001,
        "cache_backend": "redis",
        "rate_limit_rate": 5,
        "rate_limit_burst": 9,
    }
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 0
    text = cfg.read_text(encoding="utf-8")
    assert "pg_host: 10.0.0.5" in text
    assert "pg_port: 5433" in text
    assert "pg_db: t2" in text
    assert "pg_user: alice" in text
    assert "port: 9001" in text
    assert "cache_backend: redis" in text
    assert "rate: 5" in text
    assert "burst: 9" in text
    assert f"pg_password: {PLACEHOLDER}" in text
    assert "TRINITY_PG_PASSWORD=" not in text  # no plaintext anywhere in yaml


def test_apply_accepts_trinity_prefixed_keys(tmp_path):
    payload = {"TRINITY_PG_HOST": "192.168.1.9", "TRINITY_CACHE_BACKEND": "memory"}
    rc, cfg, env = _apply(tmp_path, payload)
    assert rc == 0
    text = cfg.read_text(encoding="utf-8")
    assert "pg_host: 192.168.1.9" in text
    assert "cache_backend: memory" in text


def test_apply_preserves_existing_structure(tmp_path):
    cfg = tmp_path / "trinity.yaml"
    cfg.write_text(
        "storage:\n"
        "  db_type: postgresql\n"
        "  pg_host: old-host\n"
        "  pg_min_conn: 3\n"
        "guardian:\n"
        "  enabled: false\n"
        "  tiers: L1,L2\n"
        "  blocking_policy: aggregate\n",
        encoding="utf-8",
    )
    rc, cfg, env = _apply(tmp_path, {"pg_host": "new-host", "pg_password": "sekrit"})
    assert rc == 0
    text = cfg.read_text(encoding="utf-8")
    assert "pg_host: new-host" in text
    assert "pg_min_conn: 3" in text
    assert "tiers: L1,L2" in text
    assert "blocking_policy: aggregate" in text
    assert f"pg_password: {PLACEHOLDER}" in text
    assert "sekrit" not in text


def test_apply_no_env_flag(tmp_path):
    payload = {"pg_password": "pw123"}
    rc, cfg, env = _apply(tmp_path, payload, extra=["--no-env"])
    assert rc == 0
    assert PLACEHOLDER in cfg.read_text(encoding="utf-8")
    assert not env.exists()


# ── --show ───────────────────────────────────────────────────────────────
def test_show_masks_password(tmp_path, capsys):
    cfg = tmp_path / "trinity.yaml"
    cfg.write_text(
        "storage:\n  pg_host: 127.0.0.1\n  pg_password: super-secret-value\n",
        encoding="utf-8",
    )
    rc = cli.main(["--show", "--config", str(cfg), "--env", str(tmp_path / ".env")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "super-secret-value" not in out
    assert "***" in out
    assert "pg_host: 127.0.0.1" in out


def test_show_missing_config(tmp_path, capsys):
    rc = cli.main(["--show", "--config", str(tmp_path / "nope.yaml"), "--env", str(tmp_path / ".env")])
    assert rc == 2


# ── interactive wizard ───────────────────────────────────────────────────
def test_interactive_wizard(tmp_path, monkeypatch, capsys):
    answers = iter(
        [
            "10.1.2.3",  # pg_host
            "5432",      # pg_port
            "trinity",   # pg_db
            "postgres",  # pg_user
            "pw123",     # pg_password (getpass)
            "8001",      # api_port
            "off",       # cache_backend
            "60",        # rate_limit_rate
            "120",       # rate_limit_burst
            "y",         # write password to .env
        ]
    )
    monkeypatch.setattr(cli, "input", lambda prompt="": next(answers), raising=False)
    monkeypatch.setattr(cli, "getpass", types.SimpleNamespace(getpass=lambda prompt="": next(answers)))

    cfg = tmp_path / "trinity.yaml"
    env = tmp_path / ".env"
    rc = cli.main(["--config", str(cfg), "--env", str(env)])
    assert rc == 0
    text = cfg.read_text(encoding="utf-8")
    assert "pg_host: 10.1.2.3" in text
    assert f"pg_password: {PLACEHOLDER}" in text
    assert "pw123" not in text
    env_text = env.read_text(encoding="utf-8")
    assert "TRINITY_PG_PASSWORD=pw123" in env_text
    assert "wrote" in capsys.readouterr().out

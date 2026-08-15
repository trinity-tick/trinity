#!/usr/bin/env python3
"""
Trinity — Harvester 运行器（2026-08-15）
========================================
按 harvesters/registry.json 加载启用的采集插件，执行 harvest(config)，
把返回的记忆条目写入 SQLite 大库（运行时权威）。

用法：
    python scripts/run_harvesters.py                 # 跑 registry 中所有启用插件
    python scripts/run_harvesters.py --plugin file-harvester
    python scripts/run_harvesters.py --dry-run       # 只采集不写入
    python scripts/run_harvesters.py --config '{"dirs":["C:/notes"]}'   # 覆盖配置

依赖：插件实现 harvest(config) -> [{content, category, tags, importance, metadata}]
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("run_harvesters")

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

REGISTRY = _TRINITY_ROOT / "harvesters" / "registry.json"
DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")


def load_registry() -> List[Dict[str, Any]]:
    if not REGISTRY.exists():
        return []
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return [p for p in data.get("plugins", []) if p.get("enabled")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity harvester runner")
    parser.add_argument("--plugin", default="", help="只跑指定插件 id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="", help="JSON 覆盖插件配置")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    args = parser.parse_args()

    overrides: Dict[str, Any] = {}
    if args.config:
        overrides = json.loads(args.config)

    from trinity.adapters.sqlite import SQLiteAdapter

    adapter = None if args.dry_run else SQLiteAdapter(db_path=args.sqlite_path)
    if adapter:
        adapter.connect()

    plugins = load_registry()
    if args.plugin:
        plugins = [p for p in plugins if p["id"] == args.plugin]
    if not plugins:
        logger.warning("no enabled plugins in %s", REGISTRY)
        return 0

    total = 0
    for spec in plugins:
        pid = spec["id"]
        try:
            mod = importlib.import_module(spec["module"])
        except Exception as exc:  # noqa: BLE001
            logger.error("plugin %s import failed: %s", pid, exc)
            continue
        harvest = getattr(mod, "harvest", None)
        if not callable(harvest):
            logger.error("plugin %s has no harvest()", pid)
            continue
        config = dict(spec.get("config") or {})
        config.update(overrides)
        try:
            items = harvest(config)
        except Exception as exc:  # noqa: BLE001
            logger.error("plugin %s harvest failed: %s", pid, exc)
            continue
        if not items:
            logger.info("plugin %s: 0 items", pid)
            continue
        if args.dry_run:
            logger.info("plugin %s: %d items (dry-run, 未写入)", pid, len(items))
            total += len(items)
            continue
        for it in items:
            try:
                adapter.store_memory(
                    content=it.get("content", ""),
                    persona_id=it.get("persona_id", "default"),
                    session_id=it.get("session_id") or "harvest",
                    agent_id=it.get("agent_id", "default"),
                    role=it.get("role", "user"),
                    importance=float(it.get("importance", 0.5)),
                    tags=it.get("tags") or [],
                    category=it.get("category", "harvested"),
                    metadata=it.get("metadata") or {},
                    source_uri=(it.get("metadata") or {}).get("source_uri"),
                )
                total += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("write failed: %s", exc)
        logger.info("plugin %s: %d items written", pid, len(items))

    if adapter:
        adapter.disconnect()
    logger.info("harvest total: %d", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())

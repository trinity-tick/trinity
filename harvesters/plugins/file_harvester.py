"""
Trinity Harvester — 本地文件导入采集器（2026-08-15）
======================================================
把指定目录下的 .md/.txt/.log 文件转为结构化记忆。

接口遵循 harvesters/plugin_spec.md：
  PLUGIN = {...};  def harvest(config) -> list[dict]

幂等：按 (path, mtime, size) 记录已导入状态（harvesters/state.json），
重复运行跳过未变更文件。

用法（配合 scripts/run_harvesters.py）：
  config: {"dirs": ["C:/path/to/notes"], "category": "file_harvested", "tags": ["file"]}
"""

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("trinity.harvesters.file")

PLUGIN = {
    "id": "file-harvester",
    "name": "本地文件导入采集器",
    "version": "1.0.0",
    "source": "local-files",
    "capabilities": ["text", "markdown"],
}

_STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"  # harvesters/state.json
_MAX_BYTES = 200_000  # 单文件上限 200KB


def _load_state() -> dict:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("state save failed: %s", exc)


def _file_fingerprint(path: Path) -> tuple:
    st = path.stat()
    return (str(path), st.st_mtime, st.st_size)


def harvest(config: dict) -> list[dict]:
    dirs = config.get("dirs") or []
    category = config.get("category", "file_harvested")
    tags = list(config.get("tags") or []) + [PLUGIN["id"]]
    importance = float(config.get("importance", 0.5))

    state = _load_state()
    seen = set(state.get("imported", []))
    memories: list[dict] = []
    new_state: dict = {"imported": list(seen)}

    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            logger.warning("dir not found: %s", d)
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".md", ".txt", ".log"):
                continue
            if path.stat().st_size > _MAX_BYTES:
                continue
            fp = _file_fingerprint(path)
            key = f"{fp[0]}|{fp[1]}|{fp[2]}"
            if key in seen:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("read failed %s: %s", path, exc)
                continue
            if not content:
                continue
            memories.append({
                "content": content[:_MAX_BYTES],
                "category": category,
                "tags": tags + [f"file:{path.name}"],
                "importance": importance,
                "metadata": {
                    "source_uri": str(path),
                    "mtime": fp[1],
                    "size": fp[2],
                    "ext": path.suffix.lstrip("."),
                },
            })
            new_state["imported"].append(key)

    if memories:
        _save_state(new_state)
        logger.info("file-harvester: harvested %d files from %d dirs", len(memories), len(dirs))
    return memories

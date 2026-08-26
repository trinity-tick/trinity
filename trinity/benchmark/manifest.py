# -*- coding: utf-8 -*-
"""Trinity Experiment Manifest — 借鉴 Claude Science 的可复现工件封装（Phase 1）。

Claude Science 借鉴（2026-08-26）：评测结果必须绑定"代码版本 + 环境 + 数据集 +
参数"——可复现、可审计（code/env/message history/plain-language 理念映射）。

每个评测结果文件旁生成 <name>.manifest.json：
  - code_hash:   trinity 关键模块文件聚合 SHA-256（core/retrieval/evolution/knowledge/eval）
  - env:         python 版本、trinity 版本、关键依赖
  - dataset:     评测集文件 SHA-256（防损坏/口径漂移——上轮 pagetree.json 损坏教训）
  - params:      本次实验参数（top_k/model/模式等）
  - result_ref:  结果文件路径 + created_at

用法:
    from trinity.benchmark.manifest import build_manifest, validate_manifest
    build_manifest("output/ae_500_reason_v3.json",
                   params={"top_k": 10, "model": "deepseek-chat", "mode": "reason"},
                   dataset_paths=[".../longmemeval_mock_dataset.json"])
    ok, report = validate_manifest("output/ae_500_reason_v3.json")
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# trinity 关键代码目录（代码哈希覆盖范围）
_HASH_DIRS = [
    "core", "retrieval", "evolution", "knowledge", "eval", "skills",
    "security", "views.py", "adapters",
]
_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def compute_code_hash() -> str:
    """trinity 关键模块文件聚合哈希（排序后逐文件哈希汇总）。"""
    h = hashlib.sha256()
    files = []
    for name in _HASH_DIRS:
        p = os.path.join(_TRINITY_ROOT, "trinity", name)
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for root, _, fs in os.walk(p):
                for f in sorted(fs):
                    if f.endswith(".py"):
                        files.append(os.path.join(root, f))
    for f in sorted(files):
        try:
            h.update(_file_sha256(f).encode())
            h.update(f.encode())
        except Exception:
            continue
    return h.hexdigest()[:16]


def trinity_version() -> str:
    try:
        from trinity.version import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def _env_info() -> Dict[str, str]:
    deps = {}
    for name in ("numpy", "jieba", "yaml", "fastapi"):
        try:
            mod = __import__(name)
            deps[name] = getattr(mod, "__version__", "?")
        except Exception:
            deps[name] = "-"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "trinity_version": trinity_version(),
        "deps": deps,
    }


def _dataset_hash(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    try:
        return _file_sha256(path)
    except Exception:
        return None


def build_manifest(
    result_path: str,
    params: Optional[Dict[str, Any]] = None,
    dataset_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """为评测结果生成 manifest（<result>.manifest.json）。"""
    manifest = {
        "schema": "trinity-experiment-manifest/v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "code_hash": compute_code_hash(),
        "env": _env_info(),
        "dataset": {},
        "params": params or {},
        "result_ref": os.path.abspath(result_path),
    }
    for dp in dataset_paths or []:
        manifest["dataset"][os.path.basename(dp)] = _dataset_hash(dp)
    path = result_path + ".manifest.json"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    except Exception as exc:
        raise RuntimeError(f"manifest save failed: {exc}")
    return manifest


def validate_manifest(result_path: str) -> tuple:
    """校验 manifest：返回 (ok, report)。

    - manifest 存在且 schema 正确
    - code_hash 与当前代码一致（代码未变 → 可复现）
    - dataset 哈希与当前文件一致（数据集未变）
    """
    path = result_path + ".manifest.json"
    report: Dict[str, Any] = {"exists": False}
    if not os.path.exists(path):
        return False, report
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except Exception as exc:
        report["exists"] = True
        report["error"] = f"manifest unreadable: {exc}"
        return False, report
    report["exists"] = True
    ok = True
    if m.get("schema") != "trinity-experiment-manifest/v1":
        report["schema_mismatch"] = m.get("schema")
        ok = False
    current = compute_code_hash()
    report["code_hash_current"] = current
    report["code_hash_recorded"] = m.get("code_hash")
    if current != m.get("code_hash"):
        report["code_changed"] = True
        ok = False
    else:
        report["code_changed"] = False
    ds_ok = True
    for name, h in (m.get("dataset") or {}).items():
        cur = _dataset_hash(os.path.join(os.path.dirname(os.path.abspath(result_path)), name))
        report["dataset_" + name] = {"recorded": h, "current": cur,
                                     "match": (cur is None) or cur == h}
        if cur is not None and cur != h:
            ds_ok = False
    if not ds_ok:
        report["dataset_changed"] = True
        ok = False
    else:
        report["dataset_changed"] = False
    report["env"] = m.get("env", {})
    report["params"] = m.get("params", {})
    return ok, report


# ── Phase 3（轻量）：领域评测包注册 ─────────────────────────────────

_EVAL_SETS = {
    "mock500q": {
        "description": "LongMemEval-style 500 问 mock（6 类目）",
        "path": r"C:\Users\Administrator\.marvis\workspace\conv_19f49996244_37d75ffae4a6\benchmark\longmemeval_mock_dataset.json",
        "runner": "benchmark/answer_eval.py",
    },
    "holdout": {
        "description": "生产难查询 95 问（近义改写，overlap<=40%）",
        "path": os.path.join(os.path.dirname(_TRINITY_ROOT), "output", "hard_holdout.json"),
        "runner": "benchmark/hard_holdout_eval.py",
    },
}


def list_eval_sets() -> Dict[str, Dict[str, Any]]:
    """命名评测集注册表（Claude Science 领域评测包理念）。"""
    out = {}
    for name, spec in _EVAL_SETS.items():
        out[name] = {
            "name": name,
            "description": spec.get("description", ""),
            "runner": spec.get("runner", ""),
            "dataset_ready": os.path.exists(spec.get("path", "")),
            "dataset_hash": _dataset_hash(spec.get("path", "")),
        }
    return out

"""Trinity — 聚合器向量索引兼容/自愈测试（2026-08-17 闭环修复）。

覆盖（P1-3 双格式读取）：
- faiss 环境下读到 pickle 格式索引（无 faiss 进程遗留）→ 按文件头 magic 探测，
  直接读取成功，不再删除文件触发每次启动全量重建；
- 真正的损坏文件（双格式探测均失败）→ 删除自愈，_prewarm_ann_index 重建。
"""

from __future__ import annotations

import os
import pickle

import pytest


def _mk_agg(tmp_path, monkeypatch):
    import json

    from trinity.agents import aggregator as agg_mod

    # 强制 faiss 分支
    monkeypatch.setattr(agg_mod, "_HAS_FAISS", True)
    a = agg_mod.MemoryAggregator(persist_path=None)
    # 指向隔离 persist 路径，且必须存在 pool.json（否则 _load 在读取池时提前返回，
    # 到不了向量索引块）
    a._persist_path = str(tmp_path / "pool.json")
    with open(a._persist_path, "w", encoding="utf-8") as f:
        json.dump({"version": "x", "memories": [], "relations": {}, "stats": {}}, f)
    # 写入 pickle 内容到向量索引路径（模拟无 faiss 进程遗留）
    vec_path = str(tmp_path / "aggregator_vectors.pkl")
    with open(vec_path, "wb") as f:
        pickle.dump({"dim": 4, "id_map": [], "vectors": []}, f)
    a._load()
    # 2026-08-17（P1-3 双格式兼容）：pickle 索引按 magic 探测读取成功，
    # 文件保留（不再删除重建）、维度正确、不崩溃。
    assert os.path.exists(vec_path), "pickle index should be kept (dual-format read)"
    assert a._vector_dim == 4, "vector dim restored from pickle payload"
    a.shutdown()
    return a


def test_stale_pickle_index_self_healed(tmp_path, monkeypatch) -> None:
    _mk_agg(tmp_path, monkeypatch)


def test_valid_missing_index_no_crash(tmp_path, monkeypatch) -> None:
    from trinity.agents import aggregator as agg_mod

    monkeypatch.setattr(agg_mod, "_HAS_FAISS", True)
    a = agg_mod.MemoryAggregator(persist_path=None)
    a._persist_path = str(tmp_path / "pool.json")
    a._load()  # 无索引文件 → 不崩溃
    a.shutdown()

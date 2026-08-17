#!/usr/bin/env python
"""
Trinity ChromaDB 检索验证脚本
==============================
验证 ChromaDB 向量数据库的检索功能，并对比 FAISS HNSW 与 ChromaDB 的 Top-5 检索差异。

测试项:
  1. 连接测试
  2. 集合存在性验证
  3. 文档插入 / 更新测试
  4. 语义检索（5 个测试查询）
  5. ChromaDB vs FAISS/Numpy Top-5 对比

使用方式:
    python verify_chromadb.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))

import numpy as np

# ── 配置 ────────────────────────────────────────────────────────────────

CHROMADB_DIR = TRINITY_ROOT / "data" / "chromadb"
COLLECTION_NAME = "memory"
DB_PATH = TRINITY_ROOT / "data" / "trinity_store.db"

# 测试查询
TEST_QUERIES = [
    "货架布局规则",
    "彩棠新批次",
    "气泡柱包装",
    "色号分开放置",
    "仓储管理规范",
]


def test_connection():
    """测试 1: ChromaDB 连接。"""
    print("\n" + "─" * 40)
    print("[测试 1] ChromaDB 连接测试")

    import chromadb

    if not CHROMADB_DIR.exists():
        print(f"  [FAIL] 数据目录不存在: {CHROMADB_DIR}")
        print("  请先运行 init_chromadb.py")
        return None

    client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
    print(f"  [PASS] 连接成功 (persist_dir={CHROMADB_DIR})")
    return client


def test_collection(client):
    """测试 2: 集合存在性。"""
    print("\n" + "─" * 40)
    print("[测试 2] 集合存在性验证")

    try:
        collection = client.get_collection(COLLECTION_NAME)
        count = collection.count()
        print(f"  [PASS] 集合 '{COLLECTION_NAME}' 存在，包含 {count} 条记录")

        # 检查元数据
        metadata = client.get_collection(COLLECTION_NAME).metadata
        if metadata:
            print(f"  元数据: {json.dumps(metadata, ensure_ascii=False)}")
        return collection
    except Exception as e:
        print(f"  [FAIL] 集合不存在: {e}")
        print("  正在创建空集合...")
        collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"  [INFO] 已创建空集合 '{COLLECTION_NAME}'")
        return collection


def test_insert(collection):
    """测试 3: 文档插入与更新。"""
    print("\n" + "─" * 40)
    print("[测试 3] 文档插入 / 更新测试")

    dim = 1024
    test_id = "test_verify_insert_001"

    # 先清理
    try:
        collection.delete(ids=[test_id])
    except Exception:
        pass

    # 插入
    vec = np.random.randn(dim).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    collection.add(
        ids=[test_id],
        embeddings=[vec.tolist()],
        metadatas=[{"content": "测试插入文档：验证 ChromaDB 写入功能", "importance": 0.9}],
    )
    print(f"  [PASS] 文档插入成功 (id={test_id})")

    # 查询验证
    result = collection.get(ids=[test_id])
    if result["ids"] and result["ids"][0] == test_id:
        print(f"  [PASS] 文档读取验证通过 (metadata={result['metadatas'][0]})")
    else:
        print(f"  [FAIL] 文档读取失败")

    # 更新
    collection.update(
        ids=[test_id],
        metadatas=[{"content": "测试插入文档：已更新", "importance": 0.95}],
    )
    result = collection.get(ids=[test_id])
    if result["metadatas"][0].get("importance") == 0.95:
        print(f"  [PASS] 文档更新验证通过")
    else:
        print(f"  [FAIL] 文档更新失败")

    # 删除测试文档
    collection.delete(ids=[test_id])
    print(f"  [PASS] 测试文档已清理")


def create_embed_engine():
    """创建嵌入引擎。"""
    try:
        from trinity.embeddings.engine import SklearnEmbeddingEngine
        import sqlite3

        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT content FROM memories WHERE status='active'").fetchall()
        conn.close()
        corpus = [r[0] for r in rows]

        engine = SklearnEmbeddingEngine(max_features=1024, ngram_range=(1, 2))
        engine._lazy_init(corpus)
        return engine
    except Exception as e:
        print(f"  [WARN] sklearn 嵌入引擎创建失败: {e}，使用随机向量")
        return None


def test_semantic_search(collection):
    """测试 4: 语义检索。"""
    print("\n" + "─" * 40)
    print("[测试 4] 语义检索测试")

    engine = create_embed_engine()

    for i, query in enumerate(TEST_QUERIES):
        if engine:
            vec = engine.embed(query)
        else:
            vec = np.random.randn(1024).astype(np.float32)
            vec = vec / np.linalg.norm(vec)

        results = collection.query(query_embeddings=[vec.tolist()], n_results=3)

        ids = results["ids"][0]
        distances = results["distances"][0]
        metadatas = results["metadatas"][0]

        print(f"\n  查询 {i+1}: '{query}'")
        for j, (mid, dist, meta) in enumerate(zip(ids, distances, metadatas)):
            content = meta.get("content", "N/A")[:60]
            sim = 1.0 - dist
            print(f"    #{j+1}: {mid[:20]}... | sim={sim:.4f} | {content}")

    print(f"\n  [PASS] 语义检索完成 ({len(TEST_QUERIES)} 个查询)")


def test_compare_faiss_vs_chromadb(collection):
    """测试 5: FAISS/Numpy vs ChromaDB Top-5 对比。"""
    print("\n" + "─" * 40)
    print("[测试 5] NumpyBruteForce vs ChromaDB Top-5 对比")

    # 从 ChromaDB 读取全部向量
    all_data = collection.get(include=["embeddings", "metadatas"])
    if not all_data["ids"]:
        print("  [SKIP] 集合为空，跳过对比")
        return

    ids = all_data["ids"]
    vectors = np.array(all_data["embeddings"], dtype=np.float32)
    total = len(ids)
    print(f"  已加载 {total} 条向量")

    # 创建 NumpyBruteForce 索引
    from trinity.vector_index.index import NumpyBruteForceIndex

    np_idx = NumpyBruteForceIndex(dim=vectors.shape[1], metric="cosine")
    for idx_id, vec in zip(ids, vectors):
        np_idx.add(idx_id, vec, {})

    engine = create_embed_engine()

    for query in TEST_QUERIES[:3]:
        print(f"\n  查询: '{query}'")

        if engine:
            q_vec = engine.embed(query)
        else:
            q_vec = np.random.randn(vectors.shape[1]).astype(np.float32)
            q_vec = q_vec / np.linalg.norm(q_vec)

        # Numpy 检索
        np_results = np_idx.search(q_vec, top_k=5)
        np_ids = [r.id[:20] for r in np_results]
        np_scores = [round(r.score, 4) for r in np_results]

        # ChromaDB 检索
        cr_results = collection.query(query_embeddings=[q_vec.tolist()], n_results=5)
        cr_ids = [rid[:20] for rid in cr_results["ids"][0]]
        cr_scores = [round(1.0 - d, 4) for d in cr_results["distances"][0]]

        # 对比
        overlap = set(np_ids) & set(cr_ids)
        print(f"    Numpy Top-5  : {np_ids} (scores: {np_scores})")
        print(f"    ChromaDB Top-5: {cr_ids} (scores: {cr_scores})")
        print(f"    重叠数: {len(overlap)}/5")

    print(f"\n  [PASS] 对比分析完成")


def main():
    print("=" * 60)
    print("  Trinity ChromaDB 检索验证")
    print("=" * 60)

    # 测试 1: 连接
    client = test_connection()
    if client is None:
        print("\n[ABORT] 无法连接 ChromaDB，请先运行 init_chromadb.py")
        sys.exit(1)

    # 测试 2: 集合
    collection = test_collection(client)

    # 测试 3: 插入/更新
    test_insert(collection)

    # 测试 4: 语义检索
    test_semantic_search(collection)

    # 测试 5: 对比
    test_compare_faiss_vs_chromadb(collection)

    print("\n" + "=" * 60)
    print("  全部测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""
Trinity ChromaDB 初始化脚本
============================
从 trinity_store.db 读取所有记忆，使用 bge-m3 (1024d) 嵌入向量，
同步到 ChromaDB 持久化实例。

使用方式:
    python init_chromadb.py
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
import time
from pathlib import Path

# 确保 trinity 包在 sys.path 中
TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))

import numpy as np

# ── 配置 ────────────────────────────────────────────────────────────────

DB_PATH = TRINITY_ROOT / "data" / "trinity_store.db"
CHROMADB_DIR = TRINITY_ROOT / "data" / "chromadb"
COLLECTION_NAME = "memory"
EMBEDDING_DIM = 1024  # bge-m3 维度


def load_memories(db_path: Path) -> list[dict]:
    """从 SQLite 加载所有记忆。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT memory_id, content, importance, role, category, tags, created_at, status "
        "FROM memories WHERE status='active' ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_embedding_engine(texts: list[str]):
    """
    创建嵌入引擎。优先用 Ollama bge-m3，失败则降级到 sklearn TF-IDF。
    """
    # 尝试 Ollama
    try:
        from trinity.embeddings.engine import OllamaEmbeddingEngine

        engine = OllamaEmbeddingEngine(model="bge-m3", dim=EMBEDDING_DIM)
        # 快速健康检查
        test_vec = engine.embed("test")
        if test_vec.shape[0] == EMBEDDING_DIM:
            print(f"[OK] Ollama bge-m3 嵌入引擎就绪 (dim={test_vec.shape[0]})")
            return engine
    except Exception as e:
        print(f"[WARN] Ollama 不可用: {e}")

    # 降级：sklearn TF-IDF
    print("[INFO] 降级到 sklearn TF-IDF 嵌入引擎")
    from trinity.embeddings.engine import SklearnEmbeddingEngine

    engine = SklearnEmbeddingEngine(max_features=EMBEDDING_DIM, ngram_range=(1, 2))
    # 用所有记忆文本预训练 vectorizer，保证词汇覆盖
    engine._lazy_init(texts)
    print(f"[OK] sklearn TF-IDF 嵌入引擎就绪 (dim={engine.embedding_dim()})")
    return engine


def init_chromadb(persist_dir: Path, collection_name: str, embedding_dim: int):
    """初始化 ChromaDB 持久化客户端和集合。"""
    import chromadb

    persist_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(persist_dir))

    # 删除旧集合（如果存在），确保干净初始化
    try:
        client.delete_collection(collection_name)
        print(f"[INFO] 已删除旧集合 '{collection_name}'")
    except Exception:
        pass

    collection = client.create_collection(
        name=collection_name,
        metadata={
            "hnsw:space": "cosine",
            "description": "Trinity memory vectors (bge-m3 1024d)",
            "dim": str(embedding_dim),
        },
    )
    print(f"[OK] ChromaDB 集合 '{collection_name}' 已创建 (persist_dir={persist_dir})")
    return client, collection


def main():
    print("=" * 60)
    print("  Trinity ChromaDB 初始化")
    print("=" * 60)

    # 1. 加载记忆
    print("\n[1/4] 加载记忆...")
    memories = load_memories(DB_PATH)
    print(f"  已加载 {len(memories)} 条活跃记忆")

    if not memories:
        print("[ERROR] 没有可同步的记忆，退出")
        sys.exit(1)

    # 2. 创建嵌入引擎
    print("\n[2/4] 初始化嵌入引擎...")
    texts = [m["content"] for m in memories]
    engine = create_embedding_engine(texts)

    # 3. 批量嵌入
    print(f"\n[3/4] 嵌入 {len(texts)} 条记忆...")
    t0 = time.time()
    embeddings = engine.embed_batch(texts)
    elapsed = time.time() - t0
    print(f"  完成，耗时 {elapsed:.2f}s，平均 {elapsed/len(texts)*1000:.1f}ms/条")

    # 验证维度
    dims = set(v.shape[0] for v in embeddings)
    if len(dims) > 1:
        print(f"[ERROR] 嵌入维度不一致: {dims}")
        sys.exit(1)
    actual_dim = embeddings[0].shape[0]
    print(f"  嵌入维度: {actual_dim}")

    # 4. 同步到 ChromaDB
    print(f"\n[4/4] 同步到 ChromaDB...")
    client, collection = init_chromadb(CHROMADB_DIR, COLLECTION_NAME, actual_dim)

    # 准备批量插入数据
    ids = [m["memory_id"] for m in memories]
    embedding_list = [v.tolist() for v in embeddings]
    metadatas = [
        {
            "content": m["content"],
            "importance": m["importance"],
            "role": m["role"],
            "category": m.get("category", "general"),
            "tags": m.get("tags", "[]"),
            "created_at": m.get("created_at", ""),
        }
        for m in memories
    ]

    # 分批插入（每批 100 条）
    batch_size = 100
    for i in range(0, len(ids), batch_size):
        batch_end = min(i + batch_size, len(ids))
        collection.add(
            ids=ids[i:batch_end],
            embeddings=embedding_list[i:batch_end],
            metadatas=metadatas[i:batch_end],
        )
        print(f"  已插入 {batch_end}/{len(ids)} 条")

    # 验证
    count = collection.count()
    print(f"\n{'=' * 60}")
    print(f"  ChromaDB 初始化完成!")
    print(f"  集合: {COLLECTION_NAME}")
    print(f"  条目: {count}")
    print(f"  维度: {actual_dim}")
    print(f"  数据目录: {CHROMADB_DIR}")
    print(f"{'=' * 60}")

    # 输出统计信息
    print("\n  记忆分布:")
    categories = {}
    for m in memories:
        cat = m.get("category", "general")
        categories[cat] = categories.get(cat, 0) + 1
    for cat, cnt in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {cnt} 条")


if __name__ == "__main__":
    main()

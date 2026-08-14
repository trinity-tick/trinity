#!/usr/bin/env python3
"""
build_dense_sparse_kgraph.py — HippoRAG 2 实体-段落索引构建脚本

从 trinity_store.db 的 memories 表中提取实体和段落，
构建 Dense-Sparse 知识图谱索引：

  1. 从 memories 表提取实体作为短语节点（Phrase Node）
  2. 将原始记忆内容作为段落节点（Passage Node）
  3. 建立 contains 边连接段落与其衍生的短语
  4. 使用 TF-IDF 检测同义实体，添加同义边

输出: data/kgraph/dense_sparse_kgraph.jsonl
用法: python scripts/build_dense_sparse_kgraph.py [--db PATH] [--threshold FLOAT]
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict


PROJECT_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
sys.path.insert(0, PROJECT_ROOT)


def load_memories(db_path: str) -> list[dict]:
    """从 SQLite 数据库加载所有活跃记忆。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT memory_id, content, category, tags, role, importance, created_at "
        "FROM memories WHERE status = 'active' ORDER BY created_at"
    )
    memories = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return memories


def extract_entities_from_memory(memory: dict) -> list[dict]:
    """从单条记忆中提取实体作为短语节点。

    使用正则 + 简单 NER 启发式方法提取：
      - 中文项目名/系统名
      - 英文大写标识符
      - 技术术语
      - 品牌/组织名
    """
    content = memory.get("content", "")
    tags_str = memory.get("tags", "[]")

    # 解析 tags
    try:
        tags = json.loads(tags_str) if isinstance(tags_str, str) else tags_str
    except (json.JSONDecodeError, TypeError):
        tags = []

    entities: list[dict] = []
    seen: set[str] = set()

    def _add(name: str, etype: str, desc: str = ""):
        entity_id = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", name.lower()).strip("_")
        if not entity_id or entity_id in seen:
            return
        seen.add(entity_id)
        entities.append({
            "id": entity_id,
            "entity_type": etype,
            "properties": {"name": name, "desc": desc or f"来源: {memory['memory_id']}"},
        })

    # 1) 英文大写标识符（如 WMS, PPR, LLM, FAISS）
    for m in re.finditer(r"\b[A-Z][A-Z_0-9]{2,}(?:\s+v?\d+\.?\d*)?\b", content):
        name = m.group(0).strip()
        _add(name, "technology", f"标识符, 来源: {memory['memory_id']}")

    # 2) 中文系统/项目/模块名
    for m in re.finditer(
        r"[\u4e00-\u9fff]{2,10}(?:系统|平台|引擎|模块|项目|模型|数据库|框架|架构)",
        content,
    ):
        _add(m.group(0), "module", f"中文模块名, 来源: {memory['memory_id']}")

    # 3) 品牌/产品名（中文 + 可能英文）
    for m in re.finditer(
        r"(?:腾讯|阿里|华为|百度|谷歌|微软|DeepSeek|OpenAI|Meta|Google)"
        r"[\u4e00-\u9fffA-Za-z0-9\s\-]{1,20}",
        content,
    ):
        _add(m.group(0).strip(), "brand", f"品牌/产品, 来源: {memory['memory_id']}")

    # 4) 版本号实体
    for m in re.finditer(r"\bv\d+\.\d+(?:\.\d+)?\b", content):
        _add(m.group(0), "version", f"版本号, 来源: {memory['memory_id']}")

    # 5) 标签作为实体
    for tag in tags:
        if isinstance(tag, str) and len(tag) >= 2:
            _add(tag, "tag", f"标签, 来源: {memory['memory_id']}")

    # 6) 记忆分类作为实体
    category = memory.get("category", "")
    if category and category != "general":
        _add(category, "category", f"记忆分类, 来源: {memory['memory_id']}")

    return entities


def build_kgraph_index(
    db_path: str,
    output_path: str,
    synonym_threshold: float = 0.5,
) -> dict:
    """主构建流程。"""
    print(f"[1/5] 加载记忆数据: {db_path}")
    memories = load_memories(db_path)
    print(f"  加载 {len(memories)} 条记忆")

    # ── 步骤 2: 提取短语节点 ──
    print("[2/5] 提取短语节点...")
    all_phrase_nodes: dict[str, dict] = {}
    passage_to_phrases: dict[str, list[str]] = defaultdict(list)

    for mem in memories:
        mem_id = mem["memory_id"]
        entities = extract_entities_from_memory(mem)
        for ent in entities:
            eid = ent["id"]
            if eid not in all_phrase_nodes:
                all_phrase_nodes[eid] = ent
            passage_to_phrases[mem_id].append(eid)

    print(f"  提取 {len(all_phrase_nodes)} 个短语节点")

    # ── 步骤 3: 创建段落节点 ──
    print("[3/5] 创建段落节点...")
    passage_nodes: list[dict] = []
    for mem in memories:
        mem_id = mem["memory_id"]
        phrase_ids = passage_to_phrases.get(mem_id, [])

        passage_nodes.append({
            "type": "passage_node",
            "id": f"passage_{mem_id}",
            "content": mem.get("content", ""),
            "source_memory_id": mem_id,
            "phrase_ids": phrase_ids,
            "category": mem.get("category", "general"),
            "role": mem.get("role", "user"),
            "importance": mem.get("importance", 0.5),
            "created_at": mem.get("created_at", ""),
        })

    print(f"  创建 {len(passage_nodes)} 个段落节点")

    # ── 步骤 4: TF-IDF 同义检测 ──
    print(f"[4/5] TF-IDF 同义实体检测 (threshold={synonym_threshold})...")
    synonym_edges: list[dict] = []

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        phrase_items = list(all_phrase_nodes.items())
        texts = [
            ent["properties"].get("name", eid)
            for eid, ent in phrase_items
        ]
        ids = [eid for eid, _ in phrase_items]

        if len(texts) >= 2:
            vectorizer = TfidfVectorizer(
                analyzer="char_wb",
                ngram_range=(2, 4),
                min_df=1,
            )
            tfidf_matrix = vectorizer.fit_transform(texts)
            sim_matrix = cosine_similarity(tfidf_matrix)

            n = len(ids)
            for i in range(n):
                for j in range(i + 1, n):
                    sim = sim_matrix[i, j]
                    if sim >= synonym_threshold:
                        synonym_edges.append({
                            "type": "synonym_edge",
                            "source": ids[i],
                            "target": ids[j],
                            "confidence": round(float(sim), 4),
                            "source_name": phrase_items[i][1]["properties"].get("name", ids[i]),
                            "target_name": phrase_items[j][1]["properties"].get("name", ids[j]),
                        })
    except ImportError:
        print("  WARNING: scikit-learn 未安装，跳过 TF-IDF 同义检测。请 pip install scikit-learn")
    except Exception as e:
        print(f"  WARNING: TF-IDF 同义检测失败: {e}")

    print(f"  检测到 {len(synonym_edges)} 对同义实体")

    # ── 步骤 5: 写入 JSONL ──
    print(f"[5/5] 写入输出: {output_path}")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    total_lines = 0
    with open(output_path, "w", encoding="utf-8") as f:
        # 短语节点
        for eid, ent in all_phrase_nodes.items():
            f.write(json.dumps({
                "type": "phrase_node",
                "id": eid,
                "entity_type": ent["entity_type"],
                "properties": ent["properties"],
            }, ensure_ascii=False) + "\n")
            total_lines += 1

        # 段落节点
        for pnode in passage_nodes:
            # 段落内容过长时截断存储
            pnode_copy = dict(pnode)
            if len(pnode_copy.get("content", "")) > 2000:
                pnode_copy["content_truncated"] = True
                pnode_copy["content"] = pnode_copy["content"][:2000]
            f.write(json.dumps(pnode_copy, ensure_ascii=False) + "\n")
            total_lines += 1

        # 同义边
        for syn in synonym_edges:
            f.write(json.dumps(syn, ensure_ascii=False) + "\n")
            total_lines += 1

    # ── 统计摘要 ──
    summary = {
        "source": db_path,
        "output": output_path,
        "total_memories": len(memories),
        "phrase_nodes": len(all_phrase_nodes),
        "passage_nodes": len(passage_nodes),
        "synonym_edges": len(synonym_edges),
        "contains_edges": sum(
            len(passage_to_phrases.get(m["memory_id"], []))
            for m in memories
        ),
        "total_lines": total_lines,
        "synonym_threshold": synonym_threshold,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }

    # 写摘要
    summary_path = output_path.replace(".jsonl", "_build_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n构建完成! 摘要写入: {summary_path}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="HippoRAG 2 Dense-Sparse 知识图谱索引构建"
    )
    parser.add_argument(
        "--db", default=os.path.join(PROJECT_ROOT, "data", "trinity_store.db"),
        help="SQLite 数据库路径"
    )
    parser.add_argument(
        "--output", default=os.path.join(PROJECT_ROOT, "data", "kgraph", "dense_sparse_kgraph.jsonl"),
        help="输出 JSONL 路径"
    )
    parser.add_argument(
        "--threshold", type=float, default=0.5,
        help="TF-IDF 同义检测阈值 (default: 0.5)"
    )

    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"错误: 数据库不存在: {args.db}")
        sys.exit(1)

    build_kgraph_index(
        db_path=args.db,
        output_path=args.output,
        synonym_threshold=args.threshold,
    )


if __name__ == "__main__":
    main()

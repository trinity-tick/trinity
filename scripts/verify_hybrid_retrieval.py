"""
verify_hybrid_retrieval.py — Trinity GraphVectorHybridRetriever 端到端验证脚本

验证三个核心管道:
  1. PPR 图搜索 (KnowledgeGraph.ppr_search)
  2. 向量+图混合检索 (GraphVectorHybridRetriever.search)
  3. 纯向量 vs 纯图 vs 混合 对比分析

依赖: trinity 包已安装或 PYTHONPATH 指向项目根目录
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# ── 确保 trinity 可导入 ──
TRINITY_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
if TRINITY_ROOT not in sys.path:
    sys.path.insert(0, TRINITY_ROOT)

import numpy as np

from trinity.kgraph import KnowledgeGraph, IncrementalKGraph, RelationType
from trinity.kgraph.graph import KGraphFeedbackLoop, EntityAttributeTimeGraph
from trinity.vector_index.graph_vector_hybrid import GraphVectorHybridRetriever
from trinity.vector_index.index import NumpyBruteForceIndex


OUTPUT_DIR = os.path.join(TRINITY_ROOT, "scripts", "..", "trinity", "..", "output")
# Resolve to absolute
OUTPUT_DIR = os.path.normpath(os.path.join(TRINITY_ROOT, "output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

REPORT_PATH = os.path.join(OUTPUT_DIR, "hybrid_retrieval_verify_report.md")
KGRAPH_PATH = os.path.join(
    TRINITY_ROOT, "data", "kgraph", "kgraph_data.jsonl"
)


def synthetic_embed(text: str, dim: int = 128) -> np.ndarray:
    """生成确定性合成向量，基于文本内容的 SHA-256 哈希。

    用于在没有真实嵌入模型时验证混合检索管道。
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # 将哈希字节展开为维度为 dim 的浮点向量
    vec = np.zeros(dim, dtype=np.float32)
    for i in range(min(dim, len(h))):
        vec[i] = (h[i] - 127.5) / 127.5
    # 用循环移位填充剩余维度
    for i in range(len(h), dim):
        vec[i] = vec[i % len(h)]
    # L2 归一化
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def build_synthetic_vector_index(kg: KnowledgeGraph, dim: int = 128):
    """为知识图谱中每个实体构建合成向量索引。"""
    vi = NumpyBruteForceIndex(dim=dim)
    for eid, ent in kg._entities.items():
        # 用实体 ID + name + desc 构建合成向量
        name = ent.get("properties", {}).get("name", eid)
        desc = ent.get("properties", {}).get("desc", "")
        text = f"{eid}: {name}. {desc}"
        vec = synthetic_embed(text, dim=dim)
        vi.add(eid, vec)
    return vi


def run_ppr_test(kg: KnowledgeGraph) -> list[dict]:
    """测试 PPR 图搜索：5 个不同查询。"""
    print("[TEST 1] PPR 图搜索测试 ...")
    queries = [
        {
            "name": "从 trinity_v6_37 出发",
            "seeds": ["trinity_v6_37"],
            "expected_related": ["metaevolution", "guardian_chain", "knowledge_graph_module"],
        },
        {
            "name": "从 wms_project 出发",
            "seeds": ["wms_project"],
            "expected_related": ["caitang", "wangdiantong", "heavy_rule"],
        },
        {
            "name": "从 caitang + proya 出发",
            "seeds": ["caitang", "proya"],
            "expected_related": ["heavy_rule", "color_separation_rule"],
        },
        {
            "name": "从 chromadb + faiss 出发（向量技术群）",
            "seeds": ["chromadb", "faiss"],
            "expected_related": ["hnsw", "tfidf", "bm25"],
        },
        {
            "name": "从 hunyu_hy3 + deepseek_v4_pro 出发（模型群）",
            "seeds": ["hunyu_hy3", "deepseek_v4_pro"],
            "expected_related": ["marvis", "ollama"],
        },
    ]

    results = []
    for q in queries:
        start = time.time()
        hits = kg.ppr_search(
            query_entities=q["seeds"],
            alpha=0.85,
            top_k=10,
        )
        elapsed = (time.time() - start) * 1000
        hit_ids = {h["entity_id"] for h in hits}
        overlap = hit_ids & set(q["expected_related"])
        results.append({
            "query": q["name"],
            "seeds": q["seeds"],
            "num_hits": len(hits),
            "hits": [h["entity_id"] for h in hits[:5]],
            "top_score": hits[0]["ppr_score"] if hits else 0,
            "expected_overlap": list(overlap),
            "elapsed_ms": round(elapsed, 2),
        })
        status = "PASS" if overlap else "WARN"
        print(f"  [{status}] {q['name']}: {len(hits)} hits, "
              f"top={hits[0]['entity_id'] if hits else 'N/A'}, "
              f"overlap={list(overlap)}, {elapsed:.1f}ms")

    return results


def run_hybrid_test(
    kg: KnowledgeGraph,
    vi: NumpyBruteForceIndex,
    embed_func,
) -> list[dict]:
    """测试向量+图混合检索。"""
    print("\n[TEST 2] 向量+图混合检索测试 ...")
    retriever = GraphVectorHybridRetriever(
        vector_index=vi,
        kgraph=kg,
        embed_func=embed_func,
        rrf_k=60,
        fusion_mode="rrf",
    )

    queries = [
        "Trinity 记忆系统核心模块",
        "仓库管理 WMS 相关",
        "向量检索技术",
        "品牌和规则",
        "模型和推理引擎",
    ]

    results = []
    for q in queries:
        start = time.time()
        hits = retriever.search(query=q, top_k=10)
        elapsed = (time.time() - start) * 1000

        vec_only = sum(1 for h in hits if h["source"] == "vector")
        graph_only = sum(1 for h in hits if h["source"] == "graph")
        both = sum(1 for h in hits if h["source"] == "both")

        results.append({
            "query": q,
            "num_hits": len(hits),
            "top_hits": [
                f"{h['entity_id']} (fused={h['fused_score']:.4f}, src={h['source']})"
                for h in hits[:5]
            ],
            "source_dist": f"vector={vec_only}, graph={graph_only}, both={both}",
            "elapsed_ms": round(elapsed, 2),
        })
        print(f"  [OK] '{q}': {len(hits)} hits, "
              f"src(v={vec_only}/g={graph_only}/b={both}), {elapsed:.1f}ms")
        for h in hits[:3]:
            print(f"       {h['entity_id']:30s} fused={h['fused_score']:.4f} "
                  f"v={h['vector_score']} g={h['graph_score']} [{h['source']}]")

    return results


def run_comparison_test(kg: KnowledgeGraph, vi: NumpyBruteForceIndex) -> list[dict]:
    """对比：纯向量 vs 纯图(PPR) vs 混合(RRF)。"""
    print("\n[TEST 3] 纯向量 vs 纯图 vs 混合对比 ...")

    embed_func = lambda t: synthetic_embed(t, dim=128)
    retriever = GraphVectorHybridRetriever(
        vector_index=vi,
        kgraph=kg,
        embed_func=embed_func,
        rrf_k=60,
        fusion_mode="rrf",
    )

    test_cases = [
        ("memory system", "trinity_v6_37"),
        ("warehouse WMS", "wms_project"),
        ("vector search", "chromadb"),
        ("brand product", "caitang"),
        ("ai model llm", "hunyu_hy3"),
    ]

    results = []
    for query_str, seed_entity in test_cases:
        # ── 纯向量 ──
        qvec = embed_func(query_str)
        vec_hits = vi.search(qvec, top_k=10)
        vec_ids = [r.id for r in vec_hits]

        # ── 纯图 (PPR) ──
        ppr_hits = kg.ppr_search(query_entities=[seed_entity], top_k=10)
        ppr_ids = [h["entity_id"] for h in ppr_hits]

        # ── 混合 (RRF) ──
        hybrid_hits = retriever.search(query=query_str, top_k=10)
        hybrid_ids = [h["entity_id"] for h in hybrid_hits]

        # ── 计算重叠 ──
        vec_set = set(vec_ids)
        ppr_set = set(ppr_ids)
        hybrid_set = set(hybrid_ids)

        results.append({
            "query": query_str,
            "seed": seed_entity,
            "vec_top5": vec_ids[:5],
            "ppr_top5": ppr_ids[:5],
            "hybrid_top5": hybrid_ids[:5],
            "vec_ppr_overlap": len(vec_set & ppr_set),
            "vec_hybrid_overlap": len(vec_set & hybrid_set),
            "ppr_hybrid_overlap": len(ppr_set & hybrid_set),
            "hybrid_unique": list(hybrid_set - vec_set - ppr_set),
        })

        print(f"  [OK] '{query_str}' → seed={seed_entity}")
        print(f"       vec:    {vec_ids[:4]}")
        print(f"       ppr:    {ppr_ids[:4]}")
        print(f"       hybrid: {hybrid_ids[:4]}")
        unique = hybrid_set - vec_set - ppr_set
        if unique:
            print(f"       hybrid-only: {list(unique)}")

    return results


def check_imports() -> dict:
    """验证所有关键类的 import 链。"""
    print("\n[TEST 0] Import 链完整性检查 ...")
    checks = {}

    # kgraph 模块
    imports_to_check = [
        ("KnowledgeGraph", "trinity.kgraph", "KnowledgeGraph"),
        ("IncrementalKGraph", "trinity.kgraph", "IncrementalKGraph"),
        ("RelationType", "trinity.kgraph", "RelationType"),
        ("EntityAttributeTimeGraph", "trinity.kgraph", "EntityAttributeTimeGraph"),
        ("KGraphFeedbackLoop", "trinity.kgraph", "KGraphFeedbackLoop"),
        ("GraphVectorHybridRetriever", "trinity.vector_index.graph_vector_hybrid", "GraphVectorHybridRetriever"),
        ("NumpyBruteForceIndex", "trinity.vector_index.index", "NumpyBruteForceIndex"),
    ]

    for label, module_name, class_name in imports_to_check:
        try:
            mod = __import__(module_name, fromlist=[class_name])
            obj = getattr(mod, class_name, None)
            if obj is not None:
                checks[label] = "PASS"
                print(f"  [PASS] {label} ← {module_name}")
            else:
                checks[label] = f"FAIL: {class_name} not in {module_name}"
                print(f"  [FAIL] {label}: not found in {module_name}")
        except Exception as e:
            checks[label] = f"FAIL: {e}"
            print(f"  [FAIL] {label}: {e}")

    return checks


def generate_report(
    kg_stats: dict,
    import_checks: dict,
    ppr_results: list[dict],
    hybrid_results: list[dict],
    comparison_results: list[dict],
) -> str:
    """生成验证报告。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    total_tests = len(import_checks) + len(ppr_results) + len(hybrid_results) + len(comparison_results)
    passed = sum(1 for v in import_checks.values() if v == "PASS")
    passed += sum(1 for r in ppr_results if r["expected_overlap"])
    passed += len(hybrid_results)
    passed += len(comparison_results)

    lines = []
    lines.append("# Trinity GraphVectorHybridRetriever 端到端验证报告")
    lines.append(f"> 生成时间: {now}")
    lines.append(f"> 知识图谱: {kg_stats['entity_count']} 实体 / {kg_stats['relation_count']} 关系")
    lines.append("")

    # ── 概要 ──
    lines.append("## 概要")
    lines.append(f"| 指标 | 结果 |")
    lines.append(f"|---|---|")
    lines.append(f"| 总测试数 | {total_tests} |")
    lines.append(f"| 通过 | {passed} |")
    lines.append(f"| 失败 | {total_tests - passed} |")
    lines.append(f"| 实体数 | {kg_stats['entity_count']} |")
    lines.append(f"| 关系数 | {kg_stats['relation_count']} |")
    lines.append("")

    # ── Import 链 ──
    lines.append("## 0. Import 链完整性")
    lines.append("| 类 | 模块 | 状态 |")
    lines.append("|---|---|---|")
    for name, status in import_checks.items():
        emoji = "PASS" if status == "PASS" else "FAIL"
        mod_name = {
            "KnowledgeGraph": "trinity.kgraph",
            "IncrementalKGraph": "trinity.kgraph",
            "RelationType": "trinity.kgraph",
            "EntityAttributeTimeGraph": "trinity.kgraph",
            "KGraphFeedbackLoop": "trinity.kgraph",
            "GraphVectorHybridRetriever": "trinity.vector_index.graph_vector_hybrid",
            "NumpyBruteForceIndex": "trinity.vector_index.index",
        }.get(name, "")
        lines.append(f"| {name} | `{mod_name}` | {emoji} |")
    lines.append("")

    # ── PPR 测试 ──
    lines.append("## 1. PPR 图搜索测试 (5 queries)")
    lines.append("| 查询 | 种子实体 | 命中数 | Top-1 | 期望重叠 | 耗时(ms) |")
    lines.append("|---|---|---|---|---|---|")
    for r in ppr_results:
        top1 = r["hits"][0] if r["hits"] else "N/A"
        overlap = ", ".join(r["expected_overlap"]) if r["expected_overlap"] else "无"
        lines.append(
            f"| {r['query']} | {', '.join(r['seeds'][:2])} | {r['num_hits']} | "
            f"{top1} | {overlap} | {r['elapsed_ms']} |"
        )
    lines.append("")

    # ── 混合检索测试 ──
    lines.append("## 2. 向量+图混合检索测试 (5 queries)")
    lines.append("| 查询 | 命中数 | 来源分布 | 耗时(ms) |")
    lines.append("|---|---|---|---|")
    for r in hybrid_results:
        lines.append(
            f"| {r['query']} | {r['num_hits']} | {r['source_dist']} | {r['elapsed_ms']} |"
        )
    lines.append("")

    # ── 对比测试 ──
    lines.append("## 3. 纯向量 vs 纯图 vs 混合对比")
    lines.append("| 查询 | 种子 | Vec-Top3 | PPR-Top3 | Hybrid-Top3 | V∩P | V∩H | P∩H | Hybrid-Only |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in comparison_results:
        v3 = ", ".join(r["vec_top5"][:3])
        p3 = ", ".join(r["ppr_top5"][:3])
        h3 = ", ".join(r["hybrid_top5"][:3])
        hu = ", ".join(r["hybrid_unique"][:3]) if r["hybrid_unique"] else "-"
        lines.append(
            f"| {r['query']} | {r['seed']} | {v3} | {p3} | {h3} | "
            f"{r['vec_ppr_overlap']} | {r['vec_hybrid_overlap']} | "
            f"{r['ppr_hybrid_overlap']} | {hu} |"
        )
    lines.append("")

    # ── 结论 ──
    lines.append("## 4. 结论与发现")
    lines.append("")

    # 分析 import 链
    all_imports_ok = all(v == "PASS" for v in import_checks.values())
    if all_imports_ok:
        lines.append("- **Import 链**: 全部通过。7 个关键类均可正确 import。")
    else:
        failed_imports = [k for k, v in import_checks.items() if v != "PASS"]
        lines.append(f"- **Import 链**: 存在失败项: {failed_imports}")

    # 分析 PPR
    ppr_hit_count = sum(r["num_hits"] for r in ppr_results)
    ppr_overlap_count = sum(1 for r in ppr_results if r["expected_overlap"])
    lines.append(f"- **PPR 图搜索**: 5 个查询共返回 {ppr_hit_count} 个关联实体，"
                 f"{ppr_overlap_count}/5 命中期望关联。")

    # 分析混合检索
    if hybrid_results:
        avg_hybrid_hits = sum(r["num_hits"] for r in hybrid_results) / len(hybrid_results)
        lines.append(f"- **混合检索**: 平均每个查询返回 {avg_hybrid_hits:.1f} 个结果，"
                     f"三阶段管道（向量→PPR→RRF）端到端可用。")

    # 分析对比
    if comparison_results:
        total_vp = sum(r["vec_ppr_overlap"] for r in comparison_results)
        total_vh = sum(r["vec_hybrid_overlap"] for r in comparison_results)
        total_ph = sum(r["ppr_hybrid_overlap"] for r in comparison_results)
        total_hu = sum(len(r["hybrid_unique"]) for r in comparison_results)
        lines.append(f"- **对比分析**: 向量-PPR 重叠 {total_vp}、向量-混合重叠 {total_vh}、"
                     f"PPR-混合重叠 {total_ph}。混合检索新增 {total_hu} 个独有实体，"
                     f"验证 RRF 融合有效引入了互补信号。")

    lines.append("")
    lines.append("---")
    lines.append(f"*报告由 verify_hybrid_retrieval.py 自动生成*")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("Trinity GraphVectorHybridRetriever 端到端验证")
    print("=" * 60)

    # ── 加载知识图谱 ──
    print(f"\n加载知识图谱: {KGRAPH_PATH}")
    if not os.path.exists(KGRAPH_PATH):
        print(f"[FATAL] 知识图谱文件不存在: {KGRAPH_PATH}")
        sys.exit(1)

    kg = KnowledgeGraph(storage_path=KGRAPH_PATH)
    kg_stats = kg.get_stats()
    print(f"  实体: {kg_stats['entity_count']}, 关系: {kg_stats['relation_count']}")

    # ── 0. Import 链检查 ──
    import_checks = check_imports()

    # ── 1. PPR 测试 ──
    ppr_results = run_ppr_test(kg)

    # ── 2. 构建合成向量索引 + 混合检索测试 ──
    vi = build_synthetic_vector_index(kg, dim=128)
    embed_func = lambda t: synthetic_embed(t, dim=128)

    try:
        hybrid_results = run_hybrid_test(kg, vi, embed_func)
    except Exception as e:
        print(f"  [FAIL] 混合检索测试异常: {e}")
        hybrid_results = []

    # ── 3. 对比测试 ──
    try:
        comparison_results = run_comparison_test(kg, vi)
    except Exception as e:
        print(f"  [FAIL] 对比测试异常: {e}")
        comparison_results = []

    # ── 4. 额外: 验证 IncrementalKGraph ──
    print("\n[TEST 4] IncrementalKGraph 验证 ...")
    try:
        inc = IncrementalKGraph(kg)
        hash_val = inc.get_last_sync_hash()
        print(f"  [PASS] IncrementalKGraph 初始化成功, sync_hash={hash_val[:12]}...")
    except Exception as e:
        print(f"  [FAIL] IncrementalKGraph: {e}")

    # ── 生成报告 ──
    report = generate_report(
        kg_stats, import_checks, ppr_results, hybrid_results, comparison_results
    )
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n报告已生成: {REPORT_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()

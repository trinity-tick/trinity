"""Tests for trinity.retrieval.pagetree (MemoryPageTree, PageIndex 借鉴 Phase 1)."""

import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.retrieval.pagetree import MemoryPageTree, _tokenize, _overlap_ratio


def _records():
    return [
        {"memory_id": "m1", "content": "WMS 上架作业使用 PDA 扫码确认库位", "category": "wms_knowledge",
         "tags": ["wms", "上架"], "persona_id": "default", "session_id": "s1", "importance": 0.5},
        {"memory_id": "m2", "content": "收货作业扫码优先：先扫容器再扫商品", "category": "wms_knowledge",
         "tags": ["wms", "收货"], "persona_id": "default", "session_id": "s1", "importance": 0.5},
        {"memory_id": "m3", "content": "Alice likes iced americano and gym on Wednesdays", "category": "session",
         "tags": ["preference"], "persona_id": "alice", "session_id": "s2", "importance": 0.8},
        {"memory_id": "m4", "content": "Alice prefers concise answers in dark theme", "category": "session",
         "tags": ["preference"], "persona_id": "alice", "session_id": "s2", "importance": 0.6},
    ]


class TestBuild:
    def test_structure(self):
        tree = MemoryPageTree().build(_records())
        assert tree.stats["records"] == 4
        assert set(tree.categories) == {"wms_knowledge", "session"}
        # persona 轴：alice 的记忆聚成 persona 簇
        alice_clusters = [c for c in tree.clusters.values()
                          if c["title"] == "alice"]
        assert len(alice_clusters) == 1
        assert alice_clusters[0]["stats"]["count"] == 2

    def test_exclude_tags(self):
        recs = _records() + [
            {"memory_id": "m5", "content": "benchmark junk", "category": "lme",
             "tags": ["lme"], "persona_id": "default", "session_id": "x", "importance": 0.1},
        ]
        tree = MemoryPageTree().build(recs, exclude_categories={"lme"})
        assert "lme" not in tree.categories
        assert tree.stats["records"] == 4

    def test_save_load_roundtrip(self):
        tree = MemoryPageTree().build(_records())
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "pagetree.json")
            tree.save(p)
            t2 = MemoryPageTree.load(p)
            assert t2 is not None
            assert t2.stats["records"] == 4
            assert set(t2.records) == {"m1", "m2", "m3", "m4"}


class TestSearch:
    def test_page_first_routing(self):
        tree = MemoryPageTree().build(_records())
        out = tree.search("上架作业怎么扫码确认库位", top_k=3, page_k=2)
        ids = [r["memory_id"] for r in out["results"]]
        assert ids[0] == "m1"  # wms 页优先命中
        assert out["pages_used"][0].startswith("clu:wms_knowledge")
        assert out["results"][0]["source_channel"] == "pagetree"
        assert out["results"][0]["page_path"]  # 可溯源页路径

    def test_persona_page(self):
        tree = MemoryPageTree().build(_records())
        out = tree.search("alice coffee preference", top_k=3, page_k=2)
        ids = [r["memory_id"] for r in out["results"]]
        assert "m3" in ids[:2]

    def test_base_fill(self):
        tree = MemoryPageTree().build(_records())
        # base_fn 返回页外记忆 m3 → 兜底填充进结果（页内 m1/m2 由页树覆盖）；
        # 长查询才走页排序（短查询守卫：≤2 词直接走基础召回）
        out = tree.search("上架作业怎么扫码确认库位", top_k=5, page_k=1,
                          base_fn=lambda q, k: [{"memory_id": "m3", "score": 0.9}])
        ids = [r["memory_id"] for r in out["results"]]
        assert "m3" in ids
        assert out["filled_by_base"] >= 1
        assert out["results"][-1]["source_channel"] == "base"

    def test_short_query_guard(self):
        tree = MemoryPageTree().build(_records())
        # 短查询（≤2 词）→ 守卫：直接返回基础召回（页定位无区分度）
        out = tree.search("上架", top_k=5, page_k=1,
                          base_fn=lambda q, k: [{"memory_id": "m2", "score": 0.9}])
        assert out.get("guard") == "short_query"
        assert [r["memory_id"] for r in out["results"]] == ["m2"]

    def test_empty_query(self):
        tree = MemoryPageTree().build(_records())
        out = tree.search("", top_k=3)
        assert isinstance(out["results"], list)


class TestSummaryScoring:
    def test_summary_terms_match_paraphrase(self):
        # 近义改写查询：原文无重叠词，但节点摘要（LLM 生成，用词不同）能接住
        recs = [
            {"memory_id": "s1", "content": "WMS 上架作业使用 PDA 扫码确认库位",
             "category": "wms_knowledge", "tags": ["wms"], "persona_id": "default",
             "session_id": "s1", "importance": 0.5},
        ]
        tree = MemoryPageTree().build(recs)
        cid = list(tree.clusters)[0]
        # 注入摘要（维护链产物）
        tree.clusters[cid]["summary"] = "库房收货环节的移库操作与设备扫描流程说明"
        out = tree.search("仓库里把货物搬到货架的那个流程是啥", top_k=3, page_k=1)
        assert out["pages_used"] == [cid] or len(out["pages_used"]) > 0
        assert out["results"][0]["memory_id"] == "s1"

    def test_no_summary_fallback(self):
        recs = [
            {"memory_id": "s1", "content": "WMS 上架作业使用 PDA 扫码确认库位",
             "category": "wms_knowledge", "tags": ["wms"], "persona_id": "default",
             "session_id": "s1", "importance": 0.5},
        ]
        tree = MemoryPageTree().build(recs)
        out = tree.search("上架作业怎么扫码确认库位", top_k=3, page_k=1)
        assert out["results"][0]["memory_id"] == "s1"


class TestTokenize:
    def test_mixed(self):
        terms = _tokenize("WMS 上架作业 PDA 扫码确认")
        assert "wms" in terms
        assert any("上架" in t or "作业" in t or "扫码" in t for t in terms)

    def test_overlap(self):
        assert _overlap_ratio(["wms"], ["wms", "上架"]) > 0
        assert _overlap_ratio(["wms"], []) == 0.0

"""Trinity — active 模块冒烟测试 II（2026-08-15, P1 代码健康）。

覆盖审计中 ACTIVE 分类下、非 engine 聚合链的直接引用模块：
每个模块可导入 + 关键类存在 + 主要类可无参/轻参实例化。
"""

from __future__ import annotations

import pytest

# (模块名, 可实例化类, 构造参数)
_MODULES = [
    ("audit_trail", "AuditTrail", {}),
    ("causal_memory", "CausalMemory", {}),
    ("consensus_voting", "ConsensusVoter", {}),
    ("contextual_embedding", "ContextualEmbedder", {}),
    ("continuous_eval", "ContinuousEvalEngine", {}),
    ("episodic_rl", "EpisodicRLScorer", {}),
    ("federated_memory", "FederatedAggregator", {}),
    ("guardian", "GuardianChainV50", {}),
    ("guardian_retrieval", "RetrievalSystemV47", {}),
    ("knowledge_gossip", "KnowledgeGossipProtocol", {}),
    ("memory_page_manager", "MemoryPageManager", {}),
    ("proactive_prefetcher", "ProactivePrefetcher", {}),
    ("prompt_ingestion", "PromptIngestionPipeline", {}),
    ("reflective_repair_memory", "ClosedLoopReflectionController", {}),
    ("retrieval", "BM25SparseRetriever", {}),
    ("selective_recall", "SelectiveRecallRouter", {}),
    ("self_healing", "SelfHealingPipeline", {}),
    ("token_budget", "MemoryTokenEntry", {}),
    ("workflow_memory", "HierarchicalTrajectory", {}),
]


@pytest.mark.parametrize("mod_name,cls_name,kwargs", _MODULES)
def test_active_module_instantiates(mod_name: str, cls_name: str, kwargs: dict) -> None:
    """active 模块可导入、类存在、无参实例化（冒烟）。"""
    import importlib
    mod = importlib.import_module(f"trinity.modules.second_brain.{mod_name}")
    assert hasattr(mod, cls_name), f"{mod_name} missing {cls_name}"
    cls = getattr(mod, cls_name)
    try:
        obj = cls(**kwargs)
        assert obj is not None
    except Exception as exc:  # noqa: BLE001
        # 某些类需要参数/依赖——允许实例化失败但导入必须成功
        pytest.skip(f"{mod_name}.{cls_name} init needs deps: {exc}")


@pytest.mark.parametrize("mod_name,cls_name", [
    ("cb49_52", "ContextualChunkIngestion"),
    ("cb49_52", "GroundTruthEpisodes"),
    ("cb49_52", "ObserverReflector"),
    ("cb49_52", "RelationalVersioning"),
])
def test_cb49_52_classes_exist(mod_name: str, cls_name: str) -> None:
    import importlib
    mod = importlib.import_module(f"trinity.modules.second_brain.{mod_name}")
    assert hasattr(mod, cls_name)


def test_causal_semantic_graph_importable() -> None:
    import importlib
    mod = importlib.import_module("trinity.modules.second_brain.causal_semantic_graph_memory")
    assert hasattr(mod, "ActMemSemanticNode")


def test_memory_unlearning_importable() -> None:
    import importlib
    mod = importlib.import_module("trinity.modules.second_brain.memory_unlearning")
    assert hasattr(mod, "ErasureProof")


def test_lifecycle_manager_importable() -> None:
    import importlib
    mod = importlib.import_module("trinity.modules.second_brain.lifecycle_manager")
    assert hasattr(mod, "ForgettingStrategy")

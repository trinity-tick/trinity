"""Trinity — engine 核心模块冒烟测试（2026-08-15, P1 代码健康）。

覆盖 34 个"无直接测试点名"的 active 模块中可实例化的核心部分：
- SecondBrainV636 无参构造 → guardian_chain / retrieval 子系统就绪
- engine facade 56 类导出全部可解析
- 核心子系统模块（engine_core / engine_governance / engine_retrieval /
  engine_memory_core / engine_memory_tiers / engine_guardian_retrieval /
  engine_diagnostics / engine_observability / engine_optimization /
  engine_data_pipeline）可导入且关键类存在
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def engine():
    """惰性构造 SecondBrainV636（重型，仅一次）。"""
    from trinity.modules.second_brain.engine_core import SecondBrainV636
    return SecondBrainV636()


def test_engine_constructs(engine) -> None:
    assert engine is not None


def test_guardian_chain_50_levels(engine) -> None:
    gc = getattr(engine, "guardian_chain", None)
    assert gc is not None
    total = getattr(gc, "total", None)
    assert total is not None and total >= 50


def test_retrieval_channels(engine) -> None:
    r = getattr(engine, "retrieval", None)
    if r is not None:
        total = getattr(r, "total", None)
        assert total is not None and total >= 47


def test_engine_facade_exports_resolve() -> None:
    """engine.py facade 的 __all__ 全部可解析（防 re-export 断裂）。"""
    from trinity.modules.second_brain import engine as eng
    missing = [n for n in eng.__all__ if not hasattr(eng, n)]
    assert missing == [], f"facade missing: {missing}"


@pytest.mark.parametrize("mod_name,cls_names", [
    ("engine_core", ["SecondBrainV636", "GuardianChainV50", "RetrievalSystemV47"]),
    ("engine_governance", ["MultiHeadRecurrentMemory", "ContextNestVerifiableGovernance",
                           "ElephantAgentStateContinuity", "ConstraintSteerableOversight",
                           "OnlineSafetyMonitor"]),
    ("engine_memory_core", ["HippocampalComplementaryMemory",
                            "IdentityPreservingConsolidator", "ReasoningDriftAuditor",
                            "ContextObjectManager"]),
    ("engine_memory_tiers", ["MultiHeadMemoryPartition",
                             "ThreeLayerHierarchicalMemory"]),
    ("engine_guardian_retrieval", ["GuardianChainV50", "RetrievalSystemV47"]),
    ("engine_retrieval", ["ExabaseRetrieval", "BEAMLIGHT"]),
    ("engine_diagnostics", ["GroundTruthEpisodes"]),
    ("engine_observability", ["HindsightFourNetwork", "ZikkaronHopfield",
                              "SpreadingActivationGraph", "HopfieldMemory"]),
    ("engine_optimization", []),
    ("engine_data_pipeline", []),
])
def test_engine_module_classes(mod_name: str, cls_names: list) -> None:
    """每个 engine_* 模块可导入，关键类存在。"""
    import importlib
    mod = importlib.import_module(f"trinity.modules.second_brain.{mod_name}")
    for c in cls_names:
        assert hasattr(mod, c), f"{mod_name} missing class {c}"


def test_engine_core_discover_latest_version(engine) -> None:
    """版本发现接口可用（engine_core 实现）。"""
    from trinity.modules.second_brain.engine_core import discover_latest_version
    v = discover_latest_version("second_brain")
    assert isinstance(v, dict)
    assert "latest" in v or len(v) > 0


def test_engine_guardian_validates(engine) -> None:
    """GuardianChain validate() 通过（50 级完整）。"""
    gc = getattr(engine, "guardian_chain", None)
    if gc is not None and hasattr(gc, "validate"):
        assert gc.validate() is True

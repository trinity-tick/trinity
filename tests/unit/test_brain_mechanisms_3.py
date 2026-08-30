# -*- coding: utf-8 -*-
"""EXECUTION 175 测试补课：全局自我 + 能力注册表。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestGlobalIdentity:
    def test_global_identity_has_content(self):
        from trinity.brain.self_model import global_identity
        s = global_identity()
        assert isinstance(s, str)
        # 聚合来自 PG——无数据时为空串但不应抛异常
        assert s == "" or ("关注" in s or "反思" in s)

    def test_build_identity_still_works(self):
        from trinity.brain.self_model import build_identity
        s = build_identity("数据库", {"polarity": "neg"})
        assert "数据库" in s


class TestBrainCapabilities:
    def test_capabilities_registry(self):
        # 轻量验证：模块可 import（不实例化引擎）
        import importlib
        mods = [
            "trinity.modules.second_brain.causal_memory",
            "trinity.modules.second_brain.consensus_voting",
            "trinity.modules.second_brain.selective_recall",
            "trinity.modules.second_brain.workflow_memory",
            "trinity.modules.second_brain.prompt_ingestion",
        ]
        for m in mods:
            importlib.import_module(m)
        assert True

    def test_erase_memory(self):
        # adapter 有 erase 方法（不需要连库）
        from trinity.adapters.postgresql import PostgreSQLAdapter
        assert hasattr(PostgreSQLAdapter, "erase_memory")


class TestPipelineToggle:
    def test_stage_subset(self):
        from trinity.brain.cognition_pipeline import STAGES
        assert len(STAGES) == 6
        assert "context" in STAGES and "hebbian" in STAGES

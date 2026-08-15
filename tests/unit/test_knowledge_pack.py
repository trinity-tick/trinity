"""Trinity — V2 动作 C 单元测试：知识包 + A2A 协作（2026-08-15）。

覆盖：
- knowledge_pack：打包脱敏 / 跨实例拆包 / 幂等
- A2A 协作：治理策略 + 共享池跨 agent（核心裁决逻辑）
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.knowledge_pack import pack_memories, pack_info, unpack_pack
from trinity.adapters.sqlite import SQLiteAdapter
from trinity.governance import GovernanceEngine


@pytest.fixture()
def src_db(tmp_path: Path) -> str:
    d = str(tmp_path / "src.db")
    a = SQLiteAdapter(d)
    a.connect()
    a.store_memory("PPR 检索提升召回，联系 13800138000", persona_id="p1",
                   agent_id="eng", category="research", tags=["ppr"])
    a.store_memory("Redis 缓存命中率 95%", persona_id="p1", agent_id="eng",
                   category="research", tags=["cache"])
    a.store_memory("候选人电话 13911112222", persona_id="p1", agent_id="hr",
                   category="hiring", tags=["hiring"])
    a.disconnect()
    return d


def test_pack_filters_and_redacts(src_db: str, tmp_path: Path) -> None:
    pk = str(tmp_path / "kb.json")
    res = pack_memories(src_db, pk, category="research", title="检索优化")
    assert res["items"] == 2  # 只含 research 类
    raw = Path(pk).read_text(encoding="utf-8")
    assert "13800138000" not in raw  # PII 已脱敏
    assert "[PHONE]" in raw


def test_pack_info(src_db: str, tmp_path: Path) -> None:
    pk = str(tmp_path / "kb.json")
    pack_memories(src_db, pk, category="research")
    info = pack_info(pk)
    assert info["title"] == "research"
    assert info["item_count"] == 2
    assert info["redacted"] is True


def test_unpack_import_and_idempotent(src_db: str, tmp_path: Path) -> None:
    pk = str(tmp_path / "kb.json")
    pack_memories(src_db, pk, category="research")
    dst = str(tmp_path / "dst.db")
    r1 = unpack_pack(dst, pk, persona_id="imported")
    assert r1["imported"] == 2
    r2 = unpack_pack(dst, pk, persona_id="imported")
    assert r2["imported"] == 0
    assert r2["skipped"] == 2


def test_unpack_dry_run(src_db: str, tmp_path: Path) -> None:
    pk = str(tmp_path / "kb.json")
    pack_memories(src_db, pk, category="research")
    dst = str(tmp_path / "d.db")
    r = unpack_pack(dst, pk, persona_id="imported", dry_run=True)
    assert r["imported"] == 2  # dry-run 不写库


def test_a2a_governance_collab() -> None:
    """多 agent 协作的治理裁决（部门内/跨部门/知识库只读）。"""
    root = Path(__file__).resolve().parent.parent.parent
    gov = GovernanceEngine([
        str(root / "trinity/governance/policies/enterprise/engineering.yaml"),
        str(root / "trinity/governance/policies/enterprise/hr.yaml"),
    ])
    assert gov.check("eng-qa", "read", "eng-dev")["allow"] is True       # 部门内
    assert gov.check("hr-recruiter", "read", "eng-dev")["allow"] is False  # 跨部门拒
    assert gov.check("eng-qa", "read", "eng-kb")["allow"] is True        # 知识库只读
    assert gov.check("hr-recruiter", "write", "eng-kb")["allow"] is False # 写拒

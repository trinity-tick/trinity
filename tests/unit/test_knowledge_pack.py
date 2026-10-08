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


#: 原始 PII 样本（与 fixture 里写入的内容对应）——用于"输出里不得出现**原始** PII"的意图断言。
_RAW_PII_SAMPLES = ("13800138000", "13911112222", "4111111111111111", "zhangsan@example.com")


def _find_raw_pii(text: str) -> list:
    """输出里出现的**原始** PII（不限脱敏发生在哪一层）。"""
    return [p for p in _RAW_PII_SAMPLES if p in text]


def test_pack_filters_and_redacts(src_db: str, tmp_path: Path, monkeypatch) -> None:
    """**意图断言（t49 改写，不覆盖历史）**：pack 的输出里**不得出现原始 PII**。

    📌 **历史（原位保留，不要删）**：本条原先还断言 `"[PHONE]" in raw` ——
    即"pack 自己产出了 `[PHONE]` 标记"。该断言有**隐含前提：pack 是唯一的脱敏者**。
    **这个前提已被 G2/G7 改变**：t43/G2 把脱敏扩到"有 PII 就掩（客户端层）"、t48/G7 又**下沉到
    适配器写入边界** ⇒ fixture 里那条记忆**写入时**就已被掩成 `138********`，
    pack 自己的 `(r"1[3-9]\\d{9}" → "[PHONE]")` **再也匹配不上** ⇒ 旧断言必然失败。
    ⇒ 现在改断言它**真正关心**的东西：**输出无原始 PII**（无论哪一层脱的敏）。

    ⚠️ **防"为了变绿而放宽"**：本用例末尾带**负向对照** —— 把 adapter 守卫与 pack 自己的
    `_redact` **同时关掉**，输出**必须**出现原始手机号（证明这条断言**承重**、不是空转）。
    """
    import scripts.knowledge_pack as kp

    pk = str(tmp_path / "kb.json")
    res = pack_memories(src_db, pk, category="research", title="检索优化")
    assert res["items"] == 2  # 只含 research 类
    raw = Path(pk).read_text(encoding="utf-8")
    # ── 意图断言（t49）：不看"谁脱的敏"，只看"原始 PII 有没有漏出来" ──
    assert _find_raw_pii(raw) == [], (
        "知识包输出里出现了**原始 PII**（无论写入侧还是 pack 侧都该被脱敏）：%r" % _find_raw_pii(raw))

    # ── 负向对照（承重证明）：两层都关掉 ⇒ 原始 PII **必须**出现 ──
    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")          # 关写入侧守卫
    monkeypatch.setattr(kp, "_redact", lambda s: s)            # 关 pack 侧脱敏
    raw_db = str(tmp_path / "raw.db")
    _a = SQLiteAdapter(raw_db)
    _a.connect()
    _a.store_memory("PPR 检索提升召回，联系 13800138000", persona_id="p1",
                    agent_id="eng", category="research", tags=["ppr"])
    _a.disconnect()
    pk2 = str(tmp_path / "kb_raw.json")
    pack_memories(raw_db, pk2, category="research", title="未脱敏对照")
    raw2 = Path(pk2).read_text(encoding="utf-8")
    assert _find_raw_pii(raw2) == ["13800138000"], (
        "两层都关掉后原始 PII 竟然没出现 ⇒ 上面的意图断言是**空转**（不承重）：%r" % raw2[:200])


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

# -*- coding: utf-8 -*-
"""G73 判据：已知的"缩列写者"必须在 INSERT 列清单里带 agent_id 与 version。

为什么需要它（2026-10-08）：
  `agent_id IS NULL AND version IS NULL` 这一族曾累计 11,808 行（已回填）。
  它【不止一个产出者】—— 迁移路径（postgresql.py，已由 e0e62f7 修）+ 另外四处。
  本判据把"已知产出者"钉住：它们不得再省略这两列。

判据形态：**结构判据**（不触发写库）。
  理由：这类"丢列"缺陷用结构判据最便宜、不随数据 flaky、且恰好覆盖其形态；
  行为判据要【真的触发这些写路径并写库】—— 那在本轮是被禁止的。

牙齿：把任一处的修复回退成旧文本 ⇒ 本判据必须红。
  见 `_violations()`：它只读源码文本，所以"旧版本"可用合成文本喂进来验证。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 已知产出者：这些文件里的 INSERT INTO memories 必须带 agent_id 与 version
KNOWN_PRODUCERS = [
    "trinity/adapters/postgresql.py",              # 迁移 INSERT（e0e62f7 已修）
    "scripts/knowledge_sublimation.py",            # 维护链 :218 会跑（本轮修）
    "scripts/knowledge_sublimate_all.py",          # 本轮修
    "trinity/brain/memory_transaction.py",         # 本轮修
    "scripts/sqlite_pg_mirror.py",                 # 镜像（本轮修）
]

# 这几行是【允许】不带 agent_id 的：种子/DDL 模板行（见 _pg_schema.py:353-361）
ALLOWLIST_SUBSTRINGS = (
    "CREATE TABLE IF NOT EXISTS memories",
    "Trinity PostgreSQL initialized at",
)


def _violations(text: str, rel: str) -> list[str]:
    """返回该文件里"省略 agent_id 或 version 的 INSERT INTO memories"的说明。"""
    bad: list[str] = []
    # 逐条抓 INSERT INTO memories ( ... )，跨行
    for m in re.finditer(r"INSERT INTO memories\s*\(([^)]*)\)", text, re.S):
        cols = [c.strip().strip('"') for c in m.group(1).replace("\n", " ").split(",")]
        cols = [c for c in cols if c]
        if "agent_id" not in cols or "version" not in cols:
            line = text[: m.start()].count("\n") + 1
            missing = [c for c in ("agent_id", "version") if c not in cols]
            bad.append(f"{rel}:{line}: INSERT 列清单缺 {missing}（共 {len(cols)} 列）")
    return bad


def test_known_producers_set_agent_id_and_version():
    """正向：已知产出者必须带这两列。"""
    all_bad: list[str] = []
    for rel in KNOWN_PRODUCERS:
        p = ROOT / rel
        assert p.exists(), f"已知产出者不存在（路径变了？）：{rel}"
        text = p.read_text(encoding="utf-8-sig")
        ast.parse(text)  # 顺带证语法可解析
        all_bad.extend(_violations(text, rel))
    # 该文件本身是 DDL 模板，其"种子行"豁免 —— 但它不在 KNOWN_PRODUCERS 里
    assert not all_bad, "已知产出者仍省略 agent_id/version：\n" + "\n".join(all_bad)


def test_tooth_old_text_goes_red():
    """牙齿：把修复回退成【旧文本】⇒ 同一条 _violations 必须报违规。

    这条牙咬的是"判据本人"，而不是"另写一个检查器"。
    """
    old_sublimation = (
        'cur.execute("""\n'
        "    INSERT INTO memories (memory_id, content, category, status, importance, created_at)\n"
        "    VALUES (gen_random_uuid()::text, %s, 'semantic', 'active', 0.7, NOW())\n"
        '""", (knowledge,))\n'
    )
    bad = _violations(old_sublimation, "OLD:knowledge_sublimation.py")
    assert bad, "旧文本竟然没被判违规 ⇒ 牙齿不咬"
    assert "agent_id" in bad[0] and "version" in bad[0]

    # 反向：修后文本不得被判违规
    new_sublimation = (
        'cur.execute("""\n'
        "    INSERT INTO memories (memory_id, content, category, status, importance, created_at, agent_id, version)\n"
        "    VALUES (gen_random_uuid()::text, %s, 'semantic', 'active', 0.7, NOW(), 'default', 1)\n"
        '""", (knowledge,))\n'
    )
    assert _violations(new_sublimation, "NEW") == []


def test_allowlisted_seed_row_is_exempt():
    """反向：DDL 种子行（_pg_schema.py）确实会被 _violations 抓 —— 所以它【不在】KNOWN_PRODUCERS 里。

    这条判据记录一个事实：那个 9 列 INSERT 是【建表模板里的种子行】，
    只在"表不存在"时插一行；把它算作产出者是错的（见报告 §14.1/§16）。
    """
    p = ROOT / "trinity/adapters/_pg_schema.py"
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8-sig")
    bad = _violations(text, "_pg_schema.py")
    # 它【会】被判违规 —— 这正是为什么它必须留在 KNOWN_PRODUCERS 之外
    assert bad, "预期 DDL 种子行会被判违规（它确实只有 9 列）"
    assert any("Trinity PostgreSQL initialized at" in text for _ in [0])

"""存储加密口径闸门（2026-09-30，外部审计目标项 12）。

### 缺陷

`content` 列用 AES-256-GCM 加密，但 SQLite 的 **`tokenized_content` 保留明文 jieba 分词**
（`_crypto.py::_tokenized_for_storage` 的**有意**行为，为了让 FTS 能搜中文）。
⇒ 密文旁边躺着同一段内容的明文，「存储加密」对**实际机密性失效**。

而且加密**只在 SQLite 镜像启用**，生产 PostgreSQL 侧未实装。
项目已在 `SECURITY.md` 与一处对比文档里做过 2026-09-29 勘误，
但 README 仍有**未加限定**的宣称（「存储加密 + 审计可证明」「AES-GCM 加密」图示等）。

### 本轮实测（2026-09-30，只读）

| 指标 | 实测 |
|---|---|
| `memories` 总行 | 111,485 |
| active 行 | 24,863 |
| `content` 已加密（全部） | 106,865（95.9%） |
| `content` 已加密（active） | 22,572（90.8%） |
| **加密 active 行中影子列为明文** | **21,126（93.6%）** |
| active 行有 `tokenized_content` | 23,917（96.2%） |

> 注意：`SECURITY.md` 原记的 **97.3%** 与本次复测的 **93.6%** 不同 ——
> 比例随每日新写入漂移，**不是一个可以单独引用的常数**。已在文档里注明口径与测量日。

### 契约

任何对外文档若要提「存储加密 / 加密存储 / AES-GCM」，**同一行或紧邻的限定段**必须
同时出现作用域或影子列限定；否则判红。

运行：``python -m pytest tests/unit/test_encryption_claims_20260930.py -q``
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 用户会读的对外文档（口径必须站得住）
DOCS = ["README.md", "SECURITY.md",
        "docs/COMPARISON_VS_2026_SOTA_R2.md",
        "docs/BEFORE_AFTER_COMPARISON_20260829.md"]

#: 声称存储加密的措辞
CLAIM = re.compile(r"存储加密|加密存储|AES-?256-?GCM|AES-GCM|静态加密")

#: 可接受的限定（出现任一即算已限定）
QUALIFIER = re.compile(
    r"仅\s*SQLite|只.{0,4}SQLite|SQLite\s*镜像|影子列|tokenized_content|"
    r"不是生产默认|未实装|未启用|失效|勘误|口径校正|待决策|🟡|SECURITY_BOUNDARIES"
)


def _doc_lines(rel: str):
    p = ROOT / rel
    if not p.exists():
        return
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        yield i, line


@pytest.mark.parametrize("rel", DOCS)
def test_storage_encryption_claim_is_always_qualified(rel: str) -> None:
    """声称存储加密的行必须自带限定；限定也可出现在上下 6 行内（如 docstring 段/表格邻行）。"""
    lines = list(_doc_lines(rel))
    if not lines:
        pytest.skip(f"{rel} 不在本检出中")
    offenders = []
    for idx, (ln, line) in enumerate(lines):
        if not CLAIM.search(line):
            continue
        window = "\n".join(t for _, t in lines[max(0, idx - 6): idx + 7])
        if not QUALIFIER.search(window):
            offenders.append(f"{rel}:{ln}: {line.strip()[:90]}")
    assert offenders == [], (
        "以下「存储加密」宣称**缺少限定**（必须说明仅 SQLite 镜像 / 明文影子列 / 非生产默认）：\n  "
        + "\n  ".join(offenders)
    )


def test_readme_documents_the_plaintext_shadow_column() -> None:
    """README 必须点明 `tokenized_content` 是明文影子列（这是失效的根因）。"""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "影子列" in text, "README 未提明文影子列"
    assert "tokenized_content" in text, "README 未点名具体列"
    assert re.search(r"93\.6%|97\.3%", text), "README 缺少实测比例"


def test_security_md_records_both_measurements() -> None:
    """SECURITY.md 必须同时留下 09-29 与 09-30 两次测量，并提醒比例会漂移。"""
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "97.3%" in text, "不应删掉历史测量值（那会丢失口径演进）"
    assert "93.6%" in text, "缺少 2026-09-30 复测值"
    assert "漂移" in text or "测量日" in text, "必须提醒比例随写入漂移、引用需带口径"


def test_no_sota_table_still_claims_alignment() -> None:
    """对比表不得再把安全项标成「已对齐 ✅」—— 加密作用域不成立。"""
    text = (ROOT / "docs/COMPARISON_VS_2026_SOTA_R2.md").read_text(encoding="utf-8")
    bad = [_ln for _ln in text.splitlines()
           if "存储加密" in _ln and "已对齐" in _ln and "不可称" not in _ln]
    assert bad == [], f"对比表仍称已对齐：{bad}"


# ── 数据面事实（只读；库不在本机时跳过）──────────────────────────────────

DB = Path.home() / ".trinity" / "store" / "trinity_store.db"


@pytest.mark.skipif(not DB.exists(), reason="本机无 Trinity 存储，跳过数据面判据")
def test_shadow_column_really_is_plaintext() -> None:
    """**实证**影子列是明文：取一条已加密 active 行，验证其影子列不含 `enc:v1:` 前缀。

    这条判据的价值在于：它把「文档里的限定」锚在**可复现的数据事实**上，
    而不是靠人记得去改文字。
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=40)
    try:
        row = con.execute(
            "SELECT memory_id, substr(tokenized_content,1,80) FROM memories "
            "WHERE status='active' AND content LIKE 'enc:v1:%' "
            "AND tokenized_content IS NOT NULL "
            "AND tokenized_content NOT LIKE 'enc:v1:%' LIMIT 1").fetchone()
        assert row is not None, "找不到「密文行 + 明文影子列」的样本（前提变了，需重审）"
        mid, shadow = row
        assert shadow and not shadow.startswith("enc:v1:"), (
            f"{mid} 的影子列看似已加密 —— 若确实改了，文档限定与 SB-1/SB-2 需同步更新"
        )
    finally:
        con.close()


@pytest.mark.skipif(not DB.exists(), reason="本机无 Trinity 存储，跳过数据面判据")
def test_encryption_is_partial_so_claim_must_be_scope_limited() -> None:
    """加密覆盖率**不是** 100% ⇒ 任何「全库加密」表述都不成立。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=40)
    try:
        act = con.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        enc = con.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' "
            "AND content LIKE 'enc:v1:%'").fetchone()[0]
    finally:
        con.close()
    assert act > 0
    ratio = enc / act
    assert ratio < 1.0, "active 行竟然 100% 加密 —— 前提变了，需重审限定措辞"

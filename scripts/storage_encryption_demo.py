#!/usr/bin/env python3
"""
Trinity — B5 存储加密 Demo（2026-08-15）
==========================================
验证 AES-256-GCM 可选加密：
  1. 开启 TRINITY_STORAGE_ENCRYPTION=on，写记忆
  2. 直接读 SQLite 原始行 → content 应为密文（enc:v1: 前缀）
  3. API 层读取 → 返回解密后的明文
  4. FTS5 全文检索仍命中（tokenized_content 明文）
  5. 版本链 content 解密一致
  6. 关闭加密开关 → 明文落盘（对照组）

用法（注意：不设置 TRINITY_STORAGE_ENCRYPTION 则运行明文对照组）：
    python scripts/storage_encryption_demo.py            # 明文对照组
    $env:TRINITY_STORAGE_ENCRYPTION="on"
    python scripts/storage_encryption_demo.py            # 加密组
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

from trinity.adapters.sqlite import SQLiteAdapter  # noqa: E402


def main() -> int:
    encrypted = os.environ.get("TRINITY_STORAGE_ENCRYPTION", "").strip().lower() in ("1", "on", "true", "yes")
    print(f"== B5 存储加密 Demo（mode={'ENCRYPTED 🔐' if encrypted else 'PLAINTEXT'}）==")

    db_path = os.path.join(tempfile.mkdtemp(prefix="trinity_enc_"), "test_enc.db")
    adapter = SQLiteAdapter(db_path)
    adapter.connect()

    # 1. 写入（含中文，验证 FTS + 加密双路径）
    r1 = adapter.store_memory(
        content="Trinity 存储加密演示：这是机密记忆，包含电话号码 13800138000。",
        persona_id="p_enc", agent_id="agent-a", tags=["加密", "演示"],
    )
    r2 = adapter.store_memory(
        content="second memory for english fts query test",
        persona_id="p_enc", agent_id="agent-a",
    )
    mid1 = r1["memory_id"]
    mid2 = r2["memory_id"]
    adapter.disconnect()

    # 2. 直接读原始行 → 检查 content 列状态
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    row = raw.execute("SELECT content, tokenized_content, sha256_hash FROM memories WHERE memory_id = ?", (mid1,)).fetchone()
    raw.close()
    stored = row["content"]
    tokenized = row["tokenized_content"]
    is_cipher = stored.startswith("enc:v1:")
    print(f"   落盘 content 前缀: {stored[:20]!r} → 密文: {is_cipher}")

    ok = True
    if encrypted:
        ok = ok and is_cipher
        # 中文内容 tokenized 应为明文分词（jieba），非空
        ok = ok and bool(tokenized)
        # 英文内容在加密模式下也应写入明文 tokenized（避免 FTS 回退密文）
        row2 = sqlite3.connect(db_path)
        row2.row_factory = sqlite3.Row
        t2 = row2.execute(
            "SELECT tokenized_content FROM memories WHERE memory_id = ?", (mid2,)
        ).fetchone()["tokenized_content"]
        row2.close()
        ok = ok and bool(t2 and "english" in t2)
    else:
        ok = ok and not is_cipher
    print(f"   落盘格式断言: {'PASS ✅' if ok else 'FAIL ❌'}")

    # 3. API 层读取 → 解密
    adapter.connect()
    got = adapter.get_memory(mid1)
    plain = got["content"]
    expect = "Trinity 存储加密演示：这是机密记忆，包含电话号码 13800138000。"
    ok3 = plain == expect and "13800138000" in plain
    print(f"   get_memory 解密: {plain[:40]!r} → {ok3}")

    # 4. FTS 检索
    hits = adapter.search_memories("机密记忆", persona_id="p_enc", top_k=5)
    fts_ok = any(h["memory_id"] == mid1 for h in hits)
    hits2 = adapter.search_memories("english", persona_id="p_enc", top_k=5)
    fts_ok2 = any(h["memory_id"] == mid2 for h in hits2)
    print(f"   FTS 中文检索命中: {fts_ok}, 英文检索命中: {fts_ok2}")

    # 5. 版本链
    chain = adapter.get_version_chain(mid1)
    chain_ok = bool(chain) and chain[0]["content"] == expect
    print(f"   版本链解密一致: {chain_ok}")

    # 6. update_memory 路径
    upd = adapter.update_memory(mid1, content="更新后的机密记忆内容")
    upd_ok = upd is not None and "更新后" in upd["content"]
    print(f"   update_memory 解密: {upd_ok}")

    # 7. get_persona_memories / get_all_memories
    pm = adapter.get_persona_memories("p_enc", limit=10)
    pm_ok = bool(pm) and all(
        m["content"] and not m["content"].startswith("enc:v1:") for m in pm
    )
    print(f"   get_persona_memories 解密: {pm_ok}")
    adapter.disconnect()

    final = ok3 and fts_ok and fts_ok2 and chain_ok and upd_ok and pm_ok
    print(f"\nRESULT: {'PASS ✅' if final else 'FAIL ❌'}")
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())

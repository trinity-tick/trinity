# -*- coding: utf-8 -*-
"""trinity/brain/memory_transaction.py — 记忆事务（EXECUTION 261，大脑化）。

借鉴 MemTX（2026：Transactional Belief Commit）——记忆更新的
原子性：批量写入要么全成要么全回滚（防部分失败不一致）。

Trinity 现在：
  begin(): 事务开始（快照状态）
  commit(): 提交（原子生效）
  rollback(): 回滚（失败恢复快照）
"""
import os
from typing import Any
import sys
import json
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 兜底：退回原静默行为
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None


STATE_FILE = os.path.expanduser("~/.trinity/txn_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active": False, "snapshot": None, "operations": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception as _e:
        swallow(__name__, _e)


def begin() -> dict:
    """事务开始：快照当前记忆状态。"""
    st = _load()
    if st.get("active"):
        return {"ok": False, "error": "已有活跃事务"}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""), connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories")
        total = cur.fetchone()[0]
        conn.close()
        st["active"] = True
        st["snapshot"] = {"total_memories": total, "ts": __import__("time").time()}
        st["operations"] = []
        _save(st)
        return {"ok": True, "snapshot": st["snapshot"]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


def _apply(operation: dict) -> bool:
    """应用操作（记录在事务中——实际在 commit 时生效）。"""
    st = _load()
    if not st.get("active"):
        return False
    st["operations"].append(operation)
    _save(st)
    return True


def write(content: str, category: str = "txn") -> dict:
    """事务内写入（记录待提交）。"""
    ok = _apply({"op": "write", "content": str(content)[:100], "category": category})
    return {"queued": ok, "op": "write"}


def commit() -> dict:
    """提交：原子生效（全部写入）。"""
    st = _load()
    if not st.get("active"):
        return {"ok": False, "error": "无活跃事务"}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""), connect_timeout=10)
        cur = conn.cursor()
        committed = 0
        for op in st.get("operations", []):
            if op.get("op") == "write":
                # t50/G9：事务里的每个 write 都过守卫；high ⇒ **整个事务拒提交**（回滚，
                # 不留部分状态）。这是本层自定的契约（原先没有任何 high 语义），已写入报告。
                from trinity.adapters._pii_guard import adapter_pii_guard
                _txn_content, _txn_md, _txn_info = adapter_pii_guard(op["content"], None)
                if _txn_info.get("refuse"):
                    conn.rollback()
                    conn.close()
                    return {"ok": False, "rolled_back": True,
                            "error": "sensitive-high refused (adapter PII guard)"}
                cur.execute("""
                    INSERT INTO memories (memory_id, content, category, status, importance, agent_id, version)
                    VALUES (gen_random_uuid()::text, %s, %s, 'active', 0.5, 'default', 1)
                """, (_txn_content, op.get("category", "txn")))
                committed += 1
        conn.commit()
        conn.close()
        st["active"] = False
        st["operations"] = []
        _save(st)
        return {"ok": True, "committed": committed, "atomic": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60], "rolled_back": self_rollback()}


def self_rollback() -> bool:
    """回滚：恢复快照（清空未提交操作）。"""
    st = _load()
    st["active"] = False
    st["operations"] = []
    _save(st)
    return True


def rollback() -> dict:
    """回滚接口。"""
    ok = self_rollback()
    return {"ok": ok, "rolled_back": True, "note": "已回滚（放弃未提交操作）"}

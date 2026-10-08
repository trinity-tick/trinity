# -*- coding: utf-8 -*-
r"""_pg_conflicts.py — PG **冲突检测**一族（2026-10-08 G27/t170：按 `_policy` 迁出）

**为什么有这个文件**：`trinity/adapters/postgresql.py` 本轮涨到 2768 行、超 `docs/STRUCTURE_BUDGETS.json`
的 2700 达 **68 行**，且**超支全部来自本轮**（HEAD 2494）。按该文件的 `_policy`
「超预算时优先**拆分/迁出**，而不是上调数字」⇒ 把这一族**原样迁出**（逻辑零改动）。

**迁出内容**（从 `postgresql.py` 原样搬来，含注释）：
  · `_conflict_tokens()` —— 模块级 token 助手（jieba 分词 + `TRINITY_CONFLICT_TOKEN_MAX` 截断 + 正则回退）；
  · `_PgConflictsMixin._conflict_recall()` / `._assign_conflicts()` —— 候选召回 + 分组分配（G6/t109 移植自 SQLite 侧）。

⚠️ **两条纪律照旧**：
  1. `_conflict_tokens` **必须留在模块级**（G6R/t130：col-0 的 `def` 若被插进类体会把类**截断** ⇒
     `PostgreSQLAdapter` 曾因此有 29 个抽象方法、无法实例化）——本模块里它同样在**类之外**；
  2. ⭐ **调用点不迁**（`postgresql.py:616/873` 的 `self._assign_conflicts(...)` 仍在调用方）。
"""
from __future__ import annotations

import hashlib
import logging
import os

try:                                        # L1 静默失败治理（与 postgresql.py 同形态）
    from trinity._swallow import swallow
except Exception:  # noqa: BLE001
    def swallow(site: str, exc: object = None, *, detail: str = "") -> None:
        return None

logger = logging.getLogger(__name__)


#: G6/t109（2026-10-08）：冲突检测的 token 集 —— 与 **SQLite 侧同一语义**
#: （`trinity/adapters/sqlite/_crud.py:454 _token_set`）：jieba 分词、`TRINITY_CONFLICT_TOKEN_MAX`
#: 截断（默认 2000）、失败回退正则切分。
#: ⚠️ G6R/t130：本段**必须留在模块级**（首版被插进类体，col-0 的 `def` 把类截断 ⇒ 见 g6r_repair.py 头注）。
def _conflict_tokens(text: str):
    import re as _re
    src = str(text or "")
    try:
        import jieba
        tmax = int(os.environ.get("TRINITY_CONFLICT_TOKEN_MAX", "2000"))
        if tmax > 0 and len(src) > tmax:
            src = src[:tmax]
        toks = [t.strip() for t in jieba.cut(src) if t.strip()]
    except Exception:  # noqa: BLE001
        toks = [t.strip() for t in _re.split(r"[\s,，。；;：:、]+", src) if t.strip()]
    return set(toks)


class _PgConflictsMixin:
    """PG 冲突检测（mixin；由 `PostgreSQLAdapter` 继承）。

    ⭐ 迁出后**成员集合不变**（`PostgreSQLAdapter` 仍能实例化，`__abstractmethods__ == 0`）。
    """

    # ── G6/t109（2026-10-08）：**PG 写入路径此前零冲突检测** ───────────────────────
    # 实测：PG `memories.conflict_group_id` NULL 74,232/85,935 = 86.38%，今晨新行全 NULL；
    # 而 SQLite 侧有 `sqlite/_crud.py:402 _assign_conflicts()` ⇒ 插件经 PG 写入的记忆
    # **永不获得冲突判定**（D-11 的原发现）。以下**移植**同一套语义：
    #   · 同分组 id 式：`"conf_" + md5("|".join(sorted([new_id, old_id])))[:12]`（SQLite `:441`）
    #   · 同阈值：`TRINITY_CONFLICT_OVERLAP`（默认 CONFLICT_TOKEN_OVERLAP=0.6）
    #   · 同动作：`UPDATE memories SET conflict_group_id=?, is_resolved=0 WHERE memory_id IN (?,?)`
    # ⚠️ **召回口径不同**（如实登记）：SQLite 用 `search_memories()`（FTS/BM25 召回 top-10）；
    #    PG adapter **没有** search 方法 ⇒ 这里用"新内容 3 个最长 token 的 LIKE 并集"召回（≤10 条）。
    #    分组**规则**相同 ⇒ 同一对内容在同一公式下得到**同一个组 id**；召回面不同 ⇒ 命中范围可能更窄。
    def _conflict_recall(self, new_memory_id: str, content: str, limit: int = 10,
                         conn=None):
        """候选召回：新内容的 3 个最长 token 的 LIKE 并集（**只读**）。

        ⭐ G10R2/t134：**传入 `conn` 时复用调用方的连接/事务**（0 额外池往返、0 额外提交）；
        不传时自开连接（与首版一致，供直调证据路径使用）。
        """
        toks = sorted(_conflict_tokens(content), key=len, reverse=True)[:3]
        toks = [t for t in toks if len(t) >= 2]
        if not toks or not self._connected:
            return []
        where = " OR ".join(["content LIKE %s"] * len(toks))
        args = ["%" + t + "%" for t in toks]
        #: ⚠️ 不能写成 `"…(%s)…LIMIT %%s" % where`：`%` 会作用于**整条** SQL，psycopg2 的 `%s`
        #: 占位符先被 Python 解释 ⇒ `not enough arguments for format string`（t109 自伤）。用拼接。
        sql = ("SELECT memory_id, content FROM memories "
               "WHERE memory_id <> %s AND (" + where + ") ORDER BY updated_at DESC LIMIT %s")
        try:
            if conn is not None:
                #: ⭐ 同一事务内读取（T134）：**不新开池往返**
                with conn.cursor() as cur:
                    cur.execute(sql, [new_memory_id] + args + [int(limit)])
                    return [(str(r[0]), str(r[1] or "")) for r in cur.fetchall()]
            with self._get_conn() as _own, _own.cursor() as cur:
                cur.execute(sql, [new_memory_id] + args + [int(limit)])
                return [(str(r[0]), str(r[1] or "")) for r in cur.fetchall()]
        except Exception as _e:  # noqa: BLE001 — 召回失败 ⇒ 不分配（**不猜**）
            swallow(__name__, _e)
            return []


    def _assign_conflicts(self, new_memory_id: str, content: str,
                         conn=None) -> int:
        """写入后分配冲突组（**语义与 SQLite 对齐**）。返回分配到的冲突组数量。

        ⭐ G10R2/t134：**传入 `conn` 时在调用方的同一事务内完成**（只 UPDATE、**不提交**，
        提交由调用方那一次 `commit()` 完成）⇒ 写入路径仍是"单事务单提交、单次池往返"。
        """
        if os.environ.get("TRINITY_CONFLICT_ASSIGN", "1").strip() in ("0", "off", "false", ""):
            return 0
        new_tokens = _conflict_tokens(content)
        if not new_tokens:
            return 0
        threshold = float(os.environ.get("TRINITY_CONFLICT_OVERLAP", "0.6"))
        assigned = 0
        for mid, old_content in self._conflict_recall(new_memory_id, content, conn=conn):
            if not mid or mid == new_memory_id or old_content == content:
                continue
            old_tokens = _conflict_tokens(old_content)
            if not old_tokens:
                continue
            overlap = len(new_tokens & old_tokens) / max(len(new_tokens), len(old_tokens))
            if overlap < threshold:
                continue
            group = "conf_" + hashlib.md5(
                "|".join(sorted([new_memory_id, mid])).encode()).hexdigest()[:12]
            try:
                if conn is not None:
                    with conn.cursor() as cur:      #: 同事务：**不提交**
                        cur.execute("UPDATE memories SET conflict_group_id=%s, is_resolved=0 "
                                    "WHERE memory_id IN (%s, %s)",
                                    (group, new_memory_id, mid))
                else:
                    with self._get_conn() as _own, _own.cursor() as cur:
                        cur.execute("UPDATE memories SET conflict_group_id=%s, is_resolved=0 "
                                    "WHERE memory_id IN (%s, %s)",
                                    (group, new_memory_id, mid))
                        _own.commit()
                assigned += 1
            except Exception as _e:  # noqa: BLE001
                swallow(__name__, _e)
        if assigned:
            logger.info("PG-CONFLICT-ASSIGNED n=%d group_candidates=%d", assigned, assigned)
        return assigned

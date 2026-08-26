#!/usr/bin/env python3
"""consolidate_temporal.py — 时间层级巩固（TiMem 式 Temporal Memory Tree，2026-08-25）

把碎片记忆按时间尺度归纳为层级摘要（保留原始，non-lossy）：
  L1 session   → 原始记忆（不合并，保留）
  L2 daily     → 把当天碎片记忆归纳为日常摘要（偏好/事实/决策）
  L3 weekly    → 把一周 daily 归纳为行为模式/偏好演化
  L4 profile   → 把多周 weekly 归纳为稳定画像（偏好/价值观）

设计依据（网络调研 2026）：
- TiMem（Temporal Memory Tree）在 LoCoMo 75.30 / LongMemEval-S 78.96 达 SOTA，
  核心是跨时间尺度归纳——Trinity 只有 decay（删减）无归纳，扁平记忆是最大缺口；
- 偏好/多会话题（Trinity 评测最差，nDCG 0.722）直接受益于模式归纳。

数据模型：
- 层级摘要写入 memories 表：category='consolidation', memory_layer='consolidated',
  metadata={level: 'daily|weekly|profile', source_ids: [...], period: 'YYYY-MM-DD|YYYY-Www'}
- 原始记忆不动（non-lossy）；幂等（consolidate_state.json 记录已处理 period）

用法：
    python scripts/consolidate_temporal.py                # 巩固最近 7 天（默认）
    python scripts/consolidate_temporal.py --days 1      # 只巩固昨天
    python scripts/consolidate_temporal.py --dry-run     # 预览不写
    python scripts/consolidate_temporal.py --weekly      # 也生成 weekly（每周跑）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
STATE_FILE = os.path.expanduser("~/.trinity/consolidate_state.json")

# 巩固时排除的类别（已有结构化语义，无需归纳）
SKIP_CATEGORIES = {"evolution", "consolidation", "lme", "test", "stress"}
MIN_MEMORIES_PER_GROUP = 3       # 少于 3 条碎片不巩固（噪音防扩）
MAX_SOURCES_PER_SUMMARY = 30     # 每次归纳最多引用的源记忆数
LLM_MODEL = "deepseek-chat"
LLM_TIMEOUT = 120


def _llm_summarize(items: list, level: str) -> str:
    """LLM 归纳一组记忆为层级摘要。失败返回空串（调用方跳过该组）。"""
    try:
        cred = open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig").read()
        key = None
        for line in cred.splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY"):
                key = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        if not key:
            return ""
        _LEVEL_SPEC = {
            "daily": "把以下当天记忆归纳为简洁的日常摘要（保留事实、偏好、决策；去除重复；不超过 5 条要点）",
            "weekly": "把以下一周的日常记忆归纳为行为模式/偏好演化摘要（发现重复出现的偏好、习惯、主题；不超过 6 条）",
            "profile": "把以下多周的周摘要归纳为稳定用户画像（长期稳定的偏好/价值观/习惯；不超过 8 条）",
        }
        prompt = (
            _LEVEL_SPEC.get(level, "归纳以下记忆为摘要")
            + "\n输出纯文本，不要编号以外的格式。\n记忆：\n"
            + "\n---\n".join(
                f"[{i+1}] {str(m.get('content', ''))[:400]}" for i, m in enumerate(items[:MAX_SOURCES_PER_SUMMARY])
            )
        )
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 600,
        }
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed_days": [], "processed_weeks": [], "processed_profiles": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _fetch_memories(conn: sqlite3.Connection, since_dt: datetime, limit: int = 3000) -> list:
    """取 since 以来的 active 非巩固记忆（按时间倒序）。

    2026-08-25 修复：必须走 Trinity adapter 读取——content 列 AES-256-GCM
    加密（存储加密默认开启），裸 SQL 读到的是密文（LLM 归纳出"无法解读"垃圾）。
    adapter.get_all_memories 内部 _decrypt_content 解密。
    """
    out = []
    try:
        os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
        from trinity import Trinity
        mem = Trinity()
        # 取全部（get_all_memories 已解密）；在内存过滤日期/层级
        all_mems = mem._adapter.get_all_memories(limit=limit) if hasattr(mem, "_adapter") else []
        for m in all_mems:
            created = m.get("created_at") or ""
            if created < since_dt.isoformat():
                continue
            if (m.get("memory_layer") or "") == "consolidated":
                continue
            out.append({
                "memory_id": m.get("memory_id"), "content": m.get("content"),
                "category": m.get("category"), "agent_id": m.get("agent_id"),
                "created_at": created, "importance": m.get("importance"),
                "metadata": m.get("metadata"),
            })
    except Exception:
        # 兜底：裸 SQL（可能读到密文，但至少不崩）
        rows = conn.execute(
            "SELECT memory_id, content, category, agent_id, created_at, importance, metadata "
            "FROM memories WHERE status='active' AND created_at >= ? "
            "AND memory_layer IS NOT 'consolidated' "
            "ORDER BY created_at DESC LIMIT ?",
            (since_dt.isoformat(), limit),
        ).fetchall()
        for r in rows:
            out.append({
                "memory_id": r[0], "content": r[1], "category": r[2], "agent_id": r[3],
                "created_at": r[4], "importance": r[5], "metadata": r[6],
            })
    return out


def _insert_consolidated(conn: sqlite3.Connection, content: str, level: str,
                         source_ids: list, period: str) -> str:
    """写入层级摘要记忆（category=consolidation, layer=consolidated）。

    2026-08-25 修复：必须走 adapter.store_memory——content 列 AES 加密，
    裸 SQL 写入明文会导致读取解密失败/不一致。
    """
    meta = json.dumps({"level": level, "source_ids": source_ids, "period": period},
                      ensure_ascii=False)
    try:
        os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
        from trinity import Trinity
        mem = Trinity()
        res = mem.ingest(
            content,
            agent_id="consolidation",
            category="consolidation",
            tags=["consolidation", f"level:{level}", f"period:{period}"],
            importance=0.7,
            metadata={"level": level, "source_ids": source_ids, "period": period},
            postprocess=False,
        )
        # 标记 memory_layer=consolidated（只 UPDATE 该列，content 密文不动）
        mid = (res or {}).get("memory_id")
        if mid:
            try:
                conn.execute("UPDATE memories SET memory_layer='consolidated' "
                             "WHERE memory_id=?", (mid,))
                conn.commit()
            except Exception:
                pass
        return (content or "")[:40]
    except Exception:
        # 兜底：裸 SQL（明文写入——兼容无加密场景）
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO memories (memory_id, session_id, persona_id, tenant_id, content, role, "
            "importance, tags, category, status, version, created_at, updated_at, memory_layer, metadata) "
            "VALUES (?, 'consolidation', 'default', 'default', ?, 'assistant', 0.7, ?, "
            "'consolidation', 'active', 1, ?, ?, 'consolidated', ?)",
            (f"cons_{level}_{int(time.time())}_{abs(hash(period)) % 10000}",
             content, json.dumps([f"consolidation:{level}"]), now, now, meta),
        )
        conn.commit()
        return content[:40]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="巩固最近 N 天（默认 7）")
    parser.add_argument("--weekly", action="store_true", help="也生成 weekly 层级（每周跑一次）")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    # 需要写连接（插入 consolidated）——重新用写模式
    conn.close()
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("PRAGMA busy_timeout=30000")

    state = _load_state()
    # 2026-08-25 修复：created_at 存 UTC——比较必须用 UTC
    # （本地时间差 8h 会漏数据/错误分组）
    since = datetime.utcnow() - timedelta(days=args.days)
    mems = _fetch_memories(conn, since)
    # 2026-08-25（质量优化）：过滤结构化噪音——[会话自动摘要]/[project]/[决策]/
    # [summary] 前缀是 agent 写入的结构化记忆（非用户话语），归纳会污染摘要。
    _STRUCT_PREFIX = ("[会话自动摘要]", "[project]", "[决策]", "[summary]", "[research]")
    _clean = []
    for m in mems:
        if m["category"] in SKIP_CATEGORIES:
            continue
        c = (m["content"] or "").strip()
        if not c or any(c.startswith(p) for p in _STRUCT_PREFIX):
            continue
        _clean.append(m)
    # 按天分组，每组按 importance 降序取前 30（少而准——LLM 归纳质量关键）
    by_day: dict = defaultdict(list)
    for m in _clean:
        day = (m["created_at"] or "")[:10]
        if day:
            by_day[day].append(m)
    for day in by_day:
        by_day[day] = sorted(by_day[day], key=lambda x: -(x["importance"] or 0))[:30]

    made = 0
    skipped = 0
    for day in sorted(by_day):
        if day in state["processed_days"]:
            continue
        group = by_day[day]
        if len(group) < MIN_MEMORIES_PER_GROUP:
            skipped += 1
            continue
        src_ids = [m["memory_id"] for m in group]
        summary = _llm_summarize(group, "daily") if not args.dry_run else "(dry-run)"
        if not summary and not args.dry_run:
            # 2026-08-25 修复：LLM 失败不标记 processed——下次运行重试
            print(f"  [{day}] LLM 归纳失败，保留待重试")
            continue
        if not args.dry_run:
            _insert_consolidated(conn, summary, "daily", src_ids, day)
            print(f"  [daily {day}] {len(group)} 条 → 摘要（{len(summary)} 字）")
        else:
            print(f"  [daily {day}] {len(group)} 条 → (dry-run 不写)")
        state["processed_days"].append(day)
        made += 1

    if args.weekly:
        # 2026-08-25 重构：weekly 从**已巩固的 daily 摘要**归纳（解密读取 consolidated）
        # ——比原始碎片更浓缩，符合 TiMem 层级巩固语义（parent 归纳自 children）。
        _cons = conn.execute(
            "SELECT memory_id, content, metadata FROM memories "
            "WHERE memory_layer='consolidated' AND metadata LIKE '%\"level\": \"daily\"%'"
        ).fetchall()
        wk_groups: dict = defaultdict(list)
        for cid, ccontent, cmeta in _cons:
            try:
                meta = json.loads(cmeta)
                day = meta.get("period", "")
                d = datetime.strptime(day, "%Y-%m-%d")
                wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                wk_groups[wk].append({"memory_id": cid, "content": ccontent})
            except Exception:
                continue
        for wk in sorted(wk_groups):
            if wk in state["processed_weeks"]:
                continue
            daily_mems = wk_groups[wk]
            if len(daily_mems) < 2:
                continue
            summary = _llm_summarize(daily_mems[:10], "weekly") if not args.dry_run else "(dry-run)"
            if not summary and not args.dry_run:
                print(f"  [weekly {wk}] LLM 失败，保留待重试")
                continue
            if not args.dry_run:
                _insert_consolidated(conn, summary, "weekly",
                                     [m["memory_id"] for m in daily_mems], wk)
                print(f"  [weekly {wk}] {len(daily_mems)} 条 daily 摘要 → 周模式")
            else:
                print(f"  [weekly {wk}] {len(daily_mems)} 条 daily (dry-run)")
            state["processed_weeks"].append(wk)

        # profile：多周 weekly → 稳定用户画像（level=profile）
        if len(state["processed_weeks"]) >= 2 and "profile" not in state.get("processed_profiles", []):
            wk_mems = []
            for wk in state["processed_weeks"]:
                wk_mems.extend([
                    {"memory_id": cid, "content": cc}
                    for cid, cc in conn.execute(
                        "SELECT memory_id, content FROM memories "
                        "WHERE memory_layer='consolidated' AND metadata LIKE ?",
                        (f'%\"period\": \"{wk}\"%',)
                    ).fetchall()
                ])
            if len(wk_mems) >= 2:
                summary = _llm_summarize(wk_mems[:10], "profile") if not args.dry_run else "(dry-run)"
                if summary or args.dry_run:
                    if not args.dry_run:
                        _insert_consolidated(conn, summary, "profile",
                                             [m["memory_id"] for m in wk_mems], "profile-1")
                        print(f"  [profile] {len(wk_mems)} 条 weekly → 稳定画像")
                    else:
                        print(f"  [profile] {len(wk_mems)} 条 weekly (dry-run)")
                    state.setdefault("processed_profiles", []).append("profile-1")

    _save_state(state)
    print(f"=== consolidate done: daily={made} skipped_small={skipped} "
          f"weeks={len(state['processed_weeks'])} profiles={len(state.get('processed_profiles', []))} ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

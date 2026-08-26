#!/usr/bin/env python3
"""memory_ops.py — Mem0 式记忆操作（2026-08-25 P2）

写路径 LLM 决策：扫描近期 ingest 记忆，对每条检索相似记忆（top-3），
LLM 判断操作：
  - ADD    （新信息）→ 保留原样
  - UPDATE （相似但需合并）→ 把新内容合并进已有记忆（走 CRDT 版本链）
  - NOOP   （重复/已被覆盖）→ 归档该条（status=archived，保留审计链）

设计依据（网络调研 2026）：
- Mem0 的 ADD/UPDATE/DELETE/NOOP 操作接口是"LLM 推理与确定性记忆语义"的
  干净接缝；Trinity write_amplification 7.28 偏高（重复 ingest 未合并）。

安全：
- 只处理 category 在白名单的记忆（default/general/decision——不碰 lme 等）；
- 只归档 NOOP（保留审计），不硬删；
- UPDATE 用 ingest 新内容合并（CRDT 自动版本化，旧版保留）；
- env TRINITY_MEM_OPS=on 启用处理（默认 off）。

用法：
    python scripts/memory_ops.py --hours 24          # 处理最近 24h
    python scripts/memory_ops.py --dry-run           # 只判断不执行
    python scripts/memory_ops.py --limit 20          # 最多处理 20 条
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
STATE_FILE = os.path.expanduser("~/.trinity/memory_ops_state.json")

# 参与判断的类别（用户话语/决策/一般事实——不碰 lme 批量导入等）
OPS_CATEGORIES = {"general", "decision", "knowledge", "session", "episodic", "consolidation"}
MAX_SIMILAR = 3
LLM_MODEL = "deepseek-chat"
LLM_TIMEOUT = 90


def _llm_decision(content: str, similar: list) -> str:
    """LLM 判断操作：返回 ADD/UPDATE/NOOP。失败默认 ADD（保守不误删）。"""
    try:
        cred = open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig").read()
        key = None
        for line in cred.splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY"):
                key = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        if not key:
            return "ADD"
        sim_text = "\n".join(
            f"[{i+1}] {str(s.get('content', ''))[:300]}" for i, s in enumerate(similar[:MAX_SIMILAR])
        ) or "(无相似记忆)"
        prompt = (
            "你是记忆管理系统操作决策器。判断新记忆与已有相似记忆的关系：\n"
            "1. ADD：新记忆包含**新信息**（不重复已有内容）→ 保留；\n"
            "2. UPDATE：新记忆是**已有记忆的补充/修正**（同一主题更完整）→ 应合并进已有；\n"
            "3. NOOP：新记忆**与已有记忆基本重复**（无新增信息）→ 应丢弃。\n"
            f"新记忆：{content[:400]}\n"
            f"相似记忆：\n{sim_text}\n"
            "只输出一个词：ADD / UPDATE / NOOP"
        )
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0, "max_tokens": 10,
        }
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        out = body["choices"][0]["message"]["content"].strip().upper()
        return out if out in ("ADD", "UPDATE", "NOOP") else "ADD"
    except Exception:
        return "ADD"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24, help="处理最近 N 小时（默认 24）")
    parser.add_argument("--limit", type=int, default=20, help="最多处理条数（默认 20）")
    parser.add_argument("--dry-run", action="store_true", help="只判断不执行")
    args = parser.parse_args()

    if os.environ.get("TRINITY_MEM_OPS", "off").strip().lower() not in ("1", "on", "true", "yes")             and not args.dry_run:
        print("TRINITY_MEM_OPS=off — 跳过（--dry-run 可预览）")
        return 0

    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("PRAGMA busy_timeout=30000")

    # 2026-08-25 修复：created_at 存 UTC（引擎写入带 +00:00），
    # 比较必须用 UTC——本地时间差 8h 会漏掉/误判数据
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()
    # 取待处理记忆（active、白名单类别、非 consolidation 输出）
    rows = conn.execute(
        "SELECT memory_id, content, category, agent_id, created_at, importance FROM memories "
        "WHERE status='active' AND created_at >= ? "
        "AND memory_layer IS NOT 'consolidated' ORDER BY created_at DESC LIMIT ?",
        (since, args.limit),
    ).fetchall()

    state = {"processed": []}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    counts = {"ADD": 0, "UPDATE": 0, "NOOP": 0}
    for r in rows:
        mid, content, category, agent_id, created = r[0], r[1], r[2], r[3], r[4]
        importance = float(r[5] or 0)
        if mid in state["processed"]:
            continue
        if category not in OPS_CATEGORIES:
            continue
        # 2026-08-25 修复：importance>=0.7 的重要记忆不参与 NOOP
        # （决策/关键事实——LLM 对模板化内容会过度合并，重要记忆应保留）
        protected = importance >= 0.7
        # 检索相似记忆（解密读取，同 agent）
        similar = []
        try:
            os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
            from trinity import Trinity
            mem = Trinity()
            h = mem.search(content, top_k=MAX_SIMILAR, agent_id=agent_id)
            hl = h.get("results", []) if isinstance(h, dict) else h
            for x in hl:
                c = (x.get("content") or "").strip()
                if c and c != content:
                    similar.append({"memory_id": x.get("memory_id"), "content": c})
        except Exception:
            pass
        decision = _llm_decision(content, similar)
        # 保护：重要记忆 NOOP → 强制 ADD（保留）
        if decision == "NOOP" and protected:
            decision = "ADD"
        counts[decision] = counts.get(decision, 0) + 1

        if args.dry_run:
            print(f"  [dry-run] {mid[:16]} ({category}, imp={importance:.1f}) → {decision}")
        elif decision == "NOOP":
            conn.execute("UPDATE memories SET status='archived' WHERE memory_id=?", (mid,))
            conn.commit()
            print(f"  [NOOP] {mid[:16]} ({category}) → archived（保留审计）")
        elif decision == "UPDATE" and similar:
            # 合并：把新内容并入最相似记忆（CRDT 新版本）
            target = similar[0]["memory_id"]
            try:
                old = conn.execute(
                    "SELECT content FROM memories WHERE memory_id=?", (target,)
                ).fetchone()
                if old and old[0]:
                    merged = str(old[0])[:2000] + "\n[merge] " + content[:500]
                    from trinity import Trinity
                    mem = Trinity()
                    mem.ingest(merged, agent_id=agent_id, category=category,
                               tags=["merged"], metadata={"merged_from": mid},
                               postprocess=False)
                    conn.execute("UPDATE memories SET status='archived' WHERE memory_id=?", (mid,))
                    conn.commit()
                    print(f"  [UPDATE] {mid[:16]} → merged into {target[:16]}")
            except Exception as exc:
                print(f"  [UPDATE] {mid[:16]} merge failed: {str(exc)[:60]}")
        else:
            print(f"  [ADD] {mid[:16]} ({category}) → keep")
        state["processed"].append(mid)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print(f"=== memory_ops done: ADD={counts['ADD']} UPDATE={counts['UPDATE']} "
          f"NOOP={counts['NOOP']} (processed {len(state['processed'])} total) ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

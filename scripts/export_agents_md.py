#!/usr/bin/env python3
"""export_agents_md.py — 生成 AGENTS.md（OpenAI/Anthropic"文件即记忆"标准接口）。

P0-4 (COMPARISON_VS_2026_SOTA_R7, 2026-08-24):
把 Trinity 的 DSH 结构层（会话/目标/事件）与记忆使用指南导出为
标准 AGENTS.md，供 Cursor / Claude Code / Codex 等生态客户端
"5 分钟接入"Trinity 记忆层。

用法:
  python scripts/export_agents_md.py                # 打印到 stdout
  python scripts/export_agents_md.py --out AGENTS.md  # 写入文件
  python scripts/export_agents_md.py --no-live       # 不含实时快照（纯模板）

数据源: SQLite 权威大库的结构层（structure_store 的 dsh_sessions /
dsh_events / dsh_goals 等表）；无需启动任何服务。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Any, Dict, List, Optional

# 让脚本可直接运行（cwd=仓库根时 scripts/ 不在 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 抑制 import 期聚合器自举（已知坑：ensure_bootstrapped 会全量构建 faiss 索引
# 数分钟、GIL 饥饿）；本脚本只需 structure_store 的查询，无需聚合器。
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _live_snapshot() -> Dict[str, Any]:
    """读取结构层实时快照（容错：任何失败降级为空快照）。"""
    from trinity.structure_store import (
        structure_sessions,
        structure_stats,
        goal_list,
    )

    snap: Dict[str, Any] = {"ok": False, "reason": ""}
    try:
        stats = structure_stats()
        sessions = structure_sessions().get("sessions", [])
        goals = goal_list().get("goals", [])

        active_goals = [
            g for g in goals
            if str(g.get("status", "")).lower() not in ("completed", "archived", "paused")
        ]
        recent_sessions = sessions[:5]

        snap = {
            "ok": True,
            "stats": {
                "sessions": stats.get("sessions", 0),
                "events": stats.get("events", 0),
                "goals": stats.get("goals", 0),
                "todos": stats.get("todos", 0),
                "schedules": stats.get("schedules", 0),
            },
            "active_goals": [
                {
                    "objective": g.get("objective", "")[:160],
                    "phase": g.get("phase") or "",
                    "round": g.get("round") or 0,
                    "status": g.get("status", ""),
                }
                for g in active_goals[:10]
            ],
            "recent_sessions": [
                {
                    "title": s.get("title") or "",
                    "status": s.get("status", ""),
                    "updated_at": s.get("updated_at", ""),
                    "session_id": s.get("session_id", ""),
                }
                for s in recent_sessions
            ],
        }
    except Exception as exc:  # pragma: no cover - defensive
        snap["reason"] = str(exc)
    return snap


def _render_snapshot(snap: Dict[str, Any]) -> str:
    if not snap.get("ok"):
        return (
            "\n<!-- 实时快照不可用（结构层读取失败）：%s -->\n"
            % snap.get("reason", "unknown")
        )

    s = snap["stats"]
    lines = [
        "\n## Trinity 记忆层实时快照（生成于 %s）\n" % _now_str(),
        "| 指标 | 值 |",
        "|---|---|",
        "| 会话数 | %s |" % s.get("sessions", 0),
        "| 结构事件数 | %s |" % s.get("events", 0),
        "| 目标数 | %s |" % s.get("goals", 0),
        "| Todos | %s |" % s.get("todos", 0),
        "| 计划 | %s |" % s.get("schedules", 0),
    ]

    if snap.get("active_goals"):
        lines.append("\n### 活跃目标（active goals）\n")
        lines.append("| 状态 | 阶段 | 轮次 | 目标 |")
        lines.append("|---|---|---|---|")
        for g in snap["active_goals"]:
            lines.append(
                "| %s | %s | %s | %s |"
                % (g["status"], g["phase"] or "-", g["round"], g["objective"])
            )

    if snap.get("recent_sessions"):
        lines.append("\n### 最近会话（recent sessions）\n")
        for s in snap["recent_sessions"]:
            title = s["title"] or "(untitled)"
            lines.append(
                "- `%s` [%s] %s" % (s["session_id"], s["status"], title)
            )

    return "\n".join(lines)


TEMPLATE = """# AGENTS.md — Trinity Memory

> 本文件由 Trinity 生成（{generated}）。它让接入本仓库/工作区的 AI Agent
> 自动了解 Trinity 记忆层的存在、用法与当前状态。
> 相关规范背景：OpenAI AGENTS.md / Anthropic CLAUDE.md 的"文件即记忆"标准。
{snapshot}

## 1. Trinity 是什么

Trinity 是长程记忆系统（Memory OS）：跨会话保存并检索事实、偏好、决策与
会话轨迹。它不是普通 RAG 知识库——记忆带 CRDT 版本链、SHA-256 审计、
时间感知与多租户隔离（persona/session/agent/tenant）。

## 2. 如何检索记忆

Agent 在回答"是否记得… / 之前做过… / 用户偏好…"类问题时，应当先检索
Trinity，而不是仅凭当前上下文猜测。

- **MCP（推荐）**：本机 MCP server 暴露 `memory_search` / `memory_write` /
  `memory_update` / `memory_delete` / `audit_query` / `memory_tag_search`
  （stdio 模式无鉴权；streamable-http :8003 用 Bearer token）。
- **REST**：`GET http://127.0.0.1:8001/memory/search?q=...&top_k=5`
  （混合检索；`/memory/search/hybrid` 走 5 通道 RRF 融合）。
- **CLI**：`python -m trinity search --query "..." --top-k 5`。

检索建议：
- 默认用混合模式（hybrid），短查询走 FTS 轻通道（毫秒级）。
- 检索不到时放宽关键词（Trinity 用 jieba 中文分词 + BM25 + 向量 + 图谱
  多通道融合；同义改写后再试一次）。
- 关键事实请用 `audit_query` 核对版本链与来源。

## 3. 如何写入记忆

- 值得记住的才写：用户偏好、事实、决策、完成的工作、踩过的坑。
- 内容自包含：让未来的 agent 不看本对话也能读懂（含路径、工具名、数字）。
- 建议结构（与 Trinity 记忆契约一致）：

```
[类型] 日期 一句话标题
- 目标/任务: ...
- 关键决策与理由: ...
- 结果/产出: ...
- 坑与经验: ...
- 下一步: ...
```

- 标签保持一致（项目名/领域/类型），importance 0.4-0.6 常规、0.7+ 决策/事故。
- 用 `memory_update` 更新已有记忆而不是重复写入新条目。

## 4. 会话身份与隔离

- 每个 DSH 会话自动注册为独立 agent 身份（agent_id=dsh-<sessionId>）。
- 未显式指定时检索默认按当前会话隔离；空结果自动回退全局检索。
- 多租户：persona_id / session_id / agent_id / tenant_id 四级过滤。

## 5. 常用命令

```bash
# 搜索记忆（混合检索，top-5）
python -m trinity search --query "用户偏好" --top-k 5

# 引擎诊断（版本/存储/通道/规模）
python -m trinity diagnostics

# 服务健康
curl -s http://127.0.0.1:8001/health

# 维护（decay/tiers/sync）
powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks all
```

## 6. 注意事项（known pitfalls）

- SQLite 大库多进程共享有写锁风险：批量写入用维护链（每日 03:00 自动），
  不要并发大量 ingest。
- 引擎默认检索路径是 FTS5（R@5 0.975 > hybrid-rrf 0.942）；显式
  `search_hybrid` 才走 5 通道融合。
- 语义缓存默认 memory 后端（TTL 300s）：刚写入的记忆可能短暂命中旧缓存，
  敏感操作可用 `TRINITY_CACHE_BACKEND=off` 临时关闭。
- PG 连接必须用 127.0.0.1（localhost 解析 IPv6 会被 pg_hba 拒绝）。

## 7. 安全与可证明性（R8-R9 起出厂默认）

- **存储加密默认开启**（AES-256-GCM）：content 列密文落盘，FTS 不受影响；
  `TRINITY_STORAGE_ENCRYPTION=off` 显式关闭。
- **记忆投毒写入过滤**（OWASP AG 类）：写路径扫描注入模式，高危命中自动
  归档 + `INJECTION_ISOLATED` 审计；`TRINITY_INJECTION_SCAN=off` 关闭。
- **可证明记忆回执**：`GET http://127.0.0.1:8001/audit/receipt/{{memory_id}}`
  返回当前哈希/版本链/审计链完整性（验证者可独立重算 SHA-256 对账）；
  `GET /audit/integrity` 全链校验。
- **健康真实上报**：`/health` 含 engine 组件——引擎故障报 degraded + 错误
  详情（不再有"健康假象"）；写锁竞争时引擎只读降级（检索可用、写报错）。

## 8. 图谱与时序能力（R7-R8 增强）

- **edge bi-temporal**：`GET /graph/relations/at?at_time=...` 时点查询；
  创建关系可带 valid_from/valid_to（对齐 Zep/Graphiti）。
- **PPR 图谱通道**（HippoRAG 式）：混合检索的图谱通道含 PPR 多跳扩散
  （`TRINITY_GRAPH_PPR` 默认 on）。

## 9. 可观测指标（/metrics）

- 记忆命中率/写放大：`trinity_write_amplification` /
  `trinity_queries_by_source_total` / `trinity_semantic_cache_hit_rate_pct` /
  `trinity_last_query_ts`——Prometheus 可直接抓取。
"""


def build_agents_md(include_live: bool = True) -> str:
    """生成 AGENTS.md 内容。"""
    snapshot_block = ""
    if include_live:
        snapshot_block = _render_snapshot(_live_snapshot())
    return TEMPLATE.format(generated=_now_str(), snapshot=snapshot_block)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AGENTS.md（Trinity 记忆接口说明）")
    parser.add_argument("--out", default="", help="输出文件路径（默认 stdout）")
    parser.add_argument(
        "--no-live", action="store_true",
        help="不包含实时结构层快照（纯模板）",
    )
    args = parser.parse_args()

    content = build_agents_md(include_live=not args.no_live)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(content)
        print("AGENTS.md written -> %s (%d bytes)" % (args.out, len(content.encode("utf-8"))))
    else:
        sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

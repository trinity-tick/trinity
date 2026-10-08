#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据资产分类审计（Step 0）——回答「本地数据到底是什么」。

## 为什么需要它

2026-10-05 的重新评价发现：全库 113,882 行里 86,319 行（75.8%）已归档，
而 active 27,027 行中 **71.8% 从未被读过一次**。在动手「提升利用率」之前，
必须先回答一个前置问题：**这 27k 行里，哪些本来就该被检索、哪些不该**。
不先分类，后面所有「覆盖率」读数都会把不该参与检索的数据算进分母（见 §13.0）。

## 三条硬约束（本仓 AGENTS.md）

1. **§13.0**：不参与检索的类目**不进**任何以「被读」为尺的分母；清单**只从引擎常量取**
   （`trinity/core/client/_search.py::_RETRIEVAL_EXCLUDE_CATEGORIES`），不另立一份；
   且剔除**必须显式报数**（`retrieval_excluded_rows`），不许静默丢数据。
2. **§13.1**：只需**数字/时间/状态**的分析可直读库；**需要内容**的分析一律走引擎/接口。
   本脚本**只读元数据列**（category / agent_id / status / access_count / source_uri 前缀），
   **不读 `content` 列** —— 该列在 SQLite 侧是 `enc:v1:` 密文。
3. **§13.3**：清单类判据的可信度取决于**匹配规则的边界**，所以本脚本把
   **实际使用的规则集原样打印并写进产物**，改规则必须重取清单。

## 判据（可失败）

- 库不可读 / 表缺列 ⇒ rc=2，结论 `INCONCLUSIVE`（**不许**把「取不到」读成「不存在」，§13.2）。
- `retrieval_excluded_rows` 必须出现在产物里；缺失即 rc=2。
- 类目名对不上引擎常量时**显式报警**（`exclusion_name_mismatch`）——
  这一类「常量写 benchmark、库里存 doc:benchmark」的错位必须被看见，不能静默失效。

用法：
    python scripts/trinity_data_taxonomy_audit.py
    python scripts/trinity_data_taxonomy_audit.py --store <path> --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import logging

try:  # §16：脚本自己 print 中文/箭头时必须钉编码，否则在 GBK 控制台当场 traceback
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/trinity_data_taxonomy_audit.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(
    os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db")

# ---- 分类规则集（改这里必须重取清单，§13.3）----------------------------------
#: 机器自产：这些 agent 名空间是 Trinity 自己的管道在写自己的运行记录，
#: 不是用户/agent 的知识。它们进检索面只会挤占名额。
MACHINE_SELF_AGENTS = (
    "brain-cycle", "brain-consumer", "brain-procedure", "memory-revival",
    "recurrence-consolidate", "evolution", "consolidation", "observation-builder",
    "brain-digest", "cognition", "social-cognition", "identity-keeper", "cm-agent",
)
#: agent 名空间前缀也是机器自产（loop-col-<ts> / loop-compress-<ts>）
MACHINE_SELF_AGENT_PREFIXES = ("loop-",)
#: 残留：测试/压测/基准消融写进来的行，本就不该出现在生产检索面。
#: 依据：2026-10-05 实测，这些名空间里有 cache_test / kw_test / inc-test /
#: heur-test3 / fix-test / probe-agent / t1 / a / b1 / sig_0 / sig_1 / _warmup /
#: ingest_test / ablate-locomo，以及 1 条 **agent_id 为空字符串**。
#:
#: ⚠ 2026-10-05 自我更正（§13.3：清单的可信度取决于匹配规则的边界）：
#: 本清单第一版把 `reader` / `reader-ops` / `reader-value` / `usage-feedback` / `u1`
#: **也**算进了 residue —— 那是**猜的**。实测这 5 个名空间共 60 行、类目是
#: observation / analysis / general，看起来像**合法的子 agent**（读取器运维 / 读取器价值
#: 追踪 / 利用率探针），把它们判成「测试残留」会导致误归档真实数据。
#: ⇒ 移出 residue，改走 AMBIGUOUS_AGENTS（分类为 `unknown`，等人工定性）。
RESIDUE_AGENTS = (
    "cache_test", "kw_test", "inc-test", "inc-vec", "inc-vec2", "heur-test3",
    "fix-test", "probe-agent", "t1", "a", "b1", "sig_0", "sig_1", "_warmup",
    "ingest_test", "ablate-locomo", "stress-agent", "test-vec-agent", "smoke",
)
#: 定性未决：看着像测试、也可能是合法子 agent。**一律不判为 residue**，
#: 归入 unknown 并显式暴露，等待人工决定（同本仓 DECISIONS_PENDING 的处置方式）。
AMBIGUOUS_AGENTS = ("reader", "reader-ops", "reader-value", "usage-feedback", "u1")
#: doc 分块：kb_harvested 是文档切块（实测 6 份文档 = 5,611 行）。
DOC_CHUNK_CATEGORIES = ("kb_harvested", "doc:general", "doc:plan", "doc:summary",
                        "doc:benchmark", "video_harvested")
#: 用户/agent 知识：真正「一条记忆 = 一个知识单元」的类目。
USER_KNOWLEDGE_CATEGORIES = ("knowledge", "general", "session", "episodic",
                             "procedural", "consolidated", "observation",
                             "decision", "insight")


def load_engine_exclusions() -> "tuple[list, str]":
    """从**引擎常量**取排除类目（§13.0：不许另立一份）。

    返回 (categories, source)。取不到时返回 ([], reason) 并由调用方判 INCONCLUSIVE ——
    绝不用本文件里的硬编码兜底冒充引擎口径。
    """
    try:
        sys.path.insert(0, ROOT)
        from trinity.core.client._search import _RETRIEVAL_EXCLUDE_CATEGORIES  # noqa: E402
        return sorted(set(_RETRIEVAL_EXCLUDE_CATEGORIES)), "trinity.core.client._search"
    except Exception as e:  # noqa: BLE001  §13.2：失败原因必须留痕
        return [], "UNAVAILABLE: %s: %s" % (type(e).__name__, str(e)[:160])


def classify(agent_id: str, category: str, exclude_cats) -> str:
    """纯函数：一行 active 记忆 -> 分类标签。可单测。"""
    a = (agent_id or "").strip()
    c = (category or "").strip()
    if c in set(exclude_cats or ()):
        return "retrieval_excluded"
    if a == "":
        return "residue"                     # 空名空间：无归属，实测存在 1 条
    if a in RESIDUE_AGENTS:
        return "residue"
    if a in AMBIGUOUS_AGENTS:
        return "unknown"                     # 不猜：等人工定性（见清单上方注释）
    if a in MACHINE_SELF_AGENTS or any(a.startswith(p) for p in MACHINE_SELF_AGENT_PREFIXES):
        return "machine_self"
    if c in DOC_CHUNK_CATEGORIES:
        return "doc_chunk"
    if c in USER_KNOWLEDGE_CATEGORIES:
        return "user_knowledge"
    return "unknown"


def audit(store: str) -> dict:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "store": store,
           "rules": {
               "machine_self_agents": list(MACHINE_SELF_AGENTS),
               "machine_self_agent_prefixes": list(MACHINE_SELF_AGENT_PREFIXES),
               "residue_agents": list(RESIDUE_AGENTS),
               "doc_chunk_categories": list(DOC_CHUNK_CATEGORIES),
               "user_knowledge_categories": list(USER_KNOWLEDGE_CATEGORIES),
           }}
    if not store or not os.path.exists(store):
        out["verdict"] = "INCONCLUSIVE"
        out["error"] = "store not found: %s" % store       # §13.2：不静默
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"),
                              uri=True, timeout=30)
        con.execute("PRAGMA busy_timeout=25000")
    except Exception as e:  # noqa: BLE001
        out["verdict"] = "INCONCLUSIVE"
        out["error"] = "cannot open store: %s: %s" % (type(e).__name__, str(e)[:160])
        return out

    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(memories)")]
    except Exception as e:  # noqa: BLE001
        out["verdict"] = "INCONCLUSIVE"
        out["error"] = "PRAGMA table_info failed: %s" % str(e)[:160]
        return out
    need = {"agent_id", "category", "status", "access_count"}
    missing = sorted(need - set(cols))
    out["columns_present"] = len(cols)
    if missing:
        out["verdict"] = "INCONCLUSIVE"
        out["error"] = "missing columns: %s" % missing     # §13.2：形状没对上 ≠ 数据问题
        return out

    exclude_cats, ex_source = load_engine_exclusions()
    out["engine_exclude_categories"] = exclude_cats
    out["engine_exclude_source"] = ex_source
    if not exclude_cats:
        # 取不到引擎常量 ⇒ 我们无法按 §13.0 剔除，读数不可信
        out["verdict"] = "INCONCLUSIVE"
        out["error"] = "engine exclusion constant unavailable: %s" % ex_source
        return out

    rows = con.execute(
        "SELECT COALESCE(agent_id,''), COALESCE(category,''), "
        "COALESCE(access_count,0) FROM memories WHERE status='active'").fetchall()
    total_all = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    by_status = dict(con.execute(
        "SELECT status, COUNT(*) FROM memories GROUP BY status").fetchall())
    # §16 观察面：类目名是否真实存在，必须扫**全部状态**，不能只看 active。
    # 实测反例（2026-10-05，本工具第一版就栽在这）：`lme` 有 13,743 行但 active=0，
    # 只看 active 会误报「类目名对不上 ⇒ 排除静默失效」，而它其实完全正常。
    all_categories = {r[0] for r in con.execute("SELECT DISTINCT category FROM memories")}
    con.close()

    out["total_rows"] = total_all
    out["by_status"] = by_status

    buckets: dict = {}
    for agent, cat, acc in rows:
        tag = classify(agent, cat, exclude_cats)
        b = buckets.setdefault(tag, {"rows": 0, "never_read": 0})
        b["rows"] += 1
        if int(acc or 0) == 0:
            b["never_read"] += 1
    out["buckets"] = buckets
    out["active_rows"] = len(rows)

    # §13.0：剔除必须显式报数
    out["retrieval_excluded_rows"] = buckets.get("retrieval_excluded", {}).get("rows", 0)
    retrievable = out["active_rows"] - out["retrieval_excluded_rows"]
    out["retrievable_rows"] = retrievable
    never = sum(b["never_read"] for t, b in buckets.items() if t != "retrieval_excluded")
    out["retrievable_never_read"] = never
    out["retrievable_cold_rate"] = round(never / retrievable, 4) if retrievable else None

    # §13.3：类目名对不上引擎常量时必须被看见（常量写 benchmark、库里存 doc:benchmark）
    # 扫全部状态（见上方 all_categories 的注释：只看 active 会误报）。
    out["exclusion_name_mismatch"] = sorted(c for c in exclude_cats
                                            if c not in all_categories)
    out["exclusion_active_rows"] = {
        c: sum(1 for _, cat, _ in rows if cat == c) for c in exclude_cats}

    out["verdict"] = "OK"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    r = audit(a.store)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== 数据资产分类审计 ==")
        print("库：%s" % r.get("store"))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("verdict") != "OK":
            print("判定：%s —— %s" % (r.get("verdict"), r.get("error")))
            return 2
        print("总行 %d；active %d；状态分布 %s"
              % (r["total_rows"], r["active_rows"], r["by_status"]))
        print("引擎排除类目 %s（来源 %s）"
              % (r["engine_exclude_categories"], r["engine_exclude_source"]))
        print()
        print("%-20s %8s %10s %8s" % ("分类", "行数", "从未被读", "冷率"))
        for tag in sorted(r["buckets"], key=lambda t: -r["buckets"][t]["rows"]):
            b = r["buckets"][tag]
            cr = "%5.1f%%" % (100.0 * b["never_read"] / b["rows"]) if b["rows"] else "-"
            print("%-20s %8d %10d %8s" % (tag, b["rows"], b["never_read"], cr))
        print()
        print("retrieval_excluded_rows = %d（已按 §13.0 从读率分母剔除，显式报数）"
              % r["retrieval_excluded_rows"])
        print("可检索 active = %d；其中从未被读 = %d；冷率 = %s"
              % (r["retrievable_rows"], r["retrievable_never_read"], r["retrievable_cold_rate"]))
        if r.get("exclusion_name_mismatch"):
            print("⚠ 类目名对不上（排除可能静默失效）：%s" % r["exclusion_name_mismatch"])

    out = a.out
    if not out:
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        out = os.path.join(ROOT, "output",
                           "data_taxonomy_audit_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False, indent=1)
    if not a.json:
        print()
        print("产物：%s" % out)
    return 0 if r.get("verdict") == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())

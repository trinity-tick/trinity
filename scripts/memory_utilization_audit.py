#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_utilization_audit.py — 记忆**利用率**四个一等指标（P0-0，2026-09-16，只读）。

## 为什么有这个脚本

本仓反复出现的病不是"存不下"，而是**"存了没用"**：

  · 735（2026-09-14）：active 25,203 条中 **91.3%** 的 `access_count=0`；
    `build_opening_surface()` / `context_injection_prompt()` 生产调用点 **0**。
  · W4（2026-09-13）：36,568 条 active 中 33,483 条（91.6%）从未被检索。
  · 结论：**检索覆盖率**与**投递量**是两条不同的断点。前者问"引擎找得到吗"，
    后者问"找到了有没有送进模型上下文"。历史上两者被混为一谈（735 §735.5），
    导致"接线完成"与"利用率提升"无法分辨。

`scripts/retrieval_coverage.py`（W4）只盯 U1 的一个面。本脚本把利用率拆成
**四个可独立移动的数**（U1–U4），一次出数、可挂 CI、可对 A/B 前后做差。

## 四个数（口径写死在代码里，便于复核）

| 指标 | 定义 | 数据源 | 方向 |
|---|---|---|---|
| **U1 检索覆盖率** | active 中被检索过的比例（ever / 30d） | PG `memories.last_retrieved_at` | 越高越好 |
| **U2 投递量** | 真正进入**模型上下文**的记忆条数（累计） | 注入账本 `context_injection_ledger.json` | 越高越好 |
| **U3 写读比** | 近 30 天写入条数 / 近 30 天被读到的**去重**条数 | PG `created_at` / `last_retrieved_at` | 越低越好 |
| **U4 注入 token** | 累计注入上下文的 token 估算（chars/4） | 注入账本 | **有上限**（预算，不是越大越好） |

### U1 三拆（P1-0，2026-09-16）：**ratchet 只认 a+b**

拆分前的 U1 是"全部被检索过的记忆"——一个数里混着**三件方向与含义都不同**的事。
S0 实测（`dsh-ops/evidence/p1_0_s0_gates.txt`）：U1 趋势 7 日均值一天之内从 199.29
涨到 **341.71**（+71%），而同期 U2 投递量**纹丝不动**（5→5）⇒ **闸门在涨，利用率没动**。
根因不是"读数错了"，是**口径把管道自读也算成了利用率**。

| 面 | 判据（写死在常量里） | 含义 | 冻结基线 |
|---|---|---|---|
| **U1-a** | `last_retrieved_at` 在 24h 内 **且** `agent_id like 'dsh-%'` | agent 命名空间内的记忆被读到 | **104 / 24h** |
| **U1-b** | 注入账本 `delivered_total` / `cold_delivered` | 真正进了模型上下文 | **5 / 0** |
| **U1-c** | `last_retrieved_at` 在 24h 内 **且** `category like 'doc:%'` | **管道与基准自读**（探针自污染源） | **879 / 24h** |

⇒ **ratchet 只判 U1-a 与 U1-b**；U1-c 与两个混口径的全量读数（点值 / 全量趋势）
**降级为报告项并在闸门输出里点名**（静默降级＝偷偷删判据，见 `REPORT_ONLY`）。

**为什么 U1-c 必须被排除（不是"眼不见为净"）**：实测那 879 条 `doc:*` 命中
**全部**由 `agent_id='doc-fusion'` 的文档管道读出（890 行里 890 行 agent_id=doc-fusion），
它们走**真实检索路径** ⇒ 一边把冷集焐热、一边把 U1 点值抬高，却丝毫不代表
"存下来的记忆被用起来了"。留着它 = 管道一忙，闸门就绿（G10 的反面：噪声驱动的闸门）。

**U1-a 的语义必须连口径一起引用（不冒充）**：`memories.agent_id` 是记忆的**归属命名空间**
（写入方），**不是读取方**。S0 实测那 104 条里只有 **12 条**能在 `audit_log` 的检索命中里
找到读取证据，且其中大部分 agent 在 24h 内**没有**任何读取审计行
⇒ U1-a 应读作「**agent 命名空间内的记忆在 24h 内被读到（被谁读的不限）**」。
`u1_split.cross_check` 同时给出**读取方归因**的窄读数（`dsh-*` 主体在 audit_log 里的
命中去重数）供交叉核对，但它**不参与 ratchet**（口径更窄，量级差 2.5 倍）。

### 口径的三个关键选择（都有实测理由，不是随手定的）

1. **U1 用 `last_retrieved_at`，不用 `last_accessed_at`**。
   `last_accessed_at` 建表即 `DEFAULT NOW()`，从未被触碰的行也带"最近"时间戳；
   实测 active 中 `access_count=0` 的 33,493 条里有 14,875 条 30 天内 —— 用它会把
   coverage 从 6.75% 抬到 46%（**假高**）。`access_count` 只作交叉核对（`u1.cross_check`）。

2. **U2 只认"投递到模型上下文"，不认"引擎浮现"**。
   这两者相差一个数量级且方向相反（735 实测：浮现 5.0 条/轮，其中 48.5% 是从未被
   检索过的冷记忆，但**一条都没进上下文**）。故分开记：
   `surfaced_by_engine`（引擎侧，来自 `opening_surface_counters.json`）与
   `delivered_to_context`（宿主侧，来自注入账本）。**U2 取后者。**

3. **U4 是天花板不是地板**。注入 token 是**成本**（污染上下文、挤占预算），
   故 ratchet 对 U4 判"不得超过基线 + 容差"，而不是"不得低于"。

### 注入账本（`~/.trinity/data/context_injection_ledger.json`）

宿主侧（`dsh-plugin/dsh-trinity`）每次把记忆块注入系统提示时追加记账：

```json
{"calls": 12, "delivered_total": 41, "chars_total": 18342,
 "tokens_est_total": 4586, "by_source": {"opening": 12},
 "latency_ms": {"n": 12, "p50": 41.2, "p95": 88.0, "max": 130.5},
 "first_ts": 1.7e9, "last_ts": 1.7e9}
```

**账本不存在 ⇒ U2=U4=0，且 `injection_ledger.present=false`**。这不是"没测"，
而是"注入通路为零"的**可证伪陈述**：任何人可核 `grep -c '"opening"' lib/index.js`。

## 用法

    python scripts/memory_utilization_audit.py                # 出数（只读）
    python scripts/memory_utilization_audit.py --json
    python scripts/memory_utilization_audit.py --ratchet      # CI：只在与基线比较变差时退出 1

退出码：0 = 通过（或建基线）；1 = ratchet 判定变差 / 基线损坏（fail-closed）。

## 已知局限（不隐瞒）

- U1/U3 需要 PG。CI（ubuntu、无 PG）下这两项为 `null`，ratchet **显式 SKIP 并打印原因**
  —— 不静默当成通过，也不把 CI 一上线就搞红（本仓刚清过一轮门禁噪音）。
- U4 的 token 是 `chars/4` 估算，不是真实 tokenizer；口径固定以便前后可比，
  **不得当作计费依据**。
- 账本由宿主插件写，**账本可以被少写**（例如注入失败但未记账）⇒ 本脚本证明的是
  "账本声称的投递量"，不是"模型真的看见了"。后者需真会话取证（见 EXECUTION P0-1）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LEDGER = os.path.expanduser("~/.trinity/data/context_injection_ledger.json")
OPENING_COUNTERS = os.path.expanduser("~/.trinity/data/opening_surface_counters.json")

#: P0-1/P1-1：来源标签。**刻意本地定义为字面量**（本脚本要保持可独立运行，
#: 不为了两个字符串去 import `trinity.engine_worker` 而拉起整个引擎）。
#: 与 `trinity/engine_worker.py` 的 `ORIGIN_PLUGIN` / `ORIGIN_UNKNOWN` **必须一致** ——
#: 这条由用例 `test_opening_origin_dimension.py::test_审计的来源常量必须与引擎一致` 压着。
ORIGIN_PLUGIN = "dsh-plugin"
ORIGIN_UNKNOWN = "unknown"
API_COUNTERS = os.path.expanduser("~/.trinity/data/api_retrieval_counters.json")

#: ── 2026-09-19（EXECUTION §916）生产面口径：把**公开评测语料**从分母里剔出来 ──────────────
#: 触发事实（实测）：并发会话按已登记方案把 LongMemEval-S 语料写入 PG
#: （`agent_id='eval-longmemeval-s-official'` 19,195 行）⇒ 分母被污染：
#:   active 20,129 → 44,995（**+124%**）；U1 覆盖率 16.95% → 11.69%（**虚低 31% 相对**）；
#:   U3 writes_7d 含 19,195 行评测写入 ⇒ 写读比虚高。
#: 与 plaintext_ratio 闸门（§915.1 的 [生产面]/[全库] 三分）**同向**：非生产单列、**可见**。
#: ⚠️ 本常量只用于**新增并列读数**：`value`/`coverage_30d`/`value(U3)` 这些**被 ratchet 的量一律不动**
#: —— 改被 ratchet 的口径属决策，不由审计脚本单方面改（这正是本仓 §835/§915.1 的纪律）。
NONPROD_SCOPE_SQL = ("agent_id LIKE 'eval-%' OR agent_id LIKE 'ablate-%' OR agent_id LIKE 'bench-%' "
                     "OR agent_id LIKE 'lme-%' OR agent_id IN ('ckpt-test', 'crash-test', 'test-stim')")


#: 2026-09-20（§918）：`--scope` 显式覆盖。存在的理由：**基线与闸门口径必须同源** ——
#: §917.3 的一行命令只能重建一次基线，而闸门（docs/GATE_SET.json）不带环境变量执行；
#: 若不同步声明口径，下次闸门就拿 all 口径去比 prod 基线（不可比 ⇒ 长期红 ⇒ G10）。
_CLI_SCOPE: str = ""


def set_cli_scope(v) -> None:
    """argparse 落地 `--scope`；空 = 未指定（回落到 env / 默认 all）。"""
    global _CLI_SCOPE
    _CLI_SCOPE = str(v or "").strip().lower()


def util_scope() -> str:
    """被 ratchet 的口径：`all`（默认，全库）| `prod`（生产面，剔评测语料）。

    ⚠️ **默认不变**（all）—— 把 ratchet 口径切成生产面属**决策**（与 §915.1 同性质），
    本仓纪律不允许审计脚本单方面改被 ratchet 的量。所有者点头后，用**一条命令**切换：

        $env:TRINITY_UTIL_SCOPE='prod'; python scripts/memory_utilization_audit.py --accept-baseline --reason "口径切生产面（§917）"

    切换后 **必须重建基线**：旧基线是全库口径，两者不可比（否则闸门会长期红 → 恒红即被忽略，G10）。
    """
    v = _CLI_SCOPE or str(os.environ.get("TRINITY_UTIL_SCOPE", "") or "").strip().lower()
    return "prod" if v in ("prod", "production", "生产面") else "all"
OUT = os.path.expanduser("~/.trinity/state/memory_utilization.json")

#: P0-1 接线点在 DSH 插件里的注入通路标记。用于回答"注入通路到底有没有"这个
#: 前置问题（U2/U4 恒为 0 时，区分"通路不存在"与"通路存在但没投递"）。
INJECTION_MARKERS = (
    ("dsh-plugin/dsh-trinity/lib/index.js", '"opening"', "宿主调用 worker 的 opening 方法"),
    ("dsh-plugin/dsh-trinity/lib/index.js", "systemPrompt", "宿主把记忆块注册进系统提示"),
)

# ── U1 三拆的口径常量（P1-0，2026-09-16）────────────────────────────────────
#: U1-a 的归属命名空间前缀。`memories.agent_id` 由写入方决定（DSH 会话写记忆时
#: 传 `dsh-<sessionId>`），故 `dsh-%` = **agent 侧命名空间**。改这个前缀
#: ⇒ 冻结基线 104/24h 不再可比 ⇒ 由 tests/unit/test_u1_split_scope.py 当场红。
U1A_AGENT_PREFIX = "dsh-"
#: U1-c 的"管道自读"分类前缀。`doc:*` 是文档管道（fuse_docs / doc_corpus_sync /
#: kb-harvester / 基准）读自己刚灌进库的语料，S0 实测全部由 agent_id='doc-fusion' 读出。
U1C_CATEGORY_PREFIX = "doc:"
#: 冻结基线的单位是 **/24h**（滑动窗口）。换窗口必须同时重录基线并写进 baseline_history。
U1_WINDOW_HOURS = 24

# ── 2026-09-21（§1015）：**冷库存归因**（U1-attrib，报告项）────────────────────
# 动机：U1 的全局覆盖率把**两件不同的事**压成一个数：
#   ① 检索没在工作（真缺陷）；② 货架上批量灌入的语料本来就不会被逐条读（库存结构）。
# 实测（2026-09-21）：active 26,052 条里 perception(6,684) + (null)(5,882) +
# kb-harvester(4,973) + doc-fusion(2,939) 四家占 20.5k（79%），近 30 天覆盖率 4–8%；
# 而会话类（dsh-session-*）覆盖率 98–100% ⇒ 全局 16% 是**结构使然**。
# 判"检索是否健康"必须看**去掉冷库存后的热路径覆盖率**（hot_path_coverage）。
BULK_COLD_MIN_ACTIVE = 500      # 生产者至少这么大才算批量灌入
BULK_COLD_MAX_COVERAGE = 0.05   # 且近 30 天覆盖率低于此
HOT_PATH_MIN_COVERAGE = 0.25    # 热路径覆盖率下限（低于它判 attention）


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        print("[warn] _read_json: 读不到 %s ⇒ 该读数缺省（不是 0）" % path, file=sys.stderr)
        return None


def _pg():
    try:
        from scripts._pg_std import pg_connect  # type: ignore
        conn = pg_connect()
        conn.autocommit = True
        return conn
    except Exception:
        print("[warn] _pg: 连库失败 ⇒ 本次读数会缺（不是 0）", file=sys.stderr)
        return None


def _count(cur, sql: str):
    try:
        cur.execute(sql)
        return int(cur.fetchone()[0])
    except Exception:
        print("[warn] _count: 查询失败 ⇒ 该计数缺省（不是 0）", file=sys.stderr)
        return None


def _u1_trend(cur, days: int = 14) -> dict:
    """U1 **日趋势**：日新增被检索条数序列 + 近 7 日均值。

    ## 为什么必须有趋势（S0 实测，2026-09-16）

    点读数（30 天覆盖率）的日变化只有 0.0002 量级（实测 0.0916 → 0.0914），
    而**日新增被检索条数**近 14 日为 min 2 / max 734 / 均值 111.3
    ⇒ **极差/均值 = 6.58**。用点值判"机制有没有效果"在数学上不可能：
    噪声比信号大两个数量级。故 ratchet 判据改为 **近 7 日日新增的均值**
    （点值仍在报告里，人要看绝对水平）。

    ⚠️ 读数会被**探针自污染**（探针走真实检索路径 ⇒ 自己把冷集焐热，735.5 已记录）：
    2026-09-14 的 734 就含评测/探针活动。故序列里同时给出**当天是否异常**只作提示，
    不作判据——判据只用均值，且均值下降即视为退化（探针污染只会抬高均值，不会掩盖下降）。
    """
    try:
        cur.execute(
            "select date_trunc('day', last_retrieved_at)::date::text d, count(*) "
            "from memories where last_retrieved_at is not null "
            "and last_retrieved_at > now() - interval '%d days' group by 1 order by 1" % int(days))
        series = [(r[0], int(r[1])) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120], "series": []}
    vals = [n for _, n in series]
    out = {"series": series, "days": len(vals),
           "last7": vals[-7:],
           "daily_mean_7d": daily_mean(vals[-7:]),
           "daily_mean_14d": daily_mean(vals),
           "min": min(vals) if vals else None, "max": max(vals) if vals else None,
           "peak_to_mean": (round(max(vals) / (sum(vals) / len(vals)), 2)
                            if vals and sum(vals) else None),
           "note": "判据用 daily_mean_7d（点读数被 6.58× 极差淹没）；序列含探针自污染"}
    out["value"] = out["daily_mean_7d"]
    return out


def daily_mean(vals) -> "float | None":
    """日均值。空序列返回 None —— **不得返回 0**：0 会被 ratchet 判成退化。"""
    try:
        xs = [float(v) for v in (vals or [])]
        return round(sum(xs) / len(xs), 2) if xs else None
    except Exception:  # noqa: BLE001
        return None


def _u2b_cold() -> dict:
    """U2b **冷投递量**：自动召回捞回的"此前从未被检索过"的记忆数（累计）。

    数据源 = 注入账本里插件记的 `cold_delivered`；引擎侧按 `ColdSet`
    （快照 `last_retrieved_at IS NULL` + 命中即移出）判定，见
    `trinity/bridges/opening_surface.py::ColdSet` 与 `engine_worker._opening`。
    """
    ledger = _read_json(LEDGER) or {}
    cold = int(ledger.get("cold_delivered") or 0)
    delivered = int(ledger.get("delivered_total") or 0)
    # 2026-09-20（§929）：**账本纪元**。该账本由 DSH 插件按**会话/worker 运行**写入
    # （transport=dsh-plugin/...→worker.opening，first_ts 每次新会话都会变）⇒
    # delivered_total 是**会话级计数器**，跨会话直接比大小必假红（实测 10215 → 75）。
    # 把纪元暴露出来，棘轮遇到纪元变化就**不判该项**（并显式点名，不静默通过）。
    return {"value": cold, "cold_delivered": cold, "delivered_total": delivered,
            "ledger_epoch": ledger.get("first_ts"),
            "cold_share": round(cold / delivered, 4) if delivered else None,
            "note": "冷 = 快照时 last_retrieved_at IS NULL 且本进程未投递过；"
                    "不是「全历史从未被用过」的绝对陈述（worker 重启会重取快照）"}


def classify_attribution(rows, min_active: int = BULK_COLD_MIN_ACTIVE,
                         max_coverage: float = BULK_COLD_MAX_COVERAGE,
                         hot_min: float = HOT_PATH_MIN_COVERAGE) -> dict:
    """把「谁在占着 active 货架、被读过多少」分类（**纯函数**，便于 S1 测试）。

    输入 rows: [{producer, active, hit_30d}, ...]（hit_30d = 近 30 天被检索过的条数）
    规则（写死、可复核、可失败）：
      · bulk_cold：active >= min_active **且** coverage_30d < max_coverage
        ⇒ 灌入型冷库存 —— 它不是「检索缺陷」，但**必须有配额/归档策略**；
      · 其余进 warm 组 ⇒ 组内覆盖率即 hot_path_coverage（判「检索是否健康」用它）。
    判据：hot_path_coverage < hot_min ⇒ verdict=ATTENTION（否则 OK）。
    为什么用组内覆盖率而不是全局：全局数被冷库存的体量稀释，涨跌主要反映**灌入速度**，
    不反映检索质量（§918 同族教训：口径不同源 ⇒ 判据必然误报）。
    """
    cold, warm = [], []
    for r in rows or []:
        a = int(r.get("active") or 0)
        h = int(r.get("hit_30d") or 0)
        cov = round(h / a, 4) if a else 0.0
        rec = {"producer": r.get("producer") or "(null)", "active": a, "hit_30d": h,
               "coverage_30d": cov}
        (cold if (a >= min_active and cov < max_coverage) else warm).append(rec)
    cold.sort(key=lambda x: -x["active"])
    warm.sort(key=lambda x: -x["active"])
    cold_active = sum(r["active"] for r in cold)
    cold_hit = sum(r["hit_30d"] for r in cold)
    warm_active = sum(r["active"] for r in warm)
    warm_hit = sum(r["hit_30d"] for r in warm)
    total = cold_active + warm_active
    hot_cov = round(warm_hit / warm_active, 4) if warm_active else None
    return {
        "value": hot_cov,
        "hot_path_coverage": hot_cov,
        "hot_path_active": warm_active,
        "hot_path_hit_30d": warm_hit,
        "cold_corpus_active": cold_active,
        "cold_corpus_hit_30d": cold_hit,
        "cold_corpus_share": round(cold_active / total, 4) if total else None,
        "bulk_cold_producers": [r["producer"] for r in cold],
        "rows": (cold + warm)[:16],
        "thresholds": {"min_active": min_active, "max_coverage": max_coverage,
                       "hot_min": hot_min},
        "verdict": ("ATTENTION" if (hot_cov is not None and hot_cov < hot_min)
                    else ("OK" if hot_cov is not None else "N/A")),
        "note": "热路径覆盖率=去掉 bulk_cold 生产者后的被读率（判检索健康）；"
                "冷库存占比=灌入型生产者占 active 的比例（判库存结构，不是缺陷）",
    }


# 2026-09-21（§1038）：**不参与检索的类目不进热路径分母**。
# 实测（§1037）：把 4,167 条 perception 归档后，冷库存占比 44.7%→22.7%，但"热路径覆盖率"
# 反而 26.2%→24.5%（判定翻成 ATTENTION）—— 因为**剩下的 active perception 多为未读**，
# 它们不参与语义检索（引擎 _RETRIEVAL_EXCLUDE_CATEGORIES），却进了"热路径"的分母，
# 等于用"不参与检索的数据"去拉低"检索是否健康"的读数。**与 §1036 是同一条口径**：
# 归档门与热路径覆盖率必须**一起**剔除这类类目，只剔一边就会自相矛盾。
# 判据：剔除的行数必须显式报出（不静默丢数据）；清单与引擎常量同源（测试看住）。
# 2026-09-21（§1044）：口径收进唯一来源 scripts/caliber.py
from caliber import RETRIEVAL_EXCLUDED_CATEGORIES  # noqa: E402 — 同目录（scripts/）


def _u1_attribution(cur) -> dict:
    """U1 冷库存归因：按生产者（agent_id）统计 active 体量与近 30 天被读率。

    2026-09-21（§1038）：**剔除不参与检索的类目**（否则它们会稀释热路径覆盖率，见上）。
    """
    try:
        _excl = list(RETRIEVAL_EXCLUDED_CATEGORIES)
        cur.execute("select count(*) from memories where status='active' "
                    "and lower(coalesce(category, '')) = any(%s)", (_excl,))
        _excluded_n = int((cur.fetchone() or [0])[0])
        cur.execute("select coalesce(agent_id, '(null)') as producer, count(*) as active, "
                    "count(*) filter (where last_retrieved_at > now() - interval '30 days') "
                    "as hit_30d from memories where status='active' "
                    "and not (lower(coalesce(category, '')) = any(%s)) group by 1", (_excl,))
        rows = [{"producer": p, "active": a, "hit_30d": h} for p, a, h in cur.fetchall()]
        out = classify_attribution(rows)
        # 2026-09-21（§1039 建议①的轻量版）：把"可归档候选"计数**放进每天跑的读数里** ——
        # 本轮 4,167 条的起点是"人工想起来跑一次"；这条读数一挂，等龄的行就会自己冒出来。
        try:
            import cold_corpus_triage as _CCT
            # 2026-09-21（§1076 根因）：**两个变量都要初始化** ——
            # §1075 我只补了 `_sel`，而下面写 `archivable_note` 时还用了 `_rows`；
            # 无 bulk 生产者的路径下 `_rows` 未定义 ⇒ NameError 被外层 except 吃掉 ⇒
            # 刚写好的 `archivable_candidates = 0` 被覆盖成 None，而 `retention_note` 已先写好
            # ⇒ 两条信息**不同源**（这就是 §1075 留档的那处不自洽的真因）。
            _sel = []
            _rows = []
            _bulk = out.get("bulk_cold_producers") or []
            if _bulk:
                cur.execute("select memory_id, category, importance, "
                            "extract(epoch from (now() - created_at))/86400.0 as age_days, "
                            "(last_retrieved_at is not null) as retrieved "
                            "from memories where status='active' and agent_id = any(%s)", (_bulk,))
                _rows = [{"memory_id": r[0], "category": r[1], "importance": r[2],
                          "age_days": r[3], "retrieved": r[4]} for r in cur.fetchall()]
                _sel = _CCT.select_candidates(_rows)
                out["archivable_candidates"] = len(_sel)
            else:
                # 2026-09-21（§1075 修复）：**没有 bulk_cold 生产者时也要记 0** ——
                # retention_alert 对「上一次为 0、本次 >0」专门判 ALERT（从 0 起跳一律人工看一遍），
                # 而那条规则只有在**零值被记下来**时才可能触发。此前整块被 if _bulk 跳过 ⇒ 0 行样本，
                # §1074 里"被回滚"的判断是错的（真因是这个守卫），这里一并更正。
                out["archivable_candidates"] = 0
                # 2026-09-21（§1074）：把候选数**落进 utilization_samples**（metric 固定名），
                # 并算**周环比**（用 caliber.retention_alert 的预先写死判据）。
                # 为什么现在就要落：判据是"周环比"，而周环比需要**历史**；
                # 等启用 signal 那天才开始记，头一周只能看到"两次 0 条"——那是没有判据的判据。
                # 采样节流：20 小时内已有样本就不再插（audit 会被闸门与日报反复调用）。
                # 2026-09-21（§1075 修复 §1074）：**用独立短连接**写样本 ——
                # 该工具的连接非 autocommit 且此前只读（从不 commit）⇒ 借用它的 cursor 写会被回滚。
                # 独立连接 = autocommit，写完即生效，且与主读路径的事务语义解耦。
                try:
                    import caliber as _cal
                    # 2026-10-06（t26）**两处同族缺陷一起修**：
                    # ① **不 merge `refs` 的读法**：原实现自己
                    #    `yaml.safe_load(open(~/.dsh/.credentials.yaml))` 再取**顶层键**
                    #    `TRINITY_PG_USER/PASSWORD` —— 而该文件自 2026-09-18 起是版本化结构
                    #    （`version`/`refs`/`records`，真键缩进在 `refs` 下）⇒ 顶层取不到 ⇒
                    #    口令空串 ⇒ 连接**必失败**（实测 `fe_sendauth: no password supplied`）。
                    #    现改走本文件**既有的统一入口** `_pg()`（→ `scripts/_pg_std.py::pg_connect`，
                    #    后者逐字做 `refs` 打底、顶层覆盖）。
                    # ② **静默降级**：原实现在 `except` 里写 `retention_level = "NA"` ——
                    #    而 "NA" 的语义是**"缺上一次读数 ⇒ 不判"**（见 `caliber.retention_alert`）
                    #    ⇒ 采样失败被伪装成一个**合法读数**。现改为显式的
                    #    `UNAVAILABLE`（+ `retention_unavailable` + `retention_error` + stderr 告警）。
                    _cx = _pg()
                    if _cx is None:
                        out["retention_level"] = "UNAVAILABLE"
                        out["retention_unavailable"] = True
                        out["retention_error"] = "PG 不可达（详见 stderr 的 [warn] _pg 行）"
                        out["retention_note"] = (
                            "保留策略判据**不可用**（PG 不可达）⇒ 本周不判。"
                            "**这不是 NA**：NA 的语义是『缺上一次读数』；把采样失败写成 NA "
                            "会让『测不出来』被读成『合法地不判』。")
                    else:
                        try:
                            _cu = _cx.cursor()
                            _cu.execute("select count(*) from utilization_samples "
                                        "where metric='archivable_candidates' and ts > now() - interval '20 hours'")
                            if int((_cu.fetchone() or [0])[0]) == 0:
                                _cu.execute("insert into utilization_samples(metric, value) "
                                            "values ('archivable_candidates', %s)", (len(_sel),))
                            _cu.execute("select value from utilization_samples "
                                        "where metric='archivable_candidates' and ts <= now() - interval '7 days' "
                                        "order by ts desc limit 1")
                            _row = _cu.fetchone()
                            _lvl, _note = _cal.retention_alert(_row[0] if _row else None, len(_sel))
                            out["retention_level"] = _lvl
                            out["retention_note"] = _note
                            out["retention_unavailable"] = False
                        except Exception as _e:  # noqa: BLE001 — 读数不许把闸门搞挂
                            out["retention_level"] = "UNAVAILABLE"
                            out["retention_unavailable"] = True
                            out["retention_error"] = "%s: %s" % (type(_e).__name__, str(_e)[:120])
                            out["retention_note"] = (
                                "保留策略判据**不可用**：采样失败（%s）⇒ 本周不判；"
                                "**不得**读成 NA（NA = 缺上一次读数）。" % type(_e).__name__)
                            print("[warn] retention 采样失败 ⇒ retention_level=UNAVAILABLE"
                                  "（不是 NA）：%s" % str(_e)[:160], file=sys.stderr)
                        finally:
                            # 2026-10-06（t41/N3）：原为 `except Exception: pass` —— 静默失败
                            # （`structure_gate` 的 silent_failure 棘轮把本文件从 3 记到 4）。
                            # 关连接失败不致命，但**必须留痕**：否则"连接泄漏/句柄没释放"
                            # 这类问题在本仓永远不可见（本仓准则：可降级，不可静默）。
                            try:
                                _cx.close()
                            except Exception as _ce:  # noqa: BLE001
                                print("[warn] retention 采样连接关闭失败（不影响本次读数，"
                                      "但可能泄漏连接）：%s: %s"
                                      % (type(_ce).__name__, str(_ce)[:120]), file=sys.stderr)
                except Exception as _e:  # noqa: BLE001 — 外层兜底（如 import caliber 失败）
                    # 与内层同理：**任何**采样不可用都必须是显式的 UNAVAILABLE，
                    # 绝不允许回落到"看起来像合法读数"的 NA。
                    out["retention_level"] = "UNAVAILABLE"
                    out["retention_unavailable"] = True
                    out["retention_error"] = "%s: %s" % (type(_e).__name__, str(_e)[:120])
                    out["retention_note"] = (
                        "保留策略判据**不可用**：%s ⇒ 本周不判；"
                        "**不得**读成 NA（NA = 缺上一次读数）。" % type(_e).__name__)
                    print("[warn] retention 采样不可用 ⇒ retention_level=UNAVAILABLE"
                          "（不是 NA）：%s" % str(_e)[:160], file=sys.stderr)
                out["archivable_note"] = ("候选池 %d ⇒ 可归档 %d（同 cold_corpus_triage 口径；"
                                          "处置：python scripts/cold_corpus_triage.py --apply）"
                                          % (len(_rows), len(_sel)))
        except Exception as e:  # noqa: BLE001 — 读数不许把闸门搞挂
            out["archivable_candidates"] = None
            out["archivable_note"] = "计算失败：%s" % str(e)[:80]
        out["retrieval_excluded_categories"] = _excl
        out["retrieval_excluded_rows"] = _excluded_n
        out["caliber_note"] = ("热路径/冷库存**只在可检索类目内**统计；被剔除的 %d 行属于"
                               "不参与语义检索的类目（它们的 value 只在<新近>，不在<被读>）"
                               ) % _excluded_n
        return out
    except Exception as e:  # noqa: BLE001
        return {"value": None, "error": str(e)[:120]}


def _u1_split(cur) -> dict:
    """U1 三拆：a = agent 命名空间被读 / b = 注入投递 / c = 管道与基准自读。

    口径（三个谓词都写死在常量里，改一个就必须重录基线）：

    | 面 | SQL 谓词 | 冻结基线 |
    |---|---|---|
    | U1-a | `last_retrieved_at > now()-24h` **且** `agent_id like 'dsh-%'` | 104 |
    | U1-c | `last_retrieved_at > now()-24h` **且** `category like 'doc:%'` | 879 |
    | 总量 | `last_retrieved_at > now()-24h` | 1487（S0） |

    U1-b 不在这里取（它来自注入账本，见 `_u1b`）—— 放在一起会让人以为
    "投递"也是从 PG 数出来的，而 PG 里**根本没有"投递"这件事**，
    只有"被检索过"（`_u2` 文档里的第 2 条口径选择就是这个区分）。

    ⚠️ 语义边界（必须连口径一起引用）：`memories.agent_id` 是记忆的**归属命名空间**
    （写入方），**不是读取方**。S0 实测 104 条里只有 12 条能在 `audit_log` 的检索命中里
    找到读取证据 ⇒ U1-a 读作「agent 命名空间内的记忆被读到（谁读的不限）」，
    不是「agent 亲自读了 104 条」。`cross_check` 给出读取方归因的窄读数供核对。

    ## ⚠️ 2026-09-30（外部审计）：**本口径在"活动存储 ≠ PG"的部署下结构上失效**

    现场（实测四步取证）：

    1. `U1-a` 数的是 **`memories.last_retrieved_at`**（本函数的 `w`），**不是** `audit_log`；
    2. **SQLite 的 `memories` 根本没有这一列**（`no such column: last_retrieved_at`）；
    3. 写这一列的是 **`trinity/adapters/_pg_touch.py`**（PostgreSQL 适配器的 touch 路径）；
    4. 实测 PG：该列**非空仅 7,554 / 71,750（10.5%）**，最新值停在 `04:51`；
       而同一时段活动存储（SQLite）的 `audit_log` 里
       `action='search_hybrid'` + `details.memory_ids` **近 1 小时有 178 行**（PG 侧 0 行）。

    ⇒ 本部署（`TRINITY_STORAGE_BACKEND` 未设 ⇒ SQLite 活动）下，**API 的读取永远不会
    touch PG 的读标记**，于是 `U1-a` 的当日值（3）与历史中位（74，n=948）
    **不是同一口径下的可比数**：窗口判据的"跌破一半"读数**由数据源决定，而不是由使用量决定**。

    ## 建议修法（**未施工**：需所有者点头 + 重录基线）

    本函数 docstring 上一行写着「口径（三个谓词都写死在常量里，**改一个就必须重录基线**）」，
    且 §955 明写 U1a 出定值棘轮是「**所有者点头=按建议执行**」。故这里只登记不擅改：

    * **按活动存储选读证据**：活动存储是 SQLite 时，读取侧读数改由**该存储的
      `audit_log`**（`action in ('search','search_hybrid') and details ? 'memory_ids'`）给出；
      活动存储是 PG 时维持 `last_retrieved_at` 口径；
    * **口径必须显式报数**：读数里带上"取自哪个存储 / 哪一列"（本仓约定：剔除/口径必须报数）；
    * **并列报出另一侧**，使口径切换**可见而非静默**；
    * **配套判据必须证明"新口径能看见旧口径看不见的读取"**（现成证据：SQLite 178 / PG 0），
      否则这次改动就只是"把红改绿"。
    """
    h = int(U1_WINDOW_HOURS)
    w = "last_retrieved_at > now() - interval '%d hours'" % h
    total = _count(cur, "select count(*) from memories where " + w)
    a = _count(cur, "select count(*) from memories where " + w
               + " and agent_id like '%s%%'" % U1A_AGENT_PREFIX)
    c = _count(cur, "select count(*) from memories where " + w
               + " and category like '%s%%'" % U1C_CATEGORY_PREFIX)
    out = {
        "window_hours": h,
        "U1a_agent_reads_24h": a,
        "U1a_agent_prefix": U1A_AGENT_PREFIX,
        "U1c_pipeline_reads_24h": c,
        "U1c_category_prefix": U1C_CATEGORY_PREFIX,
        "U1_total_reads_24h": total,
        "U1_other_reads_24h": (None if total is None else total - (a or 0) - (c or 0)),
        "note": "a 与 c 实测互斥（S0：doc:* 命中里 agent 前缀为 dsh- 的有 0 条）⇒ 可相加",
    }
    # 读取方归因的窄读数（**不参与 ratchet**，只作交叉核对 —— 口径更窄，量级差 2.5 倍）
    cc = {}
    try:
        cc["reader_attributed_mids_24h"] = _count(
            cur, "select count(distinct mid) from ("
                 " select jsonb_array_elements_text(details->'memory_ids') mid from audit_log"
                 " where action='search' and details ? 'memory_ids'"
                 "   and agent_id like '%s%%'"
                 "   and timestamp::timestamptz > now() - interval '%d hours') t"
                 % (U1A_AGENT_PREFIX, h))
    except Exception as e:  # noqa: BLE001 — 交叉核对失败不得影响主读数
        cc["reader_attributed_mids_24h"] = None
        cc["error"] = str(e)[:120]
    try:
        cc["a_with_audit_evidence"] = _count(
            cur, "select count(*) from memories m where m." + w
                 + " and m.agent_id like '%s%%' and exists ("
                   " select 1 from audit_log a where a.action='search'"
                   "   and a.details ? 'memory_ids'"
                   "   and a.details->'memory_ids' ? m.memory_id"
                   "   and a.timestamp::timestamptz > now() - interval '%d hours')"
                 % (U1A_AGENT_PREFIX, h))
    except Exception as e:  # noqa: BLE001
        cc["a_with_audit_evidence"] = None
        cc["error2"] = str(e)[:120]
    cc["note"] = ("reader_attributed_mids = 按**读取方**归因的窄读数（agent_id='dsh-*' 主体在 "
                  "audit_log 的检索命中去重数）；a_with_audit_evidence = U1-a 里**能证明被读过**"
                  "的条数。两者都只作核对：agent_id 是归属而非读取方。")
    out["cross_check"] = cc
    out["value"] = a
    return out


def _is_sqlite(cur) -> bool:
    """游标背后的连接是不是 SQLite（用于选方言）。判不出时**假定不是**（保守：走 PG 原路径）。"""
    try:
        mod = type(getattr(cur, "connection", None)).__module__ or ""
        return "sqlite3" in mod
    except Exception:  # noqa: BLE001 — 判不出就不改路径
        return False


def _u1_reader_attributed_sqlite(cur, h: int) -> dict:
    """`_u1_reader_attributed` 的 **SQLite 方言**（2026-10-02，外部审计）。

    与 PG 版**同一口径**，只换方言与时间函数：

    | 语义 | PG | SQLite（本函数） |
    |---|---|---|
    | 含 memory_ids | `details ? 'memory_ids'` | `details LIKE '%memory_ids%'` |
    | 时间窗 | `timestamp::timestamptz > now() - interval 'N hours'` | `timestamp > datetime('now','-N hours')` |
    | 展开数组 | `jsonb_array_elements_text(details->'memory_ids')` | `json_each(audit_log.details,'$.memory_ids')` |

    **为什么必须补**：本部署的活动存储是 SQLite，而该读数原先只用 PG 语法
    ⇒ **在本机根本算不出来** ⇒ U1 的"读取方"侧与覆盖率一样成了**度量盲区**
    （与 `U1a` 同一个病根：**判据的取数源比机制窄**）。
    """
    w = ("action in ('search','search_hybrid') and details LIKE '%%memory_ids%%'"
         " and timestamp > datetime('now','-%d hours')" % int(h))
    base = "action in ('search','search_hybrid') and timestamp > datetime('now','-%d hours')" % int(h)
    rows_total = _count(cur, "select count(*) from audit_log where " + base)
    rows_with_ids = _count(cur, "select count(*) from audit_log where " + w)
    mids = _count(cur, "select count(distinct je.value) from audit_log, "
                       "json_each(audit_log.details,'$.memory_ids') je where " + w)
    mids_dsh = _count(cur, "select count(distinct je.value) from audit_log, "
                           "json_each(audit_log.details,'$.memory_ids') je where " + w
                           + " and agent_id like '%s%%'" % U1A_AGENT_PREFIX)
    cov = (round((rows_with_ids or 0) / rows_total, 4) if rows_total else None)
    usable = bool(rows_with_ids)
    note = ("读取方归因（谁读了哪一条）。⚠️ 覆盖率 = 有 memory_ids 的行 / 全部检索行："
            "`search_hybrid` 直到 2026-09-17（R4）才记 memory_ids ⇒ 窗口内历史行 0 覆盖，"
            "覆盖率低时本读数**不可用**，不得读成「没人读」。"
            "⚠️ 单次至多记 10 条 ⇒ 读数是**下界**（截断），不是精确值。"
            " **[方言 = sqlite:json_each]**（与 PG 版同口径，取数源不同 ⇒ 跨存储不可直接比）。")
    if not usable:
        note = ("⚠️ **本期不可用**：窗口内 %s 条检索行中 0 条带 memory_ids"
                "（`search_hybrid` 自 R4 起才记录）⇒ 读数 0 表示「没记录」而**不是**「没人读」。"
                "需等新行累积后再看。" % rows_total) + note
    return {"value": mids, "rows_total": rows_total, "rows_with_ids": rows_with_ids,
            "coverage": cov, "usable": usable, "window_hours": h,
            "reader_side_dsh": mids_dsh, "source": "sqlite:json_each",
            "note": note + (
                " 本读数 `value` 计**任何读取方**；`reader_side_dsh` 只计读取方"
                " `agent_id like '%s-%%'`，它与 P1-0 的**归属**口径 U1-a **同前缀但不同语义**"
                "（一个问「谁读的」、一个问「记忆归谁」）⇒ 两个数必须并列引用，"
                "单看任何一个都会把「归属」与「读取」混为一谈。" % U1A_AGENT_PREFIX.rstrip("-"))}


def _u1_reader_attributed(cur, hours: int = 24) -> dict:
    """U1 的**读取方**归因读数（R4，§785.6 遗留②）——**报告项**，不动 P1-0 冻结的判据。

    ## 为什么必须与**覆盖率**一起报

    P1-0 把 U1-a 冻在 `memories.agent_id`（**归属**命名空间），并写明"归属不是读取方"。
    要给出真正的"**谁读了哪一条**"，只能从 `audit_log.details->'memory_ids'` 数 ——
    而 `search_hybrid`（**主导通路**：24h 实测 1654 行 vs `search` 1226 行）
    直到 R4 才记 `memory_ids`，此前只记 `hits` 计数。
    ⇒ 窗口内**历史行永远是 0 覆盖**。若只报"读取方读数 = 0"，会被读成"没人读"（**假低**）。
    故本读数**必须**同时给出 `rows_with_ids / rows_total`，并在覆盖率为 0 时
    显式标 `usable=false` 且说明"本期不可用"。

    ## 为什么只能声称**下界**

    与既有 `_search.py` 同形，`memory_ids` 截断到**前 10 条**
    ⇒ 单次命中 >10 时少记 ⇒ 读数是**下界**，不是精确值。这条写进 `note`。
    """
    h = int(hours)
    # 2026-10-02（外部审计 · **SQLite 方言**）：原实现只用 PG 语法
    # （`details ? 'memory_ids'` / `::timestamptz` / `jsonb_array_elements_text`），
    # 而本部署活动存储是 SQLite ⇒ 这条读数**在本机根本算不出来** ⇒ 与覆盖率一样
    # 成了**度量盲区**。补方言分支：**同一口径、只换方言**，两种存储都能出数。
    if _is_sqlite(cur):
        return _u1_reader_attributed_sqlite(cur, h)
    w = ("action in ('search','search_hybrid') and details ? 'memory_ids'"
         " and timestamp::timestamptz > now() - interval '%d hours'" % h)
    rows_total = _count(cur, "select count(*) from audit_log where "
                             "action in ('search','search_hybrid') "
                             "and timestamp::timestamptz > now() - interval '%d hours'" % h)
    rows_with_ids = _count(cur, "select count(*) from audit_log where " + w)
    mids = _count(cur, "select count(distinct mid) from ("
                       " select jsonb_array_elements_text(details->'memory_ids') mid"
                       " from audit_log where " + w + ") t")
    #: 与 P1-0 冻结的**归属**口径 U1-a 直接对照的那一个数：读数是"**读取方**属于 dsh 命名空间"
    #: （而不是"记忆归属 dsh 命名空间"）。两个数并列给出，才看得出"归属 ≠ 读取方"有多大。
    mids_dsh = _count(cur, "select count(distinct mid) from ("
                           " select jsonb_array_elements_text(details->'memory_ids') mid"
                           " from audit_log where " + w
                           + " and agent_id like '%s%%') t" % U1A_AGENT_PREFIX)
    cov = (round((rows_with_ids or 0) / rows_total, 4) if rows_total else None)
    usable = bool(rows_with_ids)
    note = ("读取方归因（谁读了哪一条）。⚠️ 覆盖率 = 有 memory_ids 的行 / 全部检索行："
            "`search_hybrid` 直到 2026-09-17（R4）才记 memory_ids ⇒ 窗口内历史行 0 覆盖，"
            "覆盖率低时本读数**不可用**，不得读成「没人读」。"
            "⚠️ 单次至多记 10 条 ⇒ 读数是**下界**（截断），不是精确值。")
    if not usable:
        note = ("⚠️ **本期不可用**：窗口内 %s 条检索行中 0 条带 memory_ids"
                "（`search_hybrid` 自 R4 起才记录）⇒ 读数 0 表示「没记录」而**不是**「没人读」。"
                "需等新行累积后再看。" % rows_total) + note
    return {"value": mids, "rows_total": rows_total, "rows_with_ids": rows_with_ids,
            "coverage": cov, "usable": usable, "window_hours": h,
            "reader_side_dsh": mids_dsh,
            "note": note + (
                " 本读数 `value` 计**任何读取方**；`reader_side_dsh` 只计读取方"
                " `agent_id like '%s-%%'`，它与 P1-0 的**归属**口径 U1-a **同前缀但不同语义**"
                "（一个问「谁读的」、一个问「记忆归谁」）⇒ 两个数必须并列引用，"
                "单看任何一个都会把「归属」与「读取」混为一谈。" % U1A_AGENT_PREFIX.rstrip("-"))}


def _u1b() -> dict:
    """U1-b 注入投递：来自**注入账本**（不是 PG —— PG 里没有"投递"这件事）。

    口径与 U2 同源（`delivered_total` / `cold_delivered`），这里只把它摆进 U1 的名字空间，
    因为"U1 覆盖率"与"U1-b 真投递"曾被混为一谈（735 §735.5 的原始病因）。
    """
    ledger = _read_json(LEDGER) or {}
    d = int(ledger.get("delivered_total") or 0)
    cold = int(ledger.get("cold_delivered") or 0)
    return {"value": d, "U1b_delivered": d, "U1b_cold": cold,
            "ledger_epoch": ledger.get("first_ts"),
            "injection_ledger_present": bool(ledger),
            "note": "U1-b 与 U2 同源（注入账本）；PG 的 last_retrieved_at 分不出「检索到」与「投递到」"}


def _u1(conn) -> dict:
    """U1 检索覆盖率。口径：last_retrieved_at（NULL = 从未检索）。"""
    cur = conn.cursor()
    cur.execute("select column_name from information_schema.columns where table_name='memories'")
    cols = {r[0] for r in cur.fetchall()}
    active = _count(cur, "select count(*) from memories where status='active'")
    out = {"active_total": active, "coverage_source": "last_retrieved_at"}
    if "last_retrieved_at" not in cols:
        out["error"] = "memories.last_retrieved_at 不存在（需先跑 migrate_last_retrieved.py）"
        return out
    ever = _count(cur, "select count(*) from memories where status='active' and last_retrieved_at is not null")
    d30 = _count(cur, "select count(*) from memories where status='active' "
                      "and last_retrieved_at > now() - interval '30 days'")
    out["retrieved_ever"] = ever
    out["retrieved_30d"] = d30
    if active:
        out["coverage_ever"] = round((ever or 0) / active, 4)
        out["coverage_30d"] = round((d30 or 0) / active, 4)
        #: 头号读数（ratchet 判据用这个）：近 30 天覆盖率
        out["value"] = out["coverage_30d"]
    # 交叉核对（不参与 ratchet）：access_count 口径与 last_retrieved_at 口径是否自洽
    out["cross_check"] = {
        "access_count_gt0": _count(cur, "select count(*) from memories where status='active' "
                                        "and coalesce(access_count,0) > 0"),
        "sum_access_count": _count(cur, "select coalesce(sum(access_count),0) from memories "
                                        "where status='active'"),
        "note": "access_count 口径仅交叉核对；last_accessed_at 因 DEFAULT NOW() 缺陷不可用作覆盖率",
    }
    # ── 生产面并列读数（§916）：**只增列，不动 value** ──────────────────────
    _np = NONPROD_SCOPE_SQL
    act_p = _count(cur, "select count(*) from memories where status='active' and not (" + _np + ")")
    ever_p = _count(cur, "select count(*) from memories where status='active' "
                         "and last_retrieved_at is not null and not (" + _np + ")")
    d30_p = _count(cur, "select count(*) from memories where status='active' "
                        "and last_retrieved_at > now() - interval '30 days' and not (" + _np + ")")
    out["nonprod_scope"] = _np
    out["active_prod"] = act_p
    out["retrieved_ever_prod"] = ever_p
    out["retrieved_30d_prod"] = d30_p
    if act_p:
        out["coverage_ever_prod"] = round((ever_p or 0) / act_p, 4)
        out["coverage_30d_prod"] = round((d30_p or 0) / act_p, 4)
        out["prod_note"] = ("生产面口径（剔除公开评测语料）：与全库口径并列展示；"
                            "value/coverage_30d 仍是全库口径，未被本项改动（§916）")
        # 决策开关（§917）：默认 all ⇒ 下面这行**不生效**；置 TRINITY_UTIL_SCOPE=prod 才切。
        if util_scope() == "prod":
            out["scope"] = "prod"
            out["value_all_db"] = out.get("value")
            out["value"] = out["coverage_30d_prod"]
            out["scope_note"] = ("ratchet 口径已切生产面（TRINITY_UTIL_SCOPE=prod）⇒ "
                                 "**必须重建基线**（旧基线是全库口径，不可比）")
    return out


def _u2(conn) -> dict:
    """U2 投递量。分两级：引擎浮现（surfaced）与真正投递到模型上下文（delivered）。"""
    oc = _read_json(OPENING_COUNTERS) or {}
    ledger = _read_json(LEDGER)
    out = {
        #: 引擎侧：worker._opening() 成功浮现的条数（累计）
        "surfaced_by_engine": int(oc.get("surfaced_total") or 0),
        "opening_calls": int(oc.get("calls") or 0),
        "opening_by_reason": oc.get("by_reason") or {},
        #: 宿主侧：真正进入模型上下文的条数（累计）——**U2 取这个**
        "delivered_to_context": int((ledger or {}).get("delivered_total") or 0),
        "injection_ledger": {"path": LEDGER, "present": ledger is not None},
    }
    out["value"] = out["delivered_to_context"]
    #: P0-1/P1-1：**来源拆分**（谁在调 `opening`）。
    #: 实测动机（2026-09-17）：`calls=307` / 注入账本 `calls=1` —— 一个计数器把
    #: **真实 DSH 会话**与**各轮探针**混成一个数，于是"注入通路在生产上到底跑没跑"
    #: 这个问题**没有判别力**，利用率读的是**探针自己制造的热度**。
    #: 分桶由 `engine_worker._opening_bump(origin=...)` 写入；插件自报 `dsh-plugin`。
    #: ⚠️ 老版本 counters 没有 `by_origin`（本项之前的累计量无法回溯拆分）⇒ 该情形下
    #: 明确标注 `legacy_no_origin=true`，**不得**把缺字段读成"生产为 0"。
    _bo = oc.get("by_origin") or {}
    _prod = int((_bo.get(ORIGIN_PLUGIN) or {}).get("calls") or 0)
    _unk = int((_bo.get(ORIGIN_UNKNOWN) or {}).get("calls") or 0)
    _probe = sum(int((v or {}).get("calls") or 0) for k, v in _bo.items()
                 if str(k).startswith("probe:"))
    out["by_origin"] = _bo
    out["origin_split"] = {
        "production_calls": _prod,
        "probe_calls": _probe,
        "unknown_calls": _unk,
        "legacy_no_origin": (not _bo),
        "note": ("`dsh-plugin` = 唯一的**生产**来源（宿主注入通路）；`probe:*` = 探针自报；"
                 "其余 = unknown。**未带 origin 的历史累计量无法回溯拆分**，"
                 "故 by_origin 缺席时标 legacy，不读成『生产为 0』。"),
    }
    if conn is not None:
        cur = conn.cursor()
        active = _count(cur, "select count(*) from memories where status='active'")
        if active:
            out["delivery_ratio"] = round(out["delivered_to_context"] / active, 6)
            out["surfaced_ratio"] = round(out["surfaced_by_engine"] / active, 6)
    # 前置问题：注入通路到底有没有？（区分"通路不存在"与"通路存在但没投递"）
    paths = []
    for rel, marker, why in INJECTION_MARKERS:
        p = os.path.join(ROOT, rel)
        try:
            hit = os.path.exists(p) and marker in open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            hit = False
        paths.append({"file": rel, "marker": marker, "why": why, "present": bool(hit)})
    out["injection_paths"] = paths
    out["injection_paths_present"] = sum(1 for p in paths if p["present"])
    return out


def _u3(conn) -> dict:
    """U3 写读比。

    2026-09-16 第三次自纠（与 U4「累计→均值」同族）：**被 ratchet 的必须是 7 日窗口比**。
    实测漂移：30 天累计比在 2 分钟内从 16.8299 涨到 16.8842（+0.054）——因为
    `writes_30d` 每分钟涨约 20 行而 `reads_distinct_30d` 几乎不动 ⇒ **约 9 分钟就吃掉
    0.05 的容差**，闸门会长期红。恒红闸门=被忽略的闸门（本仓 G10 前科），
    故累计比只作报告，ratchet 判 **7 日窗口比**（分子分母同窗移动，结构性漂移抵消）。
    """
    cur = conn.cursor()
    writes = _count(cur, "select count(*) from memories where created_at > now() - interval '30 days'")
    reads = _count(cur, "select count(*) from memories where last_retrieved_at > now() - interval '30 days'")
    w7 = _count(cur, "select count(*) from memories where created_at > now() - interval '7 days'")
    r7 = _count(cur, "select count(*) from memories where last_retrieved_at > now() - interval '7 days'")
    out = {"writes_30d": writes, "reads_distinct_30d": reads,
           "writes_7d": w7, "reads_distinct_7d": r7,
           "note": "写=新建行数；读=被检索到的**去重**行数；ratchet 判 7 日窗口比（累计比结构性漂移）"}
    # ── 生产面并列读数（§916）：评测语料写入（如 19,195 行 LongMemEval）会把「写」抬起来，
    #    而它**不是**生产写放大 ⇒ 并列展示生产面比值，**不改被 ratchet 的 value**。
    _np = NONPROD_SCOPE_SQL
    w7p = _count(cur, "select count(*) from memories where created_at > now() - interval '7 days' "
                      "and not (" + _np + ")")
    r7p = _count(cur, "select count(*) from memories where last_retrieved_at > now() - interval '7 days' "
                      "and not (" + _np + ")")
    out["writes_7d_prod"] = w7p
    out["reads_distinct_7d_prod"] = r7p
    if w7p is not None and r7p:
        out["ratio_7d_prod"] = round(w7p / r7p, 4)
        # 决策开关（§917）：默认 all ⇒ 不生效
        if util_scope() == "prod":
            out["scope"] = "prod"
            out["value_all_db"] = out.get("value")
            out["value"] = out["ratio_7d_prod"]
            out["scope_note"] = "ratchet 口径已切生产面 ⇒ 必须重建基线（§917）"
    if writes is not None and reads:
        out["cumulative_ratio"] = round(writes / reads, 4)
    if w7 is not None and r7:
        out["value"] = round(w7 / r7, 4)
        out["window"] = "7d"
    else:
        out["value"] = None
        out["error"] = "7 日读数为 0 或不可读 ⇒ 比值无定义（不是 0）"
    out["cumulative"] = {
        "active_rows": _count(cur, "select count(*) from memories where status='active'"),
        "read_events_total": _count(cur, "select coalesce(sum(access_count),0) from memories"),
    }
    return out


def _u4() -> dict:
    """U4 注入 token。**天花板指标** —— 由 ratchet 判"不得升高"。

    2026-09-16 自纠（P0-1 接线后第一次跑闸门就暴露）：
    初版把 **累计** `tokens_est_total` 当被 ratchet 的量，于是 P0-1 一接线它就从 0 涨到 509
    ⇒ 闸门红。这不是"接线错了"，而是**判据设计错了**：累计量只会单调上升，
    拿它做天花板必然**天天红**（恒红闸门=被忽略的闸门，本仓已有 G10 前科）。
    故改为 ratchet **每次投递的 token 均值**（真正的"注入成本强度"），累计量只作报告。
    另加 A5 规格硬预算（Memory Atlas ≤1500 token/次，见 OPT-REMAINING A5）作为绝对上限。
    """
    ledger = _read_json(LEDGER)
    budget = 1500
    out = {"injection_ledger_present": ledger is not None, "token_estimator": "chars/4",
           "token_budget_per_delivery": budget}
    if not ledger:
        out["value"] = 0
        out["tokens_per_delivery"] = 0.0
        out["chars_total"] = 0
        out["note"] = "无注入账本 ⇒ 0（= 注入通路未接线，不是「未测」）"
        return out
    calls = int(ledger.get("calls") or 0)
    out["value"] = int(ledger.get("tokens_est_total") or 0)
    out["calls"] = calls
    out["chars_total"] = int(ledger.get("chars_total") or 0)
    out["tokens_per_delivery"] = round(out["value"] / calls, 2) if calls else 0.0
    out["within_budget"] = (out["tokens_per_delivery"] <= budget) if calls else True
    # ── 2026-10-02：**生产面**口径（`by_origin` 由插件记账，见 dsh-plugin .../index.js）──────
    # 为什么需要：本账本此前只有**全局累计**，而实测 `calls=335` 里 **`dsh-plugin`（生产）仅 132、
    #   `probe:*`（探针）463** ⇒ `tokens_per_delivery` 主要在度量**探针**，而它是被 ratchet 当
    #   "注入成本强度"判的量（实测 355→430 判红，实为探针注入把均值拉高）。
    # 口径规则：`dsh-plugin` = **唯一生产来源**（宿主注入通路），`probe:*` = 探针自报。
    # 无 `by_origin` 时**显式标注回落**，绝不静默冒充生产值。
    _bo = ledger.get("by_origin")
    if isinstance(_bo, dict) and _bo:
        prod = _bo.get("dsh-plugin") or {}
        pcalls = int(prod.get("calls") or 0)
        ptokens = int(prod.get("tokens") or 0)
        out["by_origin"] = {k: v for k, v in _bo.items()}
        out["origin_split"] = {
            "production_origin": "dsh-plugin",
            "production_calls": pcalls,
            "production_tokens": ptokens,
            "tokens_per_delivery_prod": (round(ptokens / pcalls, 2) if pcalls else None),
            "probe_calls": sum(int((v or {}).get("calls") or 0)
                               for k, v in _bo.items() if str(k).startswith("probe:")),
            "note": ("生产面口径 = by_origin['dsh-plugin']；探针（probe:*）不计入。"
                     "该字段为**报告型**：棘轮仍按全局 tokens_per_delivery 判（切生产面口径"
                     "属于口径变更，需按 §13 重建基线并留痕，不在本次自动执行）。"),
        }
    else:
        out["origin_split"] = {
            "production_origin": "dsh-plugin",
            "tokens_per_delivery_prod": None,
            "note": ("账本无 `by_origin` ⇒ **无法**算生产面口径（显式回落，不冒充）。"
                     "插件记账版本早于 2026-10-02，下次注入后该字段自然出现。"),
        }
    out["latency_ms"] = ledger.get("latency_ms") or {}
    out["assembly_ms"] = ledger.get("assembly_ms") or {}
    return out


def _u3_retrieval_calls() -> dict:
    """辅助读数（不参与 ratchet）：API 侧检索调用量。用于解释 U3 的分母。"""
    c = _read_json(API_COUNTERS) or {}
    return {"queries_total": int(c.get("queries_total") or 0),
            "by_source": c.get("by_source") or {},
            "started_ts": c.get("started_ts")}


def run() -> dict:
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "script": "memory_utilization_audit"}
    conn = _pg()
    res["pg_available"] = conn is not None
    try:
        if conn is not None:
            _cur = conn.cursor()
            res["U1_retrieval_coverage"] = _u1(conn)
            res["U1_trend"] = _u1_trend(_cur)
            #: P1-0：U1 三拆（ratchet 只认 a+b；c 降级为报告）
            res["U1_split"] = _u1_split(_cur)
            #: 2026-09-21（§1015）：**冷库存归因**（报告项；不进 headline/ratchet ——
            #: 它是「读哪个数去判检索健康」的口径说明，不是新的棘轮判据）
            res["U1_attrib"] = _u1_attribution(_cur)
        else:
            res["U1_retrieval_coverage"] = {"value": None, "error": "PG 不可达"}
            res["U1_trend"] = {"value": None, "error": "PG 不可达", "series": []}
            res["U1_split"] = {"value": None, "error": "PG 不可达",
                               "U1a_agent_reads_24h": None, "U1c_pipeline_reads_24h": None}
            res["U1_attrib"] = {"value": None, "error": "PG 不可达"}
        res["U1b_injection"] = _u1b()
        #: R4（§785.6 遗留②）：**读取方**归因读数（报告项；与 P1-0 冻的归属口径并列）
        if conn is not None:
            res["U1a_reader_attributed"] = _u1_reader_attributed(conn.cursor())
        else:
            res["U1a_reader_attributed"] = {"value": None, "usable": False,
                                            "error": "PG 不可达"}
        res["U2_delivery"] = _u2(conn)
        res["U2b_cold_delivery"] = _u2b_cold()
        res["U3_write_read_ratio"] = _u3(conn) if conn is not None else {"value": None, "error": "PG 不可达"}
        res["U4_injected_tokens"] = _u4()
        res["aux_retrieval_calls"] = _u3_retrieval_calls()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    res["headline"] = {
        "U1_coverage_30d": (res["U1_retrieval_coverage"] or {}).get("value"),
        #: 建议①（2026-09-16）：判据从"点值"升级为"近 7 日日新增均值"——
        #: S0 实测点值日变化 0.0002，而日序列极差/均值 6.58 ⇒ 点判据必然被噪声淹没。
        #: P1-0（2026-09-16）：**再降一级为报告项** —— 它是**全量**趋势，
        #: 被 doc 管道自读（U1-c）驱动，S0 实测一天 +71% 而投递量纹丝不动。
        "U1_daily_reads_7d": (res["U1_trend"] or {}).get("value"),
        #: ── P1-0：U1 三拆（ratchet 只认 a+b）──────────────────────────────
        "U1a_agent_reads_24h": (res.get("U1_split") or {}).get("U1a_agent_reads_24h"),
        "U1b_delivered": (res.get("U1b_injection") or {}).get("U1b_delivered"),
        "U1b_cold": (res.get("U1b_injection") or {}).get("U1b_cold"),
        #: U1-c 进 headline 只为"基线里能冻住 879"与报告可读；**不参与 ratchet**。
        "U1c_pipeline_reads_24h": (res.get("U1_split") or {}).get("U1c_pipeline_reads_24h"),
        #: R4：U1 的**读取方**归因读数（与上面的**归属**口径 U1-a 并列，仅报告）
        "U1a_reader_attributed_24h": (res.get("U1a_reader_attributed") or {}).get("value"),
        "U2_delivered": (res["U2_delivery"] or {}).get("value"),
        #: U2b 冷投递量：自动召回捞回的"从未被检索过"的记忆数（核心价值口径）。
        "U2b_cold_delivered": (res["U2b_cold_delivery"] or {}).get("value"),
        #: 2026-09-21（§1024）：**把账本纪元写进基线**。§929 的守卫读的就是 `headline.ledger_epoch`，
        #: 但这一行此前**从未存在** ⇒ 守卫恒 `_epoch_changed=False`（死代码），
        #: 后果实测：09-20 18:39 录的基线（1985）与今天新会话的账本（435）直接比大小
        #: ⇒ 闸门报"U1b/U2/U2b 下降"的**假红**（会话级计数器跨纪元本就不可比）。
        "ledger_epoch": (res["U2b_cold_delivery"] or {}).get("ledger_epoch"),
        #: 第三次自纠：ratchet 判 7 日窗口比；30 天累计比只作报告（结构性漂移）
        "U3_write_read_7d": (res["U3_write_read_ratio"] or {}).get("value"),
        "U3_write_read_ratio": (res["U3_write_read_ratio"] or {}).get("cumulative_ratio"),
        #: U4 报**累计**（人读的成本总量）…
        "U4_injected_tokens": (res["U4_injected_tokens"] or {}).get("value"),
        #: …但 ratchet 判**每次投递均值**：累计量单调上升，做天花板必然天天红（见 _u4 自纠）。
        "U4_tokens_per_delivery": (res["U4_injected_tokens"] or {}).get("tokens_per_delivery"),
        #: 2026-10-02：**生产面**口径（by_origin['dsh-plugin']；探针不计入）。
        #: 仅当插件账本已带 by_origin 时非 None；**不进棘轮**（切口径需重建基线并留痕）。
        "U4_tokens_per_delivery_prod": (
            (res["U4_injected_tokens"] or {}).get("origin_split") or {}
        ).get("tokens_per_delivery_prod"),
    }
    return res


# ── ratchet ────────────────────────────────────────────────────────────────
# 方向性判据（不是"越大越好"一刀切）：
#   U1-a/U1-b/U2/U2b 越高越好 -> 低于基线 - tol 即失败
#   U3    越低越好 -> 高于基线 + tol 即失败
#   U4    是成本   -> 高于基线 + tol 即失败（天花板）
#                    被 ratchet 的是**每次投递均值**，不是累计量（自纠见 _u4 文档）
#
# 2026-09-16（P1-0）：**U1 家族只认 a+b**。
#   · 移出 `U1_daily_reads_7d` / `U1_coverage_30d`：两者都是**全量**读数，被
#     U1-c（doc 管道自读，实测 879/24h）驱动 —— S0 实测 7 日均值一天 +71%
#     而投递量纹丝不动 ⇒ 判据在动、利用率没动（噪声驱动的闸门）。降级为报告项，
#     并在 ratchet 输出里**点名**（静默降级＝偷偷删判据，见 REPORT_ONLY）。
#   · 新增 `U1a_agent_reads_24h`（容差 10 条，与旧 7 日均值同量级）与
#     `U1b_delivered`（容差 0：投递量没有"合理波动"，掉一条就是接线少了一条）。
DIRECTIONS = {
    #: U1-a：agent 命名空间内的记忆在 24h 内被读到的条数。24h 滑窗 ⇒ 容差 10 条。
    # ⚠️ U1a_agent_reads_24h **已移出定值棘轮**（2026-09-20 §955，所有者点头=按建议执行）：
    # 它是**滚动 24h 窗口**读数，对定值基线判"不得下降"必然随窗口漂移而红 ——
    # 实测：18:38 重录基线，18:39 又红（§954）。改判据见 WINDOW_JUDGED（与 7 天前**同窗口**比）。
    #: U1-b：真正投递进模型上下文的条数（与 U2 同源）。掉一条就是掉一条。
    "U1b_delivered": ("higher", 0),
    "U2_delivered": ("higher", 0),
    "U2b_cold_delivered": ("higher", 0),
    #: 第三次自纠：判 7 日窗口比而非 30 天累计比（后者约 9 分钟吃掉 0.05 容差 ⇒ 恒红）
    "U3_write_read_7d": ("lower", 1.0),
    "U4_tokens_per_delivery": ("lower", 64),
}

#: **报告项**：仍出数、仍进基线文件、但**不参与 ratchet**。必须连同理由一起列出，
#: 且 ratchet 每次都要把它们打印出来 —— 否则就是"静默降级"（= 偷偷删判据）。
#: 2026-09-20（§955）：**窗口指标改同窗口对比**（所有者点头的执行）。
#: 理由：滚动窗口读数 vs 定值基线 = 判据不成立（§954 实测一分钟内二次变红）。
#: 判据：与「7 天前最接近的一次采样」比，**跌破一半**或**为 0** 才判 FAIL；
#: 没有 ≥6 天前的样本就**明说样本不足、不判**（不猜、不造假红）。
WINDOW_JUDGED = {
    "U1a_agent_reads_24h": "滚动 24h 窗口读数 ⇒ 与 7 天前同窗口比（≥50% 且不为 0），不对定值基线判下降",
    #: 2026-10-02（外部审计 ④b）**由 REPORT_ONLY 升为判据**。
    #: 本项目自己写的前置条件是「等**覆盖率**达到可判水平**再议**是否升为判据」——
    #: 实测近 24h 覆盖率 **100.0%**（33/33）、近 30d **96.66%**（2836/2934），
    #: 且 `memory_ids` 自 **2026-08-13** 起持续记录 ⇒ **前置条件已满足**，本轮据此升级。
    #: **并增、不替换** P1-0 的归属口径（`U1a_agent_reads_24h` 仍在上一行被判）——
    #: 项目明写"直接换判据键等于把 P1-0 冻的东西偷偷换掉"。
    #: 取数源 = `audit_log`（活动 SQLite 库内）⇒ **在 SQLite 部署上也可判**
    #: （此前它在 SQLite 上一律 `null`，是**度量盲区**）。
    "U1a_reader_attributed_24h": "读取方归因（audit_log.memory_ids，取前 10 ⇒ **下界**）；"
                                 "与 6–21 天前的中位数比（≥50% 且不为 0）；"
                                 "**取数源与 PG 版不同 ⇒ 跨存储不可直接比**",
}

REPORT_ONLY = {
    "U1c_pipeline_reads_24h": "管道与基准自读（doc:* 分类，实测全部由 doc-fusion 读出）；"
                              "它是**污染源**不是利用率 —— 拿它做判据 = 管道一忙闸门就绿",
    #: R4：**读取方**归因读数。与 P1-0 冻结的**归属**口径 U1-a 并列存在，不替换它 ——
    #: 直接换判据键等于把 P1-0 冻的东西偷偷换掉（本仓对"为了让闸门好看而换尺子"的态度是明确的）。
    #: 它只有等 `search_hybrid` 新行累积出覆盖率之后才可用（字段自带 usable/coverage）。
    #: 2026-10-02（外部审计 ④b）：**本键已升为判据**（见上方 `WINDOW_JUDGED`），
    #: 故从本表移除。原报告项理由（"等覆盖率达到可判水平再议"）**已被满足**：
    #: 实测近 24h 覆盖率 100.0%（33/33）、近 30d 96.66%（2836/2934）。
    #: 留此注释是为了**可追溯**：这个键曾经在这里，为什么移走、依据是什么。
    "U1_daily_reads_7d": "全量日趋势（含 U1-c）。S0 实测一天 199.29→341.71（+71%）"
                         "而 U2 投递量不变 ⇒ 判据在动、利用率没动",
    "U1_coverage_30d": "全量 30 天点值（含 U1-c）。自身文档已记：日变化仅 0.0002，"
                       "在 6.58× 极差下数学上测不出机制效果",
}


#: 基线文件的固定说明（重录时保留，避免"抬高基线"顺手把口径说明删掉）。
BASELINE_NOTE = ("利用率基线；U1-a/U1-b/U2/U2b 下降、U3/U4 上升会使 CI 失败。"
                 "U1-c 与全量 U1 读数只作报告（REPORT_ONLY）。"
                 "口径（all/prod）必须与 docs/GATE_SET.json 的 --scope 同源，改口径必须带理由重录。")


def _u1a_window_compare(cur, old, n_ref=None):
    """纯判据（§955）：同窗口对比。返回 (ok, note)。old 为历史参照值（**稳健中位数**）。

    判据：**跌破一半**或**为 0** ⇒ FAIL；否则 PASS（比值一并写进注记，便于人工核）。
    纯函数：不碰 DB/文件，便于 S1 测试（先证明它会红）。

    2026-09-30（外部审计 · **口径修正**，有实测依据）：参照值由「6 天前的**单个**样本」
    改为「6–21 天窗口的**中位数**」。理由是本指标**自身**的历史分布无法支撑单点对比 ——

        实测（utilization_samples，n=40）：中位 51.5、均值 55.5、**stdev 54.1**、
        min**3** / max**150**，且呈**双峰**（低值 3 与高值 100–150 交替出现）。

    在这种方差下，"拿 6 天前的那一个样本当基准"会把**参照点自身的波动**读成退化：
    只要那个点恰好落在高值区，当天就会误报（实测：参照 104，当日 3 ⇒ 红）。
    中位数对离群点稳健，而**持续性**塌陷仍会被抓住（连续多天低 ⇒ 中位数也低 ⇒ 仍红）。
    样本不足以算中位数时 ⇒ 返回 None（**不判**），绝不把"没测到"读成"过了"。
    """
    try:
        cur, old = int(cur), int(old)
    except Exception:  # noqa: BLE001
        return None, "U1a 读数非法 ⇒ 不判"
    _tag = "中位" if n_ref else "7天前"
    _ref = "（参照 n=%s）" % n_ref if n_ref else ""
    if cur == 0:
        return False, "U1a=0 ⇒ 读取侧塌陷（同窗口判据%s）" % _ref
    if old > 0 and cur < 0.5 * old:
        return False, "U1a %d vs 历史%s %d ⇒ 跌破一半（同窗口判据%s）" % (cur, _tag, old, _ref)
    return True, "U1a %d vs 历史%s %d ⇒ %d%%（同窗口判据，≥50%% 通过%s）" % (
        cur, _tag, old, int(round(100.0 * cur / old)) if old else 0, _ref)


def _sqlite_store_path() -> str:
    """活动 SQLite 库路径（与 `scripts/coverage_proxy_sqlite.py` 同源）。"""
    return os.environ.get("TRINITY_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")


def _u1a_samples_sqlite(metric: str, cur_value: int, days_back: int = 30):
    """**SQLite 侧**的窗口判据采样（2026-10-02，外部审计 ④b）。

    ## 为什么要从 `audit_log` **反推历史**

    PG 版靠 `utilization_samples` 逐日累积；本部署的活动存储是 SQLite，
    那张表**不存在** ⇒ 首跑必然「样本不足 ⇒ 不判」。若就那样等 6 天，
    判据会在**一周内都是"不判"**（等于没判）。

    但 `audit_log` 里**已经有近两个月**的检索记账（实测首条带 `memory_ids` 的行是
    **2026-08-13**）⇒ 可以**按同一定义、同取数源**算出过去每一天的 24h 窗口值。
    **这不是造数**：它是**同源同口径的派生**，与"今天这条读数"是同一个函数在历史时点的取值。

    ## 口径

    `metric` 决定怎么算。目前只服务读取方归因口径：
    `distinct memory_ids`（**含预热** —— 与线上读数同为"原样"口径，剔除做在报告侧，
    以免判据口径与报告口径不一致）。
    """
    p = _sqlite_store_path()
    if not os.path.exists(p):
        raise RuntimeError("SQLite 库不存在：%s" % p)
    con = sqlite3.connect(p, timeout=60)
    try:
        cur = con.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS utilization_samples("
                    "metric TEXT, value INTEGER, ts TEXT)")
        have = cur.execute("SELECT COUNT(*) FROM utilization_samples WHERE metric=?",
                           (metric,)).fetchone()[0]
        boot = 0
        if not have:
            # 逐日反推：对每个历史日 D，算 [D-1d, D] 窗口内的读取方去重数。
            for d in range(1, int(days_back) + 1):
                row = cur.execute(
                    "SELECT COUNT(DISTINCT je.value) FROM audit_log, "
                    "json_each(audit_log.details,'$.memory_ids') je "
                    "WHERE audit_log.action IN ('search','search_hybrid') "
                    "AND audit_log.details LIKE '%memory_ids%' "
                    "AND audit_log.timestamp > datetime('now', ?) "
                    "AND audit_log.timestamp <= datetime('now', ?)",
                    ("-%d days" % (d + 1), "-%d days" % d)).fetchone()
                v = int(row[0] or 0)
                cur.execute("INSERT INTO utilization_samples(metric, value, ts) "
                            "VALUES (?,?, datetime('now', ?))", (metric, v, "-%d days" % d))
                boot += 1
            con.commit()
        cur.execute("INSERT INTO utilization_samples(metric, value, ts) "
                    "VALUES (?,?, datetime('now'))", (metric, int(cur_value)))
        refs = [int(r[0]) for r in cur.execute(
            "SELECT value FROM utilization_samples WHERE metric=? "
            "AND ts < datetime('now','-6 days') AND ts > datetime('now','-21 days')",
            (metric,)).fetchall() if r[0] is not None]
        con.commit()
    finally:
        con.close()
    return refs, boot


def _sqlite_side_is_active(hours: int = 24) -> bool:
    """**活动写入侧是不是 SQLite**（2026-10-02，外部审计 · 双向「存储纪元」判据）。

    ## 为什么需要"反向"判断

    原先只在 `resolve_backend()` **不是** postgres 时判"纪元不适用"。
    实测（凭据修好后）backend 解析成 `postgresql`，而 **API 的读写其实落在 SQLite**
    ⇒ PG 的读标记列 `last_retrieved_at` **不被维护、自然衰减**，
    于是窗口判据拿"PG 纪元的历史中位数"去比，读出**假退化**：

        FAIL(窗口) U1a_agent_reads_24h  3 vs 历史中位 93（参照 n=1017）

    ## 判据（用**证据**而不是配置）

    活动 SQLite 库里**近 `hours` 小时有 audit_log 行** ⇒ 写入侧就是 SQLite。
    这比"相信某个环境变量"可靠：**它看的是真的有数据在往里写**。
    """
    p = _sqlite_store_path()
    if not os.path.exists(p):
        return False
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
        try:
            n = con.execute("SELECT COUNT(*) FROM audit_log "
                            "WHERE timestamp > datetime('now', ?)",
                            ("-%d hours" % int(hours),)).fetchone()[0]
        finally:
            con.close()
        return int(n or 0) > 0
    except Exception:  # noqa: BLE001 — 查不出就当"不是活动侧"（保守：维持原判据）
        return False


def _u1a_window_judge(cur_value, metric: str = "U1a_agent_reads_24h"):
    """U1a 同窗口判据的 PG 版（§955）：写采样 + 取**稳健历史参照** + 调纯判据。

    无 PG（CI）⇒ 返回 (None, 原因)：**明说不判**，不是通过。
    为什么放 PG 不放 state/*.json：state 目录下每个文件都被 organ_freeze 当器官（要登记+消费者），
    而这是测量数据不是机制状态 —— §933/§945 已各踩过一次（本轮是第三次，故直接换存储）。
    """
    if cur_value is None:
        return None, "U1a 无读数（缺 PG/账本）⇒ 不判"
    # 2026-09-30（外部审计 · **存储纪元**）：读标记列 `memories.last_retrieved_at` 是
    # **PG 独有**且**只由 PG 适配器的 touch 路径写**（`trinity/adapters/_pg_touch.py`）。
    # 本部署活动存储是 SQLite 时，API 的检索**不会**去 touch 它 ⇒ 该列不再是
    # "API 读取"的标记，`U1-a` 的当日值与历史样本（同一口径的产物）**不可比**。
    # 与 U2 的"账本纪元"同款处置（本文件 §929 的既有约定）：
    # **遇到纪元变化就不判该项，并显式点名，不静默通过**。
    try:
        import os as _os2
        import sys as _sys2
        _sd2 = _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__)))
        if _sd2 not in _sys2.path:
            _sys2.path.insert(0, _sd2)
        from trinity.security.credentials import resolve_backend as _rb
        _backend = (_rb() or "sqlite").strip().lower()
    except Exception:  # noqa: BLE001 — 判不出就不拦（保守：维持原判据）
        _backend = "postgresql"
    # 2026-10-02（外部审计 ④b）：**按"量的取数源"分流**，而不是一律不判。
    #
    # · `U1a_agent_reads_24h`（P1-0 冻结的**归属**口径，读 PG 独有列
    #   `memories.last_retrieved_at`）⇒ 在 SQLite 上**仍然不判**（纪元不适用，理由见下）；
    # · `U1a_reader_attributed_24h`（**读取方**口径，走 `audit_log.details.memory_ids`，
    #   而 `audit_log` 就在活动 SQLite 库里）⇒ 改用 **SQLite 侧采样**来判。
    #   升级依据（本项目自己写的前置条件："等**覆盖率**达到可判水平再议是否升为判据"）：
    #   实测近 24h 覆盖率 **100.0%**（33/33）、近 30d **96.66%**（2836/2934），
    #   且 `memory_ids` 自 **2026-08-13** 起就在记录 ⇒ 前置条件满足。
    #   **不替换** P1-0 那个键（项目明说"不替换，否则等于偷偷换尺子"）——是**并增**。
    #
    # ⚠️ 2026-10-02（**反向纪元**，实测踩到）：backend **解析成 postgres 也可能是"假 PG 侧"** ——
    # 凭据修好后 `resolve_backend()=postgresql`，而 **API 的读写其实落在 SQLite**
    # ⇒ PG 的 `last_retrieved_at` **不被维护、自然衰减** ⇒ 拿"PG 纪元的历史中位数"去比
    # 会读出**假退化**（实测：`3 vs 93，参照 n=1017` ⇒ FAIL）。
    # 故**两个方向都看"写入侧实际是谁"**，而不是只看配置里的后端名。
    _write_side_sqlite = _sqlite_side_is_active()
    _marker_side_is_pg = bool(_backend and _backend.startswith("postgres"))
    if (not _marker_side_is_pg) or _write_side_sqlite:
        #
        # · `U1a_agent_reads_24h`（P1-0 冻结的**归属**口径，读 PG 独有列
        #   `memories.last_retrieved_at`）⇒ 在 SQLite 上**仍然不判**（纪元不适用，理由见下）；
        # · `U1a_reader_attributed_24h`（**读取方**口径，走 `audit_log.details.memory_ids`，
        #   而 `audit_log` 就在活动 SQLite 库里）⇒ 改用 **SQLite 侧采样**来判。
        #   升级依据（本项目自己写的前置条件："等**覆盖率**达到可判水平再议是否升为判据"）：
        #   实测近 24h 覆盖率 **100.0%**（33/33）、近 30d **96.66%**（2836/2934），
        #   且 `memory_ids` 自 **2026-08-13** 起就在记录 ⇒ 前置条件满足。
        #   **不替换** P1-0 那个键（项目明说"不替换，否则等于偷偷换尺子"）——是**并增**。
        if metric != "U1a_agent_reads_24h":
            try:
                refs, boot = _u1a_samples_sqlite(metric, int(cur_value))
            except Exception as _e:  # noqa: BLE001 — 采样不可用 ⇒ 明说不判
                return None, "%s SQLite 采样不可用（%s）⇒ 不判" % (metric, str(_e)[:60])
            if len(refs) < 3:
                return None, ("%s 历史样本不足（6–21 天前 %d 条 <3）⇒ 不判（不造假红）"
                              % (metric, len(refs)))
            refs.sort()
            _m = len(refs) // 2
            med = refs[_m] if len(refs) % 2 else (refs[_m - 1] + refs[_m]) // 2
            ok, note = _u1a_window_compare(cur_value, med, n_ref=len(refs))
            return ok, note + ("｜**取数源 = sqlite:audit_log.memory_ids**"
                               "（与 PG 版 `last_retrieved_at` 同语义、不同源 ⇒ 跨存储不可直接比）；"
                               "历史参照由 `audit_log` 逐日反推（本次引导 %d 条），非人工填数。" % boot)
        return None, (
            "U1a **口径不适用（存储纪元）**：读标记列 `last_retrieved_at` 只由 PG 适配器维护，"
            "而当前活动存储是 `%s` ⇒ API 的检索不会 touch 它，本读数与其历史样本不可比。"
            "**本轮不判**（显式点名，不静默通过；同 U2 的账本纪元处置）。"
            "口径修正见 `_u1_split` 的 docstring。" % _backend)
    try:
        import os as _os
        import sys as _sys
        _sd = _os.path.dirname(_os.path.abspath(__file__))
        if _sd not in _sys.path:
            _sys.path.insert(0, _sd)
        from _pg_std import pg_connect
        c = pg_connect()
        with c.cursor() as cur:
            cur.execute("INSERT INTO utilization_samples(metric, value) "
                        "VALUES ('U1a_agent_reads_24h', %s)", (int(cur_value),))
            # 2026-09-30（口径修正）：取 **6–21 天窗口的全部样本**，在 Python 侧算中位数。
            # 原实现是 `ORDER BY ABS(EXTRACT(EPOCH FROM (ts - (NOW() - interval '7 days')))) LIMIT 1`
            # —— 取**离 7 天前最近的那一个**样本 ⇒ 参照点自身的方差直接变成判决抖动。
            cur.execute("""SELECT value FROM utilization_samples
                           WHERE metric = 'U1a_agent_reads_24h'
                             AND ts < NOW() - interval '6 days'
                             AND ts > NOW() - interval '21 days'""")
            refs = [int(r[0]) for r in cur.fetchall() if r[0] is not None]
        c.commit()
        c.close()
    except Exception as e:  # noqa: BLE001
        return None, "U1a 采样不可用（%s）⇒ 不判" % str(e)[:50]
    # 样本不足 ⇒ **明说不判**（fail-closed：不把"没测到"读成"过了"）
    if len(refs) < 3:
        return None, ("U1a 历史样本不足（6–21 天前仅 %d 条，需 ≥3 条才能算中位数）"
                      "⇒ 本轮不判，不做假红" % len(refs))
    refs.sort()
    mid = len(refs) // 2
    med = refs[mid] if len(refs) % 2 else (refs[mid - 1] + refs[mid]) // 2
    return _u1a_window_compare(cur_value, med, n_ref=len(refs))



def accept_baseline(baseline_path: str, reason: str, res: dict) -> int:
    # §929：基线必须**绑定账本纪元**，否则下次新会话必然误判"下降"（见棘轮处的说明）。
    """**带理由**重录基线（2026-09-20 §918）。

    为什么需要它：本脚本原先只在"基线文件不存在"时写基线 ⇒ docstring 与 EXECUTION §917.3
    写的 `--accept-baseline --reason "..."` **根本无法执行**（argparse 报 unrecognized arguments），
    而基线口径与闸门口径必须同源（否则拿 all 基线比 prod 读数 ⇒ 不可比 ⇒ 长期红 ⇒ G10）。
    纪律与另三把闸门一致：**无理由不落盘**（抬高基线必须留痕），且重录是**追加历史**。
    """
    if not str(reason or "").strip():
        print("[baseline] FAIL：--accept-baseline 必须带 --reason（抬高/改口径要留痕）")
        return 1
    old = {}
    try:
        with open(baseline_path, encoding="utf-8-sig") as fh:
            old = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        old = {}
    hist = old.get("baseline_history")
    hist = list(hist) if isinstance(hist, list) else []
    if old.get("baseline") and not hist:
        # 老文件没有历史数组：把当前这条作为历史起点补进去，避免"重录即丢失前值"
        hist.append({"ts": old.get("ts"), "baseline": old.get("baseline"),
                     "why": "（历史补录）本文件此前无 baseline_history"})
    hist.append({"ts": res.get("ts"), "baseline": res.get("headline", {}), "why": str(reason).strip()})
    out = {"baseline": res.get("headline", {}), "ts": res.get("ts"),
           # 2026-09-20（§918）：把口径写进基线 —— ratchet 据此判"可比性"。
           "scope": util_scope(),
           "note": BASELINE_NOTE, "baseline_history": hist}
    try:
        os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
        with open(baseline_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        print("[baseline] FAIL：无法写入 %s：%r" % (baseline_path, e))
        return 1
    print("[baseline] 已重录（scope=%s，理由=%s）：%s"
          % (util_scope(), reason, json.dumps(res.get("headline"), ensure_ascii=False)))
    return 0


def ratchet(res: dict, baseline_path: str) -> int:
    if not os.path.exists(baseline_path):
        try:
            os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
            with open(baseline_path, "w", encoding="utf-8") as fh:
                json.dump({"baseline": res.get("headline", {}), "ts": res.get("ts"),
                           "note": "利用率基线；U1/U2 下降、U3/U4 上升会使 CI 失败。改善后请同步下调/上调。"},
                          fh, ensure_ascii=False, indent=1)
            print("[ratchet] 无基线 -> 本次建立：%s" % json.dumps(res.get("headline"), ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            print("[ratchet] FAIL：无法写入基线 %s：%r" % (baseline_path, e))
            return 1
        return 0

    # G6 同源纪律：基线**存在但读不出** ⇒ fail-closed，绝不静默当成"无基线"
    try:
        with open(baseline_path, encoding="utf-8-sig") as fh:
            base = json.load(fh)
    except Exception as e:  # noqa: BLE001
        print("[ratchet] FAIL：基线文件存在但无法解析：%s" % baseline_path)
        print("[ratchet] 原因：%r" % (e,))
        print("[ratchet] 门禁**不**静默失效 —— 请修复或删除该文件后重试。")
        return 1
    if not isinstance(base, dict) or not isinstance(base.get("baseline"), dict):
        print("[ratchet] FAIL：基线文件格式不正确（缺 baseline 字段）：%s" % baseline_path)
        return 1

    # 2026-09-20（§918）：**基线与运行口径必须同源**，否则拒绝比较。
    # 动机（实测）：口径切 prod 后，不带 --scope 的手动运行会把 U1 的 value 读成全库口径
    # （0.1162）⇒ 与 prod 基线（0.1671）比 ⇒ 报"下降"的**假红**（恒红闸门=被忽略的闸门，G10）。
    # fail-closed，并给出可执行指引；未记录 scope 的旧基线按"不可判"放行（向后兼容）。
    _bs = str(base.get("scope") or "").strip().lower()
    if _bs and _bs != util_scope():
        print("[ratchet] FAIL：口径不匹配 —— 基线 scope=%s，本次 scope=%s（两者不可比）"
              % (_bs, util_scope()))
        print("[ratchet] 处理：按闸门清单口径运行 --scope %s（见 docs/GATE_SET.json 的 "
              "memory_utilization 项）；确需改口径则用 --accept-baseline --reason 重录基线。"
              % _bs)
        return 1

    b = base["baseline"]
    cur = res.get("headline", {})
    bad = []
    skipped = []
    # 2026-09-20（§929）：会话级计数器（注入账本 delivered_total/cold_delivered）跨纪元
    # **不可比** —— 新会话会把账本从 0 开始重写，拿它跟基线比大小得到的是"假下降"。
    # 判据：账本纪元（first_ts）与基线不一致 ⇒ 这些键进 skipped 并**显式打印原因**。
    _epoch_cur, _epoch_base = cur.get("ledger_epoch"), b.get("ledger_epoch")
    _epoch_changed = bool(_epoch_cur and _epoch_base and float(_epoch_cur) != float(_epoch_base))
    # 2026-09-21（§1024）：**基线缺纪元 ⇒ 同样不可比**（legacy 基线）。
    # 实测：§929 的守卫要求两边都有纪元才判"变了"，而 09-20 18:39 的基线是在
    # `headline.ledger_epoch` 这行**还没写出来**之前录的 ⇒ 守卫放过 ⇒ 拿会话级
    # 计数器跨纪元比大小 ⇒ 假红（1985 → 435）。这里改为：**任一侧缺纪元就显式跳过**，
    # 并把原因打在输出里（不静默通过）；下一次 `--accept-baseline` 会自动带上纪元。
    _epoch_missing = not (_epoch_cur and _epoch_base)
    _EPOCH_KEYS = {"U1b_delivered", "U2_delivered", "U2b_cold_delivered", "U1b_cold"}
    for k, (direction, tol) in DIRECTIONS.items():
        if k in _EPOCH_KEYS and _epoch_missing:
            skipped.append("%s(基线/本次缺账本纪元⇒会话级计数器不可比，续录基线后恢复判定)" % k)
            continue
        if _epoch_changed and k in _EPOCH_KEYS:
            skipped.append("%s(账本纪元变化⇒会话级计数器不可比)" % k)
            continue
        bv, cv = b.get(k), cur.get(k)
        if cv is None or bv is None:
            # 显式 SKIP（不静默通过）：CI 无 PG 时 U1/U3 为 null
            skipped.append("%s(基线=%s 本次=%s)" % (k, bv, cv))
            continue
        try:
            bv, cv = float(bv), float(cv)
        except Exception:  # noqa: BLE001
            skipped.append("%s(非数值)" % k)
            continue
        if direction == "higher" and cv < bv - tol:
            bad.append("%s 下降 %.4f -> %.4f（基线 %.4f，容差 %.4f）" % (k, bv, cv, bv, tol))
        elif direction == "lower" and cv > bv + tol:
            bad.append("%s 上升 %.4f -> %.4f（基线 %.4f，容差 %.4f）" % (k, bv, cv, bv, tol))
        else:
            print("[ratchet] OK  %-22s %s（基线 %s）" % (k, cv, bv))
    # ── §955 窗口判据（同窗口对比；与定值棘轮并列，失败同样进 bad）──
    for k, why in WINDOW_JUDGED.items():
        okw, notew = _u1a_window_judge(cur.get(k), k)
        if okw is None:
            print("[ratchet] SKIP(窗口) %-18s %s" % (k, notew))
        elif okw:
            print("[ratchet] OK(窗口)  %-18s %s" % (k, notew))
        else:
            print("[ratchet] FAIL(窗口) %-17s %s" % (k, notew))
            bad.append("%s %s（判据：%s）" % (k, notew, why))
    if skipped:
        print("[ratchet] SKIP（不静默通过）：%s" % ", ".join(skipped))
        print("[ratchet] 原因：缺少数据源（CI 无 PG / 无账本）。这**不是**通过，也不是失败。")
    # ── 降级项必须点名（P1-0）──────────────────────────────────────────────
    # 静默降级＝偷偷删判据（本仓对"闸门悄悄少了一条"的容忍度是零）。
    # 故每次 ratchet 都把"哪些数还在报告里、但已经不再判"连同理由打印出来。
    downgraded = []
    for k, why in WINDOW_JUDGED.items():
        if k in b or k in cur:
            downgraded.append("   · %-24s 本次=%s（基线 %s）\n      为何不判定值：%s"
                              % (k, cur.get(k), b.get(k), why))
    for k, why in REPORT_ONLY.items():
        if k in b or k in cur:
            downgraded.append("   · %-24s 本次=%s（基线 %s）\n      为何不判：%s"
                              % (k, cur.get(k), b.get(k), why))
    if downgraded:
        print("[ratchet] 报告-only（仍出数、不再参与判据 —— 降级不是删除）：")
        for x in downgraded:
            print(x)
    if bad:
        print("[ratchet] FAIL：利用率变差：")
        for x in bad:
            print("   · " + x)
        return 1
    print("[ratchet] 利用率未变差（基线 %s）" % base.get("ts", "?"))
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ratchet", action="store_true",
                    help="与基线比较，仅在利用率**变差**时退出 1（历史欠账不阻断 CI）")
    ap.add_argument("--baseline",
                    default=os.path.join(ROOT, "dsh-ops", "memory_utilization_baseline.json"))
    ap.add_argument("--out", default=OUT)
    # 2026-09-20（§918）：显式口径（优先于 TRINITY_UTIL_SCOPE）。
    # 清单侧用法见 docs/GATE_SET.json 的 memory_utilization 项（带 --scope prod）。
    ap.add_argument("--accept-baseline", action="store_true",
                    help="带理由重录基线（必须同时给 --reason）；改口径后必须重录")
    ap.add_argument("--reason", default="", help="重录/改口径的理由（留痕进 baseline_history）")
    ap.add_argument("--scope", default="", choices=["", "all", "prod"],
                    help="ratchet 口径：留空=env/默认 all；prod=生产面（剔评测语料）")
    a = ap.parse_args(argv)
    set_cli_scope(a.scope)

    r = run()
    try:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass

    if a.json:
        print(json.dumps(r, ensure_ascii=False, default=str))
    else:
        u1, u2 = r["U1_retrieval_coverage"], r["U2_delivery"]
        u3, u4 = r["U3_write_read_ratio"], r["U4_injected_tokens"]
        tr, cb = r.get("U1_trend") or {}, r.get("U2b_cold_delivery") or {}
        sp, u1b = r.get("U1_split") or {}, r.get("U1b_injection") or {}
        print("memory utilization  (%s, pg=%s)" % (r["ts"], r["pg_available"]))
        print("  U1 三拆(判据)  : a(agent 命名空间被读)=%s  b(注入投递)=%s/冷%s  "
              "c(管道自读,报告-only)=%s  [总量=%s]"
              % (sp.get("U1a_agent_reads_24h"), u1b.get("U1b_delivered"),
                 u1b.get("U1b_cold"), sp.get("U1c_pipeline_reads_24h"),
                 sp.get("U1_total_reads_24h")))
        at = r.get("U1_attrib") or {}
        if at.get("hot_path_coverage") is not None:
            # 2026-09-21（§1054）：结论含义随行输出（ATTENTION=盯、OK=无需动作，两者都不是批准）
            try:
                import os as _os
                from verdict_labels import annotate as _annotate, label_with_meaning as _lwm
                # 2026-09-21（§1063）：对照开关 —— 用于"接线前后同次双跑"证明非侵入（§13.3 纪律二）
                if _os.environ.get("TRINITY_VERDICT_ANNOTATE", "on") != "off":
                    _annotate(at)
                _verdict_txt = _lwm(at)
            except Exception:  # noqa: BLE001
                _verdict_txt = str(at.get("verdict"))
            print("  U1 冷库存归因  : 热路径覆盖率=%s（%s/%s）  冷库存占比=%s（%s 条）  verdict=%s"
                  % (at.get("hot_path_coverage"), at.get("hot_path_hit_30d"),
                     at.get("hot_path_active"), at.get("cold_corpus_share"),
                     at.get("cold_corpus_active"), _verdict_txt))
            print("                   bulk_cold 生产者=%s（灌入型：占比高但不是检索缺陷）"
                  % (at.get("bulk_cold_producers") or []))
            print("                   ⚠️ 全局覆盖率被冷库存稀释 ⇒ 判检索健康看**热路径覆盖率**")
        cc = sp.get("cross_check") or {}
        print("  U1-a 口径核对  : 前缀=%r 窗口=%sh  读取方归因窄读数=%s  其中带读取证据=%s"
              % (sp.get("U1a_agent_prefix"), sp.get("window_hours"),
                 cc.get("reader_attributed_mids_24h"), cc.get("a_with_audit_evidence")))
        ra = r.get("U1a_reader_attributed") or {}
        print("  U1 读取方(报告): value=%s（任何读取方）  reader_side_dsh=%s（仅 dsh-* 读取方）"
              "  覆盖率=%s（%s/%s） usable=%s"
              % (ra.get("value"), ra.get("reader_side_dsh"), ra.get("coverage"),
                 ra.get("rows_with_ids"), ra.get("rows_total"), ra.get("usable")))
        print("                    ⚠️ 与上面的 U1-a **同前缀不同语义**：U1-a 问「记忆归谁」，"
              "本行问「谁读的」⇒ 必须并列引用（且本行是**下界**：单次至多记 10 条）")
        if u1.get("coverage_30d_prod") is not None:
            print("  U1 覆盖率[生产面]: coverage_30d=%s  ever=%s   (active=%s used_30d=%s)  ← 剔公开评测语料"
                  % (u1.get("coverage_30d_prod"), u1.get("coverage_ever_prod"),
                     u1.get("active_prod"), u1.get("retrieved_30d_prod")))
        _sc = "  〔口径=生产面，ratchet 判据〕" if u1.get("scope") == "prod" else ""
        print("  U1 检索覆盖率  : coverage_30d=%s  ever=%s   (active=%s used_30d=%s)%s"
              % (u1.get("value"), u1.get("coverage_ever"), u1.get("active_total"),
                 u1.get("retrieved_30d"), _sc))
        print("  U1 趋势(报告)  : 近7日日新增均值=%s  14日=%s  峰值/均值=%s  近7日=%s"
              % (tr.get("daily_mean_7d"), tr.get("daily_mean_14d"), tr.get("peak_to_mean"),
                 [n for _, n in (tr.get("series") or [])[-7:]]))
        print("  U2 投递量      : delivered=%s  surfaced_by_engine=%s  injection_paths=%s/%s"
              % (u2.get("value"), u2.get("surfaced_by_engine"),
                 u2.get("injection_paths_present"), len(u2.get("injection_paths") or [])))
        #: P0-1/P1-1：**来源拆分**（探针流量 vs 真实会话流量）。
        #: 实测动机：引擎侧 `calls=307` 而注入账本 `calls=1` —— 一个计数器把两者混成一个数，
        #: 于是"注入通路在生产上到底跑没跑"**没有判别力**（"被测量代替了被使用"）。
        _os = u2.get("origin_split") or {}
        print("  U2 来源拆分    : 生产(by_origin['dsh-plugin'])=%s 次 / 探针=%s 次 / 未知=%s 次"
              % (_os.get("production_calls"), _os.get("probe_calls"), _os.get("unknown_calls")))
        if _os.get("production_calls") == 0:
            print("                    ⚠️ 生产来源调用为 0 ⇒ **注入通路在真实会话上从未执行**；"
                  "引擎侧的全部 calls 都来自探针（详见 by_origin 明细）")
        print("  U2b 冷投递量   : cold=%s / delivered=%s (cold_share=%s)"
              % (cb.get("cold_delivered"), cb.get("delivered_total"), cb.get("cold_share")))
        if u3.get("ratio_7d_prod") is not None:
            print("  U3 写读比[生产面]: 7日窗口=%s  (writes_7d=%s / reads_7d=%s)  ← 剔评测语料写入；ratchet 判据仍是全库口径"
                  % (u3.get("ratio_7d_prod"), u3.get("writes_7d_prod"), u3.get("reads_distinct_7d_prod")))
        print("  U3 写读比      : 7日窗口=%s（ratchet 判据）  30日累计=%s  (writes_7d=%s / reads_7d=%s)"
              % (u3.get("value"), u3.get("cumulative_ratio"), u3.get("writes_7d"), u3.get("reads_distinct_7d")))
        print("  U4 注入 token  : %s  (chars=%s, 估算器 %s)"
              % (u4.get("value"), u4.get("chars_total"), u4.get("token_estimator")))
        print("out -> " + a.out)

    if a.accept_baseline:
        return accept_baseline(a.baseline, a.reason, r)
    if a.ratchet:
        return ratchet(r, a.baseline)
    return 0


if __name__ == "__main__":
    _rc = main()
    import datetime as _dt
    print("[采样时刻] %s（本读数只对该时刻的系统状态成立）" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(_rc)

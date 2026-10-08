# -*- coding: utf-8 -*-
"""t45 / G4 判据组：**有 PII 即掩码** 的承重判据（六面 + G7 补面 + 13 条牙齿 + 显式未覆盖清单）。

## 这是什么

G2（t43）把脱敏条件从「**命中敏感类别**才掩码」改成「**有 PII 即掩码**」；G3（t44）把写入响应
字段改成**如实反映实际发生的脱敏**；G7（t48）把守卫**下沉到适配器写入边界**
（`trinity/adapters/_pii_guard.py`，复用 G2 同一策略与开关）。本文件为这三次变更建立**防复发**判据组。

判据组与既有文件的**分工**（不重复造）：`test_sensitive_pii_scope_20261006.py`（G2 自己的 8 条 + 8 变异体）、
`test_sensitive_redact_wiring_20261006.py`（接线 + 计数）、`test_redact_response_field_20261006.py`（G3 的 8 条）。
本文件补的是它们**没有**覆盖的面：**真实误报回归集**、**high 四家族不降级**、**两档回滚的三方逐字节比较**、
**写入路径覆盖矩阵（含显式未覆盖清单）**、**响应 ⊨ 落库 的方向性一致**、**性能回归基线**。

## 六面（每面都有"人为破坏该判据必须变红"的牙齿，共 7 条）

| 面 | 判据 | 牙齿（变异体） |
|---|---|---|
| ① 两方向 | F1a 纯 PII ⇒ 落库已掩码；F1b 无 PII ⇒ **逐字不变**（G1 §4.3 的 12 条真实误报集） | `scan_pii` 恒不命中 / 「判 redact + 掩码器改写一切」组合 |
| ② 类别命中 | F2a 仍掩码 + `metadata.sensitive_scan` 在位；F2b 无 PII 的类别文本也留 `policy=category` 标记 | `scan_sensitive` 恒不 flag |
| ③ high 不降级 | F3 四个家族（自伤/心理/法律/性史/未成年）**8 条真实样本**全部拒存且**零行落库** | `policy_action` 把 high 降级为 redact |
| ④ 回滚逐字 | F4a `REDACT=0` 逐字；F4b `SCOPE=category` 精确回滚；F4c **两档 + 输入三方逐字节一致** | `sensitive_redact_enabled` 恒 True |
| ⑤ 写入路径 | F5a client ✓；F5b **API `POST /memories`** ✓（临时库，TestClient）；F5c/F5d/F5e aggregator/5 类直写/engine_worker = **显式未覆盖**（源码级证据） | 「未覆盖清单扫描器」用合成源码自证（若清单里的路径开始脱敏 ⇒ 判据红 ⇒ 强制更新清单） |
| ⑥ 响应字段 | F6a 响应字段 ⊨ 落库内容；F6b 无 PII 时响应必须为假（反事实防恒真） | 适配器返回值改成修前形态（`False/[]`）/ 恒真声称 |
| **⑦ G7 适配器边界** | G7-① 直写也被掩码 + 账本 `layer=adapter`；G7-② `ADAPTER_GUARD=0` 的组合语义；G7-③ **两通道 high 各自契约**；G7-④ 裸 SQL 盲区运行时复现；G7-⑤ PG 守卫**代码在位/运行时未验证**；G7-⑥ `scripts/` 默认被掩码已披露；G7-⑦ t44 既有判据**引用而非复制** | 守卫恒不介入 / 开关读成恒真 / 守卫永不拒存 / 盲区判定式自证 / 挖掉 PG isolate 分支 / 披露改成「可逆」 |

## 未覆盖清单（**显式，不得沉默**）——见 `UNCOVERED_PATHS` / `RUNTIME_GAPS`

本判据**不能**证明：(a) aggregator 池（另一套存储）、(b) `scripts/` 下 35 个批处理脚本（**现在默认被掩码**，G7-R4）、
(c) 镜像回填（**故意**不掩码）、(d) **6 条裸 SQL 直写**（`a2a_memory.py` / `brain/memory_manager.py` /
`brain/memory_transaction.py` / `cognition/actor.py` / `vms/backends/postgres_backend.py` /
`api/server/_routers_brain.py` —— 绕过适配器，G7 守卫不生效；本判据在临时库上**运行时复现**了明文落库）、
(e) **运行中的 engine_worker**（装旧代码，需插件 respawn）、(f) **PG 侧守卫（G7-R5：代码在位但运行时未验证）**。
`test_未覆盖清单必须显式且可机器核对` 会**强制**这份清单存在、**两表合计 ≥12**、每条带原因、
**7 条已守卫裸 SQL 逐条点名**、**镜像回填含 `postgresql.py::migrate_from_sqlite`**，
并用源码证据核对其真实性（清单过期即红）。

## t52/G11 追加（**原位保留上面的旧表述**）

1. **两张表各补 1 条**（verifier 在 G4 定稿**之后**独立枚举出来的）：
   - `trinity/engine_worker.py::_session_dispose_summary`（t51 已接守卫）⇒ 进 **`GUARDED_BARE_SQL_PATHS`**（第 7 条）；
   - `trinity/adapters/postgresql.py::migrate_from_sqlite`（:1565，SQLite→PG **镜像/回填**，
     **应然不掩**）⇒ 进 **`UNCOVERED_PATHS`**，`kind="mirror_backfill"`，**函数级**（`scope.function`）。
2. **两表核对的触发条件**（原来只写了方向 A，方向 B 从未被触发过）：
   - **方向 A**（`UNCOVERED_PATHS`）：某条**开始**调守卫 ⇒ 红 ⇒ 强制移入已守卫表；
   - **方向 B**（`GUARDED_BARE_SQL_PATHS`）：**守卫被拆 / 守卫行晚于 `INSERT INTO memories` / 不再含裸 SQL** ⇒ 红。
   **t52 的事实更正**：任务书假设"engine_worker 曾在盲区表里、修好后要移出"——**该前提不成立**，
   它**从未进过**两张表（t51 只读核对 13 条 path 字段）⇒ 方向 A 对它**本来就不会触发**；
   t52 把它补进**方向 B**，并实测"拆守卫 ⇒ 红"（`test_G11_两表双向核对的触发条件与牙齿`）。
3. **方法学**：写入路径枚举**必须由独立一方复现** —— G4 的清单来自 G1+G7 的枚举，
   而 verifier 事后独立枚举又找出 2 条 ⇒ **单一来源必然不完备**（与本轮"12 类入口"同族）。

## t54/G13 追加（**真闭环**：权威数 = 独立全仓普查；**原位保留上面的旧表述**）

1. **两个数量阈值的病**（verifier 在 G5 指出）：`>= 12 / >= 7` 取自**被核对的两表自身长度**
   ⇒ **实存但未登记的写入点既不会让计数变化、也不会进任何扫描** ⇒ 判据不会必然红（**假闭环**）。
2. **修法**：加入 `census_sql_insert_sites()` —— 走 `trinity/**` 全仓、用 **AST 字符串字面量**找
   `INSERT INTO memories (`（正则要求紧跟左括号，以排除 `memories_fts` 这类前缀相同的 FTS 语句），
   判据断言 **普查集合 == 登记集合**（`==`，不是 `>=`）+ 方向核对 + **同函数不得有第二处**。
   常数 `>=12 / >=7` **保留为下界保险**（门槛只升不降），但**不再是权威数**。
   **普查宇宙 = `trinity/**`**（显式声明；`scripts/**` 的镜像由 `batch_script_census` 那一类单独计数）。
   对照物：`scripts/direct_pg_writers_audit.py --ratchet`（同样"普查+棘轮"，实跑 PASS），
   但它的宇宙是 `psycopg2.connect` 直写者、与本判据**不同** ⇒ **只借形式、不复用数字**（防口径混用）。
3. **普查发现的 4 条补登记**（`SQL_INSERT_OTHER_SITES`）：`postgresql.py::store_memory` /
   `postgresql.py::ingest_batch` / `sqlite/_crud.py::store_memory`（`kind="adapter_guard"`，
   守卫边界的自身实现）+ `_pg_schema.py::<module>`（`kind="ddl_bootstrap"`，PG 初始化 DDL 的启动标记行）。
4. **skip 预算 = 0**：原来 3 处 `pytest.skip` 逃逸口**全部拆除**，改为 `_env_gate_fail()`（响亮失败）；
   `SKIP_SITES_BUDGET = 0` + **静态站点扫描**（AST）+ **运行期账本**三者一致才算过
   （`scripts/silent_skip_audit.py` 管的是**脚本内**静默 `pass/continue/return None`，
   pytest 侧没有现成门禁 ⇒ 预算落在本判据内，见报告 §10.2）。
5. **"两种病"的维护说明**写在 §"全仓普查"注释块里（缺登记 vs 恒真），便于下一个人判断"没红"是哪一种。

## 硬约束遵守

写域 = `tests/unit/**` + 报告；**未改任何 `trinity/**`**；未重启服务；未写生产 PG（全部临时 SQLite）；
未跑全量 pytest；每步 `compile()`。
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.sensitive as S  # noqa: E402

AGENT = "t45-g4-scope"

# ── 样本（G1 §4.3 的真实误报集 + 四类 PII + 四家族 high）────────────────────
PURE_SAMPLES = {
    "手机号": ("发货联系人手机 13812345678，请仓库核对。", "13812345678", "138********"),
    "身份证": ("客户身份证号 110101199003071233，已核验。", "110101199003071233", "110***************"),
    "邮箱": ("请联系 zhangsan@corp.cn 确认到货。", "zhangsan@corp.cn", "z***@***.cn"),
}
PURE_ALL = ("手机 13812345678，身份证 110101199003071233，邮箱 zhangsan@corp.cn，"
            "卡号 4111111111111111。")

#: **无 PII ⇒ 必须逐字不变**。全部取自 G1 §4.3（本仓语料里真实出现过的 13 条误报），
#: 用它们做回归集，是为了防"扩范围 ⇒ 把标识符/版本号/UUID 一起掩了"这类误伤。
NO_PII_SAMPLES = [
    ("bilibili_opus", "https://www.bilibili.com/opus/443715248901563958"),
    ("semver", "升级到 trinity 1.8.5.6 之后恢复正常。"),
    ("loopback", "服务监听 127.0.0.1:8001，仅本机可达。"),
    ("uuid", "request id 7d2dc403-093a-4a1c-9b6f-2f0f3a1c8e55 已记录。"),
    ("fkey_name", "CREATE TABLE skus (supplier_id bigint REFERENCES skus_supplier_id_fkey);"),
    ("order_no", "订单号 DO-20260902-1188 库存锁定失败。"),
    ("placeholder", "紧急联系人姓名：未维护"),
    ("warehouse_id", "warehouse_id 88213 的拣货效率下降。"),
    ("shopee_appid", "shopee appId 8f31c2 zz 已配置。"),
    ("ip_port_pair", "网关 10.0.0.7:5432 与 127.0.0.1:8001 都通。"),
]

CATEGORY_TEXT = "最近有点抑郁，联系方式 13812345678。"
CATEGORY_NO_PII = "用户偏好暗色模式，使用 VS Code 与 Python。"

#: 四家族 high（原文取自 `test_sensitive_tiers_20261006.py::PERSONAL_HIGH`，已实测 high）
HIGH_SAMPLES = [
    ("自伤", "我最近总想着自杀，觉得活不下去了。"),
    ("心理", "我得了抑郁症，最近在吃药。"),
    ("法律", "用户有犯罪记录，案底在老家派出所。"),
    ("性史", "我有过一段性史，那是大学时期……"),
    ("未成年", "我 14 岁的女儿在实验中学读初二，学籍号 2023001。"),
    ("心理-第三人称", "用户患有抑郁症，情绪持续低落。"),
    ("法律-个人记录", "我弟弟去年因为盗窃进了监狱，家里人一直瞒着。"),
    ("心理-确诊", "他确诊了抑郁症，正在服用抗抑郁药物。"),
]

#: ── **显式未覆盖清单**（本判据证明不了的东西；漏掉任何一条即为"沉默"）──────────
#: 与 G1 §5 / §10 的路径枚举 + G7（t48）新枚举合并；`path` 必须**源码级可核对**。
UNCOVERED_PATHS = [
    {"path": "trinity/agents/aggregator/_ingest.py", "kind": "aggregator_pool",
     "reason": "聚合池写的是另一套存储（_pool + aggregator_vectors.pkl），不经过适配器"},
    {"path": "trinity/daemon/memory_tiers.py", "kind": "daemon_direct_write",
     "reason": "daemon 直接写适配器（G7 后**已覆盖**，但本判据不覆盖 daemon；见 §G7 判据）"},
    {"path": "trinity/memory/memory_agent.py", "kind": "memory_extract_direct_write",
     "reason": "memory/* 抽取与巩固直写适配器/ingest_batch（G7 后已覆盖；本判据不覆盖该路径）"},
    {"path": "trinity/brain/self_model.py", "kind": "brain_partial_direct_write",
     "reason": "同模块内两种写法并存：:111 直写适配器（G7 覆盖）、:102/193 走 m.ingest"},
    {"path": "trinity/evolution/core.py", "kind": "other_direct_write",
     "reason": "evolution/vms/pipeline 等直写适配器（G7 后已覆盖；本判据不覆盖）"},
    # ── G7-R2 的 6 条裸 SQL 已于 **t50/G9 修好** ⇒ 移到 `GUARDED_BARE_SQL_PATHS` ─────
    # ── G1 §5 第 12/14 条：脚本与镜像（**故意**不掩码，用 ADAPTER_GUARD=0 显式退出）──
    {"path": "scripts/sqlite_pg_mirror.py", "kind": "mirror_backfill",
     "reason": "镜像/回填路径**故意**不掩码（否则 PG 与 SQLite 不再逐字一致）"},
    # ── t52/G11：**verifier 在 G4 定稿之后独立枚举**出的第 8 条（**应然不掩**，不是缺陷）──
    # 注意：本条目是**函数级**（`scope.function`）—— `postgresql.py` 文件级有 4 处守卫调用
    # （`store_memory` / `ingest_batch`），若按整文件核对会**误判**；因此只核对该函数体。
    {"path": "trinity/adapters/postgresql.py", "kind": "mirror_backfill",
     "func": "migrate_from_sqlite",
     "scope": {"function": "migrate_from_sqlite", "line": 1565},
     "reason": "t51 一行登记：`migrate_from_sqlite()`（:1530 起，`SELECT * FROM memories` ⇒ "
               ":1565 `INSERT INTO memories`）是 **SQLite→PG 镜像/回填**，按 G1 §5 第 12/14 条"
               "**应然不掩**（掩了 PG↔SQLite 不再逐字一致；这正是 `TRINITY_ADAPTER_GUARD=0` 服务的场景）"
               "⇒ 该函数体内**确实没有**守卫调用（文件级 4 处在别的函数里）"},
    {"path": "scripts/", "kind": "batch_script_census",
     "reason": "G7-R4：scripts/ 下含 store_memory/ingest_batch 的脚本**现在默认被掩码**"
               "（行为变更、不可逆）⇒ 评测类须显式用 TRINITY_ADAPTER_GUARD=0 退出；本判据只数剂量"},
]
#: **t50/G9 已关闭的裸 SQL 直写**（原 `UNCOVERED_PATHS` 里的 6 条）。
#: 核对方向与 `UNCOVERED_PATHS` **相反**：这些文件必须**含** `adapter_pii_guard`，
#: 且**守卫调用在 `INSERT INTO memories` 之前**（G7 确立的不变量：守卫晚于哈希 ⇒ 行自相矛盾）。
#: 运行时版本（含"关守卫 ⇒ 明文"的牙齿）在 `tests/unit/test_bare_sql_guard_20261006.py`。
GUARDED_BARE_SQL_PATHS = [
    {"path": "trinity/a2a_memory.py", "func": "_upsert_adapter", "kind": "bare_sql_insert",
     "reason": "t50：镜像 upsert 前过 `adapter_pii_guard`，掩码后**重算 sha256**（否则哈希算原文）；"
               "high ⇒ 拒存镜像（t50 判据还抓到过这里缺 `import hashlib` 的静默失败）"},
    {"path": "trinity/brain/memory_manager.py", "func": "promote_working_memory",
     "kind": "bare_sql_insert",
     "reason": "t50：守卫在**去重查询与 INSERT 之前**（比对与落库同口径）；high ⇒ 跳过该条"},
    {"path": "trinity/brain/memory_transaction.py", "func": "commit", "kind": "bare_sql_insert",
     "reason": "t50：事务里每条 write 过守卫；high ⇒ **整个事务拒提交并回滚**（本层自定契约）"},
    {"path": "trinity/cognition/actor.py", "func": "execute", "kind": "bare_sql_insert",
     "reason": "t50：观察回写前过守卫，`content_hash` 用同一份掩码后文本；high ⇒ 跳过回写（尽力而为语义）"},
    {"path": "trinity/vms/backends/postgres_backend.py", "func": "add", "kind": "bare_sql_insert",
     "reason": "t50：PG backend 显式过守卫 ⇒ 与**同目录 sqlite backend（走适配器）**行为一致："
               "含 PII 掩码、high 拒存"},
    {"path": "trinity/api/server/_routers_brain.py", "func": "memory_perceive",
     "kind": "bare_sql_insert",
     "reason": "t50：只对落 `memories.content` 的那份过守卫（守卫在加密前、哈希用掩码后文本）；"
               "`perceptions` 表**按代码里的明确设计保留明文**（另一张表，单独断言）"},
    # ── t52/G11：**verifier 在 G4 定稿之后独立枚举**出的第 7 条（t51 已接守卫）──────
    # 登记文本**直接取用 t51 备好的补丁**（REDACT-ENGINE-WORKER-SUMMARY.md §6.2），未重造。
    # 实测三条条件全过：① 含裸 `INSERT INTO memories`（:858）② 含 `adapter_pii_guard`（:806）
    # ③ **守卫行 806 < INSERT 行 858**（顺序不变量）。
    {"path": "trinity/engine_worker.py", "func": "_session_dispose_summary",
     "kind": "bare_sql_insert",
     "reason": "t51：`_session_dispose_summary()` 写入前过 `adapter_pii_guard`（守卫在 INSERT 之前）；"
               "含 PII ⇒ 掩码 + `metadata['pii_redaction']` 账本随行落库；"
               "high ⇒ 拒存（零落库）；该路径**故意**直写 `_resolve_store_db()`（会话销毁钩子），"
               "不改走客户端 ingest"},
]

#: **t54/G13：`trinity/**` 全仓普查发现的"其它 INSERT INTO memories 写入点"**
#: —— 它们不是"裸 SQL 绕行"，但**必须登记**，否则"普查 == 登记"的闭环不成立。
#: 权威数（`== 普查结果`）由 `census_sql_insert_sites()` 给出，**不取自本表长度**。
SQL_INSERT_OTHER_SITES = [
    {"path": "trinity/adapters/postgresql.py", "func": "store_memory", "kind": "adapter_guard",
     "reason": "守卫边界的**自身实现**（`adapter_pii_guard` 在本函数内、INSERT 之前）⇒ 已守卫；"
               "登记它是为了让全仓普查闭环，不是因为它有洞"},
    {"path": "trinity/adapters/postgresql.py", "func": "ingest_batch", "kind": "adapter_guard",
     "reason": "同上（PG 批量通道；high ⇒ `isolate` 落 `status='archived'`，见 G7-③）"},
    {"path": "trinity/adapters/sqlite/_crud.py", "func": "store_memory", "kind": "adapter_guard",
     "reason": "同上（SQLite 侧守卫边界；G7-① 实测这条路径的直写会被掩码）"},
    {"path": "trinity/adapters/_pg_schema.py", "func": "<module>", "kind": "ddl_bootstrap",
     "reason": "PG 初始化 DDL 里的**启动标记行**（`INIT_SQL` 的 `INSERT ... SELECT ... WHERE NOT EXISTS`，"
               "落的是 'Trinity PostgreSQL initialized at ...' 系统行、无用户内容）⇒ DB 侧执行、"
               "进程内守卫原则上不可达；登记它是为了普查闭环与「它**不是**应用写路径」这件事可核对"},
]

RUNTIME_GAPS = [
    {"what": "运行中的 engine_worker 进程",
     "why": "它装的是 G2/G3 之前的代码 ⇒ 代码路径已覆盖，**运行时未覆盖**；需插件 respawn 生效",
     "how_to_check": "respawn 后写一条纯 PII，看落库是否已掩码 + 响应 auto_redacted 是否为真"},
    {"what": "镜像回填（PG→SQLite 等）",
     "why": "回填的是**历史文本**，按设计不应二次改写（否则破坏可追溯性）",
     "how_to_check": "抽一行历史 PII 记录，确认回填后逐字不变"},
    {"what": "scripts/ 下含 store_memory/ingest_batch 的脚本",
     "why": "G7-R4：这些脚本**现在默认被掩码**（行为变更、不可逆）；评测语料须显式用 "
            "`TRINITY_ADAPTER_GUARD=0` 退出",
     "how_to_check": "数一遍脚本数（本判据机器核对），跑脚本时显式声明开关"},
    # ── G7-R5（队长补丁点名）：**不得写成"已覆盖"** ───────────────────────────
    {"what": "PG 侧的适配器守卫（`postgresql.py::store_memory` / `ingest_batch::_row`）",
     "why": "**代码已挂守卫，但运行时未验证**（当时无临时 PG，且硬约束禁止写生产 PG）⇒ "
            "G7-R5：**未验证**，不得读成已覆盖",
     "how_to_check": "事务内 INSERT + ROLLBACK 的 PG 冒烟（队长收口时做）"},
]

_REDACTION_PRIMITIVES = ("redact_identifiers(", "scan_pii(", "apply_policy(",
                         "from trinity.security.sensitive import")
#: `scripts/` 批量脚本普查用的正则（G7-R4 的剂量：这些脚本现在默认被掩码）
_REDACTION_BATCH_RX = re.compile(r"store_memory\(|ingest_batch\(")


def _read_source(path) -> str:
    """读源码的**唯一入口**。

    2026-10-06（t52/G11）：把它抽出来是为了让"拆掉守卫 ⇒ 判据必须红"可以被**实测**
    —— 牙齿测试 monkeypatch 本函数返回"被拆掉守卫的源码"，判据就会走它自己的判定路径变红，
    **不需要改仓库里的任何生产文件**（写域也不允许）。
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _function_source(src: str, name: str):
    """取出某个函数/方法的源码段（用于**函数级**核对，避免整文件误判）。取不到返回 None。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lines = src.replace("\r\n", "\n").split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    return None


# ══════════════════════════════════════════════════════════════════════════
# t54/G13：**全仓普查**（权威数来源）→ 「普查 == 登记」真闭环
#
# ## 为什么（verifier 在 G5 指出的结构性问题）
# 之前用 `len(两表) >= 12 / >= 7` 当门槛 —— 那个数**取自被核对的两表自身**，
# 于是"实存但未登记的写入点"**既不会让计数变化、也不会进任何扫描** ⇒ 判据**不会必然红**（假闭环）。
# 现在：权威数来自**独立普查** `census_sql_insert_sites()`（走 `trinity/**` 全仓，
# **不看三张表**），判据断言 **普查集合 == 登记集合**（`==`，不是 `>=`）。
# 对照物：`scripts/direct_pg_writers_audit.py --ratchet`（对 psycopg2 直写做全仓普查+棘轮，实跑 PASS）
# —— 注意它的**宇宙不同**（它管 `psycopg2.connect` 直写者，我管 `INSERT INTO memories` 字面量），
# 所以**不复用它的数字**，只借它的"普查+棘轮"形式（否则就是口径混用）。
#
# ## 两种病的区分（维护说明，给下一个读"没红"的人）
# · **缺登记（清单不完备）**：判据**有效**，但清单有洞 ⇒ 表现为"本该有的条目不在表里"。
#   本判据用 `==` 让它**必然红**（而不是靠人想起来补）。t52 的 engine_worker / postgresql:1565 就属这种。
# · **恒真（判据无判别力）**：看着绿，其实任何输入都绿 ⇒ 表现为"人为破坏也不红"。
#   本文件的处置：每条关键判据都配一颗**牙齿**（`test_牙齿_*` / `test_G11_*` / `test_G13_*`），
#   并且源码读取走 `_read_source()` 以便**注入被破坏的源码**做实测。
# ⇒ 出现"没红"时，先问：**是清单里没有它（缺登记），还是判据对任何输入都绿（恒真）？**
# ══════════════════════════════════════════════════════════════════════════
_SQL_INSERT_RX = re.compile(r"(?i)insert\s+into\s+memories\s*\(")
#: 参与"普查闭环"的 kind（其它 kind 不在这个宇宙里：它们不是 `INSERT INTO memories` 写入点）
CENSUS_KINDS = ("bare_sql_insert", "adapter_guard", "mirror_backfill", "ddl_bootstrap")


def census_sql_insert_sites() -> dict:
    """**独立全仓普查**：`trinity/**` 里所有真会往 `memories` 表插行的 SQL 字符串字面量。

    返回 `{(相对路径, 所属函数): {"lines": [...], "guard_before": bool}}`。
    判定用 AST 字符串常量（不是正则扫全文）⇒ 注释里的 SQL 不会误报；
    正则带 `\\(` 是为了排除 `memories_fts` 这类"前缀相同、表不同"的 FTS 语句。
    """
    out: dict = {}
    for p in sorted((ROOT / "trinity").rglob("*.py")):
        src = _read_source(p)
        if "insert into memories" not in src.lower():
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            out[(rel, "<unparsable>")] = {"lines": [0], "guard_before": False}
            continue
        lines = src.replace("\r\n", "\n").split("\n")
        funcs = [(n.name, n.lineno, n.end_lineno) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not _SQL_INSERT_RX.search(node.value):
                continue
            ln = node.lineno
            owner = next((nm for nm, a, b in funcs if a <= ln <= b), "<module>")
            a, b = next(((a, b) for nm, a, b in funcs if nm == owner), (1, len(lines)))
            before = "\n".join(lines[a - 1:ln])
            rec = out.setdefault((rel, owner), {"lines": [], "guard_before":
                                                "adapter_pii_guard" in before})
            rec["lines"].append(ln)
    return out


def registered_sql_insert_sites() -> dict:
    """登记表里的 `{(path, func): kind}`（只取**普查宇宙内**的 kind）。

    ⚠️ 普查宇宙 = `trinity/**`（本判据声明的范围）。`scripts/sqlite_pg_mirror.py` 这类
    **仓外**镜像条目**不参与**这个等式 —— 它们由 `UNCOVERED_PATHS` 的
    `batch_script_census` 那一类单独计数。**这是显式声明的范围**，不是偷偷过滤。
    """
    reg: dict = {}
    for item in GUARDED_BARE_SQL_PATHS + UNCOVERED_PATHS + SQL_INSERT_OTHER_SITES:
        if item.get("kind") not in CENSUS_KINDS:
            continue
        if not str(item.get("path", "")).startswith("trinity/"):
            continue                      # 仓外（scripts/**）单独一类
        fn = item.get("func") or (item.get("scope") or {}).get("function") or "<module>"
        reg[(item["path"], fn)] = item["kind"]
    return reg


def census_closure_diff() -> dict:
    """闭环差集（四类）：未登记 / 多登记 / 方向不符 / 同函数多处。全空才算闭环。"""
    census = census_sql_insert_sites()
    reg = registered_sql_insert_sites()
    diff = {"unregistered": sorted(set(census) - set(reg)),
            "registered_but_absent": sorted(set(reg) - set(census)),
            "direction_violations": [], "multi_site_functions": []}
    for site, meta in census.items():
        kind = reg.get(site)
        if kind in ("bare_sql_insert", "adapter_guard") and not meta["guard_before"]:
            diff["direction_violations"].append((site, kind, "缺守卫/守卫在 INSERT 之后"))
        if kind == "mirror_backfill" and meta["guard_before"]:
            diff["direction_violations"].append((site, kind, "镜像路径不该有守卫（掩了破坏镜像一致性）"))
        if len(meta["lines"]) > 1:
            diff["multi_site_functions"].append((site, meta["lines"]))
    return diff


def f13_全仓普查与登记表闭环() -> bool:
    """判据：**普查集合 == 登记集合** + 方向 + 同函数唯一。权威数 = 普查。"""
    return all(not v for v in census_closure_diff().values())


# ══════════════════════════════════════════════════════════════════════════
# t54/G13：**skip 预算 = 0**（"跳过必须响"）
#
# 仓库里`scripts/silent_skip_audit.py` 管的是**脚本里**的静默 `pass/continue/return None`
# （我实跑核对了它的输出：17 个候选中全是 scripts/ 的 gate，没有 pytest 面）
# ⇒ **pytest 侧的 skip 没有现成门禁**。所以本文件把预算做成本判据内的机器可核对常量：
#   `SKIP_SITES_BUDGET = 0`、**静态站点扫描**（AST 找 `pytest.skip(`）+ **运行期账本**；
# 任何一处新 skip ⇒ 红；预算与站点数不一致 ⇒ 红。
# ══════════════════════════════════════════════════════════════════════════
SKIP_SITES_BUDGET = 0
SKIP_LEDGER: list = []


def _skip_sites_in(source: str) -> list:
    """静态扫描 `pytest.skip(` 调用站点（返回行号列表）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [-1]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if getattr(f, "attr", "") == "skip" and getattr(getattr(f, "value", None), "id", "") == "pytest":
            out.append(node.lineno)
    return sorted(out)


def _skip_budget_ok(source: str, budget: int, ledger) -> bool:
    """预算判定：**站点数 == 预算** 且 **运行期账本为空**。"""
    return len(_skip_sites_in(source)) == budget and not ledger


def _env_gate_fail(reason: str):
    """环境不具备时的**响亮失败**（取代原来的 3 处 `pytest.skip` 逃逸口）。"""
    SKIP_LEDGER.append("env-gate-fail: %s" % reason)
    pytest.fail("本判据不接受静默降级（t54/G13：跳过必须响）：%s" % reason)


# ── 临时 SQLite 写入/读回（全部隔离，不碰 PG / 常驻服务）────────────────────
def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite", evolution_enabled=False)


def _stored(tmp_path, memory_id) -> dict:
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=str(tmp_path / "trinity_store.db"))
    ad.connect()
    try:
        return ad.get_memory(memory_id) or {}
    finally:
        ad.disconnect()


def _meta(row: dict) -> dict:
    m = row.get("metadata") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m or "{}")
        except Exception:  # noqa: BLE001
            m = {}
    return m if isinstance(m, dict) else {}


def _row_count(tmp_path) -> int:
    p = tmp_path / "trinity_store.db"
    if not p.exists():
        return 0
    con = sqlite3.connect("file:%s?mode=ro" % str(p).replace("\\", "/"), uri=True)
    try:
        return int(con.execute("SELECT count(*) FROM memories").fetchone()[0])
    finally:
        con.close()


def _ingest(client, text, **kw):
    kw.setdefault("agent_id", AGENT)
    kw.setdefault("postprocess", False)
    return client.ingest(text, **kw)


# ── ① 两方向 ────────────────────────────────────────────────────────────
def f1a_pure_pii_is_masked(tmp_path, monkeypatch) -> bool:
    """纯 PII（**无类别词**）⇒ 落库已掩码：三类各自 + 组合，且**明文一个都不许留**。

    性能：**一个 client 装全部样本**（每次 `Trinity()` 构造约 1-3s，逐样本构造会让判据跑到分钟级）。
    """
    cli = _client(tmp_path, monkeypatch)
    for kind, (text, plain, masked) in PURE_SAMPLES.items():
        mid = _ingest(cli, text).get("memory_id")
        if not mid:
            return False
        got = _stored(tmp_path, mid).get("content", "")
        if plain in got or masked not in got:
            return False
    mid = _ingest(cli, PURE_ALL).get("memory_id")
    got = _stored(tmp_path, mid).get("content", "")
    return ("13812345678" not in got and "110101199003071233" not in got
            and "zhangsan@corp.cn" not in got and "4111111111111111" not in got
            and "138********" in got and "110***************" in got)


def f1b_no_pii_is_verbatim(tmp_path, monkeypatch) -> bool:
    """**反事实（不得误伤）**：G1 §4.3 的真实误报集必须逐字不变（同一 client 批量写）。"""
    cli = _client(tmp_path, monkeypatch)
    for name, text in NO_PII_SAMPLES:
        mid = _ingest(cli, text).get("memory_id")
        if not mid:
            return False
        got = _stored(tmp_path, mid).get("content", "")
        if got != text:
            return False
    return True


# ── ② 类别命中仍掩码 + 标记在位 ─────────────────────────────────────────
def f2a_category_still_masked_with_marker(tmp_path, monkeypatch) -> bool:
    cli = _client(tmp_path, monkeypatch)
    mid = _ingest(cli, CATEGORY_TEXT).get("memory_id")
    row = _stored(tmp_path, mid)
    meta = _meta(row)
    scan = meta.get("sensitive_scan") or {}
    book = meta.get("pii_redaction") or {}
    return ("138********" in str(row.get("content", ""))
            and scan.get("severity") == "medium"
            and "psych_health" in (scan.get("categories") or [])
            and book.get("policy") == "category"
            and book.get("count", 0) >= 1)


def f2b_category_without_pii_no_redaction_book(tmp_path, monkeypatch) -> bool:
    """类别命中但**无 PII** ⇒ 内容不变、`sensitive_scan` 标记在位、且**不写掩码账本**。

    本条钉的是 G3 的原则「**账本必须等于实际发生的动作**」：
    没有发生掩码 ⇒ 不得出现 `metadata["pii_redaction"]`（否则账本又变成"说了不存在的动作"）。
    同时防"顺手把扫描标记也塞进掩码分支"—— 那样无 PII 的敏感文本会**失去审计标记**。
    """
    cli = _client(tmp_path, monkeypatch)
    text = "最近有点抑郁，睡得不好。"      # medium 类别、无任何 PII
    mid = _ingest(cli, text).get("memory_id")
    row = _stored(tmp_path, mid)
    meta = _meta(row)
    scan = meta.get("sensitive_scan") or {}
    return (row.get("content", "") == text
            and scan.get("severity") == "medium"
            and "pii_redaction" not in meta)


# ── ③ high 不得降级为"掩码后存" ─────────────────────────────────────────
def f3_high_families_refused_not_masked(tmp_path, monkeypatch) -> bool:
    """四家族 8 条真实 high 样本 ⇒ **全部拒存**，且**零行落库**（不得退化成"掩码后存"）。"""
    cli = _client(tmp_path, monkeypatch)
    for name, text in HIGH_SAMPLES:
        res = _ingest(cli, text)
        if res.get("error") != "policy_refused_sensitive" or res.get("memory_id", "") != "":
            return False
    return _row_count(tmp_path) == 0


def f3b_policy_action_high_is_not_redact() -> bool:
    """**同一条判据的第二层**：`policy_action(high)` 不得是 REDACT（面③ 不许被"顺手统一"）。"""
    for name, text in HIGH_SAMPLES:
        r = S.scan_sensitive(text)
        if r.get("severity") != "high" or S.policy_action(r) == S.ACTION_REDACT:
            return False
    return True


# ── ④ 回滚逐字（两档 + 三方逐字节比较）──────────────────────────────────
def f4a_switch_off_verbatim(tmp_path, monkeypatch) -> bool:
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    cli = _client(tmp_path, monkeypatch)
    for name, (text, _plain, _masked) in PURE_SAMPLES.items():
        got = _stored(tmp_path, _ingest(cli, text)["memory_id"]).get("content", "")
        if got != text:
            return False
    got = _stored(tmp_path, _ingest(cli, PURE_ALL)["memory_id"]).get("content", "")
    return got == PURE_ALL


def f4b_scope_category_is_precise_rollback(tmp_path, monkeypatch) -> bool:
    """`SCOPE=category` = **精确回滚 G2**：纯 PII 原样，类别文本仍掩码。"""
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT_SCOPE", "category")
    cli = _client(tmp_path, monkeypatch)
    pure = "发货联系人手机 13812345678，请仓库核对。"
    got_pure = _stored(tmp_path, _ingest(cli, pure)["memory_id"]).get("content", "")
    got_cat = _stored(tmp_path, _ingest(cli, CATEGORY_TEXT)["memory_id"]).get("content", "")
    return got_pure == pure and "138********" in got_cat


def f4c_two_rollback_paths_byte_identical(tmp_path, monkeypatch) -> bool:
    """**两档回滚的落库结果必须逐字节相同**（且都等于输入）——比"开关读了没"强。

    路径 A：`TRINITY_SENSITIVE_REDACT=0`
    路径 B：`TRINITY_SENSITIVE_REDACT_SCOPE=category`（对**纯 PII**文本而言等价于回滚）
    """
    text = "发货联系人手机 13812345678，邮箱 zhangsan@corp.cn，请核对。"
    d1 = tmp_path / "A"
    d2 = tmp_path / "B"
    d1.mkdir(parents=True, exist_ok=True)
    d2.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    a = _stored(d1, _ingest(_client(d1, monkeypatch), text)["memory_id"]).get("content", "")
    monkeypatch.delenv("TRINITY_SENSITIVE_REDACT", raising=False)
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT_SCOPE", "category")
    b = _stored(d2, _ingest(_client(d2, monkeypatch), text)["memory_id"]).get("content", "")
    return a == b == text


# ── ⑤ 写入路径覆盖（矩阵 + 显式未覆盖清单）─────────────────────────────
def f5a_client_ingestion_covered(tmp_path, monkeypatch) -> bool:
    cli = _client(tmp_path, monkeypatch)
    mid = _ingest(cli, PURE_ALL).get("memory_id")
    return "138********" in _stored(tmp_path, mid).get("content", "")


def _scan_for_redaction(src: str) -> list:
    """源码里是否出现**脱敏原语**（用于"未覆盖清单"的机器核对与自证）。"""
    return [p for p in _REDACTION_PRIMITIVES if p in src]


def f5c_uncovered_paths_have_no_redaction() -> bool:
    """未覆盖清单里的每条路径都必须**逐类**可机器核对：

    · `bare_sql_insert`（G7-R2 的 6 条盲区）：文件存在、含裸 `INSERT INTO memories`、**0 次**守卫/脱敏调用；
    · `batch_script_census`：`scripts/` 下含 `store_memory(`/`ingest_batch(` 的脚本数 ≥ 30（G7-R4 的剂量）；
    · 其余：文件存在且**自身不调用**脱敏原语（它们要么委托适配器、要么是另一套存储）。

    双向性：若哪天某条路径开始**自己**脱敏 ⇒ 本判据红 ⇒ **强制更新清单**（不许清单悄悄过期）。
    """
    for item in UNCOVERED_PATHS:
        kind = item.get("kind")
        if kind == "batch_script_census":
            hits = [p for p in (ROOT / "scripts").glob("*.py")
                    if _REDACTION_BATCH_RX.search(_read_source(p))]
            if len(hits) < 30:
                return False
            continue
        p = ROOT / item["path"]
        if not p.is_file():
            return False
        src = _read_source(p)
        # ── t52/G11：**函数级**条目（镜像/回填）只在函数体内核对 ──────────────────
        # 必要性：`postgresql.py` 整文件有 4 处守卫（store_memory/ingest_batch），
        # 按整文件核对会把"应然不掩"的镜像函数**误判**成已覆盖。
        scope_fn = (item.get("scope") or {}).get("function")
        if scope_fn:
            seg = _function_source(src, scope_fn)
            if not seg:
                return False
            if not re.search(r"(?i)insert\s+into\s+memories", seg):
                return False
            if "adapter_pii_guard" in seg or _scan_for_redaction(seg):
                return False
            continue
        if kind == "bare_sql_insert" and not re.search(r"(?i)insert\s+into\s+memories", src):
            return False          # 盲区证据（裸 SQL）不在 ⇒ 清单过期
        if _scan_for_redaction(src) or "adapter_pii_guard" in src:
            return False
    # t50：**已接守卫**的那 6 条，按**相反方向**核对（含守卫 + 守卫在裸 SQL 之前）
    # t52/G11：第 7 条（engine_worker）并入 ⇒ 本循环从"永远不触发"变成**双向可失败**
    for item in GUARDED_BARE_SQL_PATHS:
        p = ROOT / item["path"]
        if not p.is_file():
            return False
        src = _read_source(p)
        if not re.search(r"(?i)insert\s+into\s+memories", src):
            return False          # 它仍是裸 SQL（否则该从本表移走：改成走适配器了）
        if "adapter_pii_guard" not in src:
            return False          # 说好接了守卫却没接 ⇒ 清单过期
        lines = src.replace("\r\n", "\n").split("\n")
        try:
            g = next(i for i, line in enumerate(lines, 1)
                     if "adapter_pii_guard" in line and "import" not in line)
            ins = next(i for i, line in enumerate(lines, 1) if "INSERT INTO memories" in line)
        except StopIteration:
            return False
        if g > ins:
            return False          # 守卫在裸 SQL 之后 ⇒ 哈希/INSERT 用例先跑 ⇒ 不变量被破坏
    return True


def f5d_engine_worker_code_path_is_covered() -> bool:
    """`engine_worker.py` 的**代码路径**已覆盖（走客户端 ingest）；运行时另见 RUNTIME_GAPS。"""
    src = (ROOT / "trinity/engine_worker.py").read_text(encoding="utf-8", errors="replace")
    return ".ingest(" in src or "ingest_batch(" in src


def f5e_aggregator_pool_is_declared_uncovered() -> bool:
    """aggregator 池路径必须在**显式未覆盖清单**里被点名（不得沉默）。"""
    names = " ".join(i["path"] for i in UNCOVERED_PATHS)
    return "aggregator" in names


# ── ⑥ 响应字段 ⊨ 落库 ───────────────────────────────────────────────────
def f6a_response_matches_readback(tmp_path, monkeypatch) -> bool:
    """响应 `auto_redacted` / `pii_redacted_types` 必须与**读回内容**一致。"""
    cli = _client(tmp_path, monkeypatch)
    res = _ingest(cli, PURE_ALL)
    got = _stored(tmp_path, res.get("memory_id") or "").get("content", "")
    kinds = res.get("pii_redacted_types") or []
    if res.get("auto_redacted") is not True or not kinds:
        return False
    # 响应声称掩了手机号 ⇒ 落库必须真的没有明文手机号（方向性一致，而非仅"非空"）
    return ("手机号" in " ".join(map(str, kinds))) and ("13812345678" not in got)
    # 注：本函数的返回值不含"逐字节相同"，因为掩码是幂等改写；见 f6b 的反事实方向。


def f6b_response_never_claims_when_clean(tmp_path, monkeypatch) -> bool:
    """反事实：无 PII ⇒ 响应必须 `false`/`[]`（防"恒真"）。"""
    cli = _client(tmp_path, monkeypatch)
    res = _ingest(cli, NO_PII_SAMPLES[1][1])
    return res.get("auto_redacted") is False and (res.get("pii_redacted_types") or []) == []


CRITERIA = {
    "F1a_纯PII落库已掩码": f1a_pure_pii_is_masked,
    "F1b_无PII逐字不变": f1b_no_pii_is_verbatim,
    "F2a_类别仍掩码带标记": f2a_category_still_masked_with_marker,
    "F2b_类别无PII不写掩码账本": f2b_category_without_pii_no_redaction_book,
    "F3_high四家族拒存零落库": f3_high_families_refused_not_masked,
    "F4a_开关关逐字": f4a_switch_off_verbatim,
    "F4b_SCOPE精确回滚": f4b_scope_category_is_precise_rollback,
    "F4c_两档回滚逐字节一致": f4c_two_rollback_paths_byte_identical,
    "F5a_client写入已掩码": f5a_client_ingestion_covered,
    "F6a_响应与落库一致": f6a_response_matches_readback,
    "F6b_无PII响应为假": f6b_response_never_claims_when_clean,
}


# ── 正例 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_判据通过(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path / "main", monkeypatch) is True, (
        "判据 %s 未通过 —— 见 tests/unit/test_redact_scope_20261006.py 该函数的注释" % name)


@pytest.mark.parametrize("fn,name", [
    (f3b_policy_action_high_is_not_redact, "F3b_policy_action_high_不得是_redact"),
    (f5c_uncovered_paths_have_no_redaction, "F5c_未覆盖清单无脱敏调用"),
    (f5d_engine_worker_code_path_is_covered, "F5d_engine_worker代码路径覆盖"),
    (f5e_aggregator_pool_is_declared_uncovered, "F5e_aggregator必须在未覆盖清单"),
])
def test_非写入类判据通过(fn, name):
    assert fn() is True, "判据 %s 未通过" % name


def test_未覆盖清单必须显式且可机器核对():
    """**"未覆盖清单必须是显式的，不得沉默"** —— 这条本身要被机器核对：

    ① 清单非空且 ≥5 条（G1 §5 的 5 类）；② 每条都有 `path` + `reason`；
    ③ 清单里的源码路径**确实没有**脱敏调用（`f5c` 已验，这里再复核一次清单形状）；
    ④ `RUNTIME_GAPS` 非空且点名 engine_worker（运行时装旧代码这件事必须被写下来）。
    """
    # t50：6 条裸 SQL 已修 ⇒ 从"未覆盖"移到"已接守卫"，**计数按两表合计**（门槛不降低）
    # t52/G11：两表各加 1 条（engine_worker 已守卫 / postgresql:1565 镜像回填）
    #   ⇒ 门槛从 ≥10 提到 **≥12**，并**逐条点名**新增项（门槛只升不降，且覆盖新增）
    assert len(UNCOVERED_PATHS) + len(GUARDED_BARE_SQL_PATHS) >= 12, (
        "清单过短 ⇒ G1 §5 + G7-R2 + verifier 独立枚举的路径没被如实登记（两表合计）")
    assert len(GUARDED_BARE_SQL_PATHS) >= 7, (
        "G7-R2 的 6 条 + t52 新增的 engine_worker 必须都在「已守卫」表里")
    assert any(i["path"] == "trinity/engine_worker.py" and i.get("kind") == "bare_sql_insert"
               for i in GUARDED_BARE_SQL_PATHS), (
        "verifier 独立枚举出的 engine_worker（t51 已接守卫）必须登记进 GUARDED_BARE_SQL_PATHS")
    assert any(i.get("kind") == "mirror_backfill" and "postgresql.py" in i["path"]
               for i in UNCOVERED_PATHS), (
        "postgresql.py::migrate_from_sqlite（镜像/回填，应然不掩）必须登记进 UNCOVERED_PATHS")
    for item in UNCOVERED_PATHS + GUARDED_BARE_SQL_PATHS:
        assert item.get("path") and item.get("reason"), item
        assert (ROOT / item["path"]).exists(), "清单里的路径不存在：%s" % item
        assert item.get("kind"), "每条必须带 kind（否则'哪一类'不可核对）：%s" % item
    assert sum(1 for i in UNCOVERED_PATHS + GUARDED_BARE_SQL_PATHS
               if i.get("kind") == "bare_sql_insert") >= 6, (
        "G7-R2 的 **6 条裸 SQL 直写**必须逐条点名（这是本次最容易沉默的一面）")
    joined = " ".join(g["what"] + g["why"] for g in RUNTIME_GAPS)
    assert "engine_worker" in joined, "运行时装旧代码（需 respawn）这件事必须写进 RUNTIME_GAPS"
    assert "PG" in joined and "未验证" in joined, (
        "G7-R5：PG 侧守卫**运行时未验证**这一点必须写进未验证清单（不得写成已覆盖）")


# ── ⑤b API 写入路径（本进程 TestClient + 临时库）────────────────────────
def test_API_POST_memories_也脱敏(tmp_path, monkeypatch):
    """**API 面**：本进程 `TestClient(app)` + 临时库 ⇒ 落库已掩码、响应字段为真。

    ## order-robust（t56 修复）
    **套件上下文的坑（实测复现）**：全量里更早的测试可能已经调用过
    `trinity/api/server/_deps.py::get_memory()` ⇒ 模块全局 `_memory` 被钉在**那个测试的 store** 上；
    此时本用例再改 `TRINITY_STORE` **无效** ⇒ POST 写进别人的库 ⇒ 我在自己的临时库读不到 ⇒ 响亮失败
    （t54 的 skip 门禁**正确发火**；实测 `evidence/t56-api-probe.txt`：库 A 行数 1、库 B 不存在）。

    **修法（不碰产品代码）**：`_deps.py:422-433` 明说端点通过 `_live_memory()` **在调用时**
    从 `trinity.api.server` 包属性取 `get_memory`（就是为了支持 monkeypatch）
    ⇒ 这里把包属性换成**绑到我临时库的客户端**，写入目标即与 `tmp_path` 一致，**与执行顺序无关**。
    probe 实测该修法后：POST 落进我的库、读回为掩码文本（`evidence/t56-api-probe.txt`）。

    **何时不适用（显式条件，不是静默 skip）**：若本进程**无法构造 app/TestClient**
    （缺 starlette、app 导入期抛错），则 `_env_gate_fail()` 响亮失败并记账本 ——
    本判据在该环境**不适用**，但**绝不静默通过**（t54/G13 纪律）。
    """
    import os
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("TRINITY_STORE", str(tmp_path))
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "trinity_store.db"))
    monkeypatch.setenv("TRINITY_MEMORY_ENABLED", "0")
    try:
        from starlette.testclient import TestClient
        from trinity.api.server import app
        import trinity.api.server as server_pkg
    except Exception as exc:  # noqa: BLE001
        # t54/G13：原来是 `pytest.skip`（逃逸口）⇒ 改为**响亮失败**：本判据不接受静默降级。
        _env_gate_fail("API 面无法在本进程构造（%s: %s）⇒ 本判据不覆盖 API 写入路径"
                       % (type(exc).__name__, str(exc)[:120]))
    # ★ order-robust：把端点解析用的 `get_memory` 换成绑到本用例临时库的客户端
    #   （覆盖"更早的测试已把 _deps._memory 钉在别的 store 上"这一套件上下文）
    _my_engine = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(server_pkg, "get_memory", lambda: _my_engine)
    text = "发货联系人手机 13812345678，邮箱 zhangsan@corp.cn。"
    try:
        with TestClient(app) as client:
            r = client.post("/memories", json={"content": text, "agent_id": AGENT,
                                               "persona_id": AGENT},
                            headers={"X-Agent-ID": AGENT})
    except Exception as exc:  # noqa: BLE001
        _env_gate_fail("TestClient 跑不起来（%s: %s）⇒ 同上去掉静默降级"
                       % (type(exc).__name__, str(exc)[:120]))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body.get("auto_redacted") is True, "API 写入的响应必须如实报已脱敏：%r" % body
    assert body.get("pii_redacted_types"), body
    mid = body.get("memory_id")
    assert mid, body
    got = _stored(tmp_path, mid).get("content", "")
    if not got:                     # app 没路由到我的临时库 ⇒ 响亮失败，不假装通过
        _env_gate_fail("API 未使用本测试的临时库（读不到 %s）⇒ API 面判据在这台机器上没生效" % mid)
    assert "13812345678" not in got, "API 写入路径仍落明文 ⇒ 该面未覆盖"
    assert "138********" in got, got
    assert os.path.exists(str(tmp_path / "trinity_store.db"))


def test_G15_牙齿_不换get_memory就会写到别人的库(tmp_path, monkeypatch):
    """**order-robust 的牙齿（t56）**：证明"套件上下文"这个坑**真实存在**且修法是承重的。

    做法：先人为把 `trinity/api/server/_deps.py::_memory` 钉到**别的库**（模拟全量里更早的测试
    已经建过单例），然后**故意不换 `get_memory`**（= t56 之前的做法）走一次 API 写入
    ⇒ 断言它**写进了别人的库、我的库里没有**。
    ⇒ 若哪天有人把这套机制改掉（例如端点不再在调用时解析 `get_memory`），本条会先红，
    提醒"API 面判据的 order-robust 前提变了"。
    """
    from starlette.testclient import TestClient
    import trinity.api.server as server_pkg
    from trinity.api.server import _deps, app

    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    other = tmp_path / "other"
    mine = tmp_path / "mine"
    other.mkdir()
    mine.mkdir()
    if not hasattr(_deps, "_memory"):        # 机制变了 ⇒ 本条不适用（显式失败，不静默）
        pytest.fail("`_deps._memory` 单例不存在 ⇒ API 面 order-robust 的前提已变，请复核本判据")
    monkeypatch.setattr(_deps, "_memory", _client(other, monkeypatch))   # 污染：钉在别人的库
    monkeypatch.setenv("TRINITY_STORE", str(mine))
    monkeypatch.setenv("TRINITY_DB_PATH", str(mine / "trinity_store.db"))

    with TestClient(app) as client:
        r = client.post("/memories", json={"content": "手机 13812345678。", "agent_id": AGENT,
                                           "persona_id": AGENT},
                        headers={"X-Agent-ID": AGENT})
    assert r.status_code == 200, r.text[:200]
    other_db = other / "trinity_store.db"
    mine_db = mine / "trinity_store.db"
    # 未换 get_memory ⇒ 写进了被污染的库；我的库里没有
    assert other_db.exists() and _count_rows(other_db) >= 1, (
        "污染实验没生效 ⇒ 本条牙齿失效（请复核单例机制）")
    assert not mine_db.exists() or _count_rows(mine_db) == 0, (
        "未换 get_memory 竟然也写到了我的库 ⇒ 危险场景不成立，本条牙齿应删除并说明")
    _ = server_pkg        # 修法用的正是这个包属性（正式判据里已换）


def _count_rows(db_path) -> int:
    p = Path(db_path)
    if not p.exists():
        return 0
    con = sqlite3.connect("file:%s?mode=ro" % str(p).replace("\\", "/"), uri=True)
    try:
        return int(con.execute("SELECT count(*) FROM memories").fetchone()[0])
    finally:
        con.close()


# ── 牙齿：≥6 条，逐条"人为破坏 ⇒ 判据必须红" ────────────────────────────
def _pii_never(_text, **_kw):
    return {"flagged": False, "kinds": [], "hits": [], "count": 0}


def _sensitive_never(_content):
    return {"flagged": False, "severity": None, "policy": None, "action": S.ACTION_STORE,
            "categories": [], "hits": [], "truncated": False, "downgraded": False,
            "pii": _pii_never("")}


def _sensitive_always_redact(_content):
    return {"flagged": False, "severity": None, "policy": None, "action": S.ACTION_REDACT,
            "categories": [], "hits": [], "truncated": False, "downgraded": False,
            "pii": _pii_never("")}


def _mask_everything(_text, **_kw):
    return "***（变异体：整体改写）", ["一切×1"]


def _high_downgraded_to_redact(report):
    """把 high 降级成 redact ⇒ "掩码后存"（面③ 最容易被顺手统一掉的那条边界）。"""
    if report.get("severity") == "high":
        return S.ACTION_REDACT
    return S.policy_action(report)


def _f1b_mutant(mp):
    """F1b 的变异体必须**成对**（只改"决定"或只改"掩码器"都改写不了干净文本）。"""
    return (_patch_policy(mp, "scan_sensitive", _sensitive_always_redact)
            + _patch_policy(mp, "redact_identifiers", _mask_everything))



# ── t93/C1：变异体必须"patch 到底"（否则在长进程里是 no-op）────────────────────
#: t162/G19：**本文件自己的"不静默"收集器**（此前本文件的四处 `except: pass` 是静默的；
#: 同族的 `test_sensitive_pii_scope_20261006.py` 早就用 `_patch_errors` 收集 —— 这里对齐该惯用法）。
_patch_errors: list = []


def _all_sensitive_modules():
    """**所有** `trinity.security.sensitive` 实例 —— 含运行时被重新导入出来的**重复实例**。

    实测（`evidence/t93_dup_module_probe.py`）：进程里存在第二个实例时，写路径读到的是那一个，
    只 patch 收集期绑定的 `S` 就完全不起作用 ⇒ 变异体变 no-op、元判据误报"判据没有判别力"。
    """
    out, seen = [], set()
    try:
        import importlib
        cand = [importlib.import_module("trinity.security.sensitive")]
    except Exception:  # noqa: BLE001
        cand = []
    try:
        import gc
        import types
        for obj in gc.get_objects():
            if isinstance(obj, types.ModuleType) and str(getattr(obj, "__name__", "")).endswith(
                    "security.sensitive"):
                cand.append(obj)
    except Exception as _e:  # noqa: BLE001 —— 不静默（t162/G19：原先这里是 silence）
        _patch_errors.append("gc scan: %r" % (_e,))
    if S not in cand:
        cand.append(S)
    for m in cand:
        if id(m) not in seen:
            seen.add(id(m))
            out.append(m)
    return out


def _patch_policy(mp, name, new):
    """把 `name` 替换成 `new`，覆盖**所有**持有者；返回被替换的持有者数量。

    返回 0 表示"没改到任何东西" ⇒ 元判据会**直接红**（而不是静默通过）。
    """
    n = 0
    orig = None
    for m in _all_sensitive_modules():
        if getattr(m, "__name__", "") == "trinity.security.sensitive" and hasattr(m, name):
            orig = getattr(m, name)
            break
    targets = list(_all_sensitive_modules())
    for m in targets:
        try:
            if hasattr(m, name):
                mp.setattr(m, name, new, raising=False)
                n += 1
        except Exception as _e:  # noqa: BLE001 —— 不静默（t162/G19）
            _patch_errors.append("setattr %s@%s: %r" % (name, getattr(m, "__name__", "?"), _e))
    try:
        for mod in list(sys.modules.values()):
            if mod is None or mod in targets:
                continue
            try:
                if orig is not None and getattr(mod, name, None) is orig:
                    mp.setattr(mod, name, new, raising=False)
                    n += 1
            except Exception as _e:  # noqa: BLE001 —— 不静默（t162/G19）
                _patch_errors.append("setattr %s@%s: %r" % (name, getattr(mod, "__name__", "?"), _e))
    except Exception as _e:  # noqa: BLE001 —— 不静默（t162/G19）
        _patch_errors.append("module sweep: %r" % (_e,))
    return n



def _f3_mutant(mp):
    """F3 的变异体：**关掉扫描** ⇒ 客户端不再识别 high ⇒ **不再拒存**（改为照常落行）⇒ F3 必红。

    ⚠️ 前两版为何是 no-op（t100 实测，两版都跑过）：
      ① patch `scan_sensitive`（模块属性）：F3 走**客户端写路径**，长进程里策略函数可能已被捕获
         ⇒ patch 到不了它 ⇒ 变异体 no-op（全量剩下的那条红就是这个）；
      ② patch `SQLiteAdapter.store_memory`（类属性）：**客户端在调用适配器之前就已经拒存**
         ⇒ 适配器根本没被调用 ⇒ "重试为掩码后存"永不执行 ⇒ 实测 F3 仍 TRUE。
    ⇒ 打**开关**：`TRINITY_SENSITIVE_SCAN=off` 每次调用都读，且语义**正是判据所守**的
      "high 必须拒存、不得退化成掩码后存"。
    """
    mp.setenv("TRINITY_SENSITIVE_SCAN", "off")
    return 1

MUTANTS = [
    ("F1a_纯PII落库已掩码", lambda mp: _patch_policy(mp, "scan_pii", _pii_never)),
    ("F1b_无PII逐字不变", _f1b_mutant),
    ("F2a_类别仍掩码带标记", lambda mp: _patch_policy(mp, "scan_sensitive", _sensitive_never)),
    ("F2b_类别无PII不写掩码账本", lambda mp: _patch_policy(mp, "scan_sensitive", _sensitive_never)),
    # 面③ 的写路径牙齿：让 high 文本被判 redact（= "掩码后存"）⇒ 拒存断言必须红。
    # （注意：不能只 patch `policy_action` —— `_ingestion` 判的是 **scan_sensitive 报告里的 action**，
    #   那条路径的牙齿另由 `test_牙齿_F3b_的变异体必杀` 覆盖。）
    # t100/C1R：改打在**写路径的类属性**上（类属性 patch 在长进程里稳定生效，见 `_f3_mutant`）
    ("F3_high四家族拒存零落库", _f3_mutant),
    ("F4a_开关关逐字", lambda mp: _patch_policy(mp, "sensitive_redact_enabled", lambda: True)),
    ("F4b_SCOPE精确回滚", lambda mp: _patch_policy(mp, "sensitive_redact_scope", lambda: "all_pii")),
    ("F4c_两档回滚逐字节一致",
     lambda mp: _patch_policy(mp, "sensitive_redact_enabled", lambda: True)),
    ("F5a_client写入已掩码", lambda mp: _patch_policy(mp, "scan_pii", _pii_never)),
    ("F6a_响应与落库一致", lambda mp: _lie_in_adapter(mp)),
    ("F6b_无PII响应为假", lambda mp: _always_claim_redacted(mp)),
]


def _lie_in_adapter(mp):
    """把适配器返回值改成**修前形态**（恒 `False`/`[]`）⇒ 面⑥ 必须红。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    orig = SQLiteAdapter.store_memory

    def wrapper(self, *a, **kw):
        out = orig(self, *a, **kw)
        if isinstance(out, dict):
            out = dict(out)
            out["auto_redacted"] = False
            out["pii_redacted_types"] = []
        return out

    mp.setattr(SQLiteAdapter, "store_memory", wrapper)


def _always_claim_redacted(mp):
    """让响应**恒真**（声称掩了）⇒ 面⑥ 的反事实方向必须红。"""
    from trinity.adapters.sqlite import SQLiteAdapter
    orig = SQLiteAdapter.store_memory

    def wrapper(self, *a, **kw):
        out = orig(self, *a, **kw)
        if isinstance(out, dict):
            out = dict(out)
            out["auto_redacted"] = True
            out["pii_redacted_types"] = ["手机号×1"]
        return out

    mp.setattr(SQLiteAdapter, "store_memory", wrapper)


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_每条判据都有能杀掉它的变异体(name, apply_mutant, tmp_path, monkeypatch):
    crit = CRITERIA[name]
    n_patched = apply_mutant(monkeypatch)
    # t93：变异体必须**确实施加到了持有者**（否则"杀不掉"是伪结论，不得静默通过）
    if n_patched is not None:
        assert n_patched >= 1, (
            "变异体 %s 没有改到任何持有者 ⇒ 结论不可信（不是判据没有判别力）" % name)
    sub = tmp_path / name            # 子目录（不要用 TemporaryDirectory：SQLite 句柄未关会 WinError 32）
    sub.mkdir()
    assert crit(sub, monkeypatch) is False, (
        "变异体没有杀掉判据 %s ⇒ 该判据没有判别力" % name)


def test_牙齿_F3b_的变异体必杀(monkeypatch):
    monkeypatch.setattr(S, "policy_action", _high_downgraded_to_redact)
    assert f3b_policy_action_high_is_not_redact() is False


def test_牙齿_未覆盖清单扫描器必须能抓到_自证():
    """"未覆盖清单"这条判据的牙齿：给一段**真的**调用脱敏原语的合成源码 ⇒ 扫描器必须抓到。

    否则 `f5c` 就只是"扫不到东西也算通过"的空转。
    """
    assert _scan_for_redaction("x = 1\n") == [], "干净源码不该被抓到"
    assert _scan_for_redaction("from trinity.security.sensitive import redact_identifiers\n"
                               "out, _ = redact_identifiers(t)"), "合成调用源码必须被抓到"


# ── 性能回归基线（留存数字）──────────────────────────────────────────────
def test_性能_每次写入多一次PII扫描的代价(capsys):
    """量 `scan_pii` + `redact_identifiers` 的 p50/p95（留作回归基线，不设紧阈值）。"""
    text = PURE_ALL + "订单号 DO-20260902-1188 库存锁定失败。" * 2
    n = 200
    t_scan, t_red = [], []
    for _ in range(n):
        t0 = time.perf_counter()
        S.scan_pii(text)
        t_scan.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        S.redact_identifiers(text)
        t_red.append((time.perf_counter() - t0) * 1000)
    p50s, p95s = statistics.median(t_scan), sorted(t_scan)[int(0.95 * n) - 1]
    p50r, p95r = statistics.median(t_red), sorted(t_red)[int(0.95 * n) - 1]
    with capsys.disabled():
        print("\n[G4 性能基线] n=%d 长度=%d 字符" % (n, len(text)))
        print("  scan_pii            p50=%.4f ms  p95=%.4f ms" % (p50s, p95s))
        print("  redact_identifiers  p50=%.4f ms  p95=%.4f ms" % (p50r, p95r))
        print("  每次写入的额外代价 ≈ scan(+redact) p50=%.4f ms  p95=%.4f ms"
              % (p50s + p50r, p95s + p95r))
    assert p95s < 25.0 and p95r < 25.0, (
        "PII 扫描 p95 超过 25ms ⇒ 写入路径的额外代价需要复核：scan p95=%.3f redact p95=%.3f"
        % (p95s, p95r))


# ══════════════════════════════════════════════════════════════════════════
# G7（t48）补面：**适配器写入边界**的守卫 —— 直接写入 / 开关组合 / 两通道 high /
#            裸 SQL 盲区 / PG 未运行时验证 / scripts 默认被掩码
#
# 与 `tests/unit/test_adapter_pii_guard_20261006.py`（t48 自己的 9 条）**不重复**：
# 那里覆盖「直写掩码 / 无 PII 逐字 / REDACT=0 / ADAPTER_GUARD=0 / 单条 high 拒存 /
# 幂等 / 只转接不另造策略 / 覆盖链 / 幂等契约」；本文件补的是它**没有**的：
#   · 开关**组合**语义（GUARD × REDACT 的 2×2）
#   · **两通道 high 契约**（单条 vs 批量）与跨后端差异（**不断言两者相同**）
#   · **裸 SQL 盲区**的运行时复现 + 6 条路径的机器核对
#   · **PG 侧未运行时验证**（G7-R5）的显式登记
#   · `scripts/` 默认被掩码（G7-R4）的**可核对披露**
# 命名对齐 t44 那条被下沉改变了前提的既有判据：**引用它、不复制**（见 `G7_REFERENCED`）。
# ══════════════════════════════════════════════════════════════════════════

#: t44 的既有判据：下沉后其前提被改变，G7 的做法是**原位加说明 + 用 ADAPTER_GUARD=0 模拟旧形状**。
#: 本文件**只引用不复制**（防两套漂移）。
G7_REFERENCED = {
    "file": "tests/unit/test_redact_response_field_20261006.py",
    "test": "test_牙齿_没有账本时不得报已脱敏_t44",
    "switch": "TRINITY_ADAPTER_GUARD=0",
}

#: G7-R5：PG 侧守卫**代码已挂、运行时未验证**（硬约束：不写生产 PG）
PG_GUARD_SITE = "trinity/adapters/postgresql.py"

#: G7-R4：行为变更披露（脚本默认被掩码 & 不可逆）
BEHAVIOR_CHANGES = [
    {"what": "适配器写入边界（G7/t48）", "effect": "直写适配器的路径现在也会被掩码",
     "reversible": "可（TRINITY_SENSITIVE_REDACT=0 或 TRINITY_ADAPTER_GUARD=0）"},
    {"what": "scripts/ 下 35 个批处理脚本", "effect": "G7-R4：**默认被掩码**（值从「原文」变「掩码」）",
     "reversible": "已掩码的内容**不可逆**（原文不可还原）；复跑前须显式声明开关"},
]


def _adapter(tmp_path):
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=str(tmp_path / "trinity_store.db"))
    ad.connect()
    return ad


def _meta_of(ad, mid) -> dict:
    row = ad.get_memory(mid) or {}
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _adapter_direct_write_masked(tmp_path, monkeypatch, *, guard: bool = True) -> bool:
    """**G7 核心**：`adapter.store_memory()` 直写（不走 client）含 PII ⇒ 掩码 + 账本标 `layer=adapter`。"""
    if not guard:
        monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    ad = _adapter(tmp_path)
    try:
        res = ad.store_memory(content=PURE_ALL, agent_id=AGENT, persona_id=AGENT)
        mid = res.get("memory_id")
        if not mid:
            return False
        content = (ad.get_memory(mid) or {}).get("content", "")
        book = _meta_of(ad, mid).get("pii_redaction") or {}
        if not guard:
            # 守卫关 ⇒ 直写不掩码，且**不得**留下账本
            return ("13812345678" in content) and not book
        return ("13812345678" not in content
                and "138********" in content
                and book.get("layer") == "adapter"
                and "adapter_guard" in str(book.get("scanner"))
                and res.get("auto_redacted") is True
                and res.get("redaction_source") == "adapter")
    finally:
        ad.disconnect()


def _guard_switch_combination_semantics(tmp_path, monkeypatch) -> bool:
    """**开关组合语义**（t48 只单独测了两个开关）：GUARD=0 时

    · **适配器直写** ⇒ 不掩码（退出成功）；
    · **经客户端 `ingest`** ⇒ **仍然掩码**（客户端层不受 adapter 开关影响）。
    ⇒ 两级开关是**独立的**；组合起来才是"完全不掩码"。
    """
    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    d1 = tmp_path / "direct"
    d2 = tmp_path / "client"
    d1.mkdir(parents=True, exist_ok=True)
    d2.mkdir(parents=True, exist_ok=True)
    if not _adapter_direct_write_masked(d1, monkeypatch, guard=False):
        return False
    cli = _client(d2, monkeypatch)
    res = _ingest(cli, PURE_ALL)
    got = _stored(d2, res.get("memory_id") or "").get("content", "")
    return ("138********" in got) and (res.get("pii_redacted_types") or []) != []


def _two_channel_high_contract(tmp_path, monkeypatch) -> bool:
    """**两通道 high 契约**：分别钉住**各自**的声明，**不断言两者相同**。

    实测（SQLite，本判据内）：单条 `store_memory(high)` = **拒存**（error + 无行）；
    批量 `ingest_batch([high])` 也走同一条守则 ⇒ **拒存**（error 形状相同）。
    而 **PG 的批量通道源码是"隔离"**（`_pii_iso ⇒ status='archived'`，为保 1:1 records↔rows）。
    ⇒ 本判据断言：① 单条必须拒存；② 批量必须**属于 {拒存, 隔离} 二者之一且留下可读的证据**；
    ③ **PG 批量通道的隔离契约在源码里必须在位**（否则两后端语义无人钉）。
    **跨后端不一致本身**作为发现上报（见报告 §G7-③），本判据不把它断言成"一致"。
    """
    ad = _adapter(tmp_path)
    try:
        single = ad.store_memory(content=HIGH_SAMPLES[0][1], agent_id=AGENT, persona_id=AGENT)
        ok_single = (not single.get("memory_id")) and ("refus" in str(single.get("error", "")).lower())
        batch = ad.ingest_batch([{"content": HIGH_SAMPLES[0][1], "agent_id": AGENT,
                                  "persona_id": AGENT}])
        b0 = batch[0] if batch else {}
        if not isinstance(b0, dict):
            return False
        refuses = ("refus" in str(b0.get("error", "")).lower()) and (not b0.get("memory_id"))
        quarantines = (b0.get("status") == "archived") or (b0.get("pii_isolated") is True)
        ok_batch = refuses or quarantines
        rows = 0
        mid = b0.get("memory_id") or ""
        if mid:
            rows = 1 if ad.get_memory(mid) else 0
        ok_no_row_when_refused = (not refuses) or rows == 0
    finally:
        ad.disconnect()
    pg_src = (ROOT / PG_GUARD_SITE).read_text(encoding="utf-8", errors="replace")
    ok_pg_contract = ("_pii_g.get(\"isolate\")" in pg_src) and ('_status = "archived"' in pg_src)
    return bool(ok_single and ok_batch and ok_no_row_when_refused and ok_pg_contract)


def _is_blind_spot(src: str) -> bool:
    """**裸 SQL 盲区**的判定式：写了 `INSERT INTO memories` 且**没有**经过适配器守卫。"""
    return bool(re.search(r"(?i)insert\s+into\s+memories", src)) and ("adapter_pii_guard" not in src)


def _raw_sql_blind_spot(tmp_path, monkeypatch) -> bool:
    """**G7-R2 盲区**：裸 `INSERT INTO memories`（绕过适配器）⇒ 守卫不生效、明文落库。

    运行时复现 + 6 条路径的机器核对（每条都必须含裸 SQL 且 0 次守卫调用）。
    """
    ad = _adapter(tmp_path)
    ad.store_memory(content="初始化", agent_id=AGENT, persona_id=AGENT)   # 建表
    ad.disconnect()
    db = str(tmp_path / "trinity_store.db")
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT INTO memories (memory_id, content, agent_id, persona_id, status, "
                    "category, created_at, updated_at) VALUES (?,?,?,?,?,?,datetime('now'),"
                    "datetime('now'))",
                    ("m_raw_blind", PURE_ALL, AGENT, AGENT, "active", "general"))
        con.commit()
    finally:
        con.close()
    ad2 = _adapter(tmp_path)
    try:
        got = (ad2.get_memory("m_raw_blind") or {}).get("content", "")
        book = _meta_of(ad2, "m_raw_blind").get("pii_redaction")
    finally:
        ad2.disconnect()
    # 📌 **历史（原位保留）**：t50 之前这里断言"盲区存在"（明文落库 + 6 条路径都无守卫）。
    # t50/G9 把 6 条**代码路径**接上守卫后，本条**原位改断言方向**（不是删掉、不是放宽）：
    #   ① 上面这段**纯 sqlite3 直连**的写入**仍然**是明文 —— 这是**外进程残留**，进程内守卫
    #      **原则上无法覆盖**（它根本不经过本仓库代码）⇒ 显式承认，而不是假装它已被修好；
    #   ② 6 条**代码路径**改由 `GUARDED_BARE_SQL_PATHS` 按相反方向核对（见下方与 f5c）。
    if "13812345678" not in got or book:
        return False                       # 纯 SQL 直连竟被掩码了 ⇒ 前提变了，回来更新说明
    if len(GUARDED_BARE_SQL_PATHS) < 7:
        return False                       # t52：门槛随新增条目只升不降
    for item in GUARDED_BARE_SQL_PATHS:
        src = _read_source(ROOT / item["path"])
        if not re.search(r"(?i)insert\s+into\s+memories", src):
            return False
        if "adapter_pii_guard" not in src:
            return False
    return True


def _pg_guard_code_present_but_not_runtime_verified() -> bool:
    """**G7-R5**：PG 侧守卫**代码已在位**（`adapter_pii_guard` 调用 + `isolate` 处理），

    但**运行时未验证**（不写生产 PG）⇒ 必须出现在 `RUNTIME_GAPS` 里且写明"未验证"。
    """
    src = (ROOT / PG_GUARD_SITE).read_text(encoding="utf-8", errors="replace")
    if src.count("adapter_pii_guard") < 2:           # store_memory + ingest_batch 两处
        return False
    if '_status = "archived"' not in src:
        return False
    for gap in RUNTIME_GAPS:
        if "PG" in gap["what"] and "未验证" in (gap["why"] or ""):
            return True
    return False


def _scripts_default_masked_disclosed() -> bool:
    """**G7-R4**：`scripts/` 默认被掩码这件事必须**被披露且可核对**（数剂量 ≥ 30 个脚本）。"""
    hits = [p for p in (ROOT / "scripts").glob("*.py")
            if _REDACTION_BATCH_RX.search(p.read_text(encoding="utf-8", errors="replace"))]
    if len(hits) < 30:
        return False
    joined = " ".join(c["what"] + c["effect"] + c["reversible"] for c in BEHAVIOR_CHANGES)
    return ("scripts/" in joined) and ("不可逆" in joined) and ("ADAPTER_GUARD" in joined)


def _t44_criterion_referenced_not_duplicated() -> bool:
    """**引用而非复制**：t44 那条被下沉改变前提的判据必须仍在原文件里，且用 ADAPTER_GUARD 模拟旧形状。"""
    p = ROOT / G7_REFERENCED["file"]
    if not p.is_file():
        return False
    src = p.read_text(encoding="utf-8", errors="replace")
    return G7_REFERENCED["test"] in src and G7_REFERENCED["switch"] in src


# ── G7 判据的正例 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "G7-① 适配器直写含 PII ⇒ 掩码 + layer=adapter",
    "G7-② 开关组合：GUARD=0 直写不掩、客户端仍掩",
    "G7-③ 两通道 high 契约（单条/批量，各自钉）",
    "G7-④ 裸 SQL 盲区运行时复现 + 6 条路径核对",
    "G7-⑤ PG 守卫代码在位但**运行时未验证**（G7-R5）",
    "G7-⑥ scripts 默认被掩码已披露且可核对（G7-R4）",
    "G7-⑦ t44 既有判据被引用而非复制",
])
def test_G7_判据通过(name, tmp_path, monkeypatch):
    sub = tmp_path / "g7"
    sub.mkdir(parents=True, exist_ok=True)
    checks = {
        "G7-① 适配器直写含 PII ⇒ 掩码 + layer=adapter":
            lambda: _adapter_direct_write_masked(sub / "a", monkeypatch),
        "G7-② 开关组合：GUARD=0 直写不掩、客户端仍掩":
            lambda: _guard_switch_combination_semantics(sub / "b", monkeypatch),
        "G7-③ 两通道 high 契约（单条/批量，各自钉）":
            lambda: _two_channel_high_contract(sub / "c", monkeypatch),
        "G7-④ 裸 SQL 盲区运行时复现 + 6 条路径核对":
            lambda: _raw_sql_blind_spot(sub / "d", monkeypatch),
        "G7-⑤ PG 守卫代码在位但**运行时未验证**（G7-R5）": _pg_guard_code_present_but_not_runtime_verified,
        "G7-⑥ scripts 默认被掩码已披露且可核对（G7-R4）": _scripts_default_masked_disclosed,
        "G7-⑦ t44 既有判据被引用而非复制": _t44_criterion_referenced_not_duplicated,
    }
    assert checks[name]() is True, "G7 判据未通过：%s" % name


# ── G7 判据的牙齿（人为破坏 ⇒ 必须红）──────────────────────────────────
def _guard_never_refuses(content, metadata=None):
    """变异体：守卫**永不拒存**（high 也放行）⇒ G7-③ 必须红。"""
    return content, metadata, {"scanned": True, "redacted": False, "labels": [], "severity": None,
                               "refuse": False, "isolate": False, "exempt": None, "policy": None}


def test_牙齿_G7_1_守卫被关掉时判据必红(tmp_path, monkeypatch):
    """把守卫判定改成"永远不介入"⇒ 直写掩码判据必须红。"""
    import trinity.adapters._pii_guard as G
    # t49 起 `adapter_pii_guard` 走 `adapter_guard_state()`（不再读 `adapter_guard_enabled`）
    # ⇒ 旧写法已失效（t50 实测：牙齿打不死）。这里改到**当前接口**，牙齿重新有效。
    monkeypatch.setattr(G, "adapter_guard_state", lambda: (False, "tooth"))
    sub = tmp_path / "t1"
    sub.mkdir()
    assert _adapter_direct_write_masked(sub, monkeypatch) is False


def test_牙齿_G7_2_把适配器开关读成恒真即报红(tmp_path, monkeypatch):
    """把 `adapter_guard_enabled` 改成恒真 ⇒ "GUARD=0 ⇒ 不掩码" 那一半必须红。"""
    import trinity.adapters._pii_guard as G
    monkeypatch.setattr(G, "adapter_guard_state", lambda: (True, ""))
    sub = tmp_path / "t2"
    sub.mkdir()
    assert _guard_switch_combination_semantics(sub, monkeypatch) is False


def test_牙齿_G7_3_守卫永不拒存即报红(tmp_path, monkeypatch):
    import trinity.adapters._pii_guard as G
    monkeypatch.setattr(G, "adapter_pii_guard", _guard_never_refuses)
    sub = tmp_path / "t3"
    sub.mkdir()
    assert _two_channel_high_contract(sub, monkeypatch) is False


def test_牙齿_G7_4_盲区扫描器必须能抓到(tmp_path, monkeypatch):
    """扫描器自证：同样含裸 SQL，**有没有经过守卫**必须被判成不同结果（否则清单是空转）。"""
    clean = "cur.execute('INSERT INTO memories (memory_id) VALUES (?)', (x,))\n"
    guarded = ("from trinity.adapters._pii_guard import adapter_pii_guard\n"
               "c, m, _i = adapter_pii_guard(c, m)\n"
               "cur.execute('INSERT INTO memories (memory_id, content) VALUES (?,?)', (x, c))\n")
    assert _is_blind_spot(clean) is True, "含裸 SQL 且无守卫 ⇒ 必须被判成盲区"
    assert _is_blind_spot(guarded) is False, "过了守卫的写入不得再被判成盲区"
    assert _scan_for_redaction(clean) == []
    assert _scan_for_redaction("from trinity.security.sensitive import redact_identifiers\n"), (
        "脱敏原语扫描器必须能抓到真实的脱敏调用")


def test_牙齿_G7_5_去掉PG的isolate分支即报红():
    """把 PG 源码里的 `isolate` 处理"挖掉"（用改写的字符串喂给同一判定）⇒ 判据必须红。"""
    src = (ROOT / PG_GUARD_SITE).read_text(encoding="utf-8", errors="replace")
    stripped = src.replace('_pii_g.get("isolate")', "").replace('_status = "archived"', "")
    has_pg_gap = any("PG" in g["what"] and "未验证" in (g["why"] or "") for g in RUNTIME_GAPS)
    assert has_pg_gap is True
    assert (stripped.count("adapter_pii_guard") >= 2
            and '_status = "archived"' in stripped) is False, (
        "挖掉 isolate/archived 分支后判定竟然仍为真 ⇒ 该判据对 PG 契约没有判别力")


def test_牙齿_G7_6_把披露里的不可逆说成可逆即报红():
    """把披露里的「不可逆」改写成「可」⇒ 披露判据必须红。"""
    weakened = [{"what": "适配器写入边界（G7/t48）", "effect": "直写也会掩码", "reversible": "可"},
                {"what": "scripts/ 下 35 个批处理脚本", "effect": "默认被掩码", "reversible": "可"}]
    joined = " ".join(c["what"] + c["effect"] + c["reversible"] for c in weakened)
    assert (("scripts/" in joined) and ("不可逆" in joined) and ("ADAPTER_GUARD" in joined)) is False, (
        "披露被改成「可逆」后判定竟然仍为真 ⇒ 该判据没有判别力")


# ══════════════════════════════════════════════════════════════════════════
# t52/G11：两张表的**双向**核对 —— 写明触发条件 + **实测"拆掉守卫 ⇒ 必红"**
# （防"永不变红的恒真判据"；本轮 N2/fake_green 抓的就是这个形态）
# ══════════════════════════════════════════════════════════════════════════
def test_G11_两张表已含verifier独立枚举的2条():
    """验收 ①：`path` / `kind` / `reason` 三件齐备（`kind` 是"哪一类"的机器可核对表示）。"""
    guarded = {i["path"]: i for i in GUARDED_BARE_SQL_PATHS}
    assert "trinity/engine_worker.py" in guarded, "engine_worker（t51 已接守卫）必须登记"
    ew = guarded["trinity/engine_worker.py"]
    assert ew["kind"] == "bare_sql_insert" and ew["reason"].strip(), ew
    assert "_session_dispose_summary" in ew["reason"], "reason 必须点名具体函数（可复核性）"
    mirrors = [i for i in UNCOVERED_PATHS if i.get("kind") == "mirror_backfill"
               and "postgresql.py" in i["path"]]
    assert mirrors, "postgresql.py::migrate_from_sqlite 必须按 kind=mirror_backfill 登记"
    pg = mirrors[0]
    assert (pg.get("scope") or {}).get("function") == "migrate_from_sqlite", (
        "镜像条目必须是**函数级**（整文件有 4 处守卫，按整文件核对会误判）")
    assert "1565" in pg["reason"] and pg["reason"].strip(), pg
    # 门槛（只升不降）
    assert len(UNCOVERED_PATHS) + len(GUARDED_BARE_SQL_PATHS) >= 12
    assert len(GUARDED_BARE_SQL_PATHS) >= 7


def test_G11_方向A_未覆盖路径一旦开始脱敏必须红(monkeypatch):
    """**触发条件（方向 A）实测**：把某条"未覆盖"路径伪装成**已接守卫** ⇒ 判据必须红。"""
    real = _read_source
    target = ROOT / "trinity/evolution/core.py"

    def faked(path):
        src = real(path)
        if Path(path) == target:
            src = "from trinity.adapters._pii_guard import adapter_pii_guard\n" + src
        return src

    assert f5c_uncovered_paths_have_no_redaction() is True          # 干净树：绿
    monkeypatch.setattr(sys.modules[__name__], "_read_source", faked)
    assert f5c_uncovered_paths_have_no_redaction() is False, (
        "清单里的路径开始脱敏后判据竟然没红 ⇒ 方向 A 无效（清单可以悄悄过期）")
    monkeypatch.undo()
    assert f5c_uncovered_paths_have_no_redaction() is True


def test_G11_方向B_拆掉已守卫路径的守卫必须红(monkeypatch):
    """**触发条件（方向 B）实测**：人为拆掉 engine_worker 的守卫 ⇒ 判据**必须红**。

    这正是 t52 要回答的问题："那条'自动变红'判据到底会不会被触发"。
    答案是：**在 t52 之前不会**（engine_worker 从未进表）；现在会 —— 本条就是实测。
    """
    real = _read_source
    target = ROOT / "trinity/engine_worker.py"

    def stripped(path):
        src = real(path)
        if Path(path) == target:
            src = src.replace("adapter_pii_guard(", "_guard_removed(")
        return src

    assert f5c_uncovered_paths_have_no_redaction() is True
    monkeypatch.setattr(sys.modules[__name__], "_read_source", stripped)
    assert f5c_uncovered_paths_have_no_redaction() is False, (
        "把 engine_worker 的守卫拆掉后判据竟然没红 ⇒ 方向 B 恒真/无效（必须修）")
    monkeypatch.undo()
    assert f5c_uncovered_paths_have_no_redaction() is True


def test_G11_方向B_守卫晚于裸SQL也必须红(monkeypatch):
    """**顺序不变量**的牙齿：把守卫"挪到"`INSERT INTO memories` 之后 ⇒ 判据必须红。"""
    real = _read_source
    target = ROOT / "trinity/engine_worker.py"

    def reordered(path):
        src = real(path)
        if Path(path) == target:
            lines = src.replace("\r\n", "\n").split("\n")
            gi = next((i for i, ln in enumerate(lines)
                       if "adapter_pii_guard" in ln and "import" not in ln), None)
            if gi is not None:
                moved = lines.pop(gi)
                ii = next((i for i, ln in enumerate(lines) if "INSERT INTO memories" in ln),
                          len(lines) - 1)
                lines.insert(ii + 1, moved)      # 守卫挪到 INSERT 之后
                src = "\n".join(lines)
        return src

    monkeypatch.setattr(sys.modules[__name__], "_read_source", reordered)
    assert f5c_uncovered_paths_have_no_redaction() is False, (
        "守卫被挪到裸 SQL 之后判据竟然没红 ⇒ 顺序不变量没有判别力")
    monkeypatch.undo()
    assert f5c_uncovered_paths_have_no_redaction() is True


# ══════════════════════════════════════════════════════════════════════════
# t54/G13：① 全仓普查闭环（权威数 == 普查结果）② skip 预算 = 0
# ══════════════════════════════════════════════════════════════════════════
def test_G13_全仓普查与登记表必须相等_闭环():
    """**权威数来自独立普查**：`census_sql_insert_sites() == registered_sql_insert_sites()`。

    这不是 `>= 常数`：**未登记的实存写入点**会让左边变大、右边不变 ⇒ **必然红**
    （而不是"靠人想起来补登记"）。四类差集全空才算闭环，红时**打印差集**。
    """
    census = census_sql_insert_sites()
    reg = registered_sql_insert_sites()
    diff = census_closure_diff()
    detail = ("普查=%d 登记=%d | 未登记=%s | 登记了但普查里没有=%s | 方向不符=%s | 同函数多处=%s"
              % (len(census), len(reg), diff["unregistered"], diff["registered_but_absent"],
                 diff["direction_violations"], diff["multi_site_functions"]))
    assert f13_全仓普查与登记表闭环(), detail
    # 常数**只当保险**（门槛未降低）：权威数是上面的 `==`
    assert len(census) >= 12, detail
    assert len(GUARDED_BARE_SQL_PATHS) >= 7, detail
    # 普查宇宙与"已守卫/镜像/守卫边界/DDL"四类的覆盖率（可读性）
    kinds = sorted(reg.values())
    assert kinds.count("bare_sql_insert") >= 7 and kinds.count("mirror_backfill") >= 1, detail


def test_G13_牙齿_新增未登记裸SQL写入点必须红(monkeypatch):
    """**验收要点②**：人为新增一条**未登记**的裸 SQL 写入点 ⇒ 判据**必须红**（真跑一次）。

    做法（不改生产文件）：让 `_read_source` 在某个**未登记**的文件里多出一段含
    `INSERT INTO memories (` 的字符串字面量 ⇒ 普查集合比登记集合多一项 ⇒ 差集非空 ⇒ 红。
    """
    real = _read_source
    target = ROOT / "trinity/evolution/core.py"          # 未登记任何 INSERT 的文件

    def with_unregistered_insert(path):
        src = real(path)
        if Path(path) == target:
            src += ('\n_SYNTHETIC = (\n    "INSERT INTO memories (memory_id, content) "\n'
                    '    "VALUES (%s, %s)"\n)\n')
        return src

    assert f13_全仓普查与登记表闭环() is True            # 干净树：绿
    monkeypatch.setattr(sys.modules[__name__], "_read_source", with_unregistered_insert)
    diff = census_closure_diff()
    assert diff["unregistered"], "合成写入点没被普查抓到 ⇒ 普查本身失灵"
    assert f13_全仓普查与登记表闭环() is False, (
        "新增未登记裸 SQL 写入点后判据竟然没红 ⇒ 闭环不成立（又回到假闭环）")
    monkeypatch.undo()
    assert f13_全仓普查与登记表闭环() is True


def test_G13_牙齿_同函数第二处INSERT也必须红(monkeypatch):
    """闭环的第二半：**同一文件同一函数里再加一处 INSERT** ⇒ 也必须红（否则会静默漏统计）。

    目标选 `_pg_schema.py`：它的写入点在 **模块级**（`<module>`）⇒ 追加一个模块级字面量
    就是"同一个 key 的第二处"，注入**不会破坏语法**（在函数体中间插行可能截断多行 SQL 字面量）。
    """
    real = _read_source
    target = ROOT / "trinity/adapters/_pg_schema.py"

    def double_insert(path):
        src = real(path)
        if Path(path) == target:
            src += '\n_EXTRA_SQL = "INSERT INTO memories (memory_id) VALUES (%s)"\n'
        return src

    monkeypatch.setattr(sys.modules[__name__], "_read_source", double_insert)
    diff = census_closure_diff()
    assert diff["multi_site_functions"], "同文件同函数的两处 INSERT 没被识别 ⇒ 会漏统计"
    assert f13_全仓普查与登记表闭环() is False
    monkeypatch.undo()
    assert f13_全仓普查与登记表闭环() is True


def test_G13_牙齿_登记了但普查里没有也必须红(monkeypatch):
    """反方向：**登记表里多出一条普查找不到的** ⇒ 红（防"登记表自己涨、审计不动"）。"""
    real = _read_source
    target = ROOT / "trinity/cognition/actor.py"

    def without_insert(path):
        src = real(path)
        if Path(path) == target:
            src = src.replace("INSERT INTO memories", "INSERT INTO memories_x")
        return src

    monkeypatch.setattr(sys.modules[__name__], "_read_source", without_insert)
    diff = census_closure_diff()
    assert diff["registered_but_absent"], "登记表里的站点在普查里消失后竟没被识别"
    assert f13_全仓普查与登记表闭环() is False
    monkeypatch.undo()
    assert f13_全仓普查与登记表闭环() is True


def test_G13_跳过必须响_本文件skip预算为0且无逃逸口():
    """**验收要点③**：本文件的 `pytest.skip` 站点数 == `SKIP_SITES_BUDGET`（0）且账本为空。

    仓库里没有 pytest-skip 的现成门禁（`scripts/silent_skip_audit.py` 管的是**脚本内**
    静默 `pass/continue/return None`——我实跑核对过它的输出口径），所以预算就落在本判据内：
    **静态站点扫描 + 运行期账本 + 常量预算** 三者必须一致；任何一处新 `pytest.skip` ⇒ 红。
    """
    src = _read_source(Path(__file__))
    sites = _skip_sites_in(src)
    assert sites == [], (
        "本文件出现了 pytest.skip 逃逸口（行号 %s）⇒ 违反 t54/G13『跳过必须响』；"
        "请改成 _env_gate_fail()（响亮失败）或更新 SKIP_SITES_BUDGET 并在报告里点名" % sites)
    assert _skip_budget_ok(src, SKIP_SITES_BUDGET, SKIP_LEDGER) is True, (
        "skip 预算判定不一致：预算=%s 站点=%s 账本=%s）"
        % (SKIP_SITES_BUDGET, sites, SKIP_LEDGER))


def test_G13_牙齿_出现skip站点即红():
    """牙齿：给一份**含 `pytest.skip(`** 的源码 ⇒ 预算判定必须为 False（非恒真）。"""
    assert _skip_budget_ok("x = 1\n", 0, []) is True
    assert _skip_budget_ok("import pytest\npytest.skip('boom')\n", 0, []) is False
    assert _skip_sites_in("import pytest\npytest.skip('boom')\n") == [2]


def test_G13_牙齿_预算与实际不一致即红():
    """牙齿：预算 1 而站点 0（或反之）⇒ 红（防"预算是摆设"）。"""
    assert _skip_budget_ok("import pytest\npytest.skip('a')\n", 1, []) is True
    assert _skip_budget_ok("", 1, []) is False
    assert _skip_budget_ok("import pytest\npytest.skip('a')\n", 0, []) is False


def test_G13_牙齿_运行期账本非空即红():
    """牙齿：运行期账本非空（发生过降级）⇒ 红。"""
    assert _skip_budget_ok("x = 1\n", 0, ["env-gate-fail: 某原因"]) is False




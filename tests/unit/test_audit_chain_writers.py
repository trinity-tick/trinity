"""S1 反向测试：所有 audit_log 写入方都必须**记录前驱**（prev_checksum）。

## 缺陷（实测 2026-09-18）

适配器 `trinity/adapters/_pg_audit.py` 已于 2026-09-15（R41-P23）修好这一条，
其注释把危害写得很清楚：

> payload 里已经用了 prev_checksum 参与算链式哈希，但 INSERT **从未写这一列**
> ⇒ 事后任何验证者都不知道"当时用的前驱是谁"，只能按 timestamp 排序去推；
> 而该列是 TEXT 且格式混杂（`T..+00:00` 与空格分隔/+08 两种）⇒ 字典序 ≠ 时间序
> ⇒ 推出来的前驱在交界处可能与写入时不同 ⇒ checksum **在构造上就不可复现**，
> 却被 verify_audit_integrity 报成"疑似篡改"。

**但同仓还有多处脚本级写入方没有跟着修**，仍在产生不可复算的审计行。
实测证据（PG）：`prev_checksum` 为空的行 **64,277 / 82,880**，且最新一条是
**2026-09-18T01:18:51Z**（正是本轮 brain_cycle 的 BRAIN_PROPOSAL）。
`/audit/integrity` 已把其中 1 条列为 `legacy_unverifiable`。

## 本测试的性质（诚实标注）

这是**静态判据**（源码级正则），不是端到端验证——因为它要防的是"新写入方忘记
记录前驱"这类**回归**，而端到端只能证明"当前这一条写对了"。本仓已有同类静态闸门
（retrieval_wiring_audit / noop_guard_audit）。运行时证据见上面的 DB 计数。
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 已知的**合法例外**：迁移/一次性脚本按整表搬运历史行，前驱由源数据自带。
ALLOW = {
    # 从 SQLite 整表搬迁，prev_checksum 随源行一起搬（不是"新造一条链"）
    "scripts/migrate_sqlite_to_pg.py",
    # 一次性 schema 迁移：把 legacy 表（该表无 prev_checksum 列）搬进新表，
    # 搬的是历史行的既有列，不是新造链。
    "trinity/adapters/sqlite/_schema.py",
    # 单测夹具：最小 SQLite 表，只用于测别的函数，不产生生产审计行。
    "tests/unit/test_compliance_check.py",
    # ── 2026-10-06（测试归因轮 T1）：以下 4 条是**同一种**单测夹具/负向用例，逐条登记 ──
    # 口径与 test_compliance_check.py 一致：它们建的是`最小 audit_log 表`或写一条
    # 无链语义的种子行，用来测**别的**函数（覆盖面旁证 / 归因取数 / 干跑），
    # 既不产生生产审计行，也不承担"链可复算"的义务 —— 给夹具表加 prev_checksum 列
    # 会改变它们各自要测的东西（例如下面那条负向用例正是**要求**缺列时被拒）。
    # 逐条写明而不是"整个 tests/ 免检"，是因为后者的免检面无法被复核。
    # 负向用例：故意只写 id 去撞真实 schema，**期望** sqlite3.OperationalError（缺列即拒）。
    "tests/unit/test_audit_anchor_dual_chain_20261006.py",
    # 干跑夹具：为"语料回填 dry-run"造一条 create 种子行，只用于计数/判重。
    "tests/unit/test_corpus_backfill_dryrun_20261006.py",
    # 覆盖面旁证夹具：自建 (action,timestamp,details) 最小表，喂固定行测代理读数。
    "tests/unit/test_coverage_proxy_sqlite_20260930.py",
    # 读取方归因夹具：自建 (action,timestamp,details,agent_id) 最小表，喂行测 U1a 取数。
    "tests/unit/test_u1_reader_attributed_sqlite_20260930.py",
}

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "temp", "output"}
# 本文件自身的正则字面量含 "INSERT INTO audit_log"，必须排除（否则自匹配）。
SELF = "tests/unit/test_audit_chain_writers.py"


def _iter_sources():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.relpath(os.path.join(base, f), ROOT).replace("\\", "/")


def _insert_blocks(text: str):
    """产出每个真正的 INSERT INTO audit_log 语句（**AST 级**，只取 execute/executescript 实参）。

    为什么不用正则扫全文：`_audit_chain.py` 的**模块 docstring 里就写了**
    "全仓原有 12 处脚本级 INSERT INTO audit_log" —— 正则会把这句散文当成写入方
    而误报。（这正是本仓反复记录的"判据只看字面会假阳"。）
    AST 里相邻字符串字面量已在编译期折叠成一个 Constant，故能直接拿到完整 SQL。
    """
    import ast
    import warnings

    try:
        with warnings.catch_warnings():
            # 被扫的源码里有历史遗留的非法转义正则字面量，解析时会发 DeprecationWarning；
            # 与本次判据无关，压掉噪音（不改变解析结果）。
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and "insert into audit_log" in arg.value.lower():
                yield arg.value


def _writers_missing_prev():
    bad = []
    for rel in _iter_sources():
        if rel in ALLOW or rel == SELF:
            continue
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for block in _insert_blocks(text):
            # 只看列清单（VALUES 之前）
            head = re.split(r"VALUES", block, flags=re.IGNORECASE)[0]
            if "prev_checksum" not in head:
                bad.append(rel)
                break
    return sorted(set(bad))


def test_every_audit_writer_records_prev_checksum():
    bad = _writers_missing_prev()
    assert bad == [], (
        "以下审计写入方未记录 prev_checksum（会产生构造上不可复算的审计行，"
        "验证器只能靠推断前驱，格式边界处会误报篡改）：\n  " + "\n  ".join(bad))


def test_known_live_writers_are_covered():
    """防止有人靠 ALLOW 或跳过目录把问题藏起来：这几个**日链在跑**的写入方必须被扫到。"""
    scanned = set(_iter_sources())
    for must in ("scripts/brain_cycle.py", "scripts/_audit_chain.py",
                 "trinity/adapters/_pg_audit.py"):
        assert must in scanned, "扫描漏掉了 %s" % must
        assert must not in ALLOW, "%s 不应被列入例外" % must

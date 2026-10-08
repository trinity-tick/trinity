"""U1a「存储纪元」判据闸门（2026-09-30）。

## 背景：为什么 U1a 的红不是交付退化，而是**口径不适用**

`U1-a` 的谓词是（`scripts/memory_utilization_audit.py::_u1_split`）：

```sql
select count(*) from memories
where last_retrieved_at > now() - interval '24 hours' and agent_id like 'dsh-%'
```

实测四步取证：

1. 读标记列 **`memories.last_retrieved_at` 是 PG 独有** ——
   SQLite 侧 `no such column: last_retrieved_at`；
2. 写它的是 **`trinity/adapters/_pg_touch.py`**（PostgreSQL 适配器的 touch 路径）；
3. 实测 PG：该列**非空仅 7,554 / 71,750（10.5%）**，最新值停在 `04:51`；
4. 同期活动存储（SQLite）的 `audit_log` 有 `action='search_hybrid'` +
   `details.memory_ids` **178 行/小时**（PG 侧 0 行）。

⇒ 本部署（`TRINITY_STORAGE_BACKEND` 未设 ⇒ SQLite 活动）下，**API 的检索不会去 touch
PG 的读标记** ⇒ 该列不再是"API 读取"的标记 ⇒ `U1-a` 的当日值与其历史样本
**不是同一口径的产物，不可比**。

## 处置（复用仓内既有约定，不是新造绕法）

与 `U2` 的「**账本纪元**」同款（本文件所在的脚本 §929 已写明
「棘轮遇到纪元变化就**不判该项**，并显式点名，**不静默通过**」）：
活动存储不是 PG 时，`_u1a_window_judge` 返回 `(None, 原因)` ⇒ 棘轮打印
`SKIP(窗口)`，而**原始读数仍照旧报出**（`· U1a_agent_reads_24h 本次=3（基线 92）`）。

**这就是本闸门要钉住的三件事**：
① 非 PG 活动存储时必须**不判**（不是判绿，也不是判红）；
② 理由必须**点名机制**（读标记列只由 PG 适配器维护）；
③ 原始读数**不得被隐藏**（口径变化可见）。

运行：``python -m pytest tests/unit/test_u1a_store_epoch_20260930.py -q``
"""

from __future__ import annotations

import io
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "scripts/memory_utilization_audit.py"


def _mod():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("mua_epoch", AUDIT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_non_pg_active_store_skips_with_named_reason(monkeypatch) -> None:
    """**核心**：活动存储不是 PG ⇒ 必须 `None`（不判），且理由要点名机制。"""
    m = _mod()
    import trinity.security.credentials as C

    monkeypatch.setattr(C, "resolve_backend", lambda: "sqlite", raising=False)
    ok, why = m._u1a_window_judge(3)
    assert ok is None, (
        f"非 PG 活动存储竟然给出了判决（{ok!r}）—— 该读数在其数据源上不可比，"
        "必须'不判并显式点名'（同 U2 的账本纪元处置）"
    )
    assert "口径不适用" in why and "last_retrieved_at" in why, f"理由没点名机制：{why[:120]}"
    assert "存储" in why, "必须点明这是'存储纪元'问题"


def test_pg_active_store_still_judges(monkeypatch) -> None:
    """反事实：活动存储**是 PG** 时不得借故跳过（否则就是把门废掉）。

    2026-10-06（测试归因轮 T1）：原判据只把 `resolve_backend` 打成 postgresql。
    该口径**已被 2026-10-02 的「反向纪元」修复明确取代**（源码 `_u1a_window_judge`
    的注释与 `_sqlite_side_is_active` 的 docstring 都记了实测证据：凭据修好后
    `resolve_backend()` 返回 `postgresql`，而 API 的读写其实落在 SQLite ⇒ PG 的读标记列
    `last_retrieved_at` 不被维护、自然衰减 ⇒ 拿 PG 纪元的历史中位数去比会读出**假退化**
    `3 vs 93`）。故现在的判定条件是**两个**：

        backend 是 PG  **且**  写入侧不是 SQLite

    原判据只满足前一个 ⇒ 实际走的是「口径不适用」那条分支 ⇒ **假红**：
    它没有测到"PG 活动时仍会判"，只测到了"配置名不等于事实"这条**已经修好**的教训。

    现按**现在的两个条件**构造真的「PG 活动」场景。断言保留原意：
    **不许借故跳过**（连不上库时返回"采样不可用"同样算没有借故跳过）。
    """
    m = _mod()
    import trinity.security.credentials as C

    monkeypatch.setattr(C, "resolve_backend", lambda: "postgresql", raising=False)
    monkeypatch.setattr(m, "_sqlite_side_is_active", lambda *a, **k: False, raising=False)
    # PG 分支会去连库取参照；连不上时返回 (None, "采样不可用") 也算"没有借故跳过"，
    # 故这里只断言**不是**那条口径不适用理由。
    ok, why = m._u1a_window_judge(3)
    assert "口径不适用" not in why, f"PG 活动时不该走口径不适用：{why[:160]}"


def test_raw_reading_is_not_hidden(monkeypatch) -> None:
    """**口径变化必须可见**：跳过的是**判决**，不是**读数**。"""
    src = io.open(AUDIT, encoding="utf-8").read()
    # 原始 U1a 仍必须进入报告主体（本次=… 与基线并列）。
    # 注：窗口要够大 —— 该函数的 docstring 很长（含口径取证），键名落在 2000 字符之后
    # （本判据第一版就是窗口开小了而误报）。
    assert "U1a_agent_reads_24h" in src
    i = src.index("def _u1_split")
    assert "U1a_agent_reads_24h" in src[i: i + 8000], "原始读数不得从报告里消失"


def test_store_epoch_is_documented_in_source() -> None:
    """留痕：**实现该判定的那段代码**必须写明这是「存储纪元」、并引用实测依据。

    2026-10-06（测试归因轮 T1）：原实现取**全文第一处**「存储纪元」前后
    `src[i-1200 : i+900]` 的**字符窗口**。窗口锚点是"某个词第一次出现"，
    而上游同文件 2026-10-02 新增了 `_sqlite_side_is_active`（它也用到「存储纪元」），
    插入的正文把 `_pg_touch` 的引用**挤出了窗口边界**（差几十字符）⇒ 判据假红。
    判据的本意不是"某两个词在 2100 字符内共现"，而是"**做这个判定的代码**把机制与
    不可比的理由写在手边"。故锚点改为**实现该判定的函数体**（`def _u1a_window_judge`
    起 6000 字符），这才是它真正要锁的东西，也不会再被上游无关插入误伤。
    """
    src = io.open(AUDIT, encoding="utf-8").read()
    i = src.index("def _u1a_window_judge")
    body = src[i: i + 6000]
    assert "存储纪元" in body, "实现处没写明这是「存储纪元」"
    assert "_pg_touch" in body, "必须指出写该列的是 PG 适配器 touch 路径"
    assert "不可比" in body, "必须说明为何不可比"

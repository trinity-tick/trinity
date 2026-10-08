# -*- coding: utf-8 -*-
"""`session_context` 直写路径的**登记与判据**（t53 / G12，2026-10-06）

## 为什么有这个文件

verifier 在 G5 终稿里用**仓库自己的**`scripts/direct_pg_writers_audit.py` 普查发现：
🔴 `trinity/brain/situation_stream.py:258` 有**裸 `INSERT INTO session_context`**、
自带 `psycopg2.connect`、全文无守卫、**不在任何登记清单里**。

任务书要求：**先测后修**。测量结论（见 `REDACT-SESSION-CONTEXT.md` 与
`evidence/t53_session_context_pii.json`）：

- **校准组通过**（先证仪器方向，再看数据）：6 个已掩码串**不被判命中且不被改写**；
  4 个**校验通过**的未掩码样本（手机/邮箱/身份证号/银行卡号）**全部命中且被改写**。
  ⚠️ 首版校准用的是"常见测试数据"（`example.org` / `11010119900307123X` /
  `6222021234567890123`），**三个都没命中** —— 查因：守卫**带校验**
  （占位域名排除 / `_id18_ok` 校验位 / `_luhn_ok`）。**首版校准是不确定的**，
  所以那一轮的"数据也没命中"**不能作为结论**。
- **真实数据**：`session_context` 共 **11 行**（含 situation_stream 自己写的 `ctx:brain`），
  `last_query` 与 `percepts` 各组件的 `scan_pii` 命中数 **0**、`redact_identifiers` 改写 **0**。
  `ctx:brain` 长 300 字符 / 4 段 + 5 个组件，**无**手机/身份证/卡号形态的串
  （唯一的长数字串是 2 个 9 位数字，非任何证件形态）。
  ⇒ **未发现未掩码 PII** ⇒ 按任务书规则：**只登记，不改代码**。

## 本台账与 G4 那张表的**口径不同**（必须写明）

| 项 | G4（`test_redact_scope_20261006.py`） | **本台账（t53/G12）** |
|---|---|---|
| 宇宙 | **`INSERT INTO memories`**（两张表：准入面/镜像回填面） | **`INSERT INTO session_context`** |
| 被写列 | `content` / `metadata` 等记忆正文 | **`last_query`**（列名叫 query，值实际是**情境摘要**）+ `percepts` |
| 值的来源 | 用户/导入的记忆正文 | `_compose(_gather())`：计数 + 自我摘要前 60 字 + 感知信号 44 字 + 屏幕情境标题 30 字 + 好奇焦点 20 字，**整体截断 300 字** |
| 现态 | 已由 t50/G9 关闭 6 条裸 SQL | **裸 INSERT 保留**（因为实测不含 PII），**登记于此** |

⚠️ **本台账不得声明覆盖 `INSERT INTO memories`**（那是 G4 的宇宙）；反之亦然。
两者若被合并成一张表，会把"两个不同的写入面"混成一个口径 —— 本文件有一条判据钉这一点。

## 一条必须一起读的边界

`scan_pii` **不覆盖** `_PII_NOT_ENABLED = ("IP", "住址", "姓名", "车牌", "驾照", "QQ/微信")`
⇒ 本结论的准确表述是"**在可扫描类别内未发现**"。而该写入路径的组成里**有屏幕情境标题与
感知信号**（`percepts` 的组件），原则上可以携带**姓名/住址**类文本 —— 这类**本仪器判不了**。
故本登记同时记录这条**残余风险**，而不是把"未发现"写成"绝对没有"。
"""
from __future__ import annotations

import ast
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 被登记的**未守卫**直写路径（经实测无 PII ⇒ 保留裸 INSERT，但**显式登记**）。
SESSION_CONTEXT_WRITE_PATHS = [
    {
        "file": "trinity/brain/situation_stream.py",
        "func": "refresh",
        "line_hint": 258,
        "sql": "INSERT INTO session_context",
        "columns_written": ("last_query", "percepts"),
        "value_source": ("`summary = _compose(_gather())`：**不是**原始用户查询，"
                         "而是 ≤300 字符的情境摘要（列名 `last_query` 是历史命名）"),
        "universe": "session_context",
        "guard_state": "unguarded",
        "measured": {
            "at": "+08:00 2026-10-06T21:05:15",
            "rows_total": 11,
            "last_query_flagged": 0,
            "redact_changed": 0,
            "percepts_components_flagged": 0,
            "calibration": "masked_negatives 6/6 clean; unmasked_valid_positives 4/4 flagged",
            "evidence": "evidence/t53_session_context_pii.json",
        },
        "verdict": "经实测不含**可扫描** PII ⇒ 只登记，不改代码",
        "residual_risk": ("scan_pii 不覆盖 姓名/住址/IP/车牌/驾照/QQ微信；"
                          "本路径的 percepts 组件含屏幕情境标题与感知信号，"
                          "原则上可携带上述类别 ⇒ 该类别需人工/更强仪器另判"),
    },
]

#: **方向 A（G4 的既定规则）**：登记为"未守卫"的路径**一旦开始调守卫** ⇒ 判据红 ⇒
#: 必须把它移出本台账（否则台账会同时声称"未守卫"与"已守卫"）。
_GUARD_CALLS = ("adapter_pii_guard", "redact_identifiers", "scan_pii", "scan_sensitive")


def _read(path: str) -> str:
    with io.open(os.path.join(ROOT, path), encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _func_source(path: str, func: str) -> str:
    src = _read(path)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return ast.get_source_segment(src, node) or ""
    return ""


def stale_registrations(paths=None, source_of=None) -> list:
    """返回**登记已过期**的条目：

    ① 目标函数不见了 / 裸 SQL 不见了 ⇒ 登记描述的东西已不存在；
    ② 函数里**出现了守卫调用** ⇒ 它不再是"未守卫"，必须移出本台账（方向 A）。

    `source_of` 可注入（返回该条目的函数源码）⇒ 负向实测能喂一个"已接守卫"的合成源，
    证明这条判据**真的会红**，而不是恒真。
    """
    src_fn = source_of or (lambda p: _func_source(p["file"], p["func"]))
    out = []
    for p in (paths if paths is not None else SESSION_CONTEXT_WRITE_PATHS):
        seg = src_fn(p)
        if not seg:
            out.append({"path": p["file"], "why": "函数不存在"})
            continue
        if p["sql"] not in seg:
            out.append({"path": p["file"], "why": "裸 SQL 不在该函数里（登记描述失效）"})
            continue
        if any(g in seg for g in _GUARD_CALLS):
            out.append({"path": p["file"], "why": "该路径已接守卫 ⇒ 应移出未守卫台账"})
    return out


def universes_are_separate(entries=None) -> bool:
    """本台账只允许 `session_context` 宇宙；`memories` 宇宙属 G4，两表不得互相代声明。"""
    for p in (entries if entries is not None else SESSION_CONTEXT_WRITE_PATHS):
        if "memories" in str(p.get("universe", "")):
            return False
    return True


# ── 判据 ──────────────────────────────────────────────────────────────────

def test_登记路径仍然存在且仍是未守卫的裸INSERT() -> None:
    """登记必须与代码现实一致：函数在、裸 SQL 在、**没有**守卫调用。

    若有人给这条路径加上守卫（那是允许的、甚至是好的），本用例会红 ——
    红不是"错"，而是**要求把登记更新**（移出未守卫台账）。这就是 G4 的"方向 A"。
    """
    stale = stale_registrations()
    assert stale == [], (
        "session_context 直写台账与现实不一致：%s\n"
        "⇒ 若该路径已接守卫，请把它从 SESSION_CONTEXT_WRITE_PATHS 移除并在报告里登记为已守卫。"
        % stale)


def test_负向实测_该路径一旦接上守卫登记必须作废() -> None:
    """承重证明：伪造一个"已接守卫"的同款函数 ⇒ `stale_registrations` 必须报出来。

    三个方向都测：① 含守卫 ⇒ 红；② 去掉裸 SQL ⇒ 红；③ 空源（函数不存在）⇒ 红。
    这样"台账与现实一致"这条断言不是恒真的。
    """
    entry = [{
        "file": "trinity/brain/situation_stream.py",
        "func": "refresh",
        "sql": "INSERT INTO session_context",
        "universe": "session_context",
    }]
    guarded = ("def refresh():\n"
               "    from trinity.adapters._pii_guard import adapter_pii_guard\n"
               "    summary = adapter_pii_guard(summary)\n"
               "    cur.execute('INSERT INTO session_context (id, last_query) VALUES (%s, %s)')\n")
    clean = ("def refresh():\n"
             "    cur.execute('INSERT INTO session_context (id, last_query) VALUES (%s, %s)')\n")
    no_sql = "def refresh():\n    return None\n"

    assert stale_registrations(entry, source_of=lambda p: guarded), "含守卫却没判红 ⇒ 判据恒真"
    assert stale_registrations(entry, source_of=lambda p: no_sql), "裸 SQL 消失却没判红"
    assert stale_registrations(entry, source_of=lambda p: ""), "函数不存在却没判红"
    assert stale_registrations(entry, source_of=lambda p: clean) == [], "干净源被误判"


def test_口径自检_已掩码串不得被判命中_未掩码有效样本必须命中() -> None:
    """**把 t46 的校准方法钉进 CI**（本轮 t53 靠它才发现首版校准是不确定的）。

    若守卫行为变化（例如不再校验 Luhn/校验位、或开始命中掩码串），本用例立刻红
    ⇒ "未发现 PII" 这个结论所依赖的仪器方向**始终可复核**。
    """
    import sys
    sys.path.insert(0, ROOT)
    from trinity.security.sensitive import redact_identifiers, scan_pii

    masked = ["联系方式 138****1234", "邮箱 z***@e***.com",
              "身份证 1101**********1234", "卡号 6222 **** **** 1234"]
    for t in masked:
        assert scan_pii(t)["flagged"] is False, "已掩码串被判命中 ⇒ 方向反了：%s" % t
        rr = redact_identifiers(t)
        rr = rr[0] if isinstance(rr, tuple) else rr
        assert str(rr) == t, "已掩码串被再次改写：%s → %s" % (t, rr)

    # 未掩码**且校验通过**的样本（t53 实测：用无效测试数据会得到假阴性）
    def _valid_id18() -> str:
        base = "11010119900307123"
        w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
        return base + "10X98765432"[sum(int(d) * wi for d, wi in zip(base, w)) % 11]

    positives = ["联系方式 13812345678", "邮箱 zhang.san@qq.com",
                 "身份证 " + _valid_id18(), "卡号 6222021234567890128"]
    for t in positives:
        assert scan_pii(t)["flagged"] is True, (
            "校验通过的未掩码样本没被命中 ⇒ 仪器不响（本轮曾因此得到不确定的校准）：%s" % t)


def test_两个宇宙不得互相代声明() -> None:
    """本台账只声明 `session_context`；`INSERT INTO memories` 属 G4 的表。

    钉住这一点，是因为两张表**口径不同**（不同表、不同列、不同值来源）；
    谁把它们合并成一张"直写路径表"，就会掩盖这个差别。
    """
    assert universes_are_separate() is True
    assert universes_are_separate([{"universe": "memories"}]) is False
    assert SESSION_CONTEXT_WRITE_PATHS[0]["universe"] == "session_context"


def test_残余风险已写入登记_不得只写未发现() -> None:
    """`scan_pii` 的覆盖边界必须与"未发现"一起出现（防把"未发现"读成"绝对没有"）。"""
    e = SESSION_CONTEXT_WRITE_PATHS[0]
    assert "姓名" in e["residual_risk"] and "住址" in e["residual_risk"], e["residual_risk"]
    assert "可扫描" in e["verdict"], e["verdict"]

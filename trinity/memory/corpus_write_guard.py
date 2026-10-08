# -*- coding: utf-8 -*-
"""写入侧准入拦截（该不该写进去）—— 2026-10-06 t4 语料治理。

## 与既有邻居的分工（**不是同一个关注点，也不在同一个调用点**）

| 模块 | 关注点 | 调用点 | 默认 |
|---|---|---|---|
| `trinity/memory/write_verify.py` | 写**进去查不查得到**（Mano-P think-act-verify：写后回查 top-k，未命中可补救） | **store 之后**（`maybe_verify_after_store`，读回查） | off |
| 本模块 | **该不该写进去**（重复/自复制/派生回灌的准入判定） | **store 之前**（`evaluate_before_store`，纯函数，不碰库） | annotate |

两者的钩子**不重合**：一个在写入完成之后验证可检索性，一个在写入发生之前决定是否放行。
本模块**不含**任何 searcher / fixer 逻辑，也不写库（判定所需的两项外部事实 ——
"该 hash 在库里的 status 集合"与"同内容的近期写入次数" —— 由调用方查好后**传进来**，
这样判据是纯函数、可反事实测试）。

## 沿用 `write_verify.py` 的约定（同仓一致性）

* 开关命名 `TRINITY_*`；取值语义三档（见 `guard_mode`）；
* state 日志是 JSONL（`~/.trinity/state/corpus_write_guard.jsonl`，可用
  `TRINITY_CORPUS_WRITE_GUARD_LOG` 覆盖），一行一次判定，可直接统计"会拦什么"；
* **失败降级**：判定内部任何异常一律 **允许写入** 并记 `guard_error`，绝不因判据故障阻断写入；
* 默认档**不改变行为**（`annotate` ⇒ 永远 allow，只留证据）。

## 三条判据（每条都有反事实方向）

* **W1 `dup_archived_copy`** —— 该 `content_hash` 在库里**只**以非 active 状态存在。
  这是本仓语料膨胀的第一大机制：去重契约（唯一索引
  `idx_memories_content_hash ... AND status='active'`）在条目被归档后**失效**，
  生产者下一轮重跑就插一条新的 active 副本。实测指纹：18,807 个重复组里 **5,787 组**
  是"≥1 active + ≥1 非 active"，34,897 行重复落在**全归档**组里。
* **W2 `self_ref_loop_depth`** —— 内容里嵌套的**生成式头**数量 ≥ `MAX_GEN_DEPTH`。
  实测：`mem_f2b4dcba3ff34e95` 嵌套 12 个 `[自动关联]`，正文里已不含任何事实，
  只剩元数据头（记忆被检索→生成关联记录→关联记录又成为记忆→再被检索的正反馈）。
* **W3 `derivative_requota`** —— 内容带生成式头，且**同一生产者 + 同一归一化内容**
  在窗口内已被写过 ≥ `DERIVATIVE_QUOTA` 次。实测：`category='self-reflection'`
  共 546 行但只有 62 个不同内容，**单条内容被写 166 次**（2026-08-30→10-05，≈3 次/天）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("trinity.memory.corpus_write_guard")

#: 三档开关
ENV_SWITCH = "TRINITY_CORPUS_WRITE_GUARD"
DEFAULT_LOG = os.path.join("~", ".trinity", "state", "corpus_write_guard.jsonl")

#: 生成式头的标记（与本仓实测的"自产写入"一一对应；只用于**判定派生性**，不用于过滤内容）
GEN_MARKERS: Tuple[str, ...] = (
    "[自动关联]",
    "[self-reflection]",
    "[会话自动摘要]",
    "[COMPRESSED SUMMARY",
    "[AUTO-COMPRESSED]",
    "[kb-section:",
    "[kb-table-row:",
    "[procedure]",
    "[experience]",
)

#: W2 阈值：嵌套的生成式头达到此数量即判为"派生的派生"
MAX_GEN_DEPTH = 3
#: W3 阈值：同一生产者 + 同内容在窗口内的允许写入次数
DERIVATIVE_QUOTA = 3

_WS = re.compile(r"\s+")


# ────────────────────────────────────────────────────────── 开关与日志

def _env_flag(name: str, default: str = "off") -> bool:
    return os.environ.get(name, default).lower() in ("on", "1", "true", "yes")


def guard_mode(env: Optional[Dict[str, str]] = None) -> str:
    """开关三档（与 `scripts/memory_write_policy.py::admission_mode` 同语义）。

    * ``off``      —— 不判（回滚档：行为与"没有本模块"逐字节等价）
    * ``annotate`` —— **默认**：永远 allow，只把"本来会拦什么"记进 JSONL（可计数、零行为变化）
    * ``on``       —— 真的拦

    未识别的取值一律按 ``annotate``（fail-safe：不因错拼而开始拦截）。
    """
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_SWITCH, "") or "").strip().lower()
    if raw in ("0", "off", "false", "no", "none", "disable", "disabled"):
        return "off"
    if raw in ("1", "on", "true", "yes", "drop", "enforce"):
        return "on"
    return "annotate"


def is_enabled() -> bool:
    """是否至少处于 annotate 档（off 档下连日志都不写，保证"零行为变化"含零副作用）。"""
    return guard_mode() != "off"


def log_path() -> str:
    return os.path.expanduser(os.environ.get("TRINITY_CORPUS_WRITE_GUARD_LOG", DEFAULT_LOG))


# ────────────────────────────────────────────────────────── 判定

@dataclass(frozen=True)
class WriteGuardDecision:
    """一次准入判定。`allow=False` 时 `code` 必非空（机器可读、可计数）。"""

    allow: bool
    code: str = ""
    detail: str = ""
    mode: str = "annotate"
    producer: str = ""
    content_hash: str = ""
    would_block: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "allow": self.allow,
            "code": self.code,
            "would_block": self.would_block,
            "mode": self.mode,
            "producer": self.producer,
            "content_hash": (self.content_hash or "")[:16],
            "detail": self.detail[:240],
        }


def norm_text(text: Optional[str]) -> str:
    """归一化：NFKC + 折叠空白 + 去首尾。与 `_merge_safety._norm` 同口径（但不降大小写：
    记忆正文里的大小写常带信息）。"""
    if not text:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKC", str(text))).strip()


def gen_depth(content: str) -> int:
    """内容里生成式头的**出现次数**（= 派生层数）。"""
    return sum(str(content).count(m) for m in GEN_MARKERS)


def is_derivative(content: str) -> bool:
    """内容是否带生成式头（即由本系统的生成/派生回路产出，而非外部输入）。"""
    return gen_depth(content) > 0


def evaluate_before_store(
    content: str,
    *,
    producer: str = "",
    content_hash: Optional[str] = None,
    existing_statuses: Sequence[str] = (),
    recent_identical_writes: int = 0,
    max_gen_depth: int = MAX_GEN_DEPTH,
    quota: int = DERIVATIVE_QUOTA,
    mode: Optional[str] = None,
    log: bool = True,
) -> WriteGuardDecision:
    """写入前的准入判定（**纯函数**；库查询由调用方完成并作为参数传入）。

    参数：
        content                  —— 拟写入正文
        producer                 —— agent_id / 生产者名（用于配额分组与取证）
        content_hash             —— 正文指纹（与库内 `content_hash` 同算法）
        existing_statuses        —— 库内**同 content_hash** 的全部行的 status（查不到 ⇒ 空序列）
        recent_identical_writes  —— 窗口内"同生产者 + 同归一化内容"已写入的次数
        mode                     —— 覆盖开关档（测试用；None ⇒ 读环境变量）

    返回 `WriteGuardDecision`。调用方在 `mode == "on"` 且 `allow is False` 时
    **必须零变更地跳过写入**（不要落库、不要改既有行）；`annotate` 档下照常写入。
    """
    m = mode or guard_mode()
    ch = str(content_hash or "")
    try:
        if m == "off":
            d = WriteGuardDecision(True, "guard_off", "开关 off ⇒ 不判", m, producer, ch)
        else:
            d = _judge(content, producer, ch, existing_statuses, recent_identical_writes,
                       max_gen_depth, quota, m)
    except Exception as e:  # noqa: BLE001 —— 判据故障绝不阻断写入（fail-open，但要留痕）
        logger.warning("corpus_write_guard evaluation failed (fail-open): %s", e)
        d = WriteGuardDecision(True, "guard_error", "判据异常⇒放行: %s" % e, m, producer, ch)
    if log and m != "off":
        append_guard_log(d)
    return d


def _judge(content: str, producer: str, content_hash: str,
           existing_statuses: Sequence[str], recent_identical_writes: int,
           max_gen_depth: int, quota: int, mode: str) -> WriteGuardDecision:
    statuses = [str(s).lower() for s in (existing_statuses or [])]

    # ── W1：该 hash 只以"非 active"形态存在 ⇒ 这次写入会再造一条重复 active 副本 ──
    if statuses and not any(s == "active" for s in statuses):
        code = "dup_archived_copy"
        detail = ("同 content_hash 在库内已有 %d 行，状态 = %s（**无 active**）⇒ "
                  "归档把去重契约洗白了，本次写入会再造一条 active 重复副本"
                  % (len(statuses), sorted(set(statuses))))
        return _verdict(mode, code, detail, producer, content_hash)

    # ── W2：派生的派生（正文只剩元数据头） ──
    depth = gen_depth(content)
    if depth >= max_gen_depth:
        code = "self_ref_loop_depth"
        detail = "生成式头出现 %d 次（阈值 %d）⇒ 派生层数过深，正文已无新证据" % (depth, max_gen_depth)
        return _verdict(mode, code, detail, producer, content_hash)

    # ── W3：同生产者把同一段派生内容反复回灌（自复制配额） ──
    if is_derivative(content) and int(recent_identical_writes) >= int(quota):
        code = "derivative_requota"
        detail = ("生产者 %r 在窗口内已写入同一归一化内容 %d 次（配额 %d）⇒ 自复制"
                  % (producer, int(recent_identical_writes), int(quota)))
        return _verdict(mode, code, detail, producer, content_hash)

    return WriteGuardDecision(True, "", "三条判据均未命中", mode, producer, content_hash)


def _verdict(mode: str, code: str, detail: str, producer: str, content_hash: str) -> WriteGuardDecision:
    """把"本应拦"折叠成两档：on ⇒ 真的拦；annotate ⇒ 放行但记 would_block。"""
    if mode == "on":
        return WriteGuardDecision(False, code, detail, mode, producer, content_hash,
                                  would_block=True)
    return WriteGuardDecision(True, code, "[annotate] 本来会拦：" + detail, mode, producer,
                              content_hash, would_block=True)


# ────────────────────────────────────────────────────────── 调用方适配

def summarize_decisions(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """从 JSONL 行统计"会拦什么"（供报告与维护链消费）。"""
    total = len(rows)
    would = sum(1 for r in rows if r.get("would_block"))
    blocked = sum(1 for r in rows if not r.get("allow"))
    by_code: Dict[str, int] = {}
    for r in rows:
        if r.get("would_block"):
            by_code[str(r.get("code") or "?")] = by_code.get(str(r.get("code") or "?"), 0) + 1
    return {"total": total, "would_block": would, "blocked": blocked, "by_code": by_code,
            "would_block_rate": round(would / total, 4) if total else 0.0}


def load_guard_log(path: Optional[str] = None, limit: int = 0) -> list:
    """读取判定日志（缺失文件 ⇒ 空列表，不抛）。"""
    p = os.path.expanduser(path or log_path())
    out: list = []
    if not os.path.isfile(p):
        return out
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if limit and len(out) >= limit:
                    break
    except OSError as e:
        logger.warning("load_guard_log failed: %s", e)
    return out


def append_guard_log(decision: WriteGuardDecision, path: Optional[str] = None) -> bool:
    """追加一条判定到 JSONL。**任何失败返回 False 并静默**（记账绝不阻断写入）。

    捕获的是 `Exception` 而不是 `OSError`：实测 `open()` 会因路径含 NUL 抛
    `ValueError: embedded null character` —— 只吞 OSError 会让判定日志把写入路径带崩，
    正是本仓要避免的"观测反过来打业务"。
    """
    p = os.path.expanduser(path or log_path())
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # noqa: BLE001 —— 记账失败绝不阻断写入
        logger.warning("append_guard_log failed: %s", e)
        return False

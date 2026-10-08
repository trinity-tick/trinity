#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session_pull_ledger.py — **会话侧（pull 腿）· 再获取计费** + 投递/消费棘轮的输入（t88 / B4·A5-04）。

## ⚠️ 口径纪律（本文件存在的第一理由）

| 侧 | 数据源 | 谁的口径 |
|---|---|---|
| **注入侧** | `~/.trinity/state/opening_surface_deliveries.jsonl` | **t58 的 "省 71% token"** 是**这一侧**（配对 501.1 → 143.9 token/会话，均值 −357.19，p=0.0078，8/8 同向） |
| **会话侧** | `~/.trinity/state/retrieval_traces.jsonl`（**本文件**） | 就是本账本要补的那一侧：**agent 主动 pull（检索）腿** |

⛔ **两侧不得相加、不得相减、不得并列成一个数** —— 本账本把注入侧只作为**只读引用字段**
`injection_delivered_n`（单独命名空间）保留，绝不与 `reacquired_tokens` 混算。

## 网络依据（为什么"再获取"要被计费）

arXiv 2608.16370（2026-08-17，*What Does Context Compression Cost an Agent?*）逐字：
  > "compression can increase an agent's interaction cost by **forcing it to reacquire dropped state**
  >  while leaving completion statistically unchanged."
  > "Retrieval calls increase in all six model-regime comparisons and account for almost all added interaction"
  > "GPT-5.5 is the clearest case: completion changes from 80% to 85% (p = 1.0) while **retrieval increases from 21.0 to 63.9 calls** (p = .002)"

## 字段（机器可读，每行一条 JSON）

```
{
  "schema": "trinity.session_pull_ledger/1",
  "side": "session-pull",                     # ⚠️ 只表示**会话侧**
  "caliber": "会话侧(pull 腿)；注入侧另计，禁止相加",
  "anchor": "file"|"now",                     # 窗口锚点：file = 文件里最大的 ts（可复现）
  "window_start_epoch": ..., "window_end_epoch": ..., "window_hours": 24,
  "pull_calls": int,                          # 窗口内 pull 调用次数
  "pull_calls_agent": int, "pull_calls_probe": int,   # 按 agent_id 是否为空分解（口径透明）
  "pull_calls_other": int,
  "distinct_items": int,                      # 被取回的 distinct memory_id
  "repeat_items": int,                        # 出现 ≥2 次的条目数
  "repeat_rate": float,                       # 重复调用占比 = (calls - distinct_calls)/calls
  "reacquired_items": int,                    # ⭐再获取条目（≥2 次取回）
  "reacquired_tokens": int,                   # ⭐再获取的**额外** token 估计 = Σ(次数-1)×tokens_est
  "token_estimator": "ceil(len(content)/4)，min 1 —— **字符估算，非模型分词器**",
  "content_resolved_items": int,              # 能在库里取到正文的条目数（取不到 ⇒ 该条按 0 计并计数）
  "delivered_overlap_items": int,             # 与**注入侧**投递 id 的交集（只报交集）
  "injection_delivered_n": int,               # 只读引用：注入侧窗口内投递**条数**（禁止与本侧求和）
  "injection_delivered_distinct": int,
  "generated_at": "..." 
}
```

## 用法

```
python scripts/session_pull_ledger.py --json                 # 锚点=文件（可复现）
python scripts/session_pull_ledger.py --json --anchor now    # 锚点=当前时刻
python scripts/session_pull_ledger.py --append               # 追加到账本（默认 ~/.trinity/state/session_pull_ledger.jsonl）
python scripts/session_pull_ledger.py --traces <path> --deliveries <path>   # 受控输入（判据/反事实用）
```

退出码：0 = 正常 / 2 = UNTESTABLE（源文件读不到 ⇒ **不静默当通过**）
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter
import logging

STATE = os.path.join(os.path.expanduser("~"), ".trinity", "state")
DEFAULT_TRACES = os.path.join(STATE, "retrieval_traces.jsonl")
DEFAULT_DELIVERIES = os.path.join(STATE, "opening_surface_deliveries.jsonl")
DEFAULT_LEDGER = os.path.join(STATE, "session_pull_ledger.jsonl")
STORE_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db"),
    os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db"),
]
SCHEMA = "trinity.session_pull_ledger/1"
CALIBER = "会话侧(pull 腿)；注入侧另计 —— 两侧禁止相加/相减/并列"


# ── 读源（尾部扫描；容错：文件正在被写 ⇒ 末行可能截断）────────────────────────
def _iter_jsonl(path: str, max_bytes: int | None = None):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with io.open(path, "rb") as fh:
        size = os.path.getsize(path)
        if max_bytes is not None and size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()                      # 丢掉半行
        for raw in fh:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except json.JSONDecodeError:
                continue                       # 半行/坏行 → 跳过（不静默当成 0：计数在下面报）


def _load_store_sizes(ids) -> dict:
    """只读取 content 长度；取不到就**不**给数字（调用方按 0 计并计数）。"""
    out = {}
    for cand in STORE_CANDIDATES:
        if not os.path.isfile(cand):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % cand.replace("\\", "/"),
                                  uri=True, timeout=10)
            try:
                todo = [i for i in ids if i not in out]
                for k in range(0, len(todo), 400):
                    chunk = todo[k:k + 400]
                    marks = ",".join("?" * len(chunk))
                    for mid, content in con.execute(
                            "SELECT memory_id, content FROM memories WHERE memory_id IN (%s)"
                            % marks, chunk):
                        out[str(mid)] = len(content or "")
            finally:
                con.close()
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            print("session_pull_ledger: 库里取正文失败（%s）：%s" % (cand, e), file=sys.stderr)
    return out


def _tokens_est(chars: int) -> int:
    """**字符估算**：ceil(chars/4)，最小 1。⚠️ 非模型分词器，不得与分词器口径的数并列。"""
    return max(1, int(math.ceil((chars or 0) / 4.0)))


def build_record(
    traces_path: str = DEFAULT_TRACES,
    deliveries_path: str = DEFAULT_DELIVERIES,
    hours: float = 24.0,
    anchor: str = "file",
    now: float | None = None,
    max_scan_bytes: int | None = None,
    resolve_content: bool = True,
) -> dict:
    """核心：给定窗口，算**会话侧**读数（纯函数式，可独立复算）。"""
    recs = list(_iter_jsonl(traces_path, max_bytes=max_scan_bytes))
    if not recs:
        raise ValueError("轨迹文件里没有任何可解析记录：%s" % traces_path)
    stamps = [float(r.get("ts") or 0) for r in recs if r.get("ts")]
    if anchor == "now":
        end = float(now if now is not None else time.time())
    else:
        end = max(stamps) if stamps else (float(now) if now is not None else time.time())
    start = end - hours * 3600.0

    win = [r for r in recs if start <= float(r.get("ts") or 0) <= end]
    calls_agent = calls_probe = calls_other = 0
    seen = Counter()
    per_call_ids = []
    for r in win:
        ids = [str(x) for x in (r.get("chosen") or []) if x]
        per_call_ids.append(ids)
        if r.get("agent_id"):
            calls_agent += 1
        elif "probe" in str(r.get("meta", {}).get("strategy") or "").lower():
            calls_probe += 1
        elif r.get("agent_id") == "":
            calls_probe += 1
        else:
            calls_other += 1
        for i in ids:
            seen[i] += 1

    pull_calls = len(win)
    distinct_items = len(seen)
    repeat_items = sum(1 for _i, n in seen.items() if n > 1)
    hits = sum(seen.values())
    calls_without_ids = sum(1 for ids in per_call_ids if not ids)
    # 两个都写清楚，避免"repeat_rate"这种含糊名（t88 自纠：最初定义为"没有结果的调用占比"，名不副实）
    repeat_item_share = (repeat_items / float(distinct_items)) if distinct_items else 0.0
    extra_use_share = (sum(n - 1 for n in seen.values()) / float(hits)) if hits else 0.0

    reacquired = {i: n for i, n in seen.items() if n > 1}
    extra_uses = sum(n - 1 for n in reacquired.values())

    sizes = _load_store_sizes(list(reacquired)) if (resolve_content and reacquired) else {}
    resolved = sum(1 for i in reacquired if i in sizes)
    reacquired_tokens = sum(_tokens_est(sizes.get(i, 0)) * (n - 1) for i, n in reacquired.items())

    # 2026-10-07（t88 phase-2）：轨迹现在**自带**两个字段（`pull_calls_delta` / `reacquired_hit_ids`）
    # ⇒ 两条口径都报出来做**交叉核对**：不一致就说明"窗口大小 / 计费口径"有漂移（不静默取一个）。
    trace_field_calls = sum(int(r.get("pull_calls_delta") or 0) for r in win)
    trace_field_reacq = sum(len(r.get("reacquired_hit_ids") or []) for r in win)

    # 注入侧：**只读引用**（单独命名空间，禁止与本侧求和）
    inj_n = inj_distinct = 0
    overlap = 0
    try:
        inj_ids = []
        for d in _iter_jsonl(deliveries_path):
            ts = float(d.get("ts") or 0)
            if start <= ts <= end:
                ids = [str(x) for x in (d.get("ids") or []) if x]
                inj_n += len(ids)
                inj_ids.extend(ids)
        inj_distinct = len(set(inj_ids))
        overlap = len(set(inj_ids) & set(seen))
    except FileNotFoundError:
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/session_pull_ledger.py::build_record")

    return {
        "schema": SCHEMA,
        "side": "session-pull",
        "caliber": CALIBER,
        "anchor": anchor,
        "window_start_epoch": round(start, 3),
        "window_end_epoch": round(end, 3),
        "window_hours": hours,
        "pull_calls": pull_calls,
        "pull_calls_agent": calls_agent,
        "pull_calls_probe": calls_probe,
        "pull_calls_other": calls_other,
        "distinct_items": distinct_items,
        "repeat_items": repeat_items,
        "hits_total": hits,
        "repeat_item_share": round(repeat_item_share, 4),
        "extra_use_share": round(extra_use_share, 4),
        "calls_without_ids": calls_without_ids,
        "reacquired_items": len(reacquired),
        "reacquired_extra_uses": extra_uses,
        "reacquired_tokens": reacquired_tokens,
        "trace_field_pull_calls": trace_field_calls,
        "trace_field_reacquired_items": trace_field_reacq,
        "reacquired_source": ("两条口径并列：窗口扫描(上面的 reacquired_*) 与轨迹字段(trace_field_*，"
                              "t88 phase-2 落地)；两者不一致 ⇒ 口径漂移，需人工看，不静默取一个"),
        "token_estimator": "ceil(len(content)/4)，min 1 —— 字符估算，非模型分词器",
        "content_resolved_items": resolved,
        "delivered_overlap_items": overlap,
        "injection_delivered_n": inj_n,
        "injection_delivered_distinct": inj_distinct,
        "injection_side_note": "注入侧只读引用；t58 的 −71% 是这一侧 —— 禁止与本侧相加",
        "sources": {"traces": traces_path, "deliveries": deliveries_path},
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def append_record(rec: dict, ledger: str = DEFAULT_LEDGER) -> str:
    """把一条记录**响亮地**写进账本（判据 T1 的牙齿就打在"这里被改成静默"上）。

    ⚠️ 本函数刻意**不做静默降级**：写不进去就抛出（调用方报错退出），
    否则"计费"会变成"看着像在记、其实没记"——正是本轮一路在抓的形态。
    """
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with io.open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return ledger


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/session_pull_ledger.py::main")
    ap = argparse.ArgumentParser(description="会话侧(pull 腿)再获取计费")
    ap.add_argument("--traces", default=DEFAULT_TRACES)
    ap.add_argument("--deliveries", default=DEFAULT_DELIVERIES)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--anchor", choices=("file", "now"), default="file")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--append", action="store_true", help="追加一条记录到账本")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        rec = build_record(args.traces, args.deliveries, args.hours, args.anchor)
    except (FileNotFoundError, ValueError) as e:
        print("session_pull_ledger UNTESTABLE: %s" % e)   # fail-closed，不当 0
        return 2
    if args.append:
        append_record(rec, args.ledger)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    else:
        print("会话侧账本（%s，窗口 %.1fh，锚点=%s）" % (rec["caliber"], rec["window_hours"], rec["anchor"]))
        print("  pull_calls=%d（agent=%d / probe=%d / other=%d）  distinct=%d  重复条目=%d  重复条目占比=%.4f"
              % (rec["pull_calls"], rec["pull_calls_agent"], rec["pull_calls_probe"],
                 rec["pull_calls_other"], rec["distinct_items"], rec["repeat_items"],
                 rec["repeat_item_share"]))
        print("  ⭐ reacquired_items=%d  reacquired_extra_uses=%d  reacquired_tokens=%d（%s）"
              % (rec["reacquired_items"], rec["reacquired_extra_uses"], rec["reacquired_tokens"],
                 rec["token_estimator"]))
        print("  （只读引用）注入侧投递 %d 条 / distinct %d；与本侧交集 %d —— **不得相加**"
              % (rec["injection_delivered_n"], rec["injection_delivered_distinct"],
                 rec["delivered_overlap_items"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

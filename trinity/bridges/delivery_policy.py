#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delivery_policy.py —— 投递层回路 v2：**覆盖优先的投递策略**（T9，2026-10-06）。

## 为什么需要它（先说病征，再说设计）

2026-10-06 复核到的**投递层**实测（口径见 `DELIVERY-LAYER.md` §3，命令可复现）：

    U1（PG 全库）       active 27869，30 天覆盖率 0.1658  ⇒ 冷池 83.4%
    U1a（PG 24h）       agent 前缀归属的读取 = 6 条
    U2（注入账本）      生产来源 dsh-plugin：1753 次组装 / 8765 条投递
    投递账本（24h 生产） 34 批 / 170 条投递 / **去重仅 39 条** / 重复率 0.771
    投递账本（30 天生产）108 批 / 540 条投递 / **去重仅 159 条** / 覆盖率 0.0057
    单会话平均命中       5.0（= top_k，恒定）

⇒ **不存在"读取需求"这一点已经被治过了**（注入通路在跑，且是生产来源），
剩下的是**同一个病换了个位置**：投递的是**同一小撮热记忆**（24h 内 39 条去重、
重复率 77%），冷池 2.3 万条**结构性读不到** —— 引擎侧 `_COLD_SLOTS_DEFAULT = "0"`，
冷通道在生产上**一个槽位都不占**，只有"热命中恰好还没被检索过"的偶然冷投递。

## 设计三条约束（与 T9 硬要求一一对应）

① **可一键关闭**：`TRINITY_DELIVERY_V2`（默认 **off**）。off 时本模块
   **逐字段返回入参对象本身**（`apply_coverage_policy` 首行早返回），
   ⇒ 引擎行为与改动前**逐字节等价**，回滚杠杆 = 删掉这一个环境变量。
② **有界**：条数 ≤ `top_k`（沿用配额，**不追加**）；字符/估算 token 上限
   `TRINITY_DELIVERY_TOKEN_BUDGET`（默认 1500，与 U4 判据同口径 chars/4）；
   超限从尾部整行丢弃并记 `dropped_for_budget`。
③ **可解释**：每次投递返回 `delivery_plan`：每条为什么进（`why` ∈
   `hot` / `novel` / `cold` / `backfill`）、因为"近期已投过"被换下的 id
   （`swapped_out_recent`）、近期窗口与各槽位计数。同时写进投递账本
   （`delivery_ledger.record_deliveries(..., policy=..., novel_ids=...)`）。

失败一律 **fail-open**：任何异常都返回入参 surface（绝不抛出、绝不空投）。

## 策略（只在 V2=on 时生效）

    slots_novel = min(TRINITY_DELIVERY_NOVEL_SLOTS, top_k)      # 默认 2
    其余 top_k - slots_novel 格：沿用相似度排名（**旧行为原样保留**）
    novelty 格：从**更大池子**（`pool`，引擎多取 3×）里按排名取
                「近 `TRINITY_DELIVERY_RECENT_WINDOW_S`（默认 24h）内**没投过**」的条目
    novelty 候选取不满 ⇒ 用剩余排名条目回填（`backfill`）⇒ **sources 不下降**

为什么用"近期未投递"而不是"从未被检索"：投递（图册）**不写** `last_retrieved_at`
（U1 口径刻意如此，见 `delivery_ledger` 模块头），所以 SQL 层面的"冷"判不了
"这条刚被投进上下文三次"。**近期投递去重只能来自投递账本**——这正是本模块读账本的原因。

回滚：`TRINITY_DELIVERY_V2=off`（或删变量）⇒ 回到 V1 逐字节行为；
`TRINITY_DELIVERY_NOVEL_SLOTS=0` ⇒ 只保留 V2 记账、不换任何条目（第二道杠杆）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

#: T94/C2：**静默必须是显式选择**（Debezium 3.7 的 error/ignore 纪律）。
#: 本模块原先有 4 处 `except: pass`（best-effort 清理与冷集标记）——现在一律**留痕**（debug 级），
#: 既不改变行为，也不再吞掉诊断信息。
logger = logging.getLogger(__name__)

#: 总开关（默认 off ⇒ 与改动前逐字节等价）。
POLICY_GATE = "TRINITY_DELIVERY_V2"
#: novelty 槽位数（默认 2；0 = 只记账不换条目）。
NOVEL_SLOTS_ENV = "TRINITY_DELIVERY_NOVEL_SLOTS"
#: "近期已投递"的窗口（秒，默认 24h）。
RECENT_WINDOW_ENV = "TRINITY_DELIVERY_RECENT_WINDOW_S"
#: 注入块 token 上限（chars/4 口径，与 U4 同）。
TOKEN_BUDGET_ENV = "TRINITY_DELIVERY_TOKEN_BUDGET"
#: V2=on 时冷通道至少占几格（默认 1；0 = 不碰冷通道）。
COLD_SLOTS_V2_ENV = "TRINITY_DELIVERY_COLD_SLOTS"
#: 是否把 `why` 写进**注入文本本身**（默认 off：不进模型上下文，只进返回值与账本）。
EXPLAIN_IN_BLOCK_ENV = "TRINITY_DELIVERY_EXPLAIN_IN_BLOCK"
#: 读账本时的尾部行数上限（防账本无限增长把取数路径拖慢）。
MAX_LEDGER_LINES = 2000
#: 单行正文截断（与 `opening_surface` 一致，口径不得漂移）。
LINE_CONTENT_CHARS = 400
HEADER = "相关记忆（开场浮现，仅作参考语境）:\n"
WHY_TAG = {"hot": "hot", "novel": "novel", "cold": "cold", "backfill": "backfill"}


def _env_flag(name: str, default: str = "off") -> bool:
    try:
        return str(os.environ.get(name, default)).strip().lower() in ("on", "1", "true", "yes")
    except Exception:  # noqa: BLE001
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except Exception:  # noqa: BLE001
        return int(default)


def policy_enabled() -> bool:
    """总开关。默认 off ⇒ 引擎不看本模块的任何逻辑。"""
    return _env_flag(POLICY_GATE)


def novel_slots(top_k: int) -> int:
    return max(0, min(_env_int(NOVEL_SLOTS_ENV, 2), int(top_k or 0)))


def recent_window_s() -> int:
    return max(0, _env_int(RECENT_WINDOW_ENV, 86400))


def token_budget() -> int:
    return max(0, _env_int(TOKEN_BUDGET_ENV, 1500))


def cold_slots_v2(base: int, top_k: int = 5) -> int:
    """V2=on 时给**冷通道**至少 `TRINITY_DELIVERY_COLD_SLOTS`（默认 1）格。

    为什么要这一步（这是 T9 里**最有价值的一格**）：生产上 `_COLD_SLOTS_DEFAULT = "0"`
    ⇒ 冷通道一个槽位都不占，2.3 万条冷池只能靠"热命中恰好还没被检索过"的偶然。
    冷候选与相似度**不同源**（按 category 分层抽样 + 稳定哈希），是唯一能让
    "从没被读过的记忆"真的进上下文的通道。V2=off 时**原样返回 base**（逐字节等价）。
    """
    if not policy_enabled():
        return int(base or 0)
    n = max(0, _env_int(COLD_SLOTS_V2_ENV, 1))
    return max(int(base or 0), min(n, max(0, int(top_k or 0))))


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _ledger_tail(path: str, max_lines: int = MAX_LEDGER_LINES) -> List[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-max_lines:]
    except Exception:  # noqa: BLE001 —— 账本缺失/不可读不是失败，是"没有近期信息"
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def recent_delivery_counts(window_s: Optional[int] = None, path: Optional[str] = None,
                           now: Optional[float] = None,
                           skip_origins: Sequence[str] = ()) -> Dict[str, int]:
    """近窗口内每个 memory_id 被**投递**过几次（读投递账本；失败返回 {}）。

    `skip_origins`：跳过哪些来源的记录（探针自报可跳过 ⇒ 生产读数不被夹具污染）。
    """
    try:
        from trinity.bridges.delivery_ledger import ledger_path as _lp
        p = path or _lp()
    except Exception:  # noqa: BLE001
        p = path or ""
    if not p:
        return {}
    win = recent_window_s() if window_s is None else int(window_s)
    cut = float(now if now is not None else time.time()) - win
    skip = {str(s) for s in (skip_origins or ())}
    counts: Dict[str, int] = {}
    for rec in _ledger_tail(p):
        try:
            if float(rec.get("ts") or 0) < cut:
                continue
        except Exception:  # noqa: BLE001
            continue
        if skip and str(rec.get("origin") or "") in skip:
            continue
        for mid in (rec.get("ids") or []):
            m = str(mid)
            if m:
                counts[m] = counts.get(m, 0) + 1
    return counts


def _item_id(it: Any) -> str:
    if not isinstance(it, dict):
        return ""
    return str(it.get("memory_id") or it.get("id") or "")


def _candidate_gate(pool: List[dict]) -> List[dict]:
    """候选池过**与图册同一条白名单**（`filter_atlas_sources`）。

    为什么必须过：本模块能从**更大的池子**取条目（`top_k * mult`），而
    `build_opening_surface` 只对进块的那几条做了白名单过滤。若这里不过滤，
    策略就会把被图册拒掉的（评测命名空间 / 白名单外来源）条目**重新塞回上下文**
    —— 那等于在投递层开了一个绕过 §P0-2 白名单的后门。过滤函数自身出错时
    返回**空池**（宁可少换，不可放行）。
    """
    try:
        from trinity.retrieval.evidence_gate import filter_atlas_sources
        kept, _skipped = filter_atlas_sources(list(pool or []))
        return list(kept or [])
    except Exception:  # noqa: BLE001 —— 白名单不可用 ⇒ 不扩池（fail-closed 在安全侧）
        return []


def _cold_gate(cands: List[Any]) -> List[dict]:
    """冷候选过 `opening_surface` 的同一组纪律（非生产来源 / 正文合格）。"""
    out: List[dict] = []
    try:
        from trinity.bridges.opening_surface import _atlas_source_ok, cold_text_ok
    except Exception:  # noqa: BLE001
        return []
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        try:
            if not _atlas_source_ok(c):
                continue
            if not cold_text_ok(c):
                continue
        except Exception:  # noqa: BLE001
            continue
        out.append(c)
    return out


def _ordered_candidates(pool: Iterable[Any], cold_candidates: Iterable[Any]) -> List[dict]:
    """有序候选表（含正文）：相似度池在前（按排名），冷候选在后。逐 id 去重。"""
    out: List[dict] = []
    seen: Set[str] = set()
    hot_pool = _candidate_gate([it for it in (pool or []) if isinstance(it, dict)])
    cold_pool = _cold_gate([it for it in (cold_candidates or []) if isinstance(it, dict)])
    for src, why in ((hot_pool, "hot"), (cold_pool, "cold")):
        for it in src:
            if not isinstance(it, dict):
                continue
            if it.get("untrusted") is True:
                continue
            mid = _item_id(it)
            if not mid or mid in seen:
                continue
            if not str(it.get("content") or it.get("content_preview") or ""):
                continue
            seen.add(mid)
            row = dict(it)
            row["_rank_why"] = why
            out.append(row)
    return out


def _render(items: List[dict], whys: Dict[str, str], explain_in_block: bool) -> str:
    lines = []
    for i, r in enumerate(items, 1):
        content = str(r.get("content") or r.get("content_preview") or "")[:LINE_CONTENT_CHARS]
        cat = r.get("category") or "memory"
        if explain_in_block:
            tag = WHY_TAG.get(whys.get(_item_id(r), ""), "?")
            lines.append(f"{i}. [{cat}|{tag}] {content}")
        else:
            lines.append(f"{i}. [{cat}] {content}")
    if not lines:
        return ""
    return HEADER + "\n".join(lines)


def apply_coverage_policy(surface: Dict[str, Any], *,
                          pool: Optional[Iterable[Any]] = None,
                          cold_candidates: Optional[Iterable[Any]] = None,
                          top_k: int = 5,
                          cold_set: Any = None,
                          recent: Optional[Dict[str, int]] = None,
                          now: Optional[float] = None) -> Dict[str, Any]:
    """覆盖优先策略（**纯函数 + fail-open**）。

    - V2=off ⇒ 原样返回入参（同一对象），引擎行为与改动前逐字节等价；
    - 任何异常 ⇒ 原样返回入参；
    - 空面（sources=0）⇒ 原样返回（本模块**不制造**投递，只重排配额）。
    """
    if not policy_enabled():
        return surface
    try:
        if not isinstance(surface, dict) or int(surface.get("sources") or 0) <= 0:
            return surface
        budget_slots = max(1, int(top_k or 0))
        n_novel = novel_slots(budget_slots)
        cands = _ordered_candidates(pool, cold_candidates)
        if not cands:
            return surface
        recent_counts = recent if recent is not None else recent_delivery_counts()
        win = recent_window_s()

        kept_now = [it for it in (surface.get("_kept") or []) if isinstance(it, dict)]
        if not kept_now:
            # 引擎只回传 md/delivered_ids ⇒ 用 id 身份在候选表里定位正文。
            by_id = {_item_id(c): c for c in cands}
            kept_now = [by_id[m] for m in (surface.get("delivered_ids") or [])
                        if m in by_id]

        whys: Dict[str, str] = {}
        chosen: List[dict] = []
        chosen_ids: Set[str] = set()
        swapped_out_recent: List[str] = []

        def _take(row: dict, why: str) -> None:
            mid = _item_id(row)
            if not mid or mid in chosen_ids:
                return
            chosen_ids.add(mid)
            whys[mid] = why
            chosen.append(row)

        # ① 必保集合：冷通道配额（`kept_now` 里 why=cold 的那几条）
        #    必须**优先占位**，否则 novelty 轮换会把冷条目挤掉 —— 那正好把
        #    "冷池读不到"这个病治反了（换了一堆热条目，冷覆盖反而下降）。
        mandatory = [r for r in kept_now if r.get("_rank_why") == "cold"]
        mandatory_ids = {_item_id(r) for r in mandatory}
        # ② 旧行为配额：排名前 (top_k - novelty - 冷配额) 格原样保留
        hot_quota = max(0, budget_slots - n_novel - len(mandatory))
        hot_kept_count = 0
        for row in kept_now:
            if hot_kept_count >= hot_quota:
                break
            if _item_id(row) in mandatory_ids:
                continue
            _take(row, "hot")
            hot_kept_count += 1
        # ③ novelty 格：从更大池子里取「近期没投过」的条目（冷候选也算候选）
        if n_novel:
            pending_cold = len([m for m in mandatory if _item_id(m) not in chosen_ids])
            for row in cands:
                if len(chosen) + pending_cold >= budget_slots:
                    break
                mid = _item_id(row)
                if mid in chosen_ids or mid in mandatory_ids:
                    continue
                if recent_counts.get(mid):
                    swapped_out_recent.append(mid)
                    continue
                _take(row, "novel" if row.get("_rank_why") != "cold" else "cold")
        # ④ 冷配额落地（novelty 让位给冷通道）
        for row in mandatory:
            if len(chosen) >= budget_slots:
                break
            _take(row, "cold")
        # ⑤ 回填：候选不足 ⇒ 用剩余排名条目补满，**sources 不下降**
        for row in list(kept_now) + list(cands):
            if len(chosen) >= budget_slots:
                break
            _take(row, "backfill")

        # ④ 有界：token 预算（整行丢弃，绝不截半行）
        md = _render(chosen, whys, _env_flag(EXPLAIN_IN_BLOCK_ENV))
        dropped_for_budget: List[str] = []
        tbudget = token_budget()
        while chosen and tbudget and _estimate_tokens(md) > tbudget:
            gone = chosen.pop()
            dropped_for_budget.append(_item_id(gone))
            md = _render(chosen, whys, _env_flag(EXPLAIN_IN_BLOCK_ENV))

        final_ids = [_item_id(r) for r in chosen]
        # ⑤ 冷归因：只把**新加入**且 ColdSet 仍判冷的条目标冷（不得双重计数）。
        prev_cold = {str(x) for x in (surface.get("cold_ids") or [])}
        new_cold: List[str] = []
        cold_ids_final = [m for m in final_ids if m in prev_cold]
        if cold_set is not None:
            for mid in final_ids:
                if mid in prev_cold or mid in cold_ids_final:
                    continue
                try:
                    if cold_set.is_cold(mid):
                        new_cold.append(mid)
                except Exception:  # noqa: BLE001
                    continue
            if new_cold:
                try:
                    cold_set.mark_touched(new_cold)
                except Exception as exc:  # noqa: BLE001
                    #: 冷集标记失败 ⇒ 不阻断投递，但**必须留痕**（T94：不再裸 pass）
                    logger.debug("delivery_policy: cold_set.mark_touched 失败（继续投递）：%r",
                                 exc, exc_info=True)
            cold_ids_final = [m for m in final_ids if m in prev_cold or m in set(new_cold)]

        out = dict(surface)
        out["surface_md"] = md
        out["sources"] = len(chosen)
        out["delivered_ids"] = final_ids
        out["cold_ids"] = cold_ids_final
        out["cold_sources"] = len(cold_ids_final)
        out["delivery_plan"] = {
            "policy": "coverage-first/v2",
            "gate": POLICY_GATE,
            "top_k": budget_slots,
            "novel_slots": n_novel,
            "recent_window_s": win,
            "recent_ids_seen": len(recent_counts),
            "items": [{"memory_id": _item_id(r), "why": whys.get(_item_id(r), "?"),
                       "recent_times": int(recent_counts.get(_item_id(r), 0)),
                       "rank": i + 1} for i, r in enumerate(chosen)],
            "swapped_out_recent": swapped_out_recent[:10],
            "dropped_for_budget": dropped_for_budget,
            "cold_ids": cold_ids_final,
            "chars": len(md),
            "tokens_est": _estimate_tokens(md),
            "token_budget": tbudget,
            "ts": float(now if now is not None else time.time()),
        }
        return out
    except Exception:  # noqa: BLE001 —— fail-open：策略出错绝不能影响投递
        return surface


# ── CLI：读 / 自检（不启动任何服务，不需要重启） ─────────────────────────────
def _cmd_stats(days: int) -> int:
    try:
        from trinity.bridges.delivery_ledger import delivery_stats
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps({"v1_v2_gate": policy_enabled(), "novel_slots": novel_slots(5),
                      "days": days, "delivery_stats": delivery_stats(days=days)},
                     ensure_ascii=False, indent=1))
    return 0


def _cmd_selftest(query: str, sessions: int, top_k: int, queries_file: str = "") -> int:
    """真引擎 A/B：同一批开局语，V2 off vs on（两个 worker 进程，同一个库）。

    · 每个臂各自用一个**隔离账本**（`TRINITY_DELIVERY_LEDGER`）⇒ 两臂的"近期已投递"
      互不可见；生产账本**一个字节都不动**；
    · 来源自报 `probe:t9_selftest_<arm>` ⇒ 不污染生产桶（`by_origin['dsh-plugin']`）；
    · 空面单独计（`empty_surfaces`），不把"引擎没召回"记成"策略没生效"。
    """
    import subprocess
    import sys as _sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    worker = os.path.join(root, "trinity", "engine_worker.py")
    py = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = _sys.executable
    texts: List[str] = []
    if queries_file:
        try:
            with open(queries_file, encoding="utf-8", errors="replace") as fh:
                texts = [ln.strip() for ln in fh if ln.strip()]
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"queries_file unreadable: {e}"}, ensure_ascii=False))
            return 2
    if not texts:
        texts = [query]
    report: Dict[str, Any] = {"query": query[:80], "queries_file": queries_file or None,
                              "distinct_queries": len(set(texts)), "sessions": sessions,
                              "top_k": top_k, "worker": worker, "python": py, "arms": {}}
    for arm, gate in (("v1_off", "off"), ("v2_on", "on")):
        env = dict(os.environ)
        env["TRINITY_AUTO_RECALL"] = "on"
        env[POLICY_GATE] = gate
        env["TRINITY_OPENING_ORIGIN"] = f"probe:t9_selftest_{arm}"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        #: 与插件 spawn 的 env **逐字段一致**（lib/index.js:236）。不照抄这一段，
        #: 探针就落在**另一个存储**上（默认 sqlite：检索面是另一个库、冷集恒为 0 条）
        #: —— 那不是"策略没生效"，是"测的不是生产路径"（本仓反复踩过的夹具标定错误）。
        env["TRINITY_MEMORY_ENABLED"] = "0"
        env["TRINITY_STORAGE_BACKEND"] = "postgresql"
        env["TRINITY_ROUTE_REASONER"] = "on"
        env["TRINITY_DELIVERY_LEDGER"] = os.path.join(
            os.environ.get("TEMP", "."), f"t9_selftest_{arm}.jsonl")
        try:
            os.remove(env["TRINITY_DELIVERY_LEDGER"])
        except FileNotFoundError:
            #: 首次运行本来就没有账本 —— **显式的预期缺失**（不是吞异常）：留痕但不报错
            logger.debug("t9 selftest: 账本不存在（首次运行，预期）：%s", env["TRINITY_DELIVERY_LEDGER"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("t9 selftest: 清账本失败（继续）：%r", exc)
        #: T12：计数文件同样隔离 —— 探针绝不许写生产 `opening_surface_counters.json`
        #: （该文件的单值键是 last-writer-wins，探针会把它冒充成"生产现场"）。
        env["TRINITY_OPENING_COUNTERS"] = os.path.join(
            os.environ.get("TEMP", "."), f"t9_selftest_{arm}_counters.json")
        try:
            os.remove(env["TRINITY_OPENING_COUNTERS"])
        except FileNotFoundError:
            logger.debug("t9 selftest: 计数文件不存在（首次运行，预期）：%s",
                         env["TRINITY_OPENING_COUNTERS"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("t9 selftest: 清计数文件失败（继续）：%r", exc)
        proc = subprocess.Popen([py, worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", env=env, cwd=root)
        ids: List[str] = []
        cold: List[str] = []
        tokens: List[int] = []
        sources: List[int] = []
        plans: List[dict] = []
        empties = 0
        fallbacks = 0
        errors: List[str] = []

        def _one(i: int, params: dict) -> dict:
            req = {"id": i, "method": "opening", "params": params}
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("worker gave no line (exited?)")
            payload = json.loads(line)
            if payload.get("error"):
                raise RuntimeError(str(payload["error"])[:200])
            return payload.get("result") or {}

        try:
            for i in range(sessions):
                q = texts[i % len(texts)]
                sid = f"t9-selftest-{arm}-{i + 1}"
                base = {"top_k": top_k, "session_id": sid,
                        "origin": f"probe:t9_selftest_{arm}"}
                try:
                    # 与插件**逐字段一致**的两步：先会话作用域，空则回退全局
                    # （见 dsh-plugin/dsh-trinity/lib/index.js:954-967）。
                    # 不同步这一步，A/B 测的就不是生产路径。
                    r = _one(i * 2 + 1, {**base, "query": q, "agent_id": f"dsh-{sid}"})
                    scoped_sources = int(r.get("sources") or 0)
                    if not (isinstance(r.get("surface_md"), str) and r["surface_md"]):
                        fallbacks += 1
                        r2 = _one(i * 2 + 2, {**base, "query": q, "session_id": ""})
                        r2["_scoped_sources"] = scoped_sources
                        r = r2
                except Exception as e:  # noqa: BLE001
                    errors.append(f"request {i + 1}: {e}")
                    break
                got = [str(x) for x in (r.get("delivered_ids") or [])]
                if not got and not (r.get("surface_md") or ""):
                    empties += 1
                ids.extend(got)
                cold.extend(str(x) for x in (r.get("cold_ids") or []))
                sources.append(int(r.get("sources") or 0))
                tokens.append(_estimate_tokens(str(r.get("surface_md") or "")))
                if r.get("delivery_plan"):
                    plans.append(r["delivery_plan"])
        finally:
            try:
                proc.stdin.close()
                proc.wait(timeout=20)
            except Exception as exc:  # noqa: BLE001
                logger.debug("t9 selftest: worker 收尾失败，转 kill：%r", exc)
                try:
                    proc.kill()
                except Exception as exc2:  # noqa: BLE001
                    #: worker 已经不在了（或不允许 kill）⇒ 不阻断，但**留痕**
                    logger.debug("t9 selftest: worker kill 也失败（可能已退出）：%r", exc2)
        uniq = len(set(ids))
        report["arms"][arm] = {
            "gate": gate,
            "deliveries": len(ids),
            "distinct": uniq,
            "repeat_rate": (round((len(ids) - uniq) / len(ids), 4) if ids else None),
            "per_session_avg_hits": (round(len(ids) / sessions, 2) if sessions else None),
            "cold_deliveries": len(cold),
            "cold_distinct": len(set(cold)),
            "cold_share": (round(len(cold) / len(ids), 4) if ids else None),
            "sources_sum": sum(sources),
            "empty_surfaces": empties,
            "scoped_empty_fallbacks": fallbacks,
            "tokens_per_delivery": (round(sum(tokens) / len(tokens), 1) if tokens else None),
            "max_tokens": (max(tokens) if tokens else None),
            "distinct_ids": sorted(set(ids)),
            "plans_n": len(plans),
            "plan_sample": (plans[0] if plans else None),
            "errors": errors,
        }
    a, b = report["arms"]["v1_off"], report["arms"]["v2_on"]
    report["delta"] = {
        "distinct": b["distinct"] - a["distinct"],
        "repeat_rate": (round((b["repeat_rate"] or 0) - (a["repeat_rate"] or 0), 4)),
        "cold_deliveries": b["cold_deliveries"] - a["cold_deliveries"],
        "tokens_per_delivery": (round((b["tokens_per_delivery"] or 0) - (a["tokens_per_delivery"] or 0), 1)),
        "empty_surfaces": b["empty_surfaces"] - a["empty_surfaces"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="投递层回路 v2（覆盖优先策略）")
    ap.add_argument("--stats", action="store_true", help="读投递账本（只读）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--selftest", action="store_true", help="真引擎 A/B（V2 off vs on）")
    ap.add_argument("--query", default="Trinity 记忆系统 投递层 利用率")
    ap.add_argument("--queries-file", default="", dest="queries_file",
                    help="每行一条开局语（空则用 --query，逐次加 #i）")
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.stats:
        return _cmd_stats(args.days)
    if args.selftest:
        return _cmd_selftest(args.query, args.sessions, args.top_k, args.queries_file)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

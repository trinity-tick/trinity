#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delivery_ledger.py —— 开场浮现**投递账本**（2026-09-19 §870.1）。

## 为什么需要它

U1 覆盖率口径（W4 2026-09-13）把"检索时间戳"定义为"被**检索**过"，而开场浮现/图册
**投递**明确不写它（engine_worker.py:1126 / :1471）。后果两条：

  1. **口径**：被真正投进会话上下文（=被使用）的记忆，在利用率里完全不可见 ⇒ 覆盖率被低估；
  2. **功能**：`ColdSet` 只是**进程内**快照（worker 重启即从"时间戳为空"重取）⇒ 同一批冷记忆
     会被跨会话反复投递，而现有持久读数 `opening_surface_counters.json` 只有**总量**
     （calls/surfaced_total/cold_total），**没有 id 身份**，"重复投递率"根本算不出来。

本模块只补 **id 身份**：把每次投递的 id 列表追加进独立 JSONL。
**只测量、不改行为**：不写任何数据库字段（U1 判据不变）、不改投递逻辑（冷集仍按原判据取快照）。
拿到重复率之后，是否让冷集跨进程自消耗才是一个**有数据支撑**的决定。

记账绝不能拖垮投递 ⇒ 所有失败静默、返回 0。
路径可用 `TRINITY_DELIVERY_LEDGER` 覆盖（测试用）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, Optional

#: 单行最多记多少 id（防一次投递异常放大写放大）
MAX_IDS_PER_RECORD = 200


def ledger_path(env: Optional[Dict[str, str]] = None) -> str:
    e = env if env is not None else os.environ
    p = (e.get("TRINITY_DELIVERY_LEDGER") or "").strip()
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".trinity", "state",
                        "opening_surface_deliveries.jsonl")


def record_deliveries(memory_ids: Iterable[Any], origin: str = "", session_id: str = "",
                      path: Optional[str] = None, ts: Optional[float] = None,
                      cold_ids: Optional[Iterable[Any]] = None,
                      policy: str = "", novel_ids: Optional[Iterable[Any]] = None) -> int:
    """追加一条投递记录（一批一行）。返回记下的 id 条数；任何失败返回 0（静默）。

    `cold_ids`：本批里来自**冷通道**（分层抽样，不走检索 ⇒ 不写"检索时间戳"）的那些 id。
    分开记的理由（§872 校正）：只有**冷通道**的投递对 U1 覆盖率不可见；相似度通道的命中
    已被检索路径 touch 过。把两者混在一起会得出错误的"不可见比例"。

    `policy` / `novel_ids`（T9，2026-10-06）：**纯附加**字段，记录本批用的投递策略与
    "靠覆盖优先换进来的" id。旧读侧只读 `ids`/`cold_ids`/`ts`/`origin`/`session_id`，
    字段缺席时不受影响；这两个字段缺席即"V1 行为"，故新旧记录**同表可比**。
    """
    try:
        ids = [str(x) for x in (memory_ids or []) if x]
        if not ids:
            return 0
        p = path or ledger_path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        cold = [str(x) for x in (cold_ids or []) if x]
        rec = {"ts": float(ts if ts is not None else time.time()),
               "origin": str(origin or "")[:64],
               "session_id": str(session_id or "")[:128],
               "ids": ids[:MAX_IDS_PER_RECORD]}
        if cold:
            rec["cold_ids"] = cold[:MAX_IDS_PER_RECORD]
        if policy:
            rec["policy"] = str(policy)[:48]
        if novel_ids:
            rec["novel_ids"] = [str(x) for x in novel_ids if x][:MAX_IDS_PER_RECORD]
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(rec["ids"])
    except Exception:  # noqa: BLE001 —— 记账失败绝不影响投递
        return 0


def delivery_stats(days: int = 30, path: Optional[str] = None,
                   now: Optional[float] = None, top_n: int = 10,
                   skip_origins: Optional[Iterable[str]] = None,
                   origins: Optional[Iterable[str]] = None,
                   only_class: str = "") -> Dict[str, Any]:
    """近 N 天的投递读数（**只读**）：次数 / 去重 / 重复率 / 最常被重复投递的几条。

    ## T12（2026-10-06）：来源过滤 —— 为什么必须有

    实测 30 天账本被测试探针污染：`probe:test_opening_directory_empty` 一个来源就占了
    250/365 条记录，探针 id `m-2`/`m-1` 独占 250 次投递 ⇒ 未过滤的"重复率"度量的是
    **夹具**，不是生产投递质量。故本函数现在**同时给出两套数**：
    混合口径（`records/deliveries/distinct/repeat_rate`，兼容旧读侧）
    + 过滤口径（`filtered`，这才是"生产投递质量"）。

    参数（都是 additive，默认与旧版逐字段一致 ⇒ 旧调用点不受影响）：
      · `skip_origins` —— 精确排除的来源标签（**剔除探针**用这个）；
      · `origins`      —— 白名单（只保留这些来源）；
      · `only_class`   —— `prod` / `probe` / `unknown`（见 `origin_split.origin_class`）。

    返回新增（不删任何旧键）：`by_origin`（逐来源记录/投递数）、`filter`、`filtered`。
    """
    import collections as _c

    out: Dict[str, Any] = {"window_days": int(days), "records": 0, "deliveries": 0,
                           "distinct": 0, "repeat_rate": None, "max_repeat": 0, "top": []}
    try:
        from trinity.bridges.origin_split import origin_class
    except Exception:  # noqa: BLE001 —— 分类器不可用时按 unknown（**不冒充生产**）
        def origin_class(_o):  # type: ignore
            return "unknown"

    skip = {str(s) for s in (skip_origins or ()) if str(s)}
    allow = {str(s) for s in (origins or ()) if str(s)}
    oc = str(only_class or "")
    try:
        p = path or ledger_path()
        cut = float(now if now is not None else time.time()) - int(days) * 86400
        cnt_all: "_c.Counter[str]" = _c.Counter()
        cnt_fil: "_c.Counter[str]" = _c.Counter()
        rec_all = rec_fil = 0
        by_origin: Dict[str, Dict[str, Any]] = {}
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    ts = float(d.get("ts") or 0)
                except Exception:  # noqa: BLE001
                    ts = 0.0
                if ts < cut:
                    continue
                origin = str(d.get("origin") or "")
                cls = origin_class(origin)
                b = by_origin.setdefault(origin, {"class": cls, "records": 0, "deliveries": 0})
                ids = [str(m) for m in (d.get("ids") or [])]
                b["records"] += 1
                b["deliveries"] += len(ids)
                rec_all += 1
                for mid in ids:
                    cnt_all[mid] += 1
                keep = True
                if skip and origin in skip:
                    keep = False
                if allow and origin not in allow:
                    keep = False
                if oc and cls != oc:
                    keep = False
                if keep:
                    rec_fil += 1
                    for mid in ids:
                        cnt_fil[mid] += 1
        out["records"] = rec_all
        out["deliveries"] = sum(cnt_all.values())
        out["distinct"] = len(cnt_all)
        if out["deliveries"]:
            out["repeat_rate"] = round((out["deliveries"] - out["distinct"]) / out["deliveries"], 4)
        if cnt_all:
            out["max_repeat"] = max(cnt_all.values())
            out["top"] = [{"memory_id": k, "times": v} for k, v in cnt_all.most_common(top_n)]
        out["by_origin"] = by_origin
        out["filter"] = {"skip_origins": sorted(skip), "origins": sorted(allow),
                         "only_class": oc,
                         "note": ("`filtered` 才是**生产投递质量**；顶层 "
                                  "records/deliveries/distinct/repeat_rate 是混合口径"
                                  "（含探针），仅供旧读侧兼容与对照")}
        fil: Dict[str, Any] = {"records": rec_fil, "deliveries": sum(cnt_fil.values()),
                               "distinct": len(cnt_fil), "repeat_rate": None,
                               "max_repeat": 0, "top": []}
        if fil["deliveries"]:
            fil["repeat_rate"] = round((fil["deliveries"] - fil["distinct"]) / fil["deliveries"], 4)
        if cnt_fil:
            fil["max_repeat"] = max(cnt_fil.values())
            fil["top"] = [{"memory_id": k, "times": v} for k, v in cnt_fil.most_common(top_n)]
        out["filtered"] = fil
    except FileNotFoundError:
        out["note"] = "账本尚未创建（本模块上线后首次投递即写）"
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:120]
    return out

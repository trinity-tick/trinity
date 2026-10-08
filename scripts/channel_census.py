#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""channel_census.py —— 「通道」口径普查（P2-2，2026-09-17）

## 为什么需要它（2026-09-17 全面评价实测）

本仓到处出现「通道」这个词，而它至少指**三件不同的事**。实测同一天的三个读数：

    引擎初始化横幅 / 注册表 : **47**（`RetrievalSystemV47.channels`，键 `channel_1..channel_47`）
    `/health` contributing  : **5**（keyword / vector / second_brain / exabase / beamlight）
    一次 `search_hybrid` 的 breakdown : **3** 路非零（vector 5 / bm25 5 / graph 12；
                                        aggregator / procedural / pagetree 全 0）

三个数**不是矛盾**，但**不加限定地引用任何一个都会误导**：

  · 47 是**能力清单**（模块槽位），**不代表任何一路在生产上供料**
    —— `_hybrid_search.py:450` 自己就写着「47 通道的全路径权重在 PG 上完全不参与」；
  · 5 是**降级管理器**认为"真正贡献结果"的通道，另含 1 路 registry-only
    （`retrieval_v47`，原因：`search()` 恒返回 []）；
  · 3 是**某一次融合调用**里真正返回了行的通道。

本仓 `docs/SCORES.json` 的 `superseded_claims` 已经登记过这条口径问题
（"当说 47 通道时必须同时注明生产贡献的是哪 5 个"），但**没有一个可执行的东西把它钉住**。
本脚本就是那个东西：**把三个面并列打出来，各自带定义与来源**，并明确拒绝给出单一合并数。

## 纪律

· **只读**：不改任何东西（`--json` 只打印）；
· **fail-closed**：某个面取不到就报 `UNKNOWN` + 原因，**不得静默省略**
  （省掉一个面，读者就会以为"通道只有一个数"）；
· **拒绝合并**：输出里**没有**单一 `channel_count` 字段（用例压着这一条）。

用法：
    python scripts/channel_census.py            # 人读
    python scripts/channel_census.py --json     # 机读
退出码：0 = 三个面都取到；2 = 有面取不到（如实报，不崩）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

API = "http://127.0.0.1:8001"

#: `search_hybrid` 的 breakdown 里**不是通道**的键（标定依据，见 `_hybrid_supply` 的留痕）。
#: `unique_fused` 是融合后条数；`routing` / `routing_requested` 是路由状态；
#: `pg_forced_light` 是布尔开关（Python 里 bool 是 int 的子类，不排除就会被当成"通道"）。
NON_CHANNEL_KEYS = {"unique_fused", "routing", "routing_requested", "pg_forced_light",
                   # 2026-09-27（EXECUTION §1381）：`pg_forced_light` 的配套字段。
                   # 两者**结构上**已被下面的 isinstance 判据排除（一个是 bool、一个是 str），
                   # 列在这里是为了让这份「标定依据」保持**完整**（不是靠巧合正确）。
                   "routing_defaulted", "pg_forced_light_scope"}

#: 三个「通道」面 —— 每个面必须带 **定义** 与 **来源**，缺一不可。
SENSES = (
    {
        "id": "engine_registry",
        "name": "引擎注册面",
        "meaning": "**能力清单**（检索模块槽位 `channel_1..channel_47`）——"
                   "不代表任何一路在生产上供料",
        # 2026-10-06（t10 / R1 收口）：键名从 `ch01..ch47` 更正为
        # `channel_1..channel_47`。原因：t6 把 `guardian_retrieval.py` 从
        # 「同名不同体的第二份实现」改成 `engine_guardian_retrieval` 的**显式转口**，
        # 于是本脚本读到的就是**引擎真身**（键 `channel_1..channel_47`）。
        # **计数两种命名下都是 47**，本脚本的 headline 数字不变。
        # 定义点只有一处：engine_guardian_retrieval.py:215。
        "source": "trinity.modules.second_brain.engine_guardian_retrieval.RetrievalSystemV47.channels"
                  "（唯一实现；脚本经转口路径 guardian_retrieval 导入，同一对象）",
    },
    {
        "id": "health_contributing",
        "name": "健康贡献面",
        "meaning": "降级管理器认为**真正贡献结果**的通道；另有 registry-only 通道（附原因）",
        "source": "/health 的 engine.degradation.contributing_channels + registry_only_channels",
    },
    {
        "id": "hybrid_supply",
        "name": "融合供料面",
        "meaning": "**一次 `search_hybrid` 调用**里真正返回了行的通道（其余为 0）",
        "source": "search_hybrid 返回体的 breakdown",
    },
)


def _engine_registry() -> dict:
    try:
        from trinity.modules.second_brain.guardian_retrieval import RetrievalSystemV47
        rs = RetrievalSystemV47()
        chans = sorted(getattr(rs, "channels", {}) or {})
        return {"count": len(chans), "sample": chans[:5] + (["..."] if len(chans) > 5 else []),
                "unknown": False}
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "unknown": True, "reason": "%s: %s" % (type(exc).__name__, exc)}


def _health_contributing() -> dict:
    """优先走 REST（真常驻进程的读数）；不可达则退回进程内降级管理器。"""
    try:
        import urllib.request
        with urllib.request.urlopen(API + "/health", timeout=10) as fh:
            h = json.load(fh)
        dep = ((h.get("components") or {}) and (h.get("degradation") or {})) or {}
        dep = h.get("degradation") or {}
        return {
            "count": len(dep.get("contributing_channels") or []),
            "channels": dep.get("contributing_channels") or [],
            "registry_only": dep.get("registry_only_channels") or [],
            "registry_only_reasons": dep.get("registry_only_reasons") or {},
            "via": "rest:/health",
            "unknown": False,
        }
    except Exception:
        pass
    try:
        from trinity.agents.degradation import DegradationManager  # 名字可能不同，兜底再试
        dm = DegradationManager()
        st = dm.statistics() if hasattr(dm, "statistics") else {}
        return {
            "count": len(st.get("contributing_channels") or []),
            "channels": st.get("contributing_channels") or [],
            "registry_only": st.get("registry_only_channels") or [],
            "via": "in_process:DegradationManager",
            "unknown": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "unknown": True,
                "reason": "REST 不可达且进程内取不到：%s" % (type(exc).__name__,)}


def _hybrid_supply(query: str = "Trinity 检索通道融合") -> dict:
    try:
        import urllib.request
        body = json.dumps({"query": query, "top_k": 5}).encode("utf-8")
        req = urllib.request.Request(API + "/memory/search/hybrid", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as fh:
            r = json.load(fh)
        bd = r.get("breakdown") or {}
        # ⚠️ 标定留痕（首跑暴露）：初版用 `isinstance(v, int) and v > 0` 筛通道，
        # 于是把 **`unique_fused`（融合后条数，不是通道）** 和
        # **`pg_forced_light`（布尔，Python 里 bool 是 int 的子类）** 一起数了进去，
        # 得出"5 路"（真值 3）。**这正是本普查要防的那类错**：分不清"什么算通道"。
        # 处置：显式排除非通道键 + 排除 bool。
        nonzero = {k: v for k, v in bd.items()
                   if k not in NON_CHANNEL_KEYS
                   and isinstance(v, int) and not isinstance(v, bool) and v > 0}
        zero = {k: v for k, v in bd.items()
                if k not in NON_CHANNEL_KEYS
                and isinstance(v, int) and not isinstance(v, bool) and v == 0}
        return {"count": len(nonzero), "channels": sorted(nonzero), "zero_channels": sorted(zero),
                "non_channel_keys_seen": sorted(set(bd) & NON_CHANNEL_KEYS),
                "breakdown": bd, "via": "rest:/memory/search/hybrid", "unknown": False}
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "unknown": True, "reason": "%s: %s" % (type(exc).__name__, exc)}


def census(query: str = "Trinity 检索通道融合") -> dict:
    """三个面并列求值。**不产生任何单一合并数**（见文件头纪律）。"""
    vals = {
        "engine_registry": _engine_registry(),
        "health_contributing": _health_contributing(),
        "hybrid_supply": _hybrid_supply(query),
    }
    out = {"senses": [], "query": query}
    for spec in SENSES:
        rec = dict(spec)
        rec.update(vals[spec["id"]])
        out["senses"].append(rec)
    out["unknown_senses"] = [s["id"] for s in out["senses"] if s.get("unknown")]
    out["note"] = ("三个面**不是同一件事**，引用时必须说明是哪一个面；"
                   "本普查**刻意不给出**单一合并数 —— 那正是本项要治的口径混淆。")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="「通道」口径普查（三面并列，拒绝合并）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--query", default="Trinity 检索通道融合")
    args = ap.parse_args()

    r = census(args.query)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("=" * 82)
        print("「通道」口径普查 —— 三套「通道」不是同一件事（P2-2）")
        print("=" * 82)
        for s in r["senses"]:
            if s.get("unknown"):
                print("  %-10s : UNKNOWN —— %s" % (s["name"], s.get("reason")))
                print("               来源：%s" % s["source"])
                continue
            print("  %-10s : **%s**" % (s["name"], s["count"]))
            if s.get("channels"):
                print("               通道：%s" % ", ".join(s["channels"]))
            if s.get("sample"):
                print("               槽位：%s" % ", ".join(s["sample"]))
            if s.get("registry_only"):
                print("               仅注册不贡献：%s（%s）" % (
                    ", ".join(s["registry_only"]),
                    "; ".join("%s=%s" % kv for kv in (s.get("registry_only_reasons") or {}).items())))
            if s.get("zero_channels"):
                print("               本次为 0 的通道：%s" % ", ".join(s["zero_channels"]))
            print("               含义：%s" % s["meaning"])
            print("               来源：%s（%s）" % (s["source"], s.get("via", "?")))
        print("-" * 82)
        print("  ⇒ %s" % r["note"])
        if r["unknown_senses"]:
            print("  ⚠️ 取不到的面：%s（如实报，**不得**当成 0）" % r["unknown_senses"])
    return 2 if r["unknown_senses"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

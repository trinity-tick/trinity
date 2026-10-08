#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""origin_split.py —— 计数按**来源类别**分列（T12，2026-10-06）。

## 为什么要它：判据没有判别力（这是本轮的真正病根）

`trinity/engine_worker.py::_opening_bump` 的 docstring 自己写着：

    `empty_total` —— ok 但 `sources == 0` 的次数 ⇒ **判据：必须为 0**

而实测（2026-10-06 16:54 读 `~/.trinity/data/opening_surface_counters.json`）：

    empty_total = 197 / enabled = 1087  ⇒ 18.1%（判据红）
    by_origin 拆分：probe:test_opening_directory_empty 128、probe:t9_selftest_v1_off 30、
                    probe:t9_selftest_v2_on 30、probe:_p816_* 4、probe:t9_diag 3
                    ⇒ **探针合计 195/197**
                    生产来源 dsh-plugin = **1**

⇒ 这条"必须为 0"的判据**把探针与生产混在同一个数里**，于是：跑一次测试就把它推红，
没人能拿它当门禁 —— **判据在、判别力不在**（与"接了线但恒等于 no-op"同族）。

## 本模块做什么

只做**分类与聚合**（纯函数，可单测）：把计数器按 `prod`（`dsh-plugin`）/
`probe`（`probe:*`）/`unknown`（其余）三类分列，让"必须为 0"只作用于**生产**，
同时把"探针跑了多少"作为**单独可核**的数字保留下来（不是删掉，是不混）。

⚠️ 分类是**观测口径**，不是安全边界：`origin` 由调用方自报（探针必须自报 `probe:*`，
这是既有纪律）；生产判据据此才有意义。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

#: 唯一的生产来源标签（`dsh-plugin` = DSH 宿主注入通路；见 t9 DELIVERY-LAYER.md §2）
PROD_ORIGIN = "dsh-plugin"
#: 探针自报前缀
PROBE_PREFIX = "probe:"

CLASSES = ("prod", "probe", "unknown")


def origin_class(origin: Any) -> str:
    """把来源标签归到三类之一（未知一律 `unknown`，**不得**默认算生产）。"""
    try:
        o = str(origin or "").strip()
    except Exception:  # noqa: BLE001
        return "unknown"
    if o == PROD_ORIGIN:
        return "prod"
    if o.startswith(PROBE_PREFIX):
        return "probe"
    return "unknown"


def _int(v: Any, dflt: int = 0) -> int:
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return dflt


def empty_split(counters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 `empty_total` / `enabled` 按来源类别分列。返回判定用的一组数。

    返回：
      · `empty_total`         —— 兼容旧读侧（混合口径，**保留但标注**）
      · `empty_by_class`      —— {prod, probe, unknown}
      · `enabled_by_class`    —— 同上
      · `empty_rate_prod`     —— 生产空率（**这才是"必须为 0"的对象**）
      · `empty_rate_all`      —— 混合空率（旧的 18.1% 口径，仅作对照）
      · `criterion`           —— {"applies_to": "prod", "must_be": 0, "pass": bool}
    """
    c = counters or {}
    by_origin = c.get("by_origin") or {}
    empty_by_class = {k: 0 for k in CLASSES}
    enabled_by_class = {k: 0 for k in CLASSES}
    calls_by_class = {k: 0 for k in CLASSES}
    origins = {}
    if isinstance(by_origin, dict):
        for origin, bucket in by_origin.items():
            cls = origin_class(origin)
            b = bucket if isinstance(bucket, dict) else {}
            e = _int(b.get("empty"))
            en = _int(b.get("enabled"))
            empty_by_class[cls] += e
            enabled_by_class[cls] += en
            calls_by_class[cls] += _int(b.get("calls"))
            origins[str(origin)] = {"class": cls, "calls": _int(b.get("calls")),
                                    "enabled": en, "empty": e,
                                    "empty_rate": (round(e / en, 4) if en else None)}
    # 兼容：by_origin 缺席（历史累计量）时，至少别把混合数冒充成生产数
    legacy = c.get("empty_total")
    blended = _int(legacy, sum(empty_by_class.values()))
    enabled_all = _int(c.get("enabled"), sum(enabled_by_class.values()))
    prod_rate = (round(empty_by_class["prod"] / enabled_by_class["prod"], 4)
                 if enabled_by_class["prod"] else None)
    return {
        "empty_total": blended,
        "empty_by_class": empty_by_class,
        "enabled_by_class": enabled_by_class,
        "calls_by_class": calls_by_class,
        "empty_rate_prod": prod_rate,
        "empty_rate_all": (round(blended / enabled_all, 4) if enabled_all else None),
        "by_origin": origins,
        "criterion": {"applies_to": "prod", "must_be": 0,
                      "pass": empty_by_class["prod"] == 0,
                      "note": ("生产空率的对象只有 `dsh-plugin`；探针与 unknown 单列，"
                               "不再污染这条判据（T12）")},
        "legacy_note": ("`empty_total` 为混合口径（探针 + 生产），保留只为旧读侧兼容；"
                        "判据请读 `empty_by_class.prod` / `empty_rate_prod`"),
    }


def ledger_origin_filter(origins: Iterable[Any], skip: Iterable[Any] = (),
                         only_class: str = "") -> Dict[str, Any]:
    """规范化投递账本的来源过滤条件（读侧用；纯函数，便于单测）。

    · `skip`  —— 显式排除的来源标签（精确匹配）；
    · `only_class` —— 只保留某一类（prod/probe/unknown，见 `origin_class`）。
    """
    return {"skip_origins": sorted({str(s) for s in (skip or ()) if str(s)}),
            "only_class": str(only_class or ""),
            "classes": list(CLASSES)}

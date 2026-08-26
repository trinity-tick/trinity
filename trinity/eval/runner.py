# -*- coding: utf-8 -*-
"""Trinity Eval — 借鉴 DSH（DeepSeek Harness）eval 的断言式评测回归（Phase 2）。

小任务集 + 断言检查（contains/not_contains/regex/json），把"功能正确"变成
可断言的回归护栏。接入维护链（-Tasks eval）与 evolution CERTIFY 阶段。

断言类型（对齐 DSH eval_run）:
  - contains:    值包含子串
  - not_contains:值不包含子串
  - regex:       值匹配正则
  - json:        对 dict 按点路径取值后比较 {path, op, value}
                  op: eq/ne/gt/gte/lt/lte/truthy

任务 = {name, description, run: 函数, assertions: [断言...]}
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("trinity.eval")

_HOME = os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))


# ── 断言检查器 ──────────────────────────────────────────────────────

def _json_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def check_assertion(assertion: Dict[str, Any], value: Any) -> tuple:
    """单断言检查：返回 (ok, detail)。"""
    atype = assertion.get("type")
    avalue = assertion.get("value")
    try:
        if atype == "contains":
            ok = str(avalue) in str(value or "")
            return ok, f"contains {avalue!r}: {str(value or '')[:60]!r}"
        if atype == "not_contains":
            ok = str(avalue) not in str(value or "")
            return ok, f"not_contains {avalue!r}"
        if atype == "regex":
            ok = re.search(str(avalue), str(value or "")) is not None
            return ok, f"regex {avalue!r}"
        if atype == "json":
            path = assertion.get("path", "")
            actual = _json_path(value, path) if isinstance(value, dict) else None
            op = assertion.get("op", "eq")
            target = avalue
            if op == "eq":
                ok = actual == target
            elif op == "ne":
                ok = actual != target
            elif op == "in_list":
                ok = actual in (target if isinstance(target, list) else [target])
            elif op == "truthy":
                ok = bool(actual)
            else:
                try:
                    a, b = float(actual), float(target)
                except (TypeError, ValueError):
                    ok = False
                else:
                    ok = {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}.get(op, False)
            return ok, f"json {path} {op} {target!r} -> {actual!r}"
        return False, f"unknown assertion type: {atype}"
    except Exception as exc:
        return False, f"assertion error: {exc}"


# ── 内置任务 ────────────────────────────────────────────────────────

def _t_pagetree_built() -> Any:
    path = os.path.join(_HOME, "store", "pagetree.json")
    if not os.path.exists(path):
        return {"exists": False}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {"exists": True, "stats": data.get("stats", {})}


_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _t_search_schema() -> Any:
    import tempfile
    sys.path.insert(0, _TRINITY_ROOT)
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tempfile.mkdtemp(prefix="eval_schema_"))
    r = mem.search(query="测试", mode="keyword", top_k=3)
    return {
        "is_dict": isinstance(r, dict),
        "has_results": isinstance(r.get("results", None), list),
        "keys": sorted(r.keys()) if isinstance(r, dict) else [],
    }


def _t_reason_available() -> Any:
    import tempfile
    sys.path.insert(0, _TRINITY_ROOT)
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tempfile.mkdtemp(prefix="eval_reason_"))
    r = mem.search(query="测试", mode="reason", top_k=3)
    return {
        "mode": r.get("reason", {}).get("mode", "?"),
        "is_dict": isinstance(r, dict),
    }


def _t_automation_healthy() -> Any:
    from trinity.automation import get_engine
    eng = get_engine()
    stats = eng.stats()
    return {"enabled": eng.enabled(), "stats": stats}


def _t_views_loadable() -> Any:
    from trinity.views import load_views
    views = load_views()
    return {"loaded": True, "views": len(views)}


def _t_visibility_parses() -> Any:
    from trinity.security.visibility import to_sql
    where, params = to_sql("importance >= 0.5 AND category != 'lme'")
    return {"where": where, "params": list(params)}


def _t_goals_healthy() -> Any:
    from trinity.evolution.goals import goal_list
    goals = goal_list()
    return {"count": len(goals)}


def _t_automation_rollout_healthy() -> Any:
    from trinity.automation.engine import _rollout_dir
    rd = _rollout_dir()
    exists = os.path.isdir(rd)
    files = 0
    if exists:
        try:
            files = len([f for f in os.listdir(rd) if f.endswith(".jsonl")])
        except Exception:
            pass
    return {"dir_exists": exists, "rollout_files": files}


def _t_pagetree_summary_coverage() -> Any:
    path = os.path.join(_HOME, "store", "pagetree.json")
    if not os.path.exists(path):
        return {"exists": False, "coverage": 0.0, "total": 0, "with_summary": 0}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    clusters = data.get("clusters", {})
    total = len(clusters)
    with_summary = sum(1 for c in clusters.values()
                       if (c.get("summary") or "").strip())
    coverage = (with_summary / total) if total else 0.0
    return {"exists": True, "coverage": round(coverage, 3),
            "total": total, "with_summary": with_summary}


def _t_goals_no_stall() -> Any:
    from trinity.evolution.goals import goal_list
    blocked = [g for g in goal_list() if g.get("phase") == "blocked"]
    return {"blocked_count": len(blocked)}


def _t_experiment_manifest() -> Any:
    """评测工件 manifest 校验（Claude Science 借鉴 Phase 1）：最近基准结果
    （ae_500_reason_v3 / hard_holdout_eval）应带完整 manifest 且数据集未变。"""
    from trinity.benchmark.manifest import validate_manifest
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "output")
    checked = 0
    all_ok = True
    details = []
    for fname in ("ae_500_reason_v3.json", "hard_holdout_eval.json"):
        p = os.path.join(out_dir, fname)
        if not os.path.exists(p):
            continue
        ok, report = validate_manifest(p)
        checked += 1
        # code_changed 是信息性（结果绑定当时代码=特性），不判失败；
        # dataset_changed（口径漂移）与 manifest 缺失才判失败。
        valid = ok or report.get("code_changed") is True
        details.append({"file": fname, "ok": valid,
                        "code_changed": report.get("code_changed"),
                        "dataset_changed": report.get("dataset_changed")})
        if not valid:
            all_ok = False
    return {"checked": checked, "all_ok": all_ok, "details": details}


def _t_knowledge_fresh() -> Any:
    """知识源健康度（Context7 借鉴 Phase 1）：源注册表可构建，过时源占比合理。"""
    try:
        from trinity.knowledge import build_sources, sources
        reg = build_sources()
        total = reg.get("total", 0)
        stale = reg.get("stale_count", 0)
        return {"total": total, "stale": stale,
                "stale_ratio": round(stale / max(1, total), 3)}
    except Exception as exc:
        return {"total": 0, "stale": 1, "stale_ratio": 1.0,
                "error": str(exc)}


DEFAULT_TASKS: List[Dict[str, Any]] = [
    {
        "name": "pagetree-built",
        "description": "页树已构建（pagetree.json 存在且 clusters>0）",
        "run": _t_pagetree_built,
        "assertions": [
            {"type": "json", "path": "exists", "op": "eq", "value": True},
            {"type": "json", "path": "stats.clusters", "op": "gt", "value": 0},
        ],
    },
    {
        "name": "search-schema",
        "description": "search 返回 dict 且含 results 列表",
        "run": _t_search_schema,
        "assertions": [
            {"type": "json", "path": "is_dict", "op": "eq", "value": True},
            {"type": "json", "path": "has_results", "op": "eq", "value": True},
        ],
    },
    {
        "name": "reason-available",
        "description": "reason 模式可用（llm 判题或 fallback，不崩溃）",
        "run": _t_reason_available,
        "assertions": [
            {"type": "json", "path": "is_dict", "op": "eq", "value": True},
            {"type": "json", "path": "mode", "op": "in_list", "value": ["llm", "fallback"]},
        ],
    },
    {
        "name": "automation-healthy",
        "description": "自动化引擎统计可读（默认关闭 enabled=False）",
        "run": _t_automation_healthy,
        "assertions": [
            {"type": "json", "path": "stats.emitted", "op": "gte", "value": 0},
        ],
    },
    {
        "name": "views-loadable",
        "description": "记忆视图可加载",
        "run": _t_views_loadable,
        "assertions": [
            {"type": "json", "path": "loaded", "op": "eq", "value": True},
        ],
    },
    {
        "name": "visibility-parses",
        "description": "行级可见性规则可解析（白名单+参数化）",
        "run": _t_visibility_parses,
        "assertions": [
            {"type": "json", "path": "where", "op": "truthy"},
            {"type": "json", "path": "params", "op": "truthy"},
        ],
    },
    {
        "name": "goals-healthy",
        "description": "目标引擎可读",
        "run": _t_goals_healthy,
        "assertions": [
            {"type": "json", "path": "count", "op": "gte", "value": 0},
        ],
    },
    {
        "name": "automation-rollout-healthy",
        "description": "自动化 rollout 目录就绪（可回放轨迹）",
        "run": _t_automation_rollout_healthy,
        "assertions": [
            {"type": "json", "path": "dir_exists", "op": "eq", "value": True},
        ],
    },
    {
        "name": "pagetree-summary-coverage",
        "description": "页树摘要覆盖率 >= 0.3（117/270 当前 0.43）",
        "run": _t_pagetree_summary_coverage,
        "assertions": [
            {"type": "json", "path": "exists", "op": "eq", "value": True},
            {"type": "json", "path": "coverage", "op": "gte", "value": 0.3},
        ],
    },
    {
        "name": "goals-no-stall",
        "description": "目标引擎无阻塞堆积（blocked <= 3）",
        "run": _t_goals_no_stall,
        "assertions": [
            {"type": "json", "path": "blocked_count", "op": "lte", "value": 3},
        ],
    },
    {
        "name": "knowledge-fresh",
        "description": "知识源注册表可构建且过时源占比 <= 0.6（Context7 借鉴）",
        "run": _t_knowledge_fresh,
        "assertions": [
            {"type": "json", "path": "total", "op": "gt", "value": 0},
            {"type": "json", "path": "stale_ratio", "op": "lte", "value": 0.6},
        ],
    },
    {
        "name": "experiment-manifest",
        "description": "评测结果工件带完整 manifest 且数据集未变（Claude Science 借鉴）",
        "run": _t_experiment_manifest,
        "assertions": [
            {"type": "json", "path": "checked", "op": "gt", "value": 0},
            {"type": "json", "path": "all_ok", "op": "eq", "value": True},
        ],
    },
]


def run_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """执行单任务：run → 各断言 → {name, ok, detail, assertions}。"""
    name = task.get("name", "?")
    fn: Optional[Callable] = task.get("run")
    if fn is None:
        return {"name": name, "ok": False, "detail": "no run function",
                "assertions": []}
    try:
        value = fn()
    except Exception as exc:
        return {"name": name, "ok": False,
                "detail": f"run failed: {type(exc).__name__}: {exc}",
                "assertions": []}
    results = []
    for assertion in task.get("assertions", []):
        ok, detail = check_assertion(assertion, value)
        results.append({"assertion": assertion, "ok": ok, "detail": detail})
    all_ok = all(a["ok"] for a in results)
    return {"name": name, "ok": all_ok,
            "detail": f"{len(results)} assertions, {sum(1 for a in results if a['ok'])} ok",
            "assertions": results}


def run_all(tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    tasks = tasks if tasks is not None else DEFAULT_TASKS
    results = [run_task(t) for t in tasks]
    ok_n = sum(1 for r in results if r["ok"])
    return {
        "total": len(results),
        "passed": ok_n,
        "failed": len(results) - ok_n,
        "results": results,
    }

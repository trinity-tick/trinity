# -*- coding: utf-8 -*-
"""Trinity Goal Engine — 借鉴 DSH（DeepSeek Harness）goal 机制的目标驱动自进化（Phase 1）。

DSH 借鉴（2026-08-26）：持久化 Goal 对象（objective/phase/轮次/阻塞原因/验收指标）
+ 自动续轮 + 完成/阻塞上报语义。让 Trinity 自进化从"周期漫游"升级为"目标驱动"。

Goal 对象字段:
  - goal_id:        唯一 ID（g_<ts>_<rand>）
  - objective:      目标描述（一句话）
  - phase:          active | paused | blocked | complete
  - rounds:         已执行轮次（评估次数）
  - max_rounds:     轮次上限（超限自动 blocked）
  - acceptance:     验收指标 {metric, op, value}（如 {metric: answer_acc, op: gte, value: 0.75}）
  - last_metric:    最近一次指标值（无进展检测）
  - blocked_reason: 阻塞原因
  - created_at / updated_at

用法:
    from trinity.evolution.goals import goal_create, goal_update, goal_get, goal_list, evaluate_goals
    g = goal_create("全量 500q AnswerAcc >= 0.75", acceptance={"metric": "answer_acc", "op": "gte", "value": 0.75})
    evaluate_goals({"answer_acc": 0.752})   # 达标 → complete
    goal_update(g["goal_id"], "blocked", blocked_reason="...")

持久化: ~/.trinity/goals.json（线程安全，原子写）。
与 automation 集成：状态变化 emit("goal.updated")（规则可响应）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trinity.goals")

_HOME = os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))
_GOALS_FILE = os.path.join(_HOME, "goals.json")

# 2026-08-26（实测抓出）：必须用 RLock——goal_create/update/evaluate 外层持锁，
# 内部 _load()/_save() 又 acquire 同一锁，普通 Lock 同一线程重入即死锁
# （症状：进程静默 exit 1，无 traceback——被超时杀）。
_LOCK = threading.RLock()
_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_MTIME = 0.0

_ACCEPTANCE_OPS = {
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "eq": lambda a, b: a == b,
}

# 连续无进展轮数阈值（默认 3 轮判定 blocked）
STALL_ROUNDS = 3


def _goals_file() -> str:
    return os.path.join(os.environ.get("TRINITY_HOME", _HOME), "goals.json")


def _load() -> Dict[str, Dict[str, Any]]:
    global _CACHE, _MTIME
    path = _goals_file()
    try:
        mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    except Exception:
        mtime = 0.0
    if _CACHE is not None and abs(mtime - _MTIME) < 1e-6:
        return _CACHE
    with _LOCK:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _CACHE = data if isinstance(data, dict) else {}
            else:
                _CACHE = {}
            _MTIME = mtime
        except Exception as exc:
            logger.warning("goals load failed: %s", exc)
            _CACHE = _CACHE or {}
        return _CACHE


def _save() -> None:
    path = _goals_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("goals save failed: %s", exc)


def _emit_goal_event(goal: Dict[str, Any]) -> None:
    try:
        from trinity.automation import emit as _automation_emit
        _automation_emit("goal.updated", {
            "goal_id": goal.get("goal_id", ""),
            "status": goal.get("phase", "active"),
            "objective": (goal.get("objective") or "")[:200],
        })
    except Exception:
        pass


def goal_create(
    objective: str,
    acceptance: Optional[Dict[str, Any]] = None,
    max_rounds: int = 10,
) -> Dict[str, Any]:
    """创建目标（active）。"""
    with _LOCK:
        goals = _load()
        gid = f"g_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        goal = {
            "goal_id": gid,
            "objective": str(objective)[:500],
            "phase": "active",
            "rounds": 0,
            "max_rounds": int(max_rounds),
            "acceptance": acceptance or {},
            "last_metric": None,
            "blocked_reason": "",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        goals[gid] = goal
        _save()
    _emit_goal_event(goal)
    logger.info("[goals] created %s: %s", gid, objective[:80])
    return goal


def goal_update(
    goal_id: str,
    action: str = "edit",
    objective: Optional[str] = None,
    acceptance: Optional[Dict[str, Any]] = None,
    max_rounds: Optional[int] = None,
    blocked_reason: str = "",
) -> Optional[Dict[str, Any]]:
    """更新目标状态：edit（改 objective/acceptance/max_rounds）| pause | resume |
    complete | blocked。返回更新后的 goal；不存在返回 None。"""
    with _LOCK:
        goals = _load()
        goal = goals.get(goal_id)
        if goal is None:
            return None
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        if action == "edit":
            if objective is not None:
                goal["objective"] = str(objective)[:500]
            if acceptance is not None:
                goal["acceptance"] = acceptance
            if max_rounds is not None:
                goal["max_rounds"] = int(max_rounds)
        elif action == "pause":
            goal["phase"] = "paused"
        elif action == "resume":
            if goal["phase"] in ("paused", "blocked"):
                goal["phase"] = "active"
                goal["blocked_reason"] = ""
        elif action == "complete":
            goal["phase"] = "complete"
        elif action == "blocked":
            goal["phase"] = "blocked"
            goal["blocked_reason"] = str(blocked_reason)[:500]
        else:
            return None
        goal["updated_at"] = now
        _save()
    _emit_goal_event(goal)
    logger.info("[goals] %s %s (phase=%s)", action, goal_id, goal["phase"])
    return goal


def goal_get(goal_id: str) -> Optional[Dict[str, Any]]:
    return _load().get(goal_id)


def goal_list(phase: Optional[str] = None) -> List[Dict[str, Any]]:
    goals = list(_load().values())
    goals.sort(key=lambda g: g.get("created_at", ""))
    if phase:
        goals = [g for g in goals if g.get("phase") == phase]
    return goals


def evaluate_goals(metrics: Dict[str, Any], stall_rounds: int = STALL_ROUNDS) -> List[Dict[str, Any]]:
    """用指标字典评估全部 active 目标（DSH 自动续轮语义）：

    - 达标（acceptance 满足）→ complete
    - 未达标 → rounds+1，记录 last_metric；连续 stall_rounds 轮无进展 → blocked（带原因）
    - rounds 超 max_rounds → blocked（"达到轮次上限"）
    返回被更新的目标列表。
    """
    changed = []
    with _LOCK:
        goals = _load()
        for gid, goal in goals.items():
            if goal.get("phase") != "active":
                continue
            acc = goal.get("acceptance") or {}
            metric_name = acc.get("metric")
            if not metric_name or metric_name not in metrics:
                continue  # 指标不可得 → 本轮跳过（不计数）
            value = metrics[metric_name]
            op = acc.get("op", "gte")
            target = acc.get("value")
            goal["rounds"] = int(goal.get("rounds", 0)) + 1
            prev = goal.get("last_metric")
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            fn = _ACCEPTANCE_OPS.get(op)
            if fn is not None and fn(value, target):
                goal["phase"] = "complete"
                goal["last_metric"] = value
                goal["blocked_reason"] = ""
                goal["updated_at"] = now
                changed.append(goal)
                logger.info("[goals] %s COMPLETE (metric %s=%s %s %s)", gid,
                            metric_name, value, op, target)
                continue
            # 无进展检测：连续 stall_rounds 轮 last_metric 无改善
            stalled = 0
            if prev is not None and isinstance(value, (int, float)) and isinstance(prev, (int, float)):
                stalled = int(goal.get("_stall", 0))
                improved = (value > prev) if op in ("gte", "gt") else (value < prev)
                stalled = 0 if improved else stalled + 1
            else:
                stalled = int(goal.get("_stall", 0)) + 1
            goal["_stall"] = stalled
            goal["last_metric"] = value
            goal["updated_at"] = now
            if stalled >= stall_rounds:
                goal["phase"] = "blocked"
                goal["blocked_reason"] = (
                    f"连续 {stall_rounds} 轮无进展: {metric_name}={value}（目标 {op} {target}）")
                changed.append(goal)
                logger.info("[goals] %s BLOCKED: %s", gid, goal["blocked_reason"])
            elif int(goal.get("rounds", 0)) >= int(goal.get("max_rounds", 10)):
                goal["phase"] = "blocked"
                goal["blocked_reason"] = f"达到轮次上限 {goal.get('max_rounds')}"
                changed.append(goal)
                logger.info("[goals] %s BLOCKED: max rounds", gid)
            else:
                changed.append(goal)
        _save()
    for goal in changed:
        _emit_goal_event(goal)
    return changed


# ── 示例目标（文档/seed 用，不默认创建）─────────────────────────────

def sample_goals() -> List[Dict[str, Any]]:
    return [
        {
            "objective": "全量 500q AnswerAcc >= 0.75（reason 模式，output/ae_500_reason_v3.json）",
            "acceptance": {"metric": "answer_acc", "op": "gte", "value": 0.75},
            "max_rounds": 10,
        },
        {
            "objective": "生产难查询 holdout reason R@10 >= 0.55（output/hard_holdout.json）",
            "acceptance": {"metric": "holdout_reason_r10", "op": "gte", "value": 0.55},
            "max_rounds": 10,
        },
    ]


def _manifest_ok(path: str) -> bool:
    """2026-08-26（Claude Science 借鉴 建议执行）：读取前校验 manifest——
    数据集已变（口径漂移）→ 硬拦截跳过；code_changed 不阻断（旧结果绑定当时代码
    是特性）；manifest 缺失向后兼容（旧结果照读，仅告警记录）。"""
    try:
        from trinity.benchmark.manifest import validate_manifest
        if not os.path.exists(path + ".manifest.json"):
            logger.warning("default_metrics: no manifest for %s (result not reproducible)", os.path.basename(path))
            return True
        ok, report = validate_manifest(path)
        if report.get("dataset_changed"):
            logger.warning("default_metrics: dataset changed for %s — skip (口径漂移)", os.path.basename(path))
            return False
        return True
    except Exception:
        return True


def default_metrics() -> Dict[str, Any]:
    """从 output/*.json 读最近基准结果作为指标（best-effort）。

    2026-08-26（Claude Science 借鉴）：读取前校验 manifest——数据集漂移跳过。
    """
    metrics: Dict[str, Any] = {}
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "output")
    candidates = [
        ("ae_500_reason_v3.json", "AnswerAcc", "answer_acc"),
        ("ae_500_reason_v3.json", "R@5", "r5_reason_500q"),
        ("ae_500_base.json", "R@5", "r5_base_500q"),
    ]
    for fname, key, metric in candidates:
        try:
            path = os.path.join(out_dir, fname)
            if os.path.exists(path) and _manifest_ok(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if key in data:
                    metrics[metric] = float(data[key])
        except Exception:
            continue
    # 2026-08-26（建议执行）：holdout reason R@10（hard_holdout_eval_v3.json）
    for fname in ("hard_holdout_eval_v3.json", "hard_holdout_eval.json"):
        try:
            path = os.path.join(out_dir, fname)
            if os.path.exists(path) and _manifest_ok(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                arms = data.get("arms") or {}
                r10 = arms.get("reason", {}).get("r10")
                if r10 is not None:
                    metrics["holdout_reason_r10"] = float(r10)
                    break
        except Exception:
            continue
    # 2026-08-26（下一步建议）：MS 类目 AnswerAcc（ae_500_reason_v4 起有 by_category）
    for fname in ("ae_500_reason_v4.json", "ae_500_reason_v5.json", "ae_500_reason_v3.json"):
        try:
            path = os.path.join(out_dir, fname)
            if os.path.exists(path) and _manifest_ok(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ms = (data.get("by_category") or {}).get("MS") or {}
                acc = ms.get("AnswerAcc")
                if acc is not None:
                    metrics["ms_answer_acc"] = float(acc)
                    break
        except Exception:
            continue
    # 2026-08-27（方向1 通用化第一步）：系统健康综合指标——非记忆指标首次进入
    # 进化引擎（ps1 三件套 + WAL/integrity + 备份新鲜 + API 健康，四项均值）。
    try:
        import subprocess as _sp
        import glob as _gl
        import urllib.request as _ur
        import sys as _sys
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _pts = []
        # a) ps1 三件套
        _ra = _sp.run([_sys.executable, "-X", "utf8",
                       os.path.join(_root, "scripts", "audit_maintenance_ps1.py")],
                      capture_output=True, text=True, timeout=30)
        _pts.append(1.0 if "ALL OK" in _ra.stdout else 0.0)
        # b) WAL / integrity
        _rb = _sp.run([_sys.executable, "-X", "utf8",
                       os.path.join(_root, "scripts", "db_health.py")],
                      capture_output=True, text=True, timeout=60)
        _pts.append(1.0 if ("log=0" in _rb.stdout and "integrity=ok" in _rb.stdout) else 0.0)
        # c) 备份新鲜（<24h）
        _bf = _gl.glob(os.path.join(os.path.expanduser("~/.trinity/backups"), "trinity_store_*.db"))
        _fresh = 0.0
        if _bf:
            _latest = max(os.path.getmtime(p) for p in _bf)
            _fresh = 1.0 if (time.time() - _latest) < 86400 else 0.0
        _pts.append(_fresh)
        # d) API 健康
        try:
            with _ur.urlopen("http://127.0.0.1:8001/health", timeout=5) as _resp:
                _h = json.loads(_resp.read().decode("utf-8"))
                _pts.append(1.0 if _h.get("status") == "ok" else 0.0)
        except Exception:
            _pts.append(0.0)
        metrics["system_health"] = round(sum(_pts) / len(_pts), 3)
        metrics["system_health_parts"] = {
            "audit_ps1": _pts[0], "wal_integrity": _pts[1],
            "backup_fresh": _pts[2], "api_ok": _pts[3],
        }
    except Exception:
        pass
    return metrics

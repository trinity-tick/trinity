# -*- coding: utf-8 -*-
"""Trinity Automation Engine — 借鉴 Budibase Automations 的声明式事件驱动规则层。

Budibase 借鉴（2026-08-26）：触发器(trigger) → 条件(condition) → 动作链(actions)，
YAML 声明式规则、可审计、可回滚。**默认关闭**（TRINITY_AUTOMATION=on 显式启用）。

事件（Phase 1）:
  - memory.write    ingest 成功后（payload: memory_id/importance/category/tags/...）
  - memory.search   search 后（payload: query/top_k/mode/hit_count/top_score）
  - goal.updated    structure_store.goal_upsert 状态变化

动作（Phase 1）:
  - notify          写日志（+审计，经 audit_fn）
  - exec.python     调用 python 函数（module:function + args）
  - exec.command    子进程命令（超时保护）

规则文件: ~/.trinity/automation/rules.yaml（用户规则与内置 DEFAULT_RULES 合并，同名覆盖）。
示例::

    rules:
      - name: high-importance-summary
        trigger: memory.write
        condition: {field: importance, op: gte, value: 0.85}
        actions:
          - {type: notify, message: "high-importance memory written: {memory_id}"}

安全：动作失败不影响主流程；每规则每分钟限流；审计留痕；动作后台线程执行。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("trinity.automation")

_ENABLED_ENV = "TRINITY_AUTOMATION"
_HOME = os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))
_RULES_FILE = os.path.join(_HOME, "automation", "rules.yaml")
_STATS_FILE = os.path.join(_HOME, "automation", "stats.json")

DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "name": "write-high-importance-notify",
        "enabled": True,
        "trigger": "memory.write",
        "condition": {"field": "importance", "op": "gte", "value": 0.8},
        "actions": [
            {"type": "notify",
             "message": "[automation] high-importance memory written: {memory_id} (imp={importance}, cat={category})"},
        ],
    },
    {
        "name": "search-low-confidence-flag",
        "enabled": True,
        "trigger": "memory.search",
        "condition": {"field": "top_score", "op": "lt", "value": 0.25},
        "actions": [
            {"type": "notify",
             "message": "[automation] low-confidence search (top_score={top_score}): {query}"},
        ],
    },
    # ── 真实维护动作（2026-08-26 二轮，exec.command；cooldown 防抖）──
    # 低置信检索 → 重建页树（只读脚本，安全；防循环 env 已内置）
    {
        "name": "search-low-confidence-pagetree-refresh",
        "enabled": True,
        "trigger": "memory.search",
        "condition": {"field": "top_score", "op": "lt", "value": 0.2},
        "cooldown_seconds": 3600,
        "actions": [
            {"type": "exec",
             "command": ["{python}", r"C:\Users\Administrator\trinity\scripts\build_memory_pagetree.py"],
             "message": "[automation] low-confidence search → pagetree refresh"},
        ],
    },
    # 高 importance 写入 → Mem0 式记忆操作（写路径；有 SQLite 写锁并发风险，
    # 默认关闭；需要时在 rules.yaml 设 enabled: true，建议配 -LeaseJob 场景）
    {
        "name": "write-high-importance-consolidate",
        "enabled": False,
        "trigger": "memory.write",
        "condition": {"field": "importance", "op": "gte", "value": 0.85},
        "cooldown_seconds": 1800,
        "actions": [
            {"type": "exec",
             "command": ["{python}", r"C:\Users\Administrator\trinity\scripts\memory_ops.py",
                         "--hours", "24", "--limit", "20"],
             "message": "[automation] high-importance write → memory_ops consolidate"},
        ],
    },
    # goal 完成 → 会话自动总结（会写记忆，防循环 env 已内置；默认关闭）
    {
        "name": "goal-completed-summary",
        "enabled": False,
        "trigger": "goal.updated",
        "condition": {"field": "status", "op": "eq", "value": "complete"},
        "cooldown_seconds": 300,
        "actions": [
            {"type": "exec",
             "command": ["{python}", r"C:\Users\Administrator\trinity\scripts\auto_session_summary.py"],
             "message": "[automation] goal completed → session summary"},
        ],
    },
]

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) > _num(b),
    "gte": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "lte": lambda a, b: _num(a) <= _num(b),
    "contains": lambda a, b: b in (a or ""),
    "in": lambda a, b: a in (b or []),
    "not_in": lambda a, b: a not in (b or []),
}

# ── 执行策略层（2026-08-26 Codex 借鉴 Phase 1）──────────────────────
# 借鉴 Codex 的 sandbox_mode（read-only/auto/full）与 approval_policy
# （never/on-failure/always）的**策略模型**（Windows 无 seccomp/landlock，
# 用命令白名单 + 审批队列替代技术沙箱）。
#  - mode: read-only（只读脚本白名单）| auto（已知脚本白名单，默认）| full（任意命令，显式配置）
#  - approval: never（默认，直接执行）| on-failure（失败入审批队列）| always（先入队等审批）
MODE_READONLY = "read-only"
MODE_AUTO = "auto"
MODE_FULL = "full"
APPROVAL_NEVER = "never"
APPROVAL_ON_FAILURE = "on-failure"
APPROVAL_ALWAYS = "always"

# 只读脚本（read-only 模式可执行；全部为幂等/只读维护脚本）
READONLY_SCRIPTS = {
    "build_memory_pagetree.py", "db_health.py", "active_set_health.py",
    "slo_report.py", "rollout_inspect.py", "consistency_check.py",
}
# 已知维护脚本（auto 模式可执行）
KNOWN_SCRIPTS = READONLY_SCRIPTS | {
    "run_pagetree_summaries.py", "memory_ops.py", "auto_session_summary.py",
    "consolidate_temporal.py", "compact_structure.py", "entity_dedup.py",
    "cleanup_noise.py", "export_memories_markdown.py", "run_decay_compress.py",
    "harvest_kb_structured.py",  # 2026-08-27（stale 自动采集闭环）
    "rollout_audit.py",          # 2026-08-27（编排升级）
}
_PENDING_FILE = os.path.join(_HOME, "automation", "pending.json")
_ROLLOUT_DIR = os.path.join(_HOME, "automation", "rollouts")


def _pending_file() -> str:
    """pending 队列路径（运行时解析 TRINITY_HOME，测试可切换）。"""
    return os.path.join(os.environ.get("TRINITY_HOME", _HOME), "automation", "pending.json")


def _rollout_dir() -> str:
    """rollout 目录（运行时解析 TRINITY_HOME，测试可切换）。"""
    return os.path.join(os.environ.get("TRINITY_HOME", _HOME), "automation", "rollouts")


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _match_condition(cond: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> bool:
    """条件表达式：{field, op, value}；无 condition = 恒真。"""
    if not cond:
        return True
    field = cond.get("field")
    op = cond.get("op", "eq")
    value = cond.get("value")
    if field is None:
        return True
    actual = payload.get(field)
    fn = _OPS.get(op)
    if fn is None:
        return False
    try:
        return bool(fn(actual, value))
    except Exception:
        return False


class AutomationEngine:
    """事件总线 + 规则匹配 + 动作执行（线程安全单例）。"""

    def __init__(self) -> None:
        self._rules: List[Dict[str, Any]] = []
        self._rules_mtime = 0.0
        self._lock = threading.Lock()
        self._rate: Dict[str, List[float]] = {}   # rule_name -> emit timestamps
        self._cooldown: Dict[str, float] = {}     # rule_name -> last run ts（动作防抖）
        self._stats: Dict[str, int] = {"emitted": 0, "matched": 0, "executed": 0, "failed": 0}
        self._pending: List[Dict[str, Any]] = []  # 审批队列（approval always/on-failure）
        self._load_rules()
        self._load_pending()

    # ── 门控 ────────────────────────────────────────────────────────

    def enabled(self) -> bool:
        val = os.environ.get(_ENABLED_ENV, "off").strip().lower()
        return val in ("1", "on", "true", "yes")

    # ── 规则加载（默认 + 用户 YAML，同名覆盖）──────────────────────

    def _load_rules(self) -> None:
        merged: Dict[str, Dict[str, Any]] = {r["name"]: dict(r) for r in DEFAULT_RULES}
        try:
            if os.path.exists(_RULES_FILE):
                import yaml
                with open(_RULES_FILE, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                for r in data.get("rules", []):
                    if isinstance(r, dict) and r.get("name"):
                        merged[r["name"]] = dict(r)
        except Exception as exc:
            logger.warning("automation rules load failed: %s", exc)
        self._rules = list(merged.values())
        if os.path.exists(_RULES_FILE):
            try:
                self._rules_mtime = os.path.getmtime(_RULES_FILE)
            except Exception:
                pass

    def _reload_if_changed(self) -> None:
        try:
            if os.path.exists(_RULES_FILE) and os.path.getmtime(_RULES_FILE) != self._rules_mtime:
                with self._lock:
                    self._load_rules()
        except Exception:
            pass

    # ── 事件入口 ────────────────────────────────────────────────────

    def emit(
        self,
        event: str,
        payload: Dict[str, Any],
        audit_fn: Optional[Callable[[str, str, bool, Dict[str, Any]], None]] = None,
    ) -> int:
        """发布事件；返回匹配并执行的动作数。默认关闭时零开销返回 0。"""
        if not self.enabled():
            return 0
        self._reload_if_changed()
        matched = 0
        with self._lock:
            self._stats["emitted"] += 1
        for rule in self._rules:
            if not rule.get("enabled", True):
                continue
            if rule.get("trigger") != event:
                continue
            if not _match_condition(rule.get("condition"), payload):
                continue
            key = rule["name"]
            now = time.time()
            with self._lock:
                # cooldown（2026-08-26 二轮）：规则级动作防抖——cooldown 秒内
                # 只执行一次（如页树重建 3600s 防抖，避免低置信刷屏反复重建）
                cd = float(rule.get("cooldown_seconds") or 0)
                last = self._cooldown.get(key, 0.0)
                if cd > 0 and (now - last) < cd:
                    continue
                # 限流：每规则每事件类型 10 次/分钟
                window = [t for t in self._rate.get(key, []) if now - t < 60]
                if len(window) >= 10:
                    continue
                window.append(now)
                self._rate[key] = window
                self._cooldown[key] = now
                self._stats["matched"] += 1
            threading.Thread(
                target=self._run_rule, args=(rule, payload, audit_fn),
                daemon=True, name="automation-" + rule["name"],
            ).start()
            matched += 1
        return matched

    def _run_rule(self, rule: Dict[str, Any], payload: Dict[str, Any],
                  audit_fn: Optional[Callable]) -> None:
        ok = True
        try:
            # 2026-08-27（编排升级）：多步动作链——if 分支 + delay 间隔 +
            # continue_on_error（失败不中断链）
            for action in rule.get("actions", []):
                _aname = action.get("name") or rule.get("name", "?")
                _cond = action.get("if")
                if _cond and not _match_condition(_cond, payload):
                    logger.info("[automation] action skipped (if): %s", _aname)
                    continue
                if not self._run_action(action, payload, rule.get("name", "?")):
                    ok = False
                    if not action.get("continue_on_error"):
                        break
                    logger.warning("[automation] action failed but continue: %s", _aname)
                _delay = action.get("delay")
                if _delay:
                    time.sleep(min(float(_delay), 60.0))
        except Exception as exc:
            ok = False
            logger.warning("[automation] rule %s failed: %s", rule["name"], exc)
        if not ok:
            # 2026-08-27（rollout 异常规则）：动作失败 → automation.failed 事件（可被告警规则响应）
            try:
                from trinity.automation import emit as _reemit
                _reemit("automation.failed", {
                    "rule": rule.get("name", "?"),
                    "trigger": payload.get("_event", ""),
                    "error": str(exc) if "exc" in dir() else "action returned False",
                })
            except Exception:
                pass
        with self._lock:
            self._stats["executed" if ok else "failed"] += 1
            self._stats["failed"] = self._stats.get("failed", 0)  # keep key stable
            self._persist_stats_locked()
        if audit_fn:
            try:
                audit_fn(rule["name"], ok, {"event": payload.get("_event", ""),
                                            "detail": str(payload.get("memory_id") or payload.get("query") or "")[:120]})
            except Exception:
                pass

    def _run_action(self, action: Dict[str, Any], payload: Dict[str, Any],
                     rule_name: str = "?") -> bool:
        atype = action.get("type", "notify")
        # 2026-08-27（编排升级）：动作支持 retries 重试（失败退避 2^attempt 秒）
        retries = int(action.get("retries", 0) or 0)
        for attempt in range(retries + 1):
            try:
                if action.get("message"):
                    logger.info("[automation] %s", str(action["message"]).format(**payload))
                if atype == "notify":
                    msg = (action.get("message") or "").format(**payload)
                    logger.info("%s", msg)
                    return True
                if atype == "exec":
                    if "python" in action:
                        ok = self._exec_python(action["python"], action.get("args") or {}, payload)
                    elif "command" in action:
                        ok = self._exec_command_policy(action, payload, rule_name)
                    else:
                        ok = False
                    if ok or attempt >= retries:
                        return ok
                    time.sleep(2 ** attempt)  # 指数退避
                    logger.warning("[automation] action %s retry %d", rule_name, attempt + 1)
                    continue
                logger.warning("[automation] unknown action type: %s", atype)
                return False
            except Exception as exc:
                if attempt >= retries:
                    logger.warning("[automation] action failed: %s", exc)
                    return False
                time.sleep(2 ** attempt)
                logger.warning("[automation] action %s exception retry %d", rule_name, attempt + 1)
        return False

    def _exec_command_policy(self, action: Dict[str, Any], payload: Dict[str, Any],
                             rule_name: str) -> bool:
        """exec.command 执行策略（Codex 借鉴 Phase 1）：

        mode read-only/auto/full + approval never/on-failure/always；
        白名单校验失败直接拒绝；审批队列持久化到 pending.json。
        """
        command = action.get("command") or []
        mode = action.get("mode") or MODE_AUTO
        approval = action.get("approval") or APPROVAL_NEVER
        ok, err = self._validate_command(command, mode)
        if not ok:
            logger.warning("[automation] command rejected (%s): %s", rule_name, err)
            return False
        if approval == APPROVAL_ALWAYS:
            self._enqueue_pending(rule_name, action, payload, reason="approval:always")
            return True
        result = self._exec_command(command, rule_name=rule_name)
        if not result and approval == APPROVAL_ON_FAILURE:
            self._enqueue_pending(rule_name, action, payload,
                                  reason="approval:on-failure")
        return result

    def _exec_python(self, target: str, args: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """调用 module:function（args 可用 {field} 占位从 payload 取）。"""
        module_name, _, func_name = target.partition(":")
        if not module_name or not func_name:
            return False
        import importlib
        mod = importlib.import_module(module_name)
        fn = getattr(mod, func_name)
        resolved = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                resolved[k] = payload.get(v[1:-1])
            else:
                resolved[k] = v
        fn(**resolved)
        return True

    # （旧版 _exec_command 已由带 rollout 记录的新版取代，2026-08-26 Phase 2）

    # ── 执行策略（Codex 借鉴 Phase 1）──────────────────────────────

    def _validate_command(self, command: List[str], mode: str) -> tuple:
        """命令白名单校验：返回 (ok, error)。

        - read-only：只读脚本白名单
        - auto：已知维护脚本白名单（默认）
        - full：任意命令（显式配置才允许）
        """
        if mode == MODE_FULL:
            return True, ""
        if not command:
            return False, "empty command"
        if command[0] not in ("{python}", sys.executable):
            return False, f"command[0] must be python interpreter, got {command[0]!r}"
        if len(command) < 2:
            return False, "missing script path"
        script = os.path.basename(str(command[1]).replace("\\", "/")).lower()
        allowed = READONLY_SCRIPTS if mode == MODE_READONLY else KNOWN_SCRIPTS
        if script not in allowed:
            return False, f"script not in whitelist (mode={mode}): {script}"
        return True, ""

    def _load_pending(self) -> None:
        try:
            _pf = _pending_file()
            if os.path.exists(_pf):
                with open(_pf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._pending = [p for p in data if p.get("status") == "pending"]
        except Exception as exc:
            logger.warning("pending load failed: %s", exc)

    def _persist_pending_locked(self) -> None:
        try:
            _pf = _pending_file()
            os.makedirs(os.path.dirname(_pf), exist_ok=True)
            with open(_pf, "w", encoding="utf-8") as f:
                json.dump(self._pending, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _enqueue_pending(self, rule_name: str, action: Dict[str, Any],
                         payload: Dict[str, Any], reason: str) -> str:
        """动作入审批队列（approval always 预入队 / on-failure 失败后入队）。"""
        pid = f"pend_{int(time.time() * 1000)}_{len(self._pending)}"
        item = {
            "pending_id": pid,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "rule": rule_name,
            "action": {k: v for k, v in action.items() if k != "message"},
            "payload_summary": {
                "memory_id": payload.get("memory_id"),
                "query": payload.get("query"),
                "importance": payload.get("importance"),
                "goal_id": payload.get("goal_id"),
                "status": payload.get("status"),
            },
            "reason": reason,
            "status": "pending",
        }
        with self._lock:
            self._pending.append(item)
            self._persist_pending_locked()
        logger.info("[automation] pending approval: %s (rule=%s reason=%s)",
                    pid, rule_name, reason)
        return pid

    # 2026-08-27（编排升级）：审批流状态机——pending 超时自动 expired
    _PENDING_TTL_SECONDS = 86400.0  # 24h

    def _expire_stale_pending(self) -> int:
        """超时 pending -> expired（不可再批准）。返回过期数。"""
        import datetime as _dt
        n = 0
        with self._lock:
            now = _dt.datetime.now()
            for p in self._pending:
                if p.get("status") != "pending":
                    continue
                try:
                    ts = _dt.datetime.strptime(p.get("ts", ""), "%Y-%m-%dT%H:%M:%S")
                    if (now - ts).total_seconds() > self._PENDING_TTL_SECONDS:
                        p["status"] = "expired"
                        p["expired_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
                        n += 1
                except Exception:
                    continue
            if n:
                self._persist_pending_locked()
        if n:
            logger.info("[automation] expired %d pending items", n)
        return n

    def pending_items(self) -> List[Dict[str, Any]]:
        """只返回待审批项（approved/rejected 历史保留在 pending.json）。"""
        with self._lock:
            return [dict(p) for p in self._pending if p.get("status") == "pending"]

    def approve(self, pending_id: str, approve: bool = True) -> bool:
        """审批（状态机 2026-08-27）：pending -> approved/rejected；expired 不可批准。"""
        self._expire_stale_pending()
        item = None
        with self._lock:
            for p in self._pending:
                if p.get("pending_id") == pending_id and p.get("status") == "pending":
                    item = p
                    break
            if item is None or item.get("status") != "pending":
                return False
            item["status"] = "approved" if approve else "rejected"
            self._persist_pending_locked()
        if approve and item:
            # 人工已批准 → 执行时剥离 approval 字段（防重新入队死循环）
            action = {k: v for k, v in (item.get("action") or {}).items()
                      if k != "approval"}
            threading.Thread(
                target=self._run_action, args=(action, {}, item.get("rule", "")),
                daemon=True, name="automation-approve-" + pending_id,
            ).start()
        return True

    # ── Rollout 轨迹（Codex 借鉴 Phase 2）──────────────────────────

    def _record_rollout(self, record: Dict[str, Any]) -> None:
        """动作执行轨迹追加到 ~/.trinity/automation/rollouts/<ts>.jsonl。

        每行一个动作事件：ts/event/rule/action_type/command/ok/exit_code/
        duration_ms/error_tail——可回放调试与统计分析。
        """
        try:
            _rd = _rollout_dir()
            os.makedirs(_rd, exist_ok=True)
            day = time.strftime("%Y%m%d")
            path = os.path.join(_rd, f"{day}.jsonl")
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _exec_command(self, command: List[str], rule_name: str = "?",
                      action_type: str = "exec.command") -> bool:
        t0 = time.time()
        ok = False
        exit_code = None
        err_tail = ""
        try:
            resolved = [
                (sys.executable if c == "{python}" else c) for c in command
            ]
            # 防循环（2026-08-26 二轮）：子进程内的 ingest 不再触发自动化事件
            env = dict(os.environ)
            env["TRINITY_AUTOMATION_ACTION"] = "1"
            proc = subprocess.run(
                resolved, capture_output=True, text=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
            exit_code = proc.returncode
            ok = proc.returncode == 0
            if not ok:
                err_tail = (proc.stderr or "")[-300:]
                logger.warning("[automation] command failed rc=%s: %s",
                               proc.returncode, err_tail)
            return ok
        except subprocess.TimeoutExpired:
            err_tail = "timeout>120s"
            logger.warning("[automation] command timed out")
            return False
        except Exception as exc:
            err_tail = str(exc)[-300:]
            logger.warning("[automation] command error: %s", exc)
            return False
        finally:
            self._record_rollout({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "event": "action",
                "rule": rule_name,
                "action_type": action_type,
                "command": [str(c) for c in command],
                "ok": ok,
                "exit_code": exit_code,
                "duration_ms": round((time.time() - t0) * 1000, 1),
                "error_tail": err_tail,
            })

    # ── 统计 ────────────────────────────────────────────────────────

    def _persist_stats_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
            with open(_STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._stats, f)
        except Exception:
            pass

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)


_engine: Optional[AutomationEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> AutomationEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = AutomationEngine()
    return _engine


def emit(
    event: str,
    payload: Dict[str, Any],
    audit_fn: Optional[Callable[[str, str, bool, Dict[str, Any]], None]] = None,
) -> int:
    """模块级入口：automation.emit("memory.write", {...})。"""
    payload = dict(payload or {})
    payload["_event"] = event
    return get_engine().emit(event, payload, audit_fn=audit_fn)


def enabled() -> bool:
    return get_engine().enabled()

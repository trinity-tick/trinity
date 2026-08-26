# -*- coding: utf-8 -*-
"""Automation engine unit tests."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["TRINITY_HOME"] = os.path.join(os.path.dirname(__file__), "..", "temp", "auto_test_home")

from trinity.automation.engine import (
    AutomationEngine, _match_condition, DEFAULT_RULES, _RULES_FILE,
    MODE_READONLY, MODE_AUTO, MODE_FULL,
    APPROVAL_NEVER, APPROVAL_ON_FAILURE, APPROVAL_ALWAYS,
)


class TestCondition:
    def test_none_true(self):
        assert _match_condition(None, {}) is True

    def test_eq(self):
        assert _match_condition({"field": "status", "op": "eq", "value": "complete"},
                                {"status": "complete"}) is True
        assert _match_condition({"field": "status", "op": "eq", "value": "active"},
                                {"status": "complete"}) is False

    def test_gte_numeric(self):
        assert _match_condition({"field": "importance", "op": "gte", "value": 0.8},
                                {"importance": 0.9}) is True
        assert _match_condition({"field": "importance", "op": "gte", "value": 0.8},
                                {"importance": "0.7"}) is False

    def test_contains_and_in(self):
        assert _match_condition({"field": "tags", "op": "contains", "value": "wms"},
                                {"tags": ["wms", "上架"]}) is True
        assert _match_condition({"field": "category", "op": "in", "value": ["decision", "wms_knowledge"]},
                                {"category": "decision"}) is True


class TestEngine:
    def test_disabled_by_default(self):
        os.environ.pop("TRINITY_AUTOMATION", None)
        eng = AutomationEngine()
        assert eng.enabled() is False
        assert eng.emit("memory.write", {"importance": 0.9}) == 0
        assert eng.stats()["emitted"] == 0

    def test_enabled_emit_matches_default_rule(self):
        os.environ["TRINITY_AUTOMATION"] = "on"
        try:
            eng = AutomationEngine()
            assert eng.enabled() is True
            assert len(eng._rules) >= len(DEFAULT_RULES)
            # high importance write → matched (async action, just check match count)
            n = eng.emit("memory.write", {"memory_id": "m1", "importance": 0.9,
                                          "category": "decision", "tags": []})
            assert n >= 1
            # low importance → no match
            n2 = eng.emit("memory.write", {"memory_id": "m2", "importance": 0.3,
                                           "category": "general", "tags": []})
            assert n2 == 0
            # search low confidence → match
            n3 = eng.emit("memory.search", {"query": "x", "top_score": 0.1, "hit_count": 1})
            assert n3 >= 1
        finally:
            os.environ.pop("TRINITY_AUTOMATION", None)

    def test_default_rules_shape(self):
        names = [r["name"] for r in DEFAULT_RULES]
        assert "write-high-importance-notify" in names
        assert "search-low-confidence-flag" in names
        assert "search-low-confidence-pagetree-refresh" in names
        assert "write-high-importance-consolidate" in names
        assert "goal-completed-summary" in names
        for r in DEFAULT_RULES:
            assert r["trigger"]
            assert isinstance(r["actions"], list) and r["actions"]

    def test_cooldown_blocks_repeat(self):
        os.environ["TRINITY_AUTOMATION"] = "on"
        try:
            eng = AutomationEngine()
            # 自定义规则：cooldown 300s
            eng._rules = [{
                "name": "cd-test", "enabled": True, "trigger": "memory.write",
                "condition": {"field": "importance", "op": "gte", "value": 0.8},
                "cooldown_seconds": 300,
                "actions": [{"type": "notify", "message": "cd fired"}],
            }]
            assert eng.emit("memory.write", {"memory_id": "a", "importance": 0.9}) >= 1
            # cooldown 内第二次 → 不匹配
            assert eng.emit("memory.write", {"memory_id": "b", "importance": 0.9}) == 0
            # 清 cooldown → 恢复
            eng._cooldown.pop("cd-test", None)
            assert eng.emit("memory.write", {"memory_id": "c", "importance": 0.9}) >= 1
        finally:
            os.environ.pop("TRINITY_AUTOMATION", None)

    def test_loop_protection_env(self):
        # TRINITY_AUTOMATION_ACTION=1（自动化动作子进程）→ ingest hook 跳过 emit，
        # 但引擎本身不感知 env（由 hook 检查）——这里验证 env 语义与规则不匹配
        os.environ["TRINITY_AUTOMATION"] = "on"
        os.environ["TRINITY_AUTOMATION_ACTION"] = "1"
        try:
            eng = AutomationEngine()
            # 引擎仍会匹配（hook 层才拦截）；验证规则匹配逻辑不受影响
            n = eng.emit("memory.write", {"memory_id": "x", "importance": 0.9,
                                          "category": "d", "tags": []})
            assert n >= 1
        finally:
            os.environ.pop("TRINITY_AUTOMATION", None)
            os.environ.pop("TRINITY_AUTOMATION_ACTION", None)


class TestCommandPolicy:
    def test_whitelist_readonly(self):
        eng = AutomationEngine()
        ok, _ = eng._validate_command(["{python}", r"C:\trinity\scripts\build_memory_pagetree.py"], MODE_READONLY)
        assert ok
        ok2, _ = eng._validate_command(["{python}", r"C:\trinity\scripts\memory_ops.py"], MODE_READONLY)
        assert not ok2  # memory_ops 不是只读脚本

    def test_whitelist_auto(self):
        eng = AutomationEngine()
        ok, _ = eng._validate_command(["{python}", r"C:\trinity\scripts\memory_ops.py"], MODE_AUTO)
        assert ok
        ok2, _ = eng._validate_command(["{python}", r"C:\trinity\scripts\evil.py"], MODE_AUTO)
        assert not ok2  # 白名单外拒绝
        ok3, _ = eng._validate_command(["cmd", "/c", "echo"], MODE_AUTO)
        assert not ok3  # 非 python 解释器拒绝

    def test_full_mode(self):
        eng = AutomationEngine()
        ok, _ = eng._validate_command(["cmd", "/c", "echo hi"], MODE_FULL)
        assert ok

    def test_approval_always_enqueues(self):
        import tempfile
        os.environ["TRINITY_AUTOMATION"] = "on"
        os.environ["TRINITY_HOME"] = tempfile.mkdtemp(prefix="ap_test_")
        try:
            eng = AutomationEngine()
            eng._rules = [{
                "name": "ap-always", "enabled": True, "trigger": "memory.write",
                "condition": {"field": "importance", "op": "gte", "value": 0.8},
                "actions": [{"type": "exec", "approval": APPROVAL_ALWAYS,
                             "command": ["{python}", r"C:\trinity\scripts\db_health.py"]}],
            }]
            n = eng.emit("memory.write", {"memory_id": "a", "importance": 0.9})
            assert n >= 1
            pend = eng.pending_items()
            assert len(pend) == 1
            assert pend[0]["status"] == "pending"
            assert pend[0]["reason"] == "approval:always"
            # approve → 出队
            pid = pend[0]["pending_id"]
            assert eng.approve(pid, approve=True) is True
            assert len([p for p in eng.pending_items() if p["status"] == "pending"]) == 0
            # 不存在 id → False
            assert eng.approve("nope") is False
        finally:
            os.environ.pop("TRINITY_AUTOMATION", None)
            os.environ.pop("TRINITY_HOME", None)

    def test_approval_on_failure_enqueues(self):
        import tempfile
        os.environ["TRINITY_AUTOMATION"] = "on"
        os.environ["TRINITY_HOME"] = tempfile.mkdtemp(prefix="apfail_test_")
        try:
            eng = AutomationEngine()
            eng._rules = [{
                "name": "ap-onfail", "enabled": True, "trigger": "memory.search",
                "condition": {"field": "top_score", "op": "lt", "value": 0.2},
                "actions": [{"type": "exec", "approval": APPROVAL_ON_FAILURE,
                             "command": ["{python}", r"C:\trinity\scripts\db_health.py", "--bad-flag"]}],
            }]
            eng.emit("memory.search", {"query": "x", "top_score": 0.1})
            import time
            time.sleep(1.0)  # 等后台线程失败入队
            pend = eng.pending_items()
            assert any(p["reason"] == "approval:on-failure" for p in pend)
        finally:
            os.environ.pop("TRINITY_AUTOMATION", None)
            os.environ.pop("TRINITY_HOME", None)

    def test_rollout_recorded(self):
        import tempfile
        os.environ["TRINITY_AUTOMATION"] = "on"
        home = tempfile.mkdtemp(prefix="rollout_test_")
        os.environ["TRINITY_HOME"] = home
        try:
            eng = AutomationEngine()
            ok = eng._exec_command(["{python}", "-c", "print(1)"], rule_name="t-rule")
            assert ok
            import glob
            files = glob.glob(os.path.join(home, "automation", "rollouts", "*.jsonl"))
            assert len(files) == 1
            import json as _json
            with open(files[0], encoding="utf-8") as f:
                ev = _json.loads(f.readline())
            assert ev["rule"] == "t-rule"
            assert ev["ok"] is True
            assert "duration_ms" in ev
        finally:
            os.environ.pop("TRINITY_AUTOMATION", None)
            os.environ.pop("TRINITY_HOME", None)

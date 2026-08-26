# -*- coding: utf-8 -*-
"""Goal engine unit tests (DSH 借鉴 Phase 1)."""
import os
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["TRINITY_HOME"] = tempfile.mkdtemp(prefix="goals_test_")
os.environ["TRINITY_AUTOMATION"] = "off"

from trinity.evolution.goals import (
    goal_create, goal_update, goal_get, goal_list, evaluate_goals, sample_goals,
)


class TestGoalLifecycle:
    def test_create_and_get(self):
        g = goal_create("测试目标：AnswerAcc >= 0.75",
                        acceptance={"metric": "answer_acc", "op": "gte", "value": 0.75})
        assert g["phase"] == "active"
        assert g["rounds"] == 0
        got = goal_get(g["goal_id"])
        assert got["objective"].startswith("测试目标")
        assert got["acceptance"]["metric"] == "answer_acc"

    def test_list_filter(self):
        goal_create("目标A", acceptance={"metric": "x", "op": "gte", "value": 1})
        g2 = goal_create("目标B", acceptance={"metric": "x", "op": "gte", "value": 1})
        goal_update(g2["goal_id"], "complete")
        active = goal_list(phase="active")
        assert all(g["phase"] == "active" for g in active)
        complete = goal_list(phase="complete")
        assert any(g["goal_id"] == g2["goal_id"] for g in complete)

    def test_update_actions(self):
        g = goal_create("目标C", acceptance={"metric": "x", "op": "gte", "value": 1})
        g2 = goal_update(g["goal_id"], "pause")
        assert g2["phase"] == "paused"
        g3 = goal_update(g["goal_id"], "resume")
        assert g3["phase"] == "active"
        g4 = goal_update(g["goal_id"], "blocked", blocked_reason="测试阻塞")
        assert g4["phase"] == "blocked"
        assert "测试阻塞" in g4["blocked_reason"]
        assert goal_update("no-such-goal", "pause") is None

    def test_evaluate_acceptance(self):
        g = goal_create("达标目标", acceptance={"metric": "answer_acc", "op": "gte", "value": 0.75})
        changed = evaluate_goals({"answer_acc": 0.76})
        assert any(c["goal_id"] == g["goal_id"] and c["phase"] == "complete"
                   for c in changed)
        assert goal_get(g["goal_id"])["phase"] == "complete"

    def test_evaluate_stall_blocks(self):
        g = goal_create("停滞目标", acceptance={"metric": "m", "op": "gte", "value": 10})
        for i in range(3):
            evaluate_goals({"m": 5})
        assert goal_get(g["goal_id"])["phase"] == "blocked"
        assert "无进展" in goal_get(g["goal_id"])["blocked_reason"]

    def test_metrics_missing_skips(self):
        g = goal_create("指标缺失目标", acceptance={"metric": "nope", "op": "gte", "value": 1})
        changed = evaluate_goals({"other": 1})
        assert not any(c["goal_id"] == g["goal_id"] for c in changed)
        assert goal_get(g["goal_id"])["rounds"] == 0

    def test_sample_goals(self):
        samples = sample_goals()
        assert len(samples) >= 2
        assert samples[0]["acceptance"]["metric"] == "answer_acc"

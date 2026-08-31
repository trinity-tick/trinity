# -*- coding: utf-8 -*-
"""EXECUTION 220 测试补课：网络方案机制（197-218）。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestSelfAxioms:
    def test_axioms_structure(self):
        from trinity.brain.self_axioms import verify_axioms
        r = verify_axioms()
        assert r["total"] >= 5
        assert 0 <= r["score"] <= 120


class TestEmotionAxioms:
    def test_emotion_axioms(self):
        from trinity.brain.emotion_axioms import verify_emotion_axioms
        r = verify_emotion_axioms()
        assert r["total"] == 5


class TestEmotionRegulation:
    def test_clamp(self):
        from trinity.brain.emotion_regulation import regulate
        r = regulate({"valence": -0.84, "arousal": 0.9, "polarity": "neg"})
        assert r["after"]["valence"] >= -0.6
        assert r["clamped"]


class TestDopamine:
    def test_reward_signal(self):
        from trinity.brain.dopamine_reward import reward
        s = reward("test-action", True)
        assert s["signal"] > 0
        f = reward("test-action", False)
        assert f["signal"] < 0


class TestMetamemory:
    def test_feeling_of_knowing(self):
        from trinity.brain.metamemory import feeling_of_knowing
        r = feeling_of_knowing("数据库")
        assert "fok" in r


class TestHabit:
    def test_habit_forms(self):
        from trinity.brain.habit_formation import track
        r3 = track("habit-test-x", True)
        track("habit-test-x", True)
        r5 = track("habit-test-x", True)
        # 多次成功应形成习惯（>=3 次）
        assert "habit-test-x" in __import__("trinity.brain.habit_formation", fromlist=["habits"]).habits() or r5["ok"] >= 2


class TestFlexibility:
    def test_switch_logic(self):
        from trinity.brain.cognitive_flexibility import should_switch
        r = should_switch("rrf", 0.8)
        assert r["switch"] is False  # 好性能不切换


class TestAttention:
    def test_competition(self):
        from trinity.brain.attention_control import attend
        r = attend([{"signal": "a", "salience": 0.9, "value": 0.9},
                    {"signal": "b", "salience": 0.1, "value": 0.1}], top_n=1)
        assert r["attended"][0]["item"] == "a"

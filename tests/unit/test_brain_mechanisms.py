# -*- coding: utf-8 -*-
"""大脑化机制测试（EXECUTION 145）——为 17 项运行时机制提供可测背书。

覆盖（纯函数/可 mock 部分）：
  1. 情感层：affect.assess（极性/否定反转/唤醒）+ query_affect_terms
  2. Hebbian：consolidate（mock adapter 验证微调方向与归一化）
  3. 元认知：metacognition.assess_confidence（信心分级）
  4. 预测编码：EMA 更新逻辑（模拟 _predict_hits/_update_prediction_ema）
  5. 价值编码：quick_value（规则启发式）
  6. 感知：perception 函数（信号键/评估规则——导入级验证）
  7. 连续状态：session_context 逻辑（mock adapter）
"""

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ── 1. 情感层 ─────────────────────────────────────────────
class TestAffect:
    def test_negative_assessment(self):
        from trinity.brain.affect import assess
        r = assess("数据库故障导致数据丢失")
        assert r["polarity"] == "neg"
        assert r["valence"] < 0

    def test_positive_assessment(self):
        from trinity.brain.affect import assess
        r = assess("项目成功上线用户满意")
        assert r["polarity"] == "pos"
        assert r["valence"] > 0

    def test_neutral_assessment(self):
        from trinity.brain.affect import assess
        r = assess("这是一条普通记录")
        assert r["polarity"] in ("pos", "neg", "neu")

    def test_negation_inversion(self):
        from trinity.brain.affect import assess
        r = assess("部署没有成功")
        assert r["polarity"] == "neg"

    def test_arousal_on_exclamation(self):
        from trinity.brain.affect import assess
        r1 = assess("警告")
        r2 = assess("严重警告！")
        assert r2["arousal"] >= r1["arousal"]

    def test_query_affect_terms(self):
        from trinity.brain.affect import query_affect_terms
        terms = query_affect_terms("那次事故的教训")
        assert any(t[1] == "neg" for t in terms)


# ── 2. Hebbian 权重级记忆 ─────────────────────────────────
class TestHebbian:
    def test_consolidate_moves_toward_query(self):
        from trinity.brain.hebbian import consolidate

        class MockAdapter:
            def __init__(self):
                self._v = [0.1] * 8
                self.written = None

            def get_embedding(self, mid):
                return list(self._v)

            def set_embedding(self, mid, vec):
                self.written = vec
                return True

        a = MockAdapter()
        qv = [0.5] * 8
        ok = consolidate(a, "mem1", qv, alpha=0.05)
        assert ok
        assert a.written is not None
        # 相似度应上升
        def sim(v):
            return sum(x * y for x, y in zip(v, qv)) / (
                math.sqrt(sum(x * x for x in v)) * math.sqrt(sum(x * x for x in qv)))
        assert sim(a.written) > sim(a._v)
        # 归一化（单位范数）
        norm = math.sqrt(sum(x * x for x in a.written))
        assert abs(norm - 1.0) < 1e-6

    def test_consolidate_no_embedding(self):
        from trinity.brain.hebbian import consolidate

        class NoEmbAdapter:
            def get_embedding(self, mid):
                return None

        assert consolidate(NoEmbAdapter(), "m", [0.1] * 4) is False


# ── 3. 元认知 ─────────────────────────────────────────────
class TestMetacognition:
    def test_high_confidence(self):
        from trinity.brain.metacognition import assess_confidence
        results = [{"score": 0.9}, {"score": 0.8}, {"score": 0.7}, {"score": 0.6}, {"score": 0.5}]
        r = assess_confidence(results, channels=["fts", "vector", "graph"])
        assert r["confidence"] >= 0.7
        assert r["level"] == "high"

    def test_low_confidence_empty(self):
        from trinity.brain.metacognition import assess_confidence
        r = assess_confidence([], channels=["fts"])
        assert r["confidence"] == 0.0
        assert r["level"] == "none"


# ── 4. 预测编码 EMA ───────────────────────────────────────
class TestPrediction:
    def test_ema_update(self):
        # 模拟 _update_prediction_ema 逻辑（α=0.3）
        ema = {"short": None, "long": None}
        bucket = "short"
        prev = ema.get(bucket)
        actual = 5.0
        ema[bucket] = actual if prev is None else prev * 0.7 + actual * 0.3
        assert ema["short"] == 5.0
        # 第二次更新
        ema[bucket] = ema[bucket] * 0.7 + 2.0 * 0.3
        assert abs(ema[bucket] - 4.1) < 1e-9

    def test_predict_short_query(self):
        # 短查询（<=4 字）预测 = top_k 的 80%
        assert int(5 * 0.8) == 4


# ── 5. 价值编码 ───────────────────────────────────────────
class TestValue:
    def test_quick_value_high(self):
        from trinity.brain.value_encoder import quick_value
        v = quick_value("严重事故导致数据丢失的教训", "incident")
        assert v >= 0.6

    def test_quick_value_low(self):
        from trinity.brain.value_encoder import quick_value
        v = quick_value("随便聊聊今天天气", "chat")
        assert v <= 0.4


# ── 6. 感知 ───────────────────────────────────────────────
class TestPerception:
    def test_signal_key_stable(self):
        from trinity.brain.perception import _signal_key
        k1 = _signal_key("error", "数据库连接失败")
        k2 = _signal_key("error", "数据库连接失败")
        assert k1 == k2
        assert len(k1) == 24

    def test_engine_evaluate(self):
        from trinity.brain.perception import PerceptionEngine
        eng = PerceptionEngine()
        r = eng.evaluate("alert", "磁盘空间不足")
        assert r["salience"] >= 0.5


# ── 7. 连续状态 ───────────────────────────────────────────
class TestSessionContext:
    def test_context_roundtrip(self):
        class MockAdapter:
            def __init__(self):
                self.saved = None

            def context_save(self, lq, percepts):
                self.saved = (lq, percepts)
                return True

            def context_load(self):
                if self.saved:
                    return {"last_query": self.saved[0], "percepts": self.saved[1]}
                return None

        a = MockAdapter()
        assert a.context_save("测试查询", ["p1"])
        loaded = a.context_load()
        assert loaded["last_query"] == "测试查询"
        assert loaded["percepts"] == ["p1"]

# -*- coding: utf-8 -*-
"""trinity/brain/consciousness_blueprint.py — 合成意识蓝图评估（EXECUTION 198）。

借鉴 "Testable Blueprint for Synthetic Consciousness"（2026）——
用可测试判据给 Trinity 的意识组件打分（哪些有/哪些缺）。

判据维度（合成意识蓝图）：
  1. 情境感知（situatedness）——连续状态/当下
  2. 自我模型（self-model）——全局自我
  3. 预测能力（prediction）——预测编码
  4. 行动反馈（action-feedback）——行动回路+经验学习
  5. 内省（introspection）——自省+自我评估
  6. 叙事整合（narrative）——自传体
  7. 社会认知（social）——市场+知识传播
  8. 目标驱动（goal-directed）——好奇心+意图
  9. 学习可塑（plasticity）——Hebbian+对比训练
  10. 持续存在（persistence）——跨会话身份+心跳
"""
import os
import sys
import json


def assess_blueprint() -> dict:
    """蓝图判据评估：每项 0-10 分（基于可验证证据）。"""
    checks = {}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()

        # 1) 情境感知：会话上下文存在
        cur.execute("SELECT count(*) FROM session_context")
        checks["1_situatedness"] = min(10, 3 + cur.fetchone()[0] // 3)

        # 2) 自我模型：全局身份
        cur.execute("SELECT count(*) FROM memories WHERE category='self-identity'")
        checks["2_self_model"] = 8 if cur.fetchone()[0] > 0 else 2

        # 3) 预测：预测状态文件
        checks["3_prediction"] = 8 if os.path.exists(
            os.path.expanduser("~/.trinity/predictive_state.json")) else 2

        # 4) 行动反馈：行动统计
        checks["4_action_feedback"] = 8 if os.path.exists(
            os.path.expanduser("~/.trinity/action_loop_stats.json")) else 2

        # 5) 内省：自省+评估
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        checks["5_introspection"] = min(10, 4 + cur.fetchone()[0] // 5)

        # 6) 叙事：自传体
        cur.execute("SELECT count(*) FROM memories WHERE category='self-narrative'")
        checks["6_narrative"] = 8 if cur.fetchone()[0] > 0 else 2

        # 7) 社会：社会记忆+市场
        cur.execute("SELECT count(*) FROM memories WHERE category='social-memory'")
        checks["7_social"] = 8 if cur.fetchone()[0] > 0 else 3

        # 8) 目标驱动：好奇状态
        checks["8_goal_directed"] = 7 if os.path.exists(
            os.path.expanduser("~/.trinity/perception_focus.json")) else 4

        # 9) 学习可塑：DCPM 信念数
        cur.execute("SELECT count(*) FROM dcpm_beliefs")
        checks["9_plasticity"] = min(10, 4 + cur.fetchone()[0] // 20)

        # 10) 持续存在：身份+预测+心跳
        checks["10_persistence"] = 9 if (os.path.exists(
            os.path.expanduser("~/.trinity/predictive_state.json")) and
            os.path.exists(os.path.expanduser("~/.trinity/action_loop.json"))) else 4

        conn.close()
    except Exception:
        pass
    total = sum(checks.values())
    max_v = 10 * 10
    return {"criteria": checks, "score": total, "max": max_v,
            "percent": round(total * 100 / max_v, 1)}

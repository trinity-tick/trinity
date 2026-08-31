# trinity/brain 模块索引（EXECUTION 222 梳理）

> 37 个模块按认知领域分组。全部经 brain_capabilities() 注册可查。

## 感知层（5）
- perception.py（感知引擎：显著性/习惯化/注意力筛选）
- sensory_integration.py（统觉：多通道关联/融合）
- attention_control.py（注意力竞争/注意转移）
- vision.py（视觉特征描述）
- （网络感知在 scripts/）

## 记忆层（9）
- hebbian.py（Hebbian 强化）
- working_memory.py（工作记忆）
- memory_manager.py（长短期管理）
- emotional_consolidation.py（情绪记忆巩固）
- associative_memory.py（联想跳跃）
- reconstructive_memory.py（记忆重构）
- metamemory.py（元记忆）
- compression.py（压缩）
- value_encoder.py（价值编码）

## 认知层（8）
- cognition_pipeline.py（认知编排层——管线）
- curiosity.py（好奇心）
- predictive_loop.py（预测-行动环）
- cognitive_flexibility.py（认知灵活性）
- mental_simulation.py（心理模拟）
- unknown_awareness.py（未知感知）
- metacognition.py（元认知）
- resource_adaptation.py（资源自适应）

## 情绪层（4）
- affect.py（情感评估）
- affect_state.py（情绪状态机）
- emotion_regulation.py（情绪调节）
- emotion_axioms.py（情绪公理）

## 自我层（7）
- self_model.py（身份）
- self_assessment.py（评估）
- autobiographical.py（叙事）
- self_prediction.py（自我预测）
- self_axioms.py（自我公理）
- consciousness_blueprint.py（意识蓝图）
- proactive_initiative.py（主动发起）

## 社会层（3）
- social_memory.py（社会记忆）
- theory_of_mind.py（心智理论）
- observational_learning.py（观察学习）

## 行动层（3）
- action_loop.py（行动回路）
- dopamine_reward.py（多巴胺奖赏）
- habit_formation.py（习惯形成）

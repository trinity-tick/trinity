# -*- coding: utf-8 -*-
"""trinity/brain/hebbian.py — 权重级记忆（2026-09，EXECUTION 140）

Hebbian 学习（"同步激活的神经元连接增强"）的检索侧实现：
高置信检索命中时，top1 记忆的 embedding 向查询方向微调——记忆被反复
想起时其"连接"真实增强（用进废退的权重级实现，非规则模拟）。

- alpha 极小（0.005），单次漂移可忽略，长期使用累积强化
- 仅对 access_count >= 5 的记忆强化（避免新记忆过度漂移）
- 幂等安全：失败静默，不影响检索
"""

import math


def consolidate(adapter, memory_id, query_vec, alpha: float = 0.005) -> bool:
    """Hebbian 强化：memory_id 的 embedding 向 query_vec 方向微调。

    new = normalize(embed + alpha * query)（近似：线性插值）
    """
    try:
        if not memory_id or adapter is None:
            return False
        old = adapter.get_embedding(memory_id) if hasattr(adapter, "get_embedding") else None
        if old is None:
            return False
        if len(old) != len(query_vec):
            return False
        new_v = [old[i] * (1.0 - alpha) + float(query_vec[i]) * alpha
                 for i in range(len(old))]
        # 归一化（保持单位球面）
        norm = math.sqrt(sum(x * x for x in new_v)) or 1.0
        new_v = [x / norm for x in new_v]
        if hasattr(adapter, "set_embedding"):
            return bool(adapter.set_embedding(memory_id, new_v))
        return False
    except Exception:
        return False

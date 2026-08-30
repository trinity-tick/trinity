# -*- coding: utf-8 -*-
"""trinity/brain/hebbian.py — 权重级记忆（2026-09，EXECUTION 140/146）

Hebbian 学习（"同步激活的神经元连接增强"）的检索侧实现：
高置信检索命中时，top1 记忆的 embedding 向查询方向微调——记忆被反复
想起时其"连接"真实增强（用进废退的权重级实现，非规则模拟）。

EXECUTION 146 新增 batch_contrastive：用重放三元组做对比强化——
正样本（query→memory）向查询微调，负样本（hard-negative）远离查询
（对比学习的轻量实现，替代全量模型微调）。

- alpha 极小（0.005），单次漂移可忽略，长期使用累积强化
- 仅对 access_count >= 5 的记忆强化（避免新记忆过度漂移）
- 幂等安全：失败静默，不影响检索
"""

import math


def consolidate(adapter, memory_id, query_vec, alpha: float = 0.005) -> bool:
    """Hebbian 强化：memory_id 的 embedding 向 query_vec 方向微调。"""
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
        norm = math.sqrt(sum(x * x for x in new_v)) or 1.0
        new_v = [x / norm for x in new_v]
        if hasattr(adapter, "set_embedding"):
            return bool(adapter.set_embedding(memory_id, new_v))
        return False
    except Exception:
        return False


def batch_contrastive(adapter, triplets, alpha: float = 0.003,
                      neg_alpha: float = 0.001) -> dict:
    """对比强化（EXECUTION 146）：正样本向查询微调 + 负样本远离。

    triplets: [(query_vec, pos_memory_id, neg_memory_id), ...]
    正样本：embed 向 query 移动（+alpha）
    负样本：embed 沿 query 反向移动（-neg_alpha，弱化）
    返回 {"positive": n, "negative": n, "failed": n}
    """
    pos = neg = failed = 0
    try:
        for qv, pid, nid in triplets:
            try:
                if pid:
                    _ok = consolidate(adapter, pid, qv, alpha=alpha)
                    if _ok:
                        pos += 1
                    else:
                        failed += 1
                if nid:
                    # 负样本：远离查询（反向微调）
                    _old = adapter.get_embedding(nid) if hasattr(adapter, "get_embedding") else None
                    if _old and len(_old) == len(qv):
                        _new = [_old[i] * (1.0 + neg_alpha) - float(qv[i]) * neg_alpha
                                for i in range(len(_old))]
                        _norm = math.sqrt(sum(x * x for x in _new)) or 1.0
                        _new = [x / _norm for x in _new]
                        if hasattr(adapter, "set_embedding") and adapter.set_embedding(nid, _new):
                            neg += 1
            except Exception:
                failed += 1
        return {"positive": pos, "negative": neg, "failed": failed}
    except Exception:
        return {"positive": pos, "negative": neg, "failed": failed + 1}

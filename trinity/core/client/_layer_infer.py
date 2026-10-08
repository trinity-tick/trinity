"""查询性质 → 记忆层的推断（2026-08-27 方向A 认知分层）。

2026-10-06 从 `_search.py` **迁出**：该文件撞上 `docs/STRUCTURE_BUDGETS.json`
的 1400 行硬预算（迁出前 1399/1400，只剩 1 行余量），按仓库"超预算优先拆分迁出"
政策移到这里。同一先例见 `_search.py::_embed_query_bounded` → `_vec_budget.py`。

语义与迁出前**逐字一致**：时间词 → episodic（会话延续）；知识词 → semantic
（事实/规范）；无信号 → None（全层）。`_search.py` 仍以原名 `_infer_layer`
把 `infer_layer` 导入回去，故对外调用点与既有引用不变。
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from trinity._swallow import swallow
except Exception:  # 兜底：与 _search.py 的惰性重导入同义（导入失败则静默无操作）
    # 2026-10-06（测试归因轮 T1）：回退签名必须与真身**逐字一致** ——
    # 迁出时漏了 `exc: Any` 的注解，`tests/unit/test_swallow_fallback_contract.py`
    # 当场判红（该判据的存在理由正是这种"照抄旧模板时漏一处"的回潮）。
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        return None


_TIME_WORDS = ("最近", "刚才", "昨天", "今天", "上次", "之前", "刚", "刚刚", "前几天", "先前")
_KNOWLEDGE_WORDS = ("规则", "规范", "标准", "流程", "步骤", "配置", "指南", "手册", "制度")


def infer_layer(query: str) -> Optional[str]:
    """2026-08-27（方向A 认知分层）：查询性质 -> 记忆层。

    时间词 → STM/IM（会话延续）；知识词 → LTM（事实/规范）；无信号 → None（全层）。
    """
    try:
        q = str(query or "")
        if any(w in q for w in _TIME_WORDS):
            return "episodic"
        if any(w in q for w in _KNOWLEDGE_WORDS):
            return "semantic"
    except Exception as _e:
        swallow(__name__, _e)
    return None

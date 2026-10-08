"""文档可达性 + 字段一致性闸门（2026-09-30，外部审计遗留缺口）。

两个缺口（都在本轮修）：

### A. 文档语料在 hybrid API 上不可达
`memories` 有 2,939 条 active 的 `doc:*` 类，但 `search_memories` 默认加
`(category NOT LIKE 'doc:%' AND category NOT LIKE 'doc_%')`；底层与 `GET /memories`
都有 `include_docs` 开关，**唯独 hybrid 入口没有** ⇒ 项目自己的
`scripts/doc_retrieval_eval.py` 在 API 面上测得 **R@10=0.0、20/20 题"未解析"**，
`docs/SCORES.json` 登记的 `docs_corpus_hybrid_20260916`（R@10 0.35）无法复现。

修复后实测（同一 golden set，`include_docs=True`）：**未解析 20/20 → 3/20**，
R@10 = 0.25（该值与登记基线 0.35 不可直接比较 —— 基线是 2026-09-16 的另一份语料
/ 路由状态；本轮**不声称**召回提升）。

### B. light 路径不填 `hybrid_score`
线上实测 light 响应里 `hybrid_score` **恒为 0**，而全部消费方读的都是它
（`_routers_explain.py:126`、`_search.py:277`、`marvis_adapter.py:279`）
⇒ 消费者把"有结果"读成"零相关"。修复后实测 light 行
`hybrid_score == score`（如 `[1.0114, 0.8403, 0.6837, 0.2477, 0.0057]`）。

运行：``python -m pytest tests/unit/test_docs_reachability_20260930.py -q``
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ── A. include_docs 必须贯穿到 hybrid 入口 ──────────────────────────────

def test_request_model_exposes_include_docs() -> None:
    from trinity.api.server._models import HybridSearchRequest

    assert "include_docs" in HybridSearchRequest.model_fields, (
        "HybridSearchRequest 必须暴露 include_docs，否则 docs 语料在主路径上不可达"
    )
    assert HybridSearchRequest.model_fields["include_docs"].default is False, (
        "默认必须是 False —— 记忆检索面与知识检索面仍应分离（行为逐字不变）"
    )


def test_client_search_hybrid_accepts_include_docs() -> None:
    from trinity.core.client._hybrid_search import _HybridSearchMixin

    sig = inspect.signature(_HybridSearchMixin.search_hybrid)
    assert "include_docs" in sig.parameters
    assert sig.parameters["include_docs"].default is False


def test_light_path_threads_include_docs_to_adapter() -> None:
    """light 路径的每个 adapter 调用都必须带上 include_docs（否则开关是空的）。"""
    src = (ROOT / "trinity/core/client/_hybrid_search.py").read_text(encoding="utf-8")
    calls = [
        i for i, _ln in enumerate(src.splitlines(), 1)
        if "self._adapter.search_memories(" in _ln and not _ln.lstrip().startswith("#")
    ]
    assert calls, "未找到 adapter 调用点（前提变了）"
    for ln in calls:
        block = "\n".join(src.splitlines()[ln - 1: ln + 8])
        assert "include_docs=include_docs" in block, (
            f"_hybrid_search.py:{ln} 的 search_memories 调用没有传 include_docs"
        )


def test_api_route_passes_include_docs() -> None:
    src = (ROOT / "trinity/api/server/_routers_search.py").read_text(encoding="utf-8")
    assert "include_docs=" in src, "路由未把 include_docs 传给客户端层"


# ── B. light 路径必须填 hybrid_score ─────────────────────────────────────

def test_mirror_helper_adds_hybrid_score() -> None:
    from trinity.core.client._hybrid_search import _mirror_hybrid_score

    rows = [{"memory_id": "a", "score": 0.7}, {"memory_id": "b", "score": 0.2}]
    _mirror_hybrid_score(rows)
    assert [r["hybrid_score"] for r in rows] == [0.7, 0.2]


def test_mirror_helper_never_overwrites_existing() -> None:
    """full 路径已算好 hybrid_score ⇒ 补齐逻辑不得覆盖它（行为逐字不变）。"""
    from trinity.core.client._hybrid_search import _mirror_hybrid_score

    rows = [{"memory_id": "a", "score": 0.7, "hybrid_score": 0.0123}]
    _mirror_hybrid_score(rows)
    assert rows[0]["hybrid_score"] == 0.0123


def test_mirror_helper_is_defensive() -> None:
    from trinity.core.client._hybrid_search import _mirror_hybrid_score

    _mirror_hybrid_score(None)          # 不得抛
    _mirror_hybrid_score([])            # 不得抛
    _mirror_hybrid_score(["not-a-dict"])  # 不得抛


def test_light_exits_call_the_mirror() -> None:
    """两处 light 出口都必须调用补齐（只补一处会留下另一处恒 0）。

    注意排除**定义行** —— `def _mirror_hybrid_score(results)` 也含该子串
    （本判据第一版就是这么多数了 1 个）。
    """
    src = (ROOT / "trinity/core/client/_hybrid_search.py").read_text(encoding="utf-8")
    calls = [
        _ln for _ln in src.splitlines()
        if "_mirror_hybrid_score(results)" in _ln and not _ln.lstrip().startswith("def ")
    ]
    assert len(calls) == 2, f"light 出口应恰好 2 处调用补齐，实测 {len(calls)}"


# ── C. golden set 自身仍可解析（度量前提）───────────────────────────────

def test_golden_set_is_usable() -> None:
    p = ROOT / "eval" / "doc_golden_set.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    items = d if isinstance(d, list) else (d.get("questions") or d.get("items") or [])
    assert len(items) >= 20
    for it in items[:5]:
        assert it.get("query") and it.get("target")

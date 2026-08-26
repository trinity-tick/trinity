"""Trinity client - diagnostics & analysis mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
class _DiagnosticsMixin:
    def diagnostics(self) -> Dict[str, Any]:
        """Run full system diagnostics."""
        if self._adapter:
            adapter_diag = self._adapter.diagnostics()
            from trinity.modules.second_brain import Engine
            try:
                import builtins
                _orig_print = builtins.print
                builtins.print = lambda *a, **kw: None
                engine = Engine()
                builtins.print = _orig_print
                engine_diag = engine.run_diagnostics()
            except Exception:
                engine_diag = {"status": "engine not available"}
            from trinity.version import __version__, VERSION_STRING
            return {
                "trinity_version": VERSION_STRING,
                "source_version": __version__,
                "total_modules": 5,
                "adapter": adapter_diag,
                "engine": engine_diag,
            }
        return self.bridge("diagnostics")
    def detect_contradiction(
        self, statement_a: str, statement_b: str
    ) -> Dict[str, Any]:
        return self.bridge("contradiction",
                           statement_a=statement_a,
                           statement_b=statement_b)
    def hopfield_energy(
        self, memories: List[Dict[str, Any]], query: str
    ) -> Dict[str, Any]:
        return self.bridge("hopfield", memories=memories, query=query)
    def selfmem_strategy(self, actions: List[str]) -> Dict[str, Any]:
        return self.bridge("strategy", actions=actions)
    def reason(
        self,
        query: str,
        multi_hop: bool = False,
        top_k: int = 5,
        qtype: Optional[str] = None,
        question_date: Optional[str] = None,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """开放域推理。

        2026-08-17 产品化: TRINITY_ROUTE_REASONER=on 时走 RouteReasoner——
        封装基准已验证的生成策略（multi→turn 粒度 / temporal→REL+inner2 /
        pref→两段式 / 其他→dated plain，见 trinity/qa/route_reasoner.py）；
        无凭证/失败回退 OpenDomainReasoner。默认 off（行为兼容）。
        """
        # 2026-08-25（评测/生产对齐修复）：生产推理检索改用 hybrid 5 通道——
        # 此前 search_fn=self.search（默认 FTS5），而自进化评测优化的是
        # search_hybrid（5 通道 RRF）——评测结论在生产从未生效（核心失真）。
        # self.search(mode="hybrid") 走 5 通道 RRF 并自动补全 content。
        def _hybrid_search(query, top_k=8, agent_id=None, persona_id=None):
            try:
                # 确保 hybrid_retriever 已初始化（property 懒构建 5 通道；
                # 不触发则 _use_hybrid=False 回退 FTS——评测/生产失真根因）
                if getattr(self, "_hybrid_retriever", None) is None:
                    _ = self.hybrid_retriever
                return self.search(query, top_k=top_k, mode="hybrid",
                                   agent_id=agent_id, persona_id=persona_id)
            except Exception:
                return self.search(query, top_k=top_k, agent_id=agent_id,
                                   persona_id=persona_id)
        if os.environ.get("TRINITY_ROUTE_REASONER", "off").strip().lower() == "on":
            try:
                from trinity.qa.route_reasoner import RouteReasoner

                rr = RouteReasoner(search_fn=_hybrid_search)
                if rr.available:
                    return rr.answer(
                        query, qtype=qtype, question_date=question_date,
                        agent_id=agent_id, persona_id=persona_id,
                    )
            except Exception:
                pass  # 回退
        if self._engine:
            from trinity.modules.open_domain.reasoner import OpenDomainReasoner
            reasoner = OpenDomainReasoner()
            if multi_hop:
                return reasoner.answer_multi_hop(query, retriever=_hybrid_search, top_k=top_k)
            return reasoner.answer(query, retriever=_hybrid_search, top_k=top_k)
        if self.bridge is not None:
            return self.bridge("reason", query=query, multi_hop=multi_hop, top_k=top_k)
        # 2026-08-25（闭环自检修复）：bridge 缺失时返回可读错误而非崩溃
        return {"answer": None, "error": "reason unavailable: bridge module missing "
                "(trinity_call not deployed)", "strategy": None, "evidence": []}

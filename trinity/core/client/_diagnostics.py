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
        if os.environ.get("TRINITY_ROUTE_REASONER", "off").strip().lower() == "on":
            try:
                from trinity.qa.route_reasoner import RouteReasoner

                rr = RouteReasoner(search_fn=self.search)
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
                return reasoner.answer_multi_hop(query, retriever=self.search, top_k=top_k)
            return reasoner.answer(query, retriever=self.search, top_k=top_k)
        return self.bridge("reason", query=query, multi_hop=multi_hop, top_k=top_k)

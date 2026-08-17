"""
Three-Layer Memory Classifier (P0.4)
=====================================
Automatic classification of memory content into Trinity's three-layer
memory taxonomy:

  - fact     → SEMANTIC    (verifiable assertions, grounded truths)
  - context  → SEMANTIC    (environmental / situational information)
  - episodic → EPISODIC    (personal experiences, timestamped events)

Design
------
The classifier uses a lightweight heuristic pipeline:
  1. Regex-based pattern matching for high-precision signals.
  2. Keyword heuristic as fallback.
  3. Pluggable LLM callback for advanced cases.

This complements the existing consolidation engine (HippocampalConsolidator)
by providing a write-time classification gate, ensuring memories enter the
correct store from birth — not just after consolidation.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── Pattern libraries ─────────────────────────────────────────────────

# Fact patterns: verifiable statements, definitions, data points
_FACT_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\b(?:根据|据|按照|依据)\b',
        r'\b(?:defined|定义|refer|refers? to|is a|are)\b',
        r'\b(?:true|false|correct|incorrect)\b',
        r'\b(?:数值|统计|测量|calculated?|computed?)\b',
        r'\b(?:A\s*=\s*|equals|等于|total|sum|average|mean)\b',
        r'\b(?:version\s*=?\s*[\d.]+|API|endpoint|schema|table)\b',
        r'[=≈≠<>≤≥±]',
        r'\b(?:always|never|must|every|all|none)\b',
        r'\b(?:成立于|founded|located|headquarters|CEO|revenue)\b',
    ]
]

# Context patterns: environmental, situational, configuration
_CONTEXT_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\b(?:配置|config|setting|环境|environment|deployed)\b',
        r'\b(?:OS\s|Ubuntu|Windows|macOS|Linux|kernel)\b',
        r'\b(?:currently|当前|正在|running|serving|listening|port)\b',
        r'\b(?:temperature|humidity|load|CPU|RAM|disk|内存|磁盘)\b',
        r'\b(?:connected|online|offline|status|健康|healthy|degraded)\b',
        r'\b(?:ENV|\.env|API_KEY|SECRET|TOKEN)\b',
        r'\b(?:directory|path|folder|文件结构|project structure)\b',
        r'\b(?:log|日志|trace|monitor|监控)\b',
        r'\b(?:scheduled|cron|定时|每隔|interval)\b',
    ]
]

# Episodic patterns: personal experiences, temporal events
_EPISODIC_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\b(?:今天|昨天|明天|今早|昨晚|刚才|刚刚|now|just)\b',
        r'\b(?:会议|meeting|讨论|called|phoned|sent|收到|said)\b',
        r'\b(?:我|你|他|她|I|you|he|she|we|they)\s*\w{2,4}(?:了|过)\b',
        r'\b(?:happen|事件|发生|经历|remember|recall|回想起)\b',
        r'\b(?:上次|上回|上次|last\s+time|previous)\b',
        r'\b\d{1,2}[:：]\d{2}\b',  # timestamps
        r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b',  # dates
        r'\b(?:告诉|通知|提醒|reminded|informed)\b',
        r'\b(?:感觉|觉得|think|believe|opinion|认为)\b',
    ]
]

# Keyword-level signals (word-level counts, not full-line regex)
_FACT_WORDS = {
    "fact", "definition", "property", "attribute", "constraint",
    "rule", "principle", "axiom", "theorem", "equation",
}
_CONTEXT_WORDS = {
    "environment", "setup", "workspace", "context", "surrounding",
    "ambient", "runtime", "session",
}
_EPISODIC_WORDS = {
    "event", "experience", "moment", "memory", "recall",
    "episode", "occurrence", "incident",
}


def _pattern_score(text: str, patterns: list) -> int:
    """Count how many distinct patterns match the text."""
    return sum(1 for p in patterns if p.search(text))


def _keyword_score(text: str, keywords: set) -> int:
    """Count keyword occurrences in lowercased text."""
    lower = text.lower()
    return sum(1 for kw in keywords if kw in lower)


def _is_temporal(text: str) -> bool:
    """Check for strong temporal signals (dates / timestamps / relative time)."""
    temporal_patterns = [
        re.compile(r'\b(?:today|yesterday|tomorrow|now|just)\b', re.IGNORECASE),
        re.compile(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b'),
        re.compile(r'\b\d{1,2}[:：]\d{2}\b'),
        re.compile(r'\b(?:this\s+morning|this\s+afternoon|tonight|上周|下周)\b', re.IGNORECASE),
        # Chinese temporal words
        re.compile(r'(?:今天|昨天|明天|今早|昨晚|刚才|刚刚|刚才|前天|后天|上周|下周|本月|上月|今年|去年|刚才)'),
    ]
    return any(p.search(text) for p in temporal_patterns)


# ── Main classifier ───────────────────────────────────────────────────

class LayerClassifier:
    """Three-layer memory type classifier.

    Maps raw memory content to MemoryType values:
      - ``semantic`` for facts and context
      - ``episodic`` for personal experiences

    Parameters
    ----------
    llm_call : callable, optional
        Signature ``(prompt: str) -> str``. When provided, used as
        fallback for ambiguous cases. When None, defaults are used.
    """

    def __init__(self, llm_call: Optional[Callable[[str], str]] = None):
        self._llm_call = llm_call

    def classify(self, content: str, category: str = "general",
                 tags: Optional[list] = None) -> str:
        """Classify a memory content string into ``semantic`` or ``episodic``.

        Parameters
        ----------
        content : str
            Raw memory text.
        category : str
            Pre-existing category label (optional hint).
        tags : list, optional
            Pre-existing tags (optional hint).

        Returns
        -------
        str
            ``semantic`` or ``episodic``.
        """
        tags = tags or []

        # ── Stage 1: Explicit hints from tags / category ──
        if category in ("episodic", "event", "experience"):
            return "episodic"
        if category in ("semantic", "fact", "context", "knowledge", "rule"):
            return "semantic"

        # ── Stage 2: Strong temporal signal → episodic ──
        has_temporal = _is_temporal(content)
        # Chinese personal pronouns (no word boundaries in CJK)
        _cn_personal = re.compile(r'(?:我|你|他|她|我们|你们|他们|她们|咱|咱们)')
        _en_personal = re.compile(r'\b(?:I|you|he|she|we|they)\b', re.IGNORECASE)
        has_personal = bool(_cn_personal.search(content)) or bool(_en_personal.search(content))

        # ── Stage 3: Pattern scoring ──
        fact_score = _pattern_score(content, _FACT_PATTERNS) + _keyword_score(content, _FACT_WORDS)
        context_score = _pattern_score(content, _CONTEXT_PATTERNS) + _keyword_score(content, _CONTEXT_WORDS)
        episodic_score = _pattern_score(content, _EPISODIC_PATTERNS) + _keyword_score(content, _EPISODIC_WORDS)

        semantic_total = fact_score + context_score

        # ── Stage 4: Decision ──
        # Strong episodic signal → episodic
        if has_temporal and has_personal:
            return "episodic"

        # Pure temporal + weak fact → episodic
        if has_temporal and fact_score < 2:
            return "episodic"

        # Clear semantic dominance
        if semantic_total >= 2 and episodic_score == 0:
            return "semantic"

        # Clear episodic dominance
        if episodic_score >= 3 and semantic_total <= 1:
            return "episodic"

        # Default: semantic (safety-first — facts/context are safer to store as semantic)
        default = "semantic"
        if episodic_score > semantic_total:
            default = "episodic"

        # ── Stage 5: LLM fallback for ambiguous ──
        if self._llm_call and abs(semantic_total - episodic_score) <= 1:
            prompt = (
                "Classify this memory content as 'semantic' (fact/context/knowledge) "
                "or 'episodic' (personal experience/event). Reply with one word.\n\n"
                f"Content: {content[:500]}"
            )
            try:
                response = self._llm_call(prompt).strip().lower()
                if "episodic" in response:
                    return "episodic"
                if "semantic" in response:
                    return "semantic"
            except Exception:
                logger.warning("LLM classification failed, using heuristic default",
                               exc_info=True)

        return default

    def classify_batch(self, memories: list, **kwargs) -> Dict[str, Any]:
        """Classify a batch of memories and return statistics.

        Parameters
        ----------
        memories : list
            List of dicts with at least ``content`` key.

        Returns
        -------
        dict with keys: results, semantic_count, episodic_count, total.
        """
        results = []
        semantic_count = 0
        episodic_count = 0
        for mem in memories:
            content = mem.get("content", "")
            mem_type = self.classify(
                content,
                category=mem.get("category", "general"),
                tags=mem.get("tags", []),
            )
            entry = {**mem, "memory_type": mem_type}
            results.append(entry)
            if mem_type == "semantic":
                semantic_count += 1
            else:
                episodic_count += 1
        return {
            "results": results,
            "semantic_count": semantic_count,
            "episodic_count": episodic_count,
            "total": len(memories),
        }


# ── Module-level self_test ────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """Smoke test covering fact, context, and episodic classification."""

    classifier = LayerClassifier()

    # Test cases: (content, expected)
    cases = [
        # facts → semantic
        ("The Atomic Mass Evaluation (AME2020) defines neutron mass as 1.00866491588 u.",
         "semantic"),
        ("根据量子力学，电子轨道只能取分立能级。",
         "semantic"),
        ("API version 3.4 supports OAuth2 with PKCE.",
         "semantic"),
        ("Total revenue for Q3 was $12.4M, up 8.2% YoY.",
         "semantic"),

        # context → semantic
        ("当前运行环境为 Ubuntu 22.04, kernel 6.5.0-14, GPU driver 535.",
         "semantic"),
        ("The deployment listens on port 8080 with TLS 1.3.",
         "semantic"),
        ("Redis cluster status: 3/3 nodes healthy, memory usage 42%.",
         "semantic"),

        # episodic → episodic
        ("昨天下午和 Alice 讨论了 Trinity 的存储引擎架构，她建议改用 LSM-tree。",
         "episodic"),
        ("今天早上我收到了一份来自 Bob 的邮件，他说 CI/CD 管道又挂了。",
         "episodic"),
        ("Last week we had a meeting about the new deployment strategy.",
         "episodic"),
        ("2026-08-10 14:30: just got a Slack notification about the outage.",
         "episodic"),
    ]

    passed = 0
    failed = 0
    details = []
    for content, expected in cases:
        result = classifier.classify(content)
        ok = (result == expected)
        if ok:
            passed += 1
        else:
            failed += 1
            details.append(f"EXPECTED {expected} GOT {result}: {content[:60]}...")

    return {
        "module": "trinity.memory.layer_classifier",
        "result": "PASS" if failed == 0 else "FAIL",
        "passed": passed,
        "failed": failed,
        "total": len(cases),
        "details": details[:5],
    }

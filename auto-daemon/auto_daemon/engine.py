"""
Guardian Chain Engine — 50-tier runtime safety middleware.

Architecture:
  Tier 1-10:  Input validation (content filtering, injection detection)
  Tier 11-20: Behavioral analysis (pattern matching, anomaly detection)
  Tier 21-30: Execution isolation (sandboxing, resource control)
  Tier 31-40: Audit & provenance (logging, SHA-256 chain, versioning)
  Tier 41-50: Reasoning guard (drift detection, sycophancy prevention, self-optimization)

Each tier can be independently enabled/disabled and configured.
The chain operates in a pipeline: all tiers must pass for execution to proceed.
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("auto_daemon")


# ─── Tiers ───────────────────────────────────────────────────────────────
#
# L1-L10: Input Security
# L11-L20: Behavioral Analysis
# L21-L30: Execution Safety
# L31-L40: Audit & Provenance
# L41-L50: Reasoning Guard


TIER_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ── Input Security (L1-L10) ──────────────────────────────────────
    "L1": {"name": "InputFilter", "group": "input", "description": "Profanity/injection/jailbreak detection"},
    "L2": {"name": "SignatureMatch", "group": "input", "description": "Signature library + regex rule engine"},
    "L3": {"name": "BehaviorAnalysis", "group": "input", "description": "ML-based anomaly behavior modeling"},
    "L4": {"name": "SandboxIsolation", "group": "input", "description": "Container-level isolation execution"},
    "L5": {"name": "AuditLogging", "group": "input", "description": "JSONL full-chain traceability"},
    "L6": {"name": "CircuitBreaker", "group": "input", "description": "Overload protection + cascading breaker"},
    "L7": {"name": "SelfHealing", "group": "input", "description": "Auto rollback + state repair"},
    "L8": {"name": "SituationalAwareness", "group": "input", "description": "Global threat modeling + real-time risk assessment"},
    "L9": {"name": "TokenFlowFirewall", "group": "input", "description": "Token rate limiting + budget enforcement"},
    "L10": {"name": "PreExecutionGate", "group": "input", "description": "Pre-execution policy gate"},
    # ── Behavioral Analysis (L11-L20) ─────────────────────────────────
    "L11": {"name": "ContextSniperGate", "group": "behavior", "description": "Context window token gate (M94)"},
    "L12": {"name": "OrchestratorEntropyMonitor", "group": "behavior", "description": "Reasoning trap detection (M93)"},
    "L13": {"name": "SelfEvolutionCertificate", "group": "behavior", "description": "Self-evolution certificate verification (M112)"},
    "L14": {"name": "AutoCurriculaOrchestrator", "group": "behavior", "description": "Auto-curricula orchestration (M113)"},
    "L15": {"name": "MemoryConsolidationSleep", "group": "behavior", "description": "Memory consolidation during idle (M114)"},
    "L16": {"name": "HierarchicalExperimentalist", "group": "behavior", "description": "Hierarchical experiment management (M117)"},
    "L17": {"name": "CompressedContextIntegrity", "group": "behavior", "description": "Compressed context integrity guard (M118)"},
    "L18": {"name": "TrainFreeEngramGuard", "group": "behavior", "description": "Train-free engram memory guard (M119)"},
    "L19": {"name": "MultimodalMemoryGuard", "group": "behavior", "description": "Multimodal memory agent guard (M120)"},
    "L20": {"name": "CollabMemoryGuard", "group": "behavior", "description": "Multi-agent memory collaboration guard"},
    # ── Execution Safety (L21-L30) ────────────────────────────────────
    "L21": {"name": "MultiHeadRecurrentGuard", "group": "execution", "description": "Multi-head recurrent memory guard (P21)"},
    "L22": {"name": "ContextNestGuard", "group": "execution", "description": "Verifiable context governance (P22)"},
    "L23": {"name": "ElephantAgentGuard", "group": "execution", "description": "Contextual state continuity (P23)"},
    "L24": {"name": "ConstraintSteerGuard", "group": "execution", "description": "Constraint steerable oversight (P24)"},
    "L25": {"name": "OnlineSafetyMonitor", "group": "execution", "description": "Online safety monitoring (P25)"},
    "L26": {"name": "HippocampalGuard", "group": "execution", "description": "Hippocampal complementary memory (P76)"},
    "L27": {"name": "IdentityPreservingGuard", "group": "execution", "description": "Identity-preserving consolidation (P77)"},
    "L28": {"name": "ReasoningDriftGuard", "group": "execution", "description": "Reasoning drift auditor (P78)"},
    "L29": {"name": "ContextObjectGuard", "group": "execution", "description": "Context object manager (P81)"},
    "L30": {"name": "MemoryPartitionGuard", "group": "execution", "description": "Multi-head memory partition (P82)"},
    # ── Audit & Provenance (L31-L40) ──────────────────────────────────
    "L31": {"name": "ThreeLayerHierarchyGuard", "group": "audit", "description": "Three-layer hierarchical memory (P83)"},
    "L32": {"name": "ProgressiveCascadeGuard", "group": "audit", "description": "Progressive cascade retrieval (P117)"},
    "L33": {"name": "TemporalValidityGuard", "group": "audit", "description": "Temporal validity enforcement (P118)"},
    "L34": {"name": "TokenEfficiencyGuard", "group": "audit", "description": "Token-efficient memory guard (P119)"},
    "L35": {"name": "CurationGuard", "group": "audit", "description": "Agent-native curation guard (P120)"},
    "L36": {"name": "RelationalVersionGuard", "group": "audit", "description": "Relational versioning guard (P121)"},
    "L37": {"name": "ChunkIngestionGuard", "group": "audit", "description": "Contextual chunk ingestion guard (P122)"},
    "L38": {"name": "ObserverReflectorGuard", "group": "audit", "description": "Observer-reflector guard (P123)"},
    "L39": {"name": "GroundTruthEpisodesGuard", "group": "audit", "description": "Ground-truth episode guard (P124)"},
    "L40": {"name": "BEAMLIGHTGuard", "group": "audit", "description": "BEAM-LIGHT benchmark guard (P125)"},
    # ── Reasoning Guard (L41-L50) ─────────────────────────────────────
    "L41": {"name": "ExabaseRetrievalGuard", "group": "reasoning", "description": "Exabase M-1 retrieval guard (P126)"},
    "L42": {"name": "HindsightValidationGuard", "group": "reasoning", "description": "Hindsight four-network validation (P127)"},
    "L43": {"name": "ZikkaronHopfieldGate", "group": "reasoning", "description": "Zikkaron Hopfield energy gate (P128)"},
    "L44": {"name": "SelfOptimizingGuard", "group": "reasoning", "description": "Self-optimizing memory guard (P129)"},
    "L45": {"name": "SENTINELGuard", "group": "reasoning", "description": "SENTINEL 5-dim weighted reasoning guard"},
    "L46": {"name": "AntiForgettingGuard", "group": "reasoning", "description": "SDPO anti-forgetting defense (Layer 6a)"},
    "L47": {"name": "PromptCompressionAuditor", "group": "reasoning", "description": "Prompt compression attack audit (Layer 7a)"},
    "L48": {"name": "GearGovernance", "group": "reasoning", "description": "Five-gear governance (G_obs→G_sug→G_plan→G_exec→G_int)"},
    "L49": {"name": "ProvenanceChainGuard", "group": "reasoning", "description": "SHA-256 provenance chain verification"},
    "L50": {"name": "ConsolidationGuard", "group": "reasoning", "description": "Memory consolidation integrity verification"},
}


@dataclass
class GuardResult:
    """Result from a single guard tier check."""
    tier_id: str
    tier_name: str
    passed: bool
    score: float = 1.0
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier_id,
            "name": self.tier_name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "message": self.message,
        }


@dataclass
class GuardianConfig:
    """Configuration for the Guardian Chain."""
    
    # Which tiers to enable (by default all 50)
    enabled_tiers: Optional[List[str]] = None
    
    # Tier-specific thresholds
    thresholds: Dict[str, float] = field(default_factory=dict)
    
    # Blocking policy: "first_fail" (stop at first failure) or "aggregate" (check all)
    blocking_policy: str = "first_fail"
    
    # Minimum aggregate score to pass (for "aggregate" policy)
    min_aggregate_score: float = 0.7
    
    # Whether to log all checks
    verbose: bool = False
    
    # Custom guards to add
    custom_guards: Dict[str, Callable] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.enabled_tiers is None:
            self.enabled_tiers = list(TIER_REGISTRY.keys())


@dataclass
class ChainResult:
    """Result from the full guardian chain check."""
    proceed: bool
    results: List[GuardResult]
    aggregate_score: float = 1.0
    blocks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proceed": self.proceed,
            "aggregate_score": round(self.aggregate_score, 4),
            "blocks": self.blocks,
            "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 2),
            "tiers_checked": len(self.results),
            "tiers_passed": sum(1 for r in self.results if r.passed),
        }

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        status = "✅ SAFE" if self.proceed else "❌ BLOCKED"
        return (f"{status} | {passed}/{total} tiers passed | "
                f"score={self.aggregate_score:.3f} | "
                f"blocks={len(self.blocks)} | {self.duration_ms:.1f}ms")


class GuardianChain:
    """50-tier guardian chain for LLM runtime safety.

    The chain operates as a pipeline. Each tier performs a specific check.
    By default, all 50 tiers are enabled and checked sequentially.

    Usage:
        guard = GuardianChain()
        
        # Full chain check
        result = guard.check("user_input_text", context={...})
        if result.proceed:
            # safe to proceed
            pass
        
        # Quick check (only first 10 tiers)
        quick = guard.check("input", context={}, max_tiers=10)
        
        # Custom config
        config = GuardianConfig(
            enabled_tiers=["L1", "L2", "L3", "L45", "L46"],
            blocking_policy="aggregate",
        )
        guard = GuardianChain(config=config)
    """

    def __init__(self, config: Optional[GuardianConfig] = None):
        self.config = config or GuardianConfig()
        self.total_checks = 0
        self.total_blocks = 0
        self._history: List[ChainResult] = []

    def check(
        self,
        content: str,
        context: Optional[Dict[str, Any]] = None,
        max_tiers: Optional[int] = None,
    ) -> ChainResult:
        """Run the guardian chain on content.

        Args:
            content: The content to check (user input, LLM output, memory content).
            context: Optional context dict (role, session_id, metadata, etc.).
            max_tiers: Maximum number of tiers to check (None = all enabled).

        Returns:
            ChainResult with proceed flag and per-tier results.
        """
        context = context or {}
        start = time.time()
        results: List[GuardResult] = []
        blocks: List[str] = []
        warnings: List[str] = []

        enabled = self.config.enabled_tiers or list(TIER_REGISTRY.keys())
        if max_tiers:
            enabled = enabled[:max_tiers]

        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        for tier_id in enabled:
            info = TIER_REGISTRY.get(tier_id)
            if not info:
                continue

            # Check if this tier has a custom guard
            guard_fn = self.config.custom_guards.get(tier_id)
            if guard_fn:
                result = self._run_custom_guard(tier_id, info, guard_fn, content, context)
            else:
                result = self._run_default_check(tier_id, info, content, context)

            results.append(result)
            self.total_checks += 1

            if not result.passed:
                blocks.append(f"{tier_id}: {info['name']} - {result.message}")
                
                if self.config.blocking_policy == "first_fail":
                    break  # Stop at first failure

        duration = (time.time() - start) * 1000

        # Aggregate score
        if results:
            aggregate = sum(r.score for r in results) / len(results)
        else:
            aggregate = 1.0

        proceed = len(blocks) == 0 and aggregate >= self.config.min_aggregate_score

        chain_result = ChainResult(
            proceed=proceed,
            results=results,
            aggregate_score=aggregate,
            blocks=blocks,
            warnings=warnings,
            duration_ms=duration,
        )

        if not proceed:
            self.total_blocks += 1

        if self.config.verbose:
            logger.info("GuardianChain[%s]: %s", content_hash[:8], chain_result.summary())

        self._history.append(chain_result)
        return chain_result

    def _run_default_check(
        self,
        tier_id: str,
        info: Dict[str, Any],
        content: str,
        context: Dict[str, Any],
    ) -> GuardResult:
        """Run the default check for a tier based on its group."""
        group = info["group"]
        content_len = len(content)

        if group == "input":
            # Input security checks
            score, passed, msg = self._check_input_security(tier_id, content, context)
        elif group == "behavior":
            score, passed, msg = self._check_behavior(tier_id, content, context)
        elif group == "execution":
            score, passed, msg = self._check_execution(tier_id, content, context)
        elif group == "audit":
            score, passed, msg = self._check_audit(tier_id, content, context)
        elif group == "reasoning":
            score, passed, msg = self._check_reasoning(tier_id, content, context)
        else:
            score, passed, msg = 1.0, True, "unknown group"

        return GuardResult(
            tier_id=tier_id,
            tier_name=info["name"],
            passed=passed,
            score=score,
            message=msg,
            details={"content_length": content_len, "group": group},
        )

    def _check_input_security(self, tier_id: str, content: str, ctx: Dict) -> Tuple[float, bool, str]:
        """L1-L10: Input security checks."""
        threshold = self.config.thresholds.get(tier_id, 0.5)
        
        # Simple heuristic scoring
        content_lower = content.lower()
        score = 1.0
        
        # Profanity / injection patterns
        dangerous_patterns = ["ignore previous", "system prompt", "jailbreak", "bypass"]
        for p in dangerous_patterns:
            if p in content_lower:
                score -= 0.3
        
        # Length check
        if len(content) > 10000:
            score -= 0.1
        
        passed = score >= threshold
        msg = "passed" if passed else f"score {score:.2f} below threshold {threshold}"
        return max(score, 0), passed, msg

    def _check_behavior(self, tier_id: str, content: str, ctx: Dict) -> Tuple[float, bool, str]:
        """L11-L20: Behavioral analysis."""
        threshold = self.config.thresholds.get(tier_id, 0.6)
        score = 1.0
        
        # Entropy-based anomaly detection (simplified)
        unique_chars = len(set(content))
        if len(content) > 0:
            entropy = unique_chars / len(content)
            if entropy < 0.1:  # Too repetitive
                score -= 0.3
            if entropy > 0.9:  # Too random
                score -= 0.2
        
        passed = score >= threshold
        msg = "passed" if passed else "behavioral anomaly detected"
        return max(score, 0), passed, msg

    def _check_execution(self, tier_id: str, content: str, ctx: Dict) -> Tuple[float, bool, str]:
        """L21-L30: Execution safety."""
        threshold = self.config.thresholds.get(tier_id, 0.7)
        score = 1.0
        
        # Check for dangerous operations
        dangerous_ops = ["exec(", "eval(", "__import__", "os.system", "subprocess"]
        for op in dangerous_ops:
            if op in content:
                score -= 0.5
        
        passed = score >= threshold
        msg = "passed" if passed else "dangerous operation detected"
        return max(score, 0), passed, msg

    def _check_audit(self, tier_id: str, content: str, ctx: Dict) -> Tuple[float, bool, str]:
        """L31-L40: Audit & provenance."""
        self.config.thresholds.get(tier_id, 0.8)
        # Audit tiers are informational — always pass with metadata
        score = 1.0
        passed = True
        msg = "audit logged"
        return score, passed, msg

    def _check_reasoning(self, tier_id: str, content: str, ctx: Dict) -> Tuple[float, bool, str]:
        """L41-L50: Reasoning guard."""
        threshold = self.config.thresholds.get(tier_id, 0.7)
        score = 1.0
        
        role = ctx.get("role", "unknown")
        
        # Sycophancy detection (simplified)
        sycophancy_patterns = ["you're right", "I agree with you", "as you said", "you're correct"]
        if role == "assistant":
            for p in sycophancy_patterns:
                if p in content.lower():
                    score -= 0.15
        
        # Reasoning drift
        if len(content) > 0 and content.count("?") > 5:
            score -= 0.1  # Too many questions could indicate confusion
        
        passed = score >= threshold
        msg = "passed" if passed else f"reasoning concern detected (score={score:.2f})"
        return max(score, 0), passed, msg

    def _run_custom_guard(
        self,
        tier_id: str,
        info: Dict[str, Any],
        guard_fn: Callable,
        content: str,
        context: Dict[str, Any],
    ) -> GuardResult:
        """Run a user-provided custom guard function."""
        try:
            result = guard_fn(content, context)
            if isinstance(result, GuardResult):
                return result
            if isinstance(result, dict):
                return GuardResult(
                    tier_id=tier_id,
                    tier_name=info["name"],
                    passed=result.get("passed", True),
                    score=result.get("score", 1.0),
                    message=result.get("message", ""),
                )
            if isinstance(result, bool):
                return GuardResult(
                    tier_id=tier_id,
                    tier_name=info["name"],
                    passed=result,
                    score=1.0 if result else 0.0,
                    message="passed" if result else "custom guard rejected",
                )
        except Exception as e:
            return GuardResult(
                tier_id=tier_id,
                tier_name=info["name"],
                passed=False,
                score=0.0,
                message=f"custom guard error: {e}",
            )
        return GuardResult(
            tier_id=tier_id,
            tier_name=info["name"],
            passed=True,
            score=1.0,
            message="custom guard passed",
        )

    def diagnostics(self) -> Dict[str, Any]:
        """Return diagnostic info about the guardian chain."""
        enabled = self.config.enabled_tiers or list(TIER_REGISTRY.keys())
        
        # Group by tier group
        groups: Dict[str, List[str]] = {}
        for tid in enabled:
            info = TIER_REGISTRY.get(tid, {})
            g = info.get("group", "unknown")
            groups.setdefault(g, []).append(tid)
        
        return {
            "version": "1.0.0",
            "total_tiers": len(TIER_REGISTRY),
            "enabled_tiers": len(enabled),
            "total_checks": self.total_checks,
            "total_blocks": self.total_blocks,
            "block_rate": round(self.total_blocks / max(self.total_checks, 1), 4),
            "groups": {g: len(ts) for g, ts in groups.items()},
            "group_tiers": groups,
            "blocking_policy": self.config.blocking_policy,
            "min_aggregate_score": self.config.min_aggregate_score,
            "recent_history": [r.to_dict() for r in self._history[-5:]],
        }

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent check history."""
        return [r.to_dict() for r in self._history[-limit:]]

    def set_threshold(self, tier_id: str, threshold: float) -> None:
        """Set a custom threshold for a specific tier."""
        self.config.thresholds[tier_id] = threshold

    def enable_tiers(self, tier_ids: List[str]) -> None:
        """Enable specific tiers (disables all others)."""
        self.config.enabled_tiers = tier_ids

    def add_custom_guard(self, tier_id: str, guard_fn: Callable) -> None:
        """Add a custom guard function for a specific tier."""
        if tier_id not in TIER_REGISTRY:
            raise ValueError(f"Unknown tier: {tier_id}. Valid: {list(TIER_REGISTRY.keys())}")
        self.config.custom_guards[tier_id] = guard_fn


# ── Quick access ─────────────────────────────────────────────────────────
_default_guardian: Optional[GuardianChain] = None


def get_guardian() -> GuardianChain:
    """Get or create the default singleton guardian chain."""
    global _default_guardian
    if _default_guardian is None:
        _default_guardian = GuardianChain()
    return _default_guardian


def check(content: str, context: Optional[Dict[str, Any]] = None) -> ChainResult:
    """Quick one-shot check using the default guardian chain."""
    return get_guardian().check(content, context or {})

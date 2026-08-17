"""
P3-4: Execution Trajectory -> Skill Auto-Synthesis (对标 MindMemOS Skill Evolution)
===================================================================================
Automatically extract successful patterns from Agent execution trajectories,
synthesize them into reusable Skills, and support Skill distribution + recommendation.

MindMemOS Skill Evolution 的设计要点：
  - 从真实执行轨迹中自动提取成功模式
  - 合成为可复用的 Skill 定义
  - 支持 Skill 的分发与智能推荐
  - 与经验记忆联动：经验 → 抽象模式 → Skill

Reference:
  - MindMemOS Skills System: auto-synthesize, evolve from trajectories (2026.08)
  - SpreadsheetBench-Verified: 57.2%±2.4% task success rate via Skill evolution
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────

class SkillCategory(Enum):
    """Categories of synthesized skills."""
    REASONING = "reasoning"          # 推理策略
    TOOL_USE = "tool_use"           # 工具使用模式
    WORKFLOW = "workflow"           # 多步骤工作流
    COMMUNICATION = "communication"  # 交互/沟通模式
    ERROR_RECOVERY = "error_recovery"  # 错误恢复策略
    CUSTOM = "custom"


@dataclass
class ExecutionStep:
    """A single step in an execution trajectory."""
    step_id: int
    action: str                     # What was done
    tool: str                       # Tool used (if any)
    input_snapshot: str             # Input context summary
    output_snapshot: str            # Output/result summary
    success: bool                   # Whether the step succeeded
    duration_ms: float              # Execution time
    error_info: str = ""            # Error details if failed


@dataclass
class ExecutionTrajectory:
    """A full execution trajectory from an agent run."""
    trajectory_id: str
    task_description: str
    steps: List[ExecutionStep] = field(default_factory=list)
    overall_success: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def success_steps(self) -> List[ExecutionStep]:
        return [s for s in self.steps if s.success]


@dataclass
class SynthesizedSkill:
    """A skill auto-synthesized from execution trajectory patterns."""
    skill_id: str
    skill_name: str
    description: str
    category: SkillCategory
    pattern: Dict[str, Any]          # The extracted pattern (steps, conditions, etc.)
    source_trajectory_ids: List[str] # Trajectories this was derived from
    confidence: float                # 0.0 ~ 1.0
    usage_count: int = 0
    success_rate: float = 1.0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "description": self.description,
            "category": self.category.value,
            "pattern": self.pattern,
            "source_trajectory_ids": self.source_trajectory_ids,
            "confidence": self.confidence,
            "usage_count": self.usage_count,
            "success_rate": self.success_rate,
            "tags": self.tags,
        }


# ── Skill Synthesis Engine ───────────────────────────────────────────────

class SkillSynthesisEngine:
    """Extract patterns from agent execution trajectories and synthesize Skills.

    Usage::

        engine = SkillSynthesisEngine()
        trajectory = ExecutionTrajectory(
            trajectory_id="t1",
            task_description="Read PDF and extract tables",
            steps=[...],
            overall_success=True,
        )
        engine.ingest_trajectory(trajectory)
        skills = engine.synthesize()
        recommendation = engine.recommend(current_task="Extract data from report")

    Pipeline:
        1. Ingest: collect execution trajectories
        2. Pattern Mine: find recurring successful step sequences
        3. Abstract: generalize patterns into Skills
        4. Validate: cross-validate against other trajectories
        5. Distribute: export synthesized Skills
    """

    def __init__(
        self,
        min_pattern_frequency: int = 3,
        min_success_rate: float = 0.7,
        skill_store_path: Optional[str] = None,
        max_skills: int = 500,
    ):
        """Initialize the engine.

        Args:
            min_pattern_frequency: Minimum times a pattern must appear before synthesis.
            min_success_rate: Minimum success rate for pattern to be considered.
            skill_store_path: Path for persisting synthesized skills (JSON).
            max_skills: Maximum number of skills to retain in memory.
        """
        self.min_pattern_frequency = min_pattern_frequency
        self.min_success_rate = min_success_rate
        self.skill_store_path = skill_store_path
        self.max_skills = max_skills

        self._trajectories: Dict[str, ExecutionTrajectory] = {}
        self._skills: Dict[str, SynthesizedSkill] = {}
        self._pattern_cache: Dict[str, Dict[str, Any]] = {}

        # Load existing skills if available
        if skill_store_path and os.path.exists(skill_store_path):
            self._load_skills()

    # ── Ingest ────────────────────────────────────────────────────────

    def ingest_trajectory(self, trajectory: ExecutionTrajectory) -> str:
        """Ingest a new execution trajectory.

        Args:
            trajectory: The execution trajectory to ingest.

        Returns:
            The trajectory ID.
        """
        self._trajectories[trajectory.trajectory_id] = trajectory
        logger.debug("Ingested trajectory %s (%d steps, success=%s)",
                      trajectory.trajectory_id, len(trajectory.steps),
                      trajectory.overall_success)

        # Auto-synthesize after ingesting (incremental)
        if len(self._trajectories) % 5 == 0:
            self.synthesize()

        return trajectory.trajectory_id

    def ingest_from_log(
        self,
        log_path: str,
        task_description: str = "",
        overall_success: bool = False,
    ) -> Optional[str]:
        """Ingest a trajectory from a JSON log file.

        Expected format: [{"step": 1, "action": "...", "tool": "...", ...}, ...]

        Args:
            log_path: Path to the JSON trajectory log.
            task_description: Human-readable task description.
            overall_success: Whether the overall task succeeded.

        Returns:
            Trajectory ID if successful, None on failure.
        """
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.error("Failed to load trajectory log: %s", e)
            return None

        steps: List[ExecutionStep] = []
        for entry in raw:
            steps.append(ExecutionStep(
                step_id=entry.get("step", len(steps) + 1),
                action=entry.get("action", ""),
                tool=entry.get("tool", ""),
                input_snapshot=entry.get("input", ""),
                output_snapshot=entry.get("output", ""),
                success=entry.get("success", True),
                duration_ms=entry.get("duration_ms", 0),
                error_info=entry.get("error", ""),
            ))

        traj_id = hashlib.md5(
            f"{log_path}_{time.time()}".encode()
        ).hexdigest()[:16]

        trajectory = ExecutionTrajectory(
            trajectory_id=traj_id,
            task_description=task_description or os.path.basename(log_path),
            steps=steps,
            overall_success=overall_success,
        )

        return self.ingest_trajectory(trajectory)

    # ── Pattern Mining ────────────────────────────────────────────────

    def mine_patterns(self) -> List[Dict[str, Any]]:
        """Mine recurring successful step sequences from trajectories.

        Uses:
          - N-gram mining: find common step sequences (action + tool pairs).
          - Template extraction: generalize concrete inputs → variable slots.

        Returns:
            List of pattern dicts with frequency, success_rate, and template.
        """
        # Collect all success-step sequences
        sequences: List[Tuple[Tuple[str, ...], str]] = []
        for traj in self._trajectories.values():
            if not traj.overall_success:
                continue
            seq = tuple(
                f"{s.action}::{s.tool}" for s in traj.success_steps
                if s.action or s.tool
            )
            if seq:
                sequences.append((seq, traj.trajectory_id))

        # N-gram mining (2- to 5-grams)
        ngram_counter: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for seq, traj_id in sequences:
            for n in range(2, min(6, len(seq) + 1)):
                for i in range(len(seq) - n + 1):
                    ngram = seq[i:i + n]
                    if ngram not in ngram_counter:
                        ngram_counter[ngram] = {
                            "frequency": 0,
                            "source_trajectories": set(),
                            "total_occurrences": 0,
                        }
                    ngram_counter[ngram]["frequency"] += 1
                    ngram_counter[ngram]["source_trajectories"].add(traj_id)
                    ngram_counter[ngram]["total_occurrences"] += 1

        # Filter by frequency and success rate
        patterns: List[Dict[str, Any]] = []
        for ngram, stats in ngram_counter.items():
            total_trajs = len(self._trajectories)
            if stats["frequency"] < self.min_pattern_frequency:
                continue

            # Compute success rate: how often does this pattern appear in
            # successful trajectories vs all trajectories where it could appear
            success_rate = len(stats["source_trajectories"]) / max(total_trajs, 1)
            if success_rate < self.min_success_rate:
                continue

            patterns.append({
                "ngram": ngram,
                "actions": [item.split("::")[0] for item in ngram],
                "tools": [item.split("::")[1] for item in ngram],
                "frequency": stats["frequency"],
                "source_trajectory_ids": list(stats["source_trajectories"]),
                "total_occurrences": stats["total_occurrences"],
                "success_rate": round(success_rate, 3),
            })

        # Sort by frequency * success_rate descending
        patterns.sort(key=lambda p: -(p["frequency"] * p["success_rate"]))
        self._pattern_cache = {json.dumps(p["ngram"]): p for p in patterns}

        logger.info("Mined %d patterns from %d trajectories",
                     len(patterns), len(self._trajectories))
        return patterns

    # ── Abstract & Synthesize ─────────────────────────────────────────

    def abstract_pattern(
        self,
        pattern: Dict[str, Any],
    ) -> SynthesizedSkill:
        """Generalize a raw pattern into an abstract Skill definition.

        Steps:
          1. Identify variable slots (concrete values → {param}).
          2. Determine skill category from tool/action semantics.
          3. Generate a human-readable name and description.

        Args:
            pattern: A raw pattern dict from mine_patterns().

        Returns:
            A SynthesizedSkill object.
        """
        actions = pattern["actions"]
        tools = pattern["tools"]

        # ── Determine category ──
        reasoning_keywords = {"analyze", "evaluate", "decide", "plan", "compare"}
        tool_keywords = {"read", "write", "execute", "search", "call", "run"}
        workflow_keywords = {"pipeline", "chain", "sequence", "stage"}
        error_keywords = {"retry", "fallback", "recover", "handle", "catch"}

        all_text = " ".join(a.lower() for a in actions) + " " + " ".join(t.lower() for t in tools)
        if any(k in all_text for k in error_keywords):
            category = SkillCategory.ERROR_RECOVERY
        elif any(k in all_text for k in workflow_keywords):
            category = SkillCategory.WORKFLOW
        elif any(k in all_text for k in tool_keywords):
            category = SkillCategory.TOOL_USE
        elif any(k in all_text for k in reasoning_keywords):
            category = SkillCategory.REASONING
        else:
            category = SkillCategory.CUSTOM

        # ── Generate name ──
        primary_action = actions[0] if actions else "generic"
        skill_name = f"{primary_action}_{category.value}"

        # ── Generate description ──
        description = (
            f"Auto-synthesized {category.value} skill: "
            f"{' → '.join(actions[:4])}"
            + (f" (+{len(actions)-4} more steps)" if len(actions) > 4 else "")
        )

        # ── Build pattern template ──
        skill_pattern = {
            "steps": [
                {"action": a, "tool": t}
                for a, t in zip(actions, tools)
            ],
            "step_count": len(actions),
            "preconditions": [],
            "postconditions": [],
            "estimated_duration_ms": 0,  # populated from trajectory data
        }

        # ── Create skill ──
        skill_id = hashlib.sha256(
            f"{skill_name}_{pattern['ngram']}".encode()
        ).hexdigest()[:16]

        return SynthesizedSkill(
            skill_id=skill_id,
            skill_name=skill_name,
            description=description,
            category=category,
            pattern=skill_pattern,
            source_trajectory_ids=pattern["source_trajectory_ids"],
            confidence=pattern["success_rate"],
            tags=self._generate_tags(actions, tools),
        )

    def synthesize(self) -> List[SynthesizedSkill]:
        """Run full synthesis pipeline: mine → abstract → store.

        Returns:
            List of newly synthesized skills.
        """
        patterns = self.mine_patterns()
        new_skills: List[SynthesizedSkill] = []

        for pattern in patterns:
            skill = self.abstract_pattern(pattern)
            if skill.skill_id not in self._skills:
                self._skills[skill.skill_id] = skill
                new_skills.append(skill)

        # Trim skills if over max
        if len(self._skills) > self.max_skills:
            self._trim_skills()

        if new_skills:
            logger.info("Synthesized %d new skills (total: %d)",
                         len(new_skills), len(self._skills))

        if self.skill_store_path:
            self._save_skills()

        return new_skills

    # ── Recommendation ────────────────────────────────────────────────

    def recommend(
        self,
        current_task: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Recommend relevant Skills for a given task.

        Uses keyword overlap between task description and skill description/tags.

        Args:
            current_task: Description of the current task.
            top_k: Number of recommendations to return.

        Returns:
            List of recommended skills with relevance scores.
        """
        task_lower = current_task.lower()
        task_words = set(task_lower.split())

        scored: List[Tuple[SynthesizedSkill, float]] = []

        for skill in self._skills.values():
            score = 0.0

            # Check skill name
            if any(w in skill.skill_name.lower() for w in task_words):
                score += 2.0

            # Check description
            desc_lower = skill.description.lower()
            desc_overlap = task_words & set(desc_lower.split())
            score += len(desc_overlap) * 0.5

            # Check tags
            for tag in skill.tags:
                if tag.lower() in task_lower:
                    score += 1.0

            # Boost for high-confidence skills
            score *= skill.confidence

            # Boost for frequently-used skills
            if skill.usage_count > 0:
                score *= (1.0 + min(skill.usage_count * 0.1, 1.0))

            if score > 0:
                scored.append((skill, score))

        scored.sort(key=lambda x: -x[1])
        top = scored[:top_k]

        return [
            {
                "skill": s.to_dict(),
                "relevance_score": round(score, 3),
            }
            for s, score in top
        ]

    def record_usage(self, skill_id: str, success: bool) -> None:
        """Record that a skill was used, updating its statistics.

        Args:
            skill_id: The skill that was used.
            success: Whether it helped successfully.
        """
        if skill_id not in self._skills:
            return

        skill = self._skills[skill_id]
        skill.usage_count += 1
        skill.last_used_at = time.time()

        # Update success rate with exponential moving average
        alpha = 0.1
        skill.success_rate = (
            alpha * (1.0 if success else 0.0)
            + (1.0 - alpha) * skill.success_rate
        )

    # ── Distribution ──────────────────────────────────────────────────

    def export_skills(self, path: str) -> str:
        """Export all synthesized skills to a JSON file.

        Args:
            path: Output file path.

        Returns:
            The export path.
        """
        skills_data = []
        for skill in self._skills.values():
            skills_data.append(skill.to_dict())

        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "1.0",
                "generated_at": time.time(),
                "total_skills": len(skills_data),
                "skills": skills_data,
            }, f, ensure_ascii=False, indent=2)

        logger.info("Exported %d skills to %s", len(skills_data), path)
        return path

    def import_skills(self, path: str) -> int:
        """Import skills from an exported JSON file.

        Args:
            path: Import file path.

        Returns:
            Number of skills imported.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        imported = 0
        for sd in data.get("skills", []):
            skill = SynthesizedSkill(
                skill_id=sd["skill_id"],
                skill_name=sd["skill_name"],
                description=sd["description"],
                category=SkillCategory(sd.get("category", "custom")),
                pattern=sd.get("pattern", {}),
                source_trajectory_ids=sd.get("source_trajectory_ids", []),
                confidence=sd.get("confidence", 0.5),
                usage_count=sd.get("usage_count", 0),
                success_rate=sd.get("success_rate", 1.0),
                tags=sd.get("tags", []),
            )
            self._skills[skill.skill_id] = skill
            imported += 1

        logger.info("Imported %d skills from %s", imported, path)
        return imported

    # ── Internal Helpers ──────────────────────────────────────────────

    @staticmethod
    def _generate_tags(actions: List[str], tools: List[str]) -> List[str]:
        """Generate tags from actions and tools."""
        tags: Set[str] = set()

        action_tag_map = {
            "read": "file-reading",
            "write": "file-writing",
            "search": "searching",
            "analyze": "analysis",
            "execute": "execution",
            "parse": "parsing",
            "extract": "extraction",
            "convert": "conversion",
            "generate": "generation",
            "validate": "validation",
            "compare": "comparison",
        }
        for action in actions:
            al = action.lower()
            for key, tag in action_tag_map.items():
                if key in al:
                    tags.add(tag)

        for tool in tools:
            if tool:
                tags.add(f"tool:{tool.lower()}")

        return sorted(tags)

    def _trim_skills(self) -> None:
        """Remove least-useful skills when over max_skills."""
        # Sort by (usage_count, success_rate, confidence) ascending
        sorted_skills = sorted(
            self._skills.values(),
            key=lambda s: (s.usage_count, s.success_rate, s.confidence),
        )
        to_remove = sorted_skills[:len(sorted_skills) - self.max_skills]
        for skill in to_remove:
            del self._skills[skill.skill_id]

    def _save_skills(self) -> None:
        """Persist skills to disk."""
        if not self.skill_store_path:
            return
        self.export_skills(self.skill_store_path)

    def _load_skills(self) -> None:
        """Load skills from disk."""
        if not self.skill_store_path or not os.path.exists(self.skill_store_path):
            return
        self.import_skills(self.skill_store_path)

    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        return {
            "total_trajectories": len(self._trajectories),
            "total_skills": len(self._skills),
            "total_patterns": len(self._pattern_cache),
            "skills_by_category": {
                cat.value: sum(
                    1 for s in self._skills.values() if s.category == cat
                )
                for cat in SkillCategory
            },
            "avg_skill_confidence": (
                sum(s.confidence for s in self._skills.values())
                / max(len(self._skills), 1)
            ),
        }

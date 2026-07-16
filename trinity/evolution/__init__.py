"""
Trinity Meta-Evolution System
===============================
Unified self-evolution framework that integrates:
  - self-improving/ skill system (memory.md, corrections.md, heartbeat)
  - SecondBrain evolution modules (M112-M120)
  - Trinity embeddings + vector_index for semantic memory

Core loop: Observe → Analyze → Plan → Execute → Certify
"""

from trinity.evolution.core import MetaEvolution, EvolutionPhase
from trinity.evolution.skill_system import SkillSystemAdapter
from trinity.evolution.serialization import EvolutionStateSerializer
from trinity.evolution.cross_platform import CrossPlatformAdapter

__all__ = [
    "MetaEvolution",
    "EvolutionPhase",
    "SkillSystemAdapter",
    "EvolutionStateSerializer",
    "CrossPlatformAdapter",
]

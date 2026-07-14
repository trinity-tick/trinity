"""
Open-Domain Reasoning Module — bridging the gap to Hindsight 95.1% LoCoMo open-domain score.

Provides:
  1. BeliefNetwork — Structured evidence/inference separation (Hindsight-aligned)
  2. OpenDomainReasoner — Multi-step reasoning over retrieved context
  3. ContextExpander — Query decomposition + external knowledge integration
"""

from .reasoner import OpenDomainReasoner, BeliefNetwork, ContextExpander

__all__ = ["OpenDomainReasoner", "BeliefNetwork", "ContextExpander"]

"""
Second Brain engine — 122 modules for memory encoding, retrieval, reasoning, and self-evolution.

Version: v6.36
Papers: P1-P129 aligned
Guardian chain: 50-tier
Retrieval channels: 47-way
"""

from trinity.modules.second_brain.engine import (
    SecondBrainV636 as Engine,
    VERSION,
)

__all__ = ["Engine", "VERSION"]

"""
# status: orphan (2026-08-15 audit, not in runtime path)
Channel Configuration — cleans up the 47-channel retrieval system.

Original: 47 channels, of which only channels 1-4 are actively used
(11-17 references each). Channels 5-47 each have only 1-3 references
(mostly declarations with no real usage).

After cleanup: 8 core active channels + 4 enhanced channels + 3 backup
channels = 15 total (down from 47). Removed channels are automatically
aliased to the most semantically similar active channel for backward
compatibility.

Channel Categories:
  Core (1-8):      Primary retrieval pathways (always active)
  Enhanced (33-38): Specialized retrieval modes (conditionally active)
  Backup (45-47):  Fallback pathways (active on retry)
  Removed (5-32, 39-44): Aliased to core equivalents
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Channel Registry
# ═══════════════════════════════════════════════════════════════════════

# Active channels with descriptions
ACTIVE_CHANNELS: Dict[int, str] = {
    # Core retrieval pathways (always active)
    1: "semantic_dense",       # Primary: FAISS HNSW dense vector search
    2: "keyword_sparse",       # Primary: BM25 sparse keyword search
    3: "semantic_sparse",      # Hybrid: Combined semantic + keyword
    4: "temporal_context",     # Context: Time-weighted retrieval
    5: "importance_gate",      # Gate: Importance-thresholded retrieval
    6: "recency_bias",         # Bias: Recency-weighted retrieval
    7: "category_filter",      # Filter: Category-specific retrieval
    8: "cross_encoder_rerank", # Rerank: Cross-Encoder precision reranking

    # Enhanced retrieval modes (conditionally active)
    33: "multi_hop",           # Graph: Multi-hop semantic graph traversal
    34: "contrastive_neg",     # Contrast: Negative example contrastive search
    35: "query_decompose",     # Decompose: Multi-query decomposition
    36: "hypothesis_doc",      # Hypothetical: Generate-then-retrieve (HyDE)
    37: "cross_modal",         # Modal: Cross-modal (text↔image) retrieval
    38: "temporal_pattern",    # Pattern: Temporal pattern matching

    # Backup fallback pathways (active on retry)
    45: "exact_match",         # Exact: Exact string match fallback
    46: "brute_force",         # Brute: Full scan brute force fallback
    47: "rerank_fallback",     # Fallback: Simple reranking fallback
}

# Inactive channels → mapped to their active equivalent
CHANNEL_ALIASES: Dict[int, int] = {
    # Ch 5-32: General retrieval → mapped to core (1-8)
    9: 1,   10: 1,   11: 1,   12: 1,
    13: 2,  14: 2,   15: 3,   16: 3,
    17: 4,  18: 4,   19: 5,   20: 5,
    21: 6,  22: 6,   23: 7,   24: 7,
    25: 8,  26: 8,   27: 1,   28: 2,
    29: 3,  30: 4,   31: 5,   32: 6,

    # Ch 39-44: Enhanced retrieval → mapped to enhanced (33-38)
    39: 33,  40: 34,  41: 35,  42: 36,  43: 37,  44: 38,
}

ACTIVE_CHANNEL_IDS: Set[int] = set(ACTIVE_CHANNELS.keys())
ACTIVE_CHANNEL_NAMES: Set[str] = set(ACTIVE_CHANNELS.values())

# ═══════════════════════════════════════════════════════════════════════
#  Query Functions
# ═══════════════════════════════════════════════════════════════════════


def is_channel_active(channel_id: int) -> bool:
    """Check if a channel is active."""
    return channel_id in ACTIVE_CHANNEL_IDS


def resolve_channel(channel_id: int) -> int:
    """Resolve a channel ID to its active equivalent.

    If the channel is already active, returns it unchanged.
    If it has been removed, returns its alias.
    If no alias exists, returns channel 1 (default).
    """
    if channel_id in ACTIVE_CHANNEL_IDS:
        return channel_id
    return CHANNEL_ALIASES.get(channel_id, 1)


def resolve_channel_name(channel_id: int) -> str:
    """Get the name/description of a resolved channel."""
    resolved = resolve_channel(channel_id)
    return ACTIVE_CHANNELS.get(resolved, "unknown")


def get_active_channels(
    include_enhanced: bool = False,
    include_backup: bool = False,
) -> Dict[int, str]:
    """Get the list of active channels.

    Args:
        include_enhanced: Include channels 33-38 (specialized retrieval).
        include_backup: Include channels 45-47 (fallback pathways).

    Returns:
        Dict of {channel_id: channel_name}.
    """
    result = {k: v for k, v in ACTIVE_CHANNELS.items() if k <= 8}

    if include_enhanced:
        for k, v in ACTIVE_CHANNELS.items():
            if 33 <= k <= 38:
                result[k] = v

    if include_backup:
        for k, v in ACTIVE_CHANNELS.items():
            if 45 <= k <= 47:
                result[k] = v

    return result


def get_channel_count() -> int:
    """Return the effective number of active channels."""
    return len(ACTIVE_CHANNEL_IDS)


def get_removed_count() -> int:
    """Return the number of channels that were removed (aliased)."""
    return len(CHANNEL_ALIASES)


# ═══════════════════════════════════════════════════════════════════════
#  Statistics
# ═══════════════════════════════════════════════════════════════════════


def statistics() -> dict:
    """Return channel configuration statistics."""
    return {
        "total_original_channels": 47,
        "total_active_channels": len(ACTIVE_CHANNEL_IDS),
        "total_aliased_channels": len(CHANNEL_ALIASES),
        "core_active": get_active_channels(include_enhanced=False, include_backup=False),
        "enhanced_active": get_active_channels(include_enhanced=True, include_backup=False),
        "with_backup_active": get_active_channels(include_enhanced=True, include_backup=True),
        "reduction": f"{len(ACTIVE_CHANNEL_IDS)}/{47} active ({(1 - len(ACTIVE_CHANNEL_IDS)/47)*100:.0f}% reduction)",
    }

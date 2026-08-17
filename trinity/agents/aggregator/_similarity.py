"""MemoryAggregator - similarity / tokenization helpers mixin (split from aggregator.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

from ._constants import logger


class _SimilarityMixin:

    @staticmethod
    def _tokenize(content: str) -> Set[str]:
        """Tokenize content into a set of normalized terms for Jaccard."""
        import re
        tokens = re.findall(r'[a-zA-Z0-9_\u4e00-\u9fff]+', content.lower())
        # Filter very short tokens
        return {t for t in tokens if len(t) >= 2}

    @staticmethod
    def _jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
        """Compute Jaccard similarity coefficient."""
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union

    @staticmethod
    def _content_similarity(a: str, b: str) -> float:
        """Simple word-level Jaccard similarity on first 200 words."""
        wa = set(a.lower().split()[:200])
        wb = set(b.lower().split()[:200])
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

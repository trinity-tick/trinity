"""Module-level constants for the MemoryAggregator package (split from aggregator.py, 2026-08-17).
Part of the MemoryAggregator package decomposition. The values are byte-identical to the
pre-split single-file implementation; the package __init__ re-exports them so that
from trinity.agents.aggregator import <CONST> keeps working.
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

logger = logging.getLogger("trinity.agents.aggregator")

SIMILARITY_MERGE_THRESHOLD = 0.75
MAX_POOL_SIZE = 100000
PERSIST_FILENAME = "aggregator_pool.json"
PERSIST_DEBOUNCE_SECONDS = 2.0  # Delay save after last write
PERSIST_MAX_DIRTY = 50  # Force save after N dirty writes
VECTOR_PERSIST_FILENAME = "aggregator_vectors.pkl"
CLEANUP_INTERVAL_SECONDS = 300  # Daemon cleanup every 5 min

# Optional FAISS import
try:
    import faiss
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False
    logger.info("faiss not installed; using numpy cosine fallback for vector search")

# Internal sentinel: distinguish "not passed" (auto-discover) from "None" (disable persistence)
_SENTINEL = object()

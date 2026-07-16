"""
Trinity Shared Utilities
========================
Centralized utility functions extracted from duplicated implementations across
engine.py modules. Used by: ProgressiveCascade, TokenEfficientMemory,
ContextualChunkIngestion, GroundTruthEpisodes, BEAMLIGHT, ExabaseRetrieval,
RelationalVersioning, ZikkaronHopfield, and others.
"""

import re
import math
import random
from typing import List, Optional

# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

_STOP_WORDS: set = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because", "but", "and",
    "or", "if", "while", "although", "i", "you", "he", "she", "it", "we",
    "they", "me", "him", "her", "us", "them", "this", "that", "these",
    "those", "my", "your", "his", "its", "our", "their", "what", "which",
    "who", "whom", "about", "up", "down",
    # Chinese stop words
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那",
    "什么", "吗", "吧", "啊", "呢", "哦", "啦", "呀", "嘛", "嗯",
    "因为", "所以", "但是", "而且", "如果", "虽然", "然后", "可以",
    "已经", "还是", "或者", "不过", "比如", "让", "被", "把", "从",
}


def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text by tokenizing and filtering stop words."""
    import re
    # Normalize: lowercase, remove punctuation (keep CJK characters)
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", text.lower())
    tokens = cleaned.split()
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


# ---------------------------------------------------------------------------
# Embedding helpers (simple statistical / TF-IDF-like)
# ---------------------------------------------------------------------------

def encode_to_embedding(text: str, dim: int = 64) -> List[float]:
    """Generate a deterministic pseudo-embedding via hashing character n-grams.

    This is a lightweight stand-in when no external embedding model is
    available.  Collisions are possible; for production use an actual
    embedding model (e.g. via the ``embeddings`` engine).
    """
    import hashlib
    rng = random.Random(hashlib.md5(text.encode("utf-8")).digest())
    return [rng.random() for _ in range(dim)]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors with zero-division guard."""
    dot = sum(va * vb for va, vb in zip(a, b))
    na = math.sqrt(sum(v * v for v in a))
    nb = math.sqrt(sum(v * v for v in b))
    if na * nb == 0:
        return 0.0
    return dot / (na * nb)


def jaccard_similarity(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

def normalize_and_tokenize(text: str) -> List[str]:
    """Lowercase, remove punctuation, split into tokens."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return [t for t in cleaned.split() if t]


def compute_signature(text: str, length: int = 8) -> str:
    """Short deterministic hash signature for deduplication."""
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Temporal helpers
# ---------------------------------------------------------------------------

def estimate_token_count(text: str) -> int:
    """Rough token count estimate (~4 chars per token for English)."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Diagnostics template helpers
# ---------------------------------------------------------------------------

def diagnostics_collect(
    obj,  # instance
    bool_checks: List[tuple],  # [(attribute_or_method_name, label), ...]
) -> dict:
    """Collect a diagnostics dict from a list of (name, label) boolean checks.

    Each tuple is ``(name, label)`` where *name* is an attribute or zero-arg
    callable on *obj* and *label* is the key in the returned dict.

    Example::

        diag = diagnostics_collect(self, [
            ("memory_pool", "CB54_memory_pool"),
            ("phase1_scoring", "CB54_phase1_scoring"),
        ])
    """
    result = {}
    for name, label in bool_checks:
        attr = getattr(obj, name, None)
        if callable(attr):
            result[label] = bool(attr())
        else:
            result[label] = bool(attr)
    return result

"""
Trinity Semantic Embedding Engine
==================================
Replaces SHA-256 hash-based pseudo-embeddings with real semantic embeddings
from local Ollama models (bge-m3, qwen3-embedding) or scikit-learn TF-IDF fallback.

Architecture:
  - Abstract base: EmbeddingEngine (统一接口)
  - OllamaEmbeddingEngine: 通过 Ollama REST API 调用本地模型
  - SklearnEmbeddingEngine: 基于 scikit-learn TfidfVectorizer 的轻量降级方案
  - CachedEmbeddingEngine: 带嵌入缓存的装饰器引擎
  - Factory: create_engine() 一键创建

All engines produce L2-normalized float32 numpy arrays with configurable dim.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_EMBED_MODEL = "bge-m3"       # 最佳通用嵌入 (1024d)
FALLBACK_EMBED_MODEL = "qwen3-embedding:0.6b"  # 轻量选择 (768d or 1536d)
MINIMAL_MODEL = "qwen3:0.6b"         # 超轻量fallback

EMBEDDING_CACHE_SIZE = 10000         # LRU 缓存大小
DEFAULT_EMBED_DIM = 1024             # bge-m3 默认维度

# ── Abstract Engine ────────────────────────────────────────────────────

class EmbeddingEngine(ABC):
    """Abstract base for all embedding engines."""

    @abstractmethod
    def embed(self, text: str) -> np.ndarray:
        """Embed a single text string. Returns L2-normalized float32 array."""
        ...

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Embed a batch of texts. Default: sequential fallback."""
        return [self.embed(t) for t in texts]

    @abstractmethod
    def embedding_dim(self) -> int:
        """Return the dimensionality of produced embeddings."""
        ...

    @abstractmethod
    def model_name(self) -> str:
        """Return the name of the underlying model."""
        ...

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two normalized embeddings."""
        return float(np.dot(a, b))

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "engine": self.__class__.__name__,
            "model": self.model_name(),
            "dim": self.embedding_dim(),
        }


# ── Ollama Embedding Engine ────────────────────────────────────────────

class OllamaEmbeddingEngine(EmbeddingEngine):
    """Real semantic embeddings via Ollama REST API.

    Supports any model registered in Ollama (bge-m3 recommended).
    Uses requests library to call /api/embed endpoint.
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout: int = 30,
        dim: Optional[int] = None,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._dim = dim
        self._loaded = False
        self._total_embeddings = 0
        self._total_time_ms = 0.0
        self._errors = 0
        self._last_error: Optional[str] = None

    def _ensure_imports(self):
        try:
            import requests
        except ImportError:
            raise RuntimeError(
                "requests library required for OllamaEmbeddingEngine. "
                "Install with: pip install requests"
            )
        return requests

    @property
    def _requests(self):
        return self._ensure_imports()

    def _call_ollama_api(self, texts: List[str]) -> List[List[float]]:
        """Call /api/embed endpoint with a batch of texts."""
        requests = self._requests
        try:
            resp = requests.post(
                f"{self._base_url}/api/embed",
                json={
                    "model": self._model,
                    "input": texts,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings", [])
            if not embeddings:
                raise RuntimeError(f"Empty embeddings response from Ollama: {data}")
            return embeddings
        except Exception as e:
            self._errors += 1
            self._last_error = str(e)
            raise

    def embed(self, text: str) -> np.ndarray:
        t0 = time.time()
        try:
            raw = self._call_ollama_api([text])
            vec = np.array(raw[0], dtype=np.float32)
        except Exception:
            # Fallback: use hash-based embedding on error
            vec = self._hash_fallback(text)
        # L2 normalize
        norm = np.linalg.norm(vec)
        if norm > 1e-8:
            vec = vec / norm
        self._total_embeddings += 1
        self._total_time_ms += (time.time() - t0) * 1000
        return vec

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Batch embedding - much faster than sequential."""
        t0 = time.time()
        try:
            raw_list = self._call_ollama_api(texts)
            vecs = [np.array(r, dtype=np.float32) for r in raw_list]
        except Exception:
            # Fallback per-item
            vecs = [self._hash_fallback(t) for t in texts]
        # Normalize
        result = []
        for v in vecs:
            norm = np.linalg.norm(v)
            if norm > 1e-8:
                v = v / norm
            result.append(v)
        self._total_embeddings += len(result)
        self._total_time_ms += (time.time() - t0) * 1000
        return result

    def _hash_fallback(self, text: str) -> np.ndarray:
        """SHA-256 based fallback embedding (same as original Trinity)."""
        d = self.embedding_dim()
        h = hashlib.sha256(text.encode()).digest()
        # Repeat hash if dimension > 32
        raw = []
        for i in range(d):
            raw.append(h[i % 32] / 255.0)
        vec = np.array(raw, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 1e-8:
            vec = vec / norm
        return vec

    def embedding_dim(self) -> int:
        if self._dim is not None:
            return self._dim
        # Try to determine from model
        model_dim_map = {
            "bge-m3": 1024,
            "bge-large": 1024,
            "bge-small": 384,
            "qwen3-embedding": 1536,
            "qwen3-embedding:0.6b": 1536,
            "qwen3:0.6b": 768,
            "qwen3:8b": 7168,
            "qwen2.5:7b": 3584,
            "qwen2.5:3b": 2048,
            "nomic-embed-text": 768,
            "mxbai-embed-large": 1024,
            "snowflake-arctic-embed": 1024,
        }
        for key, d in model_dim_map.items():
            if key in self._model.lower():
                return d
        return 1024  # safe default

    def model_name(self) -> str:
        return self._model

    def diagnostics(self) -> Dict[str, Any]:
        d = super().diagnostics()
        avg_ms = self._total_time_ms / max(1, self._total_embeddings)
        d.update({
            "total_embeddings": self._total_embeddings,
            "avg_latency_ms": round(avg_ms, 2),
            "errors": self._errors,
            "last_error": self._last_error,
            "base_url": self._base_url,
            "timeout": self._timeout,
        })
        return d


# ── Scikit-learn TF-IDF Embedding Engine (lightweight fallback) ───────

class OnnxEmbeddingEngine(EmbeddingEngine):
    """bge-m3 内镶引擎（2026-08-25）：onnxruntime 进程内推理，不依赖外部 Ollama。

    - 模型：hooman650/bge-m3-onnx-o4（量化版 ~1.08GB，1024d）
    - 目录：~/.trinity/models/bge-m3-onnx/（scripts/pull_bge_m3_onnx.py 下载）
    - 推理：onnxruntime CPU（Int8 量化，快于 Ollama API 往返）
    - 输入：transformers tokenizer（sentencepiece，max 8192）
    - 输出：L2 归一化 float32 1024d（与 Ollama bge-m3 一致）
    """

    DEFAULT_DIR = os.path.expanduser("~/.trinity/models/bge-m3-onnx")

    def __init__(self, model_dir: Optional[str] = None,
                 max_length: int = 8192, providers: Optional[list] = None):
        self._model_dir = model_dir or self.DEFAULT_DIR
        self._max_length = max_length
        self._providers = providers or ["CPUExecutionProvider"]
        self._session = None
        self._tokenizer = None
        self._input_names = None
        self._total = 0
        self._init_lock = threading.Lock()  # 2026-09: 预热线程与首请求并发安全

    def _lazy_init(self):
        if self._session is not None:
            return
        with self._init_lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from transformers import AutoTokenizer
            model_path = os.path.join(self._model_dir, "model_optimized.onnx")
            if not os.path.exists(model_path):
                raise RuntimeError(
                    f"bge-m3 ONNX 模型缺失: {self._model_dir} —— "
                    f"运行 python scripts/pull_bge_m3_onnx.py 下载")
            # 2026-09（EXECUTION 104.7）：SessionOptions 调优——graph_optimization_level
            # ALL + intra_op_num_threads=8 实测批量 29ms/条（-25%）、单条 94ms（-21%）；
            # 线程数过高反而恶化（t56 实测 522ms/条）；TRINITY_ONNX_THREADS 可覆盖。
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            _ths = os.environ.get("TRINITY_ONNX_THREADS", "8")
            so.intra_op_num_threads = int(_ths) if _ths.isdigit() else 8
            self._session = ort.InferenceSession(model_path, sess_options=so, providers=self._providers)
            self._tokenizer = AutoTokenizer.from_pretrained(self._model_dir)
            self._input_names = [i.name for i in self._session.get_inputs()]

    def _tokenize(self, text: str) -> dict:
        enc = self._tokenizer(
            text, max_length=self._max_length, truncation=True,
            padding=True, return_tensors="np")
        feeds = {}
        for name in self._input_names:
            lname = name.lower()
            if "input_ids" in lname:
                feeds[name] = enc["input_ids"]
            elif "attention" in lname or "mask" in lname:
                feeds[name] = enc["attention_mask"]
            elif "token_type" in lname:
                feeds[name] = enc.get("token_type_ids", enc["input_ids"] * 0)
        return feeds

    def embed(self, text: str) -> np.ndarray:
        self._lazy_init()
        feeds = self._tokenize(text)
        hidden = self._session.run(None, feeds)[0]  # (batch, seq, 1024)
        # 2026-08-25：bge-m3 ONNX 输出 last_hidden_state（每 token 向量）——
        # 用 CLS token（首个 token）池化 + L2 归一化（bge 系列惯例）
        vec = np.asarray(hidden[0, 0], dtype=np.float32)  # CLS pooling
        norm = np.linalg.norm(vec)
        self._total += 1
        return vec / norm if norm > 1e-8 else vec

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        # 2026-09（EXECUTION 104.7）：真批量推理——单次 tokenize + 单次 session.run
        # （原实现逐条 embed 串行；实测 20 条 batch 29ms/条 vs 串行 101ms/条）。
        # 空输入/单条仍走原路径；失败条目以零向量兜底（与旧语义一致）。
        if not texts:
            return []
        self._lazy_init()
        if len(texts) == 1:
            try:
                return [self.embed(texts[0])]
            except Exception:
                return [np.zeros(1024, dtype=np.float32)]
        try:
            enc = self._tokenizer(
                texts, max_length=self._max_length, truncation=True,
                padding=True, return_tensors="np")
            feeds = {}
            for name in self._input_names:
                lname = name.lower()
                if "input_ids" in lname:
                    feeds[name] = enc["input_ids"]
                elif "attention" in lname or "mask" in lname:
                    feeds[name] = enc["attention_mask"]
                elif "token_type" in lname:
                    feeds[name] = enc.get("token_type_ids", enc["input_ids"] * 0)
            hidden = self._session.run(None, feeds)[0]  # (batch, seq, 1024)
            out = []
            for i in range(hidden.shape[0]):
                v = np.asarray(hidden[i, 0], dtype=np.float32)  # CLS pooling
                norm = np.linalg.norm(v)
                out.append(v / norm if norm > 1e-8 else v)
            self._total += len(out)
            return out
        except Exception:
            # 兜底：逐条重试（与旧行为一致，单条失败零向量）
            out = []
            for t in texts:
                try:
                    out.append(self.embed(t))
                except Exception:
                    out.append(np.zeros(1024, dtype=np.float32))
            return out

    def embedding_dim(self) -> int:
        return 1024

    def model_name(self) -> str:
        return "bge-m3-onnx"

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "engine": self.__class__.__name__,
            "model": self.model_name(),
            "dim": self.embedding_dim(),
            "model_dir": self._model_dir,
            "total_embeddings": self._total,
            "providers": self._providers,
        }


class SklearnEmbeddingEngine(EmbeddingEngine):
    """Lightweight embedding using scikit-learn TfidfVectorizer.

    Good for small vocabularies and offline operation.
    Dim = vocabulary size (configurable via max_features).
    """

    def __init__(self, max_features: int = 1024, ngram_range: Tuple[int, int] = (1, 2)):
        self._max_features = max_features
        self._ngram_range = ngram_range
        self._vectorizer = None
        self._fitted = False
        self._total_embeddings = 0

    def _lazy_init(self, texts: List[str]):
        if self._fitted:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:
            raise RuntimeError("scikit-learn required for SklearnEmbeddingEngine")
        self._vectorizer = TfidfVectorizer(
            max_features=self._max_features,
            ngram_range=self._ngram_range,
            analyzer="char_wb",
            sublinear_tf=True,
        )
        # Fit on provided texts or use a default corpus
        corpus = texts or [
            "memory retrieval search query", "user preference setting",
            "temporal time date", "entity person organization",
            "semantic similarity embedding",
        ]
        self._vectorizer.fit(corpus)
        self._fitted = True

    def embed(self, text: str) -> np.ndarray:
        self._lazy_init([text])
        vec = self._vectorizer.transform([text]).toarray()[0].astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 1e-8:
            vec = vec / norm
        self._total_embeddings += 1
        return vec

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Vectorized batch embedding — transform all texts in one call."""
        if not texts:
            return []
        self._lazy_init(texts)
        rows = self._vectorizer.transform(texts).toarray().astype(np.float32)
        result = []
        for v in rows:
            norm = np.linalg.norm(v)
            if norm > 1e-8:
                v = v / norm
            result.append(v)
        self._total_embeddings += len(result)
        return result

    def embedding_dim(self) -> int:
        return self._max_features

    def model_name(self) -> str:
        return f"sklearn_tfidf_cw_{self._max_features}d"


# ── Cached Embedding Engine (decorator) ───────────────────────────────

class CachedEmbeddingEngine(EmbeddingEngine):
    """LRU-cached wrapper around any EmbeddingEngine.

    Reduces API calls for repeated texts. Thread-safe with lock.
    """

    def __init__(self, engine: EmbeddingEngine, cache_size: int = EMBEDDING_CACHE_SIZE):
        self._engine = engine
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_size = cache_size
        self._hits = 0
        self._misses = 0

    def embed(self, text: str) -> np.ndarray:
        # Use text hash as cache key to avoid storing long strings
        key = hashlib.md5(text.encode()).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
        vec = self._engine.embed(text)
        if len(self._cache) >= self._cache_size:
            self._cache.popitem(last=False)
        self._cache[key] = vec
        self._misses += 1
        return vec

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        results = []
        uncached_indices = []
        uncached_texts = []

        for i, text in enumerate(texts):
            key = hashlib.md5(text.encode()).hexdigest()
            if key in self._cache:
                self._cache.move_to_end(key)
                results.append(self._cache[key])
                self._hits += 1
            else:
                results.append(None)
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            batch_vecs = self._engine.embed_batch(uncached_texts)
            for idx, vec in zip(uncached_indices, batch_vecs):
                key = hashlib.md5(texts[idx].encode()).hexdigest()
                if len(self._cache) >= self._cache_size:
                    self._cache.popitem(last=False)
                self._cache[key] = vec
                results[idx] = vec
                self._misses += 1

        return results

    def embedding_dim(self) -> int:
        return self._engine.embedding_dim()

    def model_name(self) -> str:
        return f"cached({self._engine.model_name()})"

    def cache_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(1, total) * 100, 1),
            "cache_size": len(self._cache),
            "max_size": self._cache_size,
        }

    def diagnostics(self) -> Dict[str, Any]:
        d = super().diagnostics()
        d["cache"] = self.cache_stats()
        d["wrapped_engine"] = self._engine.diagnostics()
        return d


# ── Fusion Embedding Engine (multi-engine fusion) ──────────────────────

class FusionEmbeddingEngine(EmbeddingEngine):
    """Multi-engine fusion embedding engine.

    Combines multiple embedding engines by concatenating their normalized
    vectors, preserving information from each source. Supports per-engine
    weights for balanced or weighted fusion.

    Example:
        engine = FusionEmbeddingEngine(
            engines=[OllamaEmbeddingEngine(), SklearnEmbeddingEngine()],
            weights=[0.7, 0.3],
        )
    """

    def __init__(
        self,
        engines: Optional[List[EmbeddingEngine]] = None,
        weights: Optional[List[float]] = None,
    ):
        if engines is None:
            # Auto-initialize: Ollama (bge-m3) + sklearn fallback
            engines = self._auto_init_engines()
        self._engines = engines

        if weights is not None:
            if len(weights) != len(engines):
                raise ValueError(
                    f"Number of weights ({len(weights)}) must match number of "
                    f"engines ({len(engines)})"
                )
            # Normalize weights to sum to 1
            w = np.array(weights, dtype=np.float32)
            w = w / w.sum()
            self._weights = w.tolist()
        else:
            self._weights = [1.0] * len(engines)

        self._total_dim = sum(e.embedding_dim() for e in engines)
        self._total_embeddings = 0
        self._errors = 0

    @staticmethod
    def _auto_init_engines() -> List[EmbeddingEngine]:
        """Auto-initialize with Ollama bge-m3 + sklearn fallback."""
        engines: List[EmbeddingEngine] = []
        try:
            import requests
            resp = requests.get(
                f"{DEFAULT_OLLAMA_BASE_URL}/api/tags",
                timeout=3,
            )
            if resp.status_code == 200:
                engines.append(OllamaEmbeddingEngine(model=DEFAULT_EMBED_MODEL))
        except Exception:
            pass
        engines.append(SklearnEmbeddingEngine())
        return engines

    def embed(self, text: str) -> np.ndarray:
        """Return fused embedding: concatenate (weighted + normalized) sub-vectors."""
        parts = []
        for engine, weight in zip(self._engines, self._weights):
            try:
                vec = engine.embed(text)
            except Exception as e:
                self._errors += 1
                # Fallback: zero vector of appropriate dimension
                vec = np.zeros(engine.embedding_dim(), dtype=np.float32)
            # Apply weight factor (sqrt scaling for concatenation)
            # Weight affects the magnitude within the concatenated space
            if weight != 1.0:
                vec = vec * weight
            parts.append(vec)

        fused = np.concatenate(parts)
        # L2 normalize the fused vector
        norm = np.linalg.norm(fused)
        if norm > 1e-8:
            fused = fused / norm
        self._total_embeddings += 1
        return fused

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Batch embed with fusion."""
        # Get per-engine batch results
        engine_results: List[List[np.ndarray]] = []
        for engine in self._engines:
            try:
                vecs = engine.embed_batch(texts)
            except Exception:
                vecs = [np.zeros(engine.embedding_dim(), dtype=np.float32) for _ in texts]
            engine_results.append(vecs)

        # Fuse
        results = []
        for i in range(len(texts)):
            parts = []
            for j, engine in enumerate(self._engines):
                vec = engine_results[j][i]
                w = self._weights[j]
                if w != 1.0:
                    vec = vec * w
                parts.append(vec)
            fused = np.concatenate(parts)
            norm = np.linalg.norm(fused)
            if norm > 1e-8:
                fused = fused / norm
            results.append(fused)

        self._total_embeddings += len(texts)
        return results

    def embedding_dim(self) -> int:
        return self._total_dim

    def model_name(self) -> str:
        names = [e.model_name() for e in self._engines]
        return f"fusion({' + '.join(names)}):{self._total_dim}d"

    def get_engine_info(self) -> List[Dict[str, Any]]:
        """Return diagnostics for each sub-engine."""
        info = []
        for i, (engine, w) in enumerate(zip(self._engines, self._weights)):
            d = engine.diagnostics()
            d["fusion_weight"] = round(w, 4)
            d["fusion_index"] = i
            info.append(d)
        return info

    def diagnostics(self) -> Dict[str, Any]:
        d = super().diagnostics()
        d["num_engines"] = len(self._engines)
        d["weights"] = [round(w, 4) for w in self._weights]
        d["total_embeddings"] = self._total_embeddings
        d["errors"] = self._errors
        d["sub_engines"] = self.get_engine_info()
        return d


# ── Factory ────────────────────────────────────────────────────────────

# 2026-09（EXECUTION 104.9）：auto 后端默认参数单例——防每次请求新建
# ONNX 实例（/vector/search 等端点曾每次 create_engine → 每次加载 1.9GB
# session + 24s，并发即内存爆炸，实测 13GB 卡死）。显式 backend/kwargs
# 不缓存（行为不变）；进程内 env 固定，单例安全。
_AUTO_SINGLETON: Optional[EmbeddingEngine] = None
_AUTO_SINGLETON_LOCK = threading.Lock()
_AUTO_SINGLETON_BUILDING = False


def _get_auto_singleton(use_cache: bool) -> EmbeddingEngine:
    """返回 auto 后端共享引擎（线程安全单例，仅默认参数）。"""
    global _AUTO_SINGLETON, _AUTO_SINGLETON_BUILDING
    if _AUTO_SINGLETON is not None:
        return _AUTO_SINGLETON
    with _AUTO_SINGLETON_LOCK:
        if _AUTO_SINGLETON is None:
            _AUTO_SINGLETON_BUILDING = True
            try:
                _AUTO_SINGLETON = create_engine(backend="auto", use_cache=use_cache)
            finally:
                _AUTO_SINGLETON_BUILDING = False
    return _AUTO_SINGLETON


def create_engine(
    backend: str = "auto",
    model: Optional[str] = None,
    use_cache: bool = True,
    cache_size: int = EMBEDDING_CACHE_SIZE,
    **kwargs,
) -> EmbeddingEngine:
    """Create an embedding engine with the specified backend.

    Args:
        backend: One of "auto", "ollama", "sklearn", "hash".
                 "auto" tries Ollama, falls back to sklearn.
        model: Ollama model name (only for "ollama" backend).
        use_cache: Wrap with CachedEmbeddingEngine.
        cache_size: LRU cache size.

    Returns:
        Configured EmbeddingEngine instance.
    """
    # 2026-09（EXECUTION 104.9）：auto + 默认参数（无 kwargs）→ 共享单例
    if backend == "auto" and use_cache and not kwargs and not _AUTO_SINGLETON_BUILDING:
        return _get_auto_singleton(use_cache=use_cache)
    if backend == "hash":
        # Original Trinity behavior for comparison
        class HashEngine(EmbeddingEngine):
            def embed(self, text):
                h = hashlib.sha256(text.encode()).digest()
                vec = np.array([b/255.0 for b in h[:32]], dtype=np.float32)
                norm = np.linalg.norm(vec)
                return vec / norm if norm > 1e-8 else vec
            def embedding_dim(self): return 32
            def model_name(self): return "sha256_hash"
        engine = HashEngine()

    elif backend == "onnx":
        engine = OnnxEmbeddingEngine(**kwargs)

    elif backend == "sklearn":
        engine = SklearnEmbeddingEngine(**kwargs)

    elif backend == "ollama":
        engine = OllamaEmbeddingEngine(
            model=model or DEFAULT_EMBED_MODEL,
            **kwargs,
        )

    elif backend == "fusion":
        engines = kwargs.pop("engines", None)
        weights = kwargs.pop("weights", None)
        engine = FusionEmbeddingEngine(engines=engines, weights=weights)
        # Fusion engine already handles its own sub-engines;
        # user might still want caching on top
        if use_cache:
            engine = CachedEmbeddingEngine(engine, cache_size=cache_size)
        return engine  # early return to avoid double-wrapping below

    elif backend == "auto":
        # 2026-09（Ollama 解耦，dsh-ops/EXECUTION.md 记录）：TRINITY_EMBED_BACKEND=onnx
        # 时直接走进程内 ONNX bge-m3（跳过 Ollama 探测，零外部依赖）；未设置时维持
        # 原 auto 探测链（Ollama → ONNX → sklearn 降级）。
        _forced = os.environ.get("TRINITY_EMBED_BACKEND", "").strip().lower()
        if _forced in ("onnx", "bge-m3-onnx"):
            engine = OnnxEmbeddingEngine(**{
                k: v for k, v in kwargs.items()
                if k in ("model_dir", "max_length", "providers")
            })
        else:
            try:
                import requests
                # Quick health check
                resp = requests.get(
                    f"{kwargs.get('base_url', DEFAULT_OLLAMA_BASE_URL)}/api/tags",
                    timeout=3,
                )
                if resp.status_code == 200:
                    engine = OllamaEmbeddingEngine(
                        model=model or DEFAULT_EMBED_MODEL,
                        **{k: v for k, v in kwargs.items() if k != 'base_url'},
                    )
                else:
                    raise RuntimeError("Ollama not responding")
            except Exception:
                # 2026-08-25（内镶）：Ollama 不可用时优先 bge-m3 ONNX
                # （进程内推理）；模型缺失才回退 sklearn（128d 降级）。
                if os.path.exists(os.path.join(OnnxEmbeddingEngine.DEFAULT_DIR,
                                               "model_optimized.onnx")):
                    engine = OnnxEmbeddingEngine(**kwargs)
                else:
                    engine = SklearnEmbeddingEngine(**kwargs)

    else:
        raise ValueError(f"Unknown backend: {backend}. Choose from: auto, ollama, sklearn, hash")

    if use_cache:
        engine = CachedEmbeddingEngine(engine, cache_size=cache_size)

    return engine


# ── Self-test ──────────────────────────────────────────────────────────

def self_test():
    """Quick self-test of embedding engines."""
    print("=" * 60)
    print("  Trinity Embedding Engine - Self Test")
    print("=" * 60)

    test_texts = [
        "Alice prefers hiking in the Rocky Mountains",
        "What is the user's favorite outdoor activity?",
        "Bob works as a software engineer at Google",
        "The capital of France is Paris",
        "Alice prefers hiking in the Rocky Mountains",  # duplicate for cache test
    ]

    for backend in ["hash", "sklearn", "auto"]:
        print(f"\n  Backend: {backend}")
        try:
            engine = create_engine(backend=backend, use_cache=True)
            print(f"    Model: {engine.model_name()}, Dim: {engine.embedding_dim()}")

            vecs = engine.embed_batch(test_texts)
            for text, vec in zip(test_texts, vecs):
                print(f"    [{vec.shape}] {text[:40]}... norm={np.linalg.norm(vec):.4f}")

            # Similarity test
            sim = engine.cosine_similarity(vecs[0], vecs[4])
            print(f"    Duplicate sim: {sim:.4f} (should be ~1.0)")
            sim = engine.cosine_similarity(vecs[0], vecs[3])
            print(f"    Unrelated sim: {sim:.4f} (should be < 0.5)")

            if hasattr(engine, 'cache_stats'):
                print(f"    Cache: {engine.cache_stats()}")

        except Exception as e:
            print(f"    ERROR: {e}")

    print("\n" + "=" * 60)
    print("  Self-test complete")
    print("=" * 60)


if __name__ == "__main__":
    self_test()

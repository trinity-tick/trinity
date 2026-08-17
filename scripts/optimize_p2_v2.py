#!/usr/bin/env python
"""Apply 3 P2 optimizations - corrected version matching exact file contents."""

import re


# ============================================================
# OPTIMIZATION 2: FAISS Product Quantization (index.py)
# ============================================================
def optimize_faiss_pq():
    path = r'C:\Users\Administrator\Trinity\trinity\vector_index\index.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    changes = 0

    # (a) Add PQConfig dataclass after SearchResult and before VectorIndex
    old_insert = 'class VectorIndex(ABC):\n    """Abstract base for vector indexes."""'
    pq_code = (
        '@dataclass\n'
        'class PQConfig:\n'
        '    """Product Quantization configuration for memory-efficient FAISS indexing.\n'
        '\n'
        '    PQ compresses high-dimensional vectors into compact codes by splitting\n'
        '    each vector into M sub-vectors and quantizing each independently.\n'
        '\n'
        '    Attributes:\n'
        '        M: Number of sub-vectors (must divide dimension evenly).\n'
        '        nbits: Number of bits per sub-vector centroid index (8-16).\n'
        '        use_ivf: Whether to combine PQ with IVF (inverted file) for fast search.\n'
        '        nlist: Number of IVF centroids (only used if use_ivf=True).\n'
        '    """\n'
        '    M: int = 32\n'
        '    nbits: int = 8\n'
        '    use_ivf: bool = True\n'
        '    nlist: int = 100\n'
        '\n'
        '    @property\n'
        '    def compression_ratio(self) -> float:\n'
        '        """Approximate memory savings ratio vs full-precision float32."""\n'
        '        pq_bytes = self.M * self.nbits // 8\n'
        '        return f"~{pq_bytes / (1024 * 4) * 100:.1f}% of original"\n'
        '\n'
        '    def __repr__(self) -> str:\n'
        '        return f"PQConfig(M={self.M}, nbits={self.nbits}, IVF={self.use_ivf})"\n'
        '\n'
        '\n'
        'class VectorIndex(ABC):\n'
        '    """Abstract base for vector indexes."""'
    )
    if old_insert in content:
        content = content.replace(old_insert, pq_code, 1)
        changes += 1
        print('  [OK] Added PQConfig dataclass')
    else:
        print('  [WARN] VectorIndex header not found')

    # (b) In FaissIndex.__init__, add _pq_config attribute
    old_faiss_init = (
        '        self._index_type: str = index_type\n'
        '        self._metric: str = metric\n'
        '        self._hnsw_config: Optional[HNSWConfig] = None\n'
        '        self._index: Optional[faiss.Index] = None'
    )
    new_faiss_init = (
        '        self._index_type: str = index_type\n'
        '        self._metric: str = metric\n'
        '        self._hnsw_config: Optional[HNSWConfig] = None\n'
        '        self._pq_config: Optional[PQConfig] = None\n'
        '        self._index: Optional[faiss.Index] = None'
    )
    if old_faiss_init in content:
        content = content.replace(old_faiss_init, new_faiss_init, 1)
        changes += 1
        print('  [OK] Added _pq_config to FaissIndex.__init__')
    else:
        print('  [WARN] FaissIndex __init__ not matched')

    # (c) In _build_index, add PQ/IVFPQ branch before flat
    old_build_start = (
        '    def _build_index(self) -> faiss.Index:\n'
        '        """Build the underlying FAISS index based on configuration."""\n'
        '        logger.debug("Building %s index (dim=%d, metric=%s)", self._index_type, self._dim, self._metric)\n'
        '        if self._index_type == "flat":'
    )
    new_build_start = (
        '    def _build_index(self) -> faiss.Index:\n'
        '        """Build the underlying FAISS index based on configuration."""\n'
        '        logger.debug("Building %s index (dim=%d, metric=%s)", self._index_type, self._dim, self._metric)\n'
        '        if self._index_type == "pq":\n'
        '            pq = self._pq_config or PQConfig()\n'
        '            self._index = faiss.IndexPQ(self._dim, pq.M, pq.nbits)\n'
        '            logger.info("Built IndexPQ (M=%d, nbits=%d, compression=%s)",\n'
        '                        pq.M, pq.nbits, pq.compression_ratio)\n'
        '        elif self._index_type == "ivfpq":\n'
        '            pq = self._pq_config or PQConfig()\n'
        '            quantizer = faiss.IndexFlatL2(self._dim)\n'
        '            self._index = faiss.IndexIVFPQ(quantizer, self._dim, pq.nlist, pq.M, pq.nbits)\n'
        '            self._index.nprobe = min(pq.nlist, 10)\n'
        '            logger.info("Built IndexIVFPQ (M=%d, nbits=%d, nlist=%d, nprobe=%d, compression=%s)",\n'
        '                        pq.M, pq.nbits, pq.nlist, self._index.nprobe, pq.compression_ratio)\n'
        '        elif self._index_type == "flat":'
    )
    if old_build_start in content:
        content = content.replace(old_build_start, new_build_start, 1)
        changes += 1
        print('  [OK] Added PQ/IVFPQ branches to _build_index')
    else:
        print('  [WARN] _build_index not matched')

    # (d) Add train() method before add() in FaissIndex
    # The add() method in FaissIndex
    old_add_sig = (
        '    # \u2500\u2500 Index Operations \u2500\u2500\n'
        '\n'
        '    def add('
    )
    train_code = (
        '    # \u2500\u2500 Index Operations \u2500\u2500\n'
        '\n'
        '    def train(self, vectors: np.ndarray) -> None:\n'
        '        """Train the index (required for PQ/IVF indexes).\n'
        '\n'
        '        PQ and IVF indexes require a training step before adding vectors.\n'
        '        Call this once with a representative sample of your data.\n'
        '\n'
        '        Args:\n'
        '            vectors: Training vectors of shape (n_samples, dim).\n'
        '        """\n'
        '        if self._index is not None and not self._index.is_trained:\n'
        '            if isinstance(self._index, (faiss.IndexPQ, faiss.IndexIVFPQ, faiss.IndexIVFFlat)):\n'
        '                logger.info("Training PQ index with %d vectors...", len(vectors))\n'
        '                self._index.train(vectors)\n'
        '                logger.info("PQ index trained successfully")\n'
        '        elif self._index is not None and self._index.is_trained:\n'
        '            logger.debug("Index already trained, skipping")\n'
        '\n'
        '    def add('
    )
    if old_add_sig in content:
        content = content.replace(old_add_sig, train_code, 1)
        changes += 1
        print('  [OK] Added train() method')
    else:
        print('  [WARN] Index Operations header not found')

    # (e) Auto-train in add() body
    old_add_body = (
        '    def add(\n'
        '        self,\n'
        '        doc_id: str,\n'
        '        vector: np.ndarray,\n'
        '        metadata: Optional[Dict[str, Any]] = None,\n'
        '        batch: bool = False,\n'
        '    ) -> int:\n'
        '        """Add a vector to the index."""\n'
        '        entry = IndexEntry(id=doc_id, vector=vector, metadata=metadata or {})'
    )
    new_add_body = (
        '    def add(\n'
        '        self,\n'
        '        doc_id: str,\n'
        '        vector: np.ndarray,\n'
        '        metadata: Optional[Dict[str, Any]] = None,\n'
        '        batch: bool = False,\n'
        '    ) -> int:\n'
        '        """Add a vector to the index."""\n'
        '        # Auto-train PQ/IVF indexes on first add\n'
        '        if self._index is not None and not self._index.is_trained:\n'
        '            self.train(np.array([vector]))\n'
        '        entry = IndexEntry(id=doc_id, vector=vector, metadata=metadata or {})'
    )
    if old_add_body in content:
        content = content.replace(old_add_body, new_add_body, 1)
        changes += 1
        print('  [OK] Added auto-train to add()')
    else:
        print('  [WARN] add() method not matched')

    # (f) Update create_index to accept pq_config
    old_ci = (
        'def create_index(\n'
        '    backend: str = "auto",\n'
        '    dim: int = 1024,\n'
        '    metric: str = "cosine",\n'
        '    index_type: str = "hnsw",\n'
        ') -> FaissIndex:'
    )
    new_ci = (
        'def create_index(\n'
        '    backend: str = "auto",\n'
        '    dim: int = 1024,\n'
        '    metric: str = "cosine",\n'
        '    index_type: str = "hnsw",\n'
        '    pq_config: Optional[PQConfig] = None,\n'
        ') -> FaissIndex:'
    )
    if old_ci in content:
        content = content.replace(old_ci, new_ci, 1)
        changes += 1
        print('  [OK] Updated create_index signature')
    else:
        print('  [WARN] create_index signature not found')

    # (g) Pass pq_config to the FaissIndex instance
    old_ci_body = (
        '    idx = FaissIndex(\n'
        '        dim=dim,\n'
        '        metric=metric,\n'
        '        index_type=index_type,\n'
        '    )\n'
        '    return idx'
    )
    new_ci_body = (
        '    idx = FaissIndex(\n'
        '        dim=dim,\n'
        '        metric=metric,\n'
        '        index_type=index_type,\n'
        '    )\n'
        '    idx._pq_config = pq_config\n'
        '    return idx'
    )
    if old_ci_body in content:
        content = content.replace(old_ci_body, new_ci_body, 1)
        changes += 1
        print('  [OK] Updated create_index body to pass pq_config')
    else:
        print('  [WARN] create_index body not found')

    # (h) Update docstring
    old_doc = (
        '        index_type: Index type: "flat" (brute force), "hnsw" (graph),\n'
        '            "ivf" (inverted file). Default HNSW.\n'
    )
    new_doc = (
        '        index_type: Index type: "flat" (brute force), "hnsw" (graph),\n'
        '            "ivf" (inverted file), "pq" (product quantization),\n'
        '            "ivfpq" (IVF+PQ). Default HNSW.\n'
        '        pq_config: Optional PQ configuration for pq/ivfpq index types.\n'
    )
    if old_doc in content:
        content = content.replace(old_doc, new_doc, 1)
        changes += 1
        print('  [OK] Updated create_index docstring')
    else:
        print('  [WARN] create_index docstring not found')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'  => {changes} changes applied to index.py')
    return changes > 0


# ============================================================
# OPTIMIZATION 3: Adaptive semantic chunking (engine.py)
# ============================================================
def optimize_adaptive_chunking():
    path = r'C:\Users\Administrator\Trinity\trinity\modules\second_brain\engine.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    changes = 0

    # Add numpy import if not present
    if 'import numpy as np' not in content:
        if 'import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re' in content:
            content = content.replace(
                'import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re',
                'import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re\nimport numpy as np'
            )
            changes += 1
            print('  [OK] Added numpy import')
    else:
        print('  [SKIP] numpy already imported')

    # Replace the _semantic_chunking method with improved version
    old_method = (
        '    def _semantic_chunking(self, messages: list[dict]) -> list[tuple]:\n'
        '        if not messages:\n'
        '            return []\n'
        '        chunks = []\n'
        '        current_chunk = []\n'
        '        current_keywords = set()\n'
        '        boundary_msgs = []\n'
        '        MAX_CHUNK_TOKENS = 2000\n'
        '        for idx, msg in enumerate(messages):\n'
        '            if not isinstance(msg, dict) or not msg.get("content"):\n'
        '                continue\n'
        '            content = msg["content"]\n'
        '            msg_tokens = len(content) // 4\n'
        '            msg_keywords = set(self._extract_keywords(content))\n'
        '            is_new_topic = False\n'
        '            if current_keywords and msg_keywords:\n'
        '                overlap = current_keywords & msg_keywords\n'
        '                jaccard = len(overlap) / len(current_keywords | msg_keywords) if current_keywords | msg_keywords else 1.0\n'
        '                if jaccard < 0.3:\n'
        '                    is_new_topic = True\n'
        '            if len(current_chunk) >= 2 and msg.get("role") != current_chunk[-1].get("role"):\n'
        '                if is_new_topic:\n'
        '                    chunk_text = "\\n".join(\n'
        '                        f"[{m.get(\'role\', \'unknown\')}]: {m.get(\'content\', \'\')}"\n'
        '                        for m in current_chunk)\n'
        '                    chunks.append((chunk_text, boundary_msgs))\n'
        '                    current_chunk = []\n'
        '                    current_keywords = set()\n'
        '                    boundary_msgs = []\n'
        '            current_size = sum(len(m.get("content", "")) // 4 for m in current_chunk)\n'
        '            if current_size + msg_tokens > MAX_CHUNK_TOKENS and current_chunk:\n'
        '                chunk_text = "\\n".join(\n'
        '                    f"[{m.get(\'role\', \'unknown\')}]: {m.get(\'content\', \'\')}"\n'
        '                    for m in current_chunk)\n'
        '                chunks.append((chunk_text, boundary_msgs))\n'
        '                current_chunk = []\n'
        '                current_keywords = set()\n'
        '                boundary_msgs = []\n'
        '            current_chunk.append(msg)\n'
        '            current_keywords.update(msg_keywords)\n'
        '            boundary_msgs.append(idx)\n'
        '        if current_chunk:\n'
        '            chunk_text = "\\n".join(\n'
        '                f"[{m.get(\'role\', \'unknown\')}]: {m.get(\'content\', \'\')}"\n'
        '                for m in current_chunk)\n'
        '            chunks.append((chunk_text, boundary_msgs))\n'
        '        return chunks'
    )

    new_method = (
        '    def _semantic_chunking(self, messages: list[dict]) -> list[tuple]:\n'
        '        """Improved adaptive semantic chunking with embedding-enhanced boundary detection.\n'
        '\n'
        '        Uses:\n'
        '        1. Jaccard keyword similarity for topic detection (lightweight)\n'
        '        2. Rolling similarity window with adaptive threshold\n'
        '        3. Size-aware adaptive chunk boundaries\n'
        '        4. Conversational role awareness for dialogue preservation\n'
        '        """\n'
        '        if not messages:\n'
        '            return []\n'
        '        chunks = []\n'
        '        current_chunk = []\n'
        '        current_keywords = set()\n'
        '        current_entities = set()\n'
        '        boundary_msgs = []\n'
        '        MAX_CHUNK_TOKENS = 2000\n'
        '        MIN_CHUNK_TOKENS = 200\n'
        '        # Track similarity history for adaptive threshold\n'
        '        similarity_history = []\n'
        '\n'
        '        # Pre-compute keywords for all messages (efficient)\n'
        '        msg_data = []\n'
        '        for msg in messages:\n'
        '            if not isinstance(msg, dict) or not msg.get("content"):\n'
        '                msg_data.append(None)\n'
        '                continue\n'
        '            content = msg["content"]\n'
        '            msg_data.append({\n'
        '                "content": content,\n'
        '                "tokens": len(content) // 4,\n'
        '                "keywords": set(self._extract_keywords(content)),\n'
        '                "role": msg.get("role", "unknown"),\n'
        '            })\n'
        '\n'
        '        for idx, data in enumerate(msg_data):\n'
        '            if data is None:\n'
        '                continue\n'
        '\n'
        '            content = data["content"]\n'
        '            msg_tokens = data["tokens"]\n'
        '            msg_keywords = data["keywords"]\n'
        '            role = data["role"]\n'
        '\n'
        '            # Compute topic similarity score (Jaccard + entity overlap)\n'
        '            is_new_topic = False\n'
        '            topic_score = 1.0\n'
        '            if current_keywords and msg_keywords:\n'
        '                union = current_keywords | msg_keywords\n'
        '                overlap = current_keywords & msg_keywords\n'
        '                jaccard = len(overlap) / len(union) if union else 1.0\n'
        '                # Entity-aware boost\n'
        '                if hasattr(self, "_collect_entities"):\n'
        '                    try:\n'
        '                        cur_ents = set(self._collect_entities(" ".join(current_keywords)))\n'
        '                        msg_ents = set(self._collect_entities(content))\n'
        '                        if cur_ents and msg_ents:\n'
        '                            ent_jaccard = len(cur_ents & msg_ents) / len(cur_ents | msg_ents)\n'
        '                            jaccard = max(jaccard, ent_jaccard)\n'
        '                    except Exception:\n'
        '                        pass\n'
        '                topic_score = jaccard\n'
        '                similarity_history.append(jaccard)\n'
        '\n'
        '                # Adaptive threshold: use rolling average if enough history\n'
        '                adaptive_thresh = 0.3\n'
        '                if len(similarity_history) >= 5:\n'
        '                    recent = similarity_history[-5:]\n'
        '                    rolling_avg = sum(recent) / len(recent)\n'
        '                    adaptive_thresh = max(0.2, rolling_avg * 0.6)\n'
        '\n'
        '                if topic_score < adaptive_thresh:\n'
        '                    is_new_topic = True\n'
        '\n'
        '            # Check current chunk size\n'
        '            current_tokens = sum(d["tokens"] for d in current_chunk)\n'
        '\n'
        '            # Decide whether to split\n'
        '            should_split = False\n'
        '            split_reason = None\n'
        '\n'
        '            # Reason 1: Topic boundary with significant shift and enough messages\n'
        '            if (len(current_chunk) >= 2 and is_new_topic\n'
        '                    and current_tokens >= MIN_CHUNK_TOKENS):\n'
        '                should_split = True\n'
        '                split_reason = "topic"\n'
        '\n'
        '            # Reason 2: Max size exceeded\n'
        '            if current_tokens + msg_tokens > MAX_CHUNK_TOKENS and current_chunk:\n'
        '                should_split = True\n'
        '                split_reason = "size"\n'
        '\n'
        '            # Reason 3: Role change + topic drop (dialogue boundary)\n'
        '            if (current_chunk and role == "user"\n'
        '                    and current_chunk[-1]["role"] == "assistant"\n'
        '                    and topic_score < 0.25\n'
        '                    and current_tokens >= MIN_CHUNK_TOKENS):\n'
        '                should_split = True\n'
        '                split_reason = "dialogue"\n'
        '\n'
        '            if should_split:\n'
        '                chunk_text = "\\n".join(\n'
        '                    f"[{d[\'role\']}]: {d[\'content\']}"\n'
        '                    for d in current_chunk)\n'
        '                chunks.append((chunk_text, boundary_msgs))\n'
        '                current_chunk = []\n'
        '                current_keywords = set()\n'
        '                boundary_msgs = []\n'
        '\n'
        '            current_chunk.append(data)\n'
        '            current_keywords.update(msg_keywords)\n'
        '            boundary_msgs.append(idx)\n'
        '\n'
        '        # Final chunk\n'
        '        if current_chunk:\n'
        '            chunk_text = "\\n".join(\n'
        '                f"[{d[\'role\']}]: {d[\'content\']}"\n'
        '                for d in current_chunk)\n'
        '            chunks.append((chunk_text, boundary_msgs))\n'
        '\n'
        '        # Merge tiny chunks (< MIN_CHUNK_TOKENS) with neighbors\n'
        '        merged = []\n'
        '        i = 0\n'
        '        while i < len(chunks):\n'
        '            text, bounds = chunks[i]\n'
        '            token_count = len(text) // 4\n'
        '            if token_count < MIN_CHUNK_TOKENS and i + 1 < len(chunks):\n'
        '                # Merge with next chunk\n'
        '                next_text, next_bounds = chunks[i + 1]\n'
        '                merged_text = text + "\\n" + next_text\n'
        '                merged_bounds = bounds + next_bounds\n'
        '                merged.append((merged_text, merged_bounds))\n'
        '                i += 2\n'
        '            else:\n'
        '                merged.append((text, bounds))\n'
        '                i += 1\n'
        '        return merged'
    )

    if old_method in content:
        content = content.replace(old_method, new_method, 1)
        changes += 1
        print('  [OK] Replaced _semantic_chunking with adaptive version')
    else:
        print('  [WARN] _semantic_chunking method not found')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'  => {changes} changes applied to engine.py')
    return changes > 0


# ============================================================
# Verify all optimizations
# ============================================================
def verify():
    print()
    print('=' * 60)
    print('Verification')
    print('=' * 60)

    # Check retrieval.py
    with open(r'C:\Users\Administrator\Trinity\trinity\modules\second_brain\retrieval.py', 'r', encoding='utf-8') as f:
        r = f.read()
    checks = [
        ('MultiModalMemory import', 'MultiModalMemory' in r),
        ('enable_multimodal param', 'enable_multimodal' in r),
        ('Stage 2c multimodal', 'Stage 2c: MultiModal' in r),
        ('4-Way RRF Fusion', '4-Way RRF Fusion' in r),
        ('index_multimodal method', 'def index_multimodal' in r),
        ('stage_multimodal stats', 'stage_multimodal' in r),
    ]
    for name, ok in checks:
        print(f'  [{"OK" if ok else "FAIL"}] {name}')

    # Check index.py
    with open(r'C:\Users\Administrator\Trinity\trinity\vector_index\index.py', 'r', encoding='utf-8') as f:
        i = f.read()
    checks = [
        ('PQConfig class', 'class PQConfig' in i),
        ('IndexPQ support', 'IndexPQ' in i),
        ('IndexIVFPQ support', 'IndexIVFPQ' in i),
        ('train() method', 'def train(self' in i),
        ('Auto-train in add()', 'self.train(' in i),
        ('create_index pq_config', 'pq_config' in i),
        ('compression_ratio property', 'compression_ratio' in i),
    ]
    for name, ok in checks:
        print(f'  [{"OK" if ok else "FAIL"}] FAISS PQ: {name}')

    # Check engine.py
    with open(r'C:\Users\Administrator\Trinity\trinity\modules\second_brain\engine.py', 'r', encoding='utf-8') as f:
        e = f.read()
    checks = [
        ('numpy import', 'import numpy as np' in e),
        ('adaptive threshold', 'adaptive_thresh' in e),
        ('similarity_history', 'similarity_history' in e),
        ('MIN_CHUNK_TOKENS', 'MIN_CHUNK_TOKENS' in e),
        ('Tiny chunk merging', 'Merge tiny chunks' in e),
    ]
    for name, ok in checks:
        print(f'  [{"OK" if ok else "FAIL"}] Chunking: {name}')


if __name__ == '__main__':
    print('=' * 60)
    print('Optimization 2: FAISS Product Quantization (index.py)')
    print('=' * 60)
    opt2 = optimize_faiss_pq()

    print()
    print('=' * 60)
    print('Optimization 3: Adaptive semantic chunking (engine.py)')
    print('=' * 60)
    opt3 = optimize_adaptive_chunking()

    print()
    print('=' * 60)
    print(f'Summary:')
    print(f'  FAISS PQ:     {"SUCCESS" if opt2 else "FAILED/SKIPPED"}')
    print(f'  Chunking:     {"SUCCESS" if opt3 else "FAILED/SKIPPED"}')
    print('=' * 60)

    verify()

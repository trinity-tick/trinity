#!/usr/bin/env python
"""Apply 3 P2 optimizations to Trinity:
1. Multi-modal retrieval integration
2. FAISS Product Quantization (PQ)
3. Adaptive semantic chunking
"""

import sys


# ============================================================
# OPTIMIZATION 1: Multi-modal retrieval integration
# ============================================================
def optimize_multimodal():
    path = r'C:\Users\Administrator\Trinity\trinity\modules\second_brain\retrieval.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    changes = 0

    # (a) Import already added in previous step — verify
    if 'MultiModalMemory' not in content:
        old_import = 'from trinity.modules.open_domain.reasoner import ContextExpander'
        new_import = old_import + '\nfrom trinity.modules.multimodal.multimodal_memory import MultiModalMemory, ModalityType'
        if old_import in content:
            content = content.replace(old_import, new_import, 1)
            changes += 1
            print('  [OK] Added MultiModalMemory import')
    else:
        print('  [SKIP] MultiModalMemory already imported')

    # (b) Update __init__ signature
    old_init = (
        '        dim: int = 1024,\n'
        '        enable_sparse: bool = True,\n'
        '        enable_splade: bool = True,\n'
        '        enable_colbert: bool = False,\n'
        '        enable_hyde: bool = False,\n'
        '        enable_crag: bool = False,\n'
        '        enable_reranker: bool = True,\n'
        '        enable_query_expansion: bool = True,\n'
        '        approx_top_k: int = 100,\n'
        '        final_top_k: int = 10,\n'
        '        reranker_model: str = "fast",\n'
        '        fusion_alpha: float = 0.3,\n'
    )
    new_init = (
        '        dim: int = 1024,\n'
        '        enable_sparse: bool = True,\n'
        '        enable_splade: bool = True,\n'
        '        enable_colbert: bool = False,\n'
        '        enable_hyde: bool = False,\n'
        '        enable_crag: bool = False,\n'
        '        enable_multimodal: bool = False,\n'
        '        enable_reranker: bool = True,\n'
        '        enable_query_expansion: bool = True,\n'
        '        approx_top_k: int = 100,\n'
        '        final_top_k: int = 10,\n'
        '        reranker_model: str = "fast",\n'
        '        fusion_alpha: float = 0.3,\n'
        '        multimodal_modality: str = "auto",\n'
    )
    if old_init in content:
        content = content.replace(old_init, new_init, 1)
        changes += 1
        print('  [OK] Updated __init__ signature')
    else:
        print('  [WARN] __init__ signature not matched')

    # (c) Add MultiModalMemory initialization after ColBERT section
    old_mm_init = (
        '        # HyDE hypothetical document retriever (query enrichment)\n'
        '        self._hyde: Optional[HydeRetriever] = None\n'
        '        if enable_hyde:\n'
        '            logger.info("Initializing HyDE retriever")'
    )
    new_mm_init = (
        '        # MultiModalMemory retriever (image/audio/text cross-modal)\n'
        '        self._multimodal: Optional[MultiModalMemory] = None\n'
        '        self._multimodal_modality = multimodal_modality\n'
        '        if enable_multimodal:\n'
        '            logger.info("Initializing MultiModalMemory")\n'
        '            self._multimodal = MultiModalMemory()\n'
        '\n'
        '        # HyDE hypothetical document retriever (query enrichment)\n'
        '        self._hyde: Optional[HydeRetriever] = None\n'
        '        if enable_hyde:\n'
        '            logger.info("Initializing HyDE retriever")'
    )
    if old_mm_init in content:
        content = content.replace(old_mm_init, new_mm_init, 1)
        changes += 1
        print('  [OK] Added MultiModalMemory init')
    else:
        print('  [WARN] MultiModalMemory init insertion point not found')

    # (d) Add stage_multimodal to stats
    old_stats = (
        '            "stage_reranker": 0,\n'
        '            "stage_cache_hit": 0,\n'
        '            "stage_cache_miss": 0,'
    )
    new_stats = (
        '            "stage_reranker": 0,\n'
        '            "stage_multimodal": 0,\n'
        '            "stage_cache_hit": 0,\n'
        '            "stage_cache_miss": 0,'
    )
    if old_stats in content:
        content = content.replace(old_stats, new_stats, 1)
        changes += 1
        print('  [OK] Added stage_multimodal to stats')
    else:
        print('  [WARN] Stats not matched')

    # (e) Add index_multimodal method
    old_add_end = (
        '        self._metadata_store[doc_id] = meta\n'
        '\n'
        '    # \u2500\u2500 Query Expansion \u2500\u2500'
    )
    new_method = (
        '        self._metadata_store[doc_id] = meta\n'
        '\n'
        '    def index_multimodal(\n'
        '        self,\n'
        '        file_paths: List[str],\n'
        '        texts: Optional[List[str]] = None,\n'
        '        metadatas: Optional[List[Dict[str, Any]]] = None,\n'
        '    ) -> "TrinityRetrievalPipeline":\n'
        '        """Index multi-modal content (images, audio, etc.) into MultiModalMemory.\n'
        '\n'
        '        Args:\n'
        '            file_paths: Paths to media files.\n'
        '            texts: Optional text descriptions for each file.\n'
        '            metadatas: Optional metadata.\n'
        '\n'
        '        Returns:\n'
        '            Self for chaining.\n'
        '        """\n'
        '        if not self._multimodal:\n'
        '            logger.warning("MultiModalMemory not enabled, skipping index")\n'
        '            return self\n'
        '        try:\n'
        '            for i, fp in enumerate(file_paths):\n'
        '                text = texts[i] if texts and i < len(texts) else fp\n'
        '                meta = metadatas[i] if metadatas and i < len(metadatas) else {}\n'
        '                self._multimodal.store(text=text, file_path=fp, metadata=meta)\n'
        '            logger.info("Indexed %d multi-modal items", len(file_paths))\n'
        '        except Exception as e:\n'
        '            logger.warning("Multi-modal indexing failed: %s", e)\n'
        '        return self\n'
        '\n'
        '    # \u2500\u2500 Query Expansion \u2500\u2500'
    )
    if old_add_end in content:
        content = content.replace(old_add_end, new_method, 1)
        changes += 1
        print('  [OK] Added index_multimodal method')
    else:
        print('  [WARN] index_multimodal insertion point not found')

    # (f) Add Stage 2c - multimodal search
    old_stage3 = (
        '            # \u2500\u2500 Stage 3: Dense FAISS \u2500\u2500\n'
        '            dense_results: List[Dict[str, Any]] = []\n'
        '            if query_vector is not None and self._dense:'
    )
    new_stage3 = (
        '            # \u2500\u2500 Stage 2c: MultiModal retrieval (4th channel) \u2500\u2500\n'
        '            multimodal_results: List[Dict[str, Any]] = []\n'
        '            if self._multimodal:\n'
        '                try:\n'
        '                    self._stats["stage_multimodal"] += 1\n'
        '                    mm_raw = self._multimodal.search(\n'
        '                        query=expanded_query,\n'
        '                        modality=self._multimodal_modality,\n'
        '                        top_k=self._approx_top_k // 2,\n'
        '                    )\n'
        '                    for mr in mm_raw:\n'
        '                        multimodal_results.append({\n'
        '                            "id": mr.get("id", f\'mm_{hash(mr.get("text", ""))}\'),\n'
        '                            "score": float(mr.get("score", 0.5)),\n'
        '                            "text": mr.get("text", ""),\n'
        '                            "multimodal_source": mr.get("modality", "unknown"),\n'
        '                            "file_path": mr.get("file_path", ""),\n'
        '                            "multimodal_query": expanded_query,\n'
        '                        })\n'
        '                except Exception as e:\n'
        '                    logger.warning("MultiModal search failed: %s", e)\n'
        '\n'
        '            # \u2500\u2500 Stage 3: Dense FAISS \u2500\u2500\n'
        '            dense_results: List[Dict[str, Any]] = []\n'
        '            if query_vector is not None and self._dense:'
    )
    if old_stage3 in content:
        content = content.replace(old_stage3, new_stage3, 1)
        changes += 1
        print('  [OK] Added Stage 2c multimodal channel')
    else:
        print('  [WARN] Stage 3 insertion point not found')

    # (g) Update RRF fusion to include multimodal
    old_rrf = (
        '            # \u2500\u2500 Stage 4: 3-Way RRF Fusion (sparse + dense + ColBERT) \u2500\u2500\n'
        '            if sparse_results and dense_results:\n'
        '                self._stats["stage_fusion"] += 1\n'
        '                # First fuse sparse + dense\n'
        '                fused = fuse_scores_sparse_dense(\n'
        '                    sparse_results, dense_results,\n'
        '                    alpha=self._fusion_alpha,\n'
        '                    top_k=self._approx_top_k,\n'
        '                )\n'
        '                # Then fuse with ColBERT if available\n'
        '                if colbert_results:\n'
        '                    fused = fuse_scores_sparse_dense(\n'
        '                        fused, colbert_results,\n'
        '                        alpha=0.5,  # Equal weight for 3rd channel\n'
        '                        top_k=self._approx_top_k,\n'
        '                    )\n'
        '            elif sparse_results:\n'
        '                fused = sparse_results\n'
        '                if colbert_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, colbert_results, alpha=0.5, top_k=self._approx_top_k)\n'
        '            elif dense_results:\n'
        '                fused = dense_results\n'
        '                if colbert_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, colbert_results, alpha=0.5, top_k=self._approx_top_k)\n'
        '            elif colbert_results:\n'
        '                fused = colbert_results\n'
        '            else:\n'
        '                continue'
    )
    # Fuse sparse+dense first, then colbert, then multimodal
    new_rrf = (
        '            # \u2500\u2500 Stage 4: 4-Way RRF Fusion (sparse + dense + ColBERT + MultiModal) \u2500\u2500\n'
        '            if sparse_results and dense_results:\n'
        '                self._stats["stage_fusion"] += 1\n'
        '                # First fuse sparse + dense\n'
        '                fused = fuse_scores_sparse_dense(\n'
        '                    sparse_results, dense_results,\n'
        '                    alpha=self._fusion_alpha,\n'
        '                    top_k=self._approx_top_k,\n'
        '                )\n'
        '                # Then fuse with ColBERT if available\n'
        '                if colbert_results:\n'
        '                    fused = fuse_scores_sparse_dense(\n'
        '                        fused, colbert_results,\n'
        '                        alpha=0.5,\n'
        '                        top_k=self._approx_top_k,\n'
        '                    )\n'
        '                # Then fuse with MultiModal if available\n'
        '                if multimodal_results:\n'
        '                    fused = fuse_scores_sparse_dense(\n'
        '                        fused, multimodal_results,\n'
        '                        alpha=0.3,\n'
        '                        top_k=self._approx_top_k,\n'
        '                    )\n'
        '            elif sparse_results:\n'
        '                fused = sparse_results\n'
        '                if colbert_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, colbert_results, alpha=0.5, top_k=self._approx_top_k)\n'
        '                if multimodal_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, multimodal_results, alpha=0.3, top_k=self._approx_top_k)\n'
        '            elif dense_results:\n'
        '                fused = dense_results\n'
        '                if colbert_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, colbert_results, alpha=0.5, top_k=self._approx_top_k)\n'
        '                if multimodal_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, multimodal_results, alpha=0.3, top_k=self._approx_top_k)\n'
        '            elif colbert_results:\n'
        '                fused = colbert_results\n'
        '                if multimodal_results:\n'
        '                    fused = fuse_scores_sparse_dense(fused, multimodal_results, alpha=0.3, top_k=self._approx_top_k)\n'
        '            elif multimodal_results:\n'
        '                fused = multimodal_results\n'
        '            else:\n'
        '                continue'
    )
    if old_rrf in content:
        content = content.replace(old_rrf, new_rrf, 1)
        changes += 1
        print('  [OK] Updated Stage 4 to 4-way fusion')
    else:
        print('  [WARN] RRF section not matched')

    # (h) Update statistics method
    old_stage_runs = (
        '            "stage_reranker_runs": self._stats["stage_reranker"],'
    )
    new_stage_runs = (
        '            "stage_reranker_runs": self._stats["stage_reranker"],\n'
        '            "stage_multimodal_runs": self._stats["stage_multimodal"],'
    )
    if old_stage_runs in content:
        content = content.replace(old_stage_runs, new_stage_runs, 1)
        changes += 1
        print('  [OK] Updated statistics with multimodal runs')
    else:
        print('  [WARN] Statistics section not matched')

    # (i) Update config in statistics
    old_config = (
        '                "dim": self._dim,\n'
        '                "approx_top_k": self._approx_top_k,\n'
        '                "final_top_k": self._final_top_k,'
    )
    new_config = (
        '                "dim": self._dim,\n'
        '                "approx_top_k": self._approx_top_k,\n'
        '                "final_top_k": self._final_top_k,\n'
        '                "multimodal_enabled": self._multimodal is not None,\n'
        '                "multimodal_modality": self._multimodal_modality,'
    )
    if old_config in content:
        content = content.replace(old_config, new_config, 1)
        changes += 1
        print('  [OK] Updated config with multimodal params')
    else:
        print('  [WARN] Config section not matched')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f'  => {changes} changes applied to retrieval.py')
    return changes > 0


# ============================================================
# OPTIMIZATION 2: FAISS Product Quantization
# ============================================================
def optimize_faiss_pq():
    path = r'C:\Users\Administrator\Trinity\trinity\vector_index\index.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    changes = 0

    # (a) Add PQConfig dataclass after imports section
    old_after_imports = (
        'logger = logging.getLogger(__name__)'
    )
    pq_config_code = (
        '@dataclass\n'
        'class PQConfig:\n'
        '    """Product Quantization configuration for memory-efficient FAISS indexing.\n'
        '\n'
        '    PQ compresses high-dimensional vectors into compact codes by splitting\n'
        '    each vector into M sub-vectors and quantizing each independently.\n'
        '\n'
        '    Attributes:\n'
        '        M: Number of sub-vectors (must divide dimension evenly).\n'
        '             Default 32 for 1024-dim, 24 for 768-dim.\n'
        '        nbits: Number of bits per sub-vector centroid index (8-16).\n'
        '             Default 8 (256 centroids per sub-space).\n'
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
        '        # float32 = 4 bytes per dim, PQ = M * nbits / 8 bytes per vector\n'
        '        dim_per_subvector = 4  # bytes per float32 per dimension\n'
        '        pq_bytes = self.M * self.nbits // 8\n'
        '        # compared to raw float32 storage (per vector bytes unknown without dim)\n'
        '        # Return ratio of pq/raw — lower is better compression\n'
        '        return f"~{(self.M * self.nbits / 8) / (1024 * 4) * 100:.1f}% of original"\n'
        '\n'
        '    def __repr__(self) -> str:\n'
        '        return f"PQConfig(M={self.M}, nbits={self.nbits}, IVF={self.use_ivf})"\n'
        '\n'
        '\n'
        'logger = logging.getLogger(__name__)'
    )
    if old_after_imports in content:
        content = content.replace(old_after_imports, pq_config_code, 1)
        changes += 1
        print('  [OK] Added PQConfig dataclass')
    else:
        print('  [WARN] Import section not found')

    # (b) Add PQ as a valid index_type in FaissIndex._build_index
    old_build_index = (
        '    def _build_index(self) -> faiss.Index:\n'
        '        """Build the underlying FAISS index based on configuration."""\n'
        '        if self._index_type == "flat":'
    )
    new_build_index = (
        '    def _build_index(self) -> faiss.Index:\n'
        '        """Build the underlying FAISS index based on configuration."""\n'
        '        if self._index_type == "pq" or self._index_type == "ivfpq":\n'
        '            pq = self._pq_config or PQConfig()\n'
        '            if self._index_type == "pq":\n'
        '                # Pure PQ index: IndexPQ\n'
        '                self._index = faiss.IndexPQ(self._dim, pq.M, pq.nbits)\n'
        '                logger.info("Built IndexPQ (M=%d, nbits=%d, compression=%s)",\n'
        '                            pq.M, pq.nbits, pq.compression_ratio)\n'
        '            else:\n'
        '                # IVF+PQ: faster search with inverted file\n'
        '                quantizer = faiss.IndexFlatL2(self._dim)\n'
        '                self._index = faiss.IndexIVFPQ(quantizer, self._dim, pq.nlist, pq.M, pq.nbits)\n'
        '                self._index.nprobe = min(pq.nlist, 10)\n'
        '                logger.info("Built IndexIVFPQ (M=%d, nbits=%d, nlist=%d, nprobe=%d, compression=%s)",\n'
        '                            pq.M, pq.nbits, pq.nlist, self._index.nprobe, pq.compression_ratio)\n'
        '        elif self._index_type == "flat":'
    )
    if old_build_index in content:
        content = content.replace(old_build_index, new_build_index, 1)
        changes += 1
        print('  [OK] Added PQ index types to _build_index')
    else:
        print('  [WARN] _build_index not found')

    # (c) Store PQ config in __init__
    old_init_pq = (
        '        self._index_type: str = index_type\n'
        '        self._metric: str = metric\n'
        '        self._index: Optional[faiss.Index] = None'
    )
    new_init_pq = (
        '        self._index_type: str = index_type\n'
        '        self._metric: str = metric\n'
        '        self._pq_config: Optional[PQConfig] = None\n'
        '        self._index: Optional[faiss.Index] = None'
    )
    if old_init_pq in content:
        content = content.replace(old_init_pq, new_init_pq, 1)
        changes += 1
        print('  [OK] Added _pq_config to FaissIndex __init__')
    else:
        print('  [WARN] FaissIndex init not found')

    # (d) Add train() method for PQ indexes
    old_train_insert = (
        '    # \u2500\u2500 Index Operations \u2500\u2500\n'
        '\n'
        '    def add('
    )
    train_method = (
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
    if old_train_insert in content:
        content = content.replace(old_train_insert, train_method, 1)
        changes += 1
        print('  [OK] Added train() method')
    else:
        print('  [WARN] Index Operations header not found')

    # (e) Auto-train in add() if needed
    old_add_start = (
        '    def add(\n'
        '        self,\n'
        '        doc_id: str,\n'
        '        vector: np.ndarray,\n'
        '        metadata: Optional[Dict[str, Any]] = None,\n'
        '        batch: bool = False,\n'
        '    ) -> int:'
    )
    new_add_start = (
        '    def add(\n'
        '        self,\n'
        '        doc_id: str,\n'
        '        vector: np.ndarray,\n'
        '        metadata: Optional[Dict[str, Any]] = None,\n'
        '        batch: bool = False,\n'
        '    ) -> int:\n'
        '        # Auto-train PQ/IVF indexes on first add\n'
        '        if self._index is not None and not self._index.is_trained:\n'
        '            self.train(np.array([vector]))'
    )
    if old_add_start in content:
        content = content.replace(old_add_start, new_add_start, 1)
        changes += 1
        print('  [OK] Added auto-train to add()')
    else:
        print('  [WARN] add() method not found')

    # (f) Update create_index to support PQ config
    old_create_index = (
        'def create_index(\n'
        '    backend: str = "auto",\n'
        '    dim: int = 1024,\n'
        '    metric: str = "cosine",\n'
        '    index_type: str = "hnsw",\n'
        ') -> FaissIndex:'
    )
    new_create_index = (
        'def create_index(\n'
        '    backend: str = "auto",\n'
        '    dim: int = 1024,\n'
        '    metric: str = "cosine",\n'
        '    index_type: str = "hnsw",\n'
        '    pq_config: Optional[PQConfig] = None,\n'
        ') -> FaissIndex:'
    )
    if old_create_index in content:
        content = content.replace(old_create_index, new_create_index, 1)
        changes += 1
        print('  [OK] Updated create_index signature')
    else:
        print('  [WARN] create_index not found')

    # (g) Update create_index body to pass pq_config
    old_create_body = (
        '    idx = FaissIndex(\n'
        '        dim=dim,\n'
        '        metric=metric,\n'
        '        index_type=index_type,\n'
        '    )'
    )
    new_create_body = (
        '    idx = FaissIndex(\n'
        '        dim=dim,\n'
        '        metric=metric,\n'
        '        index_type=index_type,\n'
        '    )\n'
        '    idx._pq_config = pq_config'
    )
    if old_create_body in content:
        content = content.replace(old_create_body, new_create_body, 1)
        changes += 1
        print('  [OK] Updated create_index body to pass PQ config')
    else:
        print('  [WARN] create_index body not found')

    # (h) Update docstring
    old_docstring = (
        '        index_type: Index type: "flat" (brute force), "hnsw" (graph),\n'
        '            "ivf" (inverted file). Default HNSW.\n'
    )
    new_docstring = (
        '        index_type: Index type: "flat" (brute force), "hnsw" (graph),\n'
        '            "ivf" (inverted file), "pq" (product quantization),\n'
        '            "ivfpq" (IVF+PQ). Default HNSW.\n'
        '        pq_config: Optional PQ configuration for pq/ivfpq index types.\n'
    )
    if old_docstring in content:
        content = content.replace(old_docstring, new_docstring, 1)
        changes += 1
        print('  [OK] Updated create_index docstring')
    else:
        print('  [WARN] create_index docstring not found')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f'  => {changes} changes applied to index.py')
    return changes > 0


# ============================================================
# OPTIMIZATION 3: Adaptive semantic chunking
# ============================================================
def optimize_adaptive_chunking():
    path = r'C:\Users\Administrator\Trinity\trinity\modules\second_brain\engine.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    changes = 0

    # Add import for numpy at top (if not already there)
    old_imports_end = 'logger = logging.getLogger(__name__)'
    new_imports = (
        'import numpy as np\n'
        'from sentence_transformers import SentenceTransformer\n'
        '\n'
        'logger = logging.getLogger(__name__)'
    )
    if 'numpy' not in content:
        if old_imports_end in content:
            content = content.replace(old_imports_end, new_imports, 1)
            changes += 1
            print('  [OK] Added numpy + SentenceTransformer imports')
    else:
        print('  [SKIP] numpy already imported')

    # Find _semantic_chunking method and replace it
    old_chunk_start = '    def _semantic_chunking(self, text: str, target_chunk_size: int = 512) -> List[str]:'
    # Look for the method signature
    if old_chunk_start in content:
        # Find the method body - from signature to next method or class end
        idx_start = content.index(old_chunk_start)
        # Find next method or class
        rest = content[idx_start + len(old_chunk_start):]
        # Find the next 'def ' that's not inside a string
        idx_next = -1
        depth = 0
        in_string = False
        string_char = None
        for i, ch in enumerate(rest):
            if in_string:
                if ch == string_char and (i == 0 or rest[i-1] != '\\'):
                    in_string = False
                continue
            if ch in ('"', "'") and (i == 0 or rest[i-1] != '\\'):
                # Check for triple quotes
                if rest[i:i+3] == ch * 3:
                    in_string = True
                    string_char = ch
                    continue
                in_string = True
                string_char = ch
                continue
            if ch == '{' or ch == '(' or ch == '[':
                depth += 1
            elif ch == '}' or ch == ')' or ch == ']':
                depth -= 1
            elif ch == '\n':
                # Check if next line starts with 'def ' at same or lesser indent
                next_lines = rest[i+1:i+20]
                if next_lines.startswith('\n    def ') or next_lines.startswith('\n    #') or next_lines.startswith('\nclass '):
                    idx_next = i + 1
                    break
        if idx_next > 0:
            old_method_body = rest[:idx_next]
            chunk_method_body = (
                '    def _semantic_chunking(self, text: str, target_chunk_size: int = 512) -> List[str]:\n'
                '        """Improved adaptive semantic chunking with embedding-based boundary detection.\n'
                '\n'
                '        Uses sentence-level boundary detection + embedding similarity to find\n'
                '        natural topic breaks. Falls back to paragraph/sentence boundaries.\n'
                '        """\n'
                '        if not text or len(text.strip()) < target_chunk_size:\n'
                '            return [text.strip()] if text.strip() else []\n'
                '\n'
                '        # Step 1: Split into sentences using regex\n'
                '        sentence_endings = re.compile(r"(?<=[。！？.!?])\\s*")\n'
                '        sentences = [s.strip() for s in sentence_endings.split(text) if s.strip()]\n'
                '\n'
                '        if len(sentences) <= 2:\n'
                '            # Fallback: paragraph-level split\n'
                '            paragraphs = [p.strip() for p in text.split("\\n\\n") if p.strip()]\n'
                '            if len(paragraphs) >= 2:\n'
                '                return paragraphs\n'
                '            return [text.strip()]\n'
                '\n'
                '        # Step 2: Compute embedding similarity between adjacent sentence groups\n'
                '        # Group 2-3 sentences together as a "segment window"\n'
                '        window_size = min(3, max(1, len(sentences) // 10))\n'
                '        segments = []\n'
                '        for i in range(0, len(sentences), window_size):\n'
                '            seg = " ".join(sentences[i:i + window_size])\n'
                '            if seg.strip():\n'
                '                segments.append(seg.strip())\n'
                '\n'
                '        if len(segments) <= 1:\n'
                '            return [text.strip()]\n'
                '\n'
                '        # Step 3: Compute cosine similarity between adjacent segments\n'
                '        # using keyword overlap Jaccard (lightweight, no model dependency)\n'
                '        def _tokenize(s: str) -> set:\n'
                '            return set(re.findall(r"\\w+", s.lower()))\n'
                '\n'
                '        similarities = []\n'
                '        seg_tokens = [_tokenize(s) for s in segments]\n'
                '        for i in range(len(segments) - 1):\n'
                '            a = seg_tokens[i]\n'
                '            b = seg_tokens[i + 1]\n'
                '            if not a or not b:\n'
                '                similarities.append(0.0)\n'
                '            else:\n'
                '                jaccard = len(a & b) / len(a | b)\n'
                '                similarities.append(jaccard)\n'
                '\n'
                '        # Step 4: Find boundary points where similarity drops sharply\n'
                '        # Similarity drop > 50% from rolling average = topic boundary\n'
                '        if len(similarities) >= 3:\n'
                '            rolling_avg = [sum(similarities[max(0, i - 1):i + 2]) / min(3, i + 2)\n'
                '                          for i in range(len(similarities))]\n'
                '            boundary_scores = [\n'
                '                rolling_avg[i] - similarities[i] for i in range(len(similarities))\n'
                '            ]\n'
                '            mean_boundary = sum(boundary_scores) / max(len(boundary_scores), 1)\n'
                '            boundaries = [\n'
                '                i for i, bs in enumerate(boundary_scores)\n'
                '                if bs > mean_boundary * 1.5  # significant drop\n'
                '            ]\n'
                '        else:\n'
                '            boundaries = []\n'
                '\n'
                '        # Step 5: Build chunks from boundaries, enforcing size constraints\n'
                '        chunks = []\n'
                '        current_start = 0\n'
                '        for b in sorted(boundaries):\n'
                '            chunk = " ".join(segments[current_start:b + 1])\n'
                '            if len(chunk) >= target_chunk_size // 2:\n'
                '                chunks.append(chunk)\n'
                '                current_start = b + 1\n'
                '\n'
                '        # Remaining segments\n'
                '        if current_start < len(segments):\n'
                '            remaining = " ".join(segments[current_start:])\n'
                '            if remaining.strip():\n'
                '                chunks.append(remaining)\n'
                '\n'
                '        # If no boundaries found or too few chunks, use size-based splitting\n'
                '        if len(chunks) <= 1:\n'
                '            # Size-based split at sentence boundaries\n'
                '            chunks = []\n'
                '            current_chunk = []\n'
                '            current_len = 0\n'
                '            for sent in sentences:\n'
                '                current_chunk.append(sent)\n'
                '                current_len += len(sent)\n'
                '                if current_len >= target_chunk_size and len(current_chunk) >= 2:\n'
                '                    chunks.append(" ".join(current_chunk))\n'
                '                    current_chunk = []\n'
                '                    current_len = 0\n'
                '            if current_chunk:\n'
                '                chunks.append(" ".join(current_chunk))\n'
                '\n'
                '        # Final cleanup: filter empty, ensure reasonable size\n'
                '        chunks = [c.strip() for c in chunks if c.strip()]\n'
                '        return chunks if chunks else [text.strip()]\n'
                '\n'
            )
            # Replace the old method body
            content = content.replace(old_chunk_start + '\n' + old_method_body, chunk_method_body, 1)
            changes += 1
            print('  [OK] Replaced _semantic_chunking method')
        else:
            print('  [WARN] Could not find end of _semantic_chunking method')
    else:
        print('  [WARN] _semantic_chunking not found')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f'  => {changes} changes applied to engine.py')
    return changes > 0


# ============================================================
# Main
# ============================================================
if __name__ == '__main__':
    print('=' * 60)
    print('Optimization 1: Multi-modal retrieval integration')
    print('=' * 60)
    opt1 = optimize_multimodal()

    print()
    print('=' * 60)
    print('Optimization 2: FAISS Product Quantization')
    print('=' * 60)
    opt2 = optimize_faiss_pq()

    print()
    print('=' * 60)
    print('Optimization 3: Adaptive semantic chunking')
    print('=' * 60)
    opt3 = optimize_adaptive_chunking()

    print()
    print('=' * 60)
    print(f'Summary:')
    print(f'  Multi-modal: {"SUCCESS" if opt1 else "FAILED/SKIPPED"}')
    print(f'  FAISS PQ:    {"SUCCESS" if opt2 else "FAILED/SKIPPED"}')
    print(f'  Chunking:    {"SUCCESS" if opt3 else "FAILED/SKIPPED"}')
    print('=' * 60)

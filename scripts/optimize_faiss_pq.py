#!/usr/bin/env python
"""FAISS PQ optimization - exact string matches for index.py."""

path = r'C:\Users\Administrator\Trinity\trinity\vector_index\index.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

changes = []

# 1. Add _pq_config to FaissIndex.__init__
old = (
    '        self._hnsw_config = hnsw_config or HNSWConfig()\n'
    '        self._index = None\n'
    '        self._id_map: Dict[int, str] = {}  # faiss_id -> our_id'
)
new = (
    '        self._hnsw_config = hnsw_config or HNSWConfig()\n'
    '        self._pq_config: Optional[PQConfig] = None\n'
    '        self._index = None\n'
    '        self._id_map: Dict[int, str] = {}  # faiss_id -> our_id'
)
if old in content:
    content = content.replace(old, new, 1)
    changes.append('_pq_config attribute')
else:
    print('FAIL: _pq_config attribute not found')

# 2. Add PQ branches to _build_index
old = (
    '        if self._index_type == "flat":\n'
    '            self._index = self._faiss.IndexFlatIP(self._dim)'
)
new = (
    '        if self._index_type == "pq":\n'
    '            pq = self._pq_config or PQConfig()\n'
    '            self._index = self._faiss.IndexPQ(self._dim, pq.M, pq.nbits)\n'
    '            logger.info("Built IndexPQ (M=%d, nbits=%d, compression=%s)",\n'
    '                        pq.M, pq.nbits, pq.compression_ratio)\n'
    '        elif self._index_type == "ivfpq":\n'
    '            pq = self._pq_config or PQConfig()\n'
    '            quantizer = self._faiss.IndexFlatIP(self._dim)\n'
    '            self._index = self._faiss.IndexIVFPQ(quantizer, self._dim, pq.nlist, pq.M, pq.nbits)\n'
    '            self._index.nprobe = min(pq.nlist, 10)\n'
    '            logger.info("Built IndexIVFPQ (M=%d, nbits=%d, nlist=%d, nprobe=%d, compression=%s)",\n'
    '                        pq.M, pq.nbits, pq.nlist, self._index.nprobe, pq.compression_ratio)\n'
    '        if self._index_type == "flat":\n'
    '            self._index = self._faiss.IndexFlatIP(self._dim)'
)
if old in content:
    content = content.replace(old, new, 1)
    changes.append('PQ/IVFPQ _build_index branches')
else:
    print('FAIL: _build_index flat branch not found')

# 3. Add train() method before _add_vector
old = (
    '    def _add_vector(self, entry: IndexEntry):\n'
    '        if not self._faiss_available:\n'
    '            return'
)
new = (
    '    def train(self, vectors: np.ndarray) -> None:\n'
    '        """Train the index (required for PQ/IVF indexes)."""\n'
    '        if self._index is not None and not self._index.is_trained:\n'
    '            if isinstance(self._index, (self._faiss.IndexPQ, self._faiss.IndexIVFPQ, self._faiss.IndexIVFFlat)):\n'
    '                logger.info("Training PQ index with %d vectors...", len(vectors))\n'
    '                self._index.train(vectors)\n'
    '                logger.info("PQ index trained successfully")\n'
    '\n'
    '    def _add_vector(self, entry: IndexEntry):\n'
    '        if not self._faiss_available:\n'
    '            return'
)
if old in content:
    content = content.replace(old, new, 1)
    changes.append('train() method + auto-train in _add_vector')
else:
    print('FAIL: train() insertion point not found')

# 4. Update _add_vector to auto-train on first call
old = (
    '    def _add_vector(self, entry: IndexEntry):\n'
    '        if not self._faiss_available:\n'
    '            return\n'
    '        vec = entry.vector.reshape(1, -1).astype(np.float32)'
)
new = (
    '    def _add_vector(self, entry: IndexEntry):\n'
    '        if not self._faiss_available:\n'
    '            return\n'
    '        # Auto-train PQ/IVF indexes on first add\n'
    '        if self._index is not None and not self._index.is_trained:\n'
    '            self.train(entry.vector.reshape(1, -1).astype(np.float32))\n'
    '        vec = entry.vector.reshape(1, -1).astype(np.float32)'
)
# We added train() before _add_vector, so the first string won't match anymore
# Let me check what's there now
# Actually, the _add_vector header was changed. Let me directly insert after the train() method.
# Since we replaced old with new in step 3, the _add_vector now looks different.
# Let me just add the auto-train logic inside _add_vector's body.

# The current _add_vector signature should now be after the train() method.
# Let me find the exact location
idx_add_vector = content.find('    def _add_vector(self, entry: IndexEntry):')
if idx_add_vector >= 0:
    # Find the vec = line
    vec_line = content.find('vec = entry.vector.reshape(1, -1).astype(np.float32)', idx_add_vector)
    if vec_line >= 0:
        # Insert auto-train before vec line
        before = content[:vec_line]
        after = content[vec_line:]
        new_content = before + (
            '        # Auto-train PQ/IVF indexes on first add\n'
            '        if self._index is not None and not self._index.is_trained:\n'
            '            self.train(entry.vector.reshape(1, -1).astype(np.float32))\n'
        ) + after
        content = new_content
        changes.append('auto-train in _add_vector')
        print('OK: auto-train added via positional insertion')
    else:
        print('FAIL: vec line not found in _add_vector')
else:
    print('FAIL: _add_vector not found')

# 5. Update create_index signature
old = (
    'def create_index(\n'
    '    backend: str = "auto",\n'
    '    dim: int = 1024,\n'
    '    metric: str = "cosine",\n'
    '    index_type: str = "hnsw",\n'
    '    hnsw_config: Optional[HNSWConfig] = None,\n'
    '    **kwargs,\n'
    ') -> VectorIndex:'
)
new = (
    'def create_index(\n'
    '    backend: str = "auto",\n'
    '    dim: int = 1024,\n'
    '    metric: str = "cosine",\n'
    '    index_type: str = "hnsw",\n'
    '    hnsw_config: Optional[HNSWConfig] = None,\n'
    '    pq_config: Optional[PQConfig] = None,\n'
    '    **kwargs,\n'
    ') -> VectorIndex:'
)
if old in content:
    content = content.replace(old, new, 1)
    changes.append('create_index signature update')
else:
    print('FAIL: create_index signature not found')

# 6. Pass pq_config to FaissIndex in create_index body
# Find where FaissIndex is instantiated
idx_fi = content.find('FaissIndex(')
if idx_fi >= 0:
    line_start = content.rfind('\n', 0, idx_fi) + 1
    line_end = content.find('\n', idx_fi)
    fi_line = content[line_start:line_end]
    # Find the closing ) and insert pq_config
    # The FaissIndex instantiation expects: FaissIndex(dim=dim, metric=metric, index_type=index_type, hnsw_config=hnsw_config)
    # We need to add pq_config=pq_config after hnsw_config
    # Find the FaissIndex instantiation block
    old_fi = (
        '            return FaissIndex(\n'
        '                dim=dim, metric=metric, index_type=index_type,\n'
        '                hnsw_config=hnsw_config\n'
        '            )'
    )
    new_fi = (
        '            return FaissIndex(\n'
        '                dim=dim, metric=metric, index_type=index_type,\n'
        '                hnsw_config=hnsw_config,\n'
        '            )\n'
        '        if pq_config:\n'
        '            idx = FaissIndex(\n'
        '                dim=dim, metric=metric, index_type=index_type,\n'
        '                hnsw_config=hnsw_config,\n'
        '            )\n'
        '            idx._pq_config = pq_config\n'
        '            return idx'
    )
    if old_fi in content:
        content = content.replace(old_fi, new_fi, 1)
        changes.append('create_index FaissIndex instantiation update')
    else:
        print('FAIL: FaissIndex instantiation not found (old_fi)')
else:
    print('FAIL: FaissIndex instantiation not found')

# 7. Update create_index docstring
old = (
    '        index_type: Index type: "flat" (brute force), "hnsw" (graph),\n'
    '            "ivf" (inverted file). Default HNSW.\n'
)
new = (
    '        index_type: Index type: "flat" (brute force), "hnsw" (graph),\n'
    '            "ivf" (inverted file), "pq" (product quantization),\n'
    '            "ivfpq" (IVF+PQ). Default HNSW.\n'
    '        pq_config: Optional PQ configuration for pq/ivfpq index types.\n'
)
if old in content:
    content = content.replace(old, new, 1)
    changes.append('create_index docstring update')
else:
    print('FAIL: create_index docstring not found')

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f'Applied {len(changes)} changes:')
for c in changes:
    print(f'  - {c}')

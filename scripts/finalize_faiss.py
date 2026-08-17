#!/usr/bin/env python
"""Finalize FAISS PQ: docstring + create_index FaissIndex instantiation."""

path = r'C:\Users\Administrator\Trinity\trinity\vector_index\index.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update docstring
old_doc = (
    '        index_type: For FAISS backend: "hnsw" (default), "flat", "ivf".\n'
    '        hnsw_config: HNSW hyperparameters (M, efConstruction, efSearch).\n'
    '\n'
    '    Returns:'
)
new_doc = (
    '        index_type: For FAISS backend: "hnsw" (default), "flat", "ivf",\n'
    '            "pq" (product quantization), "ivfpq" (IVF+PQ).\n'
    '        hnsw_config: HNSW hyperparameters (M, efConstruction, efSearch).\n'
    '        pq_config: Optional PQ configuration for pq/ivfpq index types.\n'
    '\n'
    '    Returns:'
)
if old_doc in content:
    content = content.replace(old_doc, new_doc, 1)
    print('[OK] Docstring updated')
else:
    print('[WARN] Docstring not matched')

# 2. Update FaissIndex in "faiss" backend branch
old_faiss = (
    '    if backend == "faiss":\n'
    '        return FaissIndex(dim, metric, index_type=index_type,\n'
    '                          hnsw_config=hnsw_config, **kwargs)'
)
new_faiss = (
    '    if backend == "faiss":\n'
    '        idx = FaissIndex(dim, metric, index_type=index_type,\n'
    '                         hnsw_config=hnsw_config, **kwargs)\n'
    '        if pq_config:\n'
    '            idx._pq_config = pq_config\n'
    '        return idx'
)
if old_faiss in content:
    content = content.replace(old_faiss, new_faiss, 1)
    print('[OK] Faiss backend FaissIndex updated')
else:
    print('[WARN] Faiss backend FaissIndex not matched')

# 3. Update FaissIndex in "auto" -> faiss branch
old_auto = (
    '            return FaissIndex(dim, metric, index_type=index_type,\n'
    '                              hnsw_config=hnsw_config, **kwargs)'
)
new_auto = (
    '            idx = FaissIndex(dim, metric, index_type=index_type,\n'
    '                             hnsw_config=hnsw_config, **kwargs)\n'
    '            if pq_config:\n'
    '                idx._pq_config = pq_config\n'
    '            return idx'
)
if old_auto in content:
    content = content.replace(old_auto, new_auto, 1)
    print('[OK] Auto/faiss backend FaissIndex updated')
else:
    print('[WARN] Auto/faiss backend FaissIndex not matched')

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')

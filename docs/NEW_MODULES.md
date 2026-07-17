# Trinity v6.37 — 新模块架构

## 概述
v6.37 新增三个核心优化模块，解决了 Trinity 长期存在的**伪嵌入语义无区分**、**单文件加载瓶颈**和**缺乏向量索引**三大问题。

---

## 1. `trinity/embeddings/` — 语义嵌入引擎

替换所有模块中的 SHA-256 hash 伪嵌入为真实语义嵌入。

### 后端对比

| 后端 | 模型 | 维度 | 说明 |
|------|------|------|------|
| `ollama` | bge-m3 (默认) | 1024d | 真实语义嵌入 (BAAI, 1.2GB) |
| `ollama` | qwen3-embedding:0.6b | 1536d | 轻量替代 (639MB) |
| `sklearn` | TF-IDF char n-gram | 1024d | 离线降级 (无依赖) |
| `hash` | SHA-256 | 32d | 原方案 (兼容性保留) |

### 语义区分度对比

| 对比 | SHA-256 (旧) | bge-m3 (新) | 提升 |
|------|-------------|-------------|------|
| Hiking vs Hiking | ~0.77 → 无区分 | **0.820** | ✅ |
| Hiking vs Work | ~0.77 → 无区分 | **0.386** | ✅ |
| 语义分辨率 | ❌ 不可用 | ✅ 有效 | 质的飞跃 |

### 使用

```python
from trinity.embeddings import create_engine

# 自动检测：Ollama bge-m3 → sklearn fallback
engine = create_engine(backend="auto", use_cache=True)
vec = engine.embed("Alice likes hiking")
print(f"Dim: {engine.embedding_dim()}, Norm: {sum(v*v)**0.5:.4f}")

# 批量嵌入（含LRU缓存）
vecs = engine.embed_batch(["text1", "text2", "text3"])
print(f"Cache: {engine.cache_stats()}")

# 余弦相似度
sim = engine.cosine_similarity(vecs[0], vecs[1])
```

---

## 2. `trinity/vector_index/` — 向量索引层

为真实嵌入提供配套的高效相似度搜索。

### 后端

| 后端 | 类名 | 依赖 | 性能 | 
|------|------|------|------|
| `numpy` | NumpyBruteForceIndex | numpy | O(n*d), <10K |
| `faiss` | FaissIndex | faiss-cpu/gpu | Flat/IVF/HNSW, 百万级 |
| `annoy` | AnnoyIndex | annoy | 内存映射, 百万级 |
| `chromadb` | ChromaDBIndex | chromadb | 持久化向量库 |
| `hybrid` | HybridIndex | auto | 两级 ANN+精确重排 |

### 使用

```python
from trinity.vector_index import create_index

# 创建索引
index = create_index(backend="numpy", dim=1024, metric="cosine")

# 添加向量
index.add("mem_1", embedding, {"text": "Alice likes hiking"})
index.add_batch(["mem_2", "mem_3"], [emb2, emb3])

# 搜索
results = index.search(query_embedding, top_k=5)
for r in results:
    print(f"  [{r.score:.4f}] {r.metadata['text']}")

# 混合索引（两级搜索）
from trinity.vector_index import HybridIndex
hybrid = HybridIndex(dim=1024, approx_backend="faiss", approx_top_k=100)
```

---

## 3. 模块加载优化

将 9693 行的单文件 engine.py 拆分为可懒加载的结构。

### 新增文件

| 文件 | 说明 |
|------|------|
| `registry.py` | 懒加载注册表，按需实例化模块 |
| `loader.py` | SecondBrainLoader，0.1ms 初始化 |
| `guardian.py` | 50级守护链（独立模块） |
| `retrieval.py` | 47路检索通道（独立模块） |

### 性能提升

| 指标 | 旧 (SecondBrainV636) | 新 (SecondBrainLoader) |
|------|---------------------|----------------------|
| 初始化时间 | ~500ms (加载全部) | **0.1ms** (懒加载) |
| 内存占用 | 全部 122 模块 | 按需加载 |

### 使用

```python
from trinity.modules.second_brain.loader import SecondBrainLoader
from trinity.modules.second_brain.registry import get_registry

# 懒加载模式（推荐）
loader = SecondBrainLoader(lazy=True)
print(f"Guardian: {loader.guardian_chain.total} levels")
print(f"Retrieval: {loader.retrieval.total} channels")

# 按需获取模块
cb45 = loader.get_module("CB45")  # 首次访问时加载
cb57 = loader.get_module("CB57")
```

---

## 文件结构

```
trinity/
├── embeddings/
│   ├── __init__.py        # 包导出
│   └── engine.py          # EmbeddingEngine, Ollama, Sklearn, Cache, Factory
├── vector_index/
│   ├── __init__.py        # 包导出
│   ├── index.py           # VectorIndex, Numpy/Faiss/Annoy/ChromaDB, Factory
│   └── mixed.py           # HybridIndex
└── modules/
    └── second_brain/
        ├── registry.py    # LazyModule, ModuleRegistry
        ├── loader.py      # SecondBrainLoader
        ├── guardian.py    # GuardianChainV50 (独立)
        └── retrieval.py   # RetrievalSystemV47 (独立)
```

---

## 兼容性

- ✅ 向后兼容: 所有原有接口不变
- ✅ engine.py 保持完整，新增模块为独立增量
- ✅ 所有自测试和端到端测试通过

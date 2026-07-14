# Trinity 三位一体架构详解

> 版本: v6.19 | 模块总数: 153 (113 + 40) + 十一层防御

---

## 目录

1. [设计哲学](#设计哲学)
2. [整体架构](#整体架构)
3. [second_brain: 记忆引擎](#second_brain-记忆引擎)
4. [auto_daemon: 十一层防御](#auto_daemon-八层防御)
5. [chromadb: 向量存储](#chromadb-向量存储)
6. [数据流](#数据流)
7. [模块间通信](#模块间通信)

---

## 设计哲学

### 三位一体 (Trinity) 原则

```
认知 (Cognition) × 安全 (Security) × 存储 (Storage)
```

三个子系统是**对等独立的服务**，通过标准化 MCP (Model Context Protocol) 接口通信，任一子系统可独立替换、升级、扩展。

### 核心理念

1. **记忆即服务 (Memory-as-a-Service)**: second_brain 不绑定任何特定 LLM，通过 MCP 向外暴露记忆能力
2. **安全内建 (Security by Design)**: auto_daemon 不是外挂，而是架构原生层
3. **去中心化存储**: chromadb 作为独立向量后端，second_brain 通过抽象接口访问，可替换为其他向量数据库

---

## 整体架构

```
                         ┌─────────────────┐
                         │   LLM / Agent   │
                         └────────┬────────┘
                                  │ MCP Protocol
         ┌────────────────────────┼────────────────────────┐
         │                        │                        │
         ▼                        ▼                        ▼
┌─────────────────┐   ┌──────────────────┐   ┌─────────────────┐
│  second_brain   │   │   auto_daemon    │   │    chromadb     │
│  v6.19 (113)    │◄──┤   v1.11.0        │──►│    v6.17 (40)   │
│                 │   │   8-layer defense│   │                 │
│  ┌───────────┐  │   │                  │   │  ┌───────────┐  │
│  │ Encoder   │  │   │  L1 Input Filter │   │  │ HNSW      │  │
│  │ Index     │  │   │  L2 Signature    │   │  │ IVF       │  │
│  │ Memory    │  │   │  L3 Behavior     │   │  │ PQ        │  │
│  │ Reasoner  │  │   │  L4 Sandbox      │   │  │ Cache     │  │
│  │ Graph     │  │   │  L5 Audit        │   │  │ Metadata  │  │
│  │ Tools     │  │   │  L6 Breaker      │   │  │ Embedding │  │
│  └───────────┘  │   │  L7 Recovery     │   │  └───────────┘  │
│                 │   │  L8 Awareness     │   │                 │
└────────┬────────┘   └────────┬─────────┘   └────────┬────────┘
         │                     │                      │
         └─────────────────────┼──────────────────────┘
                               │
                     ┌─────────▼─────────┐
                     │   MCP Bus (gRPC)   │
                     └───────────────────┘
```

---

## second_brain: 记忆引擎

### 模块分布 (113 modules)

```
second_brain/
├── encoder/       (18)  编码与压缩
│   ├── text_encoder.py         # 文本编码
│   ├── multimodal_encoder.py   # 多模态编码
│   ├── compression.py          # 记忆压缩
│   ├── hash_encoder.py         # 哈希指纹
│   └── ...
├── index/         (22)  检索索引
│   ├── tfidf_index.py          # TF-IDF 索引
│   ├── bm25_index.py           # BM25 索引
│   ├── semantic_index.py       # 语义索引
│   ├── hybrid_index.py         # 混合索引
│   ├── reranker.py             # 重排序器
│   └── ...
├── memory/        (16)  记忆管理
│   ├── lru_manager.py          # LRU 淘汰
│   ├── ttl_manager.py          # TTL 过期
│   ├── priority_scheduler.py   # 优先级调度
│   ├── conflict_resolver.py    # 冲突解决
│   └── ...
├── reasoner/      (14)  推理引擎
│   ├── chain_reasoner.py       # 链式推理
│   ├── temporal_reasoner.py    # 时序推理
│   ├── multi_hop_reasoner.py   # 多跳推理
│   └── ...
├── graph/         (12)  记忆图
│   ├── entity_graph.py         # 实体关系图
│   ├── knowledge_graph.py      # 知识图谱
│   ├── temporal_graph.py       # 时序图
│   └── ...
└── tools/         (25)  工具链
    ├── formatter.py            # 格式化
    ├── converter.py            # 转换器
    ├── exporter.py             # 导出
    └── ...
```

### 核心管线

#### Ingest Pipeline (注入管线)

```
Raw Text → Encoder → Dedup → Chunk → Index → Memory Store
                │                              │
                └─── Compression (optional) ───┘
```

1. **Encoder**: 文本 → 规范化 + 实体提取
2. **Dedup**: 基于哈希指纹去重
3. **Chunk**: 按语义边界分块 (默认 512 tokens)
4. **Index**: 写入 TF-IDF + BM25 + Semantic 三重索引
5. **Memory Store**: 存入优先级队列

#### Retrieve Pipeline (检索管线)

```
Query → Query Encoder → Multi-Index Retrieval → Rerank → Top-K
           │                     │
           └─── Query Expansion ─┘
```

1. **Query Encoder**: 查询意图解析 + 关键词提取
2. **Multi-Index Retrieval**: 并行检索 TF-IDF + BM25 + Semantic
3. **Fusion**: RRF (Reciprocal Rank Fusion) 合并
4. **Rerank**: LLM reranker (可选) 精排
5. **Top-K**: 返回最终结果

---

## auto_daemon: 十一层防御

### 防御层详解

```
┌──────────────────────────────────────────────────────────┐
│                    auto_daemon v1.11.0                    │
├──────────────────────────────────────────────────────────┤
│  L1: Input Filter     │  脏话/注入/越狱检测              │
│  L2: Signature Match  │  签名库 + 正则规则引擎           │
│  L3: Behavior Analysis│  ML 异常行为建模                 │
│  L4: Sandbox Isolation│  容器级隔离执行                  │
│  L5: Audit Logging    │  JSONL 全链路可追溯              │
│  L6: Circuit Breaker  │  过载保护 + 级联熔断             │
│  L7: Self-Healing     │  自动回滚 + 状态修复             │
│  L8: Situational Aware│  全局威胁建模 + 实时风险评估     │
└──────────────────────────────────────────────────────────┘
```

### 层间协作

```
Request → L1 → L2 → L3 ─┬─→ L4 (隔离执行) → L7 (故障恢复)
                         │
                         └─→ L6 (过载熔断)
                         
L5 (全链路审计)
L8 (全局态势 → 动态调整 L1-L7 阈值)
```

---

## chromadb: 向量存储

### 模块分布 (40 modules)

```
chromadb/
├── vector_index/   (12)  HNSW, IVF, PQ 等
├── embedding/      (8)   OpenAI, SBERT, Cohere 等
├── metadata/       (6)   SQL, JSON, 混合过滤
├── cache/          (5)   LRU, 分布式缓存
└── admin/          (7)   备份, 迁移, 监控
```

### 索引选型指南

| 场景 | 推荐索引 | 原因 |
|------|----------|------|
| < 10万向量 | HNSW | 精度高、构建快 |
| 10万-100万 | IVF + PQ | 内存效率高 |
| > 100万 | DiskANN | 磁盘友好 |
| 实时更新 | HNSW (增量) | 支持增量插入 |

---

## 数据流

### 写路径 (Write Path)

```
User Input
    │
    ▼
auto_daemon L1-L3 (安全检查)
    │
    ▼
second_brain Encoder (编码)
    │
    ├──► second_brain Index (稀疏索引: TF-IDF + BM25)
    │
    └──► chromadb (稠密索引: Embedding + HNSW)
    │
    ▼
second_brain Memory Store (优先级管理)
```

### 读路径 (Read Path)

```
User Query
    │
    ▼
second_brain Query Encoder
    │
    ├──► second_brain Index (稀疏检索)
    ├──► chromadb (稠密检索)
    │
    ▼
RRF Fusion
    │
    ▼
Reranker (LLM)
    │
    ▼
Top-K Results → auto_daemon L2 (输出过滤)
```

---

## 模块间通信

### MCP Protocol Stack

```
┌──────────────────────────────────────┐
│         Application Layer            │
│  (LLM Agents, Chat Interfaces)       │
├──────────────────────────────────────┤
│         MCP Protocol (JSON-RPC)      │
├──────────┬──────────┬────────────────┤
│ Ingestion│ Retrieval│ Administration │
│ Methods  │ Methods  │ Methods        │
├──────────┴──────────┴────────────────┤
│            gRPC Transport            │
└──────────────────────────────────────┘
```

### 标准接口

| 接口 | 方法 | 方向 |
|------|------|------|
| `ingest` | 记忆注入 | LLM → Trinity |
| `retrieve` | 记忆检索 | LLM → Trinity |
| `delete` | 记忆删除 | LLM → Trinity |
| `benchmark` | 评测启动 | LLM → Trinity |
| `health` | 健康检查 | LLM → Trinity |

---

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| 稀疏检索 | TF-IDF, BM25 (scikit-learn) |
| 稠密检索 | ChromaDB HNSW |
| 嵌入模型 | SBERT, OpenAI Ada |
| 通信协议 | MCP (JSON-RPC over gRPC) |
| 安全框架 | 八层防御 (auto_daemon) |
| 评测框架 | LongMemEval, custom benchmarks |
| 研究支撑 | 101 篇论文 |

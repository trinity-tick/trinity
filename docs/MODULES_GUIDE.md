# Trinity modules/ 层索引与维护指南

> 价值兑现路径 3（2026-08-26）：modules 层（60 文件 / 33,440 行）是未被优化轮触达的
> 存量复杂度区。本文档为**盘点索引**（只报告，不删码）；删除前需人工确认动态加载。

## 1. 概览

- 位置：`trinity/modules/`；60 个 py 文件 / 33,440 行（全包 22% 行数）；
- 静态引用分析：**12 个被直接引用**（活跃），48 个无静态引用（含 __init__ 误报 ~6 个；
  真实孤立候选 ~42 个）；
- 注意：`second_brain/engine.py` 是活跃的（`from trinity.modules.second_brain import Engine`
  模式），但包内其余 34 个 CB 系列模块**无任何静态引用**——多为"脑启发论文对齐"
  研究实现（CB 系列命名），疑似研究型死代码。

## 2. 被引用（活跃）模块

| 模块 | 引用方 | 职责 |
|---|---|---|
| second_brain/engine.py | trinity 入口 | 第二大脑引擎（Engine 类） |
| （其余 11 个见代码 grep "from trinity.modules"） | — | — |

## 3. 孤立候选清单（无静态引用，删除前人工确认）

### 3.1 second_brain/（研究型，34 个）—— 最高优先级瘦身候选
audit_trail / cascade_repair_engine / causal_memory / causal_semantic_graph_memory /
cb49_52 / confidence_scored_retrieval / consensus_voting / contextual_embedding /
continuous_eval / dcpm_dual_process_memory / engine_diagnostics / engine_observability /
engine_retrieval / episodic_rl / federated_memory / guardian / guardian_retrieval /
intent_compression / knowledge_gossip / lifecycle_manager / loader / memory_page_manager /
memory_unlearning / p1_preamble / personalization_engine / proactive_prefetcher /
prompt_ingestion / reflective_repair_memory / retrieval / sage_graph_memory_engine /
selective_recall / self_healing / serendipity_retrieval_engine /
structured_distillation_compressor / token_budget / workflow_memory

### 3.2 multimodal/（4 个）
audio_encoder / image_encoder / multimodal_enhanced / multimodal_memory

### 3.3 open_domain/（整包孤立）
包内全部（__init__ + 实现）

### 3.4 其他
memory_replay_trainer.py / streaming_ingest.py / chromadb/（适配层）

## 4. 建议处置（按风险递增）

1. **文档化**（本文件即第 1 步）——孤立模块"有据可查"；
2. **测试引用确认**：grep 全仓（含 scripts/benchmark/tests）确认无 importlib/动态加载
   （字符串 "second_brain" 已在 tests/test_second_brain* 中出现——删除前跑全量测试）；
3. **归档式瘦身**：把确认孤立的模块移入 `trinity/modules/_research_archive/` 子目录
   （git 保留历史，import 路径变化会被测试立刻发现）；
4. 保守策略：保持现状 + 文档化（推荐——删除收益 < 稳定性风险）。

## 5. 维护纪律

- 新模块加入 modules/ 前先问：是否被引用？是否有测试？无则放 scripts/ 或 research 区；
- 每季度重跑本盘点（脚本参考 temp 内 orphan 分析：静态 import 扫描）。

---
*价值兑现路径 3 · 2026-08-26*

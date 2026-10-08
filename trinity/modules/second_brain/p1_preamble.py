"""
Trinity Second Brain — Preamble / Data Classes
===============================================
Enums, dataclasses, and value objects shared by all second_brain modules.
Auto-extracted from engine.py for modularity.
"""
# status: frozen (2026-09 EXECUTION 163)
from __future__ import annotations

# 2026-09-14（723）：初始化横幅统一走 stderr——横幅污染 stdout 已造成 loop_health(689)/claim_gate/
# 子进程断言用例(722) 三类误报。详见 _banner.py。
try:
    from trinity.modules.second_brain._banner import banner   # 包导入
except ImportError:  # 引擎按顶层模块导入（sys.path 指向 second_brain/）
    from _banner import banner


# v6.32(119模块,47守护,44检定) → v6.34(121模块,49守护,46检定)
# 新增: CB55 HindsightFourNetwork | CB56 ZikkaronHopfield
# 守护者: +L48 HindsightFourNetworkValidation | +L49 ZikkaronHopfieldEnergyGate
# 检索通道: +ch45 HindsightFourNetworkFusion | +ch46 ZikkaronHopfieldSpreadingActivation
# 论文: P127 (Hindsight Four-Network) | P128 (Zikkaron Hopfield Energy), 2026-07-13
# 守护者: +L46 BEAMLIGHTGuard | +L47 ExabaseRetrievalGuard
# 检索通道: +ch43 BEAMBenchmarkEvalSearch | +ch44 ThreePhaseTriSignalRetrieval
# 论文: P125 (BEAM-LIGHT ICLR2026) | P126 (Exabase M-1 Retrieval), 2026-07-13 (P0推进: 对齐BEAM基准+Exabase三路打分)
# v6.26(113模块,41守护,38检定) → v6.28(115模块,43守护,40检定)
# v6.26(113模块,41守护,38检定) → v6.28(115模块,43守护,40检定)
# 新增: CB49 RelationalVersioning | CB50 ContextualChunkIngestion
# 守护者: +L42 RelationalVersionGuard | +L43 ChunkIngestionGuard
# 检索通道: +ch39 RelationalVersionSearch | +ch40 ChunkBasedHybridRetrieval
# 论文: P121 (Supermemory RelationalVersioning) | P122 (Supermemory ChunkIngestion), 2026-07-12 (P2优化: 对齐Supermemory SOTA)
# v6.24(111模块,39守护,37检定) → v6.26(113模块,41守护,38检定)
# 新增: CB47 TokenEfficientMemory | CB48 AgentNativeCuration
# 守护者: +L40 TokenEfficiencyGuard | +L41 CurationGuard
# 检索通道: +ch38 TokenEfficientCascade
# 论文: P119 (Mem0) | P120 (ByteRover 写路径), 2026-07-12 (P0优化: 业界记忆系统对比后实施)
# v6.15(106模块,37守护,36检定) → v6.24(111模块,39守护,37检定)
# 新增: CB45 ProgressiveCascade | CB46 TemporalValidity | CB42-CB44 ChromaDB边缘层正式注册
# 守护者: +L38 CascadeGuard | +L39 TemporalValidityGuard
# 检索通道: +ch37 ProgressiveCascade
# 论文: P117 (ByteRover) | P118 (Zep/Graphiti), 2026-07-12

import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime

SEP = "=" * 80; SUB = "-" * 60; VERSION = "v6.34"

PAPERS = {
    "P16":{"title":"Self-GC: Self-Governing Context","source":"arXiv:2607.00692"},
    "P17":{"title":"Managed Autonomy at Runtime: Gear-Based Safety","source":"arXiv:2607.00334"},
    "P18":{"title":"Anytime-Valid Certificates","source":"arXiv:2607.00871"},
    "P19":{"title":"Bayesian Uncertainty Propagation for RAG","source":"arXiv:2607.00972"},
    "P20":{"title":"Semantic Cache Replacement","source":"arXiv:2607.00394"},
    "P21":{"title":"Multi-Head Recurrent Memory Agents","source":"arXiv:2607.01523, UW-Madison"},
    "P22":{"title":"ContextNest: Verifiable Context Governance","source":"arXiv:2607.02116"},
    "P23":{"title":"ElephantAgent: Contextual State Continuity","source":"arXiv:2607.01919"},
    "P24":{"title":"Constraint Steerability for Coding Agent Oversight","source":"arXiv:2607.02389"},
    "P25":{"title":"Online Safety Monitoring for LLMs","source":"arXiv:2607.02510"},
    "P26":{"title":"[Round2-P26]","source":"arXiv:2607.03xxx"},
    "P27":{"title":"[Round2-P27]","source":"arXiv:2607.03xxx"},
    "P28":{"title":"[Round2-P28]","source":"arXiv:2607.03xxx"},
    "P29":{"title":"[Round2-P29]","source":"arXiv:2607.03xxx"},
    "P30":{"title":"[Round2-P30]","source":"arXiv:2607.03xxx"},
    "P31":{"title":"[Round2-P31]","source":"arXiv:2607.03xxx"},
    "P32":{"title":"[Round2-P32]","source":"arXiv:2607.03xxx"},
    "P33":{"title":"[Round2-P33]","source":"arXiv:2607.03xxx"},
    "P34":{"title":"[Round2-P34]","source":"arXiv:2607.03xxx"},
    "P35":{"title":"[Round2-P35]","source":"arXiv:2607.03xxx"},
    "P36":{"title":"[Round2-P36]","source":"arXiv:2607.03xxx"},
    "P37":{"title":"[Round2-P37]","source":"arXiv:2607.03xxx"},
    "P38":{"title":"[Round2-P38]","source":"arXiv:2607.03xxx"},
    "P39":{"title":"[Round2-P39]","source":"arXiv:2607.03xxx"},
    "P40":{"title":"[Round2-P40]","source":"arXiv:2607.03xxx"},
    "P41":{"title":"[Round2-P41]","source":"arXiv:2607.03xxx"},
    "P42":{"title":"[Round2-P42]","source":"arXiv:2607.03xxx"},
    "P43":{"title":"[Round2-P43]","source":"arXiv:2607.03xxx"},
    "P44":{"title":"[Round2-P44]","source":"arXiv:2607.03xxx"},
    "P45":{"title":"[Round2-P45]","source":"arXiv:2607.03xxx"},
    "P46":{"title":"[Round3-P46]","source":"arXiv:2607.04xxx"},
    "P47":{"title":"[Round3-P47]","source":"arXiv:2607.04xxx"},
    "P48":{"title":"[Round3-P48]","source":"arXiv:2607.04xxx"},
    "P49":{"title":"[Round3-P49]","source":"arXiv:2607.04xxx"},
    "P50":{"title":"[Round3-P50]","source":"arXiv:2607.04xxx"},
    "P51":{"title":"[Round3-P51]","source":"arXiv:2607.04xxx"},
    "P52":{"title":"[Round3-P52]","source":"arXiv:2607.04xxx"},
    "P53":{"title":"[Round3-P53]","source":"arXiv:2607.04xxx"},
    "P54":{"title":"[Round3-P54]","source":"arXiv:2607.04xxx"},
    "P55":{"title":"[Round3-P55]","source":"arXiv:2607.04xxx"},
    "P56":{"title":"[Round3-P56]","source":"arXiv:2607.04xxx"},
    "P57":{"title":"[Round3-P57]","source":"arXiv:2607.04xxx"},
    "P58":{"title":"[Round3-P58]","source":"arXiv:2607.04xxx"},
    "P59":{"title":"[Round3-P59]","source":"arXiv:2607.04xxx"},
    "P60":{"title":"[Round3-P60]","source":"arXiv:2607.04xxx"},
    "P61":{"title":"[Round3-P61]","source":"arXiv:2607.04xxx"},
    "P62":{"title":"[Round3-P62]","source":"arXiv:2607.04xxx"},
    "P63":{"title":"[Round3-P63]","source":"arXiv:2607.04xxx"},
    "P64":{"title":"[Round3-P64]","source":"arXiv:2607.04xxx"},
    "P65":{"title":"[Round3-P65]","source":"arXiv:2607.04xxx"},
    "P66":{"title":"[Round3-P66]","source":"arXiv:2607.04xxx"},
    "P67":{"title":"[Round3-P67]","source":"arXiv:2607.04xxx"},
    "P68":{"title":"[Round3-P68]","source":"arXiv:2607.04xxx"},
    "P69":{"title":"[Round3-P69]","source":"arXiv:2607.04xxx"},
    "P70":{"title":"[Round3-P70]","source":"arXiv:2607.04xxx"},
    "P71":{"title":"[Round3-P71]","source":"arXiv:2607.04xxx"},
    "P72":{"title":"[Round3-P72]","source":"arXiv:2607.04xxx"},
    "P73":{"title":"[Round3-P73]","source":"arXiv:2607.04xxx"},
    "P74":{"title":"[Round3-P74]","source":"arXiv:2607.04xxx"},
    "P75":{"title":"[Round3-P75]","source":"arXiv:2607.04xxx"},
    "P76":{"title":"HOLA: Hippocampus for Linear Attention","source":"arXiv:2607.02303"},
    "P77":{"title":"Episodic-to-Semantic Consolidation Without Identity Drift","source":"arXiv:2607.01988"},
    "P78":{"title":"DRIFTLENS: Measuring Memory-Induced Reasoning Drift","source":"arXiv:2607.02374"},
    "P79":{"title":"FARMA: Forged Reasoning Attacks on Agent Memory","source":"arXiv:2607.05029"},
    "P80":{"title":"WhisperBench/MemGhost: Stealthy Memory Injection","source":"arXiv:2607.05189"},
    "P81":{"title":"Self-GC: Self-Governing Context Management","source":"arXiv:2607.00692, Round5"},
    "P82":{"title":"Multi-Head Recurrent Memory Agents (MHM-LRU)","source":"arXiv:2607.01523, Round5"},
    "P83":{"title":"Ensemble QSP: Query-Specific Partitioning","source":"arXiv:2607.07666, Round5"},
    "P117":{"title":"ByteRover: Agent-Native Memory Through LLM-Curated Hierarchical Context","source":"arXiv:2604.xxxxx, BAAI 2026-04"},
    "P118":{"title":"Zep/Graphiti: Temporal Knowledge Graph Driven Agent Memory (Bi-temporal Model)","source":"Apache 2.0, GitHub 24K+ stars"},
    "P119":{"title":"Mem0: Single-Pass ADD-Only Memory Extraction with Four-Way Signal Fusion","source":"arXiv:2504.19413, ECAI 2025; April 2026 Upgrade"},
    "P120":{"title":"ByteRover Write Path: LLM-as-Curator with Coordination Context & Crash Recovery","source":"arXiv:2604.xxxxx, BAAI 2026-04"},
    "P121":{"title":"Supermemory Relational Versioning: Updates/Extends/Derives with Version Chain & Semantic Dedup","source":"LongMemEval-S 95% SOTA, Knowledge Update 99%, Multi-session 93%"},
    "P122":{"title":"Supermemory Contextual Chunk Ingestion: Session-Based Chunking with Atomic Memory Generation & Hybrid Search","source":"LongMemEval-S 95% SOTA, 99.4% context reduction, ~720 mean tokens"},
    "P123":{"title":"Mastra Observational Memory: Observer-Reflector Dual Background Agents (94.87% LongMemEval SOTA)","source":"Mastra Research, Open Source, gpt-5-mini 94.87%"},
    "P124":{"title":"MemMachine GroundTruthEpisodes: Ground-Truth-Preserving Memory System (93.0% LongMemEval, 91.7% LoCoMo)","source":"arXiv:2604.04853, 2026-04-06"},
    "P125":{"title":"BEAM-LIGHT: Beyond a Million Tokens — Benchmarking + Enhancing Long-Term Memory (ICLR 2026)","source":"arXiv:2510.27246, ICLR 2026; 100 dialogues, up to 10M tokens, 2000 probes, 10 capabilities"},
    "P126":{"title":"Exabase M-1: Three-Phase Tri-Signal Retrieval (96.4% LongMemEval, Gemini 3 Flash)","source":"Exabase Research, May 2026; candidate scoring + multi-query decomposition + re-ranking"}
}

# ============ 枚举 ============
# 2026-10-06（T6 能力面收口）：本节原先**自己定义**了 16 个类，与
# `engine_core_types.py` 里的实现 **AST 完全相同**（逐类 `ast.dump` 比对，长度一致）。
#
# 引用面实测（AST 全仓扫描，见 evidence/import_reachability.json）：
#   · 全仓唯一的 import 者是 `research/archive/second_brain/memory_core.py`
#     （`from trinity.modules.second_brain.p1_preamble import ContextObject`）；
#   · `research/` 各级**都没有 `__init__.py`**，且 `pyproject.toml:116` 只打包
#     `trinity*` ⇒ 该文件不是可导入的包成员，全仓也无人 import 它；
#   · 本模块**不在** engine_worker / api.server / cli 的 import 闭包内。
#   ⇒ 即"零运行时路径"。且因两份定义 AST 相同，那个归档引用者拿到的类**语义不变**
#     （只有类属性 `__module__` 从 p1_preamble 变为 engine_core_types）。
#
# 处置：改为**显式转口**，定义点收敛到 `engine_core_types`。
#
# 判据：`tests/unit/test_capability_hygiene_20261006.py`
#   · 16 个名字在全仓的定义点**恰好 1 处**；
#   · `p1_preamble.<Name> is engine_core_types.<Name>`。
# 运行时行为不变证据：MRO= (cls, object)、构造与 `to_dict()` 逐字保留，
# 且 SEP/SUB/VERSION/PAPERS 与文末 banner 文案逐字不变。
from trinity.modules.second_brain.engine_core_types import (  # noqa: F401,E402
    CacheWriteDecision,
    CertificateStatus,
    ConsolidationPhase,
    ConsolidationRecord,
    ContextAction,
    ContextCommit,
    ContextObject,
    ContinuityState,
    ExactKVEntry,
    ExecutionGear,
    GovernanceState,
    MemoryErrorType,
    MemoryHead,
    ProvenanceRecord,
    SafetyAlarm,
    ValueCategoryMapping,
)

banner(f"[Second Brain {VERSION}] Core imports & data classes ready")


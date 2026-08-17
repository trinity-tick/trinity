
> ⚠️ **勘误（2026-08-17 二轮标定+验证）**：文中"检索 96.8% R@5（hybrid 47 通道）"实为
> **FTS5 关键词回退路径**的测量值——mem.search() 在 hybrid retriever 未初始化时走
> adapter.search_memories（FTS）。scripts/calibrate_ranking.py 直测 5 通道 HybridRetriever：
> **fusion 静态权重 R@5=0.008 vs rrf=0.950（120 题）/0.983（60 题）**——fusion 已废弃，
> hybrid 路径默认策略 fusion→rrf。但 scripts/verify_engine_default.py 受控 A/B（同 120 题
> 同摄入）显示 **FTS 0.975 > hybrid-rrf 0.942**：引擎默认保持 FTS；hybrid-rrf 仅对显式
> 初始化/调用 search_hybrid 的路径生效。评分特性（confidence/importance）对 R@5 无增益，
> 保持 opt-in。"检索评分 9.5/10"结论不变，口径应为"FTS 96.8%"。

# Trinity 评测现状与网络方案对比评分（2026-08-17）

> 数据口径：检索 recall（session R@k，官方 LongMemEval_S 500 题）；QA accuracy（DeepSeek judge3 3票，
> 官方分题型模板）；网络数字来自各项目公开声明 + 独立复测。全量 500 QA 未跑（用户延后），为加权推算。

## 一、Trinity 当前评测状态（实测）

| 维度 | 数值 | 说明 |
|---|---|---|
| session recall@5 | **96.8%**（全量 500 实测） | 证据会话进 top-5，命中位次 1.3 |
| turn recall@5 | 92.2%（全量 500 实测） | — |
| QA accuracy（dated，全量 500 实测） | 54.0%（旧 judge） | 2026-08-16 官方全量 |
| QA accuracy（judge3，route2，50 题） | **72%**（稳定 1.0） | 当前最优评测配置 |
| QA accuracy（judge3，route=multi-turn，50 题） | **74%** | multi 用 turn 粒度后的组合 |
| QA accuracy 全量预估 | **~70%（±3pp）** | 加权推算（3.14 节） |

### 分题型现状（judge3，最优配置下）

| 题型 | n/500 | 基线(dated) | 当前最优 | 关键优化 |
|---|---|---|---|---|
| multi-session | 133 | 35.3% | **52-59%** | turn 粒度检索（+24pp，确定性） |
| temporal-reasoning | 133 | 54.5% | **62-64%** | REL 相对天数 + inner2（session 粒度） |
| knowledge-update | 78 | 100% | ~85% | dated plain（freshness/chronos 证伪） |
| single-session-user | 70 | 100% | ~90% | dated plain |
| single-session-assistant | 56 | 100% | ~92% | dated plain |
| single-session-preference | 30 | 0-6% | **36-60%** | LLM 两段式 pref3（ppro 画像证伪） |

### 已验证的有效优化（judge3 可信口径）
- dated 时间戳注入：temporal +15.7pp（28.6→44.4）
- inner2 内检索精调：temporal +9pp（仅 temporal 用；pref 禁用，-6.7pp）
- pref3 LLM 两段式：preference 36-60%（stage-1 偏好摘要→stage-2 个性化）
- **turn 粒度检索：multi +24pp（28→52%，双重验证确定性）**
- 按题型路由组合：route2 72%（+6pp vs 66% 基线）
- judge3 治理：reason-first 3票，消除旧 judge 噪声（pref 真实水平从"3-16%"修正为"36-60%"）

### 已证伪（避免返工）
- 分题型专用生成提示（preference/multi/KU 各写一套）：负优化
- multi 生成层：multi2/stitch/extract/con/conjson 全败（-2pp ~ -34pp）
- freshness [FRESH:] 标注：KU 负优化
- chronos 细粒度事件：无增益（REL 已覆盖）
- multi 实体扩展检索：无增益（recall 已高，瓶颈在综合）
- ppro 正则画像：30 题证伪（10%）——preference 需 LLM 两段式

## 二、与网络方案对比评分

### 2.1 检索召回对比（同口径：LongMemEval_S R@5，可复现）

| 系统 | R@5 | 来源 |
|---|---|---|
| **Trinity（hybrid 47 通道）** | **96.8%** | 本报告（官方 500 题实测） |
| MemPalace raw（vector-only） | 96.6% | MemPalace HISTORY（独立复现） |
| agentmemory BM25+Vector | 95.2% | agentmemory benchmark（独立） |
| Awareness（本地优先） | 96.0% | dev.to 独立复测 |
| agentmemory BM25-only | 86.2% | agentmemory benchmark |
| Zep | 63.8% | vectorize 独立评测 |
| Mem0 OSS（独立） | ~32-49% | dev.to / vectorize |

**检索评分：Trinity 与 MemPalace/Awareness 并列头部（96.8% vs 96.6%/96.0%），
显著高于 Mem0/Zep（32-63%）。检索层已是 SOTA 水平。**

### 2.2 QA 生成对比（端到端，注意口径差异）

| 系统 | QA accuracy | 口径 |
|---|---|---|
| 官方最佳系统（论文） | 80-90% | GPT-4o judge + 专门调优（oracle 上限 ~82.4%） |
| **Trinity（当前最优 route，50 题 judge3）** | **72-74%** | DeepSeek judge3，官方模板 |
| **Trinity（全量预估）** | **~70%** | 加权推算（未跑全量） |
| Trinity（dated，全量实测） | 54.0% | DeepSeek judge（旧口径） |
| Mem0 / Zep | 未公开可复现 QA 数字 | — |

> ⚠️ **口径警示**：MemPalace 在 2026-04 的 HISTORY 中明确修正过"recall 与 QA accuracy 混排"的分类错误——
> 100% R@5 可以只有 40% QA。Trinity 检索 96.8% 头部，但 QA 生成（~70%）仍是与官方最优（80-90%）
> 的主要差距，差距来源是生成策略而非检索。

### 2.3 综合评分（10 分制，诚实口径）

| 维度 | Trinity | 网络最优 | 评分 | 说明 |
|---|---|---|---|---|
| 检索召回（R@5） | 96.8% | 96.6% | **9.5/10** | 与 MemPalace 并列头部 |
| QA 生成（judge3） | 72-74% | 80-90%（官方最优） | **6.5/10** | 差距 10-15pp，主要在 multi/temporal/pref |
| 分题型覆盖 | 6/6 题型都有专项方案 | — | **7/10** | multi/temporal/pref 均有确定性优化 |
| 评测方法论 | judge3 3票 + 证伪流程 | MemPalace 诚实口径 | **8.5/10** | 三次捕获伪增量，方法论扎实 |
| 产品化落地 | route2 脚本级 | 生产系统 | **5/10** | 尚未进生产链路 |
| 数据/基准覆盖 | 仅 LongMemEval_S | LoCoMo/BEAM 等多基准 | **5/10** | 网络阻塞，未跑其他基准 |

### 2.4 关键差距与机会（按 ROI）

1. **QA 生成是唯一大差距**（6.5/10）：检索已 SOTA，瓶颈在 multi（52-59%→目标 70%+）、
   temporal（62-64%→目标 70%+）、pref（36-60%→目标 70%+）；
2. **全量 500 验证**：72-74%（50 题）→ 全量预估 70%（±3pp），值得跑一次锁定；
3. **multi 仍是最大提升空间**：turn 粒度已 +24pp，若能叠加"turn 粒度 + 结构化综合"可再推；
4. **产品化落地**：route2/turn 粒度配置从评测脚本进生产链路（当前 5/10）。

## 三、一句话

**检索已是 SOTA（96.8% R@5，与 MemPalace 并列），QA 生成 ~70%（judge3，全量预估）距官方最优
80-90% 差 10-15pp——差距全在生成策略（multi/temporal/pref），且已找到确定性优化（turn 粒度 +24pp、
REL+inner2 +9pp、pref3 两段式 +24pp），产品化落地后评分可再上一个台阶。**

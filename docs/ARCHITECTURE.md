# Trinity 架构地图（ARCHITECTURE.md）

> 重写 2026-09（EXECUTION 108 轮）。本文件是 Trinity 的活文档：模块分级、成长规则、
> 大脑化路线图。由模块分级扫描脚本（scripts/module_classify.py）生成基线，人工审阅后生效。

## 1. 系统定位

- **Trinity = 记忆 OS**：跨会话长程记忆 + 检索 + 自进化 + 可证明审计
- 存储：PG 主存储（pgvector 融合）+ SQLite 镜像/回退 + docker 栈
- 规模：30 万行 / 48 包 / 1,311 测试 / 28k 记忆 / 107+ 轮演进

## 2. 模块分级（2026-09 扫描基线）

### 2.1 core（运行时真实生效，持续演进）— 25 个

| 模块 | 文件数 | core 引用 | 说明 |
|---|---|---|---|
| agents | 26 | 47 | 智能体/agent_brain（大脑核心） |
| modules | 55 | 29 | second_brain 引擎族（运行时路径） |
| core | 22 | 17 | Trinity 客户端/引擎 |
| telemetry | 1 | 17 | 追踪 |
| a2a | 10 | 16 | 多智能体通信 |
| adapters | 15 | 14 | PG/SQLite 存储适配器 |
| memory | 14 | 14 | 记忆生命周期 |
| vector_index | 8 | 14 | 向量索引/reranker |
| api | 27 | 12 | REST API |
| embeddings | 2 | 12 | 嵌入引擎 |
| brain | 5 | 11 | 认知包（metacognition/perception/working_memory） |
| audit | 4 | 10 | 审计链 |
| evolution | 13 | 9 | 自进化 |
| retrieval | 6 | 9 | 检索器 |
| identity | 4 | 6 | 身份 |
| mcp | 7 | 5 | MCP 服务 |
| security | 3 | 5 | 安全/加密 |
| automation | 1 | 5 | 自动化 |
| llm | 1 | 5 | LLM 封装 |
| benchmark | 4 | 4 | 基准 |
| structure_store | 1 | 4 | 结构层 |
| cognition | 3 | 3 | 认知引擎 |
| kgraph | 3 | 3 | 知识图谱 |

### 2.2 reserve（保留，代码不动，不承诺演进）

vms（13 文件）、market、a2a_memory、session_recorder、views、eval、daemon、
coze_bridge、self_test、governance、qa、bridges、a2a_registry、utils

### 2.3 frozen（冻结：无 core 引用，代码保留可考古，有证据可复活）

benchmark_scripts、tests（独立测试）、migrations、cluster、**neuromorphic
（loihi/truenorth 仿生）**、cli、elicitation、pipeline、plugins、scripts

> 注：frozen 不等于删除——仿生/储备模块（neuromorphic 等）保留为论文对齐参考
> 与未来孵化素材，见第 5 节大脑化路线图。

## 3. 成长规则（Evidence-Gated Evolution）

1. **新能力先进 experiments 沙盒**（reserve 区内），不直接进 core；
2. **晋升门槛**：A/B 基准（复用 500q/benchmark 框架）证明延迟/命中/成本不劣化，
   且 core 引用 >=3 处，方可晋升 core；
3. **冻结规则**：连续 2 轮审计（约 60 天）无 core 引用 → frozen；frozen 模块保留，
   有证据可随时复活；
4. **维护承诺**：core = 必须可测试（测试数>0）、可监控、可回滚；
5. **定期审计**：维护链 -Tasks audit 每轮重扫分级（scripts/module_classify.py）。

## 4. 大脑化方向（不变，且受益于分级）

- **大脑核心（运行时）**：Second Brain 引擎、agent_brain、47 通道检索、
  记忆分层（Core/Recall/Archival）、睡眠式整合、自进化、遗忘曲线——全部 core，持续演进；
- **仿生储备（冻结保留）**：neuromorphic（loihi/truenorth）、brain 实验模块——
  代码保留为论文对齐素材，不承诺维护；
- **新仿生孵化（experiments）**：见第 5 节优先队列。

## 5. 大脑化路线图：下一批最值得孵化的仿生能力（experiments 优先队列）

| 优先级 | 能力 | 认知科学依据 | 现有基础 | 孵化路径 |
|---|---|---|---|---|
| P0 | 突触权重衰减（遗忘曲线的物理化） | 海马体→皮层巩固 | access_count/last_accessed 已有 | 增强 decay 引擎，A/B 对照 R@5 |
| P0 | 双过程记忆（System1 快检索/System2 慢推理） | Kahneman | modules/second_brain/dcpm 已 reserve | 复活 DCPM，接 retrieval 门禁 |
| P1 | 情境依赖检索（context-dependent memory） | 编码特异性原则 | session/persona 维度已有 | 检索时注入情境向量，A/B |
| P1 | 睡眠式整合（已有雏形） | 记忆巩固 | sleep_consolidation 在链 | 升级真实 LLM 摘要（decay-llm） |
| P2 | 情节→语义泛化（episodic→semantic） | 海马体重放 | memory_replay_trainer 已 reserve | 重放训练晋升实验 |
| P2 | 多巴胺式价值编码（importance 强化） | 强化学习 | brain/value_encoder 已 reserve | 接 importance 权重，A/B |

## 6. 存储演进路径

| 规模 | 动作 | 触发 |
|---|---|---|
| 28k（现在） | 现状（pgvector fp32 + HNSW） | — |
| 100k | halfvec（存储-50%）+ 分块策略 + rerank 常驻 | >80k |
| 1M | 索引分区 + 缓存分层 + PgBouncer | >500k |
| 多机 | 流复制 + Patroni | 7x24 需求 |

## 7. 维护承诺

- 本文件每次模块分级变更同步更新（维护链 audit 任务自动提示）；
- core 模块冻结需本文件记录理由；
- 本文件由记忆系统自动归档（重要程度 0.9）。
---

---
---

# 大脑化全景（2026-09，EXECUTION 116-456 更新；457 轮情境流/语义视觉落地）

> Trinity 的认知机制（全部运行时生效）。59 项注册能力（EXECUTION 240 全验证）；
> 大脑模块索引 110 个/7 层（brain/MODULES.md，EXECUTION 298）。

## 记忆类（17）
| 机制 | 轮次 |
|---|---|
| DCPM 双过程 / 突触衰减 / Hebbian / 对比训练 / 情境依赖 / 重放泛化 / 价值编码 / 图谱×2 / 感知（日志/文件/视觉/网络）/ 主动遗忘 / 梦境回放 / 情绪记忆巩固 / 跨域重组梦境 / 记忆重构 / 元记忆 / 记忆管理器 |

## 认知类（13）
| 机制 | 轮次 |
|---|---|
| 元认知置信 / 置信评分 / serendipity / intent / 预测编码 / 连续状态 / 情绪状态机 / 情绪调节 / 认知编排层 / 好奇心 / 预测-行动环 / 注意力控制 / 认知灵活性 / 心理模拟 |

## 自我/意识类（7）
| 机制 | 轮次 |
|---|---|
| 会话身份 / 全局自我 / 自我反思 / 自我评估 / 叙事自我 / 自我预测 / 自我公理（6/6）/ 意识蓝图（85/100，情境 9/10）/ 情绪公理（5/5）/ 情境持续流 / 语义视觉 / 社会闭环（市场真实成交） |

## 社会类（5）
| 机制 | 轮次 |
|---|---|
| 市场（交易/背书/举报/信誉）/ 知识传播 / 心智理论 / 观察学习 / 社会记忆 |

## 行动类（5）
| 机制 | 轮次 |
|---|---|
| 行动回路 / 条件反射 / 多巴胺奖赏 / 习惯形成 / 主动发起 |

## 网络方案落地（25 个，2026 前沿）
Amaya/Active Perception/Interoceptive/Mental Workspace/Blueprint/Active Inference/MATE/affective-episodic/UVT-LM/Hindsight/Agentic Memory/MUSE/ECA/ToM/心理模拟/SAA/情绪调节/Discovery by Dreaming/Anima/Dopamine/Social Learning/Metamemory/Cognitive Flexibility/Habit Formation/记忆重构

## 保障体系
- 30 维护任务（+situation 情境流）/ 4 维守卫（系统/大脑化/能力/闭环）/ 59 能力注册
- 自我公理 6/6（120 分）/ 情绪公理 5/5（100 分）
- 10 闭环审计 / 30+ 测试 / OPERATIONS 手册

## 大脑距离（最近一次官方自评 = EXECUTION 240；219→240 变化：认知+1 意识+2 社会+5）
记忆 97% / 认知 96% / 意识 72% / 感知 85% / 社会 95%

> 457 轮（2026-09-02 大脑化体检优化）落地后新增可测证据：
> - 意识蓝图 82 → **85/100**（情境感知判据 6 → **9**：情境=持续上下文流，非按查询现算）
> - 情境流：situation_stream.json + PG ctx:brain 双写，检索自动注入"当下"
> - 感知：语义级画面理解接通（本地 qwen2.5vl:3b；/memory/perceive 语义优先特征降级）
> - 社会：第二真实 agent（ops-bot）种子 + 记忆市场真实成交（tx 完成）+ ToM 推断命中 8/8
> - 图谱：entities 187 → 3,188 / relations 980 → 17,803（graph_densify 日门幂等）

# 命题化 v2 设计（PROPOSITION_V2）

> 日期：2026-08-18 | 状态：**设计稿（phase=design）** | 关联 goal：goal-25064570
> 目的：为 LongMemEval_S **multi-session ≥55%**（当前 49.6% 上限）提供唯一被证据支持的技术路线——**写路径一次性命题化提取**。
> 前置证据：round 39（docs/MODEL_AB_VERIFICATION_20260818.md）+ round 38 multi A/B（EXECUTION.md）。

## 一、为什么当前路线不行（round 38/39 证伪链）

| 方案 | 结果 | 结论 |
|---|---|---|
| 检索时逐 session 提取（慢版） | 3.2 分钟/题，全量 7h（6275 次提取调用） | **成本不可接受** |
| 检索时按题聚合提取（快版，15000 字符截断） | 0.75%（1/133）灾难 | 聚合截断丢失 47 个 session 关键事实 |
| 检索侧提示词路线（dates+chrono 重排） | 14.3% | 破坏跨会话证据完整性 |
| 检索侧 turn 粒度 top-16 | **49.6%**（最优） | 当前可实现上限，未达 55% |
| 生成模型升级（deepseek-v4-pro） | 24.3%（53/74 ERR） | 推理格式需专门适配，收益未验证 |
| 判分口径（三票 vs 单票） | 三票 +14.9pp 稳定 | 口径已稳健，不能解释主要差距 |

**核心结论**：multi 弱的根因是“整段 verbatim 存储，跨事实聚合靠 LLM 在长原文上硬推”；
唯一出路是 **PlugMem 类写入时命题化**——把对话提炼成可聚合、可推理的原子命题。
round 39 同时裁定：**收益需按全量口径打折**（50 题 72% 是乐观估计，全量基线仅 63.2%）。

## 二、设计原则：写路径一次性提取（摊销成本）

- **时机**：ingest（写入）时同步提取命题，而非检索时——把 6275 次/题 的检索时成本摊销为 每会话 1-5 次 写入时成本。
- **原子性**：一条命题 = 一个事实（用户偏好/用户事实/用户做过/agent 做过），不总结整段。
- **并存策略**：命题作为**新的记忆条目**与 verbatim 并存（verbatim 保 recall，命题提 precision），
  命题条目带 proposition_type + temporal（发生时间/有效期）元数据。
- **渐进开关**：TRINITY_PROPOSITION_EXTRACT 环境变量控制，默认 off——不改变现有行为，风险隔离。

## 三、架构设计

写入路径（现有）              写入路径（v2 增量）
TrinityClient.ingest ───────► 命题提取器 (LLM, 4 类命题: 用户偏好/用户事实/用户做过/agent 做过)
   │                          - 原子化 + 时间戳 + 来源引用
   ▼                          ▼
store_memory(verbatim)        store_memory(proposition, category=proposition,
   │                          metadata: proposition_type/temporal/source_memory_id)
   ▼                          ▼
FTS5 + 向量 + 图谱            FTS5 + 向量（天然被 hybrid 检索命中）

检索侧（复用现有）：hybrid search 命中命题条目 → multi 聚合时 LLM 对
原子命题综合（而非在长原文上硬推）→ 时间命题直接支撑 temporal 排序。

### 3.1 命题提取提示（Few-shot，4 类）

- 输入：当前 turn 的 user/assistant 消息 + 已有命题（去重/冲突参考）。
- 输出：JSON 数组 [{type: user_preference|user_fact|user_done|agent_done, proposition, ts, expires}]。
- 约束：一条命题一个事实；不做总结；与已有命题冲突时标记 supersede。

### 3.2 存储 schema（复用 memories 表 + metadata）

- category=proposition；tags=[proposition, type]；
- metadata.proposition_type、metadata.temporal、metadata.source_memory_id（指向 verbatim 原文）；
- 重要性：提取器置信度映射（0.5-0.9），高置信偏好/事实 ≥0.7。

### 3.3 检索集成（最小改动）

- 无需新检索通道：命题条目进 FTS+向量后，现有 hybrid 自然命中；
- multi 聚合 prompt：命中命题时优先用命题 + 按 temporal 排序（替代整段 verbatim 硬推）；
- RouteReasoner 增加 proposition 路由分支（可选，M4 再做）。

## 四、成本估算

| 项 | 估算 |
|---|---|
| 每次 ingest 提取调用 | 1 次 LLM（输入 ~800-1500 tok Few-shot，输出 ~200-400 tok JSON） |
| 每会话摊销 | 1-5 次提取（对比检索时 6275 次/题）——**降低 3 个数量级** |
| 存储增长 | 命题条目 ≈ verbatim 的 20-40%（原子命题短） |
| 延迟影响 | 写入 +1 次 LLM 往返（可异步化：ingest 先落 verbatim，命题后台补） |
| 成本控制 | TRINITY_LLM_API_KEY（deepseek-chat 已有）；限流 + 每日上限 |

## 五、收益预估（按全量口径打折）

| 题型 | 当前（全量） | 命题化 v2 预期 | 依据 |
|---|---|---|---|
| multi-session | 49.6% | **55-60%** | 原子命题聚合替代整段硬推（PlugMem 类方案同向） |
| temporal-reasoning | 65.4% | 70%+ | 时间命题 + 有效期直接支撑排序 |
| single-session-preference | 56.7% | 60%+ | 结构化偏好命题替代启发式 |
| 整体 | 68.6% | **70-72%**（乐观） | 按 round 39 打折口径 |

⚠️ 以上为设计预期，**必须以 50 题同批 A/B（seed42 + judge3）实测为准**；
按 round 39 裁定，50 题乐观口径需按全量打折（参考：route2 50 题 72% → 全量 63.2%）。

## 六、里程碑（不启动全量实验）

| 里程碑 | 内容 | 出口标准 |
|---|---|---|
| M1 设计评审 | 本文档 | 评审通过 |
| M2 原型 | ingest 钩子 + 提取器（5 题冒烟） | 5/5 提取质量可接受，verbatim 不受影响 |
| M3 50 题 A/B | prop vs verbatim（seed42，judge3 三票） | multi 49.6%→55%+ 且 temporal/pref 不倒退 |
| M4 全量 500 | 若 M3 有效 | 全量口径 multi ≥55%，整体不倒退 |

## 七、风险与缓解

| 风险 | 缓解 |
|---|---|
| 提取质量不稳定 | 提取后校验（空/超长丢弃回退 verbatim）；置信度门槛 |
| 命题噪音/重复 | 与已有命题去重（content_hash）+ 重要性过滤 |
| 写入延迟增加 | 异步提取（后台任务）或同步+缓存 |
| LLM 成本 | 限流、批量、开关默认 off |
| 与现有 dedup/echo 清理冲突 | 命题 category=proposition 单独治理路径 |

## 八、外部依赖与开放问题

- ✅ TRINITY_LLM_API_KEY / DEEPSEEK_API_KEY 已有（deepseek-chat）；
- ❓ 提取提示的稳定性需要原型验证（Few-shot 质量）；
- ❓ 命题与 verbatim 的检索融合权重（RRF 已有，命题是否加权待 M3 定）；
- 不依赖外部数据集/网络（全量 500 已有本地）。

## 九、参考

- docs/OPTIMIZATION_PLAN_20260817.md（PlugMem 路线 P0-1/P0-2）
- docs/MODEL_AB_VERIFICATION_20260818.md（round 39 证伪证据）
- EXECUTION.md 第 38/39 轮（multi A/B、判分口径）

# Trinity 评分优化方案（基于网络最优方案 PlugMem 命题化路线，2026-08-17）

> 目标：judge3 口径下 LongMemEval_S 50 题从 72%（route2）提升至 80%+（PlugMem 通用模式 82.8 的本地等价）
> 约束：全量 500 不跑（用户指示）；50 题同批 A/B（seed42）+ judge3 判分；全部本地不发布
> 依据：网络调研（PlugMem ICML 2026 90.2%、Maximem 92%、MemPalace 96.6% R@5 口径）+ 我们历轮实测

## 一、问题诊断（我们的弱项 = PlugMem 的强项）

| 题型 | 我们（judge3） | 网络最优 | 差距原因 |
|---|---|---|---|
| multi-session（聚合） | 40% | PlugMem aggregation 强 | 整段 verbatim 存储，跨事实聚合靠 LLM 在原文上硬推 |
| temporal-reasoning | 62% | PlugMem temporal 强 | REL 时间线已加，但事实仍是整段，无结构化时间命题 |
| single-session-preference | 10-60% | PlugMem 分离用户/agent 事实 | 无结构化偏好命题（ppro 启发式已证伪，pref3 LLM 两段式有效 36-60%） |
| knowledge-update | 71% | - | 新旧命题裁决靠提示词（freshness 模块已证伪） |
| single-session-user/assistant | 87-100% | - | 已达标 |

**核心诊断**：我们是 MemPalace 的 verbatim 检索路线（不丢信息、recall 高 96.8%），缺的是 PlugMem 的
**写入时命题化**（把对话提炼成可聚合、可推理的知识单元）——这正是 aggregation/temporal/preference 三类
弱题型需要的。

## 二、方案总览：写入时命题化（借鉴 PlugMem，按 ROI 分层）

### P0 命题化管线（预计 +5~10pp，核心）

**P0-1 任务特定命题提取（PlugMem Step 1）**
- 提取提示明确四类命题：用户偏好 / 用户事实 / 用户做过的事 / agent 做过的事（Few-shot 示例）
- 不总结整段，输出原子命题（一条命题一个事实）
- 落地：ingest 后调用 LLM 提取命题，命题作为记忆内容（替代/并存 verbatim）

**P0-2 细粒度提取单元（PlugMem Step 2）**
- 按 turn（单条消息）提取，而非整段会话——每个记忆项对应单一对话动作
- 解决 QA3（turn 级 ingest 慢）的痛点：只提取命题（小），不存整 turn

**P0-3 时间命题标注（temporal 专用）**
- 提取时要求带日期：date + 命题 -> 时间命题，检索时按时间过滤/排序
- 替代/增强 REL 时间线（已有 0-2pp）

### P1 命题推理与结构（预计 +3~5pp）

**P1-1 检索命题 + 推理链生成**
- 检索命中命题（非整段）-> 生成阶段先列出相关命题 -> 再推理作答（PlugMem retrieve_and_reason 简化版）
- multi：跨��话同一实体/主题的命题聚合后推理

**P1-2 偏好命题独立索引（preference 专用）**
- 用户偏好类命题单独标记（category=preference），检索加权
- 两段式：偏好命题摘要 -> 个性化回复（pref3 已验证 36-60%，命题化使其结构化）

**P1-3 新旧命题裁决（knowledge-update）**
- 同主题命题按时间戳排序，最新优先（非 freshness 打分——已证伪）
- 保留冲突链（Trinity CRDT 已有基础）

### P2 评测基建与产品化

**P2-1 命题化 A/B 脚本**：lme_prop.py（--mode verbatim|prop|prop+time|prop+pref，50 题同批）
**P2-2 judge 口径记录**：DeepSeek judge3 vs 论文 GPT-4o 口径差异（无 GPT-4o 则记录为已知差）
**P2-3 产品化**：TRINITY_LLM_EXTRACT 从实体提取升级为命题提取（写路径，异步）

## 三、验证计划（每项 50 题同批 A/B + judge3）

| 步骤 | 实验 | 对比 | 验收 |
|---|---|---|---|
| 1 | verbatim（route2 现状） | 基线 | 72% |
| 2 | 命题化（P0-1+2） | vs 1 | 整体提升，multi/temporal 观察 |
| 3 | 命题化+时间命题（P0-3） | vs 2 | temporal 提升 |
| 4 | 命题化+偏好独立（P1-2） | vs 3 | preference 提升 |
| 5 | 命题化+推理链（P1-1） | vs 4 | multi/temporal 再提升 |
| 6 | 最优组合 | vs 1 | 目标 80%+ |

## 四、成本与风险（诚实标注）

| 项 | 说明 |
|---|---|
| LLM 成本 | 命题化每次 ingest 多 1 次调用：50 题 x ~40 turn x 1 = ~2000 次/轮（约 20 分钟），可控；可降为按会话提炼（约 50 次） |
| 信息损失 | 命题化有提炼质量依赖——用 verbatim 对照组保底（P2-1 的 verbatim 模式） |
| 模型能力 | deepseek-chat vs 论文 GPT-5.4：temporal 强推理可能仍受限于生成模型，部分差距无法靠方法补齐 |
| judge 口径 | DeepSeek judge3 vs GPT-4o：无法对齐时记录为已知差，不追求数字等同 |
| 并发工作流 | 仓库有另一工作流活跃（multi 诊断中），脚本用唯一文件名避免冲突 |

## 五、预期收益汇总

| 配置 | 预期（judge3） | 依据 |
|---|---|---|
| route2（现状） | 72% | 实测 |
| +命题化（P0） | 77-82% | PlugMem 通用模式 82.8 的本地等价（DeepSeek 口径打折） |
| +命题推理/偏好索引（P1） | 80-85% | 组合叠加 |
| 理论极限（换 GPT-5.4 级模型 + GPT-4o judge） | 85-90% | 口径对齐后接近网络最优 |

## 六、一句话

**从 verbatim 检索路线升级为 PlugMem 式写入时命题化**：任务特定提取（偏好/事实/行为四类）+ 按 turn 细粒度 + 时间命题 + 命题推理��——直击 multi/temporal/preference 三大弱项，5 步 50 题 A/B 逐项验证，目标 judge3 80%+；风险项（信息损失/模型能力/judge 口径）全部有对照组或记录策略。

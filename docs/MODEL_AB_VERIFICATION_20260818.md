# 模型能力与判分口径验证（2026-08-18，goal-4b2）

> 目的：用数据决定 multi ≥55% 的命题化管线重构是否值得投入。

## 一、生成模型 A/B（同批 74 题，seed42，RouteReasoner）

| 模型 | majority（judge3 三票） | ERR 数 | 备注 |
|---|---|---|---|
| deepseek-chat（当前生产） | **59.5%（44/74）** | 0 | 非推理模型，content 直出 |
| deepseek-v4-pro | 24.3%（18/74） | **53** | 推理模型：输出在 reasoning_content，content 为空 + finish_reason=length |

- **结论：v4-pro 不是 drop-in 替代**——推理模型响应格式不同（reasoning_content + 推理耗尽 max_tokens），需专门适配；deepseek-chat（v4-flash 类）是当前正确的生产选择。
- 附带发现：deepseek-chat / v4-flash / v4-pro 在简单与 temporal 探测题上均正确，能力接近；差异在格式与长文本推理。

## 二、判分口径对照（同批 74 题 deepseek-chat 答案）

| 判分方式 | 准确率 | 说明 |
|---|---|---|
| judge3 三票 majority（reason-first 提示） | **59.5%** | 当前生产口径 |
| 单票（简化提示 'Answer exactly YES or NO'） | 51.4% | 提示词不同 → -8.1pp |
| 单票（judge3 的 reason-first 提示） | 44.6% | 同提示但单票 → 比三票 -14.9pp |

- **结论：三票 majority 显著提升判分稳定性（+14.9pp vs 同提示单票）**；判分差异主要来自提示词设计与票数，而非 judge 模型本身。
- 网络方案 80-90% 用 GPT-4o 单票口径，与我们的 DeepSeek 三票口径**不可直接对比**——差距中被口径解释的部分有限，真实差距仍在生成能力/方法。

## 三、对命题化重构的决策建议

1. **模型升级路线暂不成立**：v4-pro 需推理格式适配，收益未验证，成本高于命题化。
2. **判分口径不能解释主要差距**：三票口径本身稳健，与网络单票的差异不是我们落后的主因。
3. **命题化重构仍是 multi ≥55% 的主要候选**，但：
   - 两条 A/B 已失败（成本/实现），需全新设计（写路径一次性提取摊销成本）；
   - 预期收益（+5~10pp）按全量口径会打折；
   - **建议**：若用户追求 multi 单项突破，命题化重构值得做（独立大工程）；若追求整体性价比，当前 68.6% 已是稳固基线，可优先把并行工作流的 dsh_events_source（真实事件源）落地。

## 四、产物

- 同批 74 题答案：~/.trinity/bench-official/model_ab_chat_74.json / model_ab_pro_74.json
- 判分：~/.trinity/bench-official/judge3_model_ab_74.json
- 本报告：docs/MODEL_AB_VERIFICATION_20260818.md
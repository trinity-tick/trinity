# Trinity 优化建议（基于官方 LongMemEval_S 实测数据，2026-08-16）

> 数据来源：docs/bench-official/LongMemEval_S_REPORT_20260816.md + .trinity/bench-official/ 实测产物

## 一、数据诊断（500 题全量实测）

| 题型 | n | session_R@5 | QA accuracy | 诊断 |
|---|---|---|---|---|
| single-session-assistant | 56 | 1.000 | **0.911** | 检索/生成都优秀 |
| single-session-user | 70 | 0.986 | **0.843** | 良好 |
| knowledge-update | 78 | 0.987 | **0.615** | 生成需强化新答案优先 |
| multi-session | 133 | 0.977 | **0.384** | 跨会话综合弱 |
| temporal-reasoning | 133 | 0.955 | **0.286** | 时间推理弱（数字类 7/64 真错） |
| single-session-preference | 30 | 0.833 | **0.033** | 生成策略不对（复述问题） |
| **整体** | 500 | **0.968** | **0.496** | 检索头部区间；QA 生成是主短板 |

**核心判断**：检索层已达目标（96.8%，与 Awareness/MemPalace 同级），**瓶颈在 QA 生成链路**——
尤其是 temporal（缺时间戳）、preference（生成策略错）、multi-session（缺跨会话综合）。

## 二、优化清单（按 ROI 排序）

### P0-1 时间感知上下文（temporal 0.286 → 目标 0.6+）
- 现状：LongMemEval 每题带 haystack_dates（会话时间戳），**当前评测完全没用**；Trinity 记忆有 created_at 但 QA 上下文未注入。
- 动作：QA 上下文给每段会话标注时间；temporal 题型用分步推理提示（列出日期证据 → 计算差值 → 结论）。
- 证据：数字类 temporal 仅 7/64 答案含期望数字——需要时间戳 + 推理链才能算对。
- 产品落地：Trinity 检索可返回记忆时间元数据；对何时/几天前类查询走时间推理提示。

### P0-2 分题型生成策略（preference 0.033 / multi 0.384）
- preference 现状：模型把用户问题复述回去，期望是【基于用户偏好生成个性化回复】。
- 动作：preference 提示改为【先总结该用户偏好 → 再给出符合偏好的回复】；multi-session 提示改为【这些片段来自不同时间的会话，综合全部信息回答】。
- 产品落地：查询意图检测（偏好/跨会话/时间类）→ 路由不同生成提示（Trinity 已有 intent 分层检索基础）。

### P1-1 preference 检索召回（0.833 最低）
- 动作：偏好类查询提高偏好记忆权重（category/tag 过滤 + 加权）；评测上 top-5 → top-10 对比。

### P1-2 judge 噪声治理
- temporal 中 39/127 被判错答案含 >50% 期望 token——部分可能是 judge 误判（如 3rd of June vs June 3rd）。
- 动作：judge 提示加先对比再判定；抽检 50 题人工校准；报告严格/宽松双口径。

### P2-1 检索粒度（turn_recall 0.922 vs session 0.968）
- 会话级 ingest 粗粒度；turn 级 ingest 慢（每题约 4000 次带审计写入）。
- 动作：会话内二次检索（先 top-5 会话 → 会话内按查询定位证据 turn）→ 上下文更聚焦，QA 也应提升。

### P2-2 写入路径性能（真实 LLM 提取 4.5s/条）
- TRINITY_LLM_EXTRACT=on 同步阻塞 4.5s/条；已存在 TRINITY_LLM_EXTRACT_ASYNC=on（异步后台提取）。
- 动作：产品默认开异步（检索不依赖 LLM 提取，实测召回不受影响）。

### P3 评测基础设施
- 脚本参数化 --prompt-strategy（plain/temporal/preference/multi）；保存每题检索上下文便于复盘；
- 补跑：LoCoMo 英文官方集、BEAM 官方 10M 口径（网络允许时）。

## 三、实测结果（2026-08-16 已执行，500 题全量验证）

### 3.1 已生效的优化

| 优化 | 实测结果 | 状态 |
|---|---|---|
| **P0-1 时间戳 + temporal 分步推理**（dated 模式） | **temporal 0.286 → 0.444（+15.7pp）**；整体 0.496 → **0.540** | ✅ 已落地（benchmark/lme_qa_opt.py --mode dated） |
| **P2-2 异步 LLM 提取默认** | TRINITY_LLM_EXTRACT=on 默认异步（TRINITY_LLM_EXTRACT_SYNC=on 强制同步）；5 测试通过 | ✅ 已落地（trinity/core/client.py + tests） |
| **P1-2 judge 双口径** | 官方模板 vs reason-first 一致性 91.7%；官方数字未被高估 | ✅ 已落地（benchmark/lme_judge2.py） |
| **P3 评测参数化** | --mode plain/dated/types/inner + 每题上下文落盘（--ctx-out） | ✅ 已落地 |

### 3.2 负面结论（避免返工）

- **分题型专用生成提示（preference/multi/KU）为负优化**：50 题 A/B 中 preference 提示诱发
  13 个 UNKNOWN、multi 提示把 multi-session 从 0.471 打到 0.176。**强基底提示 + 仅 temporal
  加分步推理 = 当前最优组合**。preference（n=30）在所有配置下均 ~0.03——需要完全不同的
  生成策略（如检索到偏好后直接生成推荐语句），而非提示词微调。

### 3.3 第二轮优化实测（2026-08-17，50 题 A/B + 偏好 30 题定向）

| 优化 | 实测结果 | 状态 |
|---|---|---|
| **preference 两段式（pref2）** | 30 题定向：**3.3% → 16.7%（+13.3pp）**；答案质量 5 倍提升（个性化推荐 vs 复述问题） | ✅ 已落地（lme_qa_opt2.py --pref2 + lme_pref_ab.py） |
| **inner2 内检索精调** | 50 题 A/B：temporal 54.5% → **63.6%**（+9pp） | ✅ 已落地（--inner2，查询词 turn + 前 2 兜底） |
| **multi2 温和跨会话** | 单独启用负优化（multi 47%→35%）；all2 组合中回到 47% | ⚠️ 单独无效，依赖 inner2 补偿 |
| **all2 组合** | 50 题：**64% vs dated 基线 62%**（+2pp） | ✅ 推荐组合（--pref2 --multi2 --inner2） |
| LoCoMo 英文官方集 / BEAM 官方 | **网络阻塞**：HF 被墙、GitHub raw 超时、LoCoMo 仓库 404 | ⛔ 环境不可行 |

> 预计全量 500 影响（推算，未跑全量——用户指示延后）：preference +13.3pp×6% + temporal +9pp×26.6%
> ≈ 整体 +2~3pp（54.0% → ~56-57%），待全量验证。

### 3.4 剩余机会（按验证过的方向）

| 优化 | 状态 | 说明 |
|---|---|---|
| 全量 500 验证 all2 | 延后（用户指示） | all2 = 50 题 64%，全量验证待命 |
| preference 进一步优化 | 待做 | 16.7% 仍低：stage-1 摘要可对齐 rubric 期望点（如"参考用户最近成功经验"） |
| multi 独立优化 | 待做 | 温和提示单独无效，需换思路（如跨会话证据拼接） |
| LoCoMo / BEAM 官方 | 待网络 | HF/GitHub 受限，需代理或离线数据 |

## 三、第三轮优化实测（2026-08-17，同批同 seed 42 A/B）

> 脚本：benchmark/lme_qa_opt3.py（--variant baseline/timeline/stitch/pref3 + --no-inner2）
> 判分：benchmark/judge_ab.py（官方分题型模板）；产物：.trinity/bench-official/ab_*.json

### 3.5 P0 时间线形式化（timeline，Chain-of-Timeline 式）— temporal-reasoning 50 题

| 配置 | QA accuracy | Δ |
|---|---|---|
| baseline（dated + inner2） | 0.580 | — |
| **timeline（REL 相对天数标注 + 时间线排序）** | **0.600** | **+2.0pp** |

- 实现：检索 top-5 会话后按日期排序，每条注入 [REL: N days before question date]（相对提问日天数），
  提示要求"列出日期+相对天数 → 按日期差计算 → 作答"。
- 样例证据（QID 8077ef71）：baseline 答 "0 days ago"（误用对话日期 2022/03/09 而非提问日 2022/04/04），
  timeline 答 "26 days" ✓——REL 锚点修正了"提问日锚点"错误。
- 结论：有效但温和（+2pp）；REL 只救了 64/133 的 how-long/many 类，排序类/文本类不受益。

### 3.6 P1 跨会话证据拼接（stitch）— multi-session 50 题

| 配置 | QA accuracy | Δ |
|---|---|---|
| baseline（dated + inner2） | 0.360 | — |
| stitch（时间线排序 + 逐会话抽取再综合） | 0.340 | -2.0pp |

- 结论：**无效**。与 multi2（温和跨会话提示）负优化结论一致——提示词路线对 multi-session 是死路，
  需换非提示词思路（如跨会话证据结构化拼接 / 检索粒度改造）。

### 3.7 P2 rubric 对齐两段式（pref3）— single-session-preference 30 题

| 配置 | QA accuracy | 备注 |
|---|---|---|
| baseline + inner2 | 0.067 | 2/30 |
| pref3 + inner2 | 0.100 | 3/30（+3.3pp） |
| **baseline 无 inner2** | **0.200** | **6/30 —— inner2 对 pref 伤害 -13.3pp** |
| pref3 无 inner2 | 0.033 | 1/30（judge 噪声 ±3.3pp/题） |

- **关键发现：inner2（query-term turn 过滤）对 preference 类是重伤害（-13.3pp）**——
  偏好证据分散在隐式表达中，按查询词过滤会把偏好线索滤掉。产品化必须**按题型路由**：
  temporal 用 inner2（+9pp），preference 禁用。
- judge 噪声确认：30 题小样本 ±1 题 = ±3.3pp，且同质量答案判定随机（QID d6233ab6 等两边答案都贴合
  rubric 但判定相反）。pref 数字必须先治理 judge（reason-first 双口径）才可信。
- pref3 答案质量确实提升（具体推荐贴合用户工具链：Premiere 高级资源、Sony 配件等），但 judge 测不准。

### 3.8 本轮结论

1. **timeline（REL 天数标注）是唯一可分辨的正向增益**（+2pp，temporal 60%）；与网络最优方案
   （Chain-of-Timeline 时间线形式化 / time-aware query expansion）方向一致；
2. **inner2 必须按题型路由**（temporal 开 / preference 关）——这是产品化硬约束；
3. **pref 瓶颈在 judge 噪声 + 小样本**，先治理 judge 再谈 pref 提升；
4. **multi 提示词路线确认死亡**，需换思路（结构化证据拼接 / 检索粒度）。

> 下一步（按 ROI）：①judge 治理（reason-first 双口径全量）→ ②按题型路由上下文策略（timeline+inner2 for
> temporal；两段式无 inner2 for preference）→ ③multi 换非提示词思路 → ④最后才跑全量 500 锁定。

### 3.9 储备模块启动验证（2026-08-17，50 题同批 A/B seed42，脚本 lme_qa_opt_reserve.py）

| 配置 | 整体 | preference | temporal | KU | multi |
|---|---|---|---|---|---|
| base（dated 基线） | **64%** | 0% | 54.5% | 71.4% | 52.9% |
| **reserve（5 储备模块全开）** | **66%** | **50%** | 54.5% | 71.4% | 52.9% |

- **ppro_profile_retrieval ✅ 唯一有效**：UserProfileDeriver（启发式画像）→ LLM 生成，preference 0%→50%
  （样本 n=2，需与 judge 治理后的大样本复核；但方向与 3.7 的"两段式无 inner2"结论一致——画像=两段式 stage-1 的算法化）
- **chronos_temporal_memory ⚠️ 无增量**：本次事件提取为"首句粗粒度"（EventTuple.object=首句），时间线信息不足；
  需 LLM 细粒度事件三元组提取 + 相对日期对齐（可结合 3.5 的 timeline REL 方案）才可能生效
- **freshness_conflict_resolver ⚠️ 无增量**：dated 基线已有 [DATE:]，[FRESH:] 排序未带来新信息
- **query_intent_router / post_retrieval_evidence_policy ⚠️ 仅名义激活**：需完整接入（意图分类路由 + 证据链构建）后再测
- 产品化建议：ppro 画像接入 preference 生成路径（配合按题型路由，preference 禁用 inner2——见 3.7 关键发现）

## 四、一句话

**检索已达标（96.8% 头部），下一步全部投入在【上下文工程 + 生成策略】：把时间戳注入上下文、按题型路由生成提示、异步化 LLM 提取**——这是从 49.6% 走向 70%+ 的三步，也是评测证明 Trinity 深度能力的关键。

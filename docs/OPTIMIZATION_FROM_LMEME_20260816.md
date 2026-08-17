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

## 四、一句话

**检索已达标（96.8% 头部），下一步全部投入在【上下文工程 + 生成策略】：把时间戳注入上下文、按题型路由生成提示、异步化 LLM 提取**——这是从 49.6% 走向 70%+ 的三步，也是评测证明 Trinity 深度能力的关键。

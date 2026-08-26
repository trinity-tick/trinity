# Trinity 进化全景：四轮借鉴优化梳理（2026-08-26）

> 本文档对"四轮借鉴优化"（PageIndex → Budibase → Codex → DSH）前后的 Trinity 做全方位对比与现状盘点。
> 基线 = 本轮优化开始前（EXECUTION.md 第 20 节之前）；权威变更记录见 `dsh-ops/EXECUTION.md` 第 20-26 节。

---

## 一、核心指标前后对比

| 维度 | 优化前（基线） | 优化后（当前） | 变化 |
|---|---|---|---|
| 500q AnswerAcc（keyword 基线） | 0.678（GEN-1 记录） | 0.726（同 harness 重测） | +0.048 |
| 500q AnswerAcc（**reason 模式**） | —（不存在） | **0.752** | 新增能力，+0.026 vs 基线 |
| 500q R@5（reason 模式） | — | **0.994**（超 keyword 0.992） | 新增能力 |
| MS 类目 R@5（reason） | — | 0.963（修复前 0.60） | 判题修复恢复 |
| TR 类目 AnswerAcc（reason） | 0.688 | **0.787** | +0.099 |
| SS-P 类目 AnswerAcc（reason） | 0.533 | **0.667** | +0.134 |
| 生产难查询 R@10（95 条近义改写） | —（无评测集） | keyword 0.432 / **reason 0.547** | 新增评测能力，+0.115 |
| hybrid R@5（+页树通道 novel_only） | 0.980（5 通道） | **0.984** | 页通道纯增益 |
| 专项单元测试 | 0（新增前） | **73**（7 个测试文件） | 新增 |
| 维护链任务 | 25 | **28**（+pagetree/eval/all） | +2 实质任务 |
| 记忆库规模 | 22,732 active | 22,732 active（页树 8,992 条入树） | 页树构建 |
| 页树 | —（不存在） | 43 类目 / 270 簇 / 117 簇有 LLM 摘要 | 新增 |
| 目标引擎 | —（无目标概念） | 2 个真实目标（1 complete + 1 active） | 新增 |
| API 端点 | 原生 | +**13 个**（/goals×4、/skills×2、/automation×4、/api/openapi.json 等） | 新增 |
| MCP 工具 | 9 | **11**（+skill_list/skill_load；memory_search 增 view/visibility 参数） | 新增 |
| 环境变量（TRINITY_*） | ~70 | **~90**（+自动化/路由/可见性等） | 新增 |

## 二、四轮借鉴逐一对比

### 第 1 轮：PageIndex（无向量树索引 + LLM 推理检索）→ 页式记忆检索

| PageIndex 机制 | Trinity 落地 | 证据 |
|---|---|---|
| 树索引（目录树） | `trinity/retrieval/pagetree.py`：category→簇→记忆页树，纯元数据建树（8,992 条/75s） | `~/.trinity/store/pagetree.json` |
| Flash 无 LLM 结构提取 | 簇轴 = persona→主标签→untagged；IDF 页打分 + 短查询守卫 + 隔离过滤 | 检索侧归因：+2 题独中 |
| 节点摘要（便宜模型） | `scripts/run_pagetree_summaries.py`（增量，deepseek-chat） | 117/270 簇有摘要 |
| LLM 树搜索（chat_model） | `mode="reason"`（候选=基础+页+向量，LLM 判题，base 填充不截断） | AnswerAcc 0.752 / holdout R@10 0.547 |
| 结果可溯源 | 结果带 page_path/page_title/page_node | API 返回 |

**关键修复**（过程中实证）：多租户隔离过滤、短查询守卫、IDF 加权、小簇 min_df、tokenize 去重、hybrid novel_only 通道（只增不减）。

### 第 2 轮：Budibase（Automations/表视图/行级权限/公开 API）→ 运维层三件套

| Budibase 机制 | Trinity 落地 | 证据 |
|---|---|---|
| Automations（触发器→动作链） | `trinity/automation/`：事件（memory.write/search、goal.updated）+ YAML 规则 + 动作（notify/exec） | E2E：emitted/matched/executed/failed 统计 |
| 表视图 | `trinity/views.py` + `~/.trinity/views.yaml` + `search(view=)` | 单元测试 9 例 |
| 行级权限（Row-level Security） | `trinity/security/visibility.py`（白名单+参数化）+ RBAC 角色规则（TRINITY_VISIBILITY_<ROLE>） | viewer 角色过滤实测 |
| 公开 API 一等公民 | `GET /api/openapi.json`（增强文档）+ 新端点 | 12 paths 文档 |

### 第 3 轮：Codex（沙箱+审批/rollout/resume/模型路由）→ 执行策略与可观测

| Codex 机制 | Trinity 落地 | 证据 |
|---|---|---|
| sandbox_mode + approval_policy | 动作 mode（read-only/auto/full）+ approval（never/on-failure/always）+ 命令白名单 | 白名单拦截实测 |
| 审批队列 | `pending.json` + `GET /automation/pending` + `POST /automation/approve` | approve 剥离 approval 防死循环修复 |
| Rollout JSONL | `~/.trinity/automation/rollouts/<date>.jsonl` + `scripts/rollout_inspect.py` | inspect 汇总实测 |
| resume | `run_pagetree_summaries` checkpoint（done/failed + --retry-failed） | dry-run 实测 |
| 模型路由（auto） | `resolve_model_for(task_type)`（TRINITY_LLM_ROUTING），接入 summarize/retrieval_judge | 编译+接入 |

### 第 4 轮：DSH（goal 引擎/eval 断言/skill 运行时）→ 目标驱动自进化

| DSH 机制 | Trinity 落地 | 证据 |
|---|---|---|
| create_goal/update_goal | `trinity/evolution/goals.py`（acceptance 验收、3 轮无进展 blocked、RLock） | E2E：REST 创建→evaluate→complete |
| eval 断言 | `trinity/eval/` + `scripts/run_evals.py`：10 个断言任务，维护链 -Tasks eval，evolution CERTIFY 集成 | 10/10 通过 |
| skill 运行时 | `trinity/skills/` + data/skills frontmatter + MCP skill_list/skill_load + REST /skills | 5 skills 注册 |
| 目标驱动进化 | evolution 周期完成自动评估 active goals | g_93a63 active（holdout R@10≥0.60） |

## 三、当前架构图

```
                          ┌─────────────────────────────────────────────┐
                          │             Trinity v8.2.0                 │
                          │   检索层：FTS5(默认) · hybrid-RRF · 页树     │
                          │   reason(LLM判题) · graph · semantic        │
                          ├─────────────────────────────────────────────┤
   API :8001              │  运维层：automation(事件规则+审批+rollout)   │
   MCP stdio/:8000        │  治理层：goal引擎 · eval断言 · visibility   │
   Gateway :8002          │  结构层：dsh_sessions/events/goals/todos    │
                          │  技能层：data/skills(frontmatter)           │
                          ├─────────────────────────────────────────────┤
                          │  存储：SQLite大库(22.7k) · pagetree.json    │
                          │        goals.json · automation/ · views.yaml│
                          │        PG镜像:5430 · 备份 14天              │
                          └─────────────────────────────────────────────┘
   维护链(28任务) ← supervisor ← autostart  ── 每日：pagetree/eval/decay/tiers/sync/backup...
   evolution 周期(OBSERVE→ANALYZE→PLAN→EXECUTE→CERTIFY) → evaluate_goals
   CERTIFY → eval_assertions(10项) → skills 沉淀(corrections.md frontmatter 保护)
```

## 四、新增资产清单

### 新模块（8 个）
- `trinity/retrieval/pagetree.py`（页树）、`trinity/automation/`（引擎+审批+rollout）、
  `trinity/views.py`（记忆视图）、`trinity/security/visibility.py`（行级规则）、
  `trinity/eval/`（断言评测）、`trinity/skills/`（技能运行时）、
  `trinity/evolution/goals.py`（目标引擎）、`trinity/api/openapi_spec.py`（API 文档）

### 新脚本（5 个）
- `scripts/build_memory_pagetree.py`、`scripts/run_pagetree_summaries.py`（checkpoint）、
  `scripts/rollout_inspect.py`、`scripts/run_evals.py`、`benchmark/pagetree_ab_compare.py`、
  `benchmark/hard_holdout_eval.py`（+JSON 输出）

### 新增 API 端点（13 个）
- 检索参数：/memories 增 view、visibility_rule；/memory/search 支持 reason 模式
- 自动化：/automation/stats、/automation/pending、/automation/approve
- 目标：/goals（GET/POST）、/goals/{id}、/goals/{id}/update
- 技能：/skills、/skills/{name}；文档：/api/openapi.json

### 新增环境变量（本会话）
- `TRINITY_AUTOMATION`（on 启用）、`TRINITY_AUTOMATION_ACTION`（防循环，引擎注入）、
  `TRINITY_PAGETREE_HYBRID`（hybrid 页通道）、`TRINITY_LLM_ROUTING`（任务分级模型路由）、
  `TRINITY_VISIBILITY_<ROLE>`（角色行级可见性）

### 新增测试（7 文件 / 73 用例）
- test_pagetree(12) / test_automation(15) / test_views(9) / test_visibility(12) /
  test_goals(7) / test_eval(11) / test_skills(7)

## 五、自进化闭环（当前形态）

```
目标引擎(acceptance) ──evolution 周期完成──▶ default_metrics(基准 JSON)
      ▲                                          │
      │ 达标→complete / 3轮无进展→blocked         ▼
      │                                   benchmark 指标
      │                                   500q AnswerAcc 0.752
      │                                   holdout R@10 0.547
      │                                          ▲
      │                                          │ 新优化迭代
      └── automation(事件) ◀── 维护链(eval 断言 10 项护栏) ◀── skills 经验沉淀
```

## 六、回滚矩阵

| 层 | 回滚方式 |
|---|---|
| 代码 | `git checkout -- <EXECUTION.md 26.4/25.4/24.4/23.4/22.4 列出的文件>` |
| 产物 | 删 `~/.trinity/store/pagetree.json`（重建即恢复）、`~/.trinity/goals.json`、automation/ 目录 |
| 数据 | 记忆/审计链不受影响（全部新增功能默认关闭或显式启用） |
| 配置 | 还原 `~/.trinity/views.yaml`、automation/rules.yaml（可删即回默认） |

## 七、已知遗留与后续方向

1. reason judge 在多事实题（MS 全量池）仍有提升空间（Acc 0.188 vs 0.25 基线类目差异）；
2. 页树启发式页定位在近义改写上仍弱（R@10 0.179）——下一步"摘要向量化页级检索"；
3. 目标 g_93a63（holdout R@10≥0.60）等待突破；eval 断言可再扩（automation pending 健康等）；
4. skills 经验沉淀可接入 automation 规则实现全自动；
5. 全量 pytest 基线待重测（新增 7 个测试文件后）。

---
*生成：2026-08-26 · 数据来源：EXECUTION.md 20-26 节、output/*.json、pytest 专项、代码盘点*

# Trinity Automation & Views — Budibase 借鉴（2026-08-26，默认关闭）

## 声明式自动化（trinity/automation/）

事件驱动规则层（借鉴 Budibase Automations）。启用：`TRINITY_AUTOMATION=on`。

事件（hook 已埋）：
- `memory.write` — ingest 成功后（payload: memory_id/importance/category/tags/persona/agent/modality）
- `memory.search` — search 返回前（payload: query/top_k/mode/hit_count/top_score）
- `goal.updated` — structure_store.goal_upsert 状态变化

规则文件 `~/.trinity/automation/rules.yaml`（与内置 DEFAULT_RULES 合并，同名覆盖）：

```yaml
rules:
  - name: high-importance-notify
    trigger: memory.write
    condition: {field: importance, op: gte, value: 0.85}
    actions:
      - {type: notify, message: "high-importance memory: {memory_id}"}
      - {type: exec, python: "mymodule:myfunc", args: {memory_id: "{memory_id}"}}
```

条件算子：eq/ne/gt/gte/lt/lte/contains/in/not_in。动作：notify / exec.python / exec.command。
限流 10 次/分钟/规则；**cooldown_seconds**（规则级动作防抖）；动作后台线程；审计 action=automation；
统计 `GET /automation/stats`。

内置真实动作规则（2026-08-26 二轮）：
- `search-low-confidence-pagetree-refresh`（默认开，cooldown 3600s）：低置信检索 → 重建页树（只读安全）
- `write-high-importance-consolidate`（默认关）：高 importance 写入 → memory_ops（写路径锁风险，需自行评估）
- `goal-completed-summary`（默认关）：goal 完成 → auto_session_summary
- 防循环：`exec.command` 子进程注入 `TRINITY_AUTOMATION_ACTION=1`，其写入不再触发自动化事件；
  command 中 `{python}` 解析为当前解释器。

MCP `memory_search` 与 API `GET /memories` 已支持 `view` / `visibility_rule` 参数。

## 执行策略层（Codex 借鉴 2026-08-26）

动作可声明 `mode` 与 `approval`（默认 auto/never，行为不变）：
- mode: `read-only`（只读脚本白名单）/ `auto`（已知维护脚本白名单）/ `full`（任意命令，显式配置）
- approval: `never`（直接执行）/ `on-failure`（失败入审批队列）/ `always`（先入队等审批）
- 命令白名单外直接拒绝；审批队列 `~/.trinity/automation/pending.json`，API：
  `GET /automation/pending`、`POST /automation/approve`（{pending_id, approve}）

## Rollout 轨迹（Codex 借鉴）

每次 exec.command 执行记录 `~/.trinity/automation/rollouts/<date>.jsonl`
（ts/rule/command/ok/exit_code/duration_ms/error_tail）；查看：
`python scripts/rollout_inspect.py --summary`（--date/--rule/--failed/--tail/--json）。

## checkpoint 与模型路由（Codex 借鉴）

- `run_pagetree_summaries.py` 默认 checkpoint（~/.trinity/automation/checkpoints/），
  中断重跑跳过已完成，`--retry-failed` 重试失败项；
- `TRINITY_LLM_ROUTING` 任务分级模型路由（JSON 或 task=model 列表）：
  summarize（摘要）/ retrieval_judge（reason 判题）按任务类型选模型。

## 记忆视图（trinity/views.py）

`~/.trinity/views.yaml`：

```yaml
views:
  wms-decision:
    categories: [decision, wms_knowledge]
    tags: [wms]
    min_importance: 0.6
    sort: importance
    top_k: 10
```

`mem.search(q, view="wms-decision")` — 显式参数优先；视图不存在忽略。

## 行级可见性（trinity/security/visibility.py）

`mem.search(q, visibility_rule="category != 'lme' AND importance >= 0.5")`
白名单字段 + 参数化（防注入）；解析失败忽略。算子：= != > >= < <= IN NOT_IN CONTAINS。

## 行级可见性 × RBAC（按角色下发）

`TRINITY_VISIBILITY_<ROLE>` env（如 `TRINITY_VISIBILITY_VIEWER="importance >= 0.4 AND category != 'lme'"`）：
`GET /memories` 未显式传 visibility_rule 时自动应用请求角色（X-Agent-Role）的规则；多角色 AND 拼接。

## OpenAPI

- `GET /api/openapi.json` — 增强版 OpenAPI 3.0（中文 + view/visibility/automation 参数说明）
- FastAPI 原生 `GET /openapi.json`（147 paths 自动生成）保留

## 目标引擎（DSH 借鉴 2026-08-26）

- `goal_create/update/get/list`（~/.trinity/goals.json）；acceptance{metric,op,value} 验收；
  连续 3 轮无进展自动 blocked；evolution 周期完成自动评估（default_metrics 读 output/*.json）
- REST：GET/POST /goals、POST /goals/{id}/update；状态变化发 goal.updated 事件

## 断言评测（DSH 借鉴）

`scripts/run_evals.py --all`（7 断言任务：pagetree/search-schema/reason/automation/views/visibility/goals）；
维护链 `-Tasks eval`；evolution CERTIFY 自动带 eval_assertions。

## 技能运行时（DSH 借鉴）

`trinity/data/skills/*.md`（frontmatter：name/description/when_to_use）→ MCP skill_list/skill_load、
REST /skills；`match_skills(q)` 按查询匹配技能。

## 回滚

见 `dsh-ops/EXECUTION.md` 第 22.5/25.4 节；hooks 默认关闭，无行为影响。

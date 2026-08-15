# DSH × Trinity 联合架构完整能力盘点（2026-08-14）

> 本文档将 **DSH（DeepSeek Harness，编排/执行层）** 与 **Trinity（记忆操作系统，记忆/认知层）**
> 视为一个联合系统，从「DSH 侧能力」「Trinity 侧能力」「联合集成点」「联合能力矩阵」四个
> 视角做完整盘点。所有标注 **实测✅** 的内容均为本机真实运行验证（2026-08-14 及当日会话内）。
>
> 配套文档：`docs/CAPABILITY_MAP_20260814.md`（Trinity 能力全景与 V3 规划）、
> `docs/FUNCTION_SUMMARY_20260814.md`（Trinity 源码功能汇总）、
> `dsh-ops/EXECUTION.md`（DSH→Trinity 24 轮优化执行记录）。

---

## 一、总览：双层架构与联合面

```
┌─────────────────────────────────────────────────────────────┐
│  DSH —— 编排/执行层（DeepSeek Harness 0.1.0-rc.6）            │
│  web GUI (:3080) / headless CLI / 插件栈 ~200 包              │
│  会话 · 工具 · 子代理 · workflow · goal · skill · schedule    │
│  沙箱权限 · 凭证 · 遥测 · 后台任务                            │
└───────────────┬─────────────────────────────────────────────┘
                │ 联合面（集成点）
                │ ① MCP 桥：8 个 mcp__trinity__* 原生工具（stdio）
                │ ② dsh-ops：maintenance / supervisor / autostart / schedules
                │ ③ 凭证：.dsh/.credentials.yaml ↔ Get-DshCredential
                │ ④ skill：trinity-maintenance（运维知识固化）
                │ ⑤ goal：evolution-as-goal（进化周期迁到 DSH goal）
                │ ⑥ workflow：trinity-benchmark.workflow.js（基准编排）
                │ ⑦ schedule：会话内定期维护提醒
                │ ⑧ 遥测：OTEL span → Jaeger（trinity 侧；DSH 侧 session-telemetry-otel）
                │ ⑨ 数据流：Hermes↔Trinity 双向同步；聚合池 ↔ 引擎库
┌───────────────┴─────────────────────────────────────────────┐
│  Trinity —— 记忆/认知层（v8.2.0，517 文件 / 195,940 行）       │
│  存储 · 47 通道检索 · 生命周期 · 多智能体 · 身份 · 治理        │
│  进化 · 经济层 · 协议（REST 129+/GraphQL/MCP）· 基建           │
└─────────────────────────────────────────────────────────────┘
```

**分工**：DSH 负责「让 agent 做事」（长会话、多轮工具调用、子代理扇出、目标跟踪、
定时、权限、凭证、结果物），Trinity 负责「让 agent 记住」（写入即版本化审计、
跨会话检索、衰减/压缩/分层、多 agent 共享与隔离、身份漂移检测、自我进化）。
联合后：DSH 会话内的每一次记忆写入/检索/审计都落在 Trinity 的可信链上，
而 Trinity 的运维（健康、进化、衰减、分层、同步、基准）由 DSH 的编排能力自动驱动。

---

## 二、DSH 侧能力盘点（编排/执行层）

### 2.1 运行形态

| 形态 | 说明 | 状态 |
|---|---|---|
| Web GUI | `dsh --profile web`（:3080），全功能会话界面：goal 面板、trajectory 回放、jobs 面板、skill 目录、子代理、workflow 运行、插件管理 | ✅ 本会话运行中 |
| Headless CLI | `dsh --profile headless "任务"`，一次性任务执行，供脚本/计划任务驱动 | ✅ 实测 exit 0 |
| 插件系统 | cordis 组合树：bundle 层 → `cordis.patch.yml` 用户补丁层 → `--patch` 覆盖层；HMR 热应用 | ✅ 已用 mcp-trinity / schedule 两个补丁 |

### 2.2 会话与上下文能力

| 能力 | 插件/机制 | 说明 |
|---|---|---|
| 长会话 | dsh-session-persistence / projection / compaction | 会话持久化（jsonl）、上下文投影缓存、压缩（compaction + tool-result-pruner） |
| 溢出 | dsh-spill / spill-policy | 超长输出落盘引用，保持上下文可控 |
| 标题/统计 | dsh-session-title-llm / session-stats | LLM 生成会话标题；会话统计 |
| 遥测 | dsh-session-telemetry-otel | 会话事件 → OpenTelemetry |
| 检查点 | dsh-session-checkpoint-policy / session-log-export | 会话检查点与导出 |

### 2.3 Agent 工具面（当前会话实际可用）

| 域 | 工具 | 说明 |
|---|---|---|
| 文件 | read / write / edit / glob / grep | 读改写查四件套；edit 为精确字符串替换（utf-8） |
| 执行 | pwsh | PowerShell 执行（原生 Windows 路径），后台 job 模式 |
| 子代理 | subagent / subagent_fork / send_message / interrupt_agent / list_agents | 后台委派、继承上下文的 fork、消息续接、中断、清单 |
| 编排 | workflow | JavaScript 脚本编排多子代理（parallel/pipeline/phase），结构化 schema 输出 |
| 目标 | create_goal / get_goal / update_goal | 同会话长期目标：多轮自动续跑、暂停/恢复/完成/阻塞 |
| 技能 | skill | 加载会话技能目录中的可复用操作手册 |
| 定时 | schedule_create / schedule_list / schedule_delete | 会话内定时提醒（≥300s 固定间隔） |
| 任务清单 | todo_write | 多步任务进度跟踪（GUI 可见） |
| 后台任务 | job_list / job_output / job_kill | 长命令后台化与结果收集 |
| 提问 | ask_user_question | 结构化向用户提问（含推荐项/多选） |
| 网络 | web_search | 实时信息检索（带来源） |
| 记忆 | mcp__trinity__*（8 个，见 4.1） | Trinity MCP 桥：搜索/写入/更新/删除/审计/诊断/编年史/标签搜索 |
| 其他 | read_image / exit_plan_mode / ralph | 图像读取、计划模式退出、fresh-agent 迭代循环（ralph） |

### 2.4 执行与安全

| 能力 | 说明 |
|---|---|
| 沙箱分级 | danger-full-access / workspace-write / read-only 三级权限预设（`settings.yaml` 默认 danger-full-access） |
| 审批 | dsh-user-approval：升级权限需用户批准（本会话审批提示已禁用） |
| 凭证 | dsh-credentials / credentials-local：`~/.dsh/.credentials.yaml`（BOM+CRLF），`Get-DshCredential` 读取 |
| 后台任务 | dsh-jobs-local：长命令 job 化，超时上限 |
| MCP 客户端 | dsh-mcp-client：stdio/SSE 拉起外部 MCP 服务器为原生工具 |

### 2.5 插件栈（~200 包，按域归类）

- **UI/前端**（~45）：dsh-web-app / dsh-client-web / dsh-client-ui-*（layout/sidebar/goal/plan/jobs/subagent/skill/tool/trajectory/workflow-run/settings/theme 等）
- **agent 运行时**：dsh-agent / dsh-agent-loop / agent-presets / agent-tool-presentation / agent-instructions
- **工具实现**：dsh-tools / dsh-tool-fs / fs-search / bash / bash-persistent / pwsh / subagent / subagent-control / subagent-report / workflow / ralph / todo / goal / jobs / skill / web / ask-user / str-replace-editor
- **执行环境**：dsh-bash-local / bash-sandbox / pwsh-local / pwsh-sandbox / shell / terminal / subprocess / code-runtime / native-command / sandbox / fs-sandbox / sandbox-windows-acl
- **会话**：session 全家桶（persistence / projection / query / stats / telemetry / title / compaction / spill / checkpoint / reference）
- **协调**：goal / goal-round-driver / workflow / workflow-worker-thread / subagent-* / schedule / plan-mode
- **平台**：credentials / jobs / storage / fs / workspace / home-paths / llm（deepseek / pi-ai / retry）/ web-search-deepseek / api-gateway / host-* / attachment / persona / brand / anonymous-user-id

---

## 三、Trinity 侧能力盘点（记忆/认知层）

> 详细实测见 `CAPABILITY_MAP_20260814.md`。以下为联合盘点所需的核心面。

### 3.1 内核与数据现状（2026-08-14 实测）

- 规模：517 Python 文件 / 195,940 行 / 138+ REST 路由 / 17 个 CB 模块 / 129+ 端点
- 版本：源码与 API 均 **v8.2.0**（`trinity_diagnostics` 实测）
- 数据：引擎库 11,425 条（active 1,473）/ 聚合池 10,632 / 图谱 11,058 实体 · 28,142 关系 / 审计 5,108 条 / 身份锚点 10

### 3.2 能力面总表

| 层 | 能力 | 实测状态 |
|---|---|---|
| 存储 | SQLite(FTS5) / PostgreSQL(pg_trgm+GIN) / ChromaDB / Vectile；CRDT 版本化 + SHA-256 审计 | ✅ 双存储运转 |
| 检索 | 47 通道（BM25+jieba、FAISS HNSW、Exabase、BEAM-LIGHT、Hindsight、Hopfield、因果图谱 GoS、RRF 融合）；SPLADE/ColBERT 重排 | ✅ 6 通道 tier=full；SQuAD R@5=98.3%；LoCoMo 0.88 |
| 生命周期 | 衰减 / 压缩（mock 或真实 LLM）/ 分层 / 冲突仲裁 / 间隔重复 / 版本链 | ✅ decay 100→7 摘要；压缩 -21% token |
| 多智能体 | A2A v0.3（AgentCard/RSA/ACL/Marvis）+ 共享聚合池 | ✅ 19 端点；跨进程 demo 6/6 |
| 身份 | 5 类锚点 / 四维漂移检测 / 重建 / 包导入导出 | ✅ 端点全通 |
| 治理 | 50 层守护链 / RBAC 6 角色 / DCSA 双循环审计 / GDPR 删除权 | ✅ 审计 integrity_ok |
| 进化 | MetaEvolution 五阶段 / 热力图 / 热点 / 课程生成 | ✅ 多轮 cycle 完成 |
| 经济层 | TrustExchange 市场（挂单/订单簿/估价/信誉/背书） | ✅ 全流程跑通 |
| 协议 | MCP 8 工具（stdio+SSE）/ REST / GraphQL(Strawberry) / OpenTelemetry | ✅ MCP 检索写入实测正常 |
| 基建 | Docker 4 容器 / Raft 共识（5 节点）/ 神经形态对齐 / 自愈 supervisor | ✅ gateway 镜像 + 容器冒烟通过 |

### 3.3 记忆闭环（联合盘点的核心链路）

```
写（memory_write，CRDT+SHA-256 审计）
 → 检索（47 通道 + 聚合池 + 图谱）
 → 更新（memory_update，版本+1 保留旧版本）
 → 反馈（evolution/feedback）
 → 软删（memory_delete，FTS 同步清理）
 → 审计（audit_query 版本链 / /audit/integrity 完整性）
```

**实测**：9/9 核心链路闭环（写→搜→版本→审计→删→重写；图谱；身份；市场；
A2A；压缩；进化；GraphQL；Collector）。

---

## 四、联合集成点（DSH × Trinity 协同能力）

### 4.1 MCP 桥：8 个 mcp__trinity__* 原生工具（联合核心）

经 `web/cordis.patch.yml` 的 `mcp-trinity` 插件实例（`dsh-mcp-client`，stdio →
`trinity-mcp --mode stdio`），每个 DSH 会话原生获得：

| 工具 | 用途 | 本会话实测 |
|---|---|---|
| `memory_search` | 四模式检索（semantic/graph/exact/hybrid） | ✅ 返回带 score 记忆 |
| `memory_write` | CRDT 版本化写入 + SHA-256 审计（自动语义关联） | ✅ 写入成功（含批量嵌入提速） |
| `memory_update` | 冲突保留式更新（版本+1） | ✅ v1→v2 实测 |
| `memory_delete` | 软删 + FTS/聚合池/BM25 同步清理 | ✅ deleted=true |
| `audit_query` | 版本链查询（CREATE/UPDATE…+ SHA-256） | ✅ 完整版本链 |
| `trinity_diagnostics` | 全组件诊断（版本/存储/通道/计数） | ✅ v8.2.0 完整报告 |
| `memory_chronicle` | 事件序列编年史写入 | ✅ |
| `memory_tag_search` | 按标签检索 | ✅ |

> 运维要点（详见 skill）：MCP 进程由 dsh-mcp-client 自动拉起，PID 变化勿依赖；
> 修复 ImportError 曾需清理系统 Python site-packages 旧拷贝（勿 `pip install .` 非 editable）。

### 4.2 dsh-ops 运维套件（DSH 驱动 Trinity 的自动化）

| 脚本 | 能力 | 状态 |
|---|---|---|
| `trinity-dsh-maintenance.ps1` | health / evolution / decay / tiers / sync / selftest；`-Direct` 或 `-ViaDsh`（headless agent）；`-DecayLLM mock|real` | ✅ 全任务实测 |
| `trinity-supervisor.ps1` | api(:8001) / mcp(:8000) / collector 探测与拉起；60s 重启间隔保护；MCP 存活判据含进程命令行核验 | ✅ 实测拉起 |
| `trinity-autostart.ps1` + VBS | 免提权常驻循环：每 5min 监督 + 每 4h health+evolution + 每日 03:00 decay,tiers,sync | ✅ 已安装 |
| `install-dsh-schedules.bat` | 5 个计划任务（需管理员） | ⚠️ 环境受限，用 autostart 替代 |
| `run-benchmarks.ps1` | 基准套件并行运行器 | ✅ |
| `align-pg-schema.sql` + `apply-pg-alignment.py` | PG schema 对齐（带备份幂等） | ✅ |
| `dsh-credentials.ps1` | 共享凭证读取（env → 凭证 → 默认） | ✅ |

### 4.3 凭证体系

- `~/.dsh/.credentials.yaml`（UTF-8 BOM+CRLF）：`TRINITY_PG_*`（127.0.0.1:5430/trinity/trinity/trinity）、
  `DEEPSEEK_API_KEY`、`TRINITY_API_KEY`（可选）等
- 消费方：maintenance / supervisor 启动 api/mcp 前注入；decay 真实 LLM 压缩读取 DeepSeek key
- 安全：`trinity.yaml` 已 `git rm --cached` 并加入 .gitignore

### 4.4 skill：trinity-maintenance

- `~/.dsh/skills/trinity-maintenance/SKILL.md`：服务拓扑、dsh-ops 脚本、凭证规范、
  11 条已知坑（PG 127.0.0.1、.ps1 必须 BOM+CRLF、faiss 噪音、聚合池/大库双套等）、
  常用命令、测试基准口径 → 新会话 agent 自动加载即得完整运维知识 ✅

### 4.5 goal：evolution-as-goal

- Trinity 自进化五相位（observe→analyze→plan→execute→certify）每轮 1 相位；
  原生调度器不真正调度、状态仅内存 → **迁到 DSH goal**：每相位一轮、持久化检查点、
  可暂停/恢复、GUI goal 面板可见 ✅（指南：`dsh-ops/evolution-as-goal.md`）

### 4.6 workflow：trinity-benchmark.workflow.js

- DSH workflow 编排示例：`parallel()` 扇出基准子代理 + `schema` 强制结构化输出 +
  汇总 agent 通读仓库产物产出完整性核查报告（曾发现 SQuAD 双口径、Raft 多 leader 等真问题）✅

### 4.7 schedule：会话内提醒

- `web/cordis.patch.yml` 的 `schedule` 插件实例：`schedule_create(every_seconds≥300)` 让 agent
  在会话内安排定期维护提醒 ✅

### 4.8 遥测

- Trinity 侧：API 请求 span + 8 个 MCP 工具全量埋点（`_traced_tool`）；导出到
  `OTEL_EXPORTER_OTLP_ENDPOINT`（默认 4318）；`docker/telemetry` Jaeger all-in-one 已验证
  （/api/services 出现 trinity，span 可见）✅
- DSH 侧：dsh-session-telemetry-otel（会话事件遥测）可用

### 4.9 数据流

```
Hermes（桌面应用/知识库）
   ↕ 双向同步（sha256 去重；sync_hermes_watch.py 轮询）
Trinity 引擎库（SQLite :~/.trinity/store/trinity_store.db，记录源）
   ↕ 镜像（sqlite_pg_mirror.py，幂等）
PG（Docker trinity-db :5430，批处理/decay/tiers 层）
Trinity 聚合池（~/.trinity/data/aggregator_pool.json，API/MCP 检索入口）
```

- Hermes→Trinity 1,449 条、Hermes→Desktop DB 2,389 条、Marvis 3 会话（sync 实测 0 错误，幂等）
- 注意双套设计：聚合池（API/MCP 检索它）+ 引擎库（/memories、hybrid 检索它），交集 0 属设计使然

---

## 五、联合能力矩阵（场景 × 成熟度）

| 联合场景 | 用到的 DSH 能力 | 用到的 Trinity 能力 | 成熟度 |
|---|---|---|---|
| 记忆即服务（外部应用接入） | web/GUI、mcp-client、凭证 | gateway（OpenAI 兼容）+ 47 通道 + 引擎/池 | ★★★ 已可演示 |
| 会话记忆闭环（写→搜→审→删） | mcp__trinity__* 8 工具 | CRDT 版本化 + 审计链 + 软删清理 | ★★★ 实测全通 |
| 运维自动化 | schedule/goal/plan 任务/autostart/supervisor | 健康/进化/衰减/分层/同步 | ★★★ 常驻运行 |
| 基准与评测 | workflow 编排 + 结构化输出 | MemBench（SQuAD 98.3%/memsyco 0.88/压缩 -21%） | ★★★ 基线已出 |
| 长时研究任务 | goal（多轮续跑、检查点） | 进化周期（五相位/轮） | ★★☆ 已落地示例 |
| 多 agent 协作 | subagent 扇出 + send_message | A2A 共享池 + 隔离过滤 + 身份漂移 | ★★☆ 演示通过 |
| RAG 记忆层 | 会话工具面 | 47 通道 + 生命周期（衰减/压缩/冲突） | ★★★ 数据已验证 |
| 垂直知识库 | skill 固化 + 检索 | 间隔重复 + 垂直采集（网页/视频） | ★★★ |
| 商业化（SaaS/知识市场） | web GUI + 凭证 | gateway + market 协议 + leaderboard | ★★☆ 待定价/上线 |
| 边缘/前沿 | — | WASM client、神经形态对齐、跨模态 | ★☆☆ 环境受限 |

---

## 六、当前运行状态快照（2026-08-14 会话内实测）

| 项 | 值 |
|---|---|
| DSH | 0.1.0-rc.6；web :3080；headless CLI 可用；插件 ~200 包 |
| Trinity API | :8001 /health 200；tier=full；6 通道 active（keyword/vector/second_brain/retrieval_v47/exabase/beamlight） |
| Trinity MCP | stdio（dsh-mcp-client 拉起）；SSE :8000（supervisor 拉起） |
| 诊断 | v8.2.0；SQLite 74.6MB；memories 11,425（active 1,473）；audit 5,108；图谱 11,058/28,142；锚点 10 |
| 测试基线 | pytest 580 passed / 43 skipped / 0 failed（18.4 轮后）；API 回归 36/36；闭环 9/9 |
| 监控 | autostart 循环 + supervisor 每 5min；Jaeger 遥测容器 Up |

---

## 七、已知边界与下一步

### 7.1 联合侧已知边界

1. **MCP memory_write 冷启动**：自动语义关联批量嵌入已从 94.5s 降到 15.4s（<30s 超时），
   但写入加工管线仍同步——建议异步化（写入即时返回，加工后台完成）。
2. **原生与 Docker 双 MCP 并存**：原生 :8000（supervisor 管理）与 Docker trinity-mcp（:8006）
   并行；端口冲突历史已通过改映射解决，需保持 supervisor 存活判据（含命令行核验）。
3. **内存压力**：vmmem(WSL) + node 宿主曾占满 32GB → OOM/全量 pytest 不稳定；
   建议 `wsl --shutdown` 释放 + 排查 node 内存泄漏。
4. **授权审批**：本会话审批提示禁用，`sandbox_permissions` 升级不可用（需另开会话）。

### 7.2 下一步（承接 CAPABILITY_MAP V3 顺序）

- **第 1 步（1 周）**：文档一致性轮（ROADMAP/README/CHANGELOG 已部分完成）+ 安全复查
- **第 2 步（1-2 周）**：gateway 端到端外部接入 demo（已跑通）+ TS SDK 补齐（已核验/补客户端）
- **第 3 步（2-4 周）**：MemBench 报告发布 + leaderboard 平台化（已就绪）
- **并行**：记忆市场内容化（10k+ 条脱敏成知识包）或 A2A 联邦演练（15 agent 实跑协作流水线）
- **联合侧增量**：MCP 写入异步化；memory_write 冷启动再优化；DSH 会话遥测接入 Jaeger 全链路

---

## 八、结论

- **DSH 是编排层**：长会话 + 全工具面 + 子代理/workflow/goal/schedule + 权限/凭证/遥测，
  让「自动化驱动 Trinity」成为现实（supervisor/autostart/计划任务/基准编排/进化 goal 全部落地）。
- **Trinity 是记忆层**：存储/检索/生命周期/多智能体/身份/治理/进化/经济全闭环，
  129+ 端点大部分实测激活，是超出文档描述的完整「记忆操作系统」。
- **联合后形成闭环**：DSH 会话内每个记忆动作都落在 Trinity 可信链（版本+SHA-256+审计），
  Trinity 每个运维动作都由 DSH 编排能力自动驱动；两侧能力互补、无重叠，联合成熟度
  最高的场景是「记忆即服务」与「会话记忆闭环」，欠的仍是外部化（社区/评测/产品化）。

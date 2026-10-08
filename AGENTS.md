# AGENTS.md — Trinity Memory

> 本文件由 Trinity 生成（2026-10-08 12:47:11）。它让接入本仓库/工作区的 AI Agent
> 自动了解 Trinity 记忆层的存在、用法与当前状态。
> 相关规范背景：OpenAI AGENTS.md / Anthropic CLAUDE.md 的「文件即记忆」标准。

**本文件导航（新会话先看这四行）**：

- **要写/改判据** ⇒ 先读 `dsh-ops/CRITERION_CHECKLIST.md`（检查单），跑 `python scripts/checklist_run.py`（`--list` 看有哪些项、`--only X` 单跑一条）；
- **要动开关/口径** ⇒ 先读 `dsh-ops/DECISIONS_PENDING.md`（人工决策项：重要性来源 / PG 加密白名单 / 预热口径 / **常驻服务的完整性级别** / **机器级第三方占用** / **窗口阻塞时的周检口径** / **独立监督腿的看护者** / **谁在杀监督腿** / **fok 补算的节奏** / **冷启动的向量索引补建** / **冷索引请求的阻塞面** / **FOK 校准映射的二值阶跃** / **FOK 打分口径（覆盖率不进分数）** / **GraphQL filter 的时间字段口径** / **PG 池中死连接的处置** / **目标注册表的陈旧提议** / **归属过滤的显式作用域豁免** / **来源未定的半归档行** / **检索决策的 outcome 口径** / **缺口清单余下五项的测试集选料** / **官方分片取不到时的替代口径** / **历史双层密文行是否就地迁移** / **②TTL 与官方 CR 的答案面做不做** / **CR 的问句→关系链要不要用 LLM 解析** / **门控概率源饱和了怎么办** / **记忆注入防御换不换机制** / **学习型显著性信号要不要投入** / **常驻 API 的存储后端口径**）；
- **要知道这一天新增了什么工具** ⇒ `dsh-ops/TOOL_INDEX.md`（本会话新增的工具 + 「现象 → 跑哪条」对照表）；
- **闸门红了** ⇒ 本文档 §10 的「判据失败特征」表 + §13 的四条纪律（窗口/密文/形状状态/写侧哑线）。

## Trinity 记忆层实时快照（生成于 2026-10-08 12:47:11）

| 指标 | 值 |
|---|---|
| 会话数 | 633 |
| 结构事件数 | 207177 |
| 目标数 | 221 |
| Todos | 825 |
| 计划 | 1 |

### 活跃目标（active goals）

| 状态 | 阶段 | 轮次 | 目标 |
|---|---|---|---|
| blocked | blocked | 12 | Trinity 优化（D:\trinity-code，不 commit/不 push/不发版）：A 自证面清退【完成，已线上生效】；B3 规划通道口径【完成】；B1 候选池放大【已被本仓实测证伪，撤回】；B2 原表述【已证伪，改查出真根因】；§1381 pg_forced_light 恒常量【已修，待重启生效】；§13 |
| active | active | 0 | 不发布（不 commit/不 push/不发版）前提下执行上轮收尾给出的 4 条建议：①postinstall 验证 pytest-timeout（pytest.ini 已配 timeout 但插件缺失）让卡死用例能超时；②cognitive_eval PASS=false 的根因判定（口径 vs 真回归）并修复，恢复 |
| active | active | 0 | 从网络系统性地采集 AI / 大模型 / 智能体 相关知识（论文元数据与全文、开源仓库、官方文档、课程、Awesome 清单、网络检索结论），下载到本地 D:\ai-kb 并汇总为可检索、带索引与综述的知识库。 |
| active | active | 0 | 按 BRAINIFICATION 重构方案全方位执行 Trinity 优化（D:\trinity-code，不发布/不 commit/不 push/不重启服务）：L1 静默失败清零（except:pass 计数化/未定义 logger/参数生效化/自证指标改量产物）、L2 回路接线（注册表消费者约束、injection |
| active | active | 0 | 不发布原则下全量执行 Trinity 短板优化（本地演进，不 push/不发版/不上市）： P1①scratchpad 高信号层（偏好 cue/指令 flag/规范化日期/高级断言，超阈 LLM 压缩）；P1②写时前瞻索引（prospective/implication 线索边，专治 MS 检索 R@5=0.525 换 |

### 最近会话（recent sessions）

- `303de6ac-782f-4bc6-aa74-3a489de0611a` [active] (untitled)
- `fae51f09-a7c0-4687-9d50-a7f970c275e2` [active] (untitled)
- `651757d7-f59c-46a9-8d92-dbe15515bf7a` [active] (untitled)
- `session-5364dd29-d7ca-4e00-a41f-7572f95fb347` [active] (untitled)
- `c15b55ce-f456-41d7-96f3-fabc942691a9` [active] (untitled)

## 1. Trinity 是什么

Trinity 是长程记忆系统（Memory OS）：跨会话保存并检索事实、偏好、决策与
会话轨迹。它不是普通 RAG 知识库——记忆带 CRDT 版本链、SHA-256 审计、
时间感知与多租户隔离（persona/session/agent/tenant）。

## 2. 如何检索记忆

Agent 在回答「是否记得… / 之前做过… / 用户偏好…」类问题时，应当先检索
Trinity，而不是仅凭当前上下文猜测。

- ⚠️ §971 实测更正：旧文档写的 GET /memory/search?q=... **是 404**（该路径不存在）；REST 请用下面那条。
- **MCP（推荐）**：本机 MCP server 暴露 `memory_search` / `memory_write` /
  `memory_update` / `memory_delete` / `audit_query` / `memory_tag_search`
  （stdio 模式无鉴权；streamable-http :8003 用 Bearer token）。
- **REST**：**POST** `http://127.0.0.1:8001/memory/search/hybrid`，体 `{"query": "...", "top_k": 5}`
- **CLI**：`python -m trinity search --query "..." --top-k 5`。

检索建议：
- 默认用混合模式（hybrid），短查询走 FTS 轻通道（毫秒级）。
- 检索不到时放宽关键词（Trinity 用 jieba 中文分词 + BM25 + 向量 + 图谱
  多通道融合；同义改写后再试一次）。
- 关键事实请用 `audit_query` 核对版本链与来源。

## 3. 如何写入记忆

- 值得记住的才写：用户偏好、事实、决策、完成的工作、踩过的坑。
- 内容自包含：让未来的 agent 不看本对话也能读懂（含路径、工具名、数字）。
- 建议结构（与 Trinity 记忆契约一致）：

```
[类型] 日期 一句话标题
- 目标/任务: ...
- 关键决策与理由: ...
- 结果/产出: ...
- 坑与经验: ...
- 下一步: ...
```

- 标签保持一致（项目名/领域/类型），importance 0.4-0.6 常规、0.7+ 决策/事故。
- 用 `memory_update` 更新已有记忆而不是重复写入新条目。

## 4. 会话身份与隔离

- 每个 DSH 会话自动注册为独立 agent 身份（agent_id=dsh-<sessionId>）。
- 未显式指定时检索默认按当前会话隔离；空结果自动回退全局检索。
- 多租户：persona_id / session_id / agent_id / tenant_id 四级过滤。

## 5. 常用命令

```bash
# 搜索记忆（混合检索，top-5）
python -m trinity search --query "用户偏好" --top-k 5

# 引擎诊断（版本/存储/通道/规模）
python -m trinity diagnostics

# 服务健康
curl -s http://127.0.0.1:8001/health

# 维护（decay/tiers/sync）
powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks all
```

## 6. 注意事项（known pitfalls）

- SQLite 大库多进程共享有写锁风险：批量写入用维护链（每日 03:00 自动），
  不要并发大量 ingest。
- 引擎默认检索路径是 FTS5（R@5 0.975 > hybrid-rrf 0.942）；显式
  `search_hybrid` 才走 5 通道融合。
- 语义缓存默认 memory 后端（TTL 300s）：刚写入的记忆可能短暂命中旧缓存，
  敏感操作可用 `TRINITY_CACHE_BACKEND=off` 临时关闭。
- PG 连接必须用 127.0.0.1（localhost 解析 IPv6 会被 pg_hba 拒绝）。

## 7. 安全与可证明性（⚠️ 加密一项**仅 SQLite 镜像启用**——原写「R8-R9 起出厂默认」，2026-09-29 勘误：对生产 PostgreSQL 不成立）

- **存储加密：仅 SQLite 镜像启用**（AES-256-GCM；原写「存储加密默认开启」，2026-09-29 勘误已撤回）：
  content 列密文落盘，FTS 不受影响——代价是 `tokenized_content` **明文影子列**（`content` 为 `enc:v1:`
  的 active 行 21,969 条中 21,370 条 = **97.3%** 为明文 jieba 分词；`sqlite/_crypto.py::_tokenized_for_storage`
  注释写明的**有意**行为）⇒ **实际机密性失效**，不得据此宣称「敏感正文不以明文落盘」
  （**理由**：同一段正文以明文分词形态落在 `tokenized_content` 列 —— 加密的是 `content`，
  而可读副本在隔壁列，所以「列是密文」不等于「内容没明文落盘」）
  （登记：`docs/SECURITY_BOUNDARIES.md` SB-1/SB-2）；
- **⚠️ PG 口径更正（2026-09-21 §1138 实测）**：上面的「默认开启」是**SQLite 路径**的描述；
  **PG 写端加密是按类目白名单**（`TRINITY_PG_ENCRYPT_CATEGORIES`，**默认空 = 不加密**），
  supervisor 当前只设了 `perception` ⇒ **其余类目（session / procedural / milestone / observation / 自省…）在 PG 里是明文**。
  实测：7d 明文 20,529 行里，生产面 1,220 行正是这些类目。**别把这条读成「PG 全加密」**；
  另有 `memory_perceive` 走**裸 psycopg2 INSERT**（绕过守卫）⇒ perception 亦有历史明文债（`direct_pg_writers_audit` 门专盯这类）。
  `TRINITY_STORAGE_ENCRYPTION=off` 显式关闭。
- **记忆投毒写入过滤**（OWASP AG 类）：写路径扫描注入模式，高危命中自动
  归档 + `INJECTION_ISOLATED` 审计；`TRINITY_INJECTION_SCAN=off` 关闭。
- **可证明记忆回执**：`GET http://127.0.0.1:8001/audit/receipt/{memory_id}`
  返回当前哈希/版本链/审计链完整性（验证者可独立重算 SHA-256 对账）；
  `GET /audit/integrity` 全链校验。
- **健康真实上报**：`/health` 含 engine 组件——引擎故障报 degraded + 错误
  详情（不再有「健康假象」）；写锁竞争时引擎只读降级（检索可用、写报错）。

## 8. 图谱与时序能力（R7-R8 增强）

- **edge bi-temporal**：`GET /graph/relations/at?at_time=...` 时点查询；
  创建关系可带 valid_from/valid_to（对齐 Zep/Graphiti）。
- **PPR 图谱通道**（HippoRAG 式）：混合检索的图谱通道含 PPR 多跳扩散
  （`TRINITY_GRAPH_PPR` 默认 on）。

## 9. 可观测指标（/metrics）

- 记忆命中率/写放大：`trinity_write_amplification` /
  `trinity_queries_by_source_total` / `trinity_semantic_cache_hit_rate_pct` /
  `trinity_last_query_ts`——Prometheus 可直接抓取。

## 10. 元记忆物化表（fok_counts）运维清单（2026-09-20 §918-§952 建）

**它是什么**：检索时的「元记忆」（feeling-of-knowing）判据依赖**子串计数**，而在线算一次要 ~8s
（44k 行 × 最多 15 个 ILIKE 谓词）⇒ 生产的 250ms 上限里从来算不完，弃答判据**静默失效**。
所以把「同一口径」的计数离线算好存表，查询侧只查表（§932 的决策级 A/B 判据：误弃 0/30、
负例弃答 29/30、平均 7.9ms；对照：词元快路误弃 11/30、抽样 2/30）。

**表（9 张）**

| 表 | 作用 |
|---|---|
| `fok_counts` | 成品计数：key(t:词 / p:词a\|词b) / cnt / updated_at(TTL 7d) / source / hit_count / last_hit_at |
| `fok_counts_pending` | 待算队列（领取即删、失败放回）：key / requested_at / source(miss\|prewarm\|prewarm_corpus\|test) |
| `fok_counts_history` | 每轮补算历史（趋势/周对比）：filled / pending_after / prewarm_added / filled_prewarm / filled_miss |
| `fok_api_samples` | 每轮抓 /metrics 的查表计数（区间命中率） |
| `fok_hits_hourly` | 命中逐小时 × 来源分类（自然 vs 合成对账） |
| `retrieval_terms` | 真实检索词登记（`--prewarm` 的输入） |
| `fok_probe_windows` | 合成探测窗口（自然流量口径） |
| `fok_corpus_runs` | 语料预热增量游标 |
| `loop_health_history` / `loop_health_daily` | 环状态逐轮历史与日聚合（跑数从几千行降到一次索引） |

**脚本**

| 脚本 | 用途 |
|---|---|
| `scripts/fok_counts_fill.py` | 离线补算：`--workers 4`（实测 3.8×）、`--prewarm`（近 24h 热词）、`--prewarm-corpus 150`（按最近记忆词频）、`--adaptive`（让路在线检索，`--max-pause-s` 上限）、ROI 自节流（判红降到 50） |
| `scripts/fok_abstain_ab.py` | **决策级 A/B**（同一把尺）：跑完自动登记探测窗口 |
| `scripts/fok_mark_test_keys.py` | 合成 key 出列（`--apply`）+ `--auto-burst` 自动突发窗口 + 日聚合 + 复核到期检查 |
| `scripts/loop_health.py` | 环的判据实现（fok 三条：counts / prewarm / hitrate） |
| `scripts/export_memories_markdown.py` | 导出 FOK_COUNTS.md（机制一眼图 + 周/日趋势 + ROI + 命中时间分布 + 环状态）＋**顺带刷新仓库根的 `AGENTS.md`**（§1318；它挂在夜链 `brain-md-export`，因为 `fok-counts-fill` 04:27 会改数 ⇒ 谁改了数就要有人刷文档。**注意副作用**：跑这个脚本会覆盖仓库根 `AGENTS.md`） |
| `scripts/execution_toc.py` | EXECUTION 轮次索引（`--check` 已进闸门集） |
| `scripts/ps1_safe_edit.py` | **保 BOM/行尾**的 .ps1 替换（改 .ps1 一律用它，见 §11） |
| `dsh-ops/_936_ttl_verify.py` | TTL 自愈验证 |
| `scripts/metrics_latency_probe.py` | API `/metrics` 延迟采样并落 PG（§969）：把「偶发超时」变成时间序列（min/p50/p90/max/错误数） |
| `scripts/api_stall_forensics.py` | **API 停摆归因采样**（§970）：停摆时抓 探测序列 + PG 活动（pg_stat_activity）+ API 进程 + 第二条端点耗时，落 PG `api_stall_events` 与 `output/api_stall_*.json` |

**开关与环境变量**

- `TRINITY_FOK_TABLE`（默认 on；off = 回落慢路 + 250ms fail-open）
- `TRINITY_FOK_TTL_DAYS`（默认 7；过期行按 miss 处理并重新登记 ⇒ 自愈）
- `TRINITY_FOK_HIT_COUNT`（默认 on；命中累加 hit_count / 逐小时桶）
- `TRINITY_FOK_TERM_REGISTRY`（默认 on；登记真实检索词）
- `TRINITY_FOK_FAST_COUNTS` / `TRINITY_FOK_SAMPLE_PCT`（**都保持 off**：A/B 判 FAIL）
- `TRINITY_FOK_NO_PAIR_FAILOPEN`（**默认 on**，2026-09-23 §1280 转正）：单实词查询**没有词对可算**时按「无法评估」处理，不再伪造成 `fok=0.0` ⇒ 不再一律弃答（`score_undefined=no_pairs` 让消费者能区分「不确信」与「没评估」）。转正依据：三列 A/B `off` ⇒ 单实词误弃 **30/30**、`on` ⇒ **0/30**，正例/负例两列一字未动。回滚 `=off`

**维护链任务（**每日尾段**：2026-09-22 §1241 实测 **1×/日**；旧文档写的「小时级」是错的）**：`fok-mark-test`（出列 + 突发窗口 + 日聚合 + 复核到期）→ `fok-counts-fill`
（推荐实参 `--max-keys 1200 --batch 40 --prewarm 200 --prewarm-corpus 150 --adaptive 1 --max-pause-s 20`）。
（**链上当前实参（2026-09-23 §1293/§1295 实测）**：`--max-keys **2400** --batch 40 --prewarm 200 --prewarm-corpus **0** --adaptive 1 --max-pause-s 20`
—— 语料预热那一半**已按 ACK 归零**（口径见 `dsh-ops/DECISIONS_PENDING.md` 的 D3，拍板为「只跟随真实检索词」）；
`--max-keys` 由 1200 提到 **2400** 是 §1295 的数据决策：日预算 1200 **排不空** 1300+ 的日入账
（09-22 大扫除后仍剩 1264），而 §1293 实测补算只要 **0.02s/key** ⇒ 提到 2400 约 +24 秒/日。）
（**理由**：这两个任务挂在 `dsh-ops/trinity-autostart.ps1` 的**尾段第 3 段**，触发条件是 `Hour -ge 4` + **当日标记**
（每段每天只跑一次）⇒ 每天一次。旧文档的「小时级」不是笔误而是**判据的前提**：`loop_health` 的 `fok-counts` 环曾按
「有待办却 >3h 没更新」判停 ⇒ 每天 ~07:30 之后**必然变红**（pending 由在线 miss 全天补充）。§1241 已按实际节奏改写，
判据与反事实见 `tests/unit/test_fok_stale_cadence.py`。）

**D9-C 的按需轮（2026-09-23 §1291 拍板并施工）**：同一份脚本的**轻量轮**挂在 `dsh-ops/trinity-autostart.ps1` 的
**每 4 小时**段（任务名 `fok-counts-fill-light`，实参 `--max-keys 200 --prewarm 0 --prewarm-corpus 0 --adaptive 1 --min-pending 500`）：
**队列短于 500 就打一行 `[SKIP] 按需触发…` 直接返回**（开关默认 `--min-pending 0` ⇒ 上面那条每日调用行为一个字节不变）。
**实测成本（§1293）**：`补算完成：80 个 key / 4 批 / 1.3s（0.02s per key）` ⇒ 阈值 500 的作用是**避免与每日大扫除重复做功**，
不是保护 PG 负载；且 `--max-keys` 是**目标**不是上限（收敛粒度 = `--batch`，实测给 20 填了 80）。
**回滚**：从该段的 `$maintTasks` 里删掉 `fok-counts-fill-light`（一行）。判据：`tests/unit/test_fok_fill_min_pending.py`。

**决策表：什么情况下跑哪个脚本**（理由：清单只回答「有什么」，不回答「什么时候用」——
后者才是现场真正要查的；下表把「现象 → 动作 → 判据」三列钉在一起，避免临场翻代码）。

| 你看到的现象 | 该跑什么 | 判据 / 产出 |
|---|---|---|
| 想确认物化表在不在起作用 | `python scripts/loop_health.py` | 三条 fok 环的注记（keys / pending / 区间命中率） |
| `fok-prewarm` 环红 | **先别改预算**：看 `~/.trinity/brain_md/FOK_COUNTS.md` 的 ROI 段 | 「预热覆盖 vs 需求覆盖」；环注记自带成色说明 |
| 队列积压（pending 持续 > 0） | `python scripts/fok_counts_fill.py --prewarm 200 --prewarm-corpus 150` | 日志「补算完成 N 个 key」+ 表内 key 数上升 |
| 改了 fok 的语义/成本（含加开关） | `python scripts/fok_abstain_ab.py --warm` → 填 → `--table-only` | **先写判据再跑**：误弃 0/30 且负例弃答 ≥27/30 |
| 怀疑表里计数过期 | 不用管（TTL 7 天自动按 miss 处理并重新登记）；要立刻验证跑 `python dsh-ops/_936_ttl_verify.py` | PASS = 过期 key 重回 pending |
| 想确认「预热到底有没有用」 | 看 FOK_COUNTS.md 的 ROI 段 + 命中时间分布 | 「窗口外命中(≈自然)」> 0 才算有自然流量证据 |
| 怀疑 API 变慢（环注记出现 metrics 超时） | `python scripts/metrics_latency_probe.py --n 30`，再看 PG `metrics_stalls` | p50/p90 对比 §969 基线（p50≈21ms）；停摆行里 `health_ok=true` = API 忙、false = 服务真挂 |
| 想抓「停摆当时发生了什么」 | `python scripts/api_stall_forensics.py --watch 180` | 落 PG `api_stall_events` + 现场 JSON；先看 `pg_activity` 有没有长查询/`PgSleep`（§970 就是这样抓到一条跑了 4 小时的探针） |
| 刚跑了自己的压测/批量探测 | **立刻** `python scripts/fok_mark_test_keys.py --record-window 60`（`--auto-burst` 兜底） | 探测窗口表出现新行 |
| 出现了合成测试 key | `python scripts/fok_mark_test_keys.py --apply` | 日志「已标注 fok_counts N 行」 |
| 往 EXECUTION 追加了新轮次 | `python scripts/execution_toc.py` | `--check` 通过（否则索引门红） |
| 要改 .ps1 | `python scripts/ps1_safe_edit.py --file X --old A --new B` | BOM=True 且 `Parser::ParseFile` 错误数=0 |
| 想把某个红标成「已复核」 | 编辑 `dsh-ops/loop_health_acks.json`（写 reason/evidence/until） | 环注记出现「已复核·已知」；到期 fok-mark-test 会喊 |
| 全量自检 | `python scripts/gate_set.py` | 尾行 `passed=N` **且** `required_failed=[]`（N **不写死**：冻结集只增不减，写死必然过期——本格曾长期写「14 / 14」，而集合早已到 19；见 §1264） |

**反向清单（这些做法已被证伪，别做）**：

- **别**用 `--accept-baseline` 糊**滚动窗口**指标的下降（理由：§954 实测一分钟内二次变红 ⇒ 棘轮名存实亡）；
- **别**把测量数据写进 `~/.trinity/state/`（理由：那里每个文件都被当器官，见 §12）；
- **别**用通用编辑器改 .ps1（理由：剥 BOM ⇒ 5.1 按 GBK 读 ⇒ 语法整体破坏，见 §11）；
- **别**对整文件做批量引号替换（理由：§958 预览当场抓到会误伤 docstring 定界符与代码示例）。

**判据失败特征：跑出来「长什么样」说明有事**（理由：决策表只写了怎么跑，没写**读什么**——
而本会话的教训恰好都是「输出看着正常、其实已退化」，例如 fok 弃答判据曾在线上静默失效）。

| 对象 | 健康时的样子 | 失败特征 → 该做什么 |
|---|---|---|
| `loop_health.py` / `fok-counts` | `"ok": true` + note 形如 `keys=N pending=N newest_update=…h 近4轮pending=… 节奏=声明：每日尾段 ≥04:00（24h…）`（pending 可为**小**非 0，如 1273 —— 队列由在线 miss 全天补充，属正常） | `"ok": false`；`表为空`；`有待办却 >1.5× 节奏（每日尾段 ⇒ 36h）没更新`（**§1241 前这里写的是 >3h** —— 那条编码了一个不存在的**小时级**节奏，每天 ~07:30 后必然变红）；`欠账在扩大`；`待办堆积`；note 里出现 `⚠️ 表路径最近异常: <原文>` ⇒ **看原文**（§963 实测它出现过一次 `OperationalError: server closed the connection unexpectedly`，为连接瞬断，整体仍 ok）|
| `loop_health.py` / `fok-prewarm` | note 形如 `需求补算 X / 提前算 Y`（**ok 可能为 false**：本环长期是「已复核·已知」的红，别看 ok 一个字段；成色另判 —— 带后缀「（预热在起作用）」是好成色，「远多于提前算的」是坏成色，见右列） | `"ok": false` ⇒ **先看 warns 里有没有「已复核·已知」**（有 ⇒ 只是标注过的红），再核 ROI 成色（是否有压测污染），最后才谈动预算；note 里出现「远多于提前算的」⇒ 预热没接上（先查 `dsh-ops/loop_health_acks.json` 的 ACK，再核 `~/.trinity/brain_md/FOK_COUNTS.md` 的 ROI 段） |
| `loop_health.py` / `fok-hitrate` | `"ok": true` + note=`本区间查表 N 次（样本不足，不判比例）`，或全命中比例正常 | `"ok": false`；`≥20 次查表且 0 次全命中` ⇒ 预热没起效；`计数器重置（API 重启）` ⇒ **正常现象**，不是失败；note=`metrics 采样超时（API 忙时停摆：20s×2 均超时；/health 正常）` ⇒ **API 忙时整体停摆**（§969：/metrics 平时 p50≈21ms／p90≈29ms，慢了就是 API 在跑重活）；note=`API 不可达（/health 也失败）` ⇒ 服务真挂了；两者都记进 PG `metrics_stalls` |
| `fok_counts_fill.py` | 每轮都有的首行 `待办 N 个 key` 与 `已有 N 个 key`；有活时另有 `补算完成：N 个 key`（空闲轮只打印首行与预热/ROI 行） | `[FAIL] 有待办 N 个但本轮 0 个算成`（rc=1）；`批失败…已放回队列` 变多；`[WARN] worker 错误` |
| `fok_abstain_ab.py` | 由 scripts/acceptance_run_963.py 读 output/fok_abstain_ab_table.json 打印：`verdict=PASS` 且 `误弃 0/30`、`负例弃答 29/30`（⚠️ §1279：正例集是「从正文抽 3 个主题词」⇒ **单实词形状测不到**，0/30 不等于「没有误弃」） | `table_verdict: FAIL`；`table.pos.abstain > 0` ⇒ **绝不允许**（误弃）；`table.neg.abstain` 骤降到 0 ⇒ fail-open 又回来了（§932 的静默失效）；`avg_ms` 飙到秒级 ⇒ 缓存失效 |
| `execution_toc.py --check` | `[OK] EXECUTION_TOC.md 是最新的` | `[STALE]` ⇒ 追加完 EXECUTION 忘了重生成索引（闸门会红） |
| `organ_freeze_gate.py` | `7/7 通过` | `[FAIL] freeze:no_unregistered_organ 新增/未登记且仍活跃：X` ⇒ 多半是我又把**测量数据**写进了 `state/`（见 §12） |
| `memory_utilization_audit.py --ratchet` | `利用率未变差`（**必须按闸门口径跑**：--ratchet --scope prod，见 docs/GATE_SET.json 的 memory_utilization 项）；或 `SKIP(窗口) … 样本不足`（U1a 尚无 6 天前的同窗口样本时）。⚠️ 两句是**互斥的健康形态**（分别对应「有可比样本」与「样本不足」），所以本格用**分号**分成两组备选（2026-09-26 §1359 实测：写成「，或」时两组字面量被当成**同时**成立 ⇒ 样本一够就必红）。不带 --scope prod 跑会打「口径不匹配」那一行 ⇒ 那是**跑法不对**（工具自己下一行给了正确跑法），不是利用率退化 | `FAIL：利用率变差`（在 --scope prod 口径下）⇒ 先分清**窗口漂移**还是真退化（见 §13） |
| `plaintext_ratio_audit.py --ratchet` | `明文写入速率:` 一行在限内、占比持平 | `速率守卫超限` ⇒ **先按 category 归因**（§961：procedural/milestone 等合法写入方），再决定调上限还是找新写入方 |
| `service_restart_gated.py` | `生效核验：ACTIVE`（平均返回 > 基线、空结果 0） | `拒绝重启`（有并发测试在跑，**正常**）；超时 ⇒ 走自恢复路径（§930） |
| `fok_mark_test_keys.py` | 非 --apply：`匹配到合成 key（按当前来源）` + `（只报告；加 --apply 落库）`；--apply 后：`已标注 fok_counts N 行` | `ACK-EXPIRED: …`（**rc=2**）⇒ 复核到期：续期 `until` 或撤下该红（§952） |
| `checklist_run.py` | `自动项失败 0 个` + `本次实际检查 N 项`（**现算**，别写死）（`--only E3` 可单跑一条、`--list` 列项名；**项名写错会报错，不静默全跑**）；解释器不是仓内规范那个时会先打一行 `[WARN]`（§1376：它的 pytest 条目可能报**假红**） | 有 FAIL ⇒ 照表里那一项去修；**人工项只给命令，脚本不代判** |
| `doc_regen_guard.py` | `[PASS] mtime` + `[PASS] 实质行比对…一致` | `FAIL` ⇒ **生成器没跑成**（§1126 那次事故的形态）：先 `py_compile`，再 `--run` |
| `criterion_hygiene.py` | `缺『可失败证明』的闸门 0 个` | 非 0 ⇒ 新闸门没带单测/`--selftest`；先跑它自己的 `--selftest` 确认判据能失败。另：`--claim-numbers` 干跑报告 `why` 里**不可追溯的大数字**（**advisory，不改 rc**；§1194 实测误伤率 ≈2/3，故不接闸门集） |
| `live_number_freshness.py` | `活数新鲜 OK（覆盖：GATE_SET / SCORES / EXECUTION_TOC …）` | `源比产物新：<哪个源>（时间）⇒ §15 读数可能已过期` ⇒ 跑 `python scripts/export_agents_md.py --out AGENTS.md` 重生成。**已知的每日必红窗口**（§1318 已修）：`fok-counts-fill` 04:27 动物化表 ⇒ §15 的 fok key 数在 04:27 后过期，而仓库根那一份原先由 03:2x 的 `snapshot` 写 ⇒ 早上必红一次；现在改由 `brain-md-export`（04:28–04:29）顺带刷新，判据见 `tests/unit/test_agents_md_render_sanity.py::test_夜链顺序_刷新在扫描之后` |
| `resource_window_check.py` | `资源窗口判定：WINDOW_OK（commit-free N GB）` | `WINDOW_TIGHT/BLOCKED` ⇒ **先别跑重活**（≥30GB 才 OK；读不到日志按 fail-closed 判 BLOCKED）—— §1184 实测：榨干提交内存时跑重活会把 API 一起赔进去 |
| `wiring_triage.py` | `retrieval_wiring: 棘轮 OK（无新增零引用）⇒ 本次无需归因`（**绿时明确说不用归因**，不硬凑报告） | 报出 `新增零引用` ⇒ 看它给的「未跟踪新文件？/ 同文件字面量重复？」与一行接线建议；**动别人的文件前先快照** |
| `xref_check.py` | `交叉引用问题合计: 0` | 非 0 ⇒ 按 `坏路径 / 坏§号` 逐条修；**注意三个 § 命名空间**（EXECUTION 轮次 / AGENTS 小节 / 本文档小节） |
| `zh_doc_check.py` | `可疑 N 处`（AGENTS.md 现为 **1**：bash 示例里的引号是语法，故意保留） | 一屏可疑 ⇒ 新写的文档又用了 ASCII 引号（见 §14） |
| `gate_set.py` | 尾行 `passed=N`（只断言这个稳定前缀：总条数随冻结集增长，写死数字或断言集合全绿都会自指，见 §964） | 任何 `required_failed=[…]` ⇒ **先跑那一条看原始输出**，别先改代码。**但先看尾行有没有「注：N 条解释器警告」**（2026-09-23 §1296 实测）：本机 PATH 上的 `python` 是 `…\hermes\hermes-agent\venv\…`（3.11，缺 strawberry / `import trinity` 失败）而不是仓内规范解释器 ⇒ 四条红**全是假的**；尾行已自带规范解释器路径与复跑命令，**照它跑一遍再读判据** |

| `fulltest_gate.py`（重型手动门禁，D4 的证据源） | `全量门禁证据: … verdict=PASS`（**两段** rc 都是 0；**跑之前**解释器不对会 `rc=3` **拒绝开跑**并给出可粘贴命令，§1376） | `verdict=PYTEST_FAILED` ⇒ 红只在 **pytest 段**（全文在 `output/fulltest_pytest_<ts>.log`，§1308）；`verdict=EVAL_FAILED` ⇒ pytest 绿、**eval 段**红（看 `eval_tail`，复跑 `python scripts/run_evals.py --all`）；`verdict=PYTEST+EVAL_FAILED` ⇒ **两段各有红，别只修一段**（§1313）；`verdict=TIMEOUT` ⇒ **没有结论**（`eval_skipped_reason` 会写明 eval 段为什么没跑；**常见成因是上限给得太小** —— §1322 已把默认从 1500s 提到 9000s，实测完成需 ~62 分钟）；证据里没有 `pytest_log` 键 ⇒ 它建于 §1308 之前；**`[COVERAGE-GAP]`**（§1319）⇒ 证据是新鲜的、也是绿的，但**代码面在它之后变过**（清单比 size/mtime/sha256）⇒ 它绿的是**旧代码**，重跑一次全量门禁（先看资源窗口）；**`rc=3` / 33 秒就 `PYTEST_FAILED` / `[FOREIGN-INTERPRETER]`**（§1376）⇒ **不是门禁红，是「这一次根本没跑」**：解释器不是仓内规范那个（PATH 上的 `python` 常是 hermes venv，缺 strawberry ⇒ 9 条 collection ERROR，而**引擎初始化日志照打**、看起来像跑过了）⇒ 照它给的命令复跑，**别照着这条红改代码**（建 §1376 之前的老证据按 `python` 键兜底判） |

**闭环判据（loop_health 三条环，红要红得精确）**

- `fok-counts`：表非空 / 更新新鲜 / 欠账趋势 / **表路径异常原文**
- `fok-prewarm`：来源有效性（需求 vs 提前算）+ 预热 ROI + 周对比
- `fok-hitrate`：区间查表全命中比例（含**计数器重置识别**，避免重启造成假红）

**两条纪律（血泪换来）**

1. **跑压测 ⇒ 立刻登记探测窗口**（`fok_mark_test_keys.py --record-window N`）——否则压测命中会被
   记成「自然流量」；`fok_abstain_ab.py` 已自动登记，`--auto-burst` 兜底。
2. **已知的红要标记、不要静默**（`dsh-ops/loop_health_acks.json`）：红仍是红，只是标注「已复核」；
   到期后 fok-mark-test 会打 `ACK-EXPIRED` 并返回非 0。

## 11. 改 .ps1 的铁律（2026-09-20 §933 事故）

- **必须保住 UTF-8 BOM 与行尾**：用通用编辑器改 .ps1 会剥掉 BOM，PowerShell 5.1 遂按 GBK 解码中文，
  here-string 结构被破坏 —— 实测 `[Parser]::ParseFile` 报 **652 个错误**、整条维护链跑不起来。
- 因此（**理由**：无 BOM 会被 5.1 按 GBK 解码 ⇒ here-string 误判 ⇒ 语法整体破坏）：
  改 .ps1 一律用 `python scripts/ps1_safe_edit.py --file X --old A --new B`（写回后自检解析错误数），
  体检用 `--audit`。**判据**：BOM=True、`Parser::ParseFile` 错误数=0。
- **锚点必须从目标文件本体取**（--old-file 里的文本要逐字节等于文件里的那几行）：
  实测 §983 连续 4 次「未找到待替换文本」的真因不是换行，而是**手写/从截断读数抄的锚点**里多了一个空行；
  正确做法是「读文件、取那几行、原样写进锚点文件」（§984 用此法一次命中，工具自报 loneLF 0→0）。
  补充（§989 实测）：**行尾也要跟着目标文件走** —— 本仓 trinity-supervisor.ps1 是 CRLF，而 trinity-dsh-maintenance.ps1 是 **LF-only**（2504 行、CRLF 计数 0）；给错行尾同样报「未找到待替换文本」。
- **第四件：凡【按锚点插入】的改动，改后必须断言「原有成员仍在原位」**（2026-10-08 §G6R 事故）。
  **规则**：只断言「加了什么」的检查，对「挤掉了什么」是瞎的 —— 必须另加一条**反向的**断言：
  **类方法数 ≥ `git show HEAD:` 的基线 且 HEAD 的成员集 ⊆ 当前**。
  **事故**：`g6_d11_patch.py` 用锚点把**模块级** helper `_conflict_tokens()` 插进了**类体内部**
  （锚点 `_compute_sha256` 是**类方法**，其后的空行**仍在类体内**）⇒ 那个 **col-0 的 `def` 把类体截断**，
  其后 **65 个原类方法（仍 4 空格缩进）变成它的嵌套函数体** ⇒ 运行期类只剩 13 个方法
  ⇒ **29 个抽象方法无人实现 ⇒ `PostgreSQLAdapter` 无法实例化 ⇒ Trinity 写入路径被打断**（40 分钟后才被撞上）。
  （**理由**：**`ast.parse` 过 ≠ 结构没变** —— 本次「dry-run + 改后 `ast.parse` + 写前备份」**三件都做了**，
  而 `ast.parse`/`py_compile`/`import` **全过**、损坏照样成立 ⇒ **三件套不足以覆盖"按锚点插入"这一类动作**。
  ⇒ 故本仓**按锚点改文件一律四件套：dry-run · 改后 `ast.parse` · 写前备份 · 改后断言原有成员仍在原位**。
  ⚠️ 「三件套」这个说法在 2026-10-08 之前**只存在于任务书与报告里、仓内搜不到**（实测）；
  **本条是它第一次落成仓内条文**，故凡引用「按锚点改文件三件套」的地方，一律以本条为权威。）
  **判据（五条机械清单）**：① 类方法数 ≥ HEAD 且 HEAD 成员集 ⊆ 当前；② **类体跨度（`end_lineno-lineno`）≥ HEAD**；
  ③ 被改函数的直接语句数不得少 ≥3 或 ≥10%；④ 模块级函数内嵌套 `def` ≤ 8；⑤ **模块级插入的锚点必须落在模块级语句上**。
  **可执行版**：`tests/unit/test_pg_adapter_instantiation_contract_20261008.py`（8 passed；
  含"合成截断源 ⇒ 必红"的牙齿）。**判据**：该判据全绿；且**改后与 HEAD 的成员数对照**已给出。
- 同理（**理由**：给 PowerShell 传含引号的参数会丢引号、内联 python 的嵌套引号极易无声失败，
  两者都表现为「命令没报错但什么都没发生」）：一律写成脚本文件再跑，判据是**看得见的输出**。
- **凡经由编辑器/write 生成的 `.ps1`，一律只写 ASCII**（中文注释也不行）—— 要中文就用
  `python scripts/ps1_safe_edit.py`（保 BOM）改，或干脆把逻辑写成 `.py`。
  （**理由**：通用编辑器/接口写出的 .ps1 **没有 BOM** ⇒ PowerShell 5.1 按 GBK 解码 ⇒ 中文把字符串定界符吃掉，
  症状是「乱码 + 莫名其妙的语法错误」。**§933/§983/§995 写过三次，§1088 是第 4 次**才看清区别：
  前三次都是「**改** .ps1」，这次是「**新建** .ps1」—— 所以规则要按**文件类型**记，不是按**动作**记。）
- **跨调度器执行的任务一律用绝对路径**（§995 实测）：维护链任务的 -DirectCommand 里写相对路径
  （如 runpy.run_path 里给相对路径）**在自己 shell 里能跑、在调度器里必失败** —— 调度循环的 cwd 是
  C:/WINDOWS/system32，实测报 FileNotFoundError: C:/WINDOWS/system32/scripts/x.py。
  本仓既有写法是 runpy.run_path(r"$TrinityRoot/scripts/x.py")（20+ 个任务都这样）⇒ **照抄它**。

## 12. 数据放哪里：state/ 是机制状态，PG 是测量数据（2026-09-20 三次验证）

**规则**：`~/.trinity/state/*.json` 里的**每个文件都被 organ_freeze 当成「器官」**——要求**登记 + 具名消费者**。所以：

- **机制状态**（驱动行为的开关/游标/队列）放 state/ ⇒ 必须登记器官、必须有具名消费者；
- **测量数据**（证据、日志、采样、窗口）放 **PostgreSQL** 或 `output/`（证据目录），**不要放 state/**。

**为什么这条要写清（理由）**：放错目录有两种坏结果 —— 要么被门抓住（好），
要么**悄悄多一个没有消费者的器官**（坏：本仓 §785-§787 就是「有生产者无订阅者」的 82 个状态文件）。

**本会话三次实测**（都是同一类错误，第三次才改成规则）：

| 轮次 | 我把什么写进了 state/ | 后果 | 改法 |
|---|---|---|---|
| §933 | A/B 证据 `fok_abstain_ab.json` | 门报「新增/未登记且仍活跃」 | 证据挪到 `output/` |
| §945 | 探测窗口（当时用 JSON 记录） | 同上 | 改 PG 表 `fok_probe_windows` |
| §955 | U1a 窗口采样 `memory_utilization_windows.json` | 同上 | 改 PG 表 `utilization_samples` |

**体检命令**：`python scripts/organ_freeze_gate.py`（7 项，含 `freeze:no_unregistered_organ`）。

**同族坑（§992 实测，别踩第三次）**：state 对象是 **PSCustomObject**，**给不存在的键赋值会抛异常**
（报错原文形如「在此对象上找不到属性」）。要加新键必须用 Add-Member -NotePropertyName X -NotePropertyValue V -Force，
判断存在用 $state.PSObject.Properties['X']。**改 state 前先在本文件里搜 Add-Member / PSObject.Properties 看既有写法** —— 本仓已经为 restartedAt 写过一次同样的修复，§992 仍踩了第二次。

## 13. 窗口指标不要用定值基线（2026-09-20 §954/§955）

### 13.0 口径统一：不参与检索的类目**不进**任何以「被读」为尺的分母（2026-09-21 §1036-§1039）

**规则**：类目属于引擎 `_RETRIEVAL_EXCLUDE_CATEGORIES`（当前为 perception）时，
**归档门 / 热路径覆盖率 / 覆盖率分母**这三处**一律剔除**，且清单**只从引擎常量取**（不另立一份）。
（**理由**：拿「被读率」衡量不参与检索的数据本身就是错的尺 —— 详见下方实测三连。）

**理由**（实测三连）：这类记忆**不参与语义检索** ⇒ 拿「被读率」衡量它本身就是错的尺；
实测把它混在里面时，读数会互相打架并对撞门限 ——

| 现象 | 实测 | 改法 |
|---|---|---|
| 归档门与 importance 打架 | importance 回填后落到 0.515 > 门限 0.5 ⇒ **钉死解除却一条都归档不了**（候选 2→104） | 排除类目不适用 importance 门（§1036）⇒ 候选 104→**4,063** |
| 热路径覆盖率被稀释 | 归档 4,167 条后 `hot_path_coverage` 反而 26.2%→**24.5%**（判定翻 ATTENTION） | 分母剔除排除类目（§1038）⇒ **26.65%，verdict=OK** |
| 覆盖率分母混淆两类批量数据 | `BULK_CATEGORIES` 把可检索的 kb_harvested 与不可检索的 perception 合算 | 拆成 `RETRIEVAL_EXCLUDED_CATEGORIES` + `RETRIEVABLE_BULK_CATEGORIES`（§1039） |

**判据**（三处都有 S1 测试，且都断言「与引擎常量同源」，防漂移）：
`python -m pytest tests/unit/test_cold_corpus_retrieval_excluded.py tests/unit/test_utilization_caliber_excluded.py tests/unit/test_retrieval_coverage_caliber.py`

**附带纪律**：剔除**必须显式报数**（如 `retrieval_excluded_rows`），不许静默丢数据 ——
否则「读数是干净了，但没人知道少了什么」。

**同族坑（新工具接手时先查这一条）**：任何按类目/生产者分桶的**新读数**，
落笔前先问一句「这个类目参与检索吗」；不参与就别放进以被读为尺的分母。

### 13.1 直读 `content` 会读到**密文**（2026-09-21 §1044-§1046，三次实测）

**规则**：需要**内容**的离线分析（查询构造、结构价值、语料抽样）**一律走引擎/接口取解密后的 content**
（REST `GET /memories/{id}` 或引擎客户端读取路径），**绝不直读 PG 的 `content` 列**；
只需**数字/时间/状态**的分析（importance 分布、年龄、覆盖率）直读 PG 没问题。
（**理由**：PG 的 `content` 列是密文，直读会拿到 `enc:v1:` 而**看起来一切正常** —— 详见下方实测代价。）

**理由**：`memories.content` 是 **AES-256-GCM 密文**（形如 `enc:v1:…`，解密发生在引擎/接口侧）。
（**2026-09-29 口径补正**：PG 侧实际是**混合**——多数行为密文，但 **15.0%（7,675/51,242）仍为明文**
且含当日新写入（PG 写端加密是按类目白名单、默认空）⇒ 规则更强：**直读 PG 的 content 一定不可信**（**仅 SQLite 镜像有实装点**）
（既可能是密文，也可能是明文），一律走引擎/接口取解密后的内容。见 `docs/SECURITY_BOUNDARIES.md` SB-1/SB-3。）
直读会静默拿到密文 ⇒ 所有基于内容的读数全部失真，而且**看起来一切正常**。

**实测代价（同一件事栽了三轮）**：
1. 自检索 A/B 跑出 `self_r5 = 0.033` —— 查询是密文碎片（不是引擎不检索、也不是查询不好）；
2. `doc_structure_value()` 分布表（「纯日志 0.248 / 规范 0.799」）**只在明文行上成立**
   —— 对加密行算的是密文的结构（函数没错，错在喂进去的输入）；
3. 改用接口取数后又有 **44/60 条取不到明文**（取数判据挡住，未静默使用），
   说明「取数路径是否正确」本身要**先验证再使用**。

**判据（可失败，写进分析脚本）**（理由：判据本身要先能失败，否则等于没有判据）：
① 抽样断言取到的文本**不以** `enc:v1:` 开头；
② 报告 `n_skipped_no_plaintext`（取不到就**计数**，不许静默跳过）；
③ 第一次用某条读取路径时，先打印 3 条**原始响应**（状态码 + 顶层键名）确认形状，
   别像 §1042 那样「猜形状」跑出 0.000 再回头查。

### 13.2 不同失败原因**不许合并计数**（2026-09-21 §1042-§1048，一轮里栽了三次）

**规则**：任何离线分析里，`取不到数据`必须**按原因分开计数并上报**，例如：
`n_api_unavailable`（接口停摆）/ `n_ciphertext`（拿到密文）/ `n_shape_mismatch`（响应形状没对上）/
`n_empty`（确实为空）。**禁止**合成一个 `skipped`，更禁止把停摆算成「数据质量问题」。

**理由**（同一类错误的**四种外衣**，全部实测；第 4 种见 §1091-§1093）：
1. **形状没对上**（§1042）：检索结果我按 list 解析、引擎实际返回 dict ⇒ 自检索跑出 `0.000`，
   看起来像「语料检索不动」；
2. **拿到密文**（§1044）：直读 PG 的 `content` 得到 `enc:v1:…` ⇒ 查询变成密文碎片（`0.033`）；
3. **接口停摆**（§1047）：一轮里 44/60 条取不到明文，我把它们与「无明文」**算进同一个计数器** ⇒
   读数像语料缺陷；重跑 3 条样本**全部 HTTP 200 + 明文**，真因是那段时间 API 在停摆。
   （附带发现：我依赖的引擎兜底 `Trinity.get_memory` **根本不存在** ⇒ 兜底是空操作，越兜越静默。）
4. **半可用**（§1091-§1093，本轮实测）：API 重启窗口里 `/openapi.json` **能取到、但只回了少数路由** ⇒
   文档里的路由被逐个判「openapi 里没有」⇒ 一次读出 **39 条假问题**（复核只有 **3** 条 = 基线）。
   改法：加**就绪守卫**（`MIN_OPENAPI_ROUTES = 50` + `openapi_ready()`），未就绪一律走
   「路由核验未执行（不静默通过）」= **1 条显式说明**、**不计入问题数**。
   **教训**：`取不到` 与 `取到了但不完整` 都必须与 `不存在` 分开 —— 否则读数会在**两个方向**上失真
   （离线时静默绿、半可用时爆红）。判据：`tests/unit/test_doc_claims_readiness.py`（假文档注入 ⇒ 必须 `__error__`）。

**判据（可失败）**：① 报告里必须同时出现「各类失败计数」与「有效样本数」；
② 只要有接口停摆，**结论一律 INCONCLUSIVE**（读数不可信 ≠ 结论）；
③ 工具自己的结论标签要写清含义 —— 例如 `PASS_RISK_ONLY` 必须自带「**不是启用批准**」的说明
（本仓已有工具用它表达「风险侧没有量到下行」，而自检索充分性在该语料上不成立）。

- **理由**：滚动窗口（如「近 24h」）读数天然漂移，对**定值基线**判「不得下降」必然随机变红 ——
  实测 §954：18:38 重录基线，18:39 又红；反复 `--accept-baseline` 会让棘轮名存实亡。
- **正确做法**：与**同窗口**比（如「与 7 天前最接近的采样比，跌破一半才判红」），样本不足时**明说不判**。
- 落地示例：`scripts/memory_utilization_audit.py` 的 `WINDOW_JUDGED` + `_u1a_window_compare`；
  纯判据带 S1 测试（**理由**：判据本身要先能失败，否则等于没有判据）——50 vs 200 判 FAIL、150 vs 200 判 PASS。
- **前置复核（理由**：阈值是经验值，第一次真实判定前无法知道灵敏度是否合适）**：U1a 的 50% 阈值，
  **第一次真实同窗口判定后必须回看**（太松=漏报缓慢退化；太紧=变成新的噪声源）。
  同族先例：§929 给 U1b 加的 ledger-epoch 守卫。

### 13.3 清单类判据：**可信度取决于匹配规则的边界**（2026-09-21 §1050-§1060，一轮里踩了四类盲区）

**规则**：用正则/扫描建出来的「清单」（结论标签表、状态词表、消费者表…）必须**每次放宽或收紧匹配规则后
重新拿一次清单**，并在声明里写清**这次覆盖的范围**。任何「已清空 / 已全部登记」的说法，
都要带一句「在什么匹配规则下」。
（**理由**：清单的可信度**完全取决于匹配规则的边界** —— 详见下方四类盲区。）

**四类盲区（全部实测，逐个把清单打回过）**：

| # | 盲区 | 实测形态 | 后果 |
|---|---|---|---|
| ① | 只认大写 | `[A-Z][A-Z_]{2,}` | 小写状态词（unwired / only_self_read / idle_by_design…）全在盲区 |
| ② | 放宽后**过量捕获** | `[A-Za-z]…` | 把相邻键（`verdict_llm: yes`）也当结论标签 ⇒ 32 个里混着 assert/llm/yes |
| ③ | 只认赋值/字典值，不认**比较** | `r.get(verdict) == idle_by_design`、`in (…)` | 这类标签**看不见**（前两版正则都漏） |
| ④ | **嵌套条件**只抓第一个字面量 | `verdict = fresh if … else (idle if …)` | 第二个分支（`idle`）漏掉 |
| ⑤ | **输出面不同**：同一工具在 tty/管道、`--json` 与交互路径下打印的行**不一样** | `python x.py` 直跑能看到判定行，子进程捕获只看到三拆行（1923 vs 1994 字节） | 比对「取不到目标」被误判成工具坏了；实际是**取错了输出面**；且 stdout 可能是**截断投影**（实测 1800 字符截断后判定字段被截掉）⇒ **一律比产物** |
| ⑥ | **工具不穿过 junction / 符号链接** | `glob(tests/**/*compress*.py)` 在一个 Junction 根上返回「No files found」，而文件**一直都在**（2026-09-24 §1316 实测：`C:\Users\Administrator\trinity` 是 `D:\trinity-code` 的 Junction） | 「没找到」被读成「不存在」⇒ 新写了一份与既有判据**重叠**的文件、并改了一个被那份判据钉住的行为 ⇒ 全量门禁 2 条红。**改法**：找文件用 `grep`（按内容走，不受 junction 影响），或先 `Test-Path` / `os.walk(realpath)` 复核再下结论 |

定稿规则（本仓已用，字面正则见 scripts/verdict_labels.py 的 scan_unregistered）：键必须**独立**出现，
且**同时**扫「赋值」与「比较」两种出现方式；嵌套条件需另读代码补齐。
**判据**：改生成器（TEMPLATE.format 类）时必须看 **traceback** —— 只回显最后一行会漏掉 KeyError，
本轮实测因此第三次踩坑（正则片段里的花括号被当成格式化字段）。


**同族纪律一：比对脚本本身要先自验**。写「接线前后是否一致」的比对时，先拿一个**已知会不同**的字段
**补充（§1063 实测第二次踩坑）**：自验的**第一条断言**应当是「**比对目标非空**」——
我把两个空字符串比成「逐字节一致」过一次；「一致」最常见的来源就是「两边都没有」。
另：**子进程捕获与交互式运行看到的输出可能不同**（同一工具在本例里交互时常打印判定行、
子进程路径下不打印）⇒ 比对前先确认「这一行真的被取到了」，别默认它一定在。
试它能不能报出来 —— 否则「一致」可能只是「没看见」。实测：`ages()` helper 只递归 dict 不递归 list，
于是报「0 个不同」，而全字段比对是 **34 个不同**（自相矛盾才被发现）。

**同族纪律二：只加字段的改动，用「对照开关 + 同次双跑」证明非侵入**。加一个 `TRINITY_*_ANNOTATE=off`
之类的开关，同次跑两遍，比对的是**判定骨架**（所有 verdict/grade 字符串）而不是整份 JSON ——
因为年龄/时间字段天然漂移（实测 34 个差异全是 `*_h`）。
**用哪条命令**（工具已就位：`scripts/verify_noninvasive.py`）：

    python scripts/verify_noninvasive.py --tool scripts/X.py --artifact output/X.json --verdict-only --env-key TRINITY_VERDICT_ANNOTATE

- `PASS` 才算非侵入；**`UNKNOWN`（取不到目标）永不当 PASS**（退出码 2）；
- 不知道产物在哪：先 `--list-artifacts`（实测 `blood_flow_status.py` 会列出 8 天产物）；
- 想确认判别力真实：**跑一次负例对照**（不 strip 关键字段，应当 `REJECT`）—— 实测 harvest 开关就是
  「strip importance ⇒ PASS / 不 strip ⇒ REJECT」这一对。
**同族纪律三：错误必须留痕，症状常在别处**（§1074-§1076 实测）：

- **任何 `except` 都要把原因写进读数/日志**，不许静默吞掉 —— 本轮的真凶其实是「计算失败：…」那行**已经写出来**的，
  只是没人去看；同类四次：形状没对上 / 拿到密文 / 接口停摆 / 变量未定义，**每一次的症状都出现在别处**
  （表现为「读数不对」「不同源」「0 行」），而真相都在被吞掉的异常里。
- **防御式初始化要成对做**：补 `_sel = []` 时忘了同一个表达式里的 `_rows`（§1076），
  ⇒ `NameError` 被吞 ⇒ 刚写好的值被 `except` 覆盖 ⇒ **两条读数不同源**。
  判据：补防御前先**把整行表达式读一遍**，逐个变量补；补完立刻用探针打印**同一读数的所有相关字段**，
  确认「同时有值或同时缺省」。

### 13.4 判据的**窗口边界**和阈值一样重要（2026-09-21 §1086-§1087，首次实跑就抓出）

**规则**：任何「趋势 / 斜率」判据，除阈值外**必须**声明**在哪段窗口上算**；窗口里若含**事件**
（登记洪峰、批量导入、事故、压测），要**从事件之后**切窗口再算。
（**理由**：阈值写错会误报；**窗口写错会把「收敛」看成「恶化」** —— 方向相反的那种错，更难发现。）

**实测（工具第一次跑就抓到自己的缺陷）**：

- 真实序列 `0, 57, 1902, 1890, 1878, 1866, 1854, 1842`：首尾比是 **+1842（涨）** ⇒ 判据说「机制变了」；
- 真相是「一次登记洪峰 + 其后**单调下降**」⇒ 切到**洪峰后 6 轮** ⇒ **`ACK_OK`（收敛）**。

**判据**：`python scripts/fok_backlog_trend.py --rounds 8`（rc=0 = 收敛、ACK 成立；rc=1 = 需重新定性）；
回归测试**同时断言反事实**（不切窗口**必须**判 `MECHANISM_CHANGED`）—— 使「这个修复不是多余的」可执行
（**理由**：反事实断言把「切窗口」钉成**必要条件** —— 否则将来有人删掉这一步，判据照样全绿）。

### 13.5 **写侧的哑线**：写了没被读、算了没被导出（2026-09-21 §1119-§1124，一天抓到两条）

**规则**：任何「我记下来了 / 标注好了 / 算出来了」的改动，**必须附一个可搜索特征串作为证据** ——
在该改动的**消费者输出**里去搜它（例如 ACK 的 `until` 日期、新字段的键名、新标签的字面量）。
（**理由**：写侧的错误**不会报错**，它只是「没有效果」；只有消费者输出里的特征串能区分「写了」与「生效了」。）

**两条实测哑线（同一类，方向相反）**：

| # | 形态 | 实测 | 判据（修完后） |
|---|---|---|---|
| 1 | **算了但没导出** | `plaintext_ratio_audit.py` 里 `_sources6` 一直在算，却从没写进 `out` ⇒ 打印侧永远打「6h 归因未就绪」；**而失败提示恰恰指向那一行** | `PLAINTEXT-SOURCE-6H:` 后面必须是**真行** |
| 2 | **写了但没被读** | `loop_health.py` 的 `_apply_ack` 只接了 `closed-loop` 与 `fok-prewarm`；`fok-counts`/`fok-hitrate` 在 `_load_acks()` 之前就 `line()` 掉了 ⇒ 写进这两条环的 ACK **永远显示不出来** | 输出里必须能搜到该 ACK 的 §号与 `until` 日期 |

**判据（可失败，两条都跑过）**：

- 修 #1 后：`含 PLAINTEXT-SOURCE-6H: <真数据>` = **True**；
- 修 #2 后：`含 §1122` = **True**、`含 2026-10-06` = **True**、`已复核·已知` 由 3 处变 **4 处**。

**同族提醒**：这条与 §13.2（「取不到 ≠ 不存在」）是**镜像**的 ——
那条防的是**读侧**把「取不到」当成「不存在」，这条防的是**写侧**把「写过了」当成「生效了」。

**判据设计检查单（写/改任何判据之前先读）**：`dsh-ops/CRITERION_CHECKLIST.md` ——
它是本文档 §13.0-§13.5 与 §16 的**可执行汇总**（**分组与条数都随实测增长，别写死**：A 输入面 / B 时间面 / C 写面 / D 判据自身 / E 横向纪律 / F 预算与调度面，且**以后还会加**；每条都带本会话的实测反例：哪一轮、什么症状、代价是什么）。
机械化自查：`python scripts/criterion_hygiene.py`（现查**四项**：每条闸门是否有**可失败证明** / 读数是否带**采样时刻** / 是否有裸 `except` / `why` 里的 **§ 号是否真实存在**）。
（**理由**：本会话的 12 条教训里有 9 条是「判据自己错了」而不是「被测系统错了」；检查单把这类错**前移**到落笔之前。）
**待人工决策项**（已查清、但有代价或涉口径，不该由 agent 单方面决定）：`dsh-ops/DECISIONS_PENDING.md` ——
一页二十八条（重要性来源 / PG 加密白名单 / 预热跟随口径 / **常驻服务的完整性级别** / **机器级第三方提交内存占用** / **窗口 BLOCKED 时的周检口径** / **独立监督腿由谁看住** / **谁在杀监督腿** / **fok 补算的节奏** / **冷启动的向量索引补建** / **冷索引请求的阻塞面** / **FOK 校准映射的二值阶跃** / **FOK 打分口径** / **GraphQL filter 的时间字段** / **PG 池中死连接的处置** / **目标注册表的陈旧提议** / **归属过滤的显式作用域豁免** / **来源未定的半归档行** / **检索决策的 outcome 口径** / **缺口清单余下五项的测试集选料** / **官方分片取不到时的替代口径** / **历史双层密文行是否就地迁移** / **②TTL 与官方 CR 的答案面做不做** / **CR 的问句→关系链要不要用 LLM 解析** / **门控概率源饱和了怎么办** / **记忆注入防御换不换机制** / **学习型显著性信号要不要投入** / **常驻 API 的存储后端口径**），每条都带**现状实测、选项与代价、回滚方式、证据 §号**。
（**条数别写死在别处**：本行与上面的导航行都跟着 `DECISIONS_PENDING.md` 的实际条数走 —— 2026-09-21 新增 D4 时漏改过一次；2026-09-22 新增 D5 时按纪律同步改了两处，但**新增 D6 时又漏了**，加 D7 才一并补上 ⇒ 纪律的判据是 `python scripts/doc_regen_guard.py --run` 全 PASS **且**人工比对条数，光靠前者抓不住漏计数。）
**要对接并行会话的待办** ⇒ `dsh-ops/HANDOFF_PEER_SESSION.md`（别人写的理由/口径里我复核出问题、但不该由我单方面改的条目；每条带证据 §号与一行改法）。
**本会话新增/加强的工具索引**：`dsh-ops/TOOL_INDEX.md` —— 本会话新增的工具（静默跳过审计 / 欠账趋势 / 明文尖峰归因 / 文档再生成守卫 / 判据卫生 / importance 位移 / 离线排序对照 / 可执行检查单）
各自的「一句话用途 + 什么时候跑 + 判据要点」，附「现象 → 跑哪条」对照表。


## 14. 写中文文档/脚本的引号纪律（2026-09-20 实测：同一轮里栽了 4 次）

**规则**：中文文本里的引号一律用 `「」`，**不要用 ASCII 双引号**；要强调的是代码/路径用反引号。

**理由**：ASCII 引号会与本仓的多层传递**抢定界符** —— JS/TS 模板串、PowerShell 参数、内联 python 字符串，
任何一层都会把它当字符串边界 ⇒ 表现为「命令没报错，但内容被截断 / 参数丢失 / 什么都不发生」。
实测（§956 一轮内）：写中文小节时用了 ASCII 引号，导致**工具调用连续 4 次解析失败**，
每次都只报一句 `Expected ',', got 'ident'`，看不出是引号问题。
（本条自身第一版也栽了同一坑：讨论引号的文字里又用了 ASCII 引号 ⇒ 再失败一次。）

**判据（写完立刻做，别相信「编辑返回 OK」）**：

1. 用**渲染后的产物**核对，而不是编辑器的返回：例如抓 `^## 1[0-9]\.` 看小节是否真的在；
2. 核对**字节数**是否按预期变化（本轮实测：声称改过但字节数没动 = 其实没改）；
3. 机械检查：`python scripts/zh_doc_check.py <file...>` —— 扫「CJK 紧邻 ASCII 引号」的行并给出行号，
   退出码非 0 表示有可疑处（advisory，供人工判断，不自动改文件）。

**同族**：§11 第 3 条（给 PowerShell 传含引号参数会丢引号、内联 python 嵌套引号极易无声失败）——
都是「定界符在传递层被吃掉」这一类。判据也一样：**看可见的输出**。

## 15. 当前真实数字速查（生成时实测；历史文档里的数字可能是当时的记录）

**理由**：文档里的数字写下即过期，而读者无从察觉；这里在**生成时刻现算一遍**并给出刷新命令，
先拿到真数，再读别处的历史数字。

| 项 | 当前值 | 刷新命令 |
|---|---|---|
| 闸门条数（冻结集） | **26** | 见 docs/GATE_SET.json / python scripts/gate_set.py |
| 环条数（近 24h 有记录的） | **29** | python scripts/loop_health.py |
| fok 物化表 key 数 | **28321** | SELECT count(*) FROM fok_counts |
| REST 路由数（openapi） | **199** | GET http://127.0.0.1:8001/openapi.json |
| EXECUTION 轮次索引节数 | **1374** | python scripts/execution_toc.py |
| 文档引用欠账（paths/flags/routes） | **9 / 0 / 3** | python scripts/doc_claims_check.py --glob "dsh-ops/*.md" --glob "docs/*.md" --exclude "dsh-ops/EXECUTION*.md" AGENTS.md |

（本节由 scripts/export_agents_md.py 的 live_numbers_block() 生成：每次重新生成 AGENTS.md 都会现算。）
## 16. 改动的**观察面**纪律（2026-09-21 本会话七条，全部付过代价）

**规则**：下结论之前先问一句 —— **这条结论的观察面，覆盖了我要断言的范围吗？** 覆盖不了就先扩大观察面，再落笔。
（**理由**：七条里有五条是同一个病：**用一次不完整的观察下了结论**，而症状都出现在别处。）

| # | 规则 | 代价（实测） |
|---|---|---|
| 1 | **改「带未提交本地改动」的文件前，先 Copy-Item 快照**；回滚就是拷回来 | 省了快照 ⇒ 只能手写反向补丁 ⇒ 把文件改坏 ⇒ git checkout 抢救 ⇒ **连带丢弃他人未提交改动**，追了四轮才补回（EXECUTION 1030） |
| 2 | **git checkout -- 文件是破坏性操作**：动前先 git diff --numstat 看有无未提交改动并另存 | 跳过这两步 ⇒ 丢弃他人 32 行指标块（已按 diff 精确恢复）（1030） |
| 3 | **判据要区分「必要代价」与「真正的危害」** | 第一版判据写成「持锁期间**零**迭代」⇒ **永远不可能通过**（快照本身必须在锁内迭代一次）；改成「锁内迭代有界 且 重调用必须在锁外」才对（1032） |
| 4 | **活系统上的数据迁移：断言落在「目标状态」，不要落在「全局计数」** | 断言「总行数不变」在并发写入的表上误触发 ROLLBACK —— 错的是断言不是迁移（1033） |
| 5 | **看日志/输出要看完整行**（截断会漏掉行尾的判据） | 按 150 字符截断看某行，漏掉行尾的 [action disabled: …] ⇒ 把周期性重启**错归因**给一个只警告不动作的机制（1031） |
| 6 | **结论落笔前先确认「搜索范围」** | 只在一个文件里 grep 某模块 ⇒ 报「未找到」⇒ 误判「接线断了」；实际接线在同包另一文件（1031） |
| 7 | **「编译通过」不是「落点正确」的判据**；判据是**渲染后的产物**，而且**要确认产物真的是自己刚生成的那份** | 我三轮都插错位置（插到了模板结束引号之后），且验证时**漏了 --out** ⇒ 读到的始终是别人生成的文件（1037） |

**同族补充**：

- **改一个机制之前，先读它邻近的注释** —— 差点删掉一个「API 在线则跳过」的守卫，而理由就写在它上面 3 行（1035b）；
- **设计工具时，失败路径比成功路径更该被设计**：维护窗口工具在「窗口被重开」时先是会覆盖数据、改好后又在清理现场（管道继承）挂住 —— 两次都是失败路径（1036b/1036d）；
- **长活要用后台作业跑**：多分钟的脚本跑在同步调用里，输出会随调用超时一起丢失（本会话丢了两次读数）。
  **并且要礼让资源**（2026-09-22 §1189/§1190 补）：跑重活前先看窗口 —— `python scripts/resource_window_check.py`，
  报 `WINDOW_BLOCKED` 就别跑。**理由**：当日实测（§1184）我在 09:04 起跑 `gate_set.py`（含 selftest 基准），
  机器提交内存被压到 `0.1GB`、内存守卫 `no-eligible-victim`，**API 在那个窗口停摆约 20 分钟**才自行恢复。
  「放后台」只解决**我这边不丢读数**，不解决**机器被榨干**——两件事都要做。

**判据的读取面（2026-09-22 §1206/§1212 补，代价：4 条长期假红 + 两个工具当场崩）**：

- **捕获子进程输出时，必须同时决定「按什么编码解」**（**理由**：子进程按什么编码写、父进程按什么编码读
  是**两件独立的事**，只钉一头另一头就把中文变成乱码 —— 而乱码**不报错**，只会让判据对不上，
  症状还出现在别处）。本机 ANSI 代码页是 **936(GBK)**，而编排器普遍写 `encoding="utf-8"`
  ⇒ 中文 `must`/`absent` 模式**全部对着乱码匹配**。实测（事故）：`checklist_run.py` 的
  `B3/§14/E3/E1` **长期假红** —— 真因不是被测系统坏了，是**读错了面**。
- **判据的结果不得取决于「它是被谁启动的」**（**理由**：维护链里 `autostart` 已设 `PYTHONIOENCODING=utf-8`，
  于是同一批判据**链内绿、手跑红**；这种靠运气的判据比没有判据更坏，它会把人训练成不信任红灯）。
- **修法**：给子进程注入 `env["PYTHONIOENCODING"]="utf-8"`（本仓先例是 `scripts/gate_set.py` 的 `_child_env()`，
  2026-09-20 就写过 —— 它**没有扩散到兄弟编排器**，这才是真问题：多入口的仓库里「修一处」等于没修）。
- **边界（2026-09-22 §1212 实测修正，别按「常识」猜）**：`PYTHONIOENCODING` 只对 **Python 子进程**生效
  （A1/A2 反例：不带钉 ⇒ `������ OK`；带钉 ⇒ `发现性 OK`）。但**不要**因此以为 powershell 子进程要按 `mbcs` 解 ——
  默认态下 powershell/cmd/schtasks 子进程**输出的就是 UTF-8**（A3 按 utf-8 解 ⇒ `中文测试 OK` 原样；
  A5 按 gbk 解 ⇒ `涓�鏂囨祴璇�` 乱码）。所以照 utf-8 解就对，**理由**：A3/A5 这一对反例已经把「按 gbk 解」证伪。
  （本轮先按常识写成「powershell 要按 mbcs 解」，被自己的 A3/A5 反例当场否掉 —— §16 第 7 条同族。）
- **但 UTF-8 不是 powershell 的「天生属性」，而是同一父进程内可被子进程改写的共享态**（§1212 复核实测）：
  只要有**任何一个**子进程执行过 `[Console]::OutputEncoding = GetEncoding(936)`，其后**所有兄弟子进程**
  （含 cmd/powershell）立刻改按 GBK 写，而父进程仍按 utf-8 解 ⇒ 当场乱码；`cmd /c chcp` 仍报 936，
  **光看 chcp 抓不到这个翻转**。**理由**：它会让「同一个文件」的读数随执行顺序漂移 ⇒
  要么在命令里显式设 UTF-8，要么父侧别把编码当天生属性。
  （实测链条两轮一致：cmd/ps=UTF-8 → 置 936 → cmd/ps=GBK → 置回 65001 → 又 UTF-8。
  仓库自己的 `.ps1` 都是置 UTF-8，方向安全 ⇒ 这是**构造复现**，不是现网已发生。）
- **副作用同族（理由同前：同一个 GBK 控制台）**：脚本自己 `print` 中文/`⇒` 时也要
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（本仓 267 个脚本里 250 个有）。
  本轮补了两个缺它的：`zh_doc_check.py`（扫到含 `⇒` 的行**当场 traceback**，而检查单的补救提示恰恰叫人跑它
  ⇒ **补救路径自身不可用**）与 `rationale_audit.py`（**理由**：它崩在打印「无理由规则」的原句上，
  而它的 rc 与「棘轮判红」**一模一样** ⇒ 工具坏了会被读成你又违规了）。
- **两个闸门的读面已按此修**（§1212）：`checklist_run.py::_run` 与 `doc_truth_gate.py::_run` 都钉了子进程编码。
  **理由**：后者的判据正是拿**中文健康签名**（`[OK] EXECUTION_TOC.md 是最新的`、`明文写入速率:`）判文档是否过期，
  而它的子工具里有没 reconfigure 的 ⇒ 无钉时签名读成乱码、**假红**；链内跑因 `gate_set` 已钉而显绿
  ⇒ 又一个「链内绿、手跑红」。反事实：`temp/_dtg_face_probe_20260922.py`（有钉原样 / 无钉乱码）。
- **同一族的另一半**：字符串里有 `⇒` 这类非 GBK 字符时，**子进程直接崩**（A6：Python 子进程打印 `⇒` 无钉 ⇒ `rc=1`；
  A7 有钉 ⇒ `rc=0`）—— 这正是 `zh_doc_check.py` 手跑时 traceback 的形态。
- **判据（可失败）**：`python scripts/checklist_run.py --selftest` 的 `selftest(encoding)` 断言
  「中文子进程输出**原样**捕获」，并带反事实断言「不钉编码的旧写法命中不了」。**理由**：只有这两条一起，
  才把「读面」钉成**必要条件** —— 否则将来有人删掉钉编码那一行，判据照样全绿（§13.4 同族）。



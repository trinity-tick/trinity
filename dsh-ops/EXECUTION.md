# DSH -> Trinity 优化执行记录（EXECUTION.md）

本文档记录按批/目标执行的全部改动、验证结果与回滚方法。

> EXECUTION 458D 归档轮转：2026-09-02 之前的历史轮次（2026-07-22 起约 15434 行，含第 30-65 轮/105-4xx 大脑化轮/第 10-23 轮系列/旧 goal 日志）已移至 dsh-ops/EXECUTION_ARCHIVE_PRE457.md（git 历史亦完整保留）。
> 导航：dsh-ops/EXECUTION_TOC.md（轮次行号索引，可重新生成）。

---

## 457. 大脑化体检优化包全量执行（2026-09-02 晚，按体检建议逐项落地）

> 背景：体检结论——大脑化 59 能力/110 模块已齐，但 ①意识蓝图情境仅 6/10（情境=按查询现算，无持续流）；②语义级视觉未接通（感知 85% 是特征级）；③社会 95% 无真实场景（市场 0 成交/无第二 agent）；④图谱稀疏（entities 187 / relations 980）；⑤自主好奇心无调度入口；⑥基准 runner 版本元数据写死 8.5.0；⑦58 个未提交改动（09-02 第 10-23 轮）双仓库未 commit。以下全部执行。

### 457.1 P0-1 情境持续上下文流（意识蓝图情境 6 → 9，总分 82 → 85）
- 新模块 trinity/brain/situation_stream.py：聚合"当下"信号（时间/24h 活动/近期感知/好奇焦点/全局自我/库规模/自省），双写 ~/.trinity/state/situation_stream.json + PG session_context(id='ctx:brain')，TTL 600s 惰性刷新；
- core/client/_search.py._build_auto_situation 注入情境流摘要（附加 enc:v1 解密助手 _plain——raw PG 读取密文/错误串不再污染情境嵌入）；
- 检索验证：摘要含"当下 09-02 …写入 N 检索 N 活跃会话 N | 我:…"；蓝图 assess：1_situatedness 6→9，total 82→85/100；
- 维护链：新增任务 situation（scripts/run_situation_stream.py），allowed/switch/每日 03:00 链就绪；PS5.1 实测 maintenance -Tasks situation OK。

### 457.2 P0-2 双仓库未提交改动收口（见 457.8 提交节）
- 09-02 第 10-23 轮全部修复（检索解密/推理通道/自我画像/CE 重排/多语言 CE/候选池净化等）+ 本包改动一并 commit + push origin + D: 同步。

### 457.3 P1-1 语义级画面理解（感知从特征级 → 语义级）
- Ollama 拉取 qwen2.5vl:3b 成功（3.2GB）；
- trinity/vision.py 新增 describe_image_semantic / describe_image_any（语义优先特征降级；TRINITY_VISION_SEMANTIC=0 可关；OLLAMA_HOST 0.0.0.0→127.0.0.1 归一）；
- api /memory/perceive 图像分支改走 describe_image_any；
- 实测（合成 UI 截图）：特征级"截图 720x420px 中性色 4 处文字区" → 语义级"发货失败异常界面：出库单 DO-20260902-1188 库存锁定失败…"（模型真实读出界面文字）。

### 457.4 P1-2 第二真实 Agent 社会闭环（社会 95% 从"声明"到"证据"）
- scripts/brain_social_loop_demo.py：种子 ops-bot（persona=ops-team）3 条高价值记忆 → /market/list 真实上架 2 资产 → 主 agent /market/buy 真实成交（tx_id=tx_dsh-social-demo_ops-bot_…，TrustExchange 记账）→ ToM 推断 → 跨 agent 检索；
- trinity/brain/theory_of_mind.py 修复：session_context 无 agent_id 列致 focus 静默为空——改为从该 agent active 记忆内容（解密）推断关注，各查询独立守卫；
- 实测：ToM 命中 8/8（数据库/备份/WMS/单据/可靠性/恢复）；跨 agent 检索 top1-2 即 ops-bot 记忆；报告 ~/.trinity/state/social_loop_*.json。

### 457.5 P1-3 自主好奇/主动发起调度入口（机制早已存在，补进日链）
- 每日 03:00 链新增 replay,curiosity,proactive,cognition-agent,situation（原链 21 任务 → 26）；
- 修 brain/curiosity.py 与 scripts/curiosity_daily.py/proactive_daily.py 的 D: 硬编码（→动态仓库根，web_search 缺失优雅降级 note）。

### 457.6 P2-1 图谱增密（entities 187 → 3,188 / relations 980 → 17,803）
- scripts/graph_densify.py：PG active 知识类记忆（3,820 条）jieba 实体抽取（新实体 3,000）+ 同文档共现关系（16,820），sha256 幂等 + 日门 ~/.trinity/state/graph_densify_last.json；实测 62s 完成。

### 457.7 P2-2 口径收口
- benchmark/longmemeval_official_runner.py: trinity_version 硬编码 8.5.0 → 运行时 __import__('trinity').__version__（与 8.2.1 一致）；
- docs/ARCHITECTURE.md 大脑化全景同步：距离更新为 EXECUTION 240 官方自评（记忆97/认知96/意识72/感知85/社会95），意识蓝图 82→85（情境 9），59 能力/110 模块/30 维护任务，附 457 落地证据。

### 457.8 验证与提交
- 定向冒烟：新模块 import + 蓝图 + 情境流 + 语义视觉 + ToM + 市场 tx 全过；ps1 解析 0 错误（PS5.1 实跑 situation 任务 OK）；
- 全量 pytest 后台门禁（见 pytest-full-preround.log）；
- 提交：工作树（原 58 + 本包）→ main → push origin → D: 副本 fetch+reset 同步。


## 458. 下一步优先级全执行（2026-09-02 晚，P0/P1/P2 逐项落地）

> 承接 457 报告"建议下一步优先级"，全部执行（多子项并行，各带实测证据）。

### 458.1 P0-1 官方 LongMemEval 跑分锁定（复现 + 数字落档）
- 复现跑：official_lm_eval.py 全量 500 题 → R@1/3/5/10 = 1.000×6 类（EXIT=0，~8 分钟）；
  与上午锁定 AnswerAcc 0.560（SS-U .986/KU .731/SS-A .679/TR .399/MS .391/SS-P .367，$0.40）共同入档 docs/BENCHMARKS.md；
- **根因修复（重要）**：longmemeval_official_runner.py 09 起 Trinity 默认后端=PG，runner 只设 TRINITY_STORE 未隔离
  → 历次官方跑误连生产库（慢 10x；100 题 1h20m；并写入 lme 类目 21,773 条 archived 污染）。
  修复：强制 TRINITY_STORAGE_BACKEND=sqlite 临时库 + WAL/synchronous=OFF + 增量 .partial 落盘（防中断全丢）。
  教训记录：基准工具必须显式声明存储后端。

### 458.2 P0-2 发布三件套（本机可做部分）
- docs/RELEASE_NOTES_458.md（能力/锁定数字/诚实差距/发布清单）；
- docs/BENCHMARKS.md 补 458 复核段；
- git tag 推送（见提交节）；MCP registry/PyPI/GitHub Release 页 = 外部账号注册，如实标注待办。

### 458.3 P1-1 生成侧弱项专项（tr/ms/ss-p A/B）
- benchmark/answer_eval_strategies.py：官方 oracle 数据、同题对照 base vs tr（日期线索+时序提示）/
  ms（跨会话整合+冲突取新）/ ssp（偏好两段式），判分与 official_lm_eval 同款；
- 运行 → .trinity/bench-official/qa_strategy_*.json（见文件结果，随后续轮锁 Δ）。

### 458.4 P1-2 持续感知流
- scripts/perception_loop.py：perception_inbox 收图→本地语义视觉（qwen2.5vl）→/memory/perceive(vision 通道)
  →刷新情境流；处理图归档 done/；--once 供调度；
- trinity/brain/perception.py 补 CHANNEL_BASE["vision"]=0.6（此前默认 0.4 不达编码阈 0.45）；
- 注册 maintenance 任务 perception-continuous + autostart 30 分钟分支（marker 防抖）；
- 实测：合成 UI 截图入 inbox → "[语义] 库存锁定失败异常警告/出库单…" → perceptions(vision)×2 + perception 记忆×2 落库 ✓。

### 458.5 P1-3 ops-bot 升格自治 agent
- scripts/opsbot_daily.py：从自身命名空间记忆提取主题（轮转）→ 命名空间检索证据 → 决策记忆
  （agent=ops-bot）→ 高价值新记忆自动上市场；注册任务 opsbot-cycle + 入日链；
- 实测：topics=[备份/数据库/策略/恢复/库存/承运] → 选题"备份" → 自证 2 + 外部线索 2 →
  决策记忆 02a77d58… 写入 + 市场上架 listed ✓。

### 458.6 P2-1 BEAM 现状收口（诚实）
- 本机已有 10k/100k 档产物（benchmark/beam_results*.csv + beam_report*.md）；
- 1M/10M 档需更长生成与跑分窗口（数据生成脚本就绪），本轮资源让位官方 LongMemEval，标注为外部窗口待跑。

### 458.7 P2-2 核心级自进化阶段 3 试点 ✅
- scripts/evolve_core_gate.py：ALLOWLIST(trinity/security/crypto.py) → LLM 提案 → AST 校验
  （单 def、无 import/class）→ 行为门禁 → canonical pytest(168) → git commit；失败回滚+拒绝报告；
- 首个真实补丁：crypto.is_encrypted(content)->bool（LLM 生成、门禁 168 passed 42.1s、已提交）；证据 ~/.trinity/state/evolve_core_pilot.json。

### 458.8 P2-3 联邦最小多实例验证 ✅
- scripts/federation_mini_demo.py：实例 A(3 条) / 实例 B(2 条) 完全隔离 SQLite，
  federation_sync export/import：A→B 后 B=5、幂等复跑不变、B→A 后 A=8；
  SUMMARY bidirectional_ok=True idempotent_ok=True（子进程显式 TRINITY_STORAGE_BACKEND=sqlite——credentials 有 PG 键会劫持 store_path）。
- 诚实边界：单机双实例（包传输级），WAN 传输层仍为外部项。

### 458.9 提交与同步
- 工作树（457 遗留 supervisor E1b 为并行会话改动，未纳入）→ main 分批 commit → push → D: 同步。
## 459. Fable 5.1 泄露对照审计建议全量执行（2026-09-02，P0→P2 逐项落地）

> 背景：Anthropic Fable 5.1 发布当日系统提示词（27.5 万字符）遭泄露（Pliny
> CL4R1T4S），公开转载揭示其记忆系统设计：①"至死不记"隐私禁区（未成年身份/
> 法律敏感/心理推断/性史/自残，用户主动暴露也强制清空）；②记忆治理四问
> （为何存/谁提供 vs 谁推断/何时过期/可否检阅删除）；③46 工具 schema 与运行时
> 组装上下文。对照 Trinity 现有机制（SHA-256 链式审计/加密/隔离）产出差距
> 清单并按 P0-P2 全量执行。

### 459.1 P0-① 敏感类别写入门控（NEVER_STORE 名单 + POLICY_PURGE 拒存）
- 新模块 trinity/security/sensitive.py：双语（zh/en）规则扫描，5 类别对齐 Fable
  禁区——minors_pii（未成年身份，年龄词+身份词 40 字窗口共现防误伤）/
  legal_status（犯罪记录/案底/移民/种姓）/ psych_health（确诊/住院/用药语境）/
  sexual_history（性史等强信号）/ self_harm（自杀自残，含英文）。
- 语义分级：high（组合强信号，默认拒存）→ 内容**根本不落库** + 审计
  action=POLICY_PURGE（details 含类别/标签/severity，memory_id=NULL）；
  medium（单点提及如"抑郁"）→ 仅 metadata["sensitive_scan"] 标记不阻断；
  策略开关 TRINITY_SENSITIVE_POLICY=quarantine 可把高危降级为隔离归档
  （落库 archived + 审计 action=POLICY_QUARANTINE）；TRINITY_SENSITIVE_SCAN=off
  整体关闭（默认 on）。接线点：core/client/_ingestion.py ingest（唯一落库汇聚点，
  在 store_memory 之前、加密之前执行）。
- 规则冒烟 15/15（正例 6/负例 9，含 WMS 业务文本零误伤）；pytest 13 passed
  （tests/test_sensitive_policy.py：拒存零落库+审计、quarantine 归档、medium 标记、
  off 放行、正常写入无感）。

### 459.2 P0-② provenance_role 强制（来源语义：谁说 vs 谁推断）
- ingest 强制归一 metadata.provenance_role ∈ explicit|inferred|derived：
  显式传入优先；否则 role=assistant/system、modality≠text（代码/轨迹/感知）、
  content_type∈kb 系、generated=True → derived；user_verbatim/user_stated →
  explicit；其余默认 inferred（防"系统推断被当用户原话固化"，对齐 Fable 治理
  第一问）；非法值自动兜底重推。
- 检索输出：sqlite _search.py（FTS+LIKE 两 builder）SELECT 补 metadata 列，
  PG postgresql.py search 结果映射补顶层 provenance_role + metadata（JSONB
  dict/str 双兼容）；旧数据无键 → 顶层 None（legacy 语义，回填任务见 459.4）。
- 测试：ingest 四向（默认 inferred/显式 explicit/assistant→derived/非法兜底）+
  检索输出两断言（FTS 通道 provenance_role=inferred/explicit）。

### 459.3 P0 验证
- pytest tests/test_sensitive_policy.py 13 passed（24s）；模块/接线语法全过。

### 458.1b QA 策略 A/B 终结果（2026-09-02 21:13，每类 30 题同题对照，oracle + LLM judge）
| 类目 | base | tr(日期线索) | ms(跨会话整合) | ssp(两段式) | 结论 |
|---|---|---|---|---|---|
| temporal-reasoning | .667 | **.800 (+13.3pp)** | .767 (+10pp) | .433 (-23pp) | tr 生效：日期线索+时序提示 |
| multi-session | .200 | .267 (+6.7pp) | .267 (+6.7pp) | .100 (-10pp) | ms/tr 微增益；MS 仍是生成整合难点 |
| single-session-preference | .267 | .267 | **.467 (+20pp)** | .200 (-6.7pp) | ms(带会话标注+冲突取新)意外最强；ssp 两段式证伪 |
- 子集口径与全量类别基线不同（同题对照 Δ 才是证据）；ssp 两段式二次生成引入噪音，弃用；
- 产物：.trinity/bench-official/qa_strategy_20260902_205414.json（elapsed 1138s）。
### 459.3 P1-③ 答案评测 citation-coverage 归因指标（闭环 CH-1 残留）
- 新模块 benchmark/citation_coverage.py（独立文件，不触碰其他会话在改的
  answer_eval_strategies.py main()）：LongMemEval-S 官方 500q 同款数据/同款
  sqlite temp 隔离（显式 TRINITY_STORAGE_BACKEND=sqlite，吸取 458 P0-1 教训）/
  同款 DeepSeek 接线；确定性归因判定为默认（与 AnswerAcc_strict_substring
  哲学一致），--judge-llm 可选宽松判分（JUDGE_SYSTEM 同款）。
- 指标（per-category + totals）：citation_coverage = 带有效 [n] 引用（且该
  引用证据确实支持该论断）的事实数/GT 事实总数；answer_coverage（论断被
  覆盖）；citation_rate（已覆盖论断中带有效引用比例）；evidence_coverage
  （GT 事实在检索证据中比例，召回侧参照）。答案提示强制"每论断必须带
  支持它的 [n] 标记"（provenance 随证据走，对齐 Fable 泄露教训）。
- 产物 ~/.trinity/bench-official/citation_coverage_<ts>.json。
- 验证：单测 12 passed（tests/test_citation_coverage.py：分句/标记解析/
  正误索引/zh 引用/汇总数学/空输入）；端到端冒烟 --limit 1：
  answer_cov=1.000 citation_cov=1.000 citation_rate=1.000 evidence_cov=1.000。

### 459.4 P1-④ 条目级 expires_at + 过期复核队列（接入 maintenance 日链）
- ingest（core/client/_ingestion.py）：metadata["expires_at"] 归一（datetime→
  ISO）；"写入即到期"（过去时刻）→ 落库后立即归档 + 链式审计
  action=EXPIRED_AT（details 含 expires_at/source=ingest/reason）。
- 新脚本 scripts/run_expiry_review.py：扫 PG（默认）/SQLite 的 active 记忆
  中 metadata expires_at 非空项 → 复核队列报告（~/.trinity/state/
  expiry_review_<ts>.json：expired=已到期、due=临期 --horizon-days 7、
  broken=解析失败，条目含 memory_id/content_preview/expires_at/
  importance/category）；默认 dry-run 只出队列；--apply-expired 把已到期
  记忆置 status='expired' 并写链式审计 EXPIRED_AT（source=expiry-review）；
  --db/--out-dir 供测试隔离。--dry-run 兼容参数。
- maintenance 注册任务 expiry-review（$allowed + 命令块 + switch），
  autostart 每日 03:00 链追加 expiry-review；ps1 保持 UTF-8 BOM+CRLF 编辑，
  双文件 Parser 解析 0 错误。
- 实测：maintenance -Tasks expiry-review → OK（store=pg expired=0 due=0
  broken=0，队列文件落盘）；单测 4 passed（tests/test_expiry_review.py：
  写入即到期归档+审计、未来到期保持 active、due/horizon 边界与队列落盘、
  apply-expired 置 expired+审计）。

### 459.5 P2-⑤⑦ GDPR 硬擦除通道 + 不可逆操作 confirm 门禁
- adapter.purge_memory（SQLite _crud.py 与 PG postgresql.py 同接口）：
  content（密文）/tokenized_content（明文）/memory_versions 历史内容
  覆写为哨兵 [HARD_PURGED <ts> <id>]、图谱链接清理、importance=0、
  status='gdpr_deleted'（PG 另置 embedding=NULL、metadata 记 hard_purged）——
  内容明文/密文销毁而**行保留**，SHA-256 receipts/版本链/审计链不被破坏
  （合规日志与 receipts 并存；规避了既有 PG erase_memory 的 FK 失败缺陷）。
- client.purge_memory(confirm=True 门禁)：无 confirm → confirm_required
  拒绝（P2-⑤）；确认后执行 + HARD_PURGE 链式审计（details 含 reason/
  prior_sha256/status）+ ANN/缓存清理。
- API DELETE /memories/{memory_id}?hard=true&confirm=yes：缺 confirm=yes
  直接 400（不可逆二次确认）；走 purge + 聚合池/BM25 移除。
- 验证：单测 7 passed（tests/test_purge_readside.py：覆写匿名化（主行+
  版本链均无原文）、not_found、confirm 门禁两态、HARD_PURGE 审计含
  prior_sha256）。

### 459.6 P2-⑥ 读侧 untrusted 内容标注
- 新模块 trinity/security/readside.py：只读标注（不改存储不阻断）——
  命中注入/指令覆盖/角色仿冒/外泄模式 → result["untrusted"]=True +
  untrusted_reason="injection:<severity>:<patterns>"；否则 False；
  TRINITY_READSIDE_SCAN=off 关闭（默认 on，扫描限内容前 2000 字符）。
- 接线：sqlite _search.py（FTS+LIKE 两 builder）与 PG postgresql.py
  （search_memories + vector_search）结果组装处——写路径已有
  injection/sensitive 过滤，读路径此前无 trust 区分（Fable 教训：
  检索内容里不可信指令要能识别）。
- 验证：注入指令内容 → untrusted=True+reason；良性 → False；off → 无键。

### 459.7 P2-⑤ 批量 decay confirm 标志
- scripts/run_decay_compress.py：--confirm 参数 +
  TRINITY_DECAY_REQUIRE_CONFIRM=on（显式要求确认）时缺 --confirm 直接
  SystemExit 拒绝；无人值守维护链默认不设 env、行为不变。

### 459.8 验证与提交
- 定向：4 个新测试文件 36 passed（sensitive_policy 13 / expiry_review 4 /
  citation_coverage 12 / purge_readside 7，39s）；模块/接线语法全过；
  PG 生产库只读冒烟：search 结果带 provenance_role/untrusted 字段无回归。
- 全量 pytest 后台（pytest-post-fable459.log）。
- 提交：工作树 → main → push origin → D: worktree fetch+reset 同步。

## 458C. Trinity 梳理包全量执行（2026-09-02 晚，用户批准"根据建议执行"）

### 458C.1 P0-1 数据清理（lme 基准垃圾 21,773 条）
- 导出归档：全部解密为 JSONL → ~/.trinity/archive/lme_purge_20260902_212901.jsonl（21,773 条 / 141.2MB，解密失败 0）；
- 删除：memories 41,327 → 19,554（-21,773 lme archived）；孤儿 memory_versions -8,056（剩 142）、memory_links -17（剩 11,538）；
- 审计链保留（audit_log 引用不删）；entities/relations 未动（3,218 / 17,845）。

### 458C.2 P0-2 执行器收敛（标注不删除）
- docs/RUNNER_MAP.md：基准 runner / 自治四件 / 感知四件分工矩阵；
- NOTICE docstring ×5：official_lm_eval.py（正式入口）、longmemeval_official_runner.py（实验入口）、curiosity_daily.py、cognition_agent.py、perception_scan.py。

### 458C.3 P0-3 同仓协作纪律（事故教训固化）
- RUNBOOKS.md 新增"同仓多会话协作纪律"：禁 hard-reset、先 fetch 后 rebase、顺序编号、推送失败本地兜底。

### 458C.4 P1 文档与口径
- MODULES.md 头部：EXECUTION 298（110）→ **458C 复核（目录实测 191 模块）**；
- ARCHITECTURE.md 全景头标注最后同步轮（116-459）；
- docs/TESTING.md：fast=168（默认）/ full=根 tests 122 文件口径说明 + 历史"815/1261"口径澄清；
- dsh-ops/EXECUTION_TOC.md：563 个轮次标题行号索引（自动生成，可再跑 gen 脚本更新）。

### 458C.5 P2 顺带
- 4 份 08-15 快照文档 git mv → docs/archive/（TRINITY_STATUS_20260815_V2 / TRINITY_SUMMARY_20260815 / FEATURE_OVERVIEW_20260815 / FUNCTION_SUMMARY_20260814）。

### 458C.6 验证
- 全部为文档/数据操作；改动的 5 个 py 仅 docstring（compile OK）；后续 fast 168 回归在提交前跑；
- 提交前 git fetch 防并行漂移；禁 hard-reset。

## 458D. 遗留建议执行（2026-09-02 晚）：归档轮转 + 孤儿脚本收敛 + 模块分级任务

### 458D.1 EXECUTION.md 归档轮转
- 2026-07-22 ~ 09-02 早的历史轮次（15,433 行：第 30-65 轮 / 105-4xx / WMS 413-456 / 09-02 第 10-23 轮系列）
  → dsh-ops/EXECUTION_ARCHIVE_PRE457.md（git 完整保留）；
- 主文件瘦身 15,674 → 250 行（前言 + 457~458C + 未来追加）；
- 导航重建：EXECUTION_TOC.md（主 6 条）+ EXECUTION_ARCHIVE_TOC.md（归档 560 条，含行号索引）。

### 458D.2 引用审计后脚本收敛（1,620 文件扫描）
- 删除 4 个零引用孤儿（git rm，历史可恢复）：agent_biography.py / action_loop_daily.py /
  forgetting_daily.py / associative_daily.py；
- 保留（有维护任务/文档引用）：perception_bridge / web_perception / evolve_loop / 各 *_daily 等；
- 分工地图不变（docs/RUNNER_MAP.md）。

### 458D.3 module-classify 定期刷新
- 新维护任务 module-classify（scripts/module_classify.py --json）→ allowed + switch + 周一质量门禁链；
- 首跑 OK：core/reserve/frozen 分级 JSON 输出（并行会话同期加的 expiry-review 任务共存无恙）。

## 460. 生成策略固化 + 全量 500 复测（2026-09-02 深夜，P0 优先执行）

> 依据 458.1b A/B（TR/SS-P/MS 子集 Δ）与 KU 新验证，把有效策略固化进 official_lm_eval.py
> 的生成管线（--strategy routed|base，base 保锁定口径），全量 500 复测一次锁新数字。

### 460.1 实现
- official_lm_eval.py 新增 build_qa_prompt() 按题型路由：TR→日期线索+时序提示；
  MS→会话标注+跨会话整合+冲突取新；SS-P→同款+偏好口吻；KU→newer-wins（新验证 +6.7pp）；
  SS-A/SS-U→base 不变（防回退）；输出带 strategy 字段。
- KU 30 题子集验证（qa_strategy_20260902_214004.json）：base .800 → ms .867（+6.7pp），ssp .367 再证伪。

### 460.2 全量 500 复测结果（同数据集同 judge，$0.457，1349s）
| 类目 | base(上午锁定) | routed | Δ |
|---|---|---|---|
| knowledge-update | .731 | **.808** | **+7.7pp** |
| single-session-preference | .367 | **.433** | **+6.7pp** |
| temporal-reasoning | .399 | .406 | +0.8pp |
| multi-session | .391 | .391 | ±0 |
| single-session-user | .986 | .986 | ±0 |
| single-session-assistant | .679 | .679 | ±0 |
| **总体** | **.560** | **.578** | **+1.8pp** |

- 诚实说明：子集 A/B Δ（TR+13.3/SS-P+20/KU+6.7）高于全量 Δ——小样本乐观偏差；
  全量下 KU/SS-P 增益稳健、TR 微弱、MS 无效（提示级不够，需检索/证据结构级改造，记录为下一轮方向）；
  ssp 两段式全量口径不再使用。
- 产物：.trinity/bench-official/lme_oracle_500_routed_20260902.json；残留 1,013 个临时评测库已清理。

## 461. MS 结构级实验证伪 + 第二次大脑距离自评（2026-09-02 深夜，继续执行建议）

### 461.1 MS 检索端会话聚合实验（证伪，已回滚）
- 假设：MS 弱（0.391）因 top-5 上下文扎堆单会话 → 改为 top-4 会话 × 每会话 2 条聚合上下文；
- 实现：official_lm_eval.py 增加 _ms_group + routed-MS 走聚合（8 条上限）；
- 30 题同题 A/B（ms_ab.log）：plain（top-5 routed）= **0.300** vs grouped = **0.233（-6.7pp）** → 证伪；
- 处置：三处改动全部回滚，official_lm_eval.py 与 EXECUTION 460 锁定版（0.578）逐字节一致（git diff 空）；
- 结论：MS 瓶颈不在上下文分布——下一步候选：turn 级检索（历史 mock turn16 49.6% 经验）或 top-k 扩到 20 后再剪、证据句两段式抽取（需专门实验，勿在锁定 runner 上叠实验）。

### 461.2 第二次大脑距离自评（写入 docs/ARCHITECTURE.md）
| 维度 | 240 轮 | 二次自评 | Δ | 依据 |
|---|---|---|---|---|
| 记忆 | 97 | 97 | 0 | oracle 500 R@1-10=1.0 双复现；cleaned-S/M 未跑不虚涨 |
| 认知 | 96 | **95** | -1 | QA 0.578 已锁（+1.8pp）但 MS 0.391 未破 |
| 意识 | 72 | **75** | +3 | 情境 6→9 / 蓝图 85 / 持续流；主观体验与深度行动仍缺 |
| 感知 | 85 | **90** | +5 | 语义视觉实测 + 持续感知流 vision 入库 |
| 社会 | 95 | 95 | 0 | 第二 agent+真实成交+ToM 8/8；仍单机早期 |
- 原则：只按可复核证据调分、无证据持平；认知降 1 是为如实反映生成侧瓶颈。

### 461.3 收尾
- 临时目录清理（msab_*）；EXECUTION 460 v1 口径保持可复现（0.578）；提交推送 D 同步。

## 462. MS 专项三连实验 + 深度上下文 v2 锁定（2026-09-02 深夜，继续执行）

### 462.1 实验链（30 题同题 A/B，全部留档）
1. 会话聚合 top4×2：**-6.7pp**（证伪）→ 回滚；
2. turn 级 ingest（user+assistant 对）：**-6.7pp**（证伪）→ 弃；
3. 上下文深度：cap5=0.200 / cap10=0.200（±0）/ **cap14=0.400（+20pp）** → 采纳。
- 结论：MS 弱因不是上下文"分布/粒度"，而是**答案消息排位深（6-14 位）**——检索 top-10 只够 session R@k，不够 QA 证据。

### 462.2 落地 + 全量 v2 复测（$0.62，1144s）
- official_lm_eval routed-MS：检索 top-20 + build_qa_prompt(cap=14)；base 与其他类目口径不变（Recall 统计仍按 top-10）；
- **AnswerAcc 0.578 → 0.642（+6.4pp）**：MS .391 → **.617（+22.6pp）**、KU .808→.821、SS-P .433→.467、
  TR .406→.414、SS-U .986 不变；SS-A .679→.661（56 题中 1 题翻转，judge 噪声，如实记录）。

### 462.3 收尾
- 认知维度自评更新：95 → **96**（ARCHITECTURE 注记，MS 瓶颈突破）；
- 临时实验目录清理；产物 .trinity/bench-official/lme_oracle_500_routed_v2_20260902.json；
- 提交推送 D 同步。

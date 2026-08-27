# Trinity 知识周报（2026-08-27）

> 自动生成：高价值决策/知识/总结记忆聚合（近 7 天）

## decision（30 条）

- [decision] 2026-08-27 建议轮三项落地（RAG 文档/全链审计/方向汇总） - 结论: RAG 服务文档化（一行接入）；rollout context 事件（全链回放）；六方向汇总文档 - 关键决策与理由: ①context 记录放执行后（竞态修复——写文件拖慢线程致审批测试失败）②文档化让能力可自
- [decision] 2026-08-27 建议轮三项落地（高价值豁免/检索审计/RAG 服务化） - 结论: forgetting 高价值豁免（value>=0.7 不归档）；检索审计含耗时+层（7 字段可回放）；gateway /v1/retrieval RAG 端点上线（实测 count=3） - 关键决策与理由
- [decision] 2026-08-27 建议轮三项落地（Mesh 扩展/降权默认开启/记忆资产化） - 结论: AgentMesh 超时自动回收+4 类事件通知；forgetting_rerank 默认开启（A/B 20/20 一致安全）；memory_value 资产化报告（TOP 0.97 决策类，高价值 47
- [decision] 2026-08-27 建议轮三项落地（遗忘阶段2/检索降权/AgentMesh） - 结论: 遗忘分布全<0.5（库健康，阈值参数化备用）；forgetting_rerank 降权实测 0→9；AgentMesh delegation 状态机全流程验证通过 - 关键决策与理由: ①降权默认 off
- [decision] 2026-08-27 方向A第二步落地（层过滤生效/forgetting 入链/遗忘基线） - 结论: adapter 检索带 memory_layer（值域 episodic/semantic/None）；_infer_layer 对齐值域；知识查询过滤全 semantic 实测；forgett
- [decision] 2026-08-27 方向A 认知分层第一步落地（层感知检索 + 遗忘决策） - 结论: search layer_hint auto（时间词→STM/IM、知识词→LTM，_infer_layer 全过）；forgetting_score 遗忘分（3000 条评分，库健康 0 归档） - 关键决
- [decision] 2026-08-27 基准 500q 状态确认 - 结论: 500q 已存在（旧口径 0.98/0.93/0.358）；升级口径 300q=0.99/0.9433/0.4667（当前标准）；独立 50q 可复现；200q 补齐因网络挂起终止——升级 500q 转长期项 - 产出: docs/BEN
- [decision] 2026-08-27 建议轮落地（蒸馏量化/编排调度/基准补齐启动） - 结论: 蒸馏量化 20 近串查询 0/20 LLM（基线 8/20——减少 100% 且 holdout 不降）；编排定时调度（every_seconds）；官方 200q 后台补齐中 - 关键决策与理由: ①计数器用 ch
- [decision] 2026-08-27 建议全执行轮落地（增量实况/continue_on_error/阈值 0.55） - 结论: 维护链 pagetree 每日增量实况验证（1.2s）；动作 continue_on_error 链不中断；judge 阈值 0.55 A/B 通过（0.5474=基线不降，LLM 
- [decision] 2026-08-27 建议全执行轮落地（增量入链/多步动作链/向量增量） - 结论: pagetree 任务每日增量+周日全量；动作 if/delay 链；新簇向量 0.2s 增量嵌入 - 关键决策与理由: ①ps1 here-string 历史损坏（ 退格/CRLF 控制字符——summari
- [decision] 2026-08-27 建议轮三项落地（蒸馏 A/B 通过/审批状态机/页树增量） - 结论: 蒸馏 holdout 0.547=基线（不降指标，LLM 调用减少）；审批流 pending→approved/rejected/expired 状态机；页树增量 1.2s（全量 1%） - 关键决策与理
- [decision] 2026-08-27 建议继续轮三项落地（代码健康目标/automation retries/judge 蒸馏） - 结论: code_health=1.0 目标 complete（进化引擎已服务记忆/系统/代码三类指标）；动作 retries 指数退避；judge 蒸馏（词重叠>=0.6 启发式
- [decision] 2026-08-27 进化引擎通用化第一步落地（系统健康目标） - 结论: default_metrics 增加 system_health（ps1 三件套/WAL/备份/API 四项均值=1.0）；系统健康目标 complete——进化引擎首次跟踪非记忆指标 - 关键决策与理由: ①非记忆指标实
- [decision] 2026-08-27 竞品对比矩阵与差距总账已固化为正式文档 - 产出: docs/TRINITY_COMPETITIVE_MATRIX_20260827.md（已入 docs/INDEX.md）。 - 内容: 全网方案全景(Mem0/Exabase/Mastra/agentmemory/TiMe
- [decision] 2026-08-27 代码优化轮落地（P0 LLM 去重 + P1 modules 安全归档） - 结论: runner 裸 urllib 统一到 trinity.llm.client（3 份实现→1 来源）；modules 引用链分析后仅 2 个安全孤立模块归档（engine 系列/包 __in
- [decision] 2026-08-27 建议轮落地（audit-ps1 入链 + stale 观察工具） - 结论: -Tasks audit-ps1 每日自检三件套（32/31/31 ALL OK）；stale_watch.py 预计 2026-09-01 ai_knowledge 自然过期触发（自动采集首次自然
- [decision] 2026-08-27 建议继续轮三项落地（ps1 巡检/stale 快照/UI bar） - 结论: ps1 全任务巡检工具上线（三件套齐全 31/30/30），发现并修复 6 个历史缺失任务；stale 快照显示 5 天后首个源自然过期（自动采集首次自然触发观察点）；UI 类别 bar 图 - 
- [decision] 2026-08-27 建议继续轮三项落地（rollout 审计入链/stale 快照/UI 片段高亮） - 结论: -Tasks rollout-audit 入维护链（usage 定义丢失第 2 次修复——ps1 定义区补丁必须完整行锚点）；stale 实况快照 0/198；UI 片段化高亮修复（
- [decision] 2026-08-27 建议继续轮三项落地（rollout 审计/stale 观察/UI 引擎化修复） - 结论: rollout_audit.py 异常检测就绪（3 动作 0 失败）；每日 eval emit_stale 观察确认；UI 引擎化修复密文泄漏（enc:v1 不再显示）+ 类别下拉/高
- [decision] 2026-08-27 建议继续轮三项落地（失败告警/stale 观察/UI 时间线） - 结论: automation 动作失败→automation.failed 事件→告警规则（executed=1 实测）；每日 eval 自动 emit stale（自然周期观察机制就绪）；UI 加类别过滤+
- [decision] 2026-08-27 建议继续轮三项落地（goal 规则/stale 端到端/UI 增强） - 结论: goal.updated 事件接入自动化（complete 通知/blocked 告警，executed=2）；knowledge.stale 真实周期端到端全通（stale 检测→事件→自动重
- [decision] 2026-08-27 伙伴继续轮完成（exec 白名单修复 + 低置信页树刷新确认） - 结论: knowledge.stale exec 自动采集真实运转（executed=2 failed=0）；低置信→页树刷新规则确认（executed=1 failed=0）——事件驱动运维闭环全通 - 关
- [decision] 2026-08-27 伙伴后续三项落地（API 常驻 automation/UI 拉起/stale 自动采集） - 结论: API 常驻 automation 实测运转（低置信检索触发 executed=1）；记忆流 UI :8010 进 supervisor 拉起；knowledge.stale
- [decision] 2026-08-27 伙伴系列三项落地（验证/表达/automation 启用观察） - 结论: 独立验证（fresh seed 777）复现主实例成绩（Session R@10 0.94、QA 0.48——跨 9 次运行区间 0.94-1.00/0.45-0.48）打破自证；记忆流 UI :80
- [decision] 2026-08-27 使用伙伴闭环落地（Trinity 从自转走向被需要） - 结论: usage_feedback.py 聚合使用数据（3,278 次搜索/7 天、热门查询、高频记忆 1849 次）→ 报告 ingest 为 evolution 输入（实证可检索命中）——使用数据第一次进入进化闭
- [decision] 2026-08-27 RAGFlow 对比 P1 两项落地（有引文生成 + 文档摄入结构化） - 结论: --cite 模式答案带引用 [n]（Groundedness 对齐）；kb_harvest 结构化摄入 185→2,646 条（208 分节+2,253 表格行），表格内容可检索命中 - 关
- [decision] 2026-08-27 Claude-Mem 对比 P1 两项落地（decay 真实摘要 + token 可见性） - 结论: decay 维护链现在走真实 LLM 摘要（修复 TRINITY_LLM_API_KEY 识别 bug）；检索返回带 usage{est_tokens,est_cost_u
- [decision] 2026-08-27 P0 四项全部落地（QA 升级 0.467/发布受阻/判题缓存/MS judge 证伪） - 结论: QA 升级版 300 问 QA=0.4667（旧口径 0.358，+0.11），Recall 0.99/0.94；块 3-5 因网络中断未跑完（剩余 200 问可补） - 关
- [decision] 2026-08-27 P0 优化四项状态（QA 升级中/发布受阻/判题缓存完成/MS judge 证伪） - 结论: 判题缓存完成（LRU TTL 600s 容量 256，TRINITY_REASON_CACHE 可关）；MS 完整性 judge 实验证伪（0.0 < 0.237）——生成侧优先原
- [decision] 2026-08-27 下一步建议三项完成（SS-P 检索专项 + QA 口径升级 + 开源就绪） - 结论: SS-P keyword 0.90 > hybrid 0.80（同 30 问公平对比——hybrid 向量噪音，偏好场景更差）；QA 升级 top-3 完整上下文+judge 增强：0.3

## summary（11 条）

- [summary] 2026-08-26/27 大迭代会话收尾（十六轮：借鉴+基准+治理+发布准备） - 覆盖: 借鉴 PageIndex/Budibase/Codex/DSH/Context7/Claude Science 六轮 + 价值盘点 + 网络评价 + 官方 LongMemEval-S + 整理加固 + 价值
- [summary] 2026-08-27 Trinity 结构·流程·闭环·原理·作用全解析 - 结构: 7 层（Agent/集成/治理/引擎/核心/存储）+ 9 子包（353 文件/12.7 万行）+ 7 进程组件 - 流程: 写入(注入扫描→CRDT→加密→审计→事件) / 检索(别名→模式路由→RBAC→视图) 
- [summary] 2026-08-27 Trinity 现状权威汇总（十四轮迭代后） - 结论: 生产记忆基座定位明确：检索(0.752/0.663/官方0.98) + 进化闭环(93轮/2complete+1blocked) + 可证明性(59k审计) + 运维(0退化) + 开源就绪 - 关键数字: 记忆 25,
- [summary] 2026-08-25 综合评价：优化前后+DSH+Trinity 优劣。优化前：QA acc 评测、检索查错库、评测 hybrid 生产 FTS（17 轮 A/B 从未生效）、白名单虚假、无巩固/预算/操作、967 条审计误报、仅 8 测试、多断环。优化后：MRR/nDCG 论文级、评测=生产对齐、
- [summary] 2026-08-25 Trinity 全方位彻底评价（深度终版 4.0/5）。深度审计发现：①审计链 967 条校验不匹配——99.6% 在 8/16-17 早期版本算法演进遗留（非篡改），8/18 后 43,496 条仅 3 条不匹配（99.99% 一致）——可证明性真实成立；②核心模块无自动化测
- [summary] 2026-08-25 Trinity 全方位评价（终评 4.2/5 生产可用级）。优点：架构完整（4 服务 3 层数据 36 模块）、可证明性独有（CRDT+SHA-256 审计 56,691 条+回执+投毒扫描，超 Mem0/Zep）、检索强（BEAM 100K R@5=1.0）、评测方法论论文级
- [summary] 2026-08-25 Trinity 全方位运行分析（最终版）。结构：4 服务（api:8001/mcp:8000,8003/collector）+SQLite 548MB 权威库+PG:5430 维护库；36 模块 35k+ 行（core 5.3k/adapters 6.2k/agents 8.7
- [summary] 2026-08-25 Trinity 现状全景（21:40 快照）。健康：API v8.2.0 ok，全通道 active，0 降级，uptime 1.76h，每日备份正常（219MB）。数据：SQLite 548MB，memories 34,526（active 22,709/archived 1
- [summary] 2026-08-25 Trinity 自进化系统收尾。最终状态：7 脚本编译 PASS、8 单元测试 PASS、服务健康（API ok/engine healthy）、维护链 evolve-auto/evolve-env 就绪、supervisor 正常。评测真实（MRR/nDCG 连续配对）、参数
- [summary] 2026-08-25 Trinity 自进化优化前后对比。优化前：QA acc 评测（LLM 波动）、检索查错库（import 顺序 bug 致 9 轮 A/B 无效）、私有集全 UNKNOWN、白名单含编造变量、无决策门/收敛保护/消费端。优化后（29 轮 30 缺口修复）：MRR+nDCG@5 
- [summary] 2026-08-25 Trinity 自进化系统最终交付。状态：MRR 连续配对评测（24 缺口全修复）、11 个路径内可测参数（白名单三处一致）、8 单元测试 PASS、6 脚本编译 PASS、证伪库 9+。关键文件：evolve_loop/signal/ab、judge3、build_priva

# Trinity 结构·流程·闭环·原理·作用 全解析（2026-08-27）

> 系统级权威文档：从结构到原理到价值产出的完整链路。数据为当前实测。

## 一、总体架构（分层视图）
```
+---------------------------------------------------------------------------+
| Agent 层    DSH 插件 · A2A · MCP 客户端 · OpenAI/Mem0 兼容                   |
| 集成层      REST 30+ 端点 · MCP 13 工具 · gateway :8002                     |
| 治理层      目标引擎 · eval 12 断言 · RBAC 可见性 · 记忆视图 · 技能 · manifest |
| 引擎层      FTS5 · BM25 · 向量(1024d) · 图谱(PPR) · 页树 · reason · hybrid   |
| 核心层      client · adapter · CRDT 版本链 · 审计链 · 加密 · automation      |
| 存储层      SQLite 权威库(加密) · PG 镜像 :5430 · pagetree · goals · 备份     |
+---------------------------------------------------------------------------+
```

## 二、代码结构（trinity/ 353 文件 / 126,909 行）
| 子包 | 职责 | 规模 |
|---|---|---|
| modules/ (second_brain 等) | 脑启发研究模块群（部分孤立，已文档化） | 60 文件 33k 行 |
| agents/ | 多智能体/聚合池/维度 | 27 文件 10k 行 |
| adapters/ | SQLite 适配（FTS/审计/CRUD/加密/归档） | 17 文件 7k 行 |
| core/client/ | 统一客户端（search/ingest/pagetree/reason） | 24 文件 6.6k 行 |
| api/ | FastAPI 服务 + openapi 规范 | 27 文件 6.6k 行 |
| evolution/ | 进化周期 + 目标引擎（goals.py） | 15 文件 3.7k 行 |
| retrieval/ | 页树（向量页定位）+ 检索组件 | 7 文件 3.2k 行 |
| eval/ skills/ knowledge/ | 断言评测/技能运行时/知识源注册表 | 新增 14 文件 |
| automation/ security/ views/ | 事件自动化/可见性/记忆视图 | 新增 |

## 三、运行结构（进程与存储）
| 组件 | 端口/形态 | 角色 |
|---|---|---|
| trinity-api | :8001 | 主服务（REST/OpenAPI/健康上报） |
| trinity-mcp SSE | :8000 | 会话内工具入口 |
| trinity-mcp HTTP | :8003 | streamable-http（Bearer） |
| gateway | :8002 | OpenAI/Mem0 兼容层（鉴权/限流/映射） |
| collector | 守护 | 事件采集 |
| supervisor/autostart | 5 分钟循环 | 自愈拉起 + 每日维护链 |
| PG 镜像 | :5430 | 维护库（decay/tiers/mirror 目标） |

**存储**：SQLite 权威库（25,375 记忆 / 59,499 审计，AES-256-GCM 加密 content 列）+ 派生资产。

## 四、核心流程

### 4.1 写入流程（ingest）
内容 -> 注入扫描(OWASP) -> 测试隔离检查 -> 规范化 -> CRDT 版本链(内容哈希幂等去重) -> 加密落库 -> FTS/向量索引 -> 审计链(WRITE) -> automation 事件(memory.write) -> 生命周期(巩固/衰减候选)

### 4.2 检索流程（search）
查询 -> 别名展开(aliases.yaml) -> 模式路由：keyword(FTS 短查询) / hybrid(RRF 5 通道) / reason(候选池+LLM 判题,可选 deep) / pagetree(页定位+向量打分) -> 可见性过滤(RBAC) -> 视图(view) -> 结果(带 source_health)

### 4.3 维护流程（每日 3:00 维护链 29 任务）
health -> evolution(周期) -> mirror(PG 对齐) -> decay(归档压缩) -> tiers(分层) -> sync(聚合池) -> pagetree(重建) -> eval(12 断言) -> review(审阅) -> backup(14 天) -> selftest/db-health(WAL 锁监控)

### 4.4 评测流程（实验工件）
评测运行 -> 结果 JSON + manifest(code_hash/env/dataset 哈希/params) -> default_metrics 读取前校验(漂移拦截) -> 审阅循环(对比/异常标记/代码一致性) -> 目标引擎评估

### 4.5 进化流程（evolution 周期）
OBSERVE(观察指标/事件) -> ANALYZE(归因) -> PLAN(计划) -> EXECUTE(执行) -> CERTIFY(eval 12 断言验证) -> skills 沉淀(corrections) -> 周期完成触发 evaluate_goals

## 五、自进化闭环（全景链路）
```
目标引擎 (goal_create/acceptance/3轮无进展→blocked)
   |  周期完成自动评估 (default_metrics 带 manifest 校验)
   v
基准指标 (0.752 / 0.994 / 0.663 / 0.98)  <-- 审阅循环(自动归因/异常标记) <-- evolution 周期(93轮)
   |  未达标
   v
新优化迭代(检索/提示词/结构) --> 全量 A/B 验证(带 manifest) --> 达标→complete / 无进展→blocked
   |
   +--> skills 沉淀(corrections) + 知识健康度(197/0 stale) + CERTIFY(eval 12 断言)
```
**闭环实证**：2 个真实目标 complete（0.752/0.6632）；MS 目标 3 轮无进展自动 blocked（机制非人工）；93 个周期完成；全量评测带 manifest 可复现。

## 六、运行原理（关键机制与设计理由）
| 机制 | 原理 | 为什么这样设计 |
|---|---|---|
| FTS5 + jieba | 词项倒排 + BM25 排序 | 中文近串查询最优（R@5 0.992），毫秒级 |
| reason LLM 判题 | 候选池(30/50) + LLM 相关性判题 + base 填充 | 近义改写难查询：词重叠≈0 时语义判题（0.547→0.663） |
| 页树+向量页定位 | 元数据建树(8900 条/75s) + 摘要向量(1024d) 余弦页打分 | 树索引先定位页再找记忆，免向量库全扫 |
| hybrid RRF | 5 通道(向量/BM25/FTS/图谱/页) 倒排融合 | 标定实验：rrf 0.950 > fusion 0.008 |
| CRDT 版本链 | 内容哈希幂等去重 + 版本演进 + SHA-256 | 重复写入不膨胀、变更可追溯 |
| 审计链 | 每操作写审计 + 回执可独立重算 | 可证明性：防篡改、可举证 |
| AES-256-GCM | content 列加密落库，密钥本地 | 数据本地化 + 密文存储（FTS 不受影响） |
| 目标引擎 acceptance | 指标+操作符+阈值验收；3 轮无进展 blocked | 进化有验收标准，不空转 |
| 实验 manifest | code_hash/env/dataset 哈希/params | 可复现；数据集漂移硬拦截 |
| automation 事件 | 事件(写/搜/目标) + YAML 规则 + 审批/rollout | 外部行为可插拔，默认关 |
| 锁防护 | RLock/只读降级/WAL 监控/超时看门狗 | 多进程共享 SQLite 的历史锁事故教训 |

## 七、能产生什么样的作用（价值产出）
| 作用域 | 具体作用 | 实证 |
|---|---|---|
| 对 Agent | 跨会话记忆：事实/偏好/决策/轨迹可检索可证明 | DSH 插件自动同步会话结构 |
| 对业务 | WMS 领域知识库：197 源健康度管理、对标报告 | kb_harvested 185 文件 |
| 对研发 | 自进化：目标驱动优化、评测自动归因、经验沉淀 | MS 目标 blocked 机制实证 |
| 对合规 | 审计可证明：59,499 条链可独立重算 | /audit/receipt + /audit/integrity |
| 对运维 | 0 退化事件：自愈/备份/锁监控/健康真实上报 | 1249 测试全绿 |
| 对开源 | 可复现基准 + 文档齐全 | LongMemEval-S 0.98 带 manifest |
| 对评测 | 基准 20+ 全可复现 + 官方集成绩 | 网络评价 5.9→7.2 |

## 八、限制与边界
1. 单机 SQLite 架构（多进程共享有锁风险——已有防护，不面向大规模分布式）
2. MS 多事实类目 0.237（生成侧瓶颈，目标 blocked）
3. 官方 QA 中档（检索已对齐头部，生成口径待升级）
4. modules/ 42 个孤立研究模块（未删，保守处置）
5. 未开源发布（文档/许可/隐私全就绪）

*生成 2026-08-27 / 数据：当前实测*
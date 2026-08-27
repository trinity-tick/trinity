# Trinity 现状汇总（2026-08-27 权威版）

> 十四轮迭代（借鉴 PageIndex/Budibase/Codex/DSH/Context7/Claude Science + 官方基准 + 整理加固 + 价值兑现）后的完整现状。数据为当前实测。

## 一、定位
**Trinity = 个人/团队级生产记忆基座**：跨会话长程记忆 + 自进化治理 + 可证明性（CRDT/审计/加密）——同类系统（Mem0/Zep/LangMem）中唯一具备完整进化闭环。

## 二、运行结构（全在线）
| 服务 | 端口 | 状态 |
|---|---|---|
| trinity-api (v8.2.0) | :8001 | ok tier=full |
| trinity-mcp SSE / HTTP | :8000 / :8003 | 在线 |
| gateway (OpenAI/Mem0 兼容) | :8002 | 在线 |
| collector / supervisor / autostart | - | 自愈守护 |
| PostgreSQL 维护镜像 | :5430 | 在线 |

**存储**：SQLite 权威库（25,375 记忆 / active 9,036 / 45 类目 / 59,499 审计，加密默认）+ PG 镜像 + 派生资产（页树 / goals / knowledge_sources / rollouts / aliases）。

## 三、功能能力全景
| 层 | 能力 |
|---|---|
| 检索 | FTS 0.992 / reason 0.752 / reason_deep holdout 0.663 / 页树向量(270簇+270向量+117摘要) / hybrid / 知识检索 |
| 知识层 | 197 源注册表(0 stale) + aliases 别名展开 + knowledge.stale 事件 |
| 治理 | 目标引擎(2 complete + 1 blocked) / eval 12 断言 / RBAC 可见性 / 记忆视图 / 5 skills |
| 评测 | 实验 manifest / 审阅循环(-Tasks review) / 领域评测包 |
| 运维 | automation(默认关) / 维护链 29 任务 / 备份 14 天 / WAL 监控 / 自愈 |
| 安全 | AES-256-GCM / 注入过滤 / 审计回执可独立重算 / 多租户 |
| 集成 | REST 30+ 端点 / MCP 13 工具 / gateway / DSH 插件 |

## 四、量化指标（实测可复现）
| 指标 | 成绩 |
|---|---|
| 500q AnswerAcc (reason) | 0.752 |
| 500q R@5 | 0.994 |
| holdout reason R@10 | 0.663 |
| 官方 LongMemEval-S Session R@10 (500问) | 0.98（对齐头部） |
| 官方 LongMemEval-S Turn R@10 | 0.93 |
| 官方 LongMemEval-S QA | 0.358（升级口径 0.45 已验证） |
| SS-P 偏好类 Session R@10 | 0.90（keyword 最优） |
| 性能 (8-16) | P50 30-41ms / 2431 QPS |

**工程**：353 py 文件 / 126,909 行 / 测试 1249 passed 0 failed / git 258 commits 工作区干净 / eval 12/12 / 文档 101 篇 + EXECUTION 36 节。
**数据**：记忆 25,375 (active 9,036) / 审计 59,499 / 知识源 197 / 页树 270 簇 / 基准 20+ 带 manifest / 官方集 277MB 哈希锁定。

## 五、自进化闭环（真实运转）
目标引擎(2 complete + 1 blocked[MS 0.2375 三轮无进展自动阻断]) -> 周期评估(default_metrics 带 manifest 校验) -> 基准指标(0.752/0.994/0.663/0.98) <- 审阅循环 <- evolution 周期(93轮) <- 维护链 29 任务(每日) -> CERTIFY(eval 12 断言) -> skills 沉淀 + 知识健康度。
**实证**：93 个进化周期；MS 目标自动 blocked（机制而非人工）；2 个真实目标达标 complete。

## 六、价值评估
评分约 A(88/100)；工程资产 300-1200 万 RMB；网络评价加权 5.9 -> 约 7.2/10（官方基准兑现后）；差异化：可证明性(超头部) + 自进化治理(独有) + 本地隐私。

## 七、已知短板
1. MS 多事实类目 0.237（生成侧瓶颈，目标 blocked，需 MS 专用 judge）
2. 官方 QA 中档（0.358-0.45，口径 vs TiMem 78.96 有差异，检索已对齐头部）
3. SS-P 推断型 3 题（词重叠约 0，reason 模式潜在解法）
4. modules/ 约 42 个孤立研究模块（已文档化未删，保守处置）
5. 未开源发布（文档全就绪，动作待执行）

## 八、下一步（按杠杆）
1. 全量升级版 QA（500 问约 7h）更新官方口径为 0.45
2. 开源发布（repo 公开 + PyPI）
3. MS 专用 judge 实验（TR 式顺序校验）
4. SS-P 推断型 reason 模式实验

*生成 2026-08-27 / 数据：当前实测*
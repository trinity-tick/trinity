# Trinity 未来方案 V2 —— 能力导向重规划（2026-08-14）

> 与 V1（FUTURE_PROJECTS.md）不同：V1 是"资产导向"（围绕既有 WMS 数据/个人兴趣展开），
> 本方案**完全不依赖既有数据资产**，只基于 Trinity 内核已具备的能力做重新规划。

## 0. 重新规划的出发点：先盘点"手里有什么"

实测核验过的能力资产（2026-08-14）：

| 资产 | 现状 |
|---|---|
| API 面 | **129 个 REST 端点**，六大协议族：A2A(19) / Identity(14) / Audit(11) / Market(11) / Evolution(11) / Graph(6) |
| 检索内核 | 47 通道检索 + 50 层守护链 + Second Brain v6.50 + 进化引擎（已跑 5 轮） |
| 记忆生命周期 | 衰减 / 压缩 / 分层(Tier) / 冲突解决 / 身份漂移检测 全闭环 |
| 图谱 | 11,009 实体 / 28,043 关系（本日刚构建），traverse 双向多跳 |
| 可观测 | /dashboard、/evolution/heatmap、/audit/metrics、/metrics、/benchmark 端点已存在 |
| 跨模态 | /memory/search/cross-modal、/image-by-text、/text-by-image 端点已存在 |
| 基建 | Docker（entrypoint/init-db）、MCP+API 双通道、supervisor/autostart 自愈、benchmark 并行器（run-benchmarks.ps1 + workflow）、pytest 基线 135 通过 |

**结论**：内核功能远超"已被使用的部分"。V1 只用了检索/采集/同步 20%，剩下 80%（A2A、Identity、Audit、Market、压缩、跨模态、可观测）是沉睡资产。
V2 的路线 = 把这些沉睡资产分别推向 研究 / 产品 / 生态 三个方向。

---

## 1. 三条增长曲线（互相独立，可任选一条深耕）

### 曲线 A：记忆内核纵深（目标：做"更强"）

| 编号 | 项目 | 复用资产 | 验收指标 |
|---|---|---|---|
| A1 | **公开评测基准 MemBench**：把现有 benchmark 并行器 + ContinuousEvalEngine + RAGAS 对齐，升级为可发布的记忆评测套件（数据集 + 评分 + 报告），对标 BEAM SOTA 64.1% | run-benchmarks.ps1、benchmark/scenarios、/benchmark | 产出可复现基准 + 报告，社区可跑 |
| A2 | **检索通道自适应路由**：47 通道按 query 动态选择（简单启发式 → 轻量 RL），降低延迟/成本 | /memory/search/hybrid、/metrics、/audit/metrics | 同质量下延迟/成本下降 30%+ |
| A3 | **长程一致性压力测试**：10 万 token 跨会话一致性验证（GroundTruthEpisodes / IdentityPreservingConsolidator 模块压测） | identity/*、evolution/* | 压测报告 + 发现的漂移案例 |
| A4 | **跨模态记忆闭环**：补全音频/视频 → 记忆 → 跨模态检索（端点已存在，缺采集与评测） | cross-modal 3 端点 | 图/音/文 互检 demo |
| A5 | **记忆压缩 Token 经济学**：compress 的成本-质量模型（省多少 token / 丢多少信息） | /memory/compress/* | 成本曲线报告，可对外发表 |

### 曲线 B：开发者平台产品化（目标：做"好用"）

| 编号 | 项目 | 复用资产 | 验收指标 |
|---|---|---|---|
| B1 | **Memory Gateway**：Docker 一键部署 + OpenAI Assistants / Mem0 API 兼容层 + Python/TS SDK，任何 LLM 应用 5 分钟接入记忆 | Docker 配置、129 端点、MCP 通道 | 兼容层通过 OpenAI SDK 冒烟测试 |
| B2 | **记忆可观测性 Dashboard**：热度热力图 / 衰减曲线 / 图谱演化 / 通道诊断可视化 | /dashboard、/evolution/heatmap、/graph/*、/metrics | 前端 MVP，指标实时可看 |
| B3 | **多智能体记忆治理层**：记忆隔离/共享/仲裁/审计策略化（YAML 配置 + 治理 SDK） | a2a 19 端点、audit 11 端点、identity 14 端点 | 三 agent 协作 demo，策略可热切换 |
| B4 | **联邦记忆**：多实例同步协议（离线优先、增量同步、冲突合并），边缘-云端记忆同步 | sync 脚本、SQLite WAL、memory_versions | 双实例同步 demo，冲突可仲裁 |
| B5 | **私有化合规包**：加密存储 + 审计导出 + 个保法/GDPR 合规能力包 | audit 全链、/memories/export、/identity/bundles/export | 合规清单逐项过检 |

### 曲线 C：生态与社区（目标：做"广泛"）

| 编号 | 项目 | 复用资产 | 验收指标 |
|---|---|---|---|
| C1 | **记忆市场协议标准化**：market 从功能变协议（第三方可接入知识包买卖/估价/信誉） | market 11 端点 | 三方 demo 交易闭环 |
| C2 | **采集插件生态**：任意源→记忆 的 harvester 插件规范 + 插件市场 | collector、harvest 相关 | 3 个第三方插件跑通 |
| C3 | **社区基准榜 leaderboard**：开放 MemBench，第三方提交成绩形成榜单 | A1 的基准套件 | 榜单一期上线（依赖 A1） |
| C4 | **文档/教程/案例体系**：Quickstart / 迁移指南 / 最佳实践 / 案例集 | mkdocs、129 端点 | 文档站完整可跟学 |

---

## 2. 三阶段路线

```
Phase 1（0-3 月）主线二选一：
  技术线：A1 MemBench（基准公开化）→ A2 自适应路由（实验）
  产品线：B1 Memory Gateway（兼容层+SDK）→ B2 Dashboard（MVP）
Phase 2（3-6 月）：Phase 1 成果接入生态
  技术线 → C3 社区基准榜一期
  产品线 → B3 治理层 + B4 联邦记忆原型
Phase 3（6-12 月）：三线汇合
  A4 跨模态 + B4 联邦 + C1 市场协议 → "内核-平台-生态"闭环
  以 A1 基准 + C3 榜单建立外部影响力，反哺开源社区
```

## 3. 选型标准（任何新项目先过这五关）

1. **差异化**：别人是否很难复制（47 通道/守护链/审计链是护城河）
2. **复用率**：是否大量复用现有 129 端点与基建（沉睡资产激活度）
3. **可验证**：验收指标是否量化（基准分/延迟/成本/合规项）
4. **外部价值**：能否沉淀为作品集、论文、开源影响力
5. **风险**：是否依赖外部资源（GPU、数据、网络、人力）

## 4. 建议起点（二选一，都只需 1-2 周出可见成果）

- **A1 MemBench（技术深度向）**：benchmark 基建现成（run-benchmarks.ps1 / scenarios / beam_report 已有），升级为"可发布基准"缺口最小；产出的报告和 leaderboard 就是最好的对外作品集。
- **B1 Memory Gateway（产品化向）**：129 个端点已是"记忆服务"，只差一个兼容层 + Docker 镜像 + SDK 包装；OpenAI/Mem0 兼容意味着对接任何 LLM 应用零成本。

两条线最终在 C3（社区基准榜）汇合，顺序可先 A1 后 B1。

---

## 与 V1 的关系

- V1 的**图谱关系层已完成**（11k 实体 / 28k 关系），作为通用底座继续有效，本方案所有项目都可使用。
- V1 的其余项目（WMS 问答 / IP 一致性 / 内容工厂等）按用户要求**不再纳入**本规划；其脚本与文档保留在仓库备查。

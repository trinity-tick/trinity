# Trinity 长远规划（2026 H2 – 2029）

> 承接 `docs_site/optimization-plan.md`（P0-P2 短期）与官方 `ROADMAP.md`
> （v6.37-v6.40 + Future）。本文件定义 3 年愿景、4 个阶段、KPI 门控与风险对策。
> 每阶段以"决策门"推进，未达标不进入下一阶段。

---

## 一、愿景（三个地平线）

| 地平线 | 时间段 | 一句话定位 |
|---|---|---|
| **H1 个人记忆骨干** | 2026 H2 – 2027 H1 | "我的第二个大脑"：本地优先、基准可信、MCP-native 的 agent 记忆系统 |
| **H2 开放平台** | 2027 H2 – 2028 H1 | 开源 core + 插件生态 + 多智能体联邦记忆的行业参考实现 |
| **H3 记忆服务** | 2028 H2 – 2029 | 托管 SaaS + 边缘运行时 + 垂直行业记忆方案 |

长期北极星指标：**让"记忆"成为 agent 的一等公民 —— 检索准、忘得对、管得住、跨 agent 共享。**

---

## 二、定位（三条长期支柱）

1. **记忆科学深度（基准驱动）**：以 LongMemEval-S / LoCoMo 官方基准为标尺，长期维持
   排行榜第一梯队（前 5）；任何功能改动必须可被基准或真实场景评测量化。
2. **本地优先 + 隐私可控**：guardian 50 级门禁 + DCSA 审计 + 凭证保险库 → 端侧可离线、
   数据主权在用户（与 Mem0/托管 SaaS 形成差异化）。
3. **生态原生（MCP + A2A）**：MCP 标准接口为第一公民；A2A 联邦记忆成为多智能体协作的
   记忆骨干（对齐 Google A2A v0.3 + DSH/Hermes/Marvis 现实生态）。

---

## 三、阶段路线图（含决策门）

### Phase 1 — 基准可信与检索质量（2026 Q3-Q4）
*衔接 optimization-plan P0/P1。*
- 交付：答案生成评测 harness（DeepSeek）；PG FTS GIN 索引；MS 多会话短板修复（session-summary
  记忆 + 检索加权）；KG/47 通道逐类目归因调优（agentmemory 式"检索+KG+整合"）；真实 LLM
  decay 灰度；聚合池原子写自愈；Redis 缓存生产开启并量化。
- **KPI**：mock 500q 答案 accuracy ≥ 92%、R@5 ≥ 0.95、MS 类目 ≥ 0.8；官方基准 harness 就绪
  （网络恢复即跑 LongMemEval-S/LoCoMo 真集）。
- **决策门**：accuracy ≥ 92% → 进入 Phase 2；否则复盘检索/KG/评测链路。

### Phase 2 — 本地优先产品化 MVP（2027 H1）
- 交付：
  - 安装与体验：自托管一键安装（已有 Windows 服务/CLI 向导 → 补 macOS/Linux/systemd）；
  - Dashboard 2.0：记忆流时间线、知识图谱可视化、检索归因（为什么返回这条）、guardian 规则
    可视化配置、token/成本仪表盘；
  - 多智能体会话状态化（Letta 思路）：agent 级会话摘要/续接；
  - 插件 SDK 定型（已有 PluginRegistry → 稳定 API + 文档）；
  - 存储抽象可插拔（SQLite/PG/向量库 backend 接口化）。
- **KPI**：首次配置 < 10 分钟；p99 检索延迟 < 50ms @ 100 万条；插件 ≥ 20；
  3 个真实 agent（DSH + Hermes + 自建）连续运行 ≥ 30 天零人工干预；
  记忆规模 ≥ 100 万条索引化。
- **决策门**：30 天无人干预稳定 + 插件生态萌芽 → Phase 3。

### Phase 3 — 开放生态（2027 H2 – 2028 H1）
- 交付：
  - 开源策略：core 开源（Apache-2.0）+ 托管层收费；社区贡献流程（CONTRIBUTING/CI 已有基础）；
  - 官方基准入榜：LongMemEval-S / LoCoMo 真集分数写入 README（对齐官方口径，杜绝口径游戏）；
  - A2A 联邦记忆参考实现：跨进程 SSE/gRPC transport + registry 持久化 + CRDT 冲突合并
    （memory_write 已有 CRDT 版本链基础）；
  - 边缘与移动：WASM 运行时 + 移动 SDK（官方 ROADMAP Future 项）；
  - 外部集成：LangChain / CrewAI / OpenAI Agents SDK / DSH 适配器。
- **KPI**：官方 LongMemEval-S 前 5；插件 ≥ 100；外部贡献者 ≥ 10；集成适配器 ≥ 5；
  GitHub 社区活跃（issue 响应 < 48h）。
- **决策门**：官方榜单前 5 + 外部贡献者 ≥ 10 → Phase 4。

### Phase 4 — 记忆服务与垂直化（2028 H2 – 2029）
- 交付：
  - 托管 SaaS：租户隔离/按量计费/合规（基于已有 RBAC + /metrics + 审计）；企业控制台
    （官方 ROADMAP Future）；
  - 垂直方案：个人知识助理、客服/CRM agent 记忆、代码仓库记忆、医疗/金融合规记忆；
  - 分布式与联邦：跨节点同步（Raft 集群已有雏形）、跨组织联邦记忆（A2A 规模化）；
  - 记忆市场：插件/技能/记忆模板市场（官方 Future 的 Plugin System + 记忆市场雏形）。
- **KPI**：托管记忆规模 ≥ 1000 万条；边缘检索 p99 < 10ms；MRR 与 NPS 健康；
  至少 2 个垂直行业落地案例。

---

## 四、长期 KPI 面板（贯穿四阶段）

| 维度 | 2026 H2 | 2027 H1 | 2027 H2–2028 H1 | 2028 H2–2029 |
|---|---|---|---|---|
| LongMemEval-S 官方 | harness 就绪（网络恢复即跑） | 入榜前 10 | 前 5 | 前 3 |
| 记忆规模 | ~1 万条（当前 1.1 万） | 100 万（索引化） | 1000 万 | 1 亿（分布式） |
| 检索延迟 p99 | ~30ms @ 1.1 万 | <50ms @ 100 万 | <100ms @ 1000 万 | 边缘 <10ms |
| 插件生态 | 框架就绪（PluginRegistry） | ≥ 20 | ≥ 100 + 市场雏形 | 市场运营 |
| 多智能体 | 双 agent demo | 3 agent 联邦 30 天 | 联邦协议稳定 + 跨进程 | 跨组织联邦 |
| 社区/商业 | 单人维护 | 首个外部贡献者 | 贡献者 ≥ 10 | MRR 为正、NPS ≥ 40 |

---

## 五、四大支柱的长期投入方向

1. **记忆科学**：遗忘曲线参数化（已有 forgetting-curve 分析）→ 个性化衰减；整合
   （consolidation）质量评测；反思（reflection）闭环；知识图谱动态拓扑（论文方向：
   [All-Mem](https://ar5iv.labs.arxiv.org/html/2603.19595) /
   [MAGMA](https://ar5iv.labs.arxiv.org/html/2601.03236)）；多模态记忆（图像/音频，
   已有 cross-modal 模块）。
2. **平台**：MCP v2 跟进；A2A 标准化参与；插件 SDK 稳定；存储抽象（adapter 接口化 +
   连接池/缓存/镜像已具备 → 统一 backend 契约）。
3. **产品**：Dashboard 可视化与检索解释性（RAG 归因）；guardian 规则即产品（可视化门禁）；
   成本透明（token/嵌入/存储仪表盘）。
4. **基础设施**：本地优先离线可用；边缘 WASM；CRDT 联邦同步；多租户合规（审计链已有
   SHA-256 版本链 + audit_query）。

---

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| 基准过拟合 / 被超越 | 双基准（LongMemEval + LoCoMo）+ 真实用户评测；公布方法学而非只晒分数 |
| 模型/嵌入成本 | 小模型蒸馏（Exabase 思路）、本地嵌入、缓存优先（Redis 已接） |
| 巨头竞争（Mem0/Letta/Zep） | 差异化：本地优先 + 记忆科学深度 + 多智能体联邦；避免正面烧钱 |
| 网络依赖（GitHub/HF 不可达） | 数据集离线镜像 + 代理；评测 harness 独立于官方集可先行 |
| 单人维护瓶颈 | 自动化运维已具备（maintenance/supervisor/skill）→ 开源化吸引贡献者 → 商业化反哺 |
| 多智能体标准漂移 | 同时支持 A2A v0.3 + MCP v2；抽象层隔离协议细节 |

---

## 七、与官方 ROADMAP.md 的衔接

| 官方项 | 落实阶段 |
|---|---|
| v6.37 DX（async API/CLI 向导/类型提示） | Phase 1（CLI 已交付；async 检索 API 补） |
| v6.38 跨平台（Windows 服务/systemd/launchd/自更新） | Phase 2 |
| v6.39 生产加固（连接池/Redis 缓存/限流/审计/Prometheus） | **已大部分交付**（Phase 1 收官验证） |
| v6.40 多智能体（A2A/分布式同步/冲突解决/上下文交接） | Phase 2-3 |
| Future：SaaS/企业控制台/插件系统/K8s/边缘 WASM/移动 SDK/MCP v2 | Phase 3-4 |

---
*活文档：每完成一项更新 KPI 面板与状态；重大方向变更需回到本文档修订并记录于 EXECUTION.md。*

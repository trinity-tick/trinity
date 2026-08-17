# Trinity V2 主执行计划（EXECUTION PLAN）

> 依据 `FUTURE_ROADMAP_V2.md`（能力导向），把三条曲线 14 个项目拆成可执行任务。
> 状态图例：◻ 未开始 ｜ 🔄 进行中 ｜ ✅ 完成
> 依赖规则：先 A1 后 C3；B1 独立；所有项目共享图谱底座（已完成）。

---

## 曲线 A：记忆内核纵深

### A1 MemBench 公开评测基准 🔄（进行中，预计 1-2 周）
目标：现有 benchmark 基建升级为可发布评测套件（数据集+评分+报告+leaderboard）。

| # | 任务 | 状态 |
|---|---|---|
| A1.1 | 盘点现有套件可运行性（memsyco/latency/concurrency 无 key 可跑；longmemeval/locomo/squad 需 LLM key） | ✅ |
| A1.2 | 跑通无 key 套件基线（memsyco dry-run、latency、concurrency）并产出基线报告 | 🔄 |
| A1.3 | 统一结果格式：各套件输出归一化为 `{suite, metric, value}` 表格 | ◻ |
| A1.4 | 编写 MemBench 说明文档（数据集来源/评测方法/复现步骤） | ◻ |
| A1.5 | 接入 LLM 评测（配置 OPENAI_API_KEY/Ollama 后跑 longmemeval/squad 真值） | ◻ |
| A1.6 | leaderboard 页面（静态 HTML 或 dashboard 端点） | ◻ |
| 里程碑 | 输出 `membench_report.md`（可发布版本）+ 复现命令 | |

### A2 检索通道自适应路由（依赖 A1 结果）
- [ ] A2.1 用 A1 基准为每条 query 打标"最优通道组合"（融合/rrf/cascade × top_k）
- [ ] A2.2 轻量路由模型（特征：query 长度/领域/历史命中率 → 通道选择）
- [ ] A2.3 A/B 对比：路由 vs 固定融合，报告延迟/成本/质量变化
- 里程碑：同质量下 p95 延迟或成本降 30%+

### A3 长程一致性压力测试
- [ ] A3.1 构造 10 万 token 多会话测试语料（模拟长期 agent 使用）
- [ ] A3.2 用 identity/drift-check + GroundTruthEpisodes 跑一致性检测
- [ ] A3.3 输出压测报告（漂移案例 + 修复建议）

### A4 跨模态记忆闭环
- [ ] A4.1 确认 image-by-text / text-by-image / cross-modal 端点行为（实测 3 个端点）
- [ ] A4.2 补图像采集→记忆管线（复用 collector 插件机制）
- [ ] A4.3 跨模态检索 demo（图找文/文找图）

### A5 记忆压缩 Token 经济学
- [ ] A5.1 抽样记忆跑 /memory/compress，统计压缩率与信息损失
- [ ] A5.2 构建成本模型（token 节省 vs 质量损失曲线）
- [ ] A5.3 输出报告：何时该压缩、压缩阈值建议

---

## 曲线 B：开发者平台产品化

### B1 Memory Gateway 🔄（进行中，预计 1-2 周）
目标：OpenAI/Mem0 兼容层 + Docker 编排 + SDK，任何 LLM 应用 5 分钟接入记忆。

| # | 任务 | 状态 |
|---|---|---|
| B1.1 | 确认 Trinity 写入/检索 schema（POST /memories、/memory/search/hybrid） | ✅ |
| B1.2 | 编写兼容层 `gateway/server.py`（/v1/memories CRUD + search + chat/completions 记忆注入） | 🔄 |
| B1.3 | Python SDK `gateway/client.py`（add/search/chat 一行接入） | 🔄 |
| B1.4 | Docker 编排 `gateway/docker-compose.yml`（postgres + trinity-api + gateway）+ Dockerfile | 🔄 |
| B1.5 | 冒烟测试：OpenAI SDK 直连 gateway 增删查记忆 | ◻ |
| 里程碑 | 用 OpenAI SDK 5 分钟跑通 demo 脚本 | |

### B2 记忆可观测性 Dashboard
- [ ] B2.1 盘点 /dashboard、/evolution/heatmap、/metrics 端点返回结构
- [ ] B2.2 前端 MVP：热度热力图 + 衰减曲线 + 图谱子图可视化（graph/traverse 数据源）
- [ ] B2.3 部署到 docker-compose（dashboard 服务）

### B3 多智能体记忆治理层
- [ ] B3.1 定义治理策略 schema（YAML：隔离/共享/仲裁/审计 规则）
- [ ] B3.2 实现策略引擎（调用 a2a/identity/audit 端点执行规则）
- [ ] B3.3 三 agent 协作 demo + 策略热切换

### B4 联邦记忆
- [ ] B4.1 定义同步协议（增量/冲突/合并，复用 memory_versions 版本链）
- [ ] B4.2 双实例同步 demo（本机两端口 + 数据互检）
- [ ] B4.3 冲突仲裁策略（参考 /memories/conflicts/resolve）

### B5 私有化合规包
- [ ] B5.1 合规清单（个保法/GDPR：加密、导出、删除、审计）
- [ ] B5.2 逐项对现有端点（audit/export/identity bundles）过检并补缺口
- [ ] B5.3 输出合规手册 + 一键导出脚本

---

## 曲线 C：生态与社区

### C1 记忆市场协议标准化
- [ ] C1.1 梳理 market 11 端点能力边界（list/price/estimate/reputation/orderbook）
- [ ] C1.2 编写协议规范文档（第三方接入流程 + 示例）
- [ ] C1.3 三方 demo：卖家上架 → 买家估价下单 → 信誉累积

### C2 采集插件生态
- [ ] C2.1 定义 harvester 插件接口（源→结构化记忆）
- [ ] C2.2 插件注册/发现机制（复用 auto_discovery）
- [ ] C2.3 示例插件 3 个 + 插件市场页

### C3 社区基准榜（依赖 A1）
- [ ] C3.1 基准结果提交格式 + 校验（防作弊：固定数据集/固定参数）
- [ ] C3.2 leaderboard 页面上线（接入 A1.6）
- [ ] C3.3 邀请第三方跑分

### C4 文档/教程/案例体系
- [ ] C4.1 Quickstart（5 分钟接入，配合 B1 网关）
- [ ] C4.2 迁移指南（Mem0/OpenAI Assistants → Trinity）
- [ ] C4.3 最佳实践 + 案例集（A1 基准报告、B3 治理 demo 作为案例）

---

## 依赖关系与排期

```
Phase 1（0-3 月）:
  A1（进行中） ──┬──> A2 ──> A3（并行）
                 └──> C3（依赖 A1 完成后启动）
  B1（进行中） ──┬──> B2
                 └──> B3 ──> B4（并行）
Phase 2（3-6 月）:
  A4 / A5（可与 A2/A3 并行）
  B5（独立，随时可做）
  C1 / C2（独立，依赖现有端点）
Phase 3（6-12 月）:
  C4 文档体系贯穿全程
  三线汇合：A 系列报告 + B 系列产品 + C 系列生态
```

## 当前进度快照（2026-08-14 终版）

- ✅ **A1**：基线 + 归一化（membench_report.py）+ **3 个 API bug 修复**（get_memory BLOB 500 / hybrid content_preview / export 500）+ leaderboard 页
- ✅ **A2**：自适应路由实验（rrf 233ms/100% 最优；cascade/keyword 不可用 → 已写入最佳实践）
- ✅ **A3**：一致性压测工具（dry-run 默认，--write 真跑）
- ⚠️ **A4**：~~端点存在；cross-modal 触发视觉模型加载超时/拖垮进程~~ → **已收口**（14.1：HF 离线保护 + 端点降级响应，秒级返回不再挂起；全功能需本地 CLIP/句子编码器模型）
- ✅ **A5**：压缩经济学（实测 -21% token）
- ✅ **B1**：Memory Gateway v0.1 全套（server/client/Docker/compose/README），实测闭环
- ✅ **B2**：Dashboard MVP（dashboard/index.html）
- ✅ **B3**：治理层（policy.yaml + governance.py，demo 通过）
- ✅ **B4**：联邦记忆（sync_protocol.py export/diff 验证通过，7MB/10632 条）
- ✅ **B5**：合规包（checklist + audit.py，实测 8/8）
- ✅ **C1**：市场协议（protocol.md + demo.py 全流程跑通）
- ✅ **C2**：采集插件规范 + 示例（dry-run 通过）
- ✅ **C3**：leaderboard 页面（benchmark/leaderboard.html，提交格式已定）
- ✅ **C4**：文档（Quickstart / 迁移指南 / 最佳实践）
- ✅ **V3 收口**（17 轮）：
  - V3-2a：gateway 端到端外部接入 demo 跑通（OpenAI SDK + 记忆注入 + DeepSeek，1.6s 正确引用新记忆）
  - V3-2b：TS SDK 补齐 TrinityGatewayClient + 类型检查/构建 PASS（修复 3 处既有类型错误）
  - V3-3a：MEMBENCH_REPORT.md 发布版（全部真实数字 + 复现命令）
  - V3-3b：leaderboard 平台化（submissions + validate + build，校验 PASS 渲染成功）
  - 附带修复 4 个真实问题（去重约束迁移、FTS 自愈、OpenAI SDK 路径别名、网关引擎优先检索）
- 📌 剩余：全功能跨模态（环境性）、LongMemEval 真实集、TS SDK 运行时联调、多语言 SDK 发布（npm/pypi）
- ✅ **遗留项收口**（14 轮）：
  - LLM 真实评测：memsyco 真实模式已集成并跑通（DeepSeek，Composite=0.63）；SQuAD 检索 R@5=98.3%（180 题）；locomo 逐轮 0.12 → 会话聚合 0.88
  - A4 跨模态：离线保护 + 降级响应（不再挂起/崩溃）
- 📌 剩余（15 轮收口后）：
  - ✅ memsyco LLM judge（准确率 15%→85%，Composite 0.88）
  - ✅ C1 asset_id（内容哈希生成，实测 ast_4aaaaec3345d）
  - ✅ Docker 实机验证（镜像 249MB build + 容器 /health 冒烟通过）
  - ⛔ A4 全功能跨模态：环境性不可行（无 CLIP 缓存/HF 不通/库内无图片记忆），降级即终态，待网络/模型就绪

# Trinity 规划 V3（2026-08-15 重新规划）

> 依据：V2（2026-08-14，三曲线 15 项）完成度 60%（9✅/4🟡/3⏳）+ 本会话 round34-45
> 全部落地成果 + 网络最优方案对照。V3 = 从"补功能"转向"验证深度 + 平台化 + 生态"。

## 0. 现状基线（已达成，不再规划）

- 引擎：122 模块/50 守护/47 通道，ALL_PASS；检索 5 路 RRF + Redis 缓存 305x + ANN 落盘 + 自适应路由；MS 召回 0.992
- 治理：真实 LLM 整合/多因子遗忘/睡眠整合/实体去重/compaction 全入每日链
- 集成：REST/MCP v1+v2(streamable-http)/DSH 原生 6/6 融合/Gateway 生产化/联邦/市场/插件/GDPR
- 数据：11.8k 记忆/11.1k 实体/28.3k 关系；两库（SQLite 权威 + docker PG 镜像）；583 tests
- 外部约束：HF 网络不可达（官方基准阻塞）、无 GPU（本地 embedding）、LangChain 依赖未装

## 1. 三阶段路线

### Phase 1 — 证明性与可读性（1-2 周，全部可落地、低风险）

| # | 项 | 目标/验收 | 复用资产 |
|---|---|---|---|
| P1-1 | **A3 长程一致性压测** | GroundTruthEpisodes + IdentityPreservingConsolidator 跑 10M token 跨会话漂移压测，产出漂移报告（锚点一致性、身份 hash 稳定性） | identity/*、evolution/* |
| P1-2 | **A5 压缩经济学报告** | 采样压缩 token 节省 vs 信息损失曲线，产出可对外报告（--llm real + mock 对比） | run_decay_compress、MemoryCompressor |
| P1-3 | **C4 mkdocs 文档站** | 现有 30+ docs 打包成 mkdocs 站点（架构/基准/合规/运维/协议/API），本地可预览 | docs/* |
| P1-4 | **C3 榜单开放页** | LEADERBOARD.md → 静态 HTML 榜单页（含 500q/LoCoMo/BEAM 口径说明） | generate_leaderboard.py |

### Phase 2 — 平台化与差异化（1-2 月）

| # | 项 | 目标/验收 | 复用资产 |
|---|---|---|---|
| P2-1 | **B3 多智能体记忆治理层** | YAML 策略（隔离/共享/委托/审计规则）→ 治理引擎调用 a2a/identity/audit 执行；单 demo（2+ agent 协作 + 策略热切换） | a2a 19 端点、identity 14 端点、audit 11 端点 |
| P2-2 | **A4 跨模态闭环** | 音频/视频 → 记忆 → 跨模态检索评测（图找文/文找图 demo + 测试集） | cross-modal 3 端点、collector |
| P2-3 | **B5 存储加密** | memories 敏感字段可选加密（AES-GCM + 密钥管理 env） | store_memory 写路径 |
| P2-4 | **A1 官方基准** | HF 网络就绪后跑 LongMemEval/BEAM 官方口径，leaderboard 标注官方数字 | benchmark 套件 |

### Phase 3 — 长期（3-12 月，多为外部条件/业务决策）

| # | 项 | 说明 |
|---|---|---|
| P3-1 | SaaS / Console | Gateway 为基座的托管服务 + 租户管理（roadmap 原始远期） |
| P3-2 | 上下文工程深化 | KV 感知检索（IntentKV 式跨轮剪枝，对齐 2026 方案） |
| P3-3 | 记忆市场真实闭环 | 第三方买卖/估值/信誉真实场景（当前为功能+协议） |
| P3-4 | SDK 生态扩展 | LangChain/其他框架集成（需装 langchain-core）、更多语言 SDK |
| P3-5 | MCP v2 深化 | 工具集补 goal/schedule 操作；OAuth 认证 |

## 2. 选择标准（沿用 V2 五关，补充）

1. 差异化（47 通道/守护链/审计链护城河）
2. 复用率（现有端点/资产激活度）
3. 可验证（量化验收：基准分/延迟/合规项）
4. 外部价值（报告/文档/开源影响力）
5. 风险（外部资源依赖：GPU/数据/网络/人力）

## 3. 建议起点（Phase 1 全做，两周内出可见成果）

P1-1 压测报告 + P1-2 压缩曲线 + P1-4 榜单页 → 三者合成"对外可信度包"；
P1-3 文档站 → 可读性。Phase 2 从 B3 治理层或 A4 跨模态选一深耕。

## 4. 与 V2 差异

- 已完成的 9 项移出规划；V1/V2 的 WMS 资产导向项（问答/日报/IP 助手）作为可选项保留
  （依赖 WMS 数据场景，非核心路线）。
- 新增：MCP v2 深化、上下文工程深化（2026 最新方案对齐）、存储加密。
- 节奏：两周可见成果（Phase 1）→ 月度平台化（Phase 2）→ 长期产品化（Phase 3）。

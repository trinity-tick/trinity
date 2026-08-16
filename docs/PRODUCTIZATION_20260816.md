# Trinity 产品化方案：审计合规记忆层（2026-08-16）

> 依据 2026-08-16 价值评估结论：Trinity 的真正护城河是「开源治理合规栈 + 多智能体联邦」，
> 这正是 Mem0/Zep 用 SaaS 付费墙挡住的部分。产品化切口：**审计合规记忆层**——
> 对标 Zep Cloud 企业卖点（SOC2/HIPAA/审计）的开源替代，叠加 Trinity 独有的 50 级守护链。

## 一、定位（一页纸）

**一句话**：给 AI Agent 提供「可审计、可治理、可加密、可迁移」的长期记忆基础设施——
企业/出海场景下，Mem0 太重云、Zep 太贵、裸 RAG 不可审计，Trinity 是唯一同时具备
存储加密 + 审计签名链 + RBAC 治理 + GDPR 工具的开源记忆层。

**目标用户**：
1. 出海/合规敏感企业（GDPR/欧盟 AI Act、金融、医疗）——审计与数据驻留是第一诉求
2. 多智能体团队（A2A + 共享池 + 治理策略）
3. 中文场景团队（jieba 分词原生）

**不做**：不做 Agent 运行时（Letta 的地盘）、不做托管云（起步阶段）、不做记忆市场（远期）。

## 二、可交付形态（起步三件套）

| 形态 | 内容 | 状态 |
|---|---|---|
| **MCP Server**（pip install trinity-memory[mcp]） | stdio/SSE/HTTP v2 三模式、8+6 工具 | 运行中，发布待 PyPI |
| **OpenAI/Mem0 兼容 Gateway** | :8002，/v1/memories 等，鉴权/限流/指标 | 运行中（实测返回数据） |
| **审计合规包** | DCSA 审计查询 API、GDPR 导出/删除、RBAC、加密开关文档 | 已实现，缺面向客户的文档 |

## 三、差异化卖点（对客户的话术）

1. **审计链开箱即用**：每次写入 SHA-256 哈希 + Ed25519/x509 签名 + 审计日志可查（audit_query）——
   对标 Zep Cloud 的 SOC2 卖点，但**开源、数据不出域**。
2. **存储加密可选**：AES-256-GCM 内容列密文落盘，FTS/哈希链兼容。
3. **治理策略**：YAML 声明 isolated/shared/delegated，热切换，default-deny RBAC。
4. **记忆可迁移**：memory_portability 导入导出——不锁定厂商（对比 Mem0 云锁定）。
5. **50 级守护链**：注入/投毒防御，企业安全团队可审计。

## 四、发布前检查（承接 MCP_RELEASE_CHECKLIST）

- [x] CLI 入口（trinity / trinity-mcp / trinity-api）已声明
- [x] 三 transport 实测运行
- [x] Gateway /v1/memories 实测返回
- [ ] 官方基准数字（LongMemEval_S 500 题，运行中 -> 完成后替换 README 借引数字）
- [ ] README 诚实化（移除 Exabase/Hindsight 借引，改真实实测）
- [ ] git tag v8.5.0 + GitHub Release
- [ ] PyPI 发布（需凭证；不可行则先 GitHub Release + 安装文档）

## 五、后续（按优先级）

1. **官方基准落地** -> README/网站可引用真实数字（本周）
2. **真实 LLM 提取默认开**（TRINITY_LLM_EXTRACT=on 文档化；实测 ~4.5s/条 DeepSeek，异步可降）
3. **审计合规客户文档**（docs/compliance 面向客户版）
4. **社区**：GitHub README 重写 + 示例仓库 + 发布帖
5. **远期**：SaaS（联邦 + Gateway 已有底座）、记忆市场（TrustExchange 协议已通）

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| 无官方基准 -> 被质疑 | 本次 LongMemEval_S 500 题实测（进行中） |
| mock LLM 路径 | 真实 LLM 提取已验证（14 实体/11 关系，条均 4.5s） |
| 单维护者 | 先立文档与示例，社区化后开源治理 |
| 三库运维重 | 产品文档给 Docker Compose 一键栈（已有） |

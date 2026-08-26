# Trinity 闭环核查与新优化空间（2026-08-24 第四轮）

> 方法：本地全量自查（交付物/孤儿模块/开关/事件源/服务）+ 两路新网络调研
> （2026 Agent 记忆新趋势 / 大模型新能力映射）。结论分三部分：
> 已闭环确认 / 未闭环清单 / 新发现的优化空间（含与 Trinity 现状的差距实证）。

---

## 一、已闭环确认（无遗漏）

| 项 | 状态 | 证据 |
|---|---|---|
| 15 项交付物 | ✅ 全部就位 | AGENTS.md / EVAL_CREDIBILITY / MCP_ECOSYSTEM_GUIDE / verify_mcp_server / slim_pool / backfill×2 / export_agents_md / ppr_core / injection / llm-client / R7-R9+FULL 报告 |
| 孤儿模块 | ✅ 257 → 2 | audit_modules 实测（audit_trail / p1_preamble 剩余） |
| 默认开关 | ✅ 甄别完毕 | 9 个 off 中：3 证伪（评分校准）、2 成本合理（LLM 提取/卸载）、4 设计 opt-in（ROUTE_REASONER/PERSONA/STRENGTH/DEBUGOFFLOAD）——**无"应开未开"缺陷** |
| DSH 事件源 | ✅ 活跃 | dsh_events 最近 1 小时 42 条新事件（tool/result 8,289 等）；collector"0 高价值"是统计口径（只计 user-message/goal/error/persist） |
| 服务/数据/检索 | ✅ 全绿 | 7 端口在线、/health ok、检索 5 查询全命中 16-104ms |
| 写锁治理 | ✅ 已闭环 | WAL 4MB 正常波动、每日 checkpoint 入 all 链、只读降级生效 |

---

## 二、未闭环清单（真实遗留）

| # | 项 | 状态 | 阻塞/依赖 |
|---|---|---|---|
| 1 | **BEAM/LoCoMo 官方英文集** | ⏳ 未跑 | 网络阻塞（HF 超时/官方仓库 404）——外部依赖 |
| 2 | **MCP 生态上架**（Smithery/mcp.so） | 📋 材料已备 | 需外部账号注册动作（清单在 MCP_ECOSYSTEM_GUIDE.md） |
| 3 | **OTel gen_ai semconv 对齐** | ⏳ 未做 | 纯本地工作（1-2 天）；2026 现状 agent span 仍未完全标准化（OTel semconv#3575）——**可等标准稳定** |
| 4 | **多模态转述式打通** | ⏳ 待 harness | 2026 新证据：转述式在硬负样本图文检索受限，"frontier LLM 转述难匹敌原生多模态 embedding"——**策略需重新评估**（见三-4） |
| 5 | **2 个孤儿模块**（audit_trail/p1_preamble） | ⚠️ 待决 | 审计/前置模块，可归档或接入 |

---

## 三、新发现的优化空间（2026 网络面 → Trinity 现状差距实证）

### 🔴 P0 级（2 项，本地已实证差距）

| # | 优化 | 网络依据 | Trinity 现状（实测） |
|---|---|---|---|
| 1 | **Structured Outputs 固化记忆提取契约** | 2026 共识：JSON Schema 是记忆管线默认契约（Schema 校验+语义校验双层） | `proposition_extractor.py` **无 response_format/JSON Schema**——纯文本解析容错但脆弱 |
| 2 | **reasoning effort 分层**（fast/slow thinking） | 2026 标准参数：写入用 fast（低成本命题化）、检索/冲突消解用 slow | `llm/client.py` **无 reasoning_effort 透传**——无法实现成本分层 |

### 🟡 P1 级（3 项，方向性）

| # | 优化 | 网络依据 | Trinity 现状 |
|---|---|---|---|
| 3 | **审计回执/可证明记忆**（AgentPrizm 式） | 2026 新产品：证明 agent 记住了什么（audit receipts + validity windows）；可信记忆 = 非篡改 + origin-bound 可检查 | Trinity 有 SHA-256 审计链 + 版本链，但**缺"对外可证明"封装**（receipt API） |
| 4 | **原生多模态证据保留** | 2026 实证：转述式丢原图证据，硬负样本受限 | ImageEncoder 待打通；策略应"转述 + 原始证据双轨"而非纯转述 |
| 5 | **端到端任务指标** | 2026 共识：R@k 已停止区分系统质量，需任务效果+命中率+成本/写放大仪表盘 | Trinity 有 MemBench 基准 + /metrics，但**缺"记忆命中率/写放大"运行仪表盘**（复用 /metrics 可低成本补） |

### 🟢 已对齐（无需做）
- Context Graphs 化：Trinity 图谱（12k 实体/30k 关系）+ 时点查询 = 语境图雏形 ✅
- 记忆安全默认：投毒扫描（R8）+ 加密（R8）+ 审计链 ✅（2026 综述的预览验证/来源标注/冻结/遗忘清洗——Trinity 已有审计+归档+GDPR 工具）
- 协议化集成：MCP 三形态 + AGENTS.md ✅
- 长上下文共存观：Trinity 定位（外部记忆=持久真源）与 2026 "state-centric serving" 共识一致 ✅
- prompt caching：R7 前缀管理实测 84.58% 命中 ✅（2026 缓存定价支柱已兑现）

---

## 四、建议执行顺序

| 优先级 | 项 | 工作量 | 价值 |
|---|---|---|---|
| P0 | ① Structured Outputs 契约（提取/压缩/QA 接 response_format） | 1-2 天 | 提取稳定性 + 缓存前缀规范 |
| P0 | ② reasoning effort 分层（llm/client 透传 + 写 fast/读 slow 路由） | 0.5-1 天 | 成本分层（2026 标准做法） |
| P1 | ③ 记忆可观测仪表盘（命中率/写放大，/metrics 扩展） | 1 天 | 运行质量可见（对齐 2026 共识） |
| P1 | ④ 审计回执 API（receipt 封装） | 1-2 天 | 企业合规差异化（AgentPrizm 对齐） |
| P1 | ⑤ 多模态双轨策略（转述 + 原始证据，待 harness） | 待外部 | 规避转述局限 |
| 外部 | BEAM 官方集 / MCP 上架 | 外部依赖 | 可信度/获客 |

---

## 五、结论

**闭环面**：本地可闭环项已全部收敛（交付物/开关/孤儿/事件源/锁治理），
剩余 5 项未闭环中 3 项为外部依赖（网络/账号/harness），2 项为可做项
（OTel 对齐——建议等标准稳定后做）。

**新优化空间**：这轮网络调研映射出 **2 个 P0 差距（Structured Outputs /
reasoning effort）+ 3 个 P1 方向（可证明记忆/多模态双轨/运行指标）**，
其中 P0 两项是"2026 大模型能力 → 记忆系统"的标准映射，Trinity 尚未接入，
**这是四轮对比后首次出现的"模型能力侧"新差距**（前几轮都是存储/检索/治理侧）。

**一句话：本地闭环已收敛；剩余优化从"存储治理"转向"模型能力利用"——
Structured Outputs 固化提取契约 + reasoning effort 成本分层是最直接的
两个 P0 动作。**

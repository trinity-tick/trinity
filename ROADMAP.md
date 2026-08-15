# Roadmap

> 2026-08-14 一致性审计更新：**此前的路线图严重滞后于代码**——v6.37-v6.40 的
> 未勾选项大多已实现（源码/实测核验）。本版勾选实际状态，并补充 v8.x 已完成里程碑与新的 Future 项。

## 已发布里程碑（均已实现 ✅）

### v6.36 — Initial Release ✅
- [x] Three-tier memory architecture (Retrieval / Memory / Guardian)
- [x] 47 retrieval channels with progressive cascading
- [x] 50-level Guardian chain
- [x] Multi-modal support (text, image, audio)
- [x] MCP Server (stdio + SSE)
- [x] REST API + Web Dashboard
- [x] Multi-tenant isolation
- [x] Docker deployment

### v6.37 — Developer Experience ✅（源码核验：除 CLI autocomplete 外均已落地）
- [x] Improved error messages and debugging（结构化错误中间件）
- [x] Async API support（FastAPI async 端点）
- [x] Better type hints and IDE integration（py.typed）
- [x] Performance profiling tools（benchmark 套件：latency/concurrency/profiler）
- [ ] CLI autocomplete（待做）
- [ ] `trinity-config` CLI wizard for first-time setup（待做）

### v6.38 — Cross-Platform Enhancement ⚠️（部分）
- [x] Windows 原生支持（Windows service 脚本 / autostart 循环实测）
- [ ] macOS launchd integration（待做）
- [ ] Linux systemd service file（待做；Docker 可用作替代）
- [ ] Daemon auto-update mechanism（待做）

### v6.39 — Production Hardening ✅（源码核验全部落地）
- [x] Connection pooling for PostgreSQL adapter（pool 1-3 实测）
- [x] Redis caching layer（TRINITY_REDIS_URL / TRINITY_CACHE_* env 已配）
- [x] Rate limiting for API endpoints（rate_limit_middleware）
- [x] Audit logging system（DCSA 审计链 + timeline/integrity/violations）
- [x] Prometheus metrics export（/metrics + OpenTelemetry）

### v6.40 — Multi-Agent Collaboration ✅（源码核验全部落地）
- [x] Agent-to-agent (A2A) memory sharing protocol（19 端点，v0.3）
- [x] Distributed memory sync across nodes（federation 同步脚本实测 10,632 条导出）
- [x] Conflict resolution for concurrent writes（/memories/conflicts + 版本链）
- [x] Cross-agent context handoff（Marvis dispatch + AgentBridge）

### v6.93 → v8.2 — 架构演进里程碑 ✅（源码核验）
- [x] v6.93 AgentBrain / DecisionEngine
- [x] v6.94 Bridge / A2A 协议
- [x] v6.95 MemoryAggregator（共享聚合池 + RRF 融合）
- [x] v6.96 AutoDiscovery（agent 自动注册）
- [x] v8.0 多锚点身份 + DCSA-EJP 双循环审计 + A2A + Marvis
- [x] v8.2 MemoryCompressor + Ed25519/x509 签名 + OpenTelemetry
- [x] v8.3 主动记忆收集（collector）
- [x] v8.5 流式摄取

---

## Future（待做，按优先级）

### 近期（0-3 月）— 一致性治理与外部化
- [ ] **文档一致性轮**：README/CHANGELOG/功能表同步（2026-08-14 已更新 ROADMAP + 补全 CHANGELOG 至 v8.2）
- [ ] **发布真实评测报告**（MemBench：SQuAD R@5=98.3%、locomo 0.88、memsyco judge 0.88）+ 可复现命令
- [ ] **Memory Gateway 产品化**：OpenAI/Mem0 兼容层对外文档 + 端到端接入 demo（代码已就绪）
- [ ] **安全加固**：git 凭证复查 ✅（无明文 token）；生产 TLS / 存储加密；删除审计事件

### 中期（3-6 月）
- [ ] **SaaS API**：Hosted memory service with pay-as-you-go pricing（gateway 为基底）
- [ ] **Enterprise Console**：Web admin dashboard for tenant management（dashboard MVP 已就绪）
- [ ] **Plugin System**：Third-party adapter plugins（harvester 插件规范已就绪）
- [ ] **评测平台化**：MemBench 开放提交 + leaderboard 上线 + 防作弊校验
- [ ] **知识包市场**：10k+ 记忆脱敏整理上架（TrustExchange 已通）
- [ ] **A2A 联邦演练**：15 个已注册 agent 实跑协作流水线

### 远期（6-12 月）
- [ ] **Cloud Native**：Helm chart for Kubernetes deployment
- [ ] **Edge Runtime**：WASM-compatible client for edge devices
- [ ] **Mobile SDK**：iOS / Android memory client
- [ ] **Model Context Protocol v2**：Support for upcoming MCP spec changes
- [ ] **身份人设产品**：drift-check + anchors 对外能力（AI 角色一致性）
- [ ] **CLI autocomplete / config wizard**（v6.37 遗留）

---

*This roadmap is a living document. Priorities may shift based on community feedback and contributions.*

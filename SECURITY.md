# Security Policy (Trinity)

## 支持版本

| 版本 | 支持 |
|---|---|
| v8.x（当前） | ✅ 安全更新 |
| v7.x | 维护 |
| 更早 | 不维护 |

## 数据安全承诺（开源就绪 2026-08-27）

- **数据本地化**：运行时数据全部本机（`~/.trinity/`），无遥测上报（TRINITY_TELEMETRY_ENABLED 默认 off）；
- **静态加密**：记忆 content 列 AES-256-GCM 加密（密钥 `~/.trinity/secrets/storage.key`，丢失不可解密）；
- **可证明性**：每条记忆 SHA-256 + CRDT 版本链 + 审计链；`GET /audit/receipt/{id}` 可独立重算验证；
- **访问控制**：多租户隔离（persona/session/agent/tenant）+ 行级可见性（RBAC 角色规则）+ API Bearer 鉴权；
- **写入治理**：注入模式扫描（OWASP AG 类）自动归档隔离；测试写入隔离默认 on。

## 报告漏洞

- 请勿在公开 issue 中提交敏感数据/密钥；
- 报告包含：受影响版本、复现步骤、影响评估、建议修复；
- 处理承诺：48 小时内确认，72 小时内给出处置计划。

## 已知边界（不构成漏洞）

- 加密密钥本地保管，未提供密钥托管/恢复机制；
- 元数据列（category/source_uri/时间戳）明文存储（检索/治理必需）；
- LLM API 调用仅在显式配置后发生（检索判题/摘要）。

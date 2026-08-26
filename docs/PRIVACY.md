# Trinity 隐私与数据安全说明

> 开源就绪（价值兑现路径 2，2026-08-26）：明确 Trinity 的数据治理承诺，供评估/集成/开源参考。

## 1. 数据本地化

- 运行时数据**全部本地**：`~/.trinity/store/trinity_store.db`（SQLite）+ 派生资产
  （pagetree.json / knowledge_sources.json / goals.json / automation/）；
- 外部仅两种可选连接：①LLM API（检索判题/摘要，按需调用）；②PostgreSQL 维护镜像
  （:5430，同机 Docker）；**无任何遥测上报**（TRINITY_TELEMETRY_ENABLED 默认 off）。

## 2. 静态加密（出厂默认）

- `content` 列 **AES-256-GCM 加密**落盘（密钥 `~/.trinity/secrets/storage.key`）；
  `TRINITY_STORAGE_ENCRYPTION=off` 显式关闭（不推荐）；
- 元数据列（category/source_uri/时间戳）明文（检索/治理必需）。

## 3. 可证明性

- 每条记忆带 **SHA-256 哈希 + CRDT 版本链 + 审计链**（59k+ 条审计）；
- `GET /audit/receipt/{memory_id}`：返回当前哈希/版本链/审计链完整性——
  **验证者可独立重算 SHA-256 对账**（防篡改/可举证）。

## 4. 访问控制

- 多租户隔离（persona/session/agent/tenant 四级）；
- **行级可见性**（RBAC 角色规则，`TRINITY_VISIBILITY_<ROLE>`）——按角色过滤检索结果；
- API 可选 Bearer 鉴权（TRINITY_API_KEY）；MCP streamable-http 用 Bearer token；
- gateway :8002 独立 GATEWAY_API_KEY。

## 5. 写入治理

- 注入模式扫描（OWASP AG 类）——高危命中自动归档 + INJECTION_ISOLATED 审计；
- 测试写入隔离（TRINITY_ISOLATE_TEST_WRITES 默认 on）——压测/自动关联内容不污染检索面；
- 删除是软删除（status=deleted，审计保留）。

## 6. 备份与保留

- `trinity-backup.ps1`（WAL 安全备份，sqlite backup API）→ `~/.trinity/backups/` 保留 14 天；
- 审计链随库备份；无自动清理策略（decay 为软归档）。

## 7. 隐私承诺

1. 记忆内容**不离开本机**（除非用户显式配置 LLM API）；
2. 无遥测/无广告/无第三方数据共享；
3. 删除操作可证明（审计回执可核验删除）；
4. 加密密钥本地保管，丢失即不可解密（请纳入备份）。

---
*价值兑现路径 2 · 2026-08-26*

# Trinity 记忆合规手册（GDPR / 数据主权，2026-08-15）

> 适用：企业/出海场景下的 Trinity 记忆系统部署（REST :8001 / MCP / DSH 原生）。
> 依据：[GDPR for AI Agents (atlan 2026)](https://atlan.com/know/ai-agent/gdpr-compliance-for-ai-agents/)、
> [出海三层记忆隔离](https://www.freebuf.com/articles/database/491970.html)、
> [AI 记忆层合规陷阱](https://www.freebuf.com/articles/database/491970.html)。

## 1. 数据资产地图

| 存储 | 内容 | 敏感面 |
|---|---|---|
| SQLite 大库 `~/.trinity/store/trinity_store.db` | 记忆正文、标签、会话归属、实体关系、审计链 | 最高（正文可含 PII） |
| docker PG :5430（维护镜像） | SQLite active 子集镜像 | 中 |
| 原生 PG :5432 | 遗留实例（无服务使用） | 低 |
| aggregator_pool.json | 聚合池副本 | 中（与主库同源） |
| 审计链（audit_log / memory_versions） | 全部写操作的不可变记录 | 中（操作元数据，非正文） |

## 2. 个人数据权利（GDPR Art.15-21）落地

| 权利 | 实现 | 命令 |
|---|---|---|
| 访问权（导出） | `export_user_data(persona_id)` 生成 JSON 快照 | `python scripts/gdpr_export.py --persona <id>` |
| 删除权（被遗忘） | `forget_user(persona_id)` 软删 + 审计保留 | `python scripts/gdpr_export.py --persona <id> --delete` |
| 预览（执行前） | dry-run 统计影响面 | `python scripts/gdpr_export.py --persona <id> --dry-run` |
| 更正权 | `update_memory`（版本链保留旧版，conflict-preserving） | REST PUT /memories |
| 可携权 | 导出为 JSON（含 tags/category/metadata） | 见导出 |

## 3. 隔离与最小化

- 多租户：persona/tenant/agent 三级隔离（round35 已修复 hybrid 检索过滤失效漏洞——
  `get_memory_owners` 现已在 SQLite/PG 实现，agent/persona/tenant 过滤真实生效）。
- 最小化：`auto_redact_pii` 写入开关（SQLiteAdapter.store_memory）、
  `detect_pii` 检测；建议生产开启。
- 保留期限：`ttl_seconds` 列支持自动过期；decay/consolidation 定期归档。

## 4. 审计与可证明性

- DCSA 审计链：`write_audit_log` 记录全部写操作（含 search 明细），
  `memory_versions` 保留版本链——满足"可解释的记忆变更"。
- 完整性：Ed25519/x509 签名（v8.2）可对外证明记录未被篡改。
- 对账：audit_log ↔ memories 定期一致性校验（维护脚本 selftest 可扩展）。

## 5. 运维建议

1. 生产启用 `TRINITY_API_KEY`（鉴权）；对外暴露走 Gateway 统一入口。
2. 敏感正文存储加密（B5 已落地，见 docs/STORAGE_ENCRYPTION_20260815.md）：
   `TRINITY_STORAGE_ENCRYPTION=on` + 密钥文件 `~/.trinity/secrets/storage.key`（或
   `TRINITY_STORAGE_KEY` 环境变量）。AES-256-GCM 保护 memories.content /
   memory_versions.content 落盘；tokenized_content 保持明文供 FTS 检索（见加密文档
   的取舍说明）。仍建议库文件 ACL 仅管理员。
3. 定期跑 GDPR dry-run 台账（各 persona 数据量），删除时保留审计（软删符合 GDPR
   "合理期限"精神，注意按当地法规确认硬删需求——`forget_user` 后可在保留期后 purge）。
4. 三库定位（round34 厘清）：运行时权威=SQLite 大库；PG 仅维护/分析；无服务的遗留
   实例建议下线以减少攻击面。

## 6. 回滚

- 合规工具为只读/软删（dry-run 不写库；--delete 走软删+审计），
  误删可用 `entity_backup` / 版本链恢复；脚本本身无侵入。

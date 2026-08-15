# Trinity 私有化合规清单 (B5)

> 对照 个保法 / GDPR 常见要求，逐项核对 Trinity 现有能力与缺口。
> 运行 `compliance/audit.py` 自动生成实测报告。

## 一、数据主体权利

| # | 要求 | Trinity 能力 | 端点/工具 | 状态 |
|---|---|---|---|---|
| C1 | 数据导出（可携带权） | ✅ 记忆导出 / 身份包导出 | `GET /agents/memory/export`、`GET /identity/bundles/export` | ✅ |
| C2 | 数据删除（被遗忘权） | ✅ 单条删除 | `DELETE /memories/{id}` | ✅ |
| C3 | 数据修改 | ✅ 版本化更新（CRDT 审计链） | `memory_versions` / `PUT` 类更新 | ✅ |
| C4 | 知情权（处理说明） | ✅ 审计轨迹可查 | `GET /audit/timeline`、`GET /audit/summary` | ✅ |

## 二、安全与治理

| # | 要求 | Trinity 能力 | 端点/工具 | 状态 |
|---|---|---|---|---|
| S1 | 访问控制 | ✅ RBAC 中间件（X-Agent-ID/Role） | `trinity.api.rbac_middleware` | ✅ |
| S2 | 最小权限 | ✅ 预置角色 admin/operator/developer/viewer | RBAC 角色表 | ✅ |
| S3 | 审计日志 | ✅ DCSA 审计链 + 完整性校验 | `GET /audit/integrity`、`GET /audit/violations` | ✅ |
| S4 | 数据加密（传输） | ◻ 生产需 TLS（本地 HTTP 仅限内网） | 部署层 | ⚠️ 待部署配置 |
| S5 | 数据加密（存储） | ◻ SQLite/PG 明文存储，需落盘加密或托管加密盘 | 存储层 | ⚠️ 待加固 |
| S6 | 敏感信息自动脱敏 | ✅ 写入自动 PII 检测（pii_redacted_types） | `POST /memories` 响应字段 | ✅ |

## 三、自动化审计工具（compliance/audit.py）

```bash
python compliance/audit.py --api http://127.0.0.1:8001
# 输出:
#   - 各审计端点可用性
#   - 审计记录数 / 违规数 / 完整性状态
#   - 导出/删除能力冒烟
#   - 合规报告 compliance_report.json
```

## 缺口与加固建议（按优先级）

1. **P1 传输加密**：生产部署启用 HTTPS（反向代理 TLS 终结），本地保持内网访问
2. **P1 存储加密**：PG 表空间加密 或 全盘加密（BitLocker/LUKS）+ 密钥管理
3. **P2 数据留存策略**：接入 `/memories/age` + decay 的自动过期（TTL）策略化
4. **P2 删除审计**：DELETE 操作写入 audit 记录（当前只软删，无删除审计事件）
5. **P3 合规导出格式**：一键导出"全部个人数据"为通用格式（JSON/CSV 包）

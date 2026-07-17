# Multi-Tenant

Trinity 支持三层次的多租户隔离：

## 租户层级

| 层级 | 标识 | 作用域 |
|:-----|:----:|:-------|
| Persona | `persona_id` | 个人配置隔离 |
| Session | `session_id` | 会话级上下文隔离 |
| Tenant | `tenant_id` | 组织级数据隔离 |

## 用法

```python
from trinity import Trinity

# 指定租户
mem = Trinity(
    tenant_id="company_a",
    persona_id="user_123",
    session_id="session_abc"
)

data = mem.search("项目文档")
# 结果仅包含该租户/用户/会话的数据
```

## 数据库隔离

适配器层会自动附加租户条件：

- **SQLite**: 按 tenant_id 分表或分文件
- **PostgreSQL**: 按 tenant_id 行级过滤 + schema 隔离

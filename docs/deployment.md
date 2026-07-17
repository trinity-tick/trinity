# Deployment

## Docker 部署

### 前提条件

- Docker 和 Docker Compose v2+

### 一键启动

```bash
git clone https://github.com/trinity-tick/trinity.git
cd trinity
docker compose up -d
```

### 服务端口

| 服务 | 端口 |
|:-----|:----:|
| REST API | 8100 |
| MCP Server (SSE) | 8000 |

## 生产环境部署

### 使用 Docker Swarm / Kubernetes

参考 `docker/docker-compose.prod.yml` 配置多实例部署。

### PostgreSQL 多租户

```python
from trinity import Trinity

mem = Trinity(
    adapter_type="postgresql",
    config={
        "host": "localhost",
        "port": 5432,
        "database": "trinity",
        "user": "trinity",
        "password": "your_password",
    },
    tenant_id="tenant_001",
)
```

### 环境变量

| 变量 | 默认值 | 说明 |
|:-----|:------:|:-----|
| `TRINITY_CONFIG` | - | JSON 配置文件路径 |
| `TRINITY_LOG_LEVEL` | INFO | 日志级别 |
| `TRINITY_API_PORT` | 8100 | API 端口 |
| `TRINITY_DATA_DIR` | ./data | 数据目录 |

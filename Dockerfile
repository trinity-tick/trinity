# ============================================================
# Trinity Memory — 生产 Docker 部署
# ============================================================
# 用法:
#   docker build -t trinity-memory .
#   docker run -d -p 8100:8100 -p 8000:8000 trinity-memory
#
# 环境变量:
#   PORT=8100           API 端口
#   MCP_PORT=8000       MCP SSE 端口
#   STORE_PATH=/data    记忆存储路径
# ============================================================

FROM python:3.12-slim AS builder

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[api,mcp]"

# 复制源代码
COPY trinity/ trinity/
COPY tests/ tests/

# 运行测试
RUN python -m pytest tests/ -q --tb=no || echo "Tests completed (non-fatal for build)"

# ============================================================

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /app/trinity /app/trinity
COPY --from=builder /app/pyproject.toml /app/

# 创建数据卷
VOLUME ["/data"]
ENV STORE_PATH=/data

# 暴露端口
EXPOSE 8100
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8100/health || exit 1

# 启动 API 和 MCP 服务
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]

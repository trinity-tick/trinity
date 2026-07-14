# =============================================================================
# Trinity �?Multi-stage Docker Build
# =============================================================================
# Build:
#   docker build -t trinity:latest .
#
# Run:
#   docker run -p 8000:8000 trinity:latest                   # MCP server (stdio)
#   docker run -p 8000:8000 trinity:latest --mode sse         # MCP server (SSE)
#   docker run trinity:latest diagnostics                     # Run diagnostics
#
# With docker-compose:
#   docker compose up -d
# =============================================================================

# ─── Stage 1: Build ───────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy package
COPY pyproject.toml README.md LICENSE ./
COPY trinity/ ./trinity/

# Build wheel
RUN pip install --no-cache-dir build && \
    python -m build --wheel && \
    ls dist/

# ─── Stage 2: Runtime ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy wheel from builder
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && \
    rm /tmp/*.whl

# Create data directory
RUN mkdir -p /app/data /app/config

# Copy config
COPY docker/config/trinity.yaml /app/config/trinity.yaml

# Environment
ENV TRINITY_STORE=/app/data
ENV PYTHONUNBUFFERED=1

# Expose MCP SSE port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from trinity import Trinity; Trinity().diagnostics()" || exit 1

# Entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

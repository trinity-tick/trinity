# Deployment

This guide covers deploying Trinity in production environments using Docker, docker-compose, and manual configuration.

---

## Docker Deployment

### Quick Start with Docker Compose

The easiest way to run Trinity in production is using the provided `docker-compose.yml`:

```yaml
# docker-compose.yml
version: "3.9"

services:
  trinity-db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: trinity
      POSTGRES_USER: trinity
      POSTGRES_PASSWORD: ${TRINITY_DB_PASSWORD:-trinity}
    volumes:
      - trinity-data:/var/lib/postgresql/data
      - ./docker/init-db.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U trinity -d trinity"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    networks:
      - trinity-net

  trinity-server:
    image: agenticai/trinity:latest
    environment:
      TRINITY_BACKEND: postgresql
      TRINITY_DB_HOST: trinity-db
      TRINITY_DB_PORT: 5432
      TRINITY_DB_NAME: trinity
      TRINITY_DB_USER: trinity
      TRINITY_DB_PASSWORD: ${TRINITY_DB_PASSWORD:-trinity}
      TRINITY_EMBEDDING_MODEL: ${TRINITY_EMBEDDING_MODEL:-text-embedding-ada-002}
      TRINITY_POOL_SIZE: 20
      TRINITY_LOG_LEVEL: info
    ports:
      - "8000:8000"
    depends_on:
      trinity-db:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - trinity-net

volumes:
  trinity-data:

networks:
  trinity-net:
    driver: bridge
```

### Run the Stack

```bash
# Start all services
docker-compose up -d

# Check logs
docker-compose logs -f trinity-server

# Verify health
curl http://localhost:8000/health
```

### Environment File (.env)

Create a `.env` file to manage configuration:

```bash
# .env
TRINITY_DB_PASSWORD=your_secure_password_here
TRINITY_EMBEDDING_MODEL=text-embedding-3-small
TRINITY_LOG_LEVEL=debug
TRINITY_MULTIMODAL_ENABLED=true
```

---

## Production Configuration

### Database Configuration

```yaml
# trinity.yaml
backend: postgresql
database:
  host: trinity-db
  port: 5432
  name: trinity
  user: trinity
  password: ${TRINITY_DB_PASSWORD}
  pool_size: 25
  max_overflow: 10
  pool_timeout: 30
  pool_pre_ping: true
  ssl_mode: require
  ssl_ca_path: /etc/ssl/certs/ca-certificates.crt

multi_tenant:
  enabled: true
  rls_enforcement: strict

embedding:
  model: text-embedding-3-small
  dimensions: 1536
  batch_size: 50
  cache_size: 10000

retrieval:
  default_top_k: 10
  hybrid_search: true
  alpha: 0.7
  min_score: 0.2
  max_results: 100

server:
  host: 0.0.0.0
  port: 8000
  workers: 4
  max_request_size: 10485760  # 10 MB
  request_timeout: 60
  rate_limit: 1000  # requests per minute

logging:
  level: info
  format: json
  output: stdout
```

### Dockerfile (Custom Build)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Trinity
COPY pyproject.toml .
RUN pip install --no-cache-dir trinity-memory[postgres,multimodal]

# Copy configuration
COPY trinity.yaml .
COPY docker/entrypoint.sh .
RUN chmod +x entrypoint.sh

# Expose ports
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
```

---

## Scaling Considerations

### Vertical Scaling

| Resource | Recommendation | Notes |
|---|---|---|
| **CPU** | 4+ cores | Embedding generation is CPU-bound |
| **RAM** | 8 GB minimum | Memory-mapped vector index benefits from RAM |
| **Storage** | SSD required | HDD causes unacceptable latency for vector search |
| **Network** | 1 Gbps+ | Embedding model API calls and client traffic |

### Horizontal Scaling

For high-traffic deployments, scale the server layer horizontally:

```yaml
# docker-compose scaled version
version: "3.9"

services:
  trinity-db:
    # ... (same as above)

  trinity-server:
    image: agenticai/trinity:latest
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: "2"
          memory: 4G
    environment:
      # ... (same as above)
    depends_on:
      trinity-db:
        condition: service_healthy

  nginx:
    image: nginx:alpine
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
    ports:
      - "80:80"
    depends_on:
      - trinity-server
```

```nginx
# nginx.conf
upstream trinity_backend {
    least_conn;
    server trinity-server:8000;
    server trinity-server:8000;
    server trinity-server:8000;
}

server {
    listen 80;
    location / {
        proxy_pass http://trinity_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }
}
```

---

## Performance Tuning

### PostgreSQL Tuning

```ini
# postgresql.conf optimizations for Trinity
shared_buffers = 2GB                    # 25% of available RAM
effective_cache_size = 6GB              # 75% of available RAM
work_mem = 64MB                         # Per-operation memory
maintenance_work_mem = 512MB           # For VACUUM and CREATE INDEX
random_page_cost = 1.1                 # SSD optimization
effective_io_concurrency = 200         # SSD optimization
wal_buffers = 64MB
max_connections = 100                  # Adjust based on pool_size

# pgvector specific
ivfflat.probes = 10                    # Higher = more accurate, slower
```

### Connection Pooling with PgBouncer

```ini
# pgbouncer.ini
[databases]
trinity = host=trinity-db port=5432 dbname=trinity

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 200
default_pool_size = 25
reserve_pool_size = 10
reserve_pool_timeout = 5.0
server_idle_timeout = 600
```

---

## Security Hardening

### Network Security

1. **Internal Network Only** — Do not expose the database port publicly.
2. **TLS/SSL** — Encrypt all connections between clients and the server.
3. **API Keys** — Use API key authentication for REST endpoints.

```python
# Client-side SSL configuration
memory = Trinity(
    host="db.internal.example.com",
    ssl_mode="require",
    ssl_ca_path="/etc/ssl/certs/ca-certificates.crt"
)
```

### Authentication

```yaml
# Enable API key authentication
server:
  auth:
    enabled: true
    api_keys:
      - key: sk-prod-abc123
        tenant: acme-corp
      - key: sk-prod-def456
        tenant: other-inc
```

### Rate Limiting

Protect against abuse with rate limiting:

```yaml
server:
  rate_limiting:
    enabled: true
    strategy: sliding_window
    default_limit: 100  # requests per minute
    tenant_overrides:
      acme-corp: 1000  # Higher limit for premium tenants
```

---

## Monitoring & Observability

### Health Check Endpoint

```bash
curl http://localhost:8000/health

# Response
{
  "status": "healthy",
  "version": "1.2.0",
  "uptime_seconds": 86400,
  "database": {
    "connected": true,
    "pool_size": 10,
    "active_connections": 3
  },
  "embedding_model": {
    "model": "text-embedding-ada-002",
    "status": "available"
  }
}
```

### Metrics Endpoint

```bash
curl http://localhost:8000/metrics

# Prometheus-formatted metrics
# HELP trinity_memories_stored_total Total number of memories stored
# TYPE trinity_memories_stored_total counter
trinity_memories_stored_total{tenant="acme-corp"} 15234
# HELP trinity_retrieval_latency_seconds Retrieval latency
# TYPE trinity_retrieval_latency_seconds histogram
trinity_retrieval_latency_seconds_bucket{le="0.005"} 8923
trinity_retrieval_latency_seconds_bucket{le="0.01"} 14231
trinity_retrieval_latency_seconds_bucket{le="0.05"} 15210
trinity_retrieval_latency_seconds_bucket{le="+Inf"} 15234
```

### Logging Configuration

```yaml
logging:
  level: info
  format: json
  output: stdout
  fields:
    - timestamp
    - level
    - service
    - tenant_id
    - user_id
    - request_id
    - duration_ms
```

---

## Backup & Disaster Recovery

### Automated Backups

```bash
#!/bin/bash
# backup.sh — Run daily via cron

BACKUP_DIR="/backups/trinity"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Backup the database
PGPASSWORD=$TRINITY_DB_PASSWORD pg_dump \
    -h $TRINITY_DB_HOST \
    -U trinity \
    -d trinity \
    -F c \
    -f "$BACKUP_DIR/trinity_$TIMESTAMP.dump"

# Compress
gzip "$BACKUP_DIR/trinity_$TIMESTAMP.dump"

# Remove backups older than 30 days
find "$BACKUP_DIR" -name "*.dump.gz" -mtime +30 -delete
```

### Restoration

```bash
pg_restore -h $TRINITY_DB_HOST -U trinity -d trinity \
    --clean --if-exists \
    trinity_20260714_220000.dump
```

---

## Next Steps

- **[Benchmarks](benchmarks.md)** — Review performance characteristics for capacity planning.
- **[Contributing](contributing.md)** — Set up a development environment.

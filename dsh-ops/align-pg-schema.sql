-- ═══════════════════════════════════════════════════════════════════
-- align-pg-schema.sql — 对齐 memories 表到 PostgreSQLAdapter 期望的列集
-- 背景：部署的 memories 表是 SQLite 风格最小结构（memory_id/session_id 为
-- VARCHAR，无 agent_id 等列），而 trinity/adapters/postgresql.py 与
-- trinity/daemon/memory_compressor.py 的 INSERT/UPDATE 引用这些列，导致
-- 记忆写入/归档失败（column "agent_id" does not exist 等）。
-- 本脚本只 ADD 列（幂等 IF NOT EXISTS），不改类型、不删数据，可安全重放。
-- 注意：memory_id 保持 VARCHAR —— 库中有 handoff_* 等非 UUID 值（29 行），
-- 不能转换类型；代码侧已改为 schema 无关的 = %s 比较（见 postgresql.py）。
-- 执行：python dsh-ops\apply-pg-alignment.py
-- ═══════════════════════════════════════════════════════════════════

ALTER TABLE memories ADD COLUMN IF NOT EXISTS agent_id           VARCHAR(128);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS ttl_seconds        INTEGER;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at   TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS access_count       INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance_score   DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_hash       VARCHAR(64);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS conflict_group_id  VARCHAR(64);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_resolved        BOOLEAN DEFAULT FALSE;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS modality           VARCHAR(32) DEFAULT 'text';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS metadata           JSONB;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_uri         TEXT;

-- memory_versions.version_id 部署为 INTEGER，而代码写入 UUID 字符串
-- （store_memory/update_memory 的版本追踪），改为 VARCHAR(64) 兼容。
ALTER TABLE memory_versions ALTER COLUMN version_id TYPE VARCHAR(64) USING version_id::text;

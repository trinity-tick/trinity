"""
BEAM Scale Data Generator for Trinity
=====================================
Generates scaled test data for BEAM benchmark based on existing 29-memory category distribution.

Usage:
    python beam_data_generator.py --scale 1K
    python beam_data_generator.py --scale 10K
    python beam_data_generator.py --scale 100K
    python beam_data_generator.py --scale 1K --clean   # drop test data first
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

# ── PostgreSQL connection ─────────────────────────────────────────────
def get_pg_config():
    return {
        "host": os.environ.get("PGHOST", "127.0.0.1"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "dbname": os.environ.get("PGDATABASE", os.environ.get("PGDBNAME", "trinity")),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
    }

# ── Category distribution from existing 29 memories ──────────────────
CATEGORY_DIST = {
    "general": 13 / 29,
    "handoff": 8 / 29,
    "memory": 5 / 29,
    "evolution_state": 2 / 29,
    "skill_memory": 1 / 29,
}

# ── 10 Topic clusters for benchmark recall measurement ───────────────
# Each topic has: label, content templates, and 5 query variants
TOPICS = [
    {
        "tid": "T0",
        "name": "deep_learning",
        "category": "general",
        "importance_range": (0.7, 0.95),
        "templates": [
            "Deep learning model {name} achieves {metric}% accuracy on {dataset} benchmark. Architecture uses {layers}-layer transformer with {heads} attention heads and {dim}-dimensional embeddings.",
            "Training {name} requires {gpus} GPUs over {hours} hours. Key hyperparameters: learning rate {lr}, batch size {bs}, dropout {drop}. [topic:T0]",
            "Paper review: {name} ({year}) introduces {innovation} for {task}. Outperforms SOTA by {gain}% on {dataset}. Code available at github.com/trinity/{repo}.",
        ],
        "queries": [
            "deep learning model accuracy benchmark",
            "transformer architecture training setup",
            "paper review neural network innovation",
            "GPU training hyperparameters optimization",
            "state of the art deep learning results",
        ],
    },
    {
        "tid": "T1",
        "name": "wms_system",
        "category": "general",
        "importance_range": (0.75, 0.95),
        "templates": [
            "WMS module {module} implements {feature} with {algo} algorithm. Supports {scale} orders/day throughput. Integration with {partner} API completed in phase {phase}.",
            "Warehouse optimization: {module} reduces picking time by {pct}% using {method}. ROI estimated at {roi}x over {period} months. [topic:T1]",
            "Inventory management system upgrade: {module} now handles {sku_count} SKUs across {warehouse_count} warehouses with {accuracy}% accuracy.",
        ],
        "queries": [
            "WMS module warehouse optimization",
            "inventory management system upgrade",
            "warehouse picking optimization algorithm",
            "order throughput system integration",
            "SKU management multi-warehouse accuracy",
        ],
    },
    {
        "tid": "T2",
        "name": "database_optimization",
        "category": "general",
        "importance_range": (0.6, 0.85),
        "templates": [
            "PostgreSQL performance tuning: {technique} improves query latency by {improvement}%. Applied to {table} table with {row_count} rows. Index type: {index_type}.",
            "Database migration strategy: {source} to {target} completed in {duration}. {data_size} data transferred with {error_count} errors. [topic:T2]",
            "Query optimization case study: rewritten {query_type} query reduces execution time from {before_ms}ms to {after_ms}ms using {method}.",
        ],
        "queries": [
            "PostgreSQL performance tuning query latency",
            "database migration strategy data transfer",
            "query optimization execution time reduction",
            "index optimization large table performance",
            "database tuning techniques throughput improvement",
        ],
    },
    {
        "tid": "T3",
        "name": "agent_handoff",
        "category": "handoff",
        "importance_range": (0.5, 0.9),
        "templates": [
            "Handoff from {from_agent} to {to_agent}: task '{task_desc}' transferred at {timestamp}. Status: {status}. Context preserved: {context_keys}.",
            "Agent collaboration record: {from_agent} completed {completed_steps} steps, pending {pending_steps} steps handed to {to_agent}. [topic:T3]",
            "Cross-agent task routing: query '{query}' routed from {from_agent} → {to_agent} based on capability match score {score}. Result: {result}.",
        ],
        "queries": [
            "agent handoff task transfer status",
            "agent collaboration completed pending steps",
            "cross agent task routing capability match",
            "handoff context preserved between agents",
            "agent transfer status query routing",
        ],
    },
    {
        "tid": "T4",
        "name": "memory_consolidation",
        "category": "handoff",
        "importance_range": (0.5, 0.85),
        "templates": [
            "Memory consolidation cycle {cycle}: processed {count} memories, merged {merged} duplicates, archived {archived} stale. Duration: {duration}s.",
            "Session boundary detected: summarizing {turn_count} turns into {summary_count} consolidated memories. Key themes: {themes}. [topic:T4]",
            "Forgetting curve analysis: {retention}% retention at {days} days for {category} memories. Recommended review interval: {interval} days.",
        ],
        "queries": [
            "memory consolidation cycle processed merged",
            "session boundary summarizing consolidated memories",
            "forgetting curve retention analysis review",
            "memory merge deduplication cycle duration",
            "session summary key themes consolidation",
        ],
    },
    {
        "tid": "T5",
        "name": "model_serving",
        "category": "general",
        "importance_range": (0.7, 0.9),
        "templates": [
            "Model serving benchmark: {model_name} on {hardware} achieves {tokens_per_sec} tokens/sec with batch size {batch}. Latency P99: {p99}ms. Memory: {mem}GB.",
            "LLM inference optimization: switched from {old_backend} to {new_backend}, throughput improved by {gain}%. Cost per 1M tokens: ${cost}. [topic:T5]",
            "Embedding model comparison: {model_a} vs {model_b} on {task}. {model_a} scores {score_a}, {model_b} scores {score_b}. Winner: {winner}.",
        ],
        "queries": [
            "model serving benchmark tokens per second",
            "LLM inference optimization throughput improvement",
            "embedding model comparison benchmark scores",
            "inference latency P99 batch size memory",
            "model serving cost per million tokens",
        ],
    },
    {
        "tid": "T6",
        "name": "personal_preferences",
        "category": "memory",
        "importance_range": (0.3, 0.7),
        "templates": [
            "User prefers {preference} when working with {context}. Noted on {date}. Confidence: {confidence}.",
            "Configuration preference: {setting} set to {value} for {reason}. User confirmed on {date}. [topic:T6]",
            "Workflow preference recorded: always use {tool} for {task_type}, avoid {alternative}. Productivity impact: {impact}.",
        ],
        "queries": [
            "user preference configuration setting",
            "workflow preference tool task type",
            "configuration preference confirmed setting",
            "user working preference context confidence",
            "productivity impact workflow tool preference",
        ],
    },
    {
        "tid": "T7",
        "name": "evolution_tracking",
        "category": "evolution_state",
        "importance_range": (0.75, 0.95),
        "templates": [
            "Evolution cycle {cycle_id}: observed {obs_count} patterns, analyzed {analysis_count}, planned {plan_count} actions, executed {exec_count}, certified {cert_count}. Score delta: {delta}.",
            "Trinity Evolution State: Phase {phase}, Active Strategies: {strategies}. Pattern library size: {pattern_count}. [topic:T7]",
            "Self-improvement log: strategy {strategy} applied, before metric {before}, after metric {after}, improvement {improvement}%.",
        ],
        "queries": [
            "evolution cycle observed analyzed planned executed",
            "Trinity evolution state phase active strategies",
            "self improvement log strategy before after metric",
            "evolution pattern library size score delta",
            "evolution cycle certified improvement percentage",
        ],
    },
    {
        "tid": "T8",
        "name": "skill_definitions",
        "category": "skill_memory",
        "importance_range": (0.5, 0.8),
        "templates": [
            "Skill '{skill_name}': {description}. Triggers: {triggers}. Tools required: {tools}. Success rate: {success_rate}%.",
            "Skill registry entry: id={skill_id}, version={version}, dependencies={deps}. Activation cost estimate: {cost} tokens. [topic:T8]",
            "Skill evaluation: {skill_name} tested on {test_count} cases, accuracy {accuracy}%, avg latency {latency}ms. Status: {status}.",
        ],
        "queries": [
            "skill definition triggers tools success rate",
            "skill registry version dependencies activation cost",
            "skill evaluation test cases accuracy latency",
            "skill description tools required success rate",
            "skill registry entry activation cost estimate",
        ],
    },
    {
        "tid": "T9",
        "name": "safety_audit",
        "category": "memory",
        "importance_range": (0.6, 0.9),
        "templates": [
            "Safety audit log: {check_name} passed at {timestamp}. Risk level: {risk_level}. Action taken: {action}. Auditor: {auditor}.",
            "Guardian chain check L{level}: {rule_name} evaluated on {content_len} chars. Result: {verdict}. Confidence: {confidence}%. [topic:T9]",
            "Security incident report: type={incident_type}, severity={severity}, detected_by={detector}, resolved_in={resolution_time}s.",
        ],
        "queries": [
            "safety audit log risk level action taken",
            "guardian chain check rule evaluated verdict",
            "security incident report type severity detected",
            "audit log check passed risk auditor",
            "guardian chain level rule confidence verdict",
        ],
    },
]

# ── Filler words for natural variation ───────────────────────────────
MODEL_NAMES = ["BERT-Large", "GPT-NeoX-20B", "Llama-3-70B", "Mistral-7B", "Gemma-27B",
               "Phi-4", "DeepSeek-V3", "Qwen2.5-72B", "Claude-4", "Gemini-2.5"]
DATASETS = ["ImageNet-1K", "CIFAR-100", "SQuAD-v2", "MMLU", "HumanEval",
            "GLUE", "SuperGLUE", "WikiText-103", "CommonCrawl", "The Pile"]
ALGOS = ["genetic algorithm", "simulated annealing", "gradient boosting", "reinforcement learning",
         "random forest", "XGBoost", "DBSCAN", "k-means++", "LSTM", "transformer"]
METHODS = ["beam search", "top-p sampling", "speculative decoding", "KV-cache optimization",
           "flash attention", "paged attention", "continuous batching", "dynamic batching"]
GPU_TYPES = ["A100-80GB", "H100-80GB", "RTX 4090", "A6000", "L40S", "MI300X", "TPU-v5p", "H200"]
STATUSES = ["completed", "in_progress", "pending_review", "blocked", "verified"]
SKILL_NAMES = ["code-review", "doc-generator", "image-processor", "data-analyzer",
               "web-scraper", "email-composer", "translation-engine", "summarizer"]

TEMPLATE_VARS = {
    "name": lambda: random.choice(MODEL_NAMES),
    "metric": lambda: f"{random.uniform(70, 99):.1f}",
    "dataset": lambda: random.choice(DATASETS),
    "layers": lambda: random.choice([12, 24, 32, 48, 64, 96]),
    "heads": lambda: random.choice([8, 16, 32, 64]),
    "dim": lambda: random.choice([512, 768, 1024, 2048, 4096]),
    "gpus": lambda: random.choice([4, 8, 16, 32, 64, 128]),
    "hours": lambda: random.choice([12, 24, 48, 72, 96, 168]),
    "lr": lambda: f"{random.choice([1e-4, 3e-4, 1e-5, 5e-5, 2e-5]):.0e}",
    "bs": lambda: random.choice([16, 32, 64, 128, 256, 512]),
    "drop": lambda: f"{random.uniform(0.05, 0.3):.2f}",
    "year": lambda: random.choice([2023, 2024, 2025, 2026]),
    "innovation": lambda: random.choice(["flash attention", "MoE routing", "KV-cache compression",
                                          "speculative decoding", "low-rank adaptation", "quantization-aware training"]),
    "task": lambda: random.choice(["text classification", "code generation", "image captioning",
                                    "sentiment analysis", "entity extraction", "question answering"]),
    "gain": lambda: f"{random.uniform(1.5, 8.0):.1f}",
    "repo": lambda: f"model-{random.randint(100,999)}",
    "module": lambda: random.choice(["inventory-sync", "order-router", "pick-pack", "wave-planner",
                                      "dock-scheduler", "replenishment", "cycle-count", "cross-dock"]),
    "feature": lambda: random.choice(["multi-warehouse allocation", "real-time tracking", "batch picking",
                                       "zone routing", "dynamic slotting", "quality inspection"]),
    "algo": lambda: random.choice(ALGOS),
    "scale": lambda: f"{random.choice([1, 5, 10, 50, 100])}K",
    "partner": lambda: random.choice(["SF-Express", "JD-Logistics", "Cainiao", "DHL", "FedEx", "UPS"]),
    "phase": lambda: f"M{random.randint(1,14)}",
    "pct": lambda: random.randint(15, 60),
    "method": lambda: random.choice(METHODS),
    "roi": lambda: f"{random.uniform(1.5, 8.0):.1f}",
    "period": lambda: random.choice([3, 6, 12, 18, 24]),
    "sku_count": lambda: f"{random.choice([10, 50, 100, 500, 1000])}K",
    "warehouse_count": lambda: random.choice([3, 5, 8, 12, 20]),
    "accuracy": lambda: f"{random.uniform(95, 99.99):.2f}",
    "technique": lambda: random.choice(["BRIN index", "partition pruning", "parallel vacuum",
                                         "JIT compilation", "incremental sort", "hash join"]),
    "improvement": lambda: random.randint(20, 80),
    "table": lambda: random.choice(["memories", "sessions", "audit_log", "memory_versions", "tenants"]),
    "row_count": lambda: f"{random.choice([1, 10, 50, 100, 500])}M",
    "index_type": lambda: random.choice(["GIN", "GiST", "BRIN", "B-tree", "Hash"]),
    "source": lambda: random.choice(["SQLite", "MySQL", "MongoDB", "Redis", "Elasticsearch"]),
    "target": lambda: random.choice(["PostgreSQL", "TimescaleDB", "CockroachDB", "YugabyteDB"]),
    "duration": lambda: f"{random.randint(10, 300)}min",
    "data_size": lambda: f"{random.choice([1, 5, 10, 50, 100])}GB",
    "error_count": lambda: random.randint(0, 5),
    "query_type": lambda: random.choice(["recursive CTE", "window function", "lateral join",
                                          "aggregate", "subquery", "full-text search"]),
    "before_ms": lambda: random.choice([500, 1200, 3000, 8000, 15000]),
    "after_ms": lambda: random.choice([5, 20, 50, 100, 300]),
    "from_agent": lambda: random.choice(["file-agent", "browser", "app-agent", "computer-agent",
                                          "search-agent", "main"]),
    "to_agent": lambda: random.choice(["file-agent", "browser", "app-agent", "computer-agent",
                                        "search-agent", "main"]),
    "task_desc": lambda: random.choice(["file search completed", "web scraping done", "app installed",
                                         "system configured", "deep research finished", "image processed"]),
    "timestamp": lambda: (datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 720))).isoformat(),
    "status": lambda: random.choice(STATUSES),
    "context_keys": lambda: ",".join(random.sample(["query", "results", "auth", "session", "config", "cache"], k=random.randint(2,4))),
    "completed_steps": lambda: random.randint(1, 10),
    "pending_steps": lambda: random.randint(1, 5),
    "query": lambda: f"user query about {random.choice(['finance', 'tech', 'health', 'travel', 'education'])}",
    "score": lambda: f"{random.uniform(0.6, 1.0):.2f}",
    "result": lambda: random.choice(["success", "partial", "rerouted", "escalated"]),
    "cycle": lambda: random.randint(1, 100),
    "count": lambda: random.randint(50, 5000),
    "merged": lambda: random.randint(0, 50),
    "archived": lambda: random.randint(0, 20),
    "turn_count": lambda: random.randint(5, 50),
    "summary_count": lambda: random.randint(1, 8),
    "themes": lambda: ", ".join(random.sample(["coding", "debugging", "planning", "review", "deployment", "monitoring"], k=random.randint(2,4))),
    "retention": lambda: random.randint(30, 95),
    "days": lambda: random.choice([1, 3, 7, 14, 30, 90]),
    "category": lambda: random.choice(["general", "handoff", "memory", "skill_memory", "evolution_state"]),
    "interval": lambda: random.choice([1, 3, 7, 14, 30]),
    "model_name": lambda: random.choice(MODEL_NAMES),
    "hardware": lambda: random.choice(GPU_TYPES),
    "tokens_per_sec": lambda: random.choice([50, 120, 300, 800, 1500, 3000]),
    "batch": lambda: random.choice([1, 4, 8, 16, 32]),
    "p99": lambda: random.choice([50, 100, 200, 500, 1000]),
    "mem": lambda: random.choice([8, 16, 32, 48, 80]),
    "old_backend": lambda: random.choice(["vLLM", "TGI", "TensorRT-LLM", "llama.cpp"]),
    "new_backend": lambda: random.choice(["SGLang", "LightLLM", "MII", "DeepSpeed-MII"]),
    "cost": lambda: f"{random.uniform(0.05, 2.0):.2f}",
    "model_a": lambda: random.choice(["bge-m3", "text-embedding-3-large", "e5-mistral-7b", "gte-large"]),
    "model_b": lambda: random.choice(["bge-large", "text-embedding-3-small", "all-MiniLM-L6", "gte-base"]),
    "score_a": lambda: f"{random.uniform(75, 95):.1f}",
    "score_b": lambda: f"{random.uniform(65, 90):.1f}",
    "winner": lambda: random.choice(["A", "B", "tie"]),
    "preference": lambda: random.choice(["dark mode", "tab indentation", "vim keybindings",
                                          "split view", "minimal UI", "verbose logging"]),
    "context": lambda: random.choice(["coding", "writing", "debugging", "data analysis", "system admin"]),
    "date": lambda: (datetime.now() - timedelta(days=random.randint(1, 180))).strftime("%Y-%m-%d"),
    "confidence": lambda: f"{random.uniform(0.6, 1.0):.2f}",
    "setting": lambda: random.choice(["editor.fontSize", "terminal.shell", "theme.accent",
                                       "autosave.interval", "notifications.level"]),
    "value": lambda: random.choice(["14", "bash", "#569CD6", "30s", "errors-only"]),
    "reason": lambda: random.choice(["readability", "performance", "consistency", "compatibility"]),
    "tool": lambda: random.choice(["PyCharm", "VS Code", "Neovim", "Jupyter", "Warp"]),
    "task_type": lambda: random.choice(["code review", "data viz", "refactoring", "scripting"]),
    "alternative": lambda: random.choice(["Eclipse", "Sublime", "Notepad++", "Spyder"]),
    "impact": lambda: random.choice(["+30% speed", "fewer errors", "better focus", "+25% output"]),
    "cycle_id": lambda: f"evo_{uuid.uuid4().hex[:12]}",
    "obs_count": lambda: random.randint(10, 200),
    "analysis_count": lambda: random.randint(5, 50),
    "plan_count": lambda: random.randint(3, 20),
    "exec_count": lambda: random.randint(2, 15),
    "cert_count": lambda: random.randint(1, 10),
    "delta": lambda: f"+{random.uniform(0.01, 0.15):.3f}",
    "strategies": lambda: ", ".join(random.sample(["memory_compaction", "retrieval_boost", "guardian_tune",
                                                     "channel_rebalance", "importance_calibration"], k=random.randint(1,3))),
    "pattern_count": lambda: random.randint(50, 500),
    "strategy": lambda: random.choice(["memory_compaction", "retrieval_boost", "guardian_tune"]),
    "before": lambda: f"{random.uniform(0.5, 0.9):.3f}",
    "after": lambda: f"{random.uniform(0.7, 0.99):.3f}",
    "skill_name": lambda: random.choice(SKILL_NAMES),
    "description": lambda: f"Automated {random.choice(['review', 'generation', 'processing', 'analysis', 'extraction'])} for {random.choice(['code', 'docs', 'images', 'data', 'audio'])}",
    "triggers": lambda: ", ".join(random.sample(["user request", "scheduled", "event-driven", "webhook", "API call"], k=random.randint(1,3))),
    "tools": lambda: ", ".join(random.sample(["Python", "FFmpeg", "Pillow", "BeautifulSoup", "PyPDF2", "OpenCV"], k=random.randint(2,4))),
    "success_rate": lambda: random.randint(85, 100),
    "skill_id": lambda: f"sk_{uuid.uuid4().hex[:8]}",
    "version": lambda: f"v{random.randint(1,5)}.{random.randint(0,9)}.{random.randint(0,20)}",
    "deps": lambda: ", ".join(random.sample(["numpy", "pandas", "pillow", "requests", "pydantic", "rich"], k=random.randint(1,4))),
    "cost": lambda: random.randint(100, 5000),
    "test_count": lambda: random.choice([50, 100, 200, 500, 1000]),
    "latency": lambda: random.choice([50, 100, 200, 500, 1000, 2000]),
    "check_name": lambda: random.choice(["content_safety", "pii_detection", "prompt_injection",
                                          "output_validation", "rate_limit", "auth_verification"]),
    "risk_level": lambda: random.choice(["low", "medium", "high", "critical"]),
    "action": lambda: random.choice(["blocked", "flagged", "allowed", "logged", "quarantined"]),
    "auditor": lambda: random.choice(["guardian-L7", "guardian-L15", "guardian-L23", "guardian-L42"]),
    "level": lambda: random.randint(1, 50),
    "rule_name": lambda: random.choice(["sanitize_input", "check_entropy", "verify_signature",
                                         "limit_tokens", "scan_patterns", "validate_json"]),
    "content_len": lambda: random.choice([100, 500, 2000, 8000, 32000]),
    "verdict": lambda: random.choice(["pass", "fail", "review", "bypass"]),
    "incident_type": lambda: random.choice(["prompt_injection", "data_leak", "dos_attempt",
                                             "privilege_escalation", "model_extraction"]),
    "severity": lambda: random.choice(["P0", "P1", "P2", "P3"]),
    "detector": lambda: random.choice(["guardian-L12", "guardian-L28", "guardian-L35", "audit-daemon"]),
    "resolution_time": lambda: f"{random.uniform(0.05, 30):.1f}",
}


def generate_content(template, topic_id):
    """Generate content by filling template variables."""
    import re
    result = template
    # Replace {var_name} patterns
    for key, func in TEMPLATE_VARS.items():
        pattern = "{" + key + "}"
        if pattern in result:
            result = result.replace(pattern, str(func()))
    # Ensure topic marker is present
    if f"[topic:{topic_id}]" not in result:
        result += f" [topic:{topic_id}]"
    return result


def generate_memory(topic):
    """Generate a single memory entry."""
    template = random.choice(topic["templates"])
    content = generate_content(template, topic["tid"])
    importance = round(random.uniform(*topic["importance_range"]), 2)
    now = datetime.now(timezone.utc) - timedelta(days=random.randint(0, 365), hours=random.randint(0, 23))
    memory_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    return {
        "memory_id": memory_id,
        "session_id": session_id,
        "persona_id": "default",
        "tenant_id": "default",
        "content": content,
        "role": random.choice(["user", "system", "assistant"]),
        "importance": importance,
        "tags": random.sample(["benchmark", "test", "BEAM", topic["name"], "scale_test"], k=min(3, random.randint(1,3))),
        "category": topic["category"],
        "sha256_hash": hashlib.sha256(content.encode()).hexdigest(),
        "status": "active",
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }


def connect_pg():
    """Connect to PostgreSQL."""
    cfg = get_pg_config()
    import psycopg2
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
    )
    return conn


def ensure_benchmark_table(conn):
    """Ensure benchmark marker table exists to distinguish test data."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS benchmark_meta (
                run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                scale VARCHAR(16),
                total_count INT,
                generated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()


def clean_test_data(conn):
    """Remove previously generated benchmark test data."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE tags::text LIKE '%BEAM%'")
        deleted = cur.rowcount
        conn.commit()
    print(f"  Cleaned {deleted} test memories")


def generate_data(scale: int, conn):
    """Generate data for a given scale."""
    total = scale

    # Calculate per-topic counts
    # Each topic gets roughly equal share
    per_topic_base = total // len(TOPICS)
    remainder = total % len(TOPICS)

    topic_counts = {}
    for i, topic in enumerate(TOPICS):
        topic_counts[topic["tid"]] = per_topic_base + (1 if i < remainder else 0)

    print(f"  Target: {total} memories across {len(TOPICS)} topics")
    for topic in TOPICS:
        print(f"    {topic['tid']} ({topic['name']}): {topic_counts[topic['tid']]} memories")

    generated = 0
    batch = []
    batch_size = 500
    start_time = time.time()

    for topic in TOPICS:
        count = topic_counts[topic["tid"]]
        for i in range(count):
            mem = generate_memory(topic)
            batch.append(mem)
            generated += 1

            if len(batch) >= batch_size:
                _flush_batch(conn, batch)
                elapsed = time.time() - start_time
                rate = generated / elapsed if elapsed > 0 else 0
                pct = generated / total * 100
                print(f"  Progress: {generated}/{total} ({pct:.1f}%) | {rate:.0f} rec/s", end="\r")
                batch = []

    if batch:
        _flush_batch(conn, batch)

    elapsed = time.time() - start_time
    print(f"\n  Completed: {generated} memories in {elapsed:.1f}s ({generated/elapsed:.0f} rec/s)")

    # Record benchmark run
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO benchmark_meta (scale, total_count) VALUES (%s, %s)",
            (f"{scale//1000}K", generated)
        )
        conn.commit()

    return generated


def _flush_batch(conn, batch):
    """Insert a batch of memories into PostgreSQL."""
    import psycopg2.extensions
    # Register list→text[] adapter for tags
    def adapt_list(lst):
        if lst is None:
            return psycopg2.extensions.AsIs("'{}'")
        quoted = [f'"{x}"' if ',' in x or '"' in x else x for x in lst]
        return psycopg2.extensions.AsIs(f"'{{{','.join(quoted)}}}'")
    psycopg2.extensions.register_adapter(list, adapt_list)

    with conn.cursor() as cur:
        from psycopg2.extras import execute_values
        rows = []
        for m in batch:
            rows.append((
                m["memory_id"], m["session_id"], m["persona_id"], m["tenant_id"],
                m["content"], m["role"], m["importance"], m["tags"],
                m["category"], m["sha256_hash"], m["status"], m["version"],
                m["created_at"], m["updated_at"],
            ))
        execute_values(cur, """
            INSERT INTO memories
            (memory_id, session_id, persona_id, tenant_id, content, role,
             importance, tags, category, sha256_hash, status, version, created_at, updated_at)
            VALUES %s
            ON CONFLICT (memory_id) DO NOTHING
        """, rows, template="""(%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::text[], %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)""")
        conn.commit()


def main():
    parser = argparse.ArgumentParser(description="BEAM Scale Data Generator for Trinity")
    parser.add_argument("--scale", choices=["1K", "10K", "100K"], required=True,
                        help="Data scale: 1K (1,000), 10K (10,000), 100K (100,000)")
    parser.add_argument("--clean", action="store_true",
                        help="Clean existing test data before generating")
    args = parser.parse_args()

    scale_map = {"1K": 1000, "10K": 10000, "100K": 100000}
    total = scale_map[args.scale]

    print(f"BEAM Data Generator — Scale: {args.scale} ({total:,} memories)")
    print(f"PostgreSQL: {get_pg_config()['host']}:{get_pg_config()['port']}/{get_pg_config()['dbname']}")

    conn = connect_pg()
    ensure_benchmark_table(conn)

    if args.clean:
        clean_test_data(conn)

    count = generate_data(total, conn)
    conn.close()

    print(f"\nDone. Generated {count:,} test memories in PostgreSQL.")


if __name__ == "__main__":
    main()

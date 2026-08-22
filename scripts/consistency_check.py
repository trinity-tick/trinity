#!/usr/bin/env python3
"""
聚合池 vs 引擎库一致性校验（治理层，只读）
=================================================
Trinity 有两套长期并行数据：
  ① 引擎库 SQLite（运行时权威）  ~/.trinity/store/trinity_store.db（memories 表，status 状态列）
  ② 聚合池 JSON                  trinity/data/aggregator_pool.json（MemoryAggregator 共享池）

本脚本对两套做 **只读** 一致性校验，绝不写任何文件：
  - 引擎库以 read-only 模式打开（mode=ro），不会占用/获取写锁；
  - 聚合池以只读方式读取，不触发 MemoryAggregator 的懒构造/持久化。

指标（drift / 漂移计数）：
  total_active    库 active 记忆数
  pool_entries    池条目数
  missing_in_pool 库 active 但池内无对应 content（缺同步）
  extra_in_pool   池内 content 在库中任意状态皆无对应（多余）——"库无对应"
  hash_mismatch   同 content 双端哈希不一致（抽样上限 --hash-sample 条；默认 200）
  source_breakdown 池 entries 的 source_agents 字段分布

注意匹配键：聚合池的 memory_id 由引擎哈希生成（非库的 mem_* 主键），因此
两者对应关系只能以 content 为匹配键（规范化：str(content).strip()）。

退出码：--fail-threshold（默认 1）—— drift = missing+extra+hash_mismatch 超过该值
则 exit 1；--fail-threshold 0 = 从不因漂移失败。

用法：
    python scripts/consistency_check.py
    python scripts/consistency_check.py --json
    python scripts/consistency_check.py --fail-threshold 5
    python scripts/consistency_check.py --hash-sample 0   # 跳过哈希比对
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


def discover_pool_path() -> str:
    """镜像聚合器 _discover_persist_path：TRINITY_HOME / ~/trinity / ~/.trinity → <base>/data/aggregator_pool.json。"""
    candidates = [
        os.environ.get("TRINITY_HOME"),
        os.path.join(os.path.expanduser("~"), "trinity"),
        os.path.join(os.path.expanduser("~"), ".trinity"),
    ]
    for base in candidates:
        if base and os.path.isdir(base):
            p = os.path.join(base, "data", "aggregator_pool.json")
            if os.path.exists(p):
                return p
    # 兜底：仓库根 trinity/data（此处即仓库根下的 data）
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "aggregator_pool.json")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def open_db_readonly(path: str) -> sqlite3.Connection:
    """以只读模式打开 SQLite；不存在或不可读时报友好错误并退出。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"SQLite 引擎库不存在: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise sqlite3.Error(f"无法以只读模式打开引擎库 {path}: {exc}")
    # 强制该连接只读（双重保险；mode=ro 仅在 SQLite3 校验模式下无法写入）
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def load_db_rows(conn: sqlite3.Connection):
    """读取 memories 表：返回 (active_rows, all_rows, cols)。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "status" not in cols:
        raise sqlite3.Error("memories 表缺少 status 列——无法判定 active 状态")
    active_rows = conn.execute(
        "SELECT * FROM memories WHERE status = 'active'"
    ).fetchall()
    all_rows = conn.execute("SELECT * FROM memories").fetchall()
    return active_rows, all_rows, cols


def _norm(content) -> str:
    if content is None:
        return ""
    return str(content).strip()


def get_content_hash(row):
    """取库侧 content 哈希字段；优先 content_hash，其次 sha256_hash，缺失则现场算。"""
    keys = row.keys() if hasattr(row, "keys") else []
    for k in ("content_hash", "sha256_hash"):
        if k in keys:
            v = row[k]
            if v:
                return str(v)
    # 无可用哈希字段 → 现场算
    return sha256_hex(_norm(row["content"]))


def hash_prefix_match(db_hash: str, pool_content: str) -> bool:
    """比对库侧哈希与池 content 现场 sha256 的前 20 hex 前缀（content_hash 即前缀截断）。"""
    return str(db_hash)[:20] == sha256_hex(_norm(pool_content))[:20]


def load_pool(pool_path: str):
    """读取聚合池 JSON（只读），返回 (entries_list, normalized_content_set)。"""
    if not os.path.exists(pool_path):
        raise FileNotFoundError(f"聚合池 JSON 不存在: {pool_path}")
    with open(pool_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    memories = data.get("memories") or []
    entries = []
    for m in memories:
        if isinstance(m, dict):
            m.setdefault("memory_id", m.get("id", ""))
        entries.append(m)
    content_set = {_norm(m.get("content")) for m in entries if _norm(m.get("content"))}
    return entries, content_set


def source_breakdown(entries):
    """池 entries 的 source_agents 分布（展开列表 / 单值字段）。"""
    counts = {}
    for m in entries:
        src = m.get("source_agents")
        if isinstance(src, list):
            for s in src:
                counts[str(s)] = counts.get(str(s), 0) + 1
        elif src:
            counts[str(src)] = counts.get(str(src), 0) + 1
        else:
            counts["(none)"] = counts.get("(none)", 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def run_check(sqlite_path: str, pool_path: str, hash_sample: int):
    """执行一致性校验，返回指标 dict。"""
    conn = open_db_readonly(sqlite_path)
    try:
        # 表名探测：真正的大库是 memories；测试用最小表可能也叫 memories
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'").fetchall()]
        if not tables:
            raise sqlite3.Error("引擎库中不存在 memories 表")
        active_rows, all_rows, cols = load_db_rows(conn)
    finally:
        conn.close()

    total_active = len(active_rows)
    active_content = {_norm(r["content"]) for r in active_rows if _norm(r["content"])}
    all_content = {_norm(r["content"]) for r in all_rows if _norm(r["content"])}

    entries, pool_content = load_pool(pool_path)
    pool_entries = len(entries)

    missing_in_pool = active_content - pool_content
    # extra = 池有但库任意状态皆无对应（"库无对应"）
    extra_in_pool = pool_content - all_content

    # 哈希比对：抽样（默认 200）条 库 active 且池有对应 content 的行
    hash_mismatch = 0
    hash_checked = 0
    hash_missing_db = 0
    if hash_sample and hash_sample >= 0:
        matched = []
        for r in active_rows:
            c = _norm(r["content"])
            if c and c in pool_content:
                matched.append(r)
        # 抽样上限：取前 N 条已配对
        for r in matched[:hash_sample]:
            db_hash = get_content_hash(r)
            if not db_hash:
                hash_missing_db += 1
                continue
            hash_checked += 1
            if not hash_prefix_match(db_hash, r["content"]):
                hash_mismatch += 1

    source_dist = source_breakdown(entries)

    return {
        "sqlite_path": sqlite_path,
        "pool_path": pool_path,
        "total_active": total_active,
        "pool_entries": pool_entries,
        "missing_in_pool": len(missing_in_pool),
        "extra_in_pool": len(extra_in_pool),
        "hash_mismatch": hash_mismatch,
        "hash_checked": hash_checked,
        "hash_missing_db": hash_missing_db,
        "drift": len(missing_in_pool) + len(extra_in_pool) + hash_mismatch,
        "source_breakdown": source_dist,
        "samples": list(missing_in_pool)[:10],
        "extra_samples": list(extra_in_pool)[:10],
    }


def render_human(rep: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("Trinity 聚合池 vs 引擎库一致性校验（只读）")
    lines.append("=" * 60)
    lines.append(f"引擎库 : {rep['sqlite_path']}")
    lines.append(f"聚合池 : {rep['pool_path']}")
    lines.append("-" * 60)
    lines.append(f"total_active      : {rep['total_active']}   (库 active 记忆数)")
    lines.append(f"pool_entries      : {rep['pool_entries']}   (聚合池条目数)")
    lines.append(f"missing_in_pool   : {rep['missing_in_pool']}   (库 active 但池无对应)")
    lines.append(f"extra_in_pool     : {rep['extra_in_pool']}   (池有但库无对应)")
    lines.append(f"hash_mismatch     : {rep['hash_mismatch']}   (同 content 双端哈希不一致)")
    lines.append(f"hash_checked      : {rep['hash_checked']}   (哈希抽查条数)")
    lines.append(f"hash_missing_db   : {rep['hash_missing_db']}   (库缺哈希字段条数)")
    lines.append(f"drift (sum)       : {rep['drift']}")
    if rep["samples"]:
        lines.append("  missing 样例:")
        for s in rep["samples"]:
            lines.append(f"    - {s[:80]}")
    if rep["extra_samples"]:
        lines.append("  extra 样例:")
        for s in rep["extra_samples"]:
            lines.append(f"    - {s[:80]}")
    lines.append("-" * 60)
    lines.append("池 source_agents 分布:")
    for src, cnt in rep["source_breakdown"].items():
        lines.append(f"  {src:<24} {cnt}")
    lines.append("=" * 60)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="聚合池 vs 引擎库一致性校验（只读治理层）")
    parser.add_argument("--sqlite-path", default=os.path.expanduser("~/.trinity/store/trinity_store.db"),
                        help="引擎库 SQLite 路径（默认 ~/.trinity/store/trinity_store.db）")
    parser.add_argument("--pool-path", default=None,
                        help="聚合池 JSON 路径（默认自动发现 trinity/data/aggregator_pool.json）")
    parser.add_argument("--hash-sample", type=int, default=200,
                        help="内容哈希抽查上限（默认 200；0=跳过哈希比对）")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 报告")
    parser.add_argument("--fail-threshold", type=int, default=1,
                        help="diift 计数超过该值则 exit 1（默认 1；0=从不失败）")
    args = parser.parse_args(argv)

    pool_path = args.pool_path or discover_pool_path()

    # Windows 控制台默认 GBK(cp936) 无法编码池/库内容中的 emoji 等字符，
    # 这里把 stdout 切到 UTF-8（errors=replace 兜底），避免 ValueError/UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        rep = run_check(args.sqlite_path, pool_path, args.hash_sample)
    except (FileNotFoundError, sqlite3.Error, json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"[consistency-check] ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_human(rep))

    fail = rep["drift"] > args.fail_threshold and args.fail_threshold != 0
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

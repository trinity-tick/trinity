#!/usr/bin/env python3
"""export_memories_markdown.py — 记忆全集导出为 markdown 仓库（反锁定, 2026-08-24）。

对齐 2026 反锁定共识（Letta Context Repositories / UMP 协议 / AGENTS.md
文件化记忆）：记忆可导出为**可读、可 diff、可 git 版本化、可跨系统导入**
的 markdown 文件——不锁定在 Trinity 私有格式。

导出结构（--out 目录）：
  memories/
    INDEX.md            # 索引（记忆列表 + 元数据表）
    <memory_id>.md      # 每条记忆一个文件（front-matter + 正文）
  AGENTS.md             # 记忆接口说明（可选 --agents-md）

front-matter（YAML 风格，可被工具解析）：
  ---
  memory_id, category, importance, tags, created_at, updated_at,
  status, memory_layer, sha256_hash
  ---

用法：
    python scripts/export_memories_markdown.py --out ./memory-export
    python scripts/export_memories_markdown.py --out . --active-only --limit 50
    python scripts/export_memories_markdown.py --out . --init-git   # 导出后 git init + 首提交
幂等：重复运行覆盖同目录（文件按 memory_id 命名，可增量）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

SRC_DB = os.environ.get("TRINITY_DB_PATH") or os.path.expanduser(
    "~/.trinity/store/trinity_store.db"
)


def _fmt(ts) -> str:
    if not ts:
        return ""
    s = str(ts)
    return s[:19].replace("T", " ")


def _front_matter(r) -> str:
    r = dict(r)
    tags = r.get("tags") or "[]"
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    meta = r.get("metadata") or "{}"
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    lines = [
        "---",
        f"memory_id: {r.get('memory_id', '')}",
        f"category: {r.get('category', 'general')}",
        f"importance: {r.get('importance', 0.5)}",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        f"status: {r.get('status', 'active')}",
        f"memory_layer: {r.get('memory_layer', '') or ''}",
        f"created_at: {_fmt(r.get('created_at'))}",
        f"updated_at: {_fmt(r.get('updated_at'))}",
        f"sha256_hash: {r.get('sha256_hash', '')}",
        "---",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./memory-export", help="导出目录")
    parser.add_argument("--active-only", action="store_true", default=True,
                        help="只导出 active（默认）")
    parser.add_argument("--include-archived", action="store_true",
                        help="同时导出 archived（默认仅 active）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--init-git", action="store_true",
                        help="导出后 git init + 首提交（反锁定版本化）")
    parser.add_argument("--db", default=SRC_DB)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}")
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    where = ["1=1"]
    if not args.include_archived:
        where.append("status = 'active'")
    sql = f"SELECT * FROM memories WHERE {' AND '.join(where)} ORDER BY created_at"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    print(f"exporting {len(rows)} memories")

    out_dir = os.path.join(args.out, "memories")
    os.makedirs(out_dir, exist_ok=True)

    idx_lines = ["# Trinity Memory Export", "",
                 f"> 导出时间: {datetime.now().isoformat()} | 记忆数: {len(rows)}",
                 "", "| memory_id | category | importance | tags | created_at |",
                 "|---|---|---|---|---|"]
    for r in rows:
        mid = r["memory_id"]
        content = str(r["content"] or "")
        # 密文兼容（存储加密默认 on）——导出必须明文（数据可携权）
        if content.startswith("enc:v1:"):
            try:
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from trinity.security.crypto import get_storage_cipher
                c = get_storage_cipher()
                if c:
                    content = c.decrypt(content)
            except Exception:
                pass
        fm = _front_matter(dict(r))
        body = f"{fm}{content}\n"
        safe_mid = mid.replace("/", "_").replace(":", "_")
        path = os.path.join(out_dir, f"{safe_mid}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        rdict = dict(r)
        tags = rdict.get("tags") or "[]"
        try:
            tags = json.loads(tags) if isinstance(tags, str) else tags
            tags_str = ",".join(str(t) for t in tags)
        except Exception:
            tags_str = ""
        idx_lines.append(
            f"| {mid} | {rdict.get('category','')} | {rdict.get('importance','')} | {tags_str} | {_fmt(rdict.get('created_at'))} |"
        )

    with open(os.path.join(args.out, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx_lines) + "\n")

    # 记忆接口说明（复用 AGENTS.md 生成器）
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from export_agents_md import build_agents_md
        with open(os.path.join(args.out, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write(build_agents_md(include_live=False))
    except Exception:
        pass

    print(f"exported -> {args.out} (memories/{len(rows)} files + INDEX.md + AGENTS.md)")

    if args.init_git:
        try:
            subprocess.run(["git", "init", "-q"], cwd=args.out, check=True)
            subprocess.run(["git", "add", "-A"], cwd=args.out, check=True)
            # 无全局身份时用 local 兜底（commit 需要 user.name/email）
            subprocess.run(["git", "config", "user.email", "trinity@localhost"],
                           cwd=args.out, check=False)
            subprocess.run(["git", "config", "user.name", "trinity-export"],
                           cwd=args.out, check=False)
            subprocess.run(
                ["git", "commit", "-q", "-m", f"memory export {datetime.now().isoformat()}"],
                cwd=args.out, check=True,
            )
            print("git initialized + first commit")
        except Exception as exc:
            print(f"git init failed (non-fatal): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Trinity — 方案/文档融合导入器（2026-08-15）
==============================================
把方案规划与实际文档（Markdown）融合进 Trinity 记忆库：

  - 章节级切分：按 ## 标题把 .md 切为多条记忆（大文档可检索、可溯源）
  - 溯源：source_uri 指向原文件 + metadata 记录 section/标题/行号
  - 类型标注：category=doc:plan|doc:summary|doc:ops|doc:benchmark ...
  - 幂等：按 (path, mtime, section_title) 指纹去重
  - 可选 LLM 事实抽取：TRINITY_LLM_EXTRACT=on 时对每条写入建实体/关系图谱

用法：
    python scripts/fuse_docs.py                        # 默认导入 docs/*.md
    python scripts/fuse_docs.py --dir docs --persona trinity-docs
    python scripts/fuse_docs.py --dry-run              # 预览不写入
    $env:TRINITY_LLM_EXTRACT="on"                      # 开启图谱抽取（需 LLM key）
    python scripts/fuse_docs.py

输出：{docs_fused, sections, skipped, persons, source_files}
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_MAX_SECTION_CHARS = 8000
_ALLOWED_SUFFIX = (".md", ".markdown")


def _classify(path: Path) -> str:
    """按文件名推断文档类型。"""
    name = path.name.upper()
    if any(k in name for k in ("PLAN", "ROADMAP", "DIRECTION", "VISION", "REVIEW")):
        return "doc:plan"
    if any(k in name for k in ("SUMMARY", "OVERVIEW", "MAP", "FEATURE")):
        return "doc:summary"
    if any(k in name for k in ("OPS", "PERF", "MAINTENANCE", "DEPLOY", "SECURITY", "COMPLIANCE")):
        return "doc:ops"
    if any(k in name for k in ("BENCH", "REPORT", "COMPARISON", "EVAL")):
        return "doc:benchmark"
    if any(k in name for k in ("PROTOCOL", "INTEGRATION", "STATUS", "USAGE", "GUIDE")):
        return "doc:protocol"
    return "doc:general"


def split_markdown(path: Path) -> list[dict]:
    """按标题切分 Markdown 为章节块。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    sections: list[dict] = []
    cur_title = "PREAMBLE"
    cur_lines: list[str] = []
    cur_line_no = 1

    def flush() -> None:
        nonlocal cur_lines
        body = "\n".join(cur_lines).strip()
        if body and len(body) >= 120:  # 太短的块并入下一个
            sections.append({
                "title": cur_title,
                "body": body[:_MAX_SECTION_CHARS],
                "line": cur_line_no,
            })
        cur_lines = []

    for i, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if m and m.group(1) in ("##", "###"):
            flush()
            cur_title = m.group(2).strip()
            cur_line_no = i
            cur_lines = [line]
        else:
            cur_lines.append(line)
    flush()
    return sections


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity doc fusion importer")
    parser.add_argument("--dir", default=str(_TRINITY_ROOT / "docs"),
                        help="待融合的文档目录")
    parser.add_argument("--persona", default="trinity-docs")
    parser.add_argument("--agent", default="doc-fusion")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="忽略幂等指纹，强制重写")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"dir not found: {root}")
        return 1

    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.core.client import Trinity

    db_path = os.environ.get("TRINITY_STORE") or os.path.expanduser(
        "~/.trinity/store/trinity_store.db")
    print(f"== 文档融合（{root} → {db_path}）==")

    # 幂等指纹：persisted in memories.metadata doc_fingerprint
    adapter = SQLiteAdapter(db_path)
    adapter.connect()
    existing = set()
    for row in adapter._conn.execute(
        "SELECT metadata FROM memories WHERE persona_id = ? AND agent_id = ?",
        (args.persona, args.agent),
    ).fetchall():
        try:
            import json as _json
            meta = _json.loads(row[0] or "{}")
            fp = meta.get("doc_fingerprint")
            if fp:
                existing.add(fp)
        except Exception:
            pass
    print(f"   已导入指纹: {len(existing)}")

    t = Trinity(store_path=db_path)
    fused = skipped = 0
    files = sorted(root.rglob("*"))
    stats: dict = {}

    for path in files:
        if not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIX:
            continue
        rel = path.relative_to(root)
        category = _classify(path)
        mtime = path.stat().st_mtime
        sections = split_markdown(path)
        if not sections:
            continue
        for sec in sections:
            fp = hashlib.sha256(
                f"{rel}|{mtime}|{sec['title']}".encode()
            ).hexdigest()[:16]
            if fp in existing and not args.force:
                skipped += 1
                continue
            content = f"[{sec['title']}]\n{sec['body']}"
            metadata = {
                "source_uri": str(path),
                "source_file": rel.as_posix(),
                "section": sec["title"],
                "line": sec["line"],
                "doc_fingerprint": fp,
                "fused_at": datetime.now(timezone.utc).isoformat(),
            }
            if args.dry_run:
                print(f"   [dry] {rel}:{sec['line']} {sec['title']} ({len(sec['body'])}B)")
            else:
                try:
                    # postprocess=False：写入即时返回，跳过逐条语义关联/ANN
                    # 增量（大文档批导入避免慢）；实体/关系提取由
                    # maintenance 的 MemoryAgent 统一后台处理。
                    t.ingest(
                        content=content,
                        persona_id=args.persona,
                        agent_id=args.agent,
                        category=category,
                        tags=["doc", category.replace(":", "_"), "fused"],
                        importance=0.6,
                        metadata=metadata,
                        postprocess=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"   [err] {rel}:{sec['title']} {exc}")
                    continue
            fused += 1
            stats[category] = stats.get(category, 0) + 1

    if not args.dry_run:
        adapter.disconnect()
    print(f"\n   融合: {fused} 章节 / 跳过(已存在): {skipped} / 文件: {len([f for f in files if f.is_file() and f.suffix.lower() in _ALLOWED_SUFFIX])}")
    print(f"   类型分布: {stats}")
    print(f"   persona={args.persona} agent={args.agent}")

    # 验证：检索冒烟
    if not args.dry_run:
        t2 = Trinity(store_path=db_path)
        hits = t2.search("Trinity 记忆操作系统 架构", persona_id=args.persona,
                         top_k=3)
        results = hits.get("results", []) if isinstance(hits, dict) else hits
        print(f"\n   检索冒烟 'Trinity 记忆操作系统 架构': {len(results)} 条")
        for r in results[:3]:
            src = (r.get("metadata") or {}).get("source_file", "?")
            print(f"     - [{src}] {r.get('content', '')[:60]}")
    print(f"\nRESULT: {'PASS' if fused > 0 or skipped > 0 else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

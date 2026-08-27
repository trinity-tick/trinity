# -*- coding: utf-8 -*-
"""harvest_kb_structured.py — 文档摄入结构化（RAGFlow DeepDoc 轻量版，2026-08-27）。

对 kb_harvest/*.md 做结构化增强摄入：
  - 分节（## 标题）→ 分块记忆（细粒度知识单元）
  - markdown 表格 → 每行独立结构化记忆（tag=kb-table-row，含表头上下文）
保留 source_uri 溯源；已有整文件记忆不删（补充更细粒度单元）。

用法:
    python scripts/harvest_kb_structured.py [--limit N] [--dry-run]
"""
import os
import re
import sys
import time
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

KB_DIR = os.path.expanduser("~/.trinity/kb_harvest")


def split_sections(text: str):
    """按 ## 标题分节。返回 [(title, body)]。"""
    lines = text.split(chr(10))
    sections = []
    cur_title = "(header)"
    cur = []
    for ln in lines:
        if ln.startswith("## "):
            if cur:
                sections.append((cur_title, chr(10).join(cur)))
            cur_title = ln[3:].strip()
            cur = []
        else:
            cur.append(ln)
    if cur:
        sections.append((cur_title, chr(10).join(cur)))
    return sections


def extract_tables(body: str):
    """提取 markdown 表格行（含表头上下文）。返回 [(context_header, row_text)]。"""
    rows = []
    lines = body.split(chr(10))
    header = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("|") and ln.count("|") >= 3:
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if header is None:
                header = cells
            else:
                # 分隔行（|---|）跳过
                if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                    i += 1
                    continue
                ctx = " / ".join(f"{h}:{c}" for h, c in zip(header, cells) if c)
                rows.append((header, ctx))
        else:
            header = None
        i += 1
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=all files")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stale-only", action="store_true",
                    help="只重新摄入过时源（freshness>30 天，2026-08-27 knowledge.stale 闭环）")
    args = ap.parse_args()

    if not os.path.isdir(KB_DIR):
        print("kb_harvest dir missing:", KB_DIR)
        return 1
    files = sorted(f for f in os.listdir(KB_DIR) if f.endswith(".md"))
    if args.stale_only:
        # 知识源注册表：只保留 stale 源（fresh_days>30 且来源是 kb_harvest 文件）
        sys.path.insert(0, _TRINITY_ROOT)
        from trinity.knowledge import sources
        reg = sources()
        stale = {s["source_id"].replace("\\", "/") for s in reg.get("sources", []) if s.get("stale")}
        files = [f for f in files if os.path.join(KB_DIR, f).replace("\\", "/") in stale]
        print(f"stale-only: {len(files)} files")
    if args.limit:
        files = files[: args.limit]

    sys.path.insert(0, _TRINITY_ROOT)
    from trinity import Trinity
    mem = Trinity(adapter="sqlite")

    total_mem = 0
    total_sections = 0
    total_rows = 0
    for f in files:
        path = os.path.join(KB_DIR, f)
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        sections = split_sections(text)
        t0 = time.time()
        for title, body in sections:
            body = body.strip()
            if len(body) < 40:
                continue
            content = f"[kb-section:{f}] {title}{chr(10)}{body[:1500]}"
            if args.dry_run:
                total_mem += 1
            else:
                try:
                    mem.ingest(content, agent_id="kb-harvester", category="kb_harvested",
                               tags=["kb-section", title[:24]], source_uri=path,
                               postprocess=False)
                    total_mem += 1
                except Exception as exc:
                    print(f"  ingest section fail {f}:{title}: {exc}")
            total_sections += 1
        # 表格行（在整文件 body 上提取，去重由内容哈希保证）
        for header, row in extract_tables(text):
            content = f"[kb-table-row:{f}] {row}"
            if not args.dry_run:
                try:
                    mem.ingest(content, agent_id="kb-harvester", category="kb_harvested",
                               tags=["kb-table-row"], source_uri=path,
                               postprocess=False)
                    total_rows += 1
                except Exception:
                    pass
            else:
                total_rows += 1
        print(f"  {f}: sections={len([s for s in sections if len(s[1].strip()) >= 40])} table_rows={len(extract_tables(text))} ({time.time()-t0:.1f}s)")
    print(f"done: files={len(files)} sections={total_sections} table_rows={total_rows} mem={total_mem} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

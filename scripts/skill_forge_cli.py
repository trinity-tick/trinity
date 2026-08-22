#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Skill 自动锻造 CLI（执行轨迹 -> 共性模式 -> 可复用 Skill）

用法：
  python scripts/skill_forge_cli.py \
      --input trinity/modules/second_brain/sidecar \
      [--out data/skills/auto] \
      [--name-prefix auto-] \
      [--store] [--dry-run] \
      [--llm on|off]

默认 --dry-run（不写记忆，只打印摘要）；--store 才通过注入的 store 写记忆。
--input 为目录时扫描 *.jsonl 合并为轨迹；为文件则直接读取。
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.memory.skill_forge import (  # noqa: E402
    parse_traces,
    extract_patterns,
    render_skill,
    write_skill,
    store_skill_meta,
    safe_filename,
)


def _load_input(path: str) -> List[Dict[str, Any]]:
    """读取 jsonl 文件或目录（扫描 *.jsonl）。"""
    records: List[Dict[str, Any]] = []
    files: List[str] = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.jsonl")))
    elif os.path.isfile(path):
        files = [path]
    else:
        raise SystemExit(f"input path not found: {path}")

    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        import json
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError as e:
            print(f"[warn] skip {fp}: {e}", file=sys.stderr)
    return records


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Skill auto-forge from execution traces")
    ap.add_argument("--input", required=True, help="jsonl 文件或目录(扫 *.jsonl)")
    ap.add_argument("--out", default="data/skills/auto", help="输出目录")
    ap.add_argument("--name-prefix", default="auto-", help="生成 skill 文件名前缀")
    ap.add_argument("--store", action="store_true", help="写 skill 摘要记忆(默认 dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run(默认已开启)")
    ap.add_argument("--llm", choices=["on", "off"], default="on", help="LLM 归纳开关")
    args = ap.parse_args(argv)

    records = _load_input(args.input)
    if not records:
        print("no trace records loaded")
        return 1

    sequences = parse_traces(records)
    if not sequences:
        print("no actionable sequences extracted")
        return 1

    print(f"loaded {len(records)} records -> {len(sequences)} sequences "
          f"({len({s['trace_id'] for s in sequences})} traces)")

    pattern = extract_patterns(sequences, llm_enabled=(args.llm == "on"))
    name = (args.name_prefix or "") + safe_filename(pattern.get("name") or "skill")
    domain = pattern.get("domain") or "general"

    md = render_skill(
        name=name,
        domain=domain,
        pattern=pattern,
        traces_count=len(sequences),
        source=",".join([
            os.path.basename(p) for p in (
                glob.glob(os.path.join(args.input, "*.jsonl"))
                if os.path.isdir(args.input) else [args.input]
            )
        ][:5]) or "traces",
    )

    path = write_skill(md, name, args.out)
    print(f"wrote skill -> {path}")
    print(f"  name: {name}  domain: {domain}  traces: {len(sequences)}")
    print("  steps:", len(pattern.get("steps") or []))
    print("  pitfalls:", len(pattern.get("pitfalls") or []))

    store = None
    if args.store and not args.dry_run:
        try:
            from trinity.core.client import Trinity  # lazy，避免导入自举
            engine = Trinity()
            store = engine.store_memory
        except Exception as e:  # noqa: BLE001
            print(f"[warn] engine unavailable, skip store: {e}", file=sys.stderr)
            store = None

    if store is not None:
        res = store_skill_meta(store, md)
        print("stored skill meta:", res if res is not None else "None")
    else:
        print("dry-run: skill meta not stored (--store to persist)")

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Trinity — 模块审计器（2026-08-15）
====================================
全量扫描 second_brain 模块，按"运行路径可达性"分类：

  - ACTIVE    : 被 engine 聚合链或全库代码引用（真实运行路径）
  - EXPERIMENT: 文件头标注 status: experimental（算法储备）
  - ORPHAN    : 全库零引用且无实验标注（候选归档/删除）

输出：
  1. 控制台摘要
  2. ~/.trinity/logs/module_audit.json（结构化报告）

用法：
    python scripts/audit_modules.py
    python scripts/audit_modules.py --json-only    # 只输出 JSON
    python scripts/audit_modules.py --mark-orphan  # 给孤儿模块文件头加 status: orphan 标注
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
_SB = _TRINITY_ROOT / "trinity" / "modules" / "second_brain"

# engine 聚合链（真实加载路径的入口文件）
ENGINE_CHAIN = [
    "engine.py", "engine_core.py", "engine_governance.py", "engine_memory_core.py",
    "engine_memory_tiers.py", "engine_guardian_retrieval.py", "engine_diagnostics.py",
    "loader.py", "p1_preamble.py", "p21_p25.py", "__init__.py",
]

_IMPORT_RE = re.compile(
    r'from trinity\.modules\.second_brain\.(\w+) import|'
    r'from \.second_brain\.(\w+) import|'
    r'from \.(\w+) import|'
    r'import trinity\.modules\.second_brain\.(\w+)'
)
_STATUS_RE = re.compile(r'^\s*#\s*status:\s*(\w+)', re.IGNORECASE | re.MULTILINE)


def scan() -> Dict[str, List[str]]:
    mod_files = sorted(_SB.glob("*.py"))
    all_mods = {f.stem for f in mod_files if f.stem != "__init__"}

    # 全库引用（含 engine 链）
    all_py = list(_TRINITY_ROOT.glob("trinity/**/*.py")) + list(_TRINITY_ROOT.glob("scripts/*.py"))
    refs: Set[str] = set()
    for f in all_py:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in _IMPORT_RE.finditer(txt):
            for g in m.groups():
                if g:
                    refs.add(g)

    # 文件头 status 标注（区分 experimental 与 orphan）
    experimental: Set[str] = set()
    orphan_marked: Set[str] = set()
    for f in mod_files:
        head = f.read_text(encoding="utf-8", errors="ignore")[:400]
        m = _STATUS_RE.search(head)
        if m:
            st = m.group(1).lower()
            if st == "experimental":
                experimental.add(f.stem)
            elif st in ("orphan", "archived"):
                orphan_marked.add(f.stem)

    active = sorted(refs & all_mods)
    # 孤儿 = 全库零引用 + 已标注 orphan（或未标注零引用） - engine 链文件
    chain_set = {f for f in ENGINE_CHAIN if f != "__init__.py"}
    zero_ref = all_mods - refs - chain_set
    orphan = sorted((zero_ref & orphan_marked) | (zero_ref - experimental - orphan_marked))
    exp = sorted((all_mods - refs) & experimental)

    return {
        "total": len(all_mods),
        "active": active,
        "experimental": exp,
        "orphan": orphan,
        "active_count": len(active),
        "experimental_count": len(exp),
        "orphan_count": len(orphan),
        "chain_imported": sorted(refs),
    }


def mark_orphans(report: Dict[str, List[str]]) -> int:
    """给孤儿模块文件头加 status: orphan 标注（幂等）。

    保护：
    - ENGINE_CHAIN 文件永不标注（它们是真实加载路径入口）
    - 保留文件头 BOM（U+FEFF）——标注行插到 BOM 之后、首个非注释行之前
    """
    marked = 0
    for name in report["orphan"]:
        if name in ENGINE_CHAIN:
            continue  # 运行链文件不标注
        p = _SB / f"{name}.py"
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if _STATUS_RE.search(txt[:400]):
            continue  # 已有标注
        bom = ""
        body = txt
        if body.startswith("\ufeff"):
            bom, body = "\ufeff", body[1:]
        lines = body.splitlines(keepends=True)
        # 在文件 docstring/注释块后插入标注行
        insert_at = 0
        for i, ln in enumerate(lines[:12]):
            if ln.strip().startswith('"""') or ln.strip().startswith("'''"):
                continue
            if ln.strip() and not ln.strip().startswith("#"):
                insert_at = i
                break
        lines.insert(insert_at, "# status: orphan (2026-08-15 audit, not in runtime path)\n")
        p.write_text(bom + "".join(lines), encoding="utf-8")
        marked += 1
    return marked


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity module auditor")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--mark-orphan", action="store_true",
                        help="给孤儿模块加 status: orphan 标注")
    args = parser.parse_args()

    report = scan()
    out = Path(os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))) / "logs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "module_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    if args.mark_orphan:
        marked = mark_orphans(report)
        print(f"marked {marked} orphan modules")
        report["marked_orphan"] = marked
        (out / "module_audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return 0

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False))
        return 0

    print(f"== Trinity 模块审计（{_SB}）==")
    print(f"   模块总数: {report['total']}")
    print(f"   ACTIVE（运行路径可达）: {report['active_count']}")
    print(f"   EXPERIMENTAL（标注）: {report['experimental_count']}")
    print(f"   ORPHAN（零引用）: {report['orphan_count']}")
    print(f"\n   ACTIVE: {', '.join(report['active'][:40])}")
    if report["orphan"]:
        print(f"\n   ORPHAN 前 30: {', '.join(report['orphan'][:30])}")
    print(f"\n   报告: {out / 'module_audit.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

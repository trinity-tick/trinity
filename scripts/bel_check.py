# -*- coding: utf-8 -*-
"""G31/t174 ③：**BEL（U+0007）自检** —— 写入后的 `count(b"\\x07")`，只读计数、不改内容。

## 为什么需要它（本轮真实事故）
本轮 `tests/unit/test_memory_id_integrity_20261008.py` 里出现过 **2 个 `U+0007`（BEL）**：
成因是**非 raw 字面量里的 `\\a` 被解析成 BEL**（`"...；\\agent_id..."` ⇒ `；\\x07gent_id`）。
字节级守卫（`tests/unit/test_text_integrity_20261006.py` 的 `control_chars`）**抓住了它**；
本脚本是**同一件事的"随手自检"形态**：写入后立刻 `count(b"\\x07")`，**零风险**（只读、只计数）。

## 零风险的理由（逐条）
1. **只读**：`open(..., "rb")` 读取字节；**不写、不修改、不修复**（修复请用字节级 Python 操作，见本文档尾）；
2. **不改判定**：它只报"有几处/在哪"，是否算缺陷由判据面决定；
3. **不引入依赖**：只用标准库。

## 若有 BEL ⇒ 怎么定位（本轮用过的方法）
1. **字节计数**：`raw.count(b"\\x07")`（本脚本）；
2. **逐行定位**：按 `\\n` 切分后在**行内找列号**，并打印**替换成 `<BEL>` 的可读行**（本脚本 `--locate`）；
3. **回到源码**：看那一行的**字符串字面量**——若是 `\\a` 形态（中文字符串里出现 `\\` + 字母），改成 **raw 字符串**或 **`\\\\a`**；
4. **修复**：**字节级** `raw.replace(b"\\x07", b"a")`（本轮即如此），⛔ **不要用 shell 编码往返**。

## 退出码
0 = 未发现；1 = 发现（打印 file:line:col）；2 = 有文件不可读（**UNTESTABLE**，不得当通过）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEL = b"\x07"
TEXT_EXT = (".py", ".md", ".json", ".txt", ".ps1", ".toml", ".yaml", ".yml", ".sql", ".cfg", ".ini")
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".worktrees", "backup", "backups"}


def find_bel_bytes(raw: bytes, limit: int = 20) -> list:
    """在**字节**里找 BEL，返回 [(line, col, 可读上下文)]（<BEL> 替代原字符，便于阅读）。"""
    hits = []
    for ln_no, line in enumerate(raw.split(b"\n"), 1):
        if BEL not in line:
            continue
        idx = 0
        while True:
            j = line.find(BEL, idx)
            if j < 0 or len(hits) >= limit:
                break
            ctx = line[max(0, j - 60):j + 60].decode("utf-8", errors="replace").replace("\x07", "<BEL>")
            hits.append((ln_no, j + 1, ctx))
            idx = j + 1
        if len(hits) >= limit:
            break
    return hits


def scan_file(path: str) -> tuple:
    try:
        raw = open(path, "rb").read()
    except Exception as e:                             # noqa: BLE001
        return None, "%r" % (e,)
    return find_bel_bytes(raw), None


def changed_files() -> list:
    """`git status --porcelain` 里的改动/未跟踪**文本**文件（= 最可能刚被写入的那批）。"""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = []
    for ln in (r.stdout or "").split("\n"):
        if not ln.strip():
            continue
        p = ln[3:].strip().strip('"')
        if p.endswith(TEXT_EXT):
            out.append(os.path.join(ROOT, p.replace("/", os.sep)))
    return out


def walk_files(roots=None) -> list:
    out = []
    for base in (roots or ["tests", "scripts", "trinity", "docs"]):
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in fn:
                if f.endswith(TEXT_EXT):
                    out.append(os.path.join(dp, f))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="BEL(U+0007) 自检：只读计数 + 定位")
    ap.add_argument("paths", nargs="*", help="要扫的文件（缺省：--changed 或 常用目录）")
    ap.add_argument("--changed", action="store_true", help="只扫 git 改动/未跟踪的文本文件")
    ap.add_argument("--all", action="store_true", help="扫 tests/scripts/trinity/docs 全部文本文件")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    files = a.paths or (changed_files() if a.changed or not a.all else walk_files())
    res = {"scanned": 0, "hits": [], "errors": []}
    for p in files:
        hits, err = scan_file(p)
        if err:
            res["errors"].append("%s: %s" % (os.path.relpath(p, ROOT), err))
            continue
        res["scanned"] += 1
        for ln, col, ctx in hits:
            res["hits"].append({"file": os.path.relpath(p, ROOT).replace("\\", "/"),
                                "line": ln, "col": col, "context": ctx})
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("[BEL 自检] 扫描 %d 个文本文件（cwd=%s）" % (res["scanned"], ROOT))
        if not res["hits"]:
            print("  ✅ 未发现 U+0007（count(b'\\x07') = 0）")
        for h in res["hits"]:
            print("  ⛔ %s:%d:%d  %s" % (h["file"], h["line"], h["col"], h["context"][:120]))
            print("     定位法：该行字符串里若有 `\\a` 形态 ⇒ 改 raw 字符串或 `\\\\a`；"
                  "修复用字节级 replace(b'\\x07', b'a')")
        for e in res["errors"]:
            print("  ⚠️ 不可读 ⇒ UNTESTABLE: %s" % e)
    if res["errors"]:
        return 2
    return 1 if res["hits"] else 0


if __name__ == "__main__":
    sys.exit(main())

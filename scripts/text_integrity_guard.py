#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""text_integrity_guard.py —— 文本/编码完整性守卫（t20，2026-10-06）

## 这是什么

**第 4 次同类事故后加的机械防线**。同一天里出现了 4 次"字节层损坏、但测试全绿"的事故：

  1. `edit` 工具剥掉 `.ps1` 的 UTF-8 BOM ⇒ PowerShell 5.1 按 GBK 解码中文注释
     ⇒ 报 4 个**假**语法错误；
  2. PowerShell 编码往返把 `trinity/engine_worker.py` 改坏（`py_compile` 失败）；
  3. GBK 控制台 `UnicodeEncodeError` 让脚本 exit 1；
  4. `Get-Content -Raw | -replace | Set-Content` 把报告写成乱码。

它们的共同点：**损坏不改变行为** ⇒ 单测、`pytest`、类型检查**全都不会红**。
所以需要一条"看字节"的判据，而不是"看行为"的判据。

## 检测项（纯二进制读，不经过任何 shell / locale 编解码）

**硬性（判红）**
  · `invalid_utf8`      —— 不是合法 UTF-8
  · `fffd`              —— 含 U+FFFD 替换字符（解码已丢数据的**痕迹**）
  · `mojibake_gbk`      —— cp936 误读族的**成串**签名（**不产生 U+FFFD** 的那种坏法）
  · `mojibake_latin`    —— cp1252/latin-1 误读族的成串签名
  · `pua`               —— 私用区字符（U+E000–U+F8FF 等，cp936 往返指纹）
  · `control_chars`     —— 除 `\\t \\n \\r \\f` 外的控制字符
  · `bom`               —— **非 `.ps1`** 文件带 UTF-8 BOM
  · `bom_missing_ps1`   —— **`.ps1` 缺 UTF-8 BOM**（本仓 `.ps1` **要求** BOM）
  · `foreign_bom`       —— UTF-16/32 BOM（任何扩展名）
  · `ast_fail`          —— `.py` 无法 `ast.parse`（BOM 已剥离后再判，避免级联误报）

**软性（只记录，**绝不**判红）**
  · `eol_crlf`          —— 含 CRLF。本机 `core.autocrlf=true` ⇒ 一次 `git checkout`
    就可能把文件变成 CRLF：**字节变了但不是损坏**。把它判红会让 5871 个文本文件里
    **3952 个**（实测 67%）变红 —— 那正是本轮在消灭的"到处假红"。

## 为什么乱码签名必须是"成串 + 阈值"

第一版按"误读**可能**产出的字符集"推导 ⇒ 集合里**含 ASCII 字母** ⇒ 5866/5868 文件全中，
**零判别力**。故改用**判别力**筛选：要求候选签名在**干净语料里 0 命中**、在**模拟损坏后高命中**，
并且**成串出现次数 ≥ 阈值**（默认 3）才判红。实测（见报告 §16）：

  · cp936 族 14 个签名：干净语料 98,359 字里 **0** 命中；模拟损坏后 2–827 命中；
  · latin 族前缀：干净 0；模拟损坏后 10–1208 命中。

⚠️ 签名集**用码点构造**，不在本文件里写出那些字符本身 —— 否则**守卫脚本自己**会被判红
（"判据不能被自己的留痕骗"，本仓已有前科）。

## 用法

    python scripts/text_integrity_guard.py                 # 看工作树改动文件（默认）
    python scripts/text_integrity_guard.py --repo          # 全仓扫（用于上界基线）
    python scripts/text_integrity_guard.py --json          # 机读
退出码：0 = 无硬性命中；1 = 有；2 = UNTESTABLE（git 不可用等，fail-closed 不当通过）。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_EXT = (".py", ".json", ".md", ".ps1", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".txt")

#: 不参与扫描的目录：依赖/构建/临时产物。**node_modules 必须排除** ——
#: 实测 `git status` 里就有 `dsh-plugin/**/node_modules/**` 的改动，那是第三方内容。
SKIP_DIRS = frozenset({
    "__pycache__", ".venv", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", "backup", "temp", "output", ".worktrees", "dist", "build",
})

#: 字节级 BPE 词表：任意 Unicode（含 latin 变音）在这里都是**合法数据**，不是乱码。
#: 实测 `benchmark/l1_corpus/out_*/*/{merges.txt,vocab.json,tokenizer.json}` 里
#: latin 族签名各出现数百到数千次 —— 必须排除，否则判据是噪声源。
BYTELEVEL_BPE = ("benchmark/l1_corpus/",)

#: cp936 误读族签名（码点构造，见模块头）。每个元素是**单个**字符的码点。
_GBK_MARKER_CODEPOINTS = (
    0x93C2, 0x9350, 0x6D63, 0x9428, 0x6D93, 0x93C4, 0x935C,
    0x9422, 0x93C3, 0x951B, 0x9239, 0x934F, 0x95BF, 0x9423,
)

#: cp1252/latin-1 误读族签名（**前缀**，多字符更能避开正常拉丁文）。
_LATIN_MARKER_CODEPOINT_SEQS = (
    (0xE2, 0x20AC),          # a-circumflex + euro
    (0xEF, 0xBC),            # i-diaeresis + fraction
    (0xE3, 0x20AC),          # a-tilde + euro
    (0xC3, 0xA4),            # A-tilde + currency
    (0xC3, 0xA9),            # A-tilde + copyright
    (0xC3, 0xBC),            # A-tilde + not-sign
)

GBK_MARKERS = tuple(chr(c) for c in _GBK_MARKER_CODEPOINTS)
LATIN_MARKERS = tuple("".join(chr(c) for c in seq) for seq in _LATIN_MARKER_CODEPOINT_SEQS)

#: 成串阈值：单次命中可能只是极生僻汉字/正常拉丁文，故要求 ≥ N。
MIN_MARKER_HITS = 3

_CTRL_ALLOWED = {"\t", "\n", "\r", "\f"}
_UTF8_BOM = b"\xef\xbb\xbf"

#: BOM 表：**(BOM 字节, 名字)**，按**长度降序**排列。
#:
#: ⚠️ 2026-10-06（t22 修复，verifier 预演期抓到的**恒假分支**）：本表原先只有一列常量，
#: 而检测写成 `any(raw[:4] == b for b in _UTF16_32_BOMS)` —— **2 字节的 UTF-16 BOM 与
#: 4 字节切片永远不可能相等** ⇒ `foreign_bom` 对**真实 UTF-16 文件永不触发**（不可达）。
#: 实测：UTF-16LE/BE 夹具只报 `invalid_utf8 + control_chars`，`foreign_bom` 不触发；UTF-32 正常。
#: ⇒ 教训：**不要把不同长度的前缀常量塞进同一条定长比较**。
#: 现在改成"按各自常量长度比较"，并且**顺序必须先长后短**（UTF-32LE 的 BOM 以 UTF-16LE 的
#: BOM 开头，先比短的会把 UTF-32 误标成 UTF-16）。
BOM_TABLE = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (_UTF8_BOM, "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def detect_bom(raw: bytes, bom_table=None):
    """返回文件开头的 BOM 名字（`utf-8` / `utf-16-le` / …），没有则 None。

    **按每个常量自己的长度比较** —— 这是本函数存在的理由（见 `BOM_TABLE` 的注释）。
    """
    for bom, name in (bom_table if bom_table is not None else BOM_TABLE):
        if raw[:len(bom)] == bom:
            return name
    return None


def bom_reachability_probe(bom_table=None, payload: bytes = b"x = 1\n", detector=None) -> list:
    """**可达性探针**：对 BOM 表里**每一个**常量，造一个以它开头的字节串，看能否被识别出来。

    这是"能发现**常量不可达/分支恒假**"的通用检查方式（而不是只加一条 UTF-16 用例）：
    任何被塞进表里、却没有与之匹配的比较逻辑的常量，都会在这里返回 `detected_as=None`。

    `detector` 可注入 ⇒ 测试能用**历史上那个坏实现**（`any(raw[:4] == b for b, _ in table)`）
    证明这条判据真的会红 —— 也就是"它本来能提前抓到这次的 bug"。
    """
    table = bom_table if bom_table is not None else BOM_TABLE

    def _default(raw, tbl):
        return detect_bom(raw, tbl)

    fn = detector or _default
    out = []
    for bom, name in table:
        raw = bom + payload
        out.append({"bom_hex": bom.hex(), "name": name, "bom_len": len(bom),
                    "detected_as": fn(raw, table)})
    return out


#: 向后兼容别名（旧代码/旧测试可能引用）。
_UTF16_32_BOMS = tuple(b for b, n in BOM_TABLE if n != "utf-8")

#: 用**正则**做逐字符判定：纯 Python 的 `for c in txt` 在 5,847 个文件（含数 MB 的 JSON）
#: 上超过 pytest 的 300s 超时（实测踩到）。正则走 C 实现。
#: PUA 三段：BMP 私用区 / 补充私用区 A / 补充私用区 B。
_PUA_RX = re.compile("[\ue000-\uf8ff\U000f0000\U000ffffd\U00100000\U0010fffd]")
#: 除 `\t \n \r \f` 之外的控制字符（`\f` 在 Python 源码里合法，故放行）
_CTRL_RX = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f]")

#: 超大文件的**内容类**检测只扫前 N 字节（纯数据块，如基准语料）。
#: 记 `size_capped=True` 保持透明：不把"没扫完"读成"扫过了没问题"。
CONTENT_SCAN_CAP = 8 * 1024 * 1024


def inspect_bytes(raw: bytes, ext: str) -> dict:
    """对一个文件的**字节**做全部检测。纯函数，便于在合成夹具上做负向实测。"""
    rec: dict = {
        "has_utf8_bom": raw[:3] == _UTF8_BOM,
        "bom_name": detect_bom(raw),
        "crlf_count": raw.count(b"\r\n"),
        "hard": [],
        "notes": [],
    }
    body = raw[3:] if rec["has_utf8_bom"] else raw
    try:
        full_txt = body.decode("utf-8")
        rec["utf8_ok"] = True
    except UnicodeDecodeError as exc:
        rec["utf8_ok"] = False
        rec["utf8_error"] = "%s at byte %s" % (exc.reason, exc.start)
        full_txt = body.decode("utf-8", errors="replace")
        rec["hard"].append("invalid_utf8")
        rec["notes"].append("非合法 UTF-8 ⇒ 后续字符类检测基于 replace 结果，仅供参考")

    # 超大文件只扫前 N 字节的**内容类**检测；显式记 size_capped，不把"没扫完"当"没问题"。
    rec["size_capped"] = len(full_txt) > CONTENT_SCAN_CAP
    txt = full_txt[:CONTENT_SCAN_CAP] if rec["size_capped"] else full_txt
    if rec["size_capped"]:
        rec["notes"].append("size_capped：仅扫前 %d 字符（内容类检测）" % CONTENT_SCAN_CAP)

    rec["fffd_count"] = txt.count("\ufffd")
    rec["pua_count"] = len(_PUA_RX.findall(txt))
    rec["control_chars"] = sorted({hex(ord(c)) for c in set(_CTRL_RX.findall(txt))})
    rec["gbk_marker_hits"] = sum(txt.count(m) for m in GBK_MARKERS)
    rec["latin_marker_hits"] = sum(txt.count(m) for m in LATIN_MARKERS)

    # 只报"确凿"的：invalid_utf8 已经解释了 fffd，不重复计入（避免同因多报）
    if rec["utf8_ok"] and rec["fffd_count"]:
        rec["hard"].append("fffd")
    if rec["utf8_ok"] and rec["gbk_marker_hits"] >= MIN_MARKER_HITS:
        rec["hard"].append("mojibake_gbk")
    if rec["utf8_ok"] and rec["latin_marker_hits"] >= MIN_MARKER_HITS:
        rec["hard"].append("mojibake_latin")
    if rec["pua_count"]:
        rec["hard"].append("pua")
    if rec["control_chars"]:
        rec["hard"].append("control_chars")
    #: `foreign_bom` 现在由 `bom_name` **直接推导**（而不是逐常量比对）——
    #: 这样"常量长度与比较长度不匹配 ⇒ 分支恒假"这种错法在结构上就不可能再发生。
    rec["foreign_bom"] = rec["bom_name"] is not None and rec["bom_name"] != "utf-8"
    if rec["foreign_bom"]:
        rec["hard"].append("foreign_bom")
        rec["notes"].append("非 UTF-8 的 BOM：%s" % rec["bom_name"])

    if ext == ".ps1":
        if not rec["has_utf8_bom"]:
            rec["hard"].append("bom_missing_ps1")
    elif rec["has_utf8_bom"]:
        rec["hard"].append("bom")

    if rec["crlf_count"]:
        rec["notes"].append("eol_crlf(%d)：本机 core.autocrlf=true ⇒ 不算损坏"
                            % rec["crlf_count"])

    if ext == ".py":
        if rec["utf8_ok"]:
            try:
                ast.parse(full_txt)
                rec["ast_ok"] = True
            except SyntaxError as exc:
                rec["ast_ok"] = False
                rec["ast_error"] = "%s (line %s)" % (exc.msg, exc.lineno)
                rec["hard"].append("ast_fail")
        else:
            rec["ast_ok"] = None
            rec["notes"].append("UTF-8 非法 ⇒ 不做 AST 判定（避免级联误报）")
    elif ext == ".json":
        # 大 JSON 的 `json.loads` 很慢；只在 2MB 以内真解析，否则如实记"跳过"。
        if rec["utf8_ok"] and len(full_txt) <= 2_000_000:
            try:
                json.loads(full_txt)
                rec["json_ok"] = True
            except Exception as exc:  # noqa: BLE001
                rec["json_ok"] = False
                rec["notes"].append("JSON 解析失败：%s" % str(exc)[:80])
        elif rec["utf8_ok"]:
            rec["json_ok"] = None
            rec["notes"].append("json_check_skipped(size>2MB)")
    return rec


def inspect_path(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as fh:
        raw = fh.read()
    rec = inspect_bytes(raw, ext)
    rec["path"] = os.path.relpath(path, ROOT).replace("\\", "/")
    rec["bytes"] = len(raw)
    return rec


def iter_text_files(base: str = None):
    root = base or ROOT
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fn in sorted(files):
            if not fn.lower().endswith(TEXT_EXT):
                continue
            p = os.path.join(cur, fn)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            if any(rel.startswith(pref) for pref in BYTELEVEL_BPE):
                continue
            yield p


def changed_text_files() -> tuple:
    """`git status --porcelain` 里的文本文件（**判定面**）。返回 (paths, error)。"""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120).stdout
    except Exception as exc:  # noqa: BLE001
        return [], "%s: %s" % (type(exc).__name__, exc)
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip().strip('"')
        if " -> " in p:
            p = p.split(" -> ")[-1].strip()
        if not p.lower().endswith(TEXT_EXT):
            continue
        if any(seg in p.split("/") for seg in SKIP_DIRS):
            continue
        if any(p.startswith(pref) for pref in BYTELEVEL_BPE):
            continue
        fp = os.path.join(ROOT, p)
        if os.path.isfile(fp):
            paths.append(fp)
    return sorted(set(paths)), ""


def head_bytes(rel: str):
    """取 `HEAD:<rel>` 的字节；不在 HEAD 里（新增/未跟踪）返回 None。"""
    try:
        p = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=ROOT,
                           capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001
        return None
    return p.stdout if p.returncode == 0 and p.stdout else None


def introduced_findings(rel: str, status: str = "") -> dict:
    """把「本次改动**引入**的硬性命中」与「既存的」分开。**这是判定面的核心口径。**

    为什么必须分：本仓**早就**有 77 个非 `.ps1` 文件带 BOM、`AGENTS.md` **早就**含 U+FFFD、
    `trinity/__init__.py` **早就**是 GBK 乱码（三者都在 `HEAD` 里，实测见报告 §16.3）。
    若口径写成"改动的文件必须完全干净"，那么**任何人合法编辑这些文件都会红** ——
    判据立刻变成噪声源，正是本轮在消灭的形态。

    故：`引入 = 现在的硬性命中 − HEAD 的硬性命中`。新增/未跟踪文件没有 HEAD ⇒ 全部算引入。
    """
    now = inspect_path(os.path.join(ROOT, rel))
    if status == "??":
        head = None
    else:
        raw = head_bytes(rel)
        head = inspect_bytes(raw, os.path.splitext(rel)[1].lower()) if raw else None
    now_set = set(now["hard"])
    head_set = set(head["hard"]) if head else set()
    return {"path": rel, "status": status,
            "now_hard": sorted(now_set), "head_hard": sorted(head_set),
            "introduced": sorted(now_set - head_set),
            "preexisting": sorted(now_set & head_set),
            "gbk": now["gbk_marker_hits"], "latin": now["latin_marker_hits"],
            "fffd": now["fffd_count"]}


def changed_text_files_with_status() -> tuple:
    """`git status --porcelain` 的文本文件 + 状态码（判定面）。返回 (list[(rel, st)], error)。"""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120).stdout
    except Exception as exc:  # noqa: BLE001
        return [], "%s: %s" % (type(exc).__name__, exc)
    items = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        st = line[:2].strip() or "?"
        p = line[3:].strip().strip('"')
        if " -> " in p:
            p = p.split(" -> ")[-1].strip()
        if not p.lower().endswith(TEXT_EXT):
            continue
        if any(seg in p.split("/") for seg in SKIP_DIRS):
            continue
        if any(p.startswith(pref) for pref in BYTELEVEL_BPE):
            continue
        if os.path.isfile(os.path.join(ROOT, p)):
            items.append((p, st))
    return sorted(set(items)), ""


def audit_introduced(items) -> dict:
    recs = [introduced_findings(rel, st) for rel, st in items]
    bad = [r for r in recs if r["introduced"]]
    return {"checked": len(recs), "flagged": len(bad),
            "findings": bad,
            "preexisting_only": [{"path": r["path"], "hard": r["preexisting"]}
                                 for r in recs if r["preexisting"] and not r["introduced"]]}


def audit(paths) -> dict:
    recs = [inspect_path(p) for p in paths]
    bad = [r for r in recs if r["hard"]]
    return {"checked": len(recs), "flagged": len(bad),
            "findings": [{"path": r["path"], "hard": r["hard"],
                          "gbk": r["gbk_marker_hits"], "latin": r["latin_marker_hits"],
                          "fffd": r["fffd_count"], "notes": r["notes"]} for r in bad],
            "clean": len(recs) - len(bad)}


def main() -> int:
    ap = argparse.ArgumentParser(description="文本/编码完整性守卫（t20）")
    ap.add_argument("--repo", action="store_true", help="全仓扫描（上界基线用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.repo:
        paths, err = list(iter_text_files()), ""
        scope = "全仓"
        rep = audit(paths)
    else:
        items, err = changed_text_files_with_status()
        scope = "工作树改动（口径：**引入**的硬性命中，见 introduced_findings）"
        rep = audit_introduced(items)
    if err:
        print("[UNTESTABLE] 取不到改动清单（git 不可用）：%s" % err)
        return 2

    rep["scope"] = scope
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print("文本/编码完整性守卫（%s；乱码签名阈值 ≥ %d）" % (scope, MIN_MARKER_HITS))
        print("=" * 78)
        for f in rep["findings"]:
            print("  %-58s [%s] %s" % (f["path"][:58], f.get("status", ""),
                                       ",".join(f.get("introduced") or f.get("hard") or [])))
        for f in rep.get("preexisting_only", [])[:10]:
            print("  (既存·不判红) %-48s %s" % (f["path"][:48], ",".join(f["hard"])))
        print("-" * 78)
        print("  checked=%d flagged=%d" % (rep["checked"], rep["flagged"]))
    return 1 if rep["flagged"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

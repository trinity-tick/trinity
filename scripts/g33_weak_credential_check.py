#!/usr/bin/env python -X utf8
# -*- coding: utf-8 -*-
"""G33/t176 **B：Trinity 相关默认/弱凭据检测（只读，不轮换）**。

⭐ 队长的决定：**不轮换**（会打断运行中的服务/连接）⇒ 本脚本**只检测 + 报警**。
⛔ 它**不修改任何文件**、**不连接任何服务**、**不做任何 git 操作**。

输出纪律（**硬要求**）：
  ⚠️ **绝不打印明文口令** ⇒ 只给「位置 + 强度判定（weak/medium/strong）+ **前 2 位掩码**」。
  ⚠️ 掩码规则：`前2位 + "***"`（长度 < 2 时给 `"***"`，**不泄露长度差**）。

退出码：
  **0** = 未发现 weak 凭据（或只发现 medium/strong）
  **1** = **发现至少一处 weak 凭据**（供 CI / 健康检查报警用）
  **2** = 用法错误

用法：
    python scripts/g33_weak_credential_check.py                 # 扫默认范围
    python scripts/g33_weak_credential_check.py --root <dir>    # 换根
    python scripts/g33_weak_credential_check.py --self-test     # ⭐ 牙齿：人造弱/强口令各测一次
    python scripts/g33_weak_credential_check.py --json          # 机器可读
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

#: ⭐ 默认扫描范围（**只扫这些**，避免全盘遍历）：
#: 仓库内**明文可能写凭据**的文件类型 + `dsh-ops/**` 的脚本/配置。
DEFAULT_ROOTS = [
    "trinity", "scripts", "dsh-ops", "docker-compose.yml", "docker-compose.yaml",
    ".env", ".env.example", "config", "deploy", "tests",
]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".venv", "dist", "build", ".ruff_cache"}
#: 只看这些后缀/文件名（避免二进制与大数据）
TEXT_EXT = {".py", ".ps1", ".sh", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
            ".env", ".md", ".txt", ".conf", ".properties"}

#: ⭐ 凭据出现的**位置模式**（(字段名正则, 说明)）
CRED_PATTERNS = [
    (re.compile(r"""\b(PGPASSWORD|PG_PASSWORD|POSTGRES_PASSWORD|TRINITY_PG_PASSWORD)\b""",
                re.I), "PG 口令字段"),
    (re.compile(r"""\b(TRINITY_PG_PASSWORD|db_password|database_password)\b""", re.I), "PG 口令字段"),
    (re.compile(r"""\b(API_KEY|APIKEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|DEEPSEEK_API_KEY)\b""",
                re.I), "API token 字段"),
    (re.compile(r"""\b(password|passwd|pwd)\s*[:=]""", re.I), "通用 password 赋值"),
]

#: ⭐ 明显的**默认/占位值**（这些一律判 weak，且**它们本身就是"默认口令"**）
KNOWN_DEFAULTS = {
    "trinity", "password", "passwd", "123456", "changeme", "change_me", "admin",
    "root", "test", "secret", "default", "postgres", "example", "your_password",
    "yourpassword", "xxxxxx", "todo", "none", "null", "foo", "bar",
}

#: 赋值取值：`KEY = value` / `KEY: value` / `KEY="value"` / `export KEY=value`
ASSIGN_RE = re.compile(
    r"""(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<q>['"]?)(?P<val>[^\s'"#,;)\]}]+)(?P=q)""")

#: 占位符特征（不是真凭据 ⇒ 降级为 info，不报警）
PLACEHOLDER_RE = re.compile(
    r"(?i)^(\$\{.*\}|%.*%|\{\{.*\}\}|<.*>|\*+|-+|\.\.\.|x{3,}|your[-_]?\w*|"
    r"placeholder|example|sample|dummy|fake|redacted|masked|todo|none|null|n/?a)$")


def mask(v: str) -> str:
    """⭐ 掩码：**只给前 2 位**（不足 2 位则完全掩掉，不泄露长度）。"""
    s = str(v or "")
    if len(s) < 2:
        return "***"
    return s[:2] + "***"


def classify(v: str, key: str = "") -> str:
    """强度判定：weak / medium / strong / placeholder。

    ⭐ **判据是"可分辨"的，不是恒红/恒绿**（见 `--self-test` 的牙齿）。
    ⚠️ 这是**启发式**，不是密码学强度评估；它只回答"这看起来是不是默认/弱值"。
    """
    s = str(v or "")
    if not s:
        return "empty"
    if PLACEHOLDER_RE.match(s) or "$" in s or "{%" in s:
        return "placeholder"
    low = s.lower().strip()
    if low in KNOWN_DEFAULTS:
        return "weak"                       # ⭐ 命中默认值表 ⇒ 直接 weak
    if len(s) < 8:
        return "weak"                       # ⭐ 太短
    if len(set(s)) <= 3:
        return "weak"                       # ⭐ 字符种类极少（aaaa / 1234 / xxxxx）
    # 中等：长度够但只有一类字符，或明显的词+数字（如 trinity2026）
    kinds = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if kinds <= 2:
        return "medium"
    if len(s) >= 16 and kinds >= 3:
        return "strong"
    if kinds >= 3 and len(s) >= 12:
        return "strong"
    return "medium"


def iter_files(root: str, rel_roots):
    for rr in rel_roots:
        p = os.path.join(root, rr)
        if os.path.isfile(p):
            yield p
            continue
        if not os.path.isdir(p):
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in TEXT_EXT or fn.startswith(".env") or fn.endswith(".ps1"):
                    yield os.path.join(dirpath, fn)


def scan_file(path: str, root: str, max_bytes: int = 400_000):
    """返回该文件里的凭据命中（**已掩码**）。⛔ 不打印明文。"""
    out = []
    try:
        if os.path.getsize(path) > max_bytes:
            return out
        txt = io.open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return out
    rel = os.path.relpath(path, root).replace("\\", "/")
    for lineno, line in enumerate(txt.splitlines(), start=1):
        if len(line) > 4000:
            continue
        # 该行是否像"凭据行"
        if not any(p.search(line) for p, _ in CRED_PATTERNS):
            continue
        for m in ASSIGN_RE.finditer(line):
            key, val = m.group("key"), m.group("val")
            # 只认"键名像凭据"的赋值
            if not any(p.search(key) for p, _ in CRED_PATTERNS):
                continue
            strength = classify(val, key)
            out.append({"file": rel, "line": lineno, "key": key,
                        "strength": strength, "masked": mask(val),
                        "len_bucket": ("<8" if len(val) < 8 else
                                       "8-15" if len(val) < 16 else ">=16")})
    return out


def self_test() -> int:
    """⭐ 牙齿：用人造弱口令 / 强口令各测一次 ⇒ **必须一个 weak、一个非 weak**。"""
    weak = "trinity"                      # 默认值表命中
    weak2 = "123456"                      # 默认值表命中
    strong = "Xk9#mQ2vL7@pR4wZ"           # 16 位、4 类字符 ⇒ strong
    medium = "trinitydatabase"            # 长度够但只 1 类 ⇒ medium
    print("=== G33 弱口令检测脚本 —— 牙齿（self-test）===")
    rows = [("人造弱口令 A", weak), ("人造弱口令 B", weak2),
            ("人造中等口令", medium), ("⭐ 人造强口令", strong)]
    ok = True
    for label, v in rows:
        s = classify(v)
        print("  %-12s masked=%-8s strength=%-8s" % (label, mask(v), s))
    got = {classify(weak), classify(weak2), classify(medium), classify(strong)}
    print("  distinct strengths =", sorted(got))
    if classify(weak) != "weak" or classify(weak2) != "weak":
        print("  ⛔ FAIL: 弱口令未被判 weak ⇒ 脚本无分辨力"); ok = False
    if classify(strong) == "weak":
        print("  ⛔ FAIL: 强口令被判 weak ⇒ 恒红"); ok = False
    if len(got) < 3:
        print("  ⛔ FAIL: 强度分级太少 ⇒ 分辨力不足"); ok = False
    if ok:
        print("  ✅ PASS: 弱→weak(×2)、中→medium、强→strong ⇒ **能分开弱与强**")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Trinity 弱/默认凭据检测（只读，不轮换）")
    ap.add_argument("--root", default=r"D:\trinity-code", help="扫描根目录")
    ap.add_argument("--self-test", action="store_true", help="⭐ 牙齿：人造弱/强口令各测一次")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--limit", type=int, default=0, help="最多报多少条（0=全部）")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = args.root
    if not os.path.isdir(root):
        print(json.dumps({"error": "root not found", "root": root}, ensure_ascii=False))
        return 2

    hits = []
    scanned = 0
    for p in iter_files(root, DEFAULT_ROOTS):
        scanned += 1
        hits.extend(scan_file(p, root))
    # 去重（同一 file:line:key 只报一次）
    seen = set()
    uniq = []
    for h in hits:
        k = (h["file"], h["line"], h["key"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    # 排除占位符（不算报警）
    real = [h for h in uniq if h["strength"] not in ("placeholder", "empty")]
    weak = [h for h in real if h["strength"] == "weak"]
    med = [h for h in real if h["strength"] == "medium"]
    strong = [h for h in real if h["strength"] == "strong"]
    if args.limit:
        real = real[:args.limit]

    result = {
        "root": root, "files_scanned": scanned,
        "hits_total": len(uniq), "hits_placeholders_excluded": len(uniq) - len(real),
        "weak": len(weak), "medium": len(med), "strong": len(strong),
        "findings": real,
        "note": ("⛔ 本脚本**只检测、不轮换、不修改任何文件**（队长裁定：轮换会打断运行中的服务/连接）；"
                 "⚠️ 所有取值**只给前 2 位掩码**，**不含明文**"),
        "remediation_one_liner": ("把命中处改为从环境变量/密钥文件读取（如 `PGPASSWORD` 走 "
                                  "`~/.dsh/.credentials.yaml` 或进程环境），**不要**把值写在仓内文件里；"
                                  "并对 PG 使用强口令（≥16 位、≥3 类字符）。**本任务不改。**"),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("=== G33 弱/默认凭据检测（只读）===")
        print("root=%s | files_scanned=%d | hits=%d（占位符已排除 %d）"
              % (root, scanned, len(real), len(uniq) - len(real)))
        print("weak=%d | medium=%d | strong=%d" % (len(weak), len(med), len(strong)))
        for h in real:
            print("  [%s] %s:%d  %s = %s  (len_bucket=%s)"
                  % (h["strength"].upper(), h["file"], h["line"], h["key"],
                     h["masked"], h["len_bucket"]))
        if not real:
            print("  未见：在默认范围（%s）内**未命中**任何凭据赋值。"
                  % ",".join(DEFAULT_ROOTS))
        print("提示：%s" % result["remediation_one_liner"])
    return 1 if weak else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

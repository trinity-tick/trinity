#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""temp_leak_audit.py — 测试侧临时目录泄漏闸门（N8，2026 优化轮）。

## 为什么有这个脚本

借鉴定性来源（Aivy）：*"每多加一层就多一层维护成本和新 bug"* —— 反过来，
**已经造成事故的模式必须能被机器查出来**，否则下次还是靠人回想。

事故（`OPT-B6-RESULTS.md` §十）：`tests/test_wal_checkpoint.py` 与
`tests/test_pg_llm_extract.py` 用 `tempfile.mkdtemp()` 建目录后**从不清理**，
累积 **144.35 GB** 把 C: 盘写满 ⇒ 一次全量回归跑成 `106 failed / 268 errors`，
且**威胁线上的 trinity-api**（同盘）。实测**每跑一次全量就净漏约 7 GB**。

## 判据

- **扫描范围 = 两个面**：主面 `tests/` + `trinity/`（棘轮，基线 24 处），
  附加面 `scripts/` + `benchmark/`（**独立棘轮**，2026-09-26 新增）。
  原实现只扫主面并显式不扫 scripts/（理由：人工一次性调用、产物常留着看）；
  §1353 实测把这条理由打回 —— 我自己的脚本漏了 14 个目录 / 156 MB 而棘轮照样
  报 `OK：24 <= 基线 24`，被读成「没有泄漏」。现在两个面都在输出里计数。
- **findings** = 文件中出现 `tempfile.mkdtemp(`，但**该文件内没有任何清理机制**。
  清理机制判据（任一即可）：`rmtree` / `addCleanup` / `TemporaryDirectory` / `cleanup`。
- 逐文件判定（而非逐函数）：unittest 风格把清理写在 `tearDown`（另一函数）里，
  逐函数判会误报；逐文件判既能抓住两个真实泄漏点，又不误伤。

## 用法

    python scripts/temp_leak_audit.py [--json] [--ratchet]

退出码：0 = 未变差；1 = 新增泄漏文件（ratchet）。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.expanduser("~/.trinity/state/temp_leak_audit.json")
BASELINE_DEFAULT = os.path.join(ROOT, "dsh-ops", "temp_leak_baseline.json")

# L1 静默失败治理（G31/t174）：**吞但计数**（与 docs/SILENT_FAILURE_BUDGETS.json 的 `_policy` 一致）
try:
    from trinity._swallow import swallow
except Exception:                      # 独立脚本可能没有 trinity 路径 ⇒ 退化为空操作
    def swallow(*_a, **_k):            # type: ignore[misc]
        return None

#: 棘轮面（主基线 total）—— tests/ = 每跑一次回归就多一份，是唯一会**跨运行累积**的形态；
#: trinity/ = **生产代码**，泄漏按**请求次数**累积（实测 `_routers_a2a.py` 的签名路由
#:           每次请求都留下一个含 RSA 私钥的临时目录 —— 既是空间问题也是安全问题）。
SCAN_ROOTS = ("tests", "trinity")

#: 附加面（2026-09-26 新增，§1353 登记项之三）：scripts/ 与 benchmark/。
#: 原来刻意不扫，理由是「人工一次性调用，产物常需留着看」。实测把这条理由打回：
#: §1353 里我自己的 `scripts/longrange_summary_coverage.py` 漏了 14 个目录 / 156 MB、
#: 容量尺子共用 `cap_caliber_*` 漏了 182 个 / 1,018.6 MB，**而棘轮照样报
#: `OK：24 <= 基线 24`** —— 读数被读成「没有泄漏」（§13.3：清单的可信度取决于
#: 匹配规则的边界）。现改为**独立棘轮**：扫、计数、单独冻结基线（不得新增），
#: 但**不与主面合并**（主面的 24 处是测试侧欠账，两者的处置责任不同）。
ADVISORY_ROOTS = ("scripts", "benchmark")

#: 清理机制标记（任一出现即视为已处置）
CLEANUP_MARKERS = ("rmtree", "addCleanup", "TemporaryDirectory", "cleanup")


# ── G31/t174 ②：**结果侧（outcome）读数** ────────────────────────────────────
#: ⚠️ 上面 `CLEANUP_MARKERS` 是**代理量（proxy）**：只要文件里出现 `shutil.rmtree(...)` 就算"有清理"。
#: t162 §9.1 实测过的漏洞：带 `ignore_errors=True` 的 rmtree 在**句柄未释放**时会**静默失败**
#: （Windows：`PermissionError: [WinError 32]`）⇒ **proxy 满足、目录仍在**。
#: ⇒ 所以本文件**新增**一把**结果量（outcome）**的尺子：直接数 `%TEMP%` 下各前缀的**残留目录数与体量**。
#: ⛔ 这一侧**不参与棘轮**（不加基线、不阻断），只作为**可观测读数**与 proxy 并列输出。
TEMP_PREFIX_KNOWN = ("g13_case_", "g9r8crit_", "g3_switch_", "g10r4")
OUTCOME_MAX_FILES = 50000        # 单个前缀的枚举上限（防病态目录拖死）


def _mkdtemp_prefixes(roots=None) -> list:
    """从源码里**自动**收集 `tempfile.mkdtemp(prefix="X")` 的**字面前缀**（含 t162 的已知前缀）。

    ⇒ 新增一处 mkdtemp 时，outcome 侧**自动**多一个观测前缀（无需手工维护清单）。
    """
    out = set(TEMP_PREFIX_KNOWN)
    for base in (roots or ("tests", "scripts", "benchmark", "trinity")):
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in dp:
                continue
            for f in fn:
                if not f.endswith(".py"):
                    continue
                try:
                    src = open(os.path.join(dp, f), encoding="utf-8-sig", errors="replace").read()
                    tree = ast.parse(src)
                except Exception:                      # noqa: BLE001
                    continue
                for n in ast.walk(tree):
                    if _is_mkdtemp(n):
                        for kw in getattr(n, "keywords", []) or []:
                            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) \
                                    and isinstance(kw.value.value, str) and kw.value.value:
                                out.add(kw.value.value)
    return sorted(out)


def outcome_scan(temp_dir: str = None, prefixes: list = None) -> dict:
    """**结果侧**：`%TEMP%` 下各前缀的残留目录数/文件数/字节数（只读；不改任何东西）。"""
    td = temp_dir or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    prefixes = prefixes or _mkdtemp_prefixes()
    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "temp_dir": td,
           "prefixes": {}, "total_dirs": 0, "total_bytes": 0, "capped": False,
           "size_error_sample": None}
    try:
        names = os.listdir(td)
    except Exception as _e:                            # noqa: BLE001
        res["error"] = "%r" % (_e,)
        return res
    for pre in prefixes:
        dirs = files = 0
        size = 0
        size_errors = 0
        size_error_sample = None
        for name in names:
            if not name.startswith(pre):
                continue
            p = os.path.join(td, name)
            if not os.path.isdir(p):
                continue
            dirs += 1
            for r, _d, fs in os.walk(p):
                for f in fs:
                    files += 1
                    if files > OUTCOME_MAX_FILES:
                        res["capped"] = True
                        break
                    try:
                        size += os.path.getsize(os.path.join(r, f))
                    except OSError as _e:
                        # G31/t174（门禁点名后的修法）：原为静默 `pass` ⇒ 改**吞但计数**，
                        # **并把"字节数可能少算"显式带进本读数**（t162 §9.2：proxy 满足而 outcome 失真）。
                        # 触发条件：目录枚举之后、文件被删除/无权限（PID 竞态、pytest 清理并发）——
                        # 见 tests/unit/test_temp_leak_outcome_20261008.py::test_C3 用替换 getsize 的方式**确定性**触发。
                        size_errors += 1
                        if size_error_sample is None:
                            size_error_sample = "%s: %r" % (f, _e)
                            res["size_error_sample"] = size_error_sample
                        swallow(__name__ + ":outcome_getsize", _e)
                if files > OUTCOME_MAX_FILES:
                    break
        res["prefixes"][pre] = {"dirs": dirs, "files": files, "bytes": size,
                                "bytes_may_undercount": size_errors}
        res["total_dirs"] += dirs
        res["total_bytes"] += size
    return res


def format_outcome(o: dict) -> list:
    out = ["[outcome] 结果侧读数（%s）—— ⚠️ 与上面 proxy 并列看：proxy=『有没有写清理』，outcome=『清没清掉』"
           % o["temp_dir"]]
    for pre, d in sorted(o["prefixes"].items()):
        flag = "✅ 0" if d["dirs"] == 0 else "⚠️ 残留"
        extra = ("  ⚠️ 字节数可能少算 %d 处（见 size_error_sample）" % d["bytes_may_undercount"]
                 if d.get("bytes_may_undercount") else "")
        out.append("   %-14s dirs=%-4d files=%-6d bytes=%-11d %s%s"
                   % (pre, d["dirs"], d["files"], d["bytes"], flag, extra))
    if o.get("size_error_sample"):
        out.append("   [样本] getsize 失败样例 = %s" % str(o["size_error_sample"])[:160])
    out.append("   合计 dirs=%d bytes=%d%s（时点 %s）"
               % (o["total_dirs"], o["total_bytes"],
                  "（**枚举达上限**）" if o.get("capped") else "", o["ts"]))
    return out


def _is_mkdtemp(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkdtemp"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "tempfile")


def scan(root: str = None) -> dict:
    """扫两个面：`findings`（棘轮面）/ `advisory_findings`（附加面）。

    `root` 只为判据单测而参数化（默认仓根）—— 单测在临时目录里造一个
    "无清理的 mkdtemp" 文件，断言**附加面确实被扫到**（反事实：不扫 ⇒ 该用例红）。
    """
    root = root or ROOT
    findings = []
    advisory = []
    n_files = 0
    n_adv_files = 0
    for base in ADVISORY_ROOTS:
        d = os.path.join(root, base)
        if not os.path.isdir(d):
            continue
        for dp, dn, fns in os.walk(d):
            dn[:] = [x for x in dn if x not in ("__pycache__", ".pytest_cache")]
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, root).replace("\\", "/")
                try:
                    src = open(p, encoding="utf-8-sig", errors="replace").read()
                    tree = ast.parse(src)
                except Exception:
                    continue
                n_adv_files += 1
                calls = [n for n in ast.walk(tree) if _is_mkdtemp(n)]
                if not calls or any(m in src for m in CLEANUP_MARKERS):
                    continue
                for c in calls:
                    advisory.append({"file": rel, "line": c.lineno})
    for base in SCAN_ROOTS:
        d = os.path.join(root, base)
        for dp, dn, fns in os.walk(d):
            dn[:] = [x for x in dn if x not in ("__pycache__", ".pytest_cache")]
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, root).replace("\\", "/")
                try:
                    src = open(p, encoding="utf-8-sig", errors="replace").read()
                    tree = ast.parse(src)
                except Exception:
                    continue
                n_files += 1
                calls = [n for n in ast.walk(tree) if _is_mkdtemp(n)]
                if not calls:
                    continue
                if any(m in src for m in CLEANUP_MARKERS):
                    continue
                for c in calls:
                    findings.append({"file": rel, "line": c.lineno})
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scanned": n_files, "total": len(findings), "findings": findings,
            "advisory_scanned": n_adv_files,
            "advisory_total": len(advisory), "advisory_findings": advisory}


def format_report(r: dict) -> list:
    """人类可读摘要（**两行**：主面 + 附加面）。

    判据（§13.5 写侧哑线）：附加面的计数**必须出现在输出里** —— 否则
    「OK：24 <= 基线 24」会被读成「没有泄漏」（§1353 实测就是这么被读错的）。
    """
    lines = ["temp leak: 扫 %d 个文件（%s），泄漏点 %d 处"
             % (r["scanned"], "+".join(SCAN_ROOTS), r["total"])]
    lines.append("temp leak: 附加面（%s）扫 %d 个文件，泄漏点 %d 处 —— 独立棘轮，只判「不得新增」"
                 % ("+".join(ADVISORY_ROOTS), int(r.get("advisory_scanned", 0)),
                    int(r.get("advisory_total", 0))))
    for x in r["findings"][:40]:
        lines.append("  %-46s:%-5d tempfile.mkdtemp 无清理" % (x["file"], x["line"]))
    for x in (r.get("advisory_findings") or [])[:10]:
        lines.append("  [附加面] %-40s:%-5d tempfile.mkdtemp 无清理" % (x["file"], x["line"]))
    return lines


def judge(r: dict, base: dict) -> tuple:
    """**两个面各自棘轮**：返回 (rc, 输出行)。纯函数 ⇒ 判据可直接单测。

    - 主面（tests+trinity）：`total` vs 基线 `total`（历史 24 处欠账）；
    - 附加面（scripts+benchmark）：`advisory_total` vs 基线 `advisory_total`；
      老基线没有该键时**本次不阻断**（先冻结现状），由 main 写回。
    """
    out, rc = [], 0
    base_n = int(base.get("total", 0))
    if r["total"] > base_n:
        out += ["[ratchet] FAIL：测试侧临时目录泄漏 %d -> %d（基线 %d）"
                % (base_n, r["total"], base_n),
                "[ratchet] 请给新增的 tempfile.mkdtemp 补 try/finally: shutil.rmtree(...)",
                "[ratchet] 原因：不清理会随回归次数累积（实测每跑一次全量净漏约 7 GB），"
                "写满磁盘后整跑失效且威胁同盘的线上服务。"]
        rc = 1
    else:
        out.append("[ratchet] OK：主面泄漏点 %d <= 基线 %d" % (r["total"], base_n))
        if r["total"] < base_n:
            out.append("[ratchet] 已改善 —— 请下调 dsh-ops/temp_leak_baseline.json 的 total 值。")
    adv = int(r.get("advisory_total", 0))
    if base.get("advisory_total") is None:
        out.append("[ratchet] 附加面无基线 -> 本次建立：advisory_total=%d（不阻断）" % adv)
    elif adv > int(base["advisory_total"]):
        out += ["[ratchet] FAIL：附加面（%s）泄漏 %d -> %d（基线 %d）"
                % ("+".join(ADVISORY_ROOTS), int(base["advisory_total"]), adv,
                   int(base["advisory_total"])),
                "[ratchet] 附加面是**人工调用**的脚本：产物可留，但不该无界增长"
                "（§1353 实测：我自己漏了 14 个目录 / 156 MB 而棘轮照样报 OK）。"]
        rc = 1
    else:
        out.append("[ratchet] OK：附加面泄漏点 %d <= 基线 %d" % (adv, int(base["advisory_total"])))
    return rc, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ratchet", action="store_true")
    ap.add_argument("--outcome", action="store_true",
                    help="G31/t174：额外打印**结果侧**读数（%TEMP% 残留），与 proxy 并列，不参与棘轮")
    ap.add_argument("--baseline", default=BASELINE_DEFAULT)
    args = ap.parse_args()

    r = scan()
    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass

    if args.json:
        print(json.dumps({"total": r["total"],
                          "advisory_total": r.get("advisory_total", 0)}, ensure_ascii=False))
    else:
        for ln in format_report(r):
            print(ln)
        print("out ->", OUT)
    if args.outcome:                                   # G31/t174 ②：proxy 与 outcome **并列**
        o = outcome_scan()
        for ln in format_outcome(o):
            print(ln)
        if args.json:
            print(json.dumps({"outcome": o}, ensure_ascii=False))

    if args.ratchet:
        base = None
        if os.path.exists(args.baseline):
            try:
                with open(args.baseline, encoding="utf-8-sig") as fh:
                    base = json.load(fh)
            except Exception as _e:
                print("[ratchet] FAIL：基线文件存在但无法解析：%s" % args.baseline)
                print("[ratchet] 原因：%r" % (_e,))
                return 1
            if not isinstance(base, dict) or "total" not in base:
                print("[ratchet] FAIL：基线格式不正确（缺 total）")
                return 1
        if base is None:
            print("[ratchet] 无基线 -> 本次建立：total=%d advisory_total=%d（不阻断）"
                  % (r["total"], r.get("advisory_total", 0)))
            try:
                with open(args.baseline, "w", encoding="utf-8") as fh:
                    json.dump({"total": r["total"],
                               "advisory_total": r.get("advisory_total", 0),
                               "note": "历史欠账基线；新增测试侧临时目录泄漏会使 CI 失败。"
                                       "清理泄漏后请下调此值。"},
                              fh, ensure_ascii=False, indent=1)
            except Exception:
                pass
            return 0
        rc, lines = judge(r, base)
        for ln in lines:
            print(ln)
        if "advisory_total" not in base:
            # 老基线（只有 total）首次遇到新面：补写基线值，**不阻断**本次
            # （理由：先把现状冻结下来，否则"新面第一次跑"必然红）。
            base["advisory_total"] = int(r.get("advisory_total", 0))
            try:
                with open(args.baseline, "w", encoding="utf-8") as fh:
                    json.dump(base, fh, ensure_ascii=False, indent=1)
                print("[ratchet] 已把附加面基线写回 %s（advisory_total=%d）"
                      % (os.path.basename(args.baseline), base["advisory_total"]))
            except Exception as _e:
                print("[ratchet] 附加面基线写回失败：%r" % (_e,))
        return rc

    return 1 if r["total"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

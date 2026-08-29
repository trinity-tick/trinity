# -*- coding: utf-8 -*-
"""evolve_patch.py — 自进化代码补丁（阶段1，2026-08-28，文本替换模式）。

给定目标（scripts/ 白名单 .py）+ 目标描述 → LLM 输出精确文本替换
（REPLACE/WITH 块）→ 唯一匹配校验 → py_compile 冒烟 → 报告。
默认只生成+验证（人工确认后 --apply 写入）；git 记录由人工 commit。

用法:
  python scripts/evolve_patch.py --target scripts/tune_judge.py \
      --goal "增加 args.queries 最小值保护"
"""
import os
import sys
import time
import argparse
import subprocess

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

_PATCH_DIR = os.path.join(_TRINITY_ROOT, "temp", "patches")


def _record_stats(ok, retries, last_err, goal, diff):
    """Record one evolve run quality stats (recursive self-improvement data)."""
    import json as _json
    import time as _time
    try:
        _stats_path = os.path.join(_TRINITY_ROOT, "temp", "patches", "evolve_stats.json")
        _entries = []
        if os.path.exists(_stats_path):
            try:
                _entries = _json.load(open(_stats_path, encoding="utf-8"))
            except Exception:
                _entries = []
        _entries.append({"ts": _time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok,
                         "retries": retries, "reason": last_err or ("ok" if ok else "other"),
                         "patch_lines": diff.count(chr(10)) if diff else 0, "goal": goal[:60]})
        _entries = _entries[-50:]
        with open(_stats_path, "w", encoding="utf-8") as f:
            _json.dump(_entries, f, ensure_ascii=False, indent=1)
    except Exception as _e:
        print("STATS_ERR:", type(_e).__name__, str(_e)[:120])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--apply", action="store_true", help="验证通过后写入文件")
    ap.add_argument("--auto", action="store_true", help="阶段2：写入+fulltest 门禁+自动 commit（失败回滚）")
    ap.add_argument("--stats", action="store_true", help="递归自改进：汇总补丁质量+失败模式+改进建议")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 2026-08-29 (recursive): stats report mode
    if args.stats:
        import json as _json
        _stats_path = os.path.join(_TRINITY_ROOT, "temp", "patches", "evolve_stats.json")
        entries = []
        if os.path.exists(_stats_path):
            try:
                entries = _json.load(open(_stats_path, encoding="utf-8"))
            except Exception:
                entries = []
        n = len(entries)
        ok_n = sum(1 for e in entries if e.get("ok"))
        fmt_fail = sum(1 for e in entries if not e.get("ok") and e.get("reason") == "format")
        retries = [e.get("retries", 0) for e in entries]
        print("evolve stats: " + str(n) + " runs | ok " + str(ok_n) + " | fail " + str(n - ok_n) + " | format-fail " + str(fmt_fail))
        if retries:
            print("retries avg: " + str(sum(retries) / len(retries))[:4] + " | max " + str(max(retries)))
        if n >= 3 and fmt_fail / max(1, n) > 0.3:
            print("SUGGEST: format-fail high - strengthen REPLACE/WITH block constraints in prompt")
        elif n >= 3 and (n - ok_n) / max(1, n) > 0.3:
            print("SUGGEST: overall fail high - check LLM quality / target complexity")
        else:
            print("SUGGEST: quality good - keep observing; try higher-value targets")
        return 0

    target = os.path.normpath(os.path.join(_TRINITY_ROOT, args.target))
    # 2026-08-29（阶段2.5）：白名单扩展——scripts/ 全目录 + tests/ 辅助文件
    _allowed = (os.path.join(_TRINITY_ROOT, "scripts"),
                os.path.join(_TRINITY_ROOT, "tests"))
    if not any(target.startswith(p) for p in _allowed) or not target.endswith(".py"):
        print("REJECTED: target must be under scripts/ or tests/ and .py")
        return 2
    if not os.path.exists(target):
        print("REJECTED: target not found:", target)
        return 2
    if os.path.getsize(target) > 20000:
        print("REJECTED: target too large")
        return 2

    from trinity.llm.client import chat_completion, resolve_api_key
    key = resolve_api_key()
    if not key:
        print("REJECTED: no LLM API key")
        return 2
    with open(target, "r", encoding="utf-8") as f:
        content = f.read()
    prompt = (
        "You are an expert Python engineer. Improve the file to achieve: " + args.goal + chr(10) + chr(10) +
        "FILE CONTENT:" + chr(10) + content + chr(10) + chr(10) +
        "OUTPUT EXACTLY this format (no other text):" + chr(10) +
        "REPLACE_START" + chr(10) +
        "<exact old text to replace, copied verbatim from the file>" + chr(10) +
        "REPLACE_END" + chr(10) +
        "WITH_START" + chr(10) +
        "<new text>" + chr(10) +
        "WITH_END" + chr(10) + chr(10) +
        "Rules: old text must appear EXACTLY once in the file; keep changes minimal and safe. "
        "IMPORTANT: keep the old text SHORT (1-3 lines max, copied exactly — never truncate mid-block)."
    )
    out = ""
    _retry_count = 0
    _last_err = ""
    for attempt in range(3):
        resp = chat_completion({
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0, "max_tokens": 1200,
        }, api_key=key)
        out = resp.get("content", "")
        if "REPLACE_START" in out and "WITH_END" in out:
            break
        print("retry %d: LLM output malformed" % (attempt + 1))

    def _between(a, b):
        try:
            s = out.index(a) + len(a)
            e = out.index(b, s)
            return out[s:e]
        except Exception:
            return None

    old_text = _between("REPLACE_START", "REPLACE_END")
    new_text = _between("WITH_START", "WITH_END")
    if old_text is None or new_text is None:
        print("REJECTED: LLM output format invalid (missing REPLACE/WITH blocks)")
        _last_err = "format"
        _record_stats(False, _retry_count, _last_err, args.goal, None)
        return 1
    old_text = old_text.rstrip(chr(10))
    new_text = new_text.rstrip(chr(10))
    cnt = content.count(old_text)
    if cnt != 1:
        print("REJECTED: old text appears %d times (must be exactly 1)" % cnt)
        return 1

    patched = content.replace(old_text, new_text)
    # 冒烟验证：py_compile
    tmp = os.path.join(_PATCH_DIR if os.path.isdir(_PATCH_DIR) else _TRINITY_ROOT, "evolve_check.py")
    os.makedirs(_PATCH_DIR, exist_ok=True)
    tmp = os.path.join(_PATCH_DIR, "evolve_check.py")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(patched)
    chk = subprocess.run([sys.executable, "-m", "py_compile", tmp],
                         capture_output=True, text=True, timeout=60)
    ok = chk.returncode == 0
    if not ok:
        print("VALIDATION FAILED (py_compile):", chk.stderr[-200:])

    # 保存补丁记录
    ts = time.strftime("%Y%m%d_%H%M%S")
    patch_path = os.path.join(_PATCH_DIR, "evolve_" + ts + ".txt")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write("GOAL: " + args.goal + chr(10) + chr(10) +
                "REPLACE_START" + chr(10) + old_text + chr(10) + "REPLACE_END" + chr(10) + chr(10) +
                "WITH_START" + chr(10) + new_text + chr(10) + "WITH_END" + chr(10))
    print("patch saved:", patch_path)

    if ok and args.apply:
        with open(target, "w", encoding="utf-8") as f:
            f.write(patched)
        print("APPLIED (validated + written)")
    # 2026-08-28（阶段2）：auto 模式——合入+门禁+自动 commit/回滚
    if ok and args.auto and args.apply:
        gt = subprocess.run(["git", "-C", _TRINITY_ROOT, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
        if gt.stdout.strip():
            subprocess.run(["git", "-C", _TRINITY_ROOT, "add", "-A"], timeout=30)
            cm = subprocess.run(["git", "-C", _TRINITY_ROOT, "commit", "-m",
                                 "auto-evolve: " + args.goal[:60]],
                                capture_output=True, text=True, timeout=60)
            print("auto committed:", cm.returncode == 0)
            # 门禁：fulltest（约 10 分钟）
            gate = subprocess.run([sys.executable, "-X", "utf8",
                                   os.path.join(_TRINITY_ROOT, "scripts", "fulltest_gate.py")],
                                  cwd=_TRINITY_ROOT, timeout=1800)
            if gate.returncode != 0:
                print("GATE FAILED - reverting")
                subprocess.run(["git", "-C", _TRINITY_ROOT, "revert", "--no-edit", "HEAD"],
                               capture_output=True, timeout=60)
                ok = False
            else:
                print("GATE PASSED (1261+ tests)")
        else:
            print("auto: no changes to commit")
    _record_stats(ok, _retry_count, _last_err, args.goal, diff if ok else None)
    print("RESULT:", "OK" if ok else "REJECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

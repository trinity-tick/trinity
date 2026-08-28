# -*- coding: utf-8 -*-
"""fulltest_gate.py — 全量测试门禁（2026-08-28 阶段1, v2）。

关键：文件重定向 + cwd=trinity 根 + 继承 env（不最小化、不 capture_output）——
与手动跑完全一致的环境（capture_output/最小 env 会导致环境干扰误失败）。
用法: python scripts/fulltest_gate.py
"""
import os
import sys
import subprocess

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_TRINITY_ROOT, "temp", "fulltest_out.txt")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tests_dir = os.path.join(_TRINITY_ROOT, "tests")
    with open(_OUT, "w", encoding="utf-8") as f:
        rc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=line", tests_dir],
            cwd=_TRINITY_ROOT, timeout=1500, stdout=f, stderr=subprocess.STDOUT)
    tail = open(_OUT, encoding="utf-8", errors="replace").read()
    print(tail[-600:])
    print("pytest rc:", rc.returncode)
    if rc.returncode != 0:
        print("EVALS SKIPPED (pytest failed)")
        return rc.returncode
    with open(_OUT + "_eval", "w", encoding="utf-8") as f:
        rc2 = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(_TRINITY_ROOT, "scripts", "run_evals.py"), "--all"],
            cwd=_TRINITY_ROOT, timeout=300, stdout=f, stderr=subprocess.STDOUT)
    print(open(_OUT + "_eval", encoding="utf-8", errors="replace").read()[-400:])
    return rc2.returncode


if __name__ == "__main__":
    raise SystemExit(main())

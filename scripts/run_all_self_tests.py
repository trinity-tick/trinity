#!/usr/bin/env python3
"""
Batch Self-Test Runner for Trinity modules.

Discovers all modules under trinity/ with a ``self_test()`` function and
executes them one-by-one with a per-module timeout of 30 seconds.
Outputs a summary table of PASS / FAIL / TIMEOUT / SKIP.

Usage:
    python scripts/run_all_self_tests.py
    python scripts/run_all_self_tests.py --target trinity.modules.second_brain
"""

import argparse
import importlib
import pkgutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Dict, List, Tuple

TRINITY_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 30

# ── discovery ──────────────────────────────────────────────────


def discover_self_test_modules(target_package: str = "trinity") -> List[str]:
    """Walk trinity package tree and return module names that define self_test().

    支持两种 target：
      - 包名（如 trinity / trinity.adapters）→ 递归发现子模块
      - 模块名（如 trinity.adapters.sqlite）→ 直接返回该模块（若含 self_test）
    """
    sys.path.insert(0, str(TRINITY_ROOT))
    try:
        root_module = importlib.import_module(target_package)
    except Exception as exc:
        print(f"[ERROR] Cannot import {target_package}: {exc}")
        sys.exit(1)

    # 修复(2026-08-14): target 是模块（无 __path__）时直接判定
    if not hasattr(root_module, "__path__"):
        try:
            origin = Path(root_module.__file__)
            if origin.exists() and "def self_test" in origin.read_text(encoding="utf-8", errors="ignore"):
                return [target_package]
        except Exception:
            pass
        print(f"[!] {target_package} 是模块但无 self_test() 或无 __file__")
        return []

    candidates = []
    package_path = Path(root_module.__path__[0])

    for module_info in pkgutil.walk_packages(
        [str(package_path)],
        prefix=f"{target_package}.",
    ):
        module_path = module_info.module_finder.find_spec(module_info.name)
        if module_path is None or module_path.origin is None:
            continue
        try:
            with open(module_path.origin, "r", encoding="utf-8", errors="ignore") as fh:
                if "def self_test" in fh.read():
                    candidates.append(module_info.name)
        except Exception:
            continue

    return sorted(candidates)


# ── isolated runner (subprocess) ───────────────────────────────


def _run_single(module_name: str, root: str) -> Dict:
    """Entry point executed in a *separate process* to enforce timeout."""
    sys.path.insert(0, root)
    try:
        mod = importlib.import_module(module_name)
        if not hasattr(mod, "self_test"):
            return {"module": module_name, "result": "SKIP", "reason": "no self_test"}
        fn = getattr(mod, "self_test")
        if not callable(fn):
            return {"module": module_name, "result": "SKIP", "reason": "not callable"}
        result = fn()
        if isinstance(result, bool):
            return {"module": module_name, "result": "PASS" if result else "FAIL", "reason": str(result)}
        if isinstance(result, dict):
            ok = result.get("passed", result.get("ok", result.get("success")))
            if isinstance(ok, bool):
                return {"module": module_name, "result": "PASS" if ok else "FAIL", "reason": str(result)}
            return {"module": module_name, "result": "PASS" if ok is not False else "FAIL", "reason": str(result)}
        if isinstance(result, (int, float)):
            return {"module": module_name, "result": "PASS" if result > 0 else "FAIL", "reason": str(result)}
        if isinstance(result, (list, tuple)):
            return {"module": module_name, "result": "PASS", "reason": f"{len(result)} items"}
        return {"module": module_name, "result": "PASS" if result else "FAIL", "reason": str(result)}
    except Exception as exc:
        return {"module": module_name, "result": "FAIL", "reason": f"{type(exc).__name__}: {exc}"}


# ── main ───────────────────────────────────────────────────────


def run_all(target: str) -> None:
    """Discover and execute every self_test in *target*, one per subprocess."""
    print(f"[*] Discovering self_test() modules under {target} …")
    modules = discover_self_test_modules(target)
    if not modules:
        print("[!] No modules with self_test() found.")
        return

    print(f"[*] Found {len(modules)} modules. Running with {TIMEOUT_SECONDS}s timeout each.\n")
    results: List[Dict] = []

    for i, mod in enumerate(modules, 1):
        print(f"  [{i:3d}/{len(modules)}] {mod} … ", end="", flush=True)
        start = time.monotonic()
        # 修复(2026-08-14): 改用 subprocess 逐模块跑，超时直接杀进程——
        # 原 ProcessPoolExecutor 在模块挂死时 shutdown(wait=True) 会无限等待（曾挂 17 分钟）
        import subprocess as _sp
        _code = (
            "import sys; sys.path.insert(0, sys.argv[2]); "
            "import json, importlib; "
            "m = importlib.import_module(sys.argv[1]); "
            "r = m.self_test(); "
            "print(json.dumps({'module': sys.argv[1], 'result': 'PASS' if r else 'FAIL', 'raw': repr(r)[:300]}))"
        )
        try:
            proc = _sp.run(
                [sys.executable, "-c", _code, mod, str(TRINITY_ROOT)],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                encoding="utf-8", errors="replace",
            )
            elapsed = time.monotonic() - start
            if proc.returncode == 0 and proc.stdout.strip():
                import json as _json
                try:
                    res = _json.loads(proc.stdout.strip().splitlines()[-1])
                except Exception:
                    res = {"module": mod, "result": "PASS", "reason": proc.stdout.strip()[:200]}
            else:
                err = (proc.stderr or "").strip().splitlines()
                res = {"module": mod, "result": "FAIL",
                       "reason": (err[-1] if err else f"exit={proc.returncode}")[:300]}
            res["elapsed"] = f"{elapsed:.2f}s"
            print(f"{res['result']:>7s}  ({res['elapsed']})")
        except _sp.TimeoutExpired:
            elapsed = time.monotonic() - start
            res = {"module": mod, "result": "TIMEOUT", "reason": f">{TIMEOUT_SECONDS}s (进程已强杀)",
                   "elapsed": f"{elapsed:.2f}s"}
            print(f"{res['result']:>7s}  ({res['elapsed']})")
        except Exception as exc:
            elapsed = time.monotonic() - start
            res = {"module": mod, "result": "ERROR", "reason": str(exc), "elapsed": f"{elapsed:.2f}s"}
            print(f"{res['result']:>7s}  ({res['elapsed']})")
        results.append(res)

    # ── summary table ──
    print("\n" + "=" * 80)
    print(f"{'SUMMARY':^80}")
    print("=" * 80)
    print(f"{'Module':<60} {'Result':>8}  {'Time':>8}")
    print("-" * 80)
    for r in results:
        print(f"{r['module']:<60} {r['result']:>8}  {r.get('elapsed', 'N/A'):>8}")

    # aggregate
    counts: Dict[str, int] = {}
    failed_modules: List[str] = []
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
        if r["result"] in ("FAIL", "TIMEOUT", "ERROR"):
            failed_modules.append(f"  - {r['module']}  [{r['result']}]  {r.get('reason', '')}")

    print("-" * 80)
    parts = "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    print(f"TOTAL: {len(results)}    {parts}")
    if failed_modules:
        print(f"\nDetails for FAIL / TIMEOUT / ERROR:")
        for line in failed_modules:
            print(line)
    else:
        print("\nAll tests passed.")

    # exit code
    if counts.get("FAIL", 0) + counts.get("TIMEOUT", 0) + counts.get("ERROR", 0) > 0:
        sys.exit(1)


# ── CLI ────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trinity Batch Self-Test Runner")
    parser.add_argument(
        "--target",
        default="trinity",
        help="Top-level package to walk (default: trinity)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=TIMEOUT_SECONDS,
        help=f"Per-module timeout in seconds (default: {TIMEOUT_SECONDS})",
    )
    args = parser.parse_args()
    TIMEOUT_SECONDS = args.timeout
    run_all(args.target)

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
    """Walk trinity package tree and return module names that define self_test()."""
    sys.path.insert(0, str(TRINITY_ROOT))
    try:
        root_module = importlib.import_module(target_package)
    except Exception as exc:
        print(f"[ERROR] Cannot import {target_package}: {exc}")
        sys.exit(1)

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
        try:
            with ProcessPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_single, mod, str(TRINITY_ROOT))
                res = future.result(timeout=TIMEOUT_SECONDS)
            elapsed = time.monotonic() - start
            res["elapsed"] = f"{elapsed:.2f}s"
            print(f"{res['result']:>7s}  ({res['elapsed']})")
        except FuturesTimeoutError:
            elapsed = time.monotonic() - start
            res = {"module": mod, "result": "TIMEOUT", "reason": f">{TIMEOUT_SECONDS}s", "elapsed": f"{elapsed:.2f}s"}
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

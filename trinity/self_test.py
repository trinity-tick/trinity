"""
Trinity v8.0.0 — Unified Self-Test Runner
==========================================
Orchestrates self_test() across all core v8.0 packages and
returns aggregate pass/fail with detailed per-component breakdown.

Usage:
    python -m trinity.self_test
    python -m trinity.self_test --json       # JSON output only
    python -m trinity.self_test --quiet      # Minimal output
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, List


def run_all_self_tests() -> Dict[str, Any]:
    """Execute self_tests for all five core components.

    Returns:
        {
            "overall": pass | fail | error,
            "components": [...],
            "summary": str,
            "duration_ms": float,
        }
    """
    components: List[Dict[str, Any]] = []
    started = time.time()

    # ── 1. HybridRouter (no DB dependency) ──────────────────────
    try:
        from trinity.identity.hybrid_router import HybridRouter
        router = HybridRouter()
        result = router.self_test()
        result["component"] = "HybridRouter"
        components.append(result)
    except Exception as e:
        components.append({
            "component": "HybridRouter",
            "pass": False,
            "checks": [{"name": "import", "pass": False, "detail": str(e)}],
            "summary": f"HybridRouter self-test: import failed — {e}",
        })

    # ── 2. CapabilityRegistry ───────────────────────────────────
    try:
        from trinity.a2a.capability_registry import CapabilityRegistry
        reg = CapabilityRegistry(adapter=None)
        result = reg.self_test()
        result["component"] = "CapabilityRegistry"
        components.append(result)
    except Exception as e:
        components.append({
            "component": "CapabilityRegistry",
            "pass": False,
            "checks": [{"name": "import", "pass": False, "detail": str(e)}],
            "summary": f"CapabilityRegistry self-test: import failed — {e}",
        })

    # ── 3. TaskManager ──────────────────────────────────────────
    try:
        from trinity.a2a.task_manager import TaskManager
        tm = TaskManager(adapter=None)
        result = tm.self_test()
        result["component"] = "TaskManager"
        components.append(result)
    except Exception as e:
        components.append({
            "component": "TaskManager",
            "pass": False,
            "checks": [{"name": "import", "pass": False, "detail": str(e)}],
            "summary": f"TaskManager self-test: import failed — {e}",
        })

    # ── 4. IdentityManager ──────────────────────────────────────
    try:
        from trinity.identity.identity_manager import IdentityManager
        mgr = IdentityManager(storage_adapter=None)
        result = mgr.self_test()
        result["component"] = "IdentityManager"
        components.append(result)
    except Exception as e:
        components.append({
            "component": "IdentityManager",
            "pass": False,
            "checks": [{"name": "import", "pass": False, "detail": str(e)}],
            "summary": f"IdentityManager self-test: import failed — {e}",
        })

    # ── 5. Auditor ──────────────────────────────────────────────
    try:
        from trinity.audit.auditor import Auditor
        auditor = Auditor(adapter=None)
        result = auditor.self_test()
        result["component"] = "Auditor"
        components.append(result)
    except Exception as e:
        components.append({
            "component": "Auditor",
            "pass": False,
            "checks": [{"name": "import", "pass": False, "detail": str(e)}],
            "summary": f"Auditor self-test: import failed — {e}",
        })

    duration_ms = round((time.time() - started) * 1000, 1)

    all_pass = all(c["pass"] for c in components)
    if all_pass:
        overall = "pass"
    elif any(not c["pass"] for c in components):
        overall = "fail"
    else:
        overall = "error"

    total_checks = sum(len(c.get("checks", [])) for c in components)
    passed_checks = sum(
        sum(1 for ch in c.get("checks", []) if ch["pass"])
        for c in components
    )

    return {
        "overall": overall,
        "components": components,
        "summary": f"{passed_checks}/{total_checks} checks passed across {len(components)} components",
        "duration_ms": duration_ms,
    }


def print_table(results: Dict[str, Any]) -> None:
    """Print formatted results table."""
    print()
    print("=" * 72)
    print("  Trinity Memory OS v8.0.0 — Self-Test Results")
    print("=" * 72)
    print(f"  Overall:  {'PASS' if results['overall'] == 'pass' else 'FAIL'}")
    print(f"  Duration: {results['duration_ms']} ms")
    print(f"  Summary:  {results['summary']}")
    print("-" * 72)
    print(f"  {'Component':<24s} {'Result':>8s}  Checks")
    print("-" * 72)

    for c in results["components"]:
        status = "PASS" if c["pass"] else "FAIL"
        check_summary = c.get("summary", "").split(": ")[-1] if ":" in c.get("summary", "") else c.get("summary", "")
        print(f"  {c['component']:<24s} {status:>8s}  {check_summary}")

    print("-" * 72)

    # Print failed check details
    failed_any = False
    for c in results["components"]:
        for ch in c.get("checks", []):
            if not ch["pass"]:
                if not failed_any:
                    print("\n  Failed Checks Detail:")
                    failed_any = True
                print(f"    [{c['component']}] {ch['name']}: {ch['detail']}")

    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description="Trinity Self-Test Runner")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    args = parser.parse_args()

    import logging
    logging.disable(logging.CRITICAL)

    results = run_all_self_tests()

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif args.quiet:
        status = "PASS" if results["overall"] == "pass" else "FAIL"
        print(f"Trinity Self-Test: {status} — {results['summary']}")
    else:
        print_table(results)

    sys.exit(0 if results["overall"] == "pass" else 1)


if __name__ == "__main__":
    main()

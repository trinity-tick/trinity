# -*- coding: utf-8 -*-
"""全场景回归：依次运行全部已构建工具脚本，收集每个的 exit code 与结果摘要。"""
import subprocess
import sys

PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
ROOT = r"C:\Users\Administrator\trinity"
RESULTS = []


def run(name: str, args: list, timeout: int = 300) -> None:
    print(f"\n===== {name} =====", flush=True)
    try:
        r = subprocess.run([PY] + args, capture_output=True, text=True, timeout=timeout,
                           cwd=ROOT, encoding="utf-8", errors="replace")
        tail = (r.stdout or "")[-600:].strip()
        err = (r.stderr or "")[-300:].strip()
        ok = r.returncode == 0
        RESULTS.append({"name": name, "ok": ok, "exit": r.returncode})
        print(f"[{'PASS' if ok else 'FAIL'}] exit={r.returncode}")
        if tail:
            print("  输出尾:", tail.replace("\n", " | ")[:400])
        if err and not ok:
            print("  错误尾:", err.replace("\n", " | ")[:300])
    except subprocess.TimeoutExpired:
        RESULTS.append({"name": name, "ok": False, "exit": "TIMEOUT"})
        print(f"[FAIL] TIMEOUT ({timeout}s)")


def main() -> None:
    tools = [
        ("A1.3 membench_report", ["benchmark/membench_report.py"], 120),
        ("A2 adaptive_routing", ["benchmark/adaptive_routing.py"], 300),
        ("A3 consistency_stress(dry)", ["benchmark/consistency_stress.py"], 120),
        ("A5 compress_economics", ["benchmark/compress_economics.py", "--samples", "5"], 300),
        ("B1 gateway client smoke", ["gateway/client.py"], 120),
        ("B1 gateway demo_app", ["gateway/demo_app.py"], 180),
        ("B3 governance", ["governance/governance.py"], 120),
        ("B4 federation export+diff", ["federation/sync_protocol.py", "export",
                                       "--api", "http://127.0.0.1:8001", "--agent", "default",
                                       "--out", r"C:\Users\Administrator\.trinity\bench-results\reg_fed.json"], 120),
        ("B5 compliance audit", ["compliance/audit.py"], 120),
        ("C1 market demo", ["market/demo.py", "report"], 60),
        ("C2 harvester dry-run", ["harvesters/example_plugin.py", "--dry-run"], 60),
        ("C3 leaderboard validate", ["benchmark/leaderboard/validate.py"], 60),
        ("C3 leaderboard build", ["benchmark/leaderboard/build.py"], 60),
        ("memsyco pipeline(dry)", ["benchmark/memsyco_evaluator.py", "--dry-run", "--output-dir",
                                   r"C:\Users\Administrator\.trinity\bench-results\20260814_v2baseline"], 120),
    ]
    for name, args, t in tools:
        run(name, args, t)

    print("\n" + "=" * 50)
    ok_n = sum(1 for r in RESULTS if r["ok"])
    print(f"工具回归汇总: {ok_n}/{len(RESULTS)} 通过")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} (exit={r['exit']})")
    sys.exit(0 if ok_n == len(RESULTS) else 1)


if __name__ == "__main__":
    main()

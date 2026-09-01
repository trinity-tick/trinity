#!/usr/bin/env python3
"""trinity-embed-observe.py — Ollama 解耦观察期检查（2026-09，EXECUTION 104.6）

检查项：
  1. /health：status/engine/vector/tier/engine_error（硬指标）
  2. 3 个固定查询抽样 /memory/search/hybrid（rrf top5），与基线对比 count 漂移（软指标）
  3. netstat 统计连 11434 的 ESTABLISHED pid（软指标——第 3 步停 Ollama 后应为空）
首次运行创建基线 ~/.trinity/observe/embed_baseline.json，之后每次对比。
退出码：0=硬指标全绿；1=health 异常或查询失败。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8001"
OBS = os.path.join(os.path.expanduser("~"), ".trinity", "observe")
os.makedirs(OBS, exist_ok=True)
BL = os.path.join(OBS, "embed_baseline.json")
QUERIES = ["Trinity 记忆系统 多租户", "用户偏好 咖啡", "PostgreSQL 主存储 切换"]


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8")), time.time() - t0


report = {}
ok = True

# 1) health (hard)
try:
    with urllib.request.urlopen(BASE + "/health", timeout=10) as resp:
        h = json.loads(resp.read().decode("utf-8"))
    deg = h.get("degradation", {}) or {}
    comp = h.get("components", {}) or {}
    report["health"] = {
        "status": h.get("status"), "engine": comp.get("engine"),
        "vector": (deg.get("health") or {}).get("vector"),
        "tier": deg.get("tier"), "engine_error": h.get("engine_error"),
    }
    if not (h.get("status") == "ok" and comp.get("engine") == "healthy"
            and (deg.get("health") or {}).get("vector") is True
            and not h.get("engine_error")):
        ok = False
except Exception as e:  # noqa: BLE001
    report["health"] = {"error": str(e)}
    ok = False

# 2.5) warmup（2026-09-01 修复：冷启动首查曾 >60s 超时导致 observe FAILED——
#      先打一发预热查询，不计入 samples、失败也不影响退出码，只记录延迟）
try:
    _, _warm_dt = post(BASE + "/memory/search/hybrid",
                       {"query": QUERIES[0], "top_k": 5, "strategy": "rrf"})
    report["warmup"] = {"latency_s": round(_warm_dt, 2)}
except Exception as _we:  # noqa: BLE001
    report["warmup"] = {"error": str(_we)}

# 2) sample queries
samples = []
for q in QUERIES:
    try:
        data, dt = post(BASE + "/memory/search/hybrid",
                        {"query": q, "top_k": 5, "strategy": "rrf"})
        mids = [r.get("memory_id") for r in (data.get("results") or [])]
        samples.append({"query": q, "latency_s": round(dt, 2),
                        "count": len(mids), "top1": mids[0] if mids else None})
    except Exception as e:  # noqa: BLE001
        samples.append({"query": q, "error": str(e)})
        ok = False
report["samples"] = samples

# baseline compare (soft)
if os.path.exists(BL):
    try:
        with open(BL, encoding="utf-8") as f:
            base = json.load(f)
        drift = []
        for s in samples:
            b = next((x for x in base.get("samples", [])
                      if x.get("query") == s.get("query")), None)
            if b and s.get("count") != b.get("count"):
                drift.append({"query": s["query"],
                              "base_count": b.get("count"),
                              "now_count": s.get("count")})
        report["drift"] = drift
    except Exception as e:  # noqa: BLE001
        report["baseline_error"] = str(e)
else:
    with open(BL, "w", encoding="utf-8") as f:
        json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "samples": samples}, f, ensure_ascii=False, indent=1)
    report["baseline"] = "created (first run)"

# 3) ollama connections (soft)
pids = []
try:
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         text=True, timeout=20).stdout or ""
    for line in out.splitlines():
        if "11434" in line and "ESTABLISHED" in line:
            parts = line.split()
            if len(parts) >= 5:
                pids.append(parts[-1])
    report["ollama_established_pids"] = pids
except Exception as e:  # noqa: BLE001
    report["ollama_check_error"] = str(e)

print(json.dumps(report, ensure_ascii=False, indent=1))
sys.exit(0 if ok else 1)

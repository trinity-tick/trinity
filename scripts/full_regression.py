# -*- coding: utf-8 -*-
"""全场景回归：核心 API 端点 + 数据完整性探测。

覆盖: 健康/通道、记忆写查删+去重重写、hybrid 检索、图谱、跨模态(降级)、
审计、身份、市场、进化、压缩、导出、嵌入、向量、会话。
输出: 控制台 PASS/FAIL 清单 + 完整 JSON 报告。
"""
import json
import sys
import time
import requests

API = "http://127.0.0.1:8001"
H = {"X-Agent-ID": "regression", "X-Agent-Role": "admin"}
REPORT = {"checks": []}


def check(name: str, fn) -> dict:
    t0 = time.time()
    try:
        detail = fn()
        ok = True
    except Exception as exc:
        ok, detail = False, {"error": str(exc)[:200]}
    entry = {"name": name, "ok": ok, "elapsed_ms": round((time.time() - t0) * 1000, 1), "detail": detail}
    REPORT["checks"].append(entry)
    print(f"  [{'OK ' if ok else 'FAIL'}] {name} ({entry['elapsed_ms']}ms)")
    return entry


def get(path, **kw):
    timeout = kw.pop("timeout", 30)
    r = requests.get(f"{API}{path}", headers=H, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


def post(path, **kw):
    timeout = kw.pop("timeout", 60)
    r = requests.post(f"{API}{path}", headers=H, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


def delete(path, **kw):
    r = requests.delete(f"{API}{path}", headers=H, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def main() -> None:
    print("== 1. 健康与数据完整性 ==")
    check("health", lambda: get("/health")["status"] == "ok")
    check("stats", lambda: get("/memories/stats")["total_memories"] > 1000)
    check("pool", lambda: get("/agents/memory/pool")["total_memories"] > 1000)
    check("api/stats 图谱", lambda: get("/api/stats")["adapter"]["entity_count"] > 1000)
    check("diagnostics", lambda: get("/diagnostics") or True)

    print("== 2. 记忆生命周期 ==")
    def write_search_delete():
        c = f"回归测试记忆-{int(time.time())}"
        w = post("/memories", json={"content": c, "tags": ["regression"]})
        mid = w["memory_id"]
        assert mid, "no memory_id"
        s = post("/memory/search/hybrid", json={"query": c[:8], "top_k": 3, "strategy": "rrf"})
        assert s.get("results"), "hybrid no results"
        # 软删后同内容重写（去重约束回归）
        delete(f"/memories/{mid}")
        w2 = post("/memories", json={"content": c, "tags": ["regression"]})
        assert w2.get("memory_id") and not w2.get("error"), f"rewrite failed: {w2}"
        delete(f"/memories/{w2['memory_id']}")
        return {"mid": mid, "rewrite_ok": True}
    check("写→搜→软删→重写", write_search_delete)
    check("get_memory(id)", lambda: get(f"/memories/{post('/memories', json={'content': '回归-get', 'tags': ['r']})['memory_id']}"))
    check("versions", lambda: get(f"/memories/{post('/memories', json={'content': '回归-ver', 'tags': ['r']})['memory_id']}/versions"))
    check("session 写入", lambda: post("/memories/session", json={
        "session_id": f"reg-{int(time.time())}",
        "turns": [{"speaker": "user", "text": "回归会话内容"}, {"speaker": "assistant", "text": "收到"}]}))

    print("== 3. 图谱 ==")
    def graph_cycle():
        e = post("/graph/entities", json={"name": f"回归实体{int(time.time())}", "type": "concept"})
        eid = e["id"]
        post("/graph/relations", json={"subject_id": eid, "predicate": "测试关系", "object_id": eid})
        t = get(f"/graph/traverse", params={"start_id": eid, "max_hops": 1})
        return {"entity": eid, "nodes": len(t.get("nodes", []))}
    check("实体+关系+遍历", graph_cycle)
    check("实体搜索", lambda: get("/graph/entities/search", params={"limit": 5}))

    print("== 4. 跨模态（降级，首次构造允许 120s）==")
    check("cross-modal", lambda: post("/memory/search/cross-modal", json={"query": "仓库", "query_type": "text", "top_k": 3}, timeout=120))
    check("image-by-text", lambda: post("/memory/search/image-by-text", json={"text": "仓库", "top_k": 3}, timeout=120))
    check("text-by-image", lambda: post("/memory/search/text-by-image", json={"image_path": "nope.png", "top_k": 3}, timeout=120))

    print("== 5. 审计 / 身份 / 治理 ==")
    check("audit/summary", lambda: get("/audit/summary"))
    check("audit/timeline", lambda: get("/audit/timeline"))
    check("audit/integrity", lambda: get("/audit/integrity"))
    check("audit/violations", lambda: get("/audit/violations"))
    check("identity/profiles", lambda: get("/identity/profiles"))
    check("identity/bundles/export", lambda: post("/identity/bundles/export", json={"agent_id": "default"}))
    check("agents/weights", lambda: get("/agents/weights"))
    check("agents/insights", lambda: get("/agents/memory/insights"))

    print("== 6. 市场 / 进化 / 压缩 / 导出 ==")
    check("market/report", lambda: post("/market/report", json={"from_agent": "reg", "to_agent": "reg", "reason": "reg"}))
    check("market/reputation", lambda: get("/market/reputation/default"))
    check("evolution/stats", lambda: get("/evolution/stats"))
    check("evolution/heatmap", lambda: get("/evolution/heatmap"))
    check("compress/stats", lambda: post("/memory/compress/stats", json={"agent_id": "default"}))
    check("agents/memory/export", lambda: get("/agents/memory/export", params={"format": "json"}))
    check("memories/dedup/stats", lambda: get("/memories/dedup/stats"))

    print("== 7. 嵌入 / 向量 / 检索 ==")
    check("embeddings", lambda: post("/embeddings", json={"text": "回归嵌入测试"}))
    check("vector/search", lambda: post("/vector/search", json={"query": "回归", "top_k": 3}))
    check("agents/memory/search", lambda: get("/agents/memory/search", params={"q": "回归", "top_k": 3}))

    print("== 8. 其它端点抽查 ==")
    def metrics_text():
        r = requests.get(f"{API}/metrics", headers=H, timeout=30)
        r.raise_for_status()
        return {"content_type": r.headers.get("Content-Type", ""), "len": len(r.text)}
    check("metrics(文本)", metrics_text)
    check("dashboard", lambda: get("/dashboard"))
    check("benchmark(长任务)", lambda: post("/benchmark", timeout=180))
    check("graphql", lambda: post("/graphql", json={"query": "{ health { status } }"}))

    n_ok = sum(1 for c in REPORT["checks"] if c["ok"])
    print(f"\n== 汇总: {n_ok}/{len(REPORT['checks'])} 通过 ==")
    for c in REPORT["checks"]:
        if not c["ok"]:
            print(f"  FAIL: {c['name']} -> {c['detail']}")
    out = r"C:\Users\Administrator\.trinity\bench-results\regression_api.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(REPORT, f, ensure_ascii=False, indent=2)
    print(f"报告: {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

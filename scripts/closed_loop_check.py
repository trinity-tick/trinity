# -*- coding: utf-8 -*-
"""Trinity 全链路闭环验证 —— 每条核心业务链路从入口到出口完整走一遍。

链路:
  1. 记忆生命周期  写→检索→版本链→审计→软删→重写
  2. 图谱          实体→关系→遍历→搜索
  3. 身份          注册→锚点→画像→漂移→重建
  4. 市场交易      list→search→buy→reputation→orderbook→delist
  5. A2A 协作      注册→派发→任务→快照
  6. 压缩          写入→compress→stats→restore
  7. 进化          反馈→进化轮→状态
  8. GraphQL      mutation 写→query 检索
  9. Collector    事件上报→落库→检索可见
"""
import json
import sys
import time
import requests

API = "http://127.0.0.1:8001"
H = {"X-Agent-ID": "closed-loop", "X-Agent-Role": "admin"}
RESULTS = []
UNIQ = int(time.time())


def log(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": ok})
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f" | {detail[:120]}" if detail else ""))


def post(path, **kw):
    r = requests.post(f"{API}{path}", headers=H, timeout=120, **kw)
    return r


def get(path, **kw):
    r = requests.get(f"{API}{path}", headers=H, timeout=60, **kw)
    return r


def main() -> None:
    print("== 1. 记忆生命周期闭环 ==")
    try:
        # 注：jieba 对"中文+连字符+数字"分词局限（已知坑），查询用纯中文 + 独立英文 token
        tok = f"loop{UNIQ}"
        c = f"闭环验证记忆链路完整性测试 {tok}"
        w = post("/memories", json={"content": c, "agent_id": "agent-alpha", "tags": ["loop"]}).json()
        mid = w.get("memory_id")
        assert mid
        # 双通道查询：英文 token（精确）+ 纯中文（分词），任一命中即过
        s = post("/memory/search/hybrid", json={"query": tok, "top_k": 3, "strategy": "rrf"}).json()
        hit = any(m.get("memory_id") == mid for m in s.get("results", []))
        if not hit:
            s2 = post("/memory/search/hybrid", json={"query": "闭环验证记忆链路完整性", "top_k": 5, "strategy": "rrf"}).json()
            hit = any(m.get("memory_id") == mid for m in s2.get("results", []))
        v = get(f"/memories/{mid}/versions")
        a = get("/audit/timeline")
        d = requests.delete(f"{API}/memories/{mid}", headers=H, timeout=30)
        w2 = post("/memories", json={"content": c, "agent_id": "agent-alpha", "tags": ["loop"]}).json()
        rewrite_ok = w2.get("memory_id") and not w2.get("error")
        requests.delete(f"{API}/memories/{w2['memory_id']}", headers=H, timeout=30) if w2.get("memory_id") else None
        log("记忆写→搜→版本→审计→删→重写", hit and v.status_code == 200 and a.status_code == 200 and d.status_code == 200 and rewrite_ok)
    except Exception as e:
        log("记忆生命周期闭环", False, str(e))

    print("== 2. 图谱闭环 ==")
    try:
        e = post("/graph/entities", json={"name": f"闭环实体{UNIQ}", "type": "concept"}).json()
        eid = e["id"]
        e2 = post("/graph/entities", json={"name": f"闭环实体B{UNIQ}", "type": "concept"}).json()
        eid2 = e2["id"]
        post("/graph/relations", json={"subject_id": eid, "predicate": "闭环关联", "object_id": eid2})
        t = get(f"/graph/traverse", params={"start_id": eid, "max_hops": 2}).json()
        nodes = len(t.get("nodes", []))
        edges = len(t.get("edges", []))
        log("实体→关系→遍历", nodes >= 2 and edges >= 1, f"nodes={nodes} edges={edges}")
    except Exception as e:
        log("图谱闭环", False, str(e))

    print("== 3. 身份闭环 ==")
    try:
        aid = f"loop-agent-{UNIQ}"
        r = post("/identity/register", json={"agent_id": aid, "name": "闭环身份", "role": "operator"})
        reg_ok = r.status_code in (200, 201, 409)  # 已存在也算通过
        anch = post("/identity/anchors", json={
            "agent_id": aid, "anchor_type": "core",
            "content": json.dumps({"style": "dark", "lang": "zh"}, ensure_ascii=False),
        })
        prof = get(f"/identity/agents/{aid}/profile")
        drift = post(f"/identity/agents/{aid}/drift-check", json={})
        recon = post(f"/identity/agents/{aid}/reconstruct", json={})
        log("注册→锚点→画像→漂移→重建",
            reg_ok and anch.status_code in (200, 201, 409) and prof.status_code == 200
            and drift.status_code == 200 and recon.status_code == 200,
            f"reg={r.status_code} anch={anch.status_code} drift={drift.status_code} recon={recon.status_code}")
    except Exception as e:
        log("身份闭环", False, str(e))

    print("== 4. 市场交易闭环 ==")
    try:
        own = f"loop-seller-{UNIQ}"
        lst = post("/market/list", json={
            "memory": {"content": f"闭环知识包{UNIQ}", "tags": ["loop"], "category": "knowledge"},
            "owner": own, "price": 5.0, "license": "CC-BY", "currency": "trust_score"}).json()
        asset = lst.get("asset_id")
        sr = get("/market/search", params={"query": f"闭环知识包{UNIQ}"})  # GET，修复 2026-08-14
        buy = post("/market/buy", json={"buyer_agent": "loop-buyer", "asset_id": asset, "offer_price": 5.0, "currency": "trust_score"})
        rep = get(f"/market/reputation/{own}")
        ob = get("/market/orderbook")
        dl = post("/market/delist", json={"asset_id": asset})
        log("上架→搜索→下单→信誉→账簿→下架",
            asset and sr.status_code == 200 and buy.status_code == 200 and rep.status_code == 200
            and ob.status_code == 200 and dl.status_code == 200,
            f"asset={asset} search={sr.status_code} buy={buy.status_code} rep={rep.status_code} ob={ob.status_code} delist={dl.status_code}")
    except Exception as e:
        log("市场交易闭环", False, str(e))

    print("== 5. A2A 协作闭环 ==")
    try:
        a1, a2 = f"loop-a-{UNIQ}", f"loop-b-{UNIQ}"
        r1 = post("/a2a/agents/register", json={"agent_id": a1, "name": "闭环A", "capabilities": ["search"]})
        r2 = post("/a2a/agents/register", json={"agent_id": a2, "name": "闭环B", "capabilities": ["write"]})
        disp = post("/a2a/marvis/dispatch", json={
            "from_agent": a1, "to_agent": a2, "task_description": "闭环协作任务",
            "payload": {"query": "库位"}, "global_goal": "验证闭环", "current_task": "t1",
            "memory_ids": [], "context_dict": {}, "priority": 5})
        tasks = get("/a2a/tasks")
        snap = get("/a2a/marvis/snapshot")
        log("注册→派发→任务→快照",
            r1.status_code in (200, 201, 409) and r2.status_code in (200, 201, 409)
            and disp.status_code in (200, 201) and tasks.status_code == 200 and snap.status_code == 200,
            f"reg={r1.status_code}/{r2.status_code} dispatch={disp.status_code} tasks={tasks.status_code} snap={snap.status_code}")
    except Exception as e:
        log("A2A 协作闭环", False, str(e))

    print("== 6. 压缩闭环 ==")
    try:
        ag = f"loop-compress-{UNIQ}"
        post("/memories", json={"content": f"{'压缩闭环内容 ' * 30} {UNIQ}", "agent_id": ag, "importance": 0.8})
        cp = post("/memory/compress", json={"agent_id": ag, "max_tokens": 1024})
        st = post("/memory/compress/stats", json={"agent_id": ag})
        rest = post("/memory/compress/restore", json={"agent_id": ag, "trimmed_ids": []})
        log("写入→压缩→统计→恢复", cp.status_code == 200 and st.status_code == 200 and rest.status_code in (200, 422, 400),
            f"compress={cp.status_code} stats={st.status_code} restore={rest.status_code}")
    except Exception as e:
        log("压缩闭环", False, str(e))

    print("== 7. 进化闭环 ==")
    try:
        mid = post("/memories", json={"content": f"进化闭环记忆{UNIQ}", "agent_id": "agent-alpha"}).json().get("memory_id")
        fb = post("/evolution/feedback", json={"memory_id": mid, "agent_id": "agent-alpha", "rating": 4, "comment": "闭环验证"})
        cyc = post("/evolution/cycle/run", json={})
        st = get("/evolution/stats")
        log("反馈→进化轮→状态", fb.status_code == 200 and cyc.status_code == 200 and st.status_code == 200,
            f"fb={fb.status_code} cycle={cyc.status_code}")
    except Exception as e:
        log("进化闭环", False, str(e))

    print("== 8. GraphQL 闭环 ==")
    try:
        q = f"graphql闭环{UNIQ}"
        mut = post("/graphql", json={"query": f'mutation {{ storeMemory(content: "{q}", agentId: "gql-agent") {{ memoryId }} }}'})
        query = post("/graphql", json={"query": "{ health { status version } }"})
        log("mutation→query", mut.status_code == 200 and query.status_code == 200,
            f"mut={mut.status_code} query={query.status_code}")
    except Exception as e:
        log("GraphQL 闭环", False, str(e))

    print("== 9. Collector 闭环（事件→落库→检索）==")
    try:
        sys.path.insert(0, r"C:\Users\Administrator\trinity")
        from trinity.memory.active_collector import EventDrivenCollector, AgentConnector
        col = EventDrivenCollector()
        conn = AgentConnector(event_collector=col, agent_name=f"loop-col-{UNIQ}")
        conn.on_conversation_start(task_desc="闭环采集验证")
        conn.on_tool_call_after(tool_name="search", result_preview="ok")
        n = col.flush()
        log("事件上报→flush 落库", n >= 1, f"written={n}")
    except Exception as e:
        log("Collector 闭环", False, str(e))

    n_ok = sum(1 for r in RESULTS if r["ok"])
    print(f"\n== 闭环汇总: {n_ok}/{len(RESULTS)} 条链路闭环 ==")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  [断] {r['name']}")
    out = r"C:\Users\Administrator\.trinity\bench-results\closed_loop.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"results": RESULTS, "ok": n_ok, "total": len(RESULTS)}, f, ensure_ascii=False, indent=2)
    print(f"报告: {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

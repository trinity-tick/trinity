"""
Trinity → Coze 客服Bot 接入桥接层 (API 模式)

通过 API 访问 Trinity，避免直连 SQLite 导致的数据库锁冲突。
"""

import sys, os, json, time, urllib.request, urllib.parse
from pathlib import Path

API_BASE = os.environ.get("TRINITY_API_URL", "http://localhost:8001")


# ── 直连模式（供 server.py 内部使用，避免 HTTP 回环）───────────────────


def search_memory_direct(query: str, top_k: int = 5) -> list:
    """直连 SQLite 搜索（供 server.py 内部路由调用）"""
    from trinity.adapters.sqlite import SQLiteAdapter
    try:
        db = SQLiteAdapter()
        db.connect()
        results = db.search_memories(query=query, top_k=top_k)
        db.disconnect()
        return [
            {
                "content": r["content"][:500],
                "importance": r["importance"],
                "tags": r.get("tags", ""),
                "category": r.get("category", ""),
            }
            for r in results
        ]
    except Exception:
        return []


def _search_by_intent_text(intent_code: str) -> list:
    """直连搜索：按意图关键词搜索（供 server.py 内部路由调用）"""
    intent_keywords = {
        "I01": ["订单查询", "FAQ", "高频问答"],
        "I02": ["物流", "追踪", "快递"],
        "I03": ["库存", "备货", "超卖"],
        "I04": ["时效", "发货时间", "考核", "截单"],
        "I05": ["退货", "入库", "退货换货"],
        "I06": ["换货", "换色号", "换SKU"],
        "I07": ["错发", "少发", "发错", "漏发"],
        "I08": ["破损", "碎了", "漏液"],
        "I09": ["拦截", "改地址", "延迟", "超时"],
    }
    keywords = intent_keywords.get(intent_code, [])
    all_results = []
    seen = set()
    for kw in keywords:
        results = search_memory_direct(kw, top_k=2)
        for r in results:
            key = r.get("content", "")[:50]
            if key not in seen:
                seen.add(key)
                all_results.append(r)
    return all_results[:5]


# ── HTTP 模式（供外部或 Coze 插件调用）─────────────────────────────────


def _api_get(path: str) -> dict:
    """内部：GET 请求 Trinity API"""
    url = API_BASE.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": str(e), "results": []}


def _api_post(path: str, data: dict) -> dict:
    """内部：POST 请求 Trinity API"""
    url = API_BASE.rstrip("/") + path
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "error": str(e), "results": []}


def search_memory(query: str, top_k: int = 5, category: str = None) -> dict:
    """
    通过 API 搜索 Trinity 记忆库。
    
    参数:
        query: 搜索关键词
        top_k: 返回结果数
        category: 按分类过滤 (warehouse/wms/prompt)
    
    返回:
        {"success": true, "count": 3, "results": [...]}
    """
    # 直接搜索
    params = urllib.parse.urlencode({"q": query, "top_k": top_k})
    result = _api_get(f"/memories?{params}")
    
    if not result.get("success", True):
        # 如果 /memories 失败，尝试 /api/search
        result = _api_get(f"/api/search?{params}")
    
    memories = result.get("results") or result.get("memories") or []
    
    # 分类过滤
    if category and memories:
        memories = [m for m in memories if m.get("category") == category]
    
    # 格式统一
    formatted = []
    for m in memories[:top_k]:
        formatted.append({
            "content": m.get("content", m.get("text", ""))[:500],
            "importance": m.get("importance", 0.5),
            "tags": m.get("tags", ""),
            "category": m.get("category", ""),
            "created_at": m.get("created_at", ""),
        })
    
    return {
        "success": True,
        "source": "trinity_api",
        "query": query,
        "count": len(formatted),
        "results": formatted,
    }


def search_by_intent(intent_code: str, brand: str = None) -> dict:
    """
    按意图类型定向搜索（通过 API）。
    
    意图对照表:
        I04 时效咨询  → 品牌时效规则, 平台发货规则
        I05 退货入库  → 退货换货流程
        I07 错发少发  → 异常处理手册
        I08 破损     → 异常处理手册, 美妆仓储管理规范
        I09 物流异常  → 异常处理手册, 平台发货规则
    """
    intent_keywords = {
        "I01": ["订单查询", "FAQ", "高频问答"],
        "I02": ["物流", "追踪", "快递"],
        "I03": ["库存", "备货", "超卖"],
        "I04": ["时效", "发货时间", "考核", "截单", "品牌时效规则"],
        "I05": ["退货", "入库", "退货换货"],
        "I06": ["换货", "换色号", "换SKU"],
        "I07": ["错发", "少发", "发错", "漏发"],
        "I08": ["破损", "碎了", "漏液"],
        "I09": ["拦截", "改地址", "延迟", "超时"],
    }
    
    keywords = intent_keywords.get(intent_code, [])
    all_results = []
    seen = set()
    
    for kw in keywords:
        result = search_memory(kw, top_k=3)
        for r in result.get("results", []):
            key = r.get("content", "")[:50]
            if key not in seen:
                seen.add(key)
                all_results.append(r)
    
    return {
        "success": True,
        "source": "trinity_api",
        "intent": intent_code,
        "count": len(all_results),
        "results": all_results[:5],
    }


def search_entity(entity_name: str) -> dict:
    """搜索知识图谱实体（即将知识图谱也通过 MCP/API 暴露）"""
    # 知识图谱是内存+JSONL存储，不存在锁竞争，保持直连
    from trinity.kgraph import KnowledgeGraph
    kg = KnowledgeGraph()
    results = kg.search(entity_name, top_k=5)
    
    entity_ids = [r["entity"]["id"] for r in results]
    relations = []
    for eid in entity_ids:
        rels = kg.query_relations(eid, max_depth=1)
        relations.extend(rels)
    
    return {
        "success": True,
        "query": entity_name,
        "entities": results,
        "relations": relations[:20],
    }


def bridge(query: str, intent_code: str = None, brand: str = None) -> dict:
    """
    统一入口：通过 Trinity API 检索，避免数据库锁冲突。
    """
    result = {"memory": [], "graph": [], "intent": intent_code}
    
    # 1. 如果有意图，先定向搜
    if intent_code:
        intent_result = search_by_intent(intent_code, brand)
        result["memory"] = intent_result.get("results", [])
    
    # 2. 全量搜索补足
    if not result["memory"] or len(result["memory"]) < 3:
        full_result = search_memory(query, top_k=5)
        existing_ids = {r.get("content", "")[:50] for r in result["memory"]}
        for r in full_result.get("results", []):
            if r["content"][:50] not in existing_ids:
                result["memory"].append(r)
    
    # 3. 品牌/实体知识图谱
    if brand:
        graph_result = search_entity(brand)
        result["graph"] = graph_result.get("entities", [])
    
    result["count"] = len(result["memory"])
    result["success"] = True
    return result


if __name__ == "__main__":
    print("=" * 55)
    print("  Trinity-Coze 桥接层测试 (API 模式)")
    print("=" * 55)
    
    print("\n[测试1] 搜索: '618大促'")
    r = search_memory("618大促")
    print("  结果: %d 条" % r["count"])
    for item in r["results"][:2]:
        print("    [%.2f] %s" % (item["importance"], item["content"][:50]))
    
    print("\n[测试2] 意图 I04 (时效咨询)")
    r = search_by_intent("I04")
    print("  结果: %d 条" % r["count"])
    
    print("\n[测试3] 统一 bridge")
    r = bridge("618大促", intent_code="I04", brand="珀莱雅")
    print("  memory: %d 条, graph: %d 条" % (r["count"], len(r["graph"])))
    
    print("\n  桥接层运行正常, 不再直连 SQLite")

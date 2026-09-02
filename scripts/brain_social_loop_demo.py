#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二真实 Agent 社会闭环演示（EXECUTION 457，P1-2）。

体检结论：社会 95% 无真实场景——市场 0 成交、无第二个 agent、ToM 无对象。
本脚本跑一条**真实（非模拟）**的双 agent 社会链：
  ① 种子：ops-bot（第二 agent，persona=ops-team）写入 3 条运维领域知识记忆
     （importance>=0.8，含明确关注主题）——与主 agent（dsh-*）命名空间并存；
  ② 市场供给：ops-bot 高价值记忆经 /market/list 真实上架（写 memory_market_orderbook.json）；
  ③ 市场成交：主 agent 作为 buyer 经 /market/buy 真实购买（TrustExchange 记账）；
  ④ ToM：theory_of_mind.infer_agent('ops-bot') 推断其心理状态，与真实种子主题对照；
  ⑤ 跨 agent 检索：主 agent 以 agent_id=ops-bot 作用域检索其记忆——"读得到别人"。
输出 JSON 到 ~/.trinity/state/social_loop_<ts>.json；幂等（--reset 先清理旧资产）。

用法: python scripts/brain_social_loop_demo.py [--reset]
"""
import argparse
import json
import os
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("TRINITY_QUIET_IMPORT", "1")

API = os.environ.get("TRINITY_API_URL", "http://127.0.0.1:8001")
AGENT_B = "ops-bot"
BUYER = "dsh-social-demo"
SEED = [
    ("旺店通WMS运维：出库单异常时先查库存锁定状态，再查承运商接口重试策略；恢复顺序=库存→单据→承运商，避免二次覆盖。",
     "wms_knowledge", ["ops-bot", "wms", "social-demo"], 0.85),
    ("数据库备份策略偏好：WAL 备份优先于全量拷贝；保留 14 天；备份必须写 NAS 双份并验证可恢复——宁可慢不可丢。",
     "decision", ["ops-bot", "backup", "social-demo"], 0.9),
    ("我（ops-bot）的关注主题：数据库可靠性、备份可恢复性、WMS 单据链路一致性；遇到告警先看最近 1 小时事件再动手。",
     "insight", ["ops-bot", "social-demo"], 0.8),
]


def api_post(path, payload):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def api_get(path):
    with urllib.request.urlopen(API + path, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="先撤销 ops-bot 已上架资产")
    args = ap.parse_args()
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 0) 复位：delist ops-bot 的既有资产
    if args.reset:
        try:
            ob = api_get("/market/orderbook")
            for e in ob.get("orders") or []:
                if (e.get("seller_agent") or e.get("owner_agent")) == AGENT_B:
                    try:
                        api_post("/market/delist", {"asset_id": e.get("asset_id")})
                    except Exception:
                        pass
        except Exception as e:
            print("reset skipped:", e)

    from trinity import Trinity
    m = Trinity(adapter="postgresql")

    # ① 种子 agent B 记忆（幂等：已有 3 条 social_demo 则复用）
    import psycopg2
    _cn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                           user="trinity", password="trinity")
    _cr = _cn.cursor()
    _cr.execute("SELECT memory_id FROM memories WHERE agent_id=%s AND status='active' "
                "AND metadata->>'social_demo'='true' LIMIT 3", (AGENT_B,))
    seeded = [str(r[0]) for r in _cr.fetchall()]
    _cn.close()
    if len(seeded) < len(SEED):
        for content, cat, tags, imp in SEED:
            r = m.ingest(content=content, category=cat, tags=tags, importance=imp,
                         agent_id=AGENT_B, persona_id="ops-team", wait_backfill=True,
                         metadata={"social_demo": True})
            seeded.append(r.get("memory_id"))
            print("seeded:", str(r.get("memory_id"))[:12], cat)
    else:
        print("seed reused:", len(seeded), "existing")
    report["seeded_memory_ids"] = seeded

    # ② 上架 ops-bot 高价值记忆（importance>=0.8 直接按 id 上架）
    listed = []
    for mid in seeded[:2]:
        try:
            body = api_post("/market/list", {
                "memory": {"memory_id": mid, "modality": "knowledge",
                           "importance": 0.9},
                "owner": AGENT_B, "price": 0.0, "license": "CC-BY"})
            print("listed:", body)
            listed.append(mid)
        except Exception as e:
            print("list fail:", str(e)[:150])
    report["listed"] = listed

    # ③ 主 agent 购买（买第一件）
    bought = None
    try:
        ob = api_get("/market/orderbook")
        target = None
        for e in ob.get("orders") or []:
            if (e.get("seller_agent") or e.get("owner_agent")) == AGENT_B:
                target = e
                break
        if target:
            bought = api_post("/market/buy", {
                "asset_id": target.get("asset_id"),
                "buyer_agent": BUYER, "offer_price": target.get("price") or 0.0})
            print("bought:", json.dumps(bought, ensure_ascii=False)[:260])
        else:
            print("buy: no ops-bot order found in", ob.get("count"))
    except Exception as e:
        print("buy fail:", str(e)[:200])
    report["bought"] = bought

    # ④ ToM 推断 vs 真实种子对照
    try:
        from trinity.brain.theory_of_mind import infer_agent, predict_behavior
        inf = infer_agent(AGENT_B)
        pred = predict_behavior(AGENT_B)
        report["tom_infer"] = inf
        report["tom_predict"] = pred
        truth = ["数据库", "备份", "WMS", "单据", "可靠性", "恢复", "库存", "承运商"]
        text = json.dumps(inf, ensure_ascii=False)
        hits = [t for t in truth if t in text]
        print("ToM focus hit rate: %d/%d -> %s" % (len(hits), len(truth), hits[:6]))
        report["tom_truth_hit"] = {"hits": hits, "n": len(truth)}
    except Exception as e:
        print("tom fail:", str(e)[:150])

    # ⑤ 跨 agent 检索（A 读 B 的记忆）
    try:
        res = m.search("数据库备份策略 恢复", top_k=5, agent_id=AGENT_B)
        hits = res.get("results") or res if isinstance(res, dict) else res
        hits = res.get("results") if isinstance(res, dict) else []
        tops = [str(h.get("content", ""))[:60] for h in hits[:5]]
        print("cross-agent top:", json.dumps(tops, ensure_ascii=False)[:400])
        report["cross_agent_hits"] = len(hits)
        report["cross_agent_top"] = tops
    except Exception as e:
        print("search fail:", str(e)[:150])

    out = os.path.expanduser("~/.trinity/state/social_loop_%s.json"
                             % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

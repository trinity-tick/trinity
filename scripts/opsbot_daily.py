#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opsbot_daily.py — 第二 agent 自治日循环（EXECUTION 458，P1-3 升格）

ops-bot 从"种子"升格为有自己日循环的 agent：
  1) 读取自身命名空间的 active 记忆 → jieba 提取关注主题（轮转，state 幂等）
  2) 以主题为 query 在**自己命名空间**检索证据 + 全局补充线索
  3) 生成决策记忆（category=decision, agent_id=ops-bot, 含证据与行动）
  4) 新决策（importance>=0.7）未上架则自动挂到记忆市场
全程确定性、零 LLM（规则+检索），幂等可日跑。

用法: python scripts/opsbot_daily.py [--dry-run]
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRINITY_QUIET_IMPORT", "1")

AGENT = "ops-bot"
API = "http://127.0.0.1:8001"
STATE = os.path.expanduser("~/.trinity/state/opsbot_state.json")
STOP = {"的", "了", "是", "在", "有", "和", "与", "及", "等", "对", "或", "被",
        "从", "到", "我们", "你们", "他们", "这个", "那个", "一个", "以及", "通过",
        "进行", "相关", "当前", "可以", "需要", "没有", "什么", "如何", "因为",
        "所以", "如果", "但是", "问题", "内容", "信息", "使用", "用户", "系统",
        "时间", "数据", "文件", "目录", "路径", "脚本", "命令", "运行", "设置"}


def pg():
    import psycopg2
    return psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity", connect_timeout=8)


def topics_of_agent():
    """从 agent 自身记忆提取关注主题（jieba 高频词，去停用词）。"""
    conn = pg()
    cur = conn.cursor()
    cur.execute("SELECT content FROM memories WHERE agent_id=%s AND status='active' "
                "ORDER BY (importance::float8) DESC NULLS LAST LIMIT 12", (AGENT,))
    rows = cur.fetchall()
    conn.close()
    try:
        import jieba
        jieba.setLogLevel(60)
    except Exception:
        jieba = None
    freq = {}
    for (c,) in rows:
        txt = str(c or "")
        if txt.startswith("enc:v1:"):
            try:
                from trinity.security.crypto import decrypt_content
                txt = str(decrypt_content(txt) or "")
            except Exception:
                txt = ""
            if txt.startswith("enc:v1:"):
                continue
        for w in (jieba.cut(txt) if jieba else txt.split()):
            w = w.strip()
            if 2 <= len(w) <= 10 and w not in STOP and not w.isdigit() and not w.isascii():
                freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])][:6]


def state_load():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    st = state_load()
    topics = topics_of_agent()
    print("ops-bot topics:", topics)
    if not topics:
        print("no topics (agent empty) — nothing to do")
        return 0
    idx = int(st.get("topic_idx", 0)) % len(topics)
    topic = topics[idx]
    print("chosen topic:", topic)

    from trinity import Trinity
    m = Trinity(adapter="postgresql")
    mine = m.search(topic, top_k=3, agent_id=AGENT)
    mine_hits = (mine.get("results") or []) if isinstance(mine, dict) else (mine or [])
    ev_self = [str(h.get("content", ""))[:200] for h in mine_hits[:3]]
    cross = m.search(topic, top_k=3)
    cross_hits = (cross.get("results") or []) if isinstance(cross, dict) else (cross or [])
    ev_cross = [str(h.get("content", ""))[:200] for h in cross_hits if str(h.get("agent_id") or "") != AGENT][:2]
    print("self evidence:", len(ev_self), "| cross clues:", len(ev_cross))

    content = (f"[ops-bot 自治] {time.strftime('%Y-%m-%d')} 主题「{topic}」巡检："
               f"自证 {len(ev_self)} 条 / 外部线索 {len(ev_cross)} 条"
               + ((" | 自证: " + " ".join(ev_self)[:180]) if ev_self else "")
               + ((" | 线索: " + " ".join(ev_cross)[:120]) if ev_cross else "")
               + f" | 结论：维持关注，等待新证据再行动。")
    if args.dry_run:
        print("[dry]", content[:200])
        return 0
    r = m.ingest(content=content[:800], category="decision",
                 tags=[AGENT, "autonomous", topic], importance=0.72,
                 agent_id=AGENT, persona_id="ops-team", wait_backfill=True,
                 metadata={"opsbot_daily": True, "topic": topic})
    mid = r.get("memory_id")
    print("decision memory:", str(mid)[:16])
    # 市场挂单（该记忆尚未上架才挂）
    try:
        ob = json.loads(urllib.request.urlopen(API + "/market/orderbook", timeout=10).read().decode())
        listed_ids = {e.get("asset_id") for e in ob.get("orders", [])}
        if mid and str(mid) not in listed_ids:
            payload = {"memory": {"memory_id": str(mid), "modality": "decision", "importance": 0.72},
                       "owner": AGENT, "price": 0.0, "license": "CC-BY"}
            req = urllib.request.Request(API + "/market/list", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
            print("listed:", resp.get("status"))
    except Exception as e:
        print("market skip:", str(e)[:80])
    st.update({"topic_idx": idx + 1, "last_run": time.strftime("%Y-%m-%d %H:%M:%S"),
               "last_topic": topic, "last_memory": str(mid)})
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("state saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())

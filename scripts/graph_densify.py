#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱增密（EXECUTION 457，P2-1）：PG 主库图谱从稀疏到成规模。

现状（2026-09-02 体检）：entities=187 / relations=980，配不上"记忆 97%"。
本脚本对 PG 主库 active 的知识类记忆做**通用**实体抽取 + 共现关系构建：
  - 实体：jieba 词频候选（文档频次>=3、长度 2..12、排除噪音），增量最多 3000 个
  - 关系：同文档共现对，predicate=co_occur，properties{weight=共现文档数}
幂等：sha256(id) 去重（ON CONFLICT DO NOTHING）+ 每日状态门（~/.trinity/state/
graph_densify_last.json），--force 可重跑。增量式：已有实体/关系自动跳过。

用法: python scripts/graph_densify.py [--limit 6000] [--force]
"""
import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("TRINITY_QUIET_IMPORT", "1")

STATE = os.path.expanduser("~/.trinity/state/graph_densify_last.json")
CATS = ("general", "knowledge", "insight", "decision", "kb_harvested",
        "wms_knowledge", "doc:general", "doc:summary", "semantic",
        "episodic", "task", "skill", "evolution", "sync")
STOP = {"的", "了", "是", "在", "有", "和", "与", "及", "等", "对", "或", "被", "把",
        "从", "到", "我们", "你们", "他们", "这个", "那个", "一个", "以及", "通过",
        "进行", "相关", "当前", "可以", "需要", "没有", "什么", "如何", "因为",
        "所以", "如果", "但是", "问题", "内容", "信息", "使用", "用户", "系统",
        "时间", "数据", "文件", "目录", "路径", "脚本", "命令", "运行", "设置",
        "默认", "这里", "下面", "上述", "如下", "其中", "以及", "之一", "之后",
        "之前", "过程中", "情况", "部分", "方式", "方法", "结果", "过程"}
NEW_ENT_MAX = 3000
REL_MAX = 80000
PER_DOC_ENT = 8


def eid(kind, *parts):
    return hashlib.sha256(f"{kind}:{'|'.join(parts)}".encode()).hexdigest()[:40]


def decrypt(v):
    s = str(v or "")
    if s.startswith("enc:v1:"):
        try:
            from trinity.security.crypto import decrypt_content
            s = str(decrypt_content(s) or "")
        except Exception:
            return ""
    return s if s and not s.startswith("enc:v1:") else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not args.force and os.path.exists(STATE):
        last = json.load(open(STATE, encoding="utf-8"))
        if last.get("date") == time.strftime("%Y-%m-%d"):
            print("SKIP: already densified today (%s); --force to rerun" % last.get("date"))
            return 0

    import psycopg2
    conn = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
                            port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
                            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
                            user=os.environ.get("TRINITY_PG_USER", "trinity"),
                            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
                            connect_timeout=10)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT name FROM entities")
    existing = {r[0] for r in cur.fetchall()}
    print("existing entities:", len(existing))

    # 载入候选记忆（知识类 active；按 importance 降序取 limit）
    cur.execute(
        "SELECT memory_id, COALESCE(metadata->>'category', category), content "
        "FROM memories WHERE status='active' "
        "AND COALESCE(metadata->>'category', category) = ANY(%s) "
        "ORDER BY (importance::float8) DESC NULLS LAST LIMIT %s",
        (list(CATS), args.limit))
    rows = cur.fetchall()
    print("candidate memories:", len(rows))

    try:
        import jieba
        jieba.setLogLevel(60)
    except Exception:
        print("jieba unavailable - abort")
        return 1

    doc_freq = Counter()
    tok_total = Counter()
    doc_ents = []          # per doc: [(name, rank) ...]
    mem_cats = {}
    t0 = time.time()
    for i, (mid, cat, content) in enumerate(rows):
        txt = decrypt(content)
        mem_cats[str(mid)] = str(cat or "")
        if not txt:
            continue
        # jieba 分词（Paddle 不可用则默认模式）
        try:
            toks = [w.strip() for w in jieba.cut(txt) if w.strip()]
        except Exception:
            toks = []
        seen = set()
        for w in toks:
            if 2 <= len(w) <= 12 and not w.isdigit() and w not in STOP and not w.isascii():
                # 仅收中文为主词（含少量中英混合去掉）
                if w not in seen:
                    seen.add(w)
                    doc_freq[w] += 1
                tok_total[w] += 1
        # 保持该文档最高频前 PER_DOC_ENT 词（覆盖既有实体锚点）
        top = sorted(seen, key=lambda w: (doc_freq[w], tok_total[w]), reverse=True)[:PER_DOC_ENT]
        doc_ents.append((str(mid), top))
        if (i + 1) % 1000 == 0:
            print("  tokenized", i + 1, "docs in", round(time.time() - t0, 1), "s")
    print("tokenized all in", round(time.time() - t0, 1), "s; vocab:", len(doc_freq))

    # 新实体：文档频次>=3 且不在现有集合，取 TOP NEW_ENT_MAX
    cand = [(w, doc_freq[w], tok_total[w]) for w in doc_freq if doc_freq[w] >= 3 and w not in existing]
    cand.sort(key=lambda x: (x[1], x[2]), reverse=True)
    new_ents = [w for w, _, _ in cand[:NEW_ENT_MAX]]
    print("new entities to add:", len(new_ents))

    # 批量写实体
    if new_ents:
        cur.executemany(
            "INSERT INTO entities (id, name, type, properties) VALUES (%s,%s,'concept','{}') "
            "ON CONFLICT (id) DO NOTHING",
            [(eid("ent", w), w) for w in new_ents])
        print("entities inserted:", len(new_ents))

    # 文档级实体集合 = 既有 + 新增
    all_ent_set = existing | set(new_ents)
    # 预聚合 id 映射
    ent_by_name = {}
    cur.execute("SELECT id, name FROM entities")
    for rid, name in cur.fetchall():
        ent_by_name[str(name)] = str(rid)

    # 共现关系聚合（predicate=co_occur；weight=文档数）
    rel_agg = defaultdict(lambda: {"docs": 0, "count": 0})
    t0 = time.time()
    for mid, tops in doc_ents:
        ents_in_doc = [w for w in tops if w in ent_by_name]
        if len(ents_in_doc) < 2:
            continue
        ents_in_doc = ents_in_doc[:8]
        for a in range(len(ents_in_doc)):
            for b in range(a + 1, len(ents_in_doc)):
                k = (ents_in_doc[a], ents_in_doc[b]) if ents_in_doc[a] < ents_in_doc[b]                     else (ents_in_doc[b], ents_in_doc[a])
                rel_agg[k]["docs"] += 1
                rel_agg[k]["count"] += 1
    print("pair aggregation done in", round(time.time() - t0, 1), "s; pairs:", len(rel_agg))

    pairs_sorted = sorted(rel_agg.items(), key=lambda kv: (kv[1]["docs"], kv[1]["count"]),
                          reverse=True)[:REL_MAX]
    ins = 0
    B = 500
    for i in range(0, len(pairs_sorted), B):
        batch = []
        for (s, o), agg in pairs_sorted[i:i + B]:
            props = json.dumps({"weight": agg["docs"], "count": agg["count"],
                                "via": "graph_densify"}, ensure_ascii=False)
            batch.append((eid("rel", s, "co_occur", o), ent_by_name[s], "co_occur",
                          ent_by_name[o], props))
        cur.executemany(
            "INSERT INTO relations (id, subject_id, predicate, object_id, properties) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", batch)
        ins += len(batch)
    print("relations attempted:", ins)

    cur.execute("SELECT count(*) FROM entities"); ne = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM relations"); nr = cur.fetchone()[0]
    print("RESULT entities:", ne, "| relations:", nr)
    json.dump({"date": time.strftime("%Y-%m-%d"), "ts": time.time(),
               "memories": len(rows), "new_entities": len(new_ents),
               "relations_added": ins, "entities_total": ne, "relations_total": nr},
              open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""构建图谱关系层：从聚合池 WMS 记录抽取实体与关系，写入主库图谱。

两层设计:
  Tier A 概念层: 仓库(7) / 店铺(18) / 物流(15) 实体 + 加权共现关系
                 (服务店铺 / 合作承运 / 使用承运, properties.weight=共现次数)
  Tier B 订单层: 订单实体(订单编号) + 关系 (发货仓库 / 下单店铺 / 承运商)

幂等：实体按 name 去重 upsert；关系 INSERT OR IGNORE（sha256 id），可重复运行。

用法:
    python scripts/build_graph_relations.py            # 全量构建
    python scripts/build_graph_relations.py --dry-run  # 只统计不写入
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict

POOL = r"C:\Users\Administrator\trinity\data\aggregator_pool.json"
STORE_DB = r"C:\Users\Administrator\.trinity\store\trinity_store.db"

# 字段级表头噪声
SKIP_WAREHOUSES = {"仓库"}


def parse_record(content: str) -> dict:
    fields = {}
    for part in content.split("|"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, val = part.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip()
        if key:
            fields[key] = val
    return fields


def is_wms_record(fields: dict) -> bool:
    return any(k in fields for k in ("仓库", "订单编号", "仓储单号", "出库单号"))


def rel_id(sid: str, pred: str, oid: str) -> str:
    return hashlib.sha256(f"{sid}:{pred}:{oid}".encode()).hexdigest()[:32]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = ap.parse_args()

    d = json.load(open(POOL, encoding="utf-8"))
    mems = d["memories"]

    warehouses = Counter()   # name -> count
    stores = Counter()
    logistics = Counter()
    orders = {}              # order_no -> {"warehouse":..., "store":..., "logistics":..., "outbound":...}
    ws_pairs = Counter()     # (warehouse, store)
    wl_pairs = Counter()     # (warehouse, logistics)
    sl_pairs = Counter()     # (store, logistics)

    for m in mems:
        content = m.get("content") or ""
        if "|" not in content:
            continue
        fields = parse_record(content)
        if not is_wms_record(fields):
            continue
        wh = fields.get("仓库")
        st = fields.get("店铺")
        lg = fields.get("物流公司")
        od = fields.get("订单编号")
        if wh and wh not in SKIP_WAREHOUSES:
            warehouses[wh] += 1
        if st:
            stores[st] += 1
        if lg:
            logistics[lg] += 1
        if od:
            orders[od] = {
                "warehouse": wh or "",
                "store": st or "",
                "logistics": lg or "",
                "outbound": fields.get("出库单号", ""),
            }
        if wh and st:
            ws_pairs[(wh, st)] += 1
        if wh and lg:
            wl_pairs[(wh, lg)] += 1
        if st and lg:
            sl_pairs[(st, lg)] += 1

    n_wh = sum(1 for w in warehouses if w not in SKIP_WAREHOUSES)
    n_st = len(stores)
    n_lg = len(logistics)
    n_od = len(orders)
    print(f"实体候选: 仓库 {n_wh} | 店铺 {n_st} | 物流 {n_lg} | 订单 {n_od}")
    print(f"概念关系: 仓库->店铺 {len(ws_pairs)} | 仓库->物流 {len(wl_pairs)} | 店铺->物流 {len(sl_pairs)}")
    n_order_rel = sum(1 for v in orders.values() if v["warehouse"]) \
        + sum(1 for v in orders.values() if v["store"]) \
        + sum(1 for v in orders.values() if v["logistics"])
    print(f"订单关系: ~{n_order_rel}")

    if args.dry_run:
        return

    # ── 写入主库 ────────────────────────────────────────────────
    conn = sqlite3.connect(STORE_DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def upsert_entity(name: str, etype: str, props: dict) -> str:
        row = conn.execute(
            "SELECT entity_id FROM entities WHERE name = ?", (name,)
        ).fetchone()
        props_json = json.dumps(props, ensure_ascii=False)
        if row:
            eid = row["entity_id"]
            conn.execute(
                "UPDATE entities SET type = ?, summary = ?, first_seen = ? WHERE entity_id = ?",
                (etype, props_json, now, eid),
            )
            return eid
        eid = f"ent_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO entities (entity_id, name, type, frequency, first_seen, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, name, etype, props.get("count", 1), now, props_json),
        )
        return eid

    def add_relation(sid: str, pred: str, oid: str, props: dict) -> None:
        if sid == oid:
            return
        conn.execute(
            "INSERT OR IGNORE INTO relations (id, subject_id, predicate, object_id, properties, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rel_id(sid, pred, oid), sid, pred, oid,
             json.dumps(props, ensure_ascii=False), now),
        )

    t0 = time.time()
    # Tier A 概念实体
    for name, cnt in warehouses.items():
        if name in SKIP_WAREHOUSES:
            continue
        upsert_entity(name, "warehouse", {"count": cnt})
    for name, cnt in stores.items():
        upsert_entity(name, "store", {"count": cnt})
    for name, cnt in logistics.items():
        upsert_entity(name, "logistics_company", {"count": cnt})
    conn.commit()
    print(f"[Tier A] 概念实体写入完成 ({time.time()-t0:.1f}s)")

    # Tier A 概念关系（加权）
    eid_of = {}
    for name in list(warehouses) + list(stores) + list(logistics):
        row = conn.execute("SELECT entity_id FROM entities WHERE name = ?", (name,)).fetchone()
        if row:
            eid_of[name] = row["entity_id"]
    for (wh, st), cnt in ws_pairs.items():
        if wh in eid_of and st in eid_of:
            add_relation(eid_of[wh], "服务店铺", eid_of[st], {"weight": cnt})
    for (wh, lg), cnt in wl_pairs.items():
        if wh in eid_of and lg in eid_of:
            add_relation(eid_of[wh], "合作承运", eid_of[lg], {"weight": cnt})
    for (st, lg), cnt in sl_pairs.items():
        if st in eid_of and lg in eid_of:
            add_relation(eid_of[st], "使用承运", eid_of[lg], {"weight": cnt})
    conn.commit()
    print(f"[Tier A] 概念关系写入完成 ({time.time()-t0:.1f}s)")

    # Tier B 订单实体 + 关系
    order_eids = {}
    batch = 0
    for od, info in orders.items():
        eid = upsert_entity(od, "order", {"order_no": od, "outbound_no": info["outbound"]})
        order_eids[od] = eid
        batch += 1
        if batch % 2000 == 0:
            conn.commit()
    conn.commit()
    print(f"[Tier B] 订单实体写入完成: {len(order_eids)} ({time.time()-t0:.1f}s)")

    batch = 0
    for od, info in orders.items():
        sid = order_eids[od]
        if info["warehouse"] and info["warehouse"] in eid_of:
            add_relation(sid, "发货仓库", eid_of[info["warehouse"]], {"count": 1})
        if info["store"] and info["store"] in eid_of:
            add_relation(sid, "下单店铺", eid_of[info["store"]], {"count": 1})
        if info["logistics"] and info["logistics"] in eid_of:
            add_relation(sid, "承运商", eid_of[info["logistics"]], {"count": 1})
        batch += 1
        if batch % 2000 == 0:
            conn.commit()
    conn.commit()
    print(f"[Tier B] 订单关系写入完成 ({time.time()-t0:.1f}s)")

    # ── 汇总 ─────────────────────────────────────────────────────
    n_ent = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    n_rel = conn.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
    by_pred = conn.execute(
        "SELECT predicate, COUNT(*) c FROM relations GROUP BY predicate ORDER BY c DESC"
    ).fetchall()
    by_type = conn.execute(
        "SELECT type, COUNT(*) c FROM entities GROUP BY type ORDER BY c DESC"
    ).fetchall()
    conn.close()
    print("=" * 50)
    print(f"实体总数: {n_ent}  关系总数: {n_rel}")
    print("实体类型:", {r["type"]: r["c"] for r in by_type})
    print("关系谓词:", {r["predicate"]: r["c"] for r in by_pred})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

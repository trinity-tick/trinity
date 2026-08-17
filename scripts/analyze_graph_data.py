# -*- coding: utf-8 -*-
"""分析聚合池中的 WMS 结构化记录，输出图谱构建所需统计。

用法:
    python scripts/analyze_graph_data.py
"""
import json
import re
import sys
from collections import Counter, defaultdict

POOL = r"C:\Users\Administrator\trinity\data\aggregator_pool.json"


def parse_record(content: str) -> dict:
    """解析 db-sync 管道分隔记录 -> 字段 dict。"""
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


def main() -> None:
    d = json.load(open(POOL, encoding="utf-8"))
    mems = d["memories"]
    print(f"pool total: {len(mems)}")

    rec_like = 0
    field_counter = Counter()
    warehouses = Counter()
    stores = Counter()
    logistics = Counter()
    skus = Counter()
    orders = Counter()
    cat_counter = Counter()
    cooccur_ws = Counter()   # (warehouse, store)
    cooccur_wl = Counter()   # (warehouse, logistics)
    cooccur_sl = Counter()   # (store, logistics)

    for m in mems:
        content = m.get("content") or ""
        if "|" not in content or ":" not in content:
            continue
        fields = parse_record(content)
        if not fields:
            continue
        # 必须是 WMS 记录：含 仓库 或 订单编号 等关键字段
        if not any(k in fields for k in ("仓库", "订单编号", "仓储单号", "出库单号")):
            continue
        rec_like += 1
        cat_counter[m.get("category", "?")] += 1
        for k in fields:
            field_counter[k] += 1
        wh = fields.get("仓库")
        st = fields.get("店铺")
        lg = fields.get("物流公司")
        od = fields.get("订单编号")
        if wh:
            warehouses[wh] += 1
        if st:
            stores[st] += 1
        if lg:
            logistics[lg] += 1
        if od:
            orders[od] += 1
        # SKU / 库位 字段探测
        for k in fields:
            if "sku" in k.lower() or "库位" in k:
                skus[k] += 1
        if wh and st:
            cooccur_ws[(wh, st)] += 1
        if wh and lg:
            cooccur_wl[(wh, lg)] += 1
        if st and lg:
            cooccur_sl[(st, lg)] += 1

    print(f"WMS record-like memories: {rec_like}")
    print(f"by category: {dict(cat_counter)}")
    print("\n--- fields (top 20) ---")
    for k, c in field_counter.most_common(20):
        print(f"  {k}: {c}")
    print(f"\n--- distinct ---")
    print(f"  warehouses: {len(warehouses)}  (top: {warehouses.most_common(8)})")
    print(f"  stores:     {len(stores)}      (top: {stores.most_common(8)})")
    print(f"  logistics:  {len(logistics)}   (top: {logistics.most_common(8)})")
    print(f"  orders:     {len(orders)}")
    print(f"  sku-ish fields: {dict(skus)}")
    print(f"\n--- co-occurrence pair counts ---")
    print(f"  warehouse-store: {len(cooccur_ws)} pairs (top: {cooccur_ws.most_common(8)})")
    print(f"  warehouse-logistics: {len(cooccur_wl)} pairs")
    print(f"  store-logistics: {len(cooccur_sl)} pairs")
    # 关系量估算
    print(f"\n--- 预计关系量 ---")
    print(f"  订单->仓库: ~{sum(1 for od in orders)}")
    print(f"  订单->店铺: ~{sum(1 for od in orders)}")
    print(f"  订单->物流: ~{sum(1 for od in orders)}")
    print(f"  仓库->店铺(weighted): {len(cooccur_ws)}")
    print(f"  仓库->物流(weighted): {len(cooccur_wl)}")
    print(f"  店铺->物流(weighted): {len(cooccur_sl)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

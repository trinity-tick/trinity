# -*- coding: utf-8 -*-
"""build_memory_pagetree.py — 生产记忆空间主题页树构建（Phase 1，纯元数据，零 LLM）。

借鉴 PageIndex 的"物化层级索引"思想：把 22k+ 条记忆组织成
category → 簇(cluster) → 记忆 的页树，检索时先定位页、再读页内。

用法:
    python scripts/build_memory_pagetree.py                     # 生产大库（~/.trinity/store）
    python scripts/build_memory_pagetree.py --store DIR         # 指定 store 目录
    python scripts/build_memory_pagetree.py --exclude-cat lme,stress-test,test,imported
    python scripts/build_memory_pagetree.py --no-exclude        # 不排除任何类目
    python scripts/build_memory_pagetree.py --dry-run           # 只统计不落盘

产物: <store>/pagetree.json（MemoryPageTree 序列化，含记录全文，自包含可检索）
维护链接入: dsh-ops/trinity-dsh-maintenance.ps1 -Tasks pagetree（Phase 2 同步摘要生成）
"""
import argparse
import os
import sys
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
# 避免 import trinity 时聚合器自举（agg-ann-prewarm GIL 饥饿）
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
# 注意：不要覆盖 TRINITY_STORAGE_ENCRYPTION——默认 on（R8 起），
# adapter 读 memories 时按密钥文件解密；覆盖为 off 会把 enc:v1 密文
# 原样读入页树（2026-08-26 首次构建踩坑，untagged 簇样例全是密文）。


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None, help="store 目录（默认权威大库）")
    ap.add_argument("--exclude-cat", default="lme,stress-test,test,imported",
                    help="排除类目（逗号分隔；空串=不排除）")
    ap.add_argument("--exclude-tags", default="lme,stress,stress-test,locktest,test,sync",
                    help="噪音标签（簇轴选择时忽略）")
    ap.add_argument("--no-exclude", action="store_true", help="不排除任何类目/标签")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    ap.add_argument("--page-size", type=int, default=1000)
    ap.add_argument("--no-vectors", action="store_true",
                    help="不生成节点摘要向量（默认生成，页定位语义化）")
    args = ap.parse_args()

    from trinity import Trinity

    kwargs = {}
    if args.store:
        kwargs["store_path"] = args.store
    mem = Trinity(adapter="sqlite", **kwargs)

    exclude_cats = None if args.no_exclude else [c.strip() for c in args.exclude_cat.split(",") if c.strip()]
    exclude_tags = None if args.no_exclude else [t.strip() for t in args.exclude_tags.split(",") if t.strip()]

    t0 = time.time()
    stats = mem.build_pagetree(
        exclude_categories=exclude_cats,
        exclude_tags=exclude_tags,
        save=not args.dry_run,
        page_size=args.page_size,
        with_vectors=not args.no_vectors,
    )
    elapsed = time.time() - t0
    print(f"PAGETREE build in {elapsed:.1f}s -> {stats.get('path') or '(dry-run)'}")
    print(f"  records      : {stats.get('records')}")
    print(f"  categories   : {stats.get('categories')}")
    print(f"  clusters     : {stats.get('clusters')}")
    print(f"  excluded cats: {stats.get('excluded_categories')}")

    tree = mem.load_pagetree(force=True)
    if tree is None:
        print("ERROR: tree not loaded")
        return 1
    # 类目/簇分布 Top10
    cats = sorted(tree.categories.items(), key=lambda kv: -kv[1]["memory_count"])[:10]
    print("  top categories:")
    for cid, node in cats:
        print(f"    {node['title']:20s} mem={node['memory_count']:5d} clusters={node['stats'].get('clusters', 0)}")
    clu = sorted(tree.clusters.items(), key=lambda kv: -kv[1]["stats"]["count"])[:10]
    print("  top clusters:")
    for cid, node in clu:
        print(f"    {node['title'][:24]:26s} mem={node['stats']['count']:5d} avg_imp={node['stats']['avg_importance']:.2f}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

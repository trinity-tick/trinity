# -*- coding: utf-8 -*-
"""DCPM System2 夜间整合（EXECUTION 117，大脑化第二步）。
从 PG dcpm_beliefs 读取 System1 信念 → System2 归纳 schema + 冲突检测
→ 结果作为记忆落库（dcpm-schema/dcpm-core 类别）。

用法: python scripts/dcpm_consolidate.py [--write] [--limit 500]
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    from trinity.adapters.postgresql import PostgreSQLAdapter
    from trinity.modules.second_brain.dcpm_dual_process_memory import (
        System2NighttimeEngine, CrossDomainAbstractor, BeliefRevisionNode,
    )

    a = PostgreSQLAdapter(auto_connect=True)
    a.connect()
    try:
        rows = a.dcpm_get_beliefs(limit=args.limit)
        print(f"persisted beliefs: {len(rows)}")
        if not rows:
            print("no beliefs to consolidate (run retrievals first)")
            return 0

        # 转换为 DCPM 节点
        beliefs = []
        for r in rows:
            try:
                from datetime import datetime
                ts = r.get("created_at")
                ts_f = ts.timestamp() if hasattr(ts, "timestamp") else 0.0
                beliefs.append(BeliefRevisionNode(
                    belief_id=str(r.get("belief_id") or "")[:12],
                    subject=r.get("subject") or "",
                    predicate=r.get("predicate") or "",
                    object=r.get("object") or "",
                    timestamp=ts_f,
                    superseded_by=r.get("superseded_by"),
                ))
            except Exception:
                continue

        s2 = System2NighttimeEngine()
        schemas = s2.induce_schemas(beliefs)
        collisions = 0
        for i in range(len(schemas)):
            for j in range(i + 1, len(schemas)):
                if s2.detect_collisions(schemas[i], schemas[j]):
                    collisions += 1
        abstractor = CrossDomainAbstractor()
        cores = abstractor.abstract(schemas)
        print(f"schemas: {len(schemas)} | collisions: {collisions} | core: {len(cores)}")
        # 2026-09 (EXECUTION 122): 冲突检测告警——跨域信念冲突（记忆矛盾）
        # 写审计标记（action=dcpm_collision），运维可查 /audit/query
        if collisions > 0:
            try:
                a.write_audit_log(
                    memory_id=None, action="dcpm_collision",
                    agent_id="dcpm-system2",
                    details={"collisions": collisions, "schemas": len(schemas),
                             "core_schemas": len(cores)},
                )
                print(f"collision alert written (audit dcpm_collision x{collisions})")
            except Exception as e:
                print(f"collision alert fail: {e}")

        if args.write:
            from trinity import Trinity
            m = Trinity(adapter="postgresql")
            n = 0
            for sch in schemas[:20]:
                content = f"[dcpm-schema] domain={sch.domain} conf={sch.confidence:.2f} slots={json.dumps(sch.slots, ensure_ascii=False)[:500]}"
                try:
                    m.ingest(content, category="dcpm-schema", tags=["dcpm", "schema", str(sch.domain)], importance=0.6)
                    n += 1
                except Exception:
                    pass
            for core in cores[:5]:
                content = f"[dcpm-core] label={core.label} domains={core.domains} invariants={json.dumps(core.invariant_slots, ensure_ascii=False)[:400]}"
                try:
                    m.ingest(content, category="dcpm-core", tags=["dcpm", "core-schema"], importance=0.7)
                    n += 1
                except Exception:
                    pass
            print(f"persisted {n} schema memories")
        return 0
    finally:
        a.disconnect()

if __name__ == "__main__":
    sys.exit(main())

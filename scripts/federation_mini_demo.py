#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""federation_mini_demo.py — 真联邦最小双实例验证（EXECUTION 458，P2-3）

两台"机器"用两个完全隔离的 SQLite 实例模拟（store A / store B，进程级隔离）：
  1. A 实例 3 条记忆（agent=a1）；B 实例 2 条独立记忆（agent=b1）
  2. federation_sync export A → import 到 B（传播 + content_hash 幂等去重）
  3. 反方向 B → A 快照 import（keep newer），双向同步且不重复
  4. 幂等复跑：再次 import 同一快照 → 0 新增
真实限制如实记录：单机双实例（包传输级同步，无 WAN 传输层）。

用法: python scripts/federation_mini_demo.py [--reset]
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

BASE = os.path.expanduser("~/.trinity/state/fed_demo")
PY = sys.executable
SYNC = os.path.join(ROOT, "scripts", "federation_sync.py")


def make_store(tag: str) -> str:
    d = os.path.join(BASE, tag)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "trinity_store.db")


def _run(args, timeout=240):
    r = subprocess.run([PY] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r


def seed(tag: str, items: list):
    store_dir = os.path.dirname(make_store(tag))
    code = []
    code.append("import sys, os")
    code.append("sys.path.insert(0, " + repr(ROOT) + ")")
    code.append("os.environ['TRINITY_STORAGE_BACKEND']='sqlite'")
    code.append("os.environ.setdefault('TRINITY_MEMORY_ENABLED','0')")
    code.append("from trinity import Trinity")
    code.append("m = Trinity(store_path=" + repr(store_dir) + ")")
    for content, agent, cat, imp in items:
        code.append("try:\n    m.ingest(" + repr(content) + ", agent_id=" + repr(agent) +
                    ", category=" + repr(cat) + ", importance=" + str(imp) +
                    ", postprocess=False)\nexcept Exception as _e:\n    if 'UNIQUE' not in str(_e): raise")
    code.append("print('seeded " + tag + " done')")
    r = _run(["-c", "\n".join(code)], timeout=240)
    print(r.stdout.strip().splitlines()[-1:] or ("err:" + r.stderr[-300:]))


def count(db: str) -> int:
    store_dir = os.path.dirname(db)
    code = []
    code.append("import sys, os")
    code.append("sys.path.insert(0, " + repr(ROOT) + ")")
    code.append("os.environ['TRINITY_STORAGE_BACKEND']='sqlite'")
    code.append("os.environ.setdefault('TRINITY_MEMORY_ENABLED','0')")
    code.append("from trinity import Trinity")
    code.append("m = Trinity(store_path=" + repr(store_dir) + ")")
    code.append("r = m.search('', top_k=10000)")
    code.append("print('TOTAL', len(r.get('results', []) if isinstance(r, dict) else r))")
    out = _run(["-c", "\n".join(code)], timeout=240)
    for line in out.stdout.splitlines():
        if line.startswith("TOTAL"):
            return int(line.split()[1])
    print("count err:", out.stderr[-300:])
    return -1


def run_sync(args_list):
    r = _run([SYNC] + args_list, timeout=300)
    tail = (r.stdout or "").strip().splitlines()
    print(" | ".join(tail[-4:])[:400] if tail else "no stdout")
    if r.returncode:
        print("sync err:", (r.stderr or "")[-300:])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    if args.reset:
        shutil.rmtree(BASE, ignore_errors=True)
    dbA = make_store("A")
    dbB = make_store("B")
    print("instance A:", dbA)
    print("instance B:", dbB)

    seed("A", [
        ("数据库备份策略偏好：WAL 优先，保留 14 天，双份 NAS（联邦演示 A 侧）", "a1", "knowledge", 0.7),
        ("旺店通出库异常先查库存锁定（联邦演示 A 侧）", "a1", "knowledge", 0.6),
        ("告警处理：先看最近 1 小时事件（联邦演示 A 侧）", "a1", "insight", 0.6),
    ])
    seed("B", [
        ("联邦演示 B 侧独立知识：多实例同步应 content_hash 幂等去重", "b1", "knowledge", 0.7),
        ("联邦演示 B 侧偏好：夜间批量任务先低峰（联邦演示 B 侧）", "b1", "preference", 0.65),
    ])
    nA0, nB0 = count(dbA), count(dbB)
    print("counts before: A=%d B=%d" % (nA0, nB0))

    snapA = os.path.join(BASE, "snap_A.json")
    run_sync(["export", "--db", dbA, "--out", snapA])
    run_sync(["import", "--db", dbB, "--file", snapA, "--strategy", "newer"])
    nB1 = count(dbB)
    print("after A->B import: B=%d (was %d)" % (nB1, nB0))
    run_sync(["import", "--db", dbB, "--file", snapA, "--strategy", "newer"])
    nB2 = count(dbB)
    print("idempotent re-import: B=%d (unchanged=%s)" % (nB2, nB2 == nB1))

    snapB = os.path.join(BASE, "snap_B.json")
    run_sync(["export", "--db", dbB, "--out", snapB])
    run_sync(["import", "--db", dbA, "--file", snapB, "--strategy", "newer"])
    nA1 = count(dbA)
    print("after B->A import: A=%d (was %d)" % (nA1, nA0))
    print("SUMMARY A:%d->%d B:%d->%d bidirectional_ok=%s idempotent_ok=%s"
          % (nA0, nA1, nB0, nB1, nA1 > nA0 and nB1 > nB0, nB2 == nB1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

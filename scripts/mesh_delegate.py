# -*- coding: utf-8 -*-
"""mesh_delegate.py — 多 agent 编排产品化（2026-08-27 第三阶段）。

automation 动作可调用：事件 → 创建委托（分析任务委派给 agent）。
用法: python scripts/mesh_delegate.py --from agent-op --to agent-an --task "..."
"""
import os
import sys
import argparse

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_agent", required=True)
    ap.add_argument("--to", dest="to_agent", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--ttl-hours", type=float, default=24.0)
    args = ap.parse_args()
    from trinity.agents.mesh import AgentMesh
    mesh = AgentMesh()
    did = mesh.create(args.from_agent, args.to_agent, args.task, ttl_hours=args.ttl_hours)
    print("delegated:", did[:18], "|", args.from_agent, "->", args.to_agent)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""多通道感知整合每日（EXECUTION 188）——统觉写入记忆。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.sensory_integration import (
        integrate_senses, integrate_to_memory, fuse_signals, fuse_to_memory)
    r = integrate_senses()
    ok = integrate_to_memory()
    fr = fuse_signals()
    fok = fuse_to_memory() if fr.get("fusion_possible") else False
    print(json.dumps({"active": r["active_channels"],
                      "correlations": len(r["correlations"]),
                      "memory": ok, "fusion": fok,
                      "fused_topics": fr.get("fused_topics", [])}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())

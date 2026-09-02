# -*- coding: utf-8 -*-
"""情境流刷新入口（EXECUTION 457）——维护链任务用。"""
import sys, os, json

def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from trinity.brain.situation_stream import refresh
    r = refresh(force=True)
    print(json.dumps(r, ensure_ascii=False)[:400])
    return 0 if r.get("ok") else 1

if __name__ == "__main__":
    sys.exit(main())

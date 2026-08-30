# -*- coding: utf-8 -*-
"""行动回路每日驱动（EXECUTION 181）——检测刺激并执行修复动作。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.action_loop import ActionLoop
    al = ActionLoop()
    stim = al.detect_stimuli()
    res = al.respond(stim)
    print(json.dumps({"stimuli": list(stim.keys()), "actions": len(res),
                      "total": al.report()["actions_taken"]}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())

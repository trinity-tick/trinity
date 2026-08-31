# -*- coding: utf-8 -*-
"""自我公理每日验证（EXECUTION 197）——自我可测试分数。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.self_axioms import verify_axioms
    from trinity.brain.emotion_axioms import verify_emotion_axioms
    r = verify_axioms()
    er = verify_emotion_axioms()
    print(json.dumps({"self_score": r["score"], "self_passed": r["passed"],
                      "emotion_score": er["score"], "emotion_passed": er["passed"]},
                     ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())

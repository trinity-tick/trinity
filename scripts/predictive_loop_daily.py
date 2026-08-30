# -*- coding: utf-8 -*-
"""预测-行动环每日（EXECUTION 187）——状态预测→误差→调查。"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")
    from trinity.brain.predictive_loop import predict_loop
    r = predict_loop()
    print(json.dumps({"surprises": len(r["surprises"]),
                      "big": r["big_surprises"]}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""每日自我反思（EXECUTION 151）——周期自省沉淀。

对 session_context 中所有会话执行 reflect_to_memory（自省写入
self-reflection 记忆），输出统计。接入维护链每日运行。

用法: python scripts/self_reflect_daily.py [--session SID]
"""
import sys, os, json

def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
    os.environ.setdefault("PGPASSWORD", "trinity")

    from trinity.adapters.postgresql import PostgreSQLAdapter
    from trinity.brain.self_model import reflect_to_memory

    a = PostgreSQLAdapter(auto_connect=True)
    a.connect()
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT id FROM session_context")
        sessions = [r[0].replace("ctx:", "") for r in cur.fetchall()]
        conn.close()
        done = 0
        for sid in sessions:
            try:
                if reflect_to_memory(a, sid):
                    done += 1
            except Exception:
                pass
        print(json.dumps({"sessions": len(sessions), "reflected": done}))
        return 0
    finally:
        a.disconnect()

if __name__ == "__main__":
    sys.exit(main())

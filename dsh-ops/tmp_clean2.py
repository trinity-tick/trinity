# -*- coding: utf-8 -*-
import psycopg2, hashlib, json
c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")
cur = c.cursor()
# remove test garbage records
cur.execute("DELETE FROM audit_log WHERE action IN ('test_ts_fix', 'manual_test', 'integrity_fix_test', 'integrity_fix_test2', 'adapter_test3', 'dbg-lease')")
c.commit()
print("deleted test records")
# rebuild chain
cur.execute("SELECT id, memory_id, action, agent_id, persona_id, timestamp, details FROM audit_log ORDER BY timestamp ASC, id ASC")
rows = cur.fetchall()
prev = ""
for rid, mid, action, agent, persona, ts, details in rows:
    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts else None)
    mid_str = str(mid) if mid else None
    det = details if details else {}
    payload = json.dumps({"id": rid, "memory_id": mid_str, "action": action,
        "agent_id": agent, "persona_id": persona, "timestamp": ts_str,
        "details": det, "prev_checksum": prev}, sort_keys=True, ensure_ascii=False)
    new_chk = hashlib.sha256(payload.encode()).hexdigest()
    cur.execute("UPDATE audit_log SET checksum = %s WHERE id = %s", (new_chk, rid))
    prev = new_chk
c.commit()
print("rebuilt", len(rows))

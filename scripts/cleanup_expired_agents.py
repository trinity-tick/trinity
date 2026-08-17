# -*- coding: utf-8 -*-
"""Cleanup expired agent_registry cards (TTL-based, 2026-08-16 P2).

agent 卡片注册时带 ttl_seconds(默认 86400s);超过 TTL 且仍 active 的标记为 expired,
避免 A2A 联邦目录积累死卡片。幂等,可任意频次运行。
"""
import sqlite3, sys, time
from datetime import datetime

DB = r'C:\Users\Administrator\.trinity\store\trinity_store.db'

def main():
    conn = sqlite3.connect(DB, timeout=15)
    now = time.time()
    expired = []
    try:
        import json
        for agent_id, reg, status, card_json in conn.execute(
            "SELECT agent_id, registered_at, status, card_json FROM agent_registry WHERE status='active'"
        ):
            if not reg:
                continue
            try:
                reg_ts = datetime.strptime(reg, '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                try:
                    reg_ts = datetime.fromisoformat(reg.replace('Z', '+00:00')).timestamp()
                except Exception:
                    continue
            ttl = 86400
            if card_json:
                try:
                    ttl = json.loads(card_json).get('ttl_seconds', 86400)
                except Exception:
                    pass
            if now - reg_ts > ttl:
                expired.append(agent_id)
        for a in expired:
            conn.execute("UPDATE agent_registry SET status='expired' WHERE agent_id=?", (a,))
        conn.commit()
    finally:
        conn.close()
    print(f'AGENT-TTL: expired={len(expired)} cards marked: {expired}')

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()

# -*- coding: utf-8 -*-
"""Export all Trinity memories to portable JSONL (migration-ready, 2026-08-16).
用法: python scripts/export_all.py [输出路径]  (默认 ~/.trinity/export/trinity_memories_<ts>.jsonl)
每行一个记忆: {memory_id, content, agent_id, session_id, persona_id, importance, tags, category, status, created_at}
"""
import sqlite3, json, sys, os
from datetime import datetime

DB = os.path.expanduser('~/.trinity/store/trinity_store.db')

def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.expanduser('~/.trinity/export'),
        f'trinity_memories_{datetime.now().strftime("%Y%m%d_%H%M%S")}.jsonl')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    cols = ['memory_id','content','agent_id','session_id','persona_id','importance',
            'tags','category','status','created_at','updated_at','access_count']
    n = 0
    with open(out_path, 'w', encoding='utf-8') as f:
        for r in conn.execute(f'SELECT {",".join(cols)} FROM memories ORDER BY created_at'):
            d = dict(r)
            try:
                d['tags'] = json.loads(d['tags']) if d['tags'] else []
            except Exception:
                d['tags'] = []
            f.write(json.dumps(d, ensure_ascii=False) + '\n')
            n += 1
    conn.close()
    print(f'EXPORT: {n} memories -> {out_path} ({os.path.getsize(out_path)/1024:.0f} KB)')

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()

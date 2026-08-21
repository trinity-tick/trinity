param(
    [int]$Interval = 10,
    [int]$MaxRounds = 0
)
# Trinity 实时数据看板: dsh_events / memories / versions / audit 增量 + collector 心跳
# 用法: powershell -File dsh-ops/trinity-live.ps1 -Interval 10  (Ctrl+C 退出)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$python = 'C:/Users/Administrator/AppData/Local/Programs/Python/Python314/python.exe'
if (-not (Test-Path $python)) { throw "system python not found: $python" }
$py = @'
import sqlite3, time, datetime, os, sys

db = os.path.expanduser('~/.trinity/store/trinity_store.db')
interval = float(sys.argv[1])
max_rounds = int(sys.argv[2])  # 0 = run forever

con = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

def now(): return datetime.datetime.now().strftime('%H:%M:%S')

def find_collector_log():
    cands = [
        os.path.expanduser('~/.trinity/logs/collector.log'),
        os.path.expanduser('~/.trinity/logs/trinity-collector.log'),
        os.path.expanduser('~/trinity/logs/collector.log'),
        os.path.expanduser('~/trinity/data/collector.log'),
        os.path.expanduser('~/.trinity/collector.log'),
    ]
    for c in cands:
        if os.path.exists(c): return c
    return None

clog = find_collector_log()
def collector_tail():
    if not clog: return ''
    try:
        with open(clog, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return lines[-1].strip() if lines else ''
    except Exception as e:
        return 'log read err: %s' % e

def base():
    return (
        cur.execute('select max(id) from dsh_events').fetchone()[0] or 0,
        cur.execute('select count(*) from memories').fetchone()[0],
        cur.execute('select count(*) from memory_versions').fetchone()[0],
        cur.execute('select count(*) from audit_log').fetchone()[0],
    )

def ft(ms):
    try: return datetime.datetime.fromtimestamp(ms/1000).strftime('%H:%M:%S')
    except Exception: return str(ms)

last_e, m0, v0, a0 = base()
print('[%s] Trinity live watch started (interval=%ss) db=%s' % (now(), interval, db))
print('  dsh_events_max=%d memories=%d versions=%d audit=%d' % (last_e, m0, v0, a0))
if clog: print('  collector log: %s' % clog)
print('-' * 78)

rounds = 0
while True:
    rounds += 1
    if max_rounds and rounds > max_rounds: break
    time.sleep(interval)
    le, m1, v1, a1 = base()
    print('[%s] dsh_events=%d (+%d)  memories=%d (+%d)  versions=%d (+%d)  audit=%d (+%d)' % (now(), le, le-last_e, m1, m1-m0, v1, v1-v0, a1, a1-a0))
    if le > last_e:
        for r in cur.execute('select id, substr(session_id,1,8) s, type, time, turn from dsh_events where id > ? order by id', (last_e,)):
            print('   EVENT #%d %s %-18s %s turn=%s' % (r['id'], ft(r['time']), r['type'], r['s'], r['turn']))
    if m1 > m0:
        for r in cur.execute('select memory_id, created_at, category, agent_id, importance, substr(content,1,80) c from memories order by created_at desc limit %d' % (m1-m0)):
            print('   NEW MEM %s %s [%s] %s imp=%s :: %s' % (r['memory_id'], str(r['created_at'])[:19], r['category'], r['agent_id'], r['importance'], str(r['c']).replace(chr(10),' ')))
    ct = collector_tail()
    if ct: print('   collector: ' + ct[-180:])
    last_e, m0, v0, a0 = le, m1, v1, a1
    print('-' * 78)
'@
$py | & $python - $Interval $MaxRounds
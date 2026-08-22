"""产品化验收：RouteReasoner（生产模块）50 题 A/B（2026-08-21）

目的：验证产品化版本（trinity/qa/route_reasoner.py + 正确的时间戳摄入）
能否达到 benchmark route3 的 74%——这是"组合路由产品化闭环"的验收标准。

与 benchmark route3 的差异点（有意保留，均为产品化真实形态）：
- 摄入：multi → turn 粒度、其他 → session 粒度，均带 [DATE: ] 前缀
  （与 route3 相同；生产链路的时间戳摄入是 RouteReasoner temporal 的前提）
- 检索/生成：完全走 RouteReasoner.answer（生产模块），不复制 benchmark 逻辑

用法：python benchmark/rr_ab50.py
"""
import json
import os
import random
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ['TRINITY_STORE'] = tempfile.mkdtemp(prefix='rr_ab_')
os.environ['TRINITY_LLM_EXTRACT'] = 'off'
os.environ['TRINITY_ISOLATE_TEST_WRITES'] = 'off'
os.environ['TRINITY_MEMORY_ENABLED'] = '0'

from trinity import Trinity  # noqa: E402
from trinity.qa.route_reasoner import RouteReasoner  # noqa: E402

DATA = r'C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json'
OUT = r'C:\Users\Administrator\.trinity\bench-official\rr_ab50.json'


def main() -> int:
    with open(DATA, encoding='utf-8') as f:
        data = json.load(f)
    random.seed(42)
    data = random.sample(data, 50)
    print('n=%d' % len(data), flush=True)

    mem = Trinity()

    def search_fn(q, top_k=5, agent_id=None, persona_id=None):
        return mem.search(q, top_k=top_k, agent_id=agent_id)

    rr = RouteReasoner(search_fn=search_fn, top_k=12, turn_top_k=16)
    print('RouteReasoner available=%s' % rr.available, flush=True)

    records = []
    t0 = time.time()
    for qi, q in enumerate(data):
        qid = q['question_id']
        qtype = q['question_type']
        agent = 'rr_' + str(qi)
        sessions = q.get('haystack_sessions', [])
        sids = q.get('haystack_session_ids') or []
        dates = q.get('haystack_dates') or []
        try:
            if qtype == 'multi-session':
                # turn 粒度摄入（带 [DATE:]）
                for si, sess in enumerate(sessions):
                    turns = sess if isinstance(sess, list) else sess.get('turns', [])
                    d = dates[si] if si < len(dates) else ''
                    for t_ in turns:
                        role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
                        content = t_.get('content', '') if isinstance(t_, dict) else str(t_)
                        if not content.strip():
                            continue
                        text = content.strip()
                        if d:
                            text = '[DATE: ' + str(d) + '] [' + role + '] ' + text
                        try:
                            mem.ingest(text, agent_id=agent, category='lme', tags=['lme'],
                                       postprocess=False)
                        except Exception:
                            pass
            else:
                # session 粒度摄入（带 [DATE:]）
                for si, sess in enumerate(sessions):
                    turns = sess if isinstance(sess, list) else sess.get('turns', [])
                    parts = []
                    for t_ in turns:
                        role = t_.get('role', 'user') if isinstance(t_, dict) else 'user'
                        content = t_.get('content', '') if isinstance(t_, dict) else str(t_)
                        parts.append('[' + role + '] ' + content)
                    text = chr(10).join(parts)
                    if not text.strip():
                        continue
                    d = dates[si] if si < len(dates) else ''
                    if d:
                        text = '[DATE: ' + str(d) + '] ' + text
                    try:
                        mem.ingest(text, agent_id=agent, category='lme', tags=['lme'],
                                   postprocess=False)
                    except Exception:
                        pass
            r = rr.answer(str(q['question']), qtype=qtype,
                          question_date=q.get('question_date'), agent_id=agent)
            answer = r.get('answer') or ''
            if r.get('error'):
                print('  [%s] ERR %s' % (qid, r['error']), flush=True)
            records.append({
                'question_id': qid, 'question_type': qtype,
                'expected': str(q.get('answer', ''))[:300],
                'answer': str(answer)[:500],
            })
        except Exception as exc:
            import traceback
            traceback.print_exc()
            records.append({
                'question_id': qid, 'question_type': qtype,
                'expected': str(q.get('answer', ''))[:300],
                'answer': 'ERR:' + type(exc).__name__,
            })
        if (qi + 1) % 10 == 0:
            print('[%d/%d] elapsed=%ds' % (qi + 1, len(data), int(time.time() - t0)), flush=True)

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'variant': 'route_reasoner_ab50', 'records': records,
                   'elapsed': round(time.time() - t0, 1)}, f, ensure_ascii=False)
    print('saved:', OUT, 'elapsed:', int(time.time() - t0), 's', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

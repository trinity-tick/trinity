# Run evaluation on pre-ingested SQuAD data and save results
import json, os, sys, time, random
sys.path.insert(0, r'C:\Users\Administrator\trinity')
os.environ['TRINITY_MEMORY_ENABLED'] = '0'
from trinity.adapters.sqlite import SQLiteAdapter

squad_path = os.environ['TEMP'] + '/squad_dev.json'
store_path = os.environ['TEMP'] + '/trinity_squad_bench.db'

# Load SQuAD
with open(squad_path, 'r', encoding='utf-8') as f:
    data = json.load(f)['data']

random.seed(42)
random.shuffle(data)
articles = data[:30]

qa_pairs = []
for article in articles:
    title = article['title']
    for para_idx, paragraph in enumerate(article['paragraphs']):
        context = paragraph['context'].strip()
        for qa in paragraph['qas']:
            qa_pairs.append({
                'title': title, 'para_idx': para_idx,
                'context_id': f"{title}__p{para_idx}",
                'context': context, 'question': qa['question'].strip(),
                'answers': [ans['text'] for ans in qa['answers']],
                'qid': qa['id'], 'is_impossible': qa.get('is_impossible', False),
            })

if len(qa_pairs) > 200:
    by_title = {}
    for q in qa_pairs:
        by_title.setdefault(q['title'], []).append(q)
    sampled = []
    for title, qs in by_title.items():
        sampled.extend(random.sample(qs, min(max(1, 200//len(by_title)), len(qs))))
    qa_pairs = sampled[:200]

# Connect to Trinity
adapter = SQLiteAdapter(db_path=store_path)
adapter.connect()
all_mems = adapter.get_all_memories()
print(f"DB has {len(all_mems)} memories")

# Build context -> memory_id map by searching for each context's first 100 chars
# Actually, let's search by title prefix to map
cid_to_mids = {}
for mem in all_mems:
    content = mem.get('content', '')
    for q in qa_pairs:
        cid = q['context_id']
        if cid not in cid_to_mids:
            if q['title'] in content and content.strip().startswith(f"[{q['title']}]"):
                cid_to_mids[cid] = [mem.get('memory_id')]
                break

print(f"Mapped {len(cid_to_mids)} context IDs to memory IDs")

# Evaluate
k = 5
results = []
hits = 0
total = 0
t0 = time.time()
for q in qa_pairs:
    if q['is_impossible']:
        continue
    total += 1
    question = q['question']
    target_cid = q['context_id']
    target_mids = cid_to_mids.get(target_cid, [])

    retrieved = adapter.search_memories(question, top_k=k)
    retrieved_ids = [r.get('memory_id') or r.get('id') for r in (retrieved or [])]

    hit = any(mid in retrieved_ids for mid in target_mids)
    if hit:
        hits += 1

    results.append({
        'qid': q['qid'], 'title': q['title'], 'question': question,
        'context_id': target_cid, 'hit': hit,
    })

elapsed = time.time() - t0
overall_r5 = round(hits / total, 4) if total > 0 else 0

# Category breakdown
cat_hits, cat_total = {}, {}
for r2 in results:
    cat = r2['title']
    cat_hits[cat] = cat_hits.get(cat, 0) + (1 if r2['hit'] else 0)
    cat_total[cat] = cat_total.get(cat, 0) + 1

output = {
    'dataset': 'SQuAD v1.1 (dev) — Rajpurkar et al. 2016',
    'dataset_source': 'https://rajpurkar.github.io/SQuAD-explorer/',
    'dataset_notes': 'Public benchmark adapted for memory retrieval QA; '
                     'LongMemEval/LoCoMo inaccessible from this environment',
    'total_questions': total,
    'hits': hits,
    'R@5': overall_r5,
    'R@5_pct': f"{overall_r5*100:.1f}%",
    'k': k,
    'elapsed_seconds': round(elapsed, 2),
    'qps': round(total/elapsed, 2) if elapsed > 0 else 0,
    'by_category': {cat: {'hits': cat_hits[cat], 'total': cat_total[cat],
                    'R@5': round(cat_hits[cat]/cat_total[cat], 4)}
                    for cat in sorted(cat_total.keys())},
    'retrieval_engine': 'Trinity BM25 FTS5 + jieba Chinese segmentation',
    'timestamp': '2026-08-12',
}

output_dir = r'C:\Users\Administrator\trinity\output'
os.makedirs(output_dir, exist_ok=True)
json_path = output_dir + '/third_party_benchmark_results.json'
with open(json_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\nR@5={output['R@5_pct']} ({hits}/{total})")
print(f"Time: {elapsed:.1f}s, QPS: {output['qps']}")
print(f"Saved: {json_path}")

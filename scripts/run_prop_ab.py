# -*- coding: utf-8 -*-
"""M3: 命题化 50 题 A/B runner（2026-08-18）

A 次（无 --propositions）: verbatim 摄入 + route2 检索生成（同 lme_qa_route）
B 次（--propositions）  : verbatim + 命题提取(真实 LLM) + route2 检索生成

用法:
  python scripts/run_prop_ab.py --out A.json            # 基线
  python scripts/run_prop_ab.py --propositions --out B.json
  然后: python benchmark/judge3.py --in A.json B.json   # 对比判分
"""
import json, os, sys, time, tempfile, urllib.request, argparse, re, glob
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--data", default=r"C:\Users\Administrator\.trinity\bench-official\longmemeval_s_cleaned.json")
parser.add_argument("--limit", type=int, default=50)
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--out", default=r"C:\Users\Administrator\.trinity\bench-official\prop_ab_out.json")
parser.add_argument("--propositions", action="store_true", help="enable write-path proposition extraction")
args = parser.parse_args()

api_key = None
with open(os.path.expanduser("~/.dsh/.credentials.yaml"), "r", encoding="utf-8-sig") as f:
    for line in f:
        if line.strip().startswith("DEEPSEEK_API_KEY"):
            api_key = line.split(":", 1)[1].strip().strip(chr(34)).strip(chr(39))
            break
assert api_key, "DEEPSEEK_API_KEY required"
os.environ["DEEPSEEK_API_KEY"] = api_key

def llm_chat(system, user, max_tokens=350, temp=0.0):
    payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temp, "max_tokens": max_tokens}
    req = urllib.request.Request("https://api.deepseek.com/v1/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()

store_dir = tempfile.mkdtemp(prefix="prop_ab_")
os.environ["TRINITY_STORE"] = store_dir
os.environ["TRINITY_ISOLATE_TEST_WRITES"] = "off"
os.environ["TRINITY_MEMORY_ENABLED"] = "0"
if args.propositions:
    os.environ["TRINITY_PROPOSITION_EXTRACT"] = "on"
else:
    os.environ["TRINITY_PROPOSITION_EXTRACT"] = "off"

from trinity import Trinity
mem = Trinity()
import importlib
ppro = importlib.import_module("trinity.modules.second_brain.ppro_profile_retrieval")
freshness = importlib.import_module("trinity.modules.second_brain.freshness_conflict_resolver")

with open(args.data, "r", encoding="utf-8") as f:
    data = json.load(f)
import random; random.seed(args.seed); data = random.sample(data, args.limit)
print("propositions=" + str(args.propositions), "| n=" + str(len(data)), "| store=" + store_dir, flush=True)

GEN_SYS_PLAIN = ("You are a meticulous assistant with access to full past conversation sessions. "
    "Read ALL excerpts carefully. The answer to the question IS somewhere in these excerpts. "
    "Find it and answer with the exact fact (name, number, date, title). "
    "Do not say UNKNOWN unless you have read every excerpt and the information is truly absent. "
    "Answer with just the fact, no preamble.")
GEN_SYS_TEMPORAL = ("You are a meticulous assistant answering a question that requires temporal reasoning across past conversations. "
    "Each excerpt is prefixed with a DATE marker and a REL marker (days before the question date) showing when the conversation happened. "
    "Read ALL excerpts carefully. The answer IS somewhere in them. "
    "Step 1: list every relevant dated fact (date + relative days). "
    "Step 2: compute the answer using date differences / most recent event / explicit day counts. "
    "Step 3: answer with just the exact fact. "
    "Do not say UNKNOWN unless the information is truly absent.")
GEN_SYS_KU = ("You are a meticulous assistant answering a question about the CURRENT state of something. "
    "Each excerpt is prefixed with [FRESH: 0-1] (higher = newer / more authoritative). "
    "Use the NEWEST information as the answer. If excerpts conflict, prefer higher freshness. "
    "Answer with just the exact fact. Do not say UNKNOWN unless the information is truly absent.")

def parse_date(s):
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", str(s))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

# 命题提取：独立 adapter 连同一库（写入命题条目，检索仍走 mem.search）
prop_adapter = None
if args.propositions:
    from trinity.adapters.sqlite import SQLiteAdapter
    db_files = glob.glob(os.path.join(store_dir, "**", "*.db"), recursive=True)
    db_path = db_files[0] if db_files else os.path.join(store_dir, "trinity_store.db")
    prop_adapter = SQLiteAdapter(db_path=db_path)
    prop_adapter.connect()
    from trinity.memory.proposition_extractor import extract_and_store
    print("prop adapter: " + db_path, flush=True)

records = []
t0 = time.time()
for qi, q in enumerate(data):
    qid = q["question_id"]
    qtype = q["question_type"]
    question = str(q["question"])
    expected = str(q.get("answer", ""))
    sessions = q.get("haystack_sessions", [])
    dates = q.get("haystack_dates", []) or []
    qdate = parse_date(q.get("question_date"))
    agent = "rte_" + str(qi)
    try:
        for si, sess in enumerate(sessions):
            turns = sess if isinstance(sess, list) else sess.get("turns", [])
            parts = []
            for t_ in turns:
                role = t_.get("role", "user") if isinstance(t_, dict) else "user"
                content = t_.get("content", "") if isinstance(t_, dict) else str(t_)
                parts.append("[" + role + "] " + content)
            text = chr(10).join(parts)
            if not text.strip():
                continue
            d = dates[si] if si < len(dates) else ""
            if d:
                text = "[DATE: " + str(d) + "] " + text
            try:
                mem.ingest(text, agent_id=agent, category="lme", tags=["lme"], postprocess=False)
                if prop_adapter is not None:
                    extract_and_store(prop_adapter, content=text, source_memory_id="verb_" + str(qi) + "_" + str(si), agent_id=agent, session_id=None, persona_id="default", role="user", timestamp=str(d) if d else None)
            except Exception as e:
                pass
        hits = mem.search(question, top_k=5, agent_id=agent)
        hit_list = hits.get("results", []) if isinstance(hits, dict) else (hits if isinstance(hits, list) else [])
        use_inner = qtype == "temporal-reasoning"
        ctx = []
        for h in hit_list:
            c = (h.get("content") or "").strip()
            if not c:
                continue
            if use_inner:
                q_terms = set(re.findall(r"[a-z0-9]+", question.lower()))
                turns = re.split(chr(10) + r"(?=\[(?:user|assistant|system)\])", c)
                kept = []
                for t_ in turns[:40]:
                    tl = t_.lower()
                    if any(term in tl for term in q_terms) or len(kept) < 2:
                        kept.append(t_[:1000])
                c2 = chr(10).join(kept[:8]) if kept else c[:12000]
                ctx.append(c2[:12000])
            else:
                ctx.append(c[:12000])
        if qtype == "temporal-reasoning":
            blocks = []
            for c in ctx:
                m = re.search(r"\[DATE: ([^\]]+)\]", c)
                d = parse_date(m.group(1)) if m else None
                rel = ""
                if d and qdate:
                    rel = " [REL: " + str((qdate - d).days) + " days]"
                blocks.append("[BLK] " + (m.group(1) if m else "?") + rel + chr(10) + c[:11000])
            def _block_date(b):
                m = re.search(r"\[DATE: ([^\]]+)\]", b)
                return parse_date(m.group(1)) if m else None
            blocks.sort(key=lambda b: (1, "") if _block_date(b) is None else (0, _block_date(b)))
            sys_prompt = GEN_SYS_TEMPORAL
            user_prompt = "Question: " + question + chr(10) + chr(10) + chr(10).join(blocks)[:60000]
        elif qtype == "knowledge-update":
            sys_prompt = GEN_SYS_KU
            user_prompt = "Question: " + question + chr(10) + chr(10) + chr(10).join(ctx)[:60000]
        else:
            sys_prompt = GEN_SYS_PLAIN
            user_prompt = "Question: " + question + chr(10) + chr(10) + chr(10).join(ctx)[:60000]
        answer = llm_chat(sys_prompt, user_prompt, max_tokens=200, temp=0.0)
        records.append({"question_id": qid, "question_type": qtype, "question": question, "expected": expected, "answer": answer, "category": qtype})
        print("  [q" + str(qi) + " " + qtype + "] " + answer[:40], flush=True)
    except Exception as e:
        records.append({"question_id": qid, "question_type": qtype, "question": question, "expected": expected, "answer": "ERR: " + str(e), "category": qtype})
        print("  [q" + str(qi) + " ERR] " + str(e)[:60], flush=True)

if prop_adapter is not None:
    try: prop_adapter.disconnect()
    except Exception: pass
with open(args.out, "w", encoding="utf-8") as f:
    json.dump({"variant": "prop_ab_" + ("prop" if args.propositions else "verbatim"), "qtype": "all", "records": records}, f, ensure_ascii=False, indent=1)
print("done: " + str(len(records)) + " records -> " + args.out + " in " + str(int(time.time() - t0)) + "s", flush=True)
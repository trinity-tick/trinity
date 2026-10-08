#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _pg_std import pg_connect  # noqa

"""memory_write_policy.py — 写入侧学习数据集 v1（EXECUTION 591, Mem-α-lite）

对照：Mem-α (arXiv 2509.25911) 用 RL 学"记忆构建"而非启发式；Trinity 写路径
目前是规则/会话末提炼(session_distill, 547)与感知门控——缺"以使用回报为信号的
写策略数据"。

本脚本（不训模型，先造"能训的数据+可复测的评估代理"）：
  build  —— 从 PG 真实记忆抽取偏好对：
      chosen   = 事后被检索使用(access_count>=1) 或 高价值提炼(insight /
                 metadata.session_distill=true) 的记忆条目（"写对了"）；
      rejected = 活跃但从未被使用(access_count=0)且低 importance(<0.45)、
                 普通 general 类、已存在>=min-age 天的条目（"没用的写入"）。
      每行附同会话邻居上下文(<=2 条, 解密, 截断) → 可作未来蒸馏/DPO 输入。
  judge  —— 抽样 N 行交 DS 判官按"内容本身是否值得写"盲评（high/low），
              与使用回报标签对照：输出分离度/翻转样例（标签代理可信度检查）。

产物：~/.trinity/write_policy/wp_v1.jsonl + wp_v1.stats.json + wp_v1.manifest.json
用法：
  python scripts/memory_write_policy.py build [--max-chosen 800] [--max-rejected 800]
  python scripts/memory_write_policy.py judge [--sample 40] [--seed 42]
红线：只读检索/审计信号，不写库不污染记忆；样本带 manifest 指纹。
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.request
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 独立脚本可能没有 trinity 路径：退回原静默行为
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_DIR = os.path.expanduser("~/.trinity/write_policy")
DS_URL = "https://api.deepseek.com/chat/completions"
MIN_AGE_DAYS = 3
MAX_CTX = 1200
HIGH_CATS = ("insight", "decision")
LOW_CATS = ("general", "session", "episodic")
STRESS_MARKERS = ("stress-test", "locktest", "[自动关联]", "LONG-STRESS")


# ── 写入侧准入判据（2026-09-19 数据治理④）──────────────────────────────
# 基线（EXECUTION §912.4，7 日实测）：agent_id='default' 写 11,261 条、98.9% 从未被读、
# 9,898 条与库内已有行 content_hash 相同；类目 general 11,583 写 / 11,485 冷 / 9,797 重复。
# ⇒ 准入的第一目标不是"少写点"，而是**别重复写、别写垃圾**。
# 本函数是判据的**单一来源**（dry-run 与将来的写路径门共用一份规则）；
# 本轮**只做 dry-run**，真接线必须另起一轮 + 前后对照 + env 开关。
ADMISSION_POLICY_V1 = {
    "dup_active_in_db": True,     # 库内已有**活跃**同 hash 行 ⇒ 重复写入
    "junk_min_len": 40,           # 低于此长度（且非高价值）⇒ 垃圾
    "keep_importance": 0.7,       # 高价值阈值：≥ 则一律保留（宁可多留）
}


def admission_enabled() -> bool:
    """总开关（默认 on；`TRINITY_WRITE_ADMISSION=off` ⇒ 一律 keep，用于回滚/对照）。"""
    return os.environ.get("TRINITY_WRITE_ADMISSION", "annotate").lower() not in ("0", "off", "false", "no")


def admission_mode() -> str:
    """写入路径准入**三档**（2026-09-19 §915.3 接线）。

    · `off`      —— 不判（回滚档：写入路径完全回到接线前）
    · `annotate` —— **默认**：照常写，只把判定落 metadata["write_admission"]（可 SQL 计数）
    · `on`       —— 真的拦：判为 drop 的写入直接以 status='archived' 落库 + 审计 WRITE_ADMISSION_DROP

    为什么默认 annotate 而不是 on：本仓纪律「任何抑制都必须 env 开关 + 前后对照」；
    §913 dry-run 的误杀检查显示被 drop 的样本里**有曾经被检索过**的行 ⇒ 先在生产标注、
    拿到真数（重复率/误杀率）再翻档。未识别的取值一律按 annotate（fail-safe：不因错拼而开始拦截）。
    """
    raw = str(os.environ.get("TRINITY_WRITE_ADMISSION", "") or "").strip().lower()
    if raw in ("0", "off", "false", "no", "none", "disable", "disabled"):
        return "off"
    if raw in ("1", "on", "true", "yes", "drop", "enforce"):
        return "on"
    return "annotate"


def admission_decision(row: dict, policy: dict = None):
    """写入侧准入判据（纯函数）。返回 `(action, reason)`，action ∈ {"keep","drop"}。

    判据顺序（**可审计**，reason 即判据名）：
      ① 总开关关 → keep/admission_disabled（回滚路径）
      ② importance ≥ keep_importance → keep/high_importance（**宁可多留**，防误杀）
      ③ 库内已有活跃同 hash 行 → drop/dup_active_hash（同内容已在库里，再写只是放大写读比）
      ④ 内容短于 junk_min_len → drop/junk_below_min_len（无实质内容）
      ⑤ 其余 → keep/default_keep
    调用方负责填 `_dup_active_in_db`（dry-run 由 SQL 窗口函数算，写路径由 hash 查一次算）。
    """
    if not admission_enabled():
        return "keep", "admission_disabled"
    p = dict(ADMISSION_POLICY_V1)
    p.update(policy or {})
    try:
        imp = float(row.get("importance") or 0)
    except (TypeError, ValueError):
        imp = 0.0
    if imp >= float(p.get("keep_importance", 0.7)):
        return "keep", "high_importance"
    if p.get("dup_active_in_db") and row.get("_dup_active_in_db"):
        return "drop", "dup_active_hash"
    # 长度口径：优先用调用方预算好的 _content_len（dry-run 的 SQL 只取 length，不取全文，
    # 避免把上万条记忆正文拉进内存）；没有则按 content 现算。
    _clen = row.get("_content_len")
    if _clen is None:
        _clen = len(str(row.get("content") or "").strip())
    try:
        _clen = int(_clen)
    except (TypeError, ValueError):
        _clen = 0
    if _clen < int(p.get("junk_min_len", 40)):
        return "drop", "junk_below_min_len"
    return "keep", "default_keep"


def admission_dryrun(days: int = 7, agent: str = "", limit: int = 200000) -> int:
    """**只读** dry-run：对最近 N 天的写入套用准入判据，报告"会拦下什么、会不会误杀"。

    误杀检查（本函数最重要的部分）：被 drop 的行里**有多少曾经被检索过**
    （`access_count>0` 或 `last_retrieved_at IS NOT NULL`）——只要 >0 就必须人工复核，
    因为那说明这条规则会拦掉"以后会被用到"的写入。
    """
    import collections
    cur = _pg().cursor()
    where = ["created_at > now() - interval '%d days'" % int(days)]
    args = []
    if agent:
        where.append("agent_id = %s")
        args.append(agent)
    sql = """
      SELECT memory_id, agent_id, category, importance, content_hash, access_count,
             last_retrieved_at, length(coalesce(content,'')) AS clen,
             count(*) FILTER (WHERE status='active') OVER (PARTITION BY content_hash) AS active_same_hash
      FROM memories WHERE %s LIMIT %d
    """ % (" AND ".join(where), int(limit))
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    stat = collections.Counter()
    reasons = collections.Counter()
    dropped_used = []
    by_agent = collections.Counter()
    by_cat = collections.Counter()
    for r in rows:
        r["_dup_active_in_db"] = int(r.get("active_same_hash") or 0) > 1
        # 2026-09-19 自纠：首版 SQL 只取 length(content) 却让纯函数去读 row["content"] ⇒
        # 每条都被判成 junk（drop 96.1%），而"误杀检查"当场报出 1,787 条曾被检索的行
        # —— 判据没错，**取数口径错了**。此处把长度显式传给纯函数。
        r["_content_len"] = r.get("clen")
        act, why = admission_decision(r)
        stat[act] += 1
        reasons[why] += 1
        if act == "drop":
            by_agent[str(r.get("agent_id"))] += 1
            by_cat[str(r.get("category"))] += 1
            used = (int(r.get("access_count") or 0) > 0) or (r.get("last_retrieved_at") is not None)
            if used:
                dropped_used.append(str(r.get("memory_id")))
    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "readonly": True,
        "window_days": int(days), "agent_filter": agent, "rows": len(rows),
        "keep": stat["keep"], "drop": stat["drop"],
        "drop_pct": round(100.0 * stat["drop"] / max(1, len(rows)), 1),
        "reasons": dict(reasons),
        "dropped_by_agent": dict(by_agent.most_common(10)),
        "dropped_by_category": dict(by_cat.most_common(10)),
        "dropped_but_ever_retrieved": len(dropped_used),
        "dropped_but_ever_retrieved_sample": dropped_used[:10],
        "policy": dict(ADMISSION_POLICY_V1),
        "switch": os.environ.get("TRINITY_WRITE_ADMISSION", "on (default)"),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "admission_dryrun.json")
    json.dump(rep, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    print("-> %s" % p)
    return 0


def _pg():
    import psycopg2  # noqa: PLC0415
    return pg_connect()


def _decrypt(t):
    t = str(t)
    if t.startswith("enc:v1:"):
        try:
            from trinity.security.crypto import decrypt_content  # noqa: PLC0415
            return str(decrypt_content(t) or "")
        except Exception:
            return ""
    return t


def readable_ok(text, min_len=40):
    """纯函数：可读性过滤（解密后长度/可打印率/压测标记）。"""
    if not text or len(text) < min_len:
        return False
    np_ = sum(1 for ch in text if ch.isprintable())
    if np_ / len(text) < 0.5:
        return False
    return not any(m in text for m in STRESS_MARKERS)


def label_row(row, age_days=MIN_AGE_DAYS, now_ts=None):
    """纯函数：给定 memories 行 → 'chosen' | 'rejected' | None。
    规则（v1.2 强信号口径——v1.1 实测判官分离失败(agreement 0.275)：
    access_count 被检索噪音/回填污染，作"好写入"标签太弱）：
      chosen:   active 且 (category in HIGH_CATS(insight/decision) 或
                metadata.session_distill=true 或
                (importance>=0.72 且 access_count>=1 且 长度 80-600 由 build 再滤))
      rejected: (active 或 archived) 且 category in LOW_CATS 且 access_count=0 且
                importance<0.45 且 created_at 距今 >= age_days
                —— archived 低值 = "最终证明无价值"的写入代理。
    """
    try:
        imp = float(row.get("importance") or 0.0)
        acc = int(row.get("access_count") or 0)
    except Exception:
        return None
    if str(row.get("status") or "") not in ("active", "archived"):
        return None
    cat = str(row.get("category") or "")
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    strong = cat in HIGH_CATS or meta.get("session_distill") is True
    if strong or (imp >= 0.72 and acc >= 1):
        return "chosen"
    if cat in LOW_CATS and acc == 0 and imp < 0.45:
        ts = row.get("created_at")
        if not ts:
            return None
        try:
            import datetime as _dt  # noqa: PLC0415
            if isinstance(ts, str):
                ts = ts.replace("Z", "+00:00")
                created = _dt.datetime.fromisoformat(ts)
            else:
                created = ts
            if created.tzinfo is None:
                created = created.replace(tzinfo=_dt.timezone.utc)
            now = (now_ts or _dt.datetime.now(_dt.timezone.utc))
            if (now - created).days >= age_days:
                return "rejected"
        except Exception:
            return None
    return None


def _raw_siblings(cur, sid, exclude_content, n=1, cap=1500):
    """同会话 raw 兄弟条（general/episodic/session，解密可读）——配对 rejected 侧。"""
    if not sid:
        return []
    try:
        cur.execute(
            "SELECT content FROM memories WHERE status='active' "
            "AND session_id=%s AND category IN ('general','episodic','session') "
            "AND content<>%s ORDER BY (created_at::text) DESC LIMIT 6",
            (sid, exclude_content))
        rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for (content,) in rows:
        d = _decrypt(content)
        if readable_ok(d, 60) and d != exclude_content:
            out.append(d[:cap])
        if len(out) >= n:
            break
    return out


def _neighbors(cur, sid, exclude_content, n=2, cap=900):
    """同会话其他 active 记忆（解密、可读、截断）——写判上下文。"""
    if not sid:
        return []
    try:
        cur.execute(
            "SELECT content FROM memories WHERE status='active' AND session_id=%s "
            "AND content<>%s ORDER BY (created_at::text) DESC LIMIT 8",
            (sid, exclude_content))
        rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for (content,) in rows:
        d = _decrypt(content)
        if readable_ok(d, 30) and d != exclude_content:
            out.append(d[:cap])
        if len(out) >= n:
            break
    return out


def _ds_key():
    try:
        for line in open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig"):
            if line.strip().startswith("DEEPSEEK_API_KEY"):
                return line.split(":", 1)[1].strip().strip("'" ).strip('"')
    except Exception as _e:
        swallow(__name__, _e)
    return os.environ.get("DEEPSEEK_API_KEY", "")


def build(max_chosen=800, max_rejected=800):
    os.makedirs(OUT_DIR, exist_ok=True)
    c = _pg()
    cur = c.cursor()
    now = time.time()
    rows = []
    cur.execute("""SELECT memory_id, content, category, importance, access_count,
                          session_id, created_at, metadata, status
                   FROM memories WHERE status IN ('active','archived')
                     AND category <> 'perception' AND category <> 'lme'
                     AND category <> 'stress-test' AND category <> 'kb_harvested'
                     AND category <> 'imported'""")
    for r in cur.fetchall():
        (mid, content, cat, imp, acc, sid, created, meta, status) = r
        row = {"memory_id": mid, "content": content, "category": cat,
               "importance": imp, "access_count": acc, "session_id": sid,
               "created_at": created, "metadata": meta, "status": status}
        lbl = label_row(row, now_ts=_dt_now())
        if lbl:
            rows.append((lbl, row))
    c.close()
    chosen = [r for _ln, r in rows if _ln == "chosen"]
    rejected = [r for _ln, r in rows if _ln == "rejected"]
    random.seed(42)
    chosen = random.sample(chosen, min(max_chosen, len(chosen)))
    rejected = random.sample(rejected, min(max_rejected, len(rejected)))
    # 内容可读性过滤 + 邻居上下文
    out_rows = []
    used = {}
    cc = _pg()
    cur = cc.cursor()
    for r in chosen:
        d = _decrypt(r["content"])
        if not readable_ok(d, 60):
            continue
        if len(d) > 900:
            continue  # 强信号写入通常精炼；超长疑为 raw 转储
        nb = _neighbors(cur, r["session_id"], r["content"])
        out_rows.append({"label": "chosen", "text": d[:1500], "ctx": nb,
                         "category": r["category"], "sid": r["session_id"]})
        used.setdefault(r["session_id"], 0)
    for r in rejected:
        d = _decrypt(r["content"])
        if not readable_ok(d):
            continue
        nb = _neighbors(cur, r["session_id"], r["content"])
        out_rows.append({"label": "rejected", "text": d[:1500], "ctx": nb,
                         "category": r["category"], "sid": r["session_id"]})
    cc.close()
    # 偏好对（DPO-ready）：chosen(强信号/提炼写入) vs 同会话 raw 兄弟条
    # ——"值得写的高价值条目 vs 原样落库的普通条目"；判官比较式校验
    chosen_out = [x for x in out_rows if x["label"] == "chosen"]
    pairs = []
    random.seed(42)
    random.shuffle(chosen_out)
    dd = _pg()
    dcur = dd.cursor()
    for x in chosen_out:
        sib = _raw_siblings(dcur, x["sid"], x["text"])
        if not sib:
            continue
        pairs.append({"sid": x["sid"], "chosen": x["text"], "rejected": sib[0],
                      "ctx": (x["ctx"] or [])[:2]})
        if len(pairs) >= 200:
            break
    dd.close()
    stats = {"chosen_eligible": len(chosen), "rejected_eligible": len(rejected),
             "chosen_out": sum(1 for x in out_rows if x["label"] == "chosen"),
             "rejected_out": sum(1 for x in out_rows if x["label"] == "rejected"),
             "total": len(out_rows),
             "pairs": len(pairs)}
    code = hashlib.sha256(open(__file__, "rb").read()).hexdigest()[:12]
    with open(os.path.join(OUT_DIR, "wp_v1.jsonl"), "w", encoding="utf-8") as f:
        for x in out_rows:
            f.write(json.dumps(x, ensure_ascii=False) + chr(10))
    if pairs:
        with open(os.path.join(OUT_DIR, "wp_v1.pairs.jsonl"), "w", encoding="utf-8") as f:
            for x in pairs:
                f.write(json.dumps(x, ensure_ascii=False) + chr(10))
    json.dump({"stats": stats, "rules": {"age_days": MIN_AGE_DAYS, "seed": 42,
              "high_cats": list(HIGH_CATS), "low_cats": list(LOW_CATS)}},
              open(os.path.join(OUT_DIR, "wp_v1.stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump({"artifact": "wp_v1.jsonl", "script_sha": code, "built_at": time.time(),
               "params": {"max_chosen": max_chosen, "max_rejected": max_rejected}},
              open(os.path.join(OUT_DIR, "wp_v1.manifest.json"), "w", encoding="utf-8"),
              indent=1)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def _dt_now():
    import datetime  # noqa: PLC0415
    return datetime.datetime.now(datetime.timezone.utc)


def judge_pair(sample=20, seed=42, rubric=False):
    """比较式判词：同语境下 chosen vs rejected 谁更值得写（偏好对代理校验）。"""
    path = os.path.join(OUT_DIR, "wp_v1.pairs.jsonl")
    if not os.path.exists(path):
        print("NO_PAIRS run build first")
        return 1
    rows = [json.loads(_ln) for _ln in open(path, encoding="utf-8")]
    random.seed(seed)
    rows = random.sample(rows, min(sample, len(rows)))
    agreed = 0
    ties = 0
    res = []
    for i, r in enumerate(rows):
        ctx = ""
        if r["ctx"]:
            ctx = "同会话背景：\n" + "\n---\n".join(x[:300] for x in r["ctx"][:2])[:900]
        if rubric:
            q = ("同一会话产生两条候选长期记忆，用三条准则判断哪条更值得写入："
                 "① 含可独立检索的关键信息(数字/实体/时间/路径/结论)；② 结构化"
                 "(事实/决策/理由/结果)；③ 低冗余。\n" + ctx + "\nA：" +
                 str(r["chosen"])[:700] + "\nB：" + str(r["rejected"])[:700] +
                 "\nA 更优答 A；B 更优答 B；相当答 T。仅回答 A/B/T。")
        else:
            q = ("同一会话产生了两条候选记忆，二选一写入库（考虑：信息密度/可独立检索/"
                 "低冗余/未来复现价值）。\n" + ctx + "\nA：" + str(r["chosen"])[:700] +
                 "\nB：" + str(r["rejected"])[:700] + "\n仅回答 A 或 B。")
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": q}],
                "max_tokens": 4, "temperature": 0.0}
        req = urllib.request.Request(DS_URL, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + _ds_key()})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                v = json.loads(resp.read().decode()).get("choices", [{}])[0].get("message", {}).get("content", "")
            v = (v or "").strip().upper()
            if rubric:
                ok = v.startswith("A")
                tie = v.startswith("T")
            else:
                ok = v.startswith("A")
                tie = False
        except Exception:
            ok = None
            tie = False
        if ok is True:
            agreed += 1
        if tie:
            ties += 1
        res.append({"ok": ok, "tie": tie, "sid": r["sid"]})
        print("p %d/%d chosen_win=%s tie=%s" % (i + 1, len(rows), ok, tie), flush=True)
    print(json.dumps({"mode": "rubric" if rubric else "naive",
                      "pairs": len(rows), "chosen_preferred": agreed, "ties": ties,
                      "pair_agreement": round(agreed / max(1, len(rows)), 3)}, ensure_ascii=False))
    json.dump({"mode": "rubric" if rubric else "naive", "rows": res},
              open(os.path.join(OUT_DIR, "wp_v1.pair_judge.json"), "w", encoding="utf-8"), indent=1)
    return 0


def judge(sample=40, seed=42):
    rows = [json.loads(_ln) for _ln in open(os.path.join(OUT_DIR, "wp_v1.jsonl"), encoding="utf-8")]
    random.seed(seed)
    # 分层抽样：两类各取一半（避免大类主导）
    half = max(1, sample // 2)
    by = {"chosen": [r for r in rows if r["label"] == "chosen"],
          "rejected": [r for r in rows if r["label"] == "rejected"]}
    rows = (random.sample(by["chosen"], min(half, len(by["chosen"])))
            + random.sample(by["rejected"], min(sample - half, len(by["rejected"]))))
    res = []
    for i, r in enumerate(rows):
        ctx = ""
        if r["ctx"]:
            ctx = "同会话背景：\n" + "\n---\n".join(x[:400] for x in r["ctx"][:2])[:MAX_CTX]
        q = ("你是一个内部记忆库质检员。判断下面这条【拟写入记忆的条目】是否值得写入"
             "（信息密度高/可独立检索/低冗余噪声）。\n" + ctx + "\n条目：" +
             str(r["text"])[:800] + "\n仅回答 high 或 low。")
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": q}],
                "max_tokens": 8, "temperature": 0.0}
        req = urllib.request.Request(DS_URL, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + _ds_key()})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                v = json.loads(resp.read().decode()).get("choices", [{}])[0].get("message", {}).get("content", "")
            v = (v or "").strip().lower()
            pred = 1 if v.startswith("high") else 0
        except Exception:
            pred = None
        want = 1 if r["label"] == "chosen" else 0
        res.append({"pred": pred, "want": want, "label": r["label"],
                    "text": str(r["text"])[:120]})
        print("j %d/%d label=%s pred=%s" % (i + 1, len(rows), r["label"], pred), flush=True)
    ok = [x for x in res if x["pred"] is not None]
    acc = sum(1 for x in ok if x["pred"] == x["want"]) / max(1, len(ok))
    sep = {"chosen_high": sum(1 for x in ok if x["label"] == "chosen" and x["pred"] == 1),
           "chosen_n": sum(1 for x in ok if x["label"] == "chosen"),
           "rejected_low": sum(1 for x in ok if x["label"] == "rejected" and x["pred"] == 0),
           "rejected_n": sum(1 for x in ok if x["label"] == "rejected")}
    print(json.dumps({"judged": len(ok), "label_agreement": round(acc, 3), **sep}, ensure_ascii=False))
    json.dump({"agreement": round(acc, 3), "sep": sep, "rows": res},
              open(os.path.join(OUT_DIR, "wp_v1.judge.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("build", "judge", "judge-pair", "admission-dryrun"))
    ap.add_argument("--max-chosen", type=int, default=800)
    ap.add_argument("--max-rejected", type=int, default=800)
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rubric", action="store_true", help="judge-pair 用三准则比较")
    ap.add_argument("--agent", default="", help="admission-dryrun：只统计某个 agent_id")
    ap.add_argument("--days", type=int, default=7, help="admission-dryrun：窗口天数")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.cmd == "build":
        sys.exit(build(a.max_chosen, a.max_rejected))
    if a.cmd == "judge-pair":
        sys.exit(judge_pair(a.sample, a.seed, rubric="--rubric" in sys.argv))
    if a.cmd == "admission-dryrun":
        # 数据治理④：**只读** dry-run（不改写路径、不写库）
        sys.exit(admission_dryrun(days=(a.days or 7), agent=a.agent or ""))
    sys.exit(judge(a.sample, a.seed))


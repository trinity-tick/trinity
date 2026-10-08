#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冷条候选源 A/B：**doc2query 选源 vs §818 随机分层抽样**。

## 要回答的问题

§818 实测（LongMemEval oracle，n=96/96/60，配对翻转）：冷槽位在**证据充分**时
**−5.3pp（有害）**、证据不足时 −2.1pp（白花 token）⇒ 默认关闭。
**但其负结果针对的是候选源 `_cold_pick` = 按 `category` 分层随机抽样** ——
随机抽出来的冷条与当前任务**无关**，注入进上下文自然只有干扰。

本轮 Step 3 建了 **doc2query 查询侧索引**（给冷记忆生成「它回答什么问题」），
它提供了一条**性质不同的候选源**：按**任务形状查询**匹配，而不是随机抽。
⇒ 本实验问的就是：**换掉候选源，冷槽位会不会从"有害"变成"有用"？**

## 实验设计（三臂，同一候选池、同一被测题集）

被测题集 = `memories_doc2query_holdout` 里的**留出问题**（索引从未见过它，
见 `doc2query_pilot.py` 的循环论证注释）。每题拿一个留出问题当"开场查询"，
看它的**目标记忆**是否进入 N 个冷槽位。

- `rand_strat`（§818 同款）：按 category 分层 + 层内 `md5(memory_id||salt)` 稳定伪随机，
  层间按时间桶轮转 —— 复现 `_cold_pick` 的形态。
- `doc2query`（本源新机制）：用留出问题打 `memories_doc2query` 取 top-N。
- `uniform`（零假设）：全池均匀随机 —— 用来给出**基础命中率**，
  否则无法判断 `doc2query` 的高分里有多少只是"池子小"。

判据（**必须能失败**）：
  · `doc2query` 必须**严格优于** `rand_strat`，否则机制不成立 ⇒ **不铺开**；
  · 若 `doc2query` 与 `uniform` 无差别，说明它没带来信息，同样不成立。

## 纪律
- **只读**：不改库、不改任何表；不设 `TRINITY_AUTO_RECALL` / `TRINITY_ATLAS_COLD_SLOTS`。
- **§13.0**：候选池剔除引擎排除类目，并显式报数。
- **§13.2**：取不到分原因计数。
- 产物落 `output/`（§12）。

用法：
    python scripts/cold_candidate_ab.py --n-slots 2
    python scripts/cold_candidate_ab.py --n-slots 2 --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/cold_candidate_ab.py::<module>")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                             "store-restored", "trinity_store.db")
D2Q = "memories_doc2query"
HOLDOUT = "memories_doc2query_holdout"


def resolve_store(explicit: str = "") -> "tuple[str, str]":
    if explicit:
        return explicit, "cli"
    env = os.environ.get("TRINITY_STORE")
    if env:
        return (env if os.path.isfile(env) else os.path.join(env, "trinity_store.db")), \
            "env:TRINITY_STORE"
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._helpers import _find_trinity_store  # noqa: E402
        d = _find_trinity_store()
        return (d if os.path.isfile(d) else os.path.join(d, "trinity_store.db")), \
            "engine:_find_trinity_store"
    except Exception:  # noqa: BLE001
        return DEFAULT_STORE, "default"


def load_exclusions() -> "tuple[list, str]":
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from trinity.core.client._search import _RETRIEVAL_EXCLUDE_CATEGORIES  # noqa: E402
        return sorted(set(_RETRIEVAL_EXCLUDE_CATEGORIES)), "trinity.core.client._search"
    except Exception as e:  # noqa: BLE001
        return [], "UNAVAILABLE: %s: %s" % (type(e).__name__, str(e)[:160])


def _tokenize(text: str) -> str:
    try:
        import jieba
        return " ".join(t for t in jieba.cut(text or "") if t.strip())
    except Exception:  # noqa: BLE001
        return text or ""


def pick_uniform(pool: list, n: int, seed: str) -> list:
    """零假设臂：全池均匀随机（用 seed 保证可复现）。纯函数、可单测。"""
    rnd = random.Random(hashlib.sha256(seed.encode("utf-8")).hexdigest())
    return rnd.sample(list(pool), min(n, len(pool)))


def pick_rand_strat(pool_by_cat: dict, n: int, salt: str) -> list:
    """§818 同款形态：按 category 分层 + 层内 md5 稳定伪随机 + 层间轮转。

    `pool_by_cat` = {category: [memory_id, ...]}。纯函数、可单测。
    层按规模降序排列，但从 `salt` 派生的偏移开始轮转，避免被最大的层垄断
    （与 `_cold_pick` 的"层间按时间桶轮转"同旨）。
    """
    strata = sorted(pool_by_cat.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if not strata:
        return []
    # 层内按 md5(memory_id || salt) 排序 —— 同一 salt 可复现、不同 salt 不撞同一条
    ordered = []
    for cat, ids in strata:
        ordered.append(sorted(ids, key=lambda m: hashlib.md5(
            ("%s|%s" % (m, salt)).encode("utf-8")).hexdigest()))
    out, i, rot = [], 0, int(hashlib.md5(salt.encode("utf-8")).hexdigest()[:8], 16) % len(strata)
    while len(out) < n:
        progressed = False
        for k in range(len(strata)):
            s = ordered[(rot + k) % len(strata)]
            if i < len(s):
                out.append(s[i])
                progressed = True
                if len(out) >= n:
                    break
        if not progressed:
            break
        i += 1
    return out


def pick_by_doc2query(con, pool_ids: set, query: str, n: int) -> list:
    """本源新机制：用查询打 doc2query 索引取 top-n，并限定在候选池内。"""
    toks = [t for t in _tokenize(query).split() if t]
    if not toks:
        return []
    mq = " OR ".join('"%s"' % t.replace('"', "") for t in toks[:12])
    try:
        rows = con.execute(
            "SELECT memory_id FROM %s WHERE questions MATCH ? LIMIT ?" % D2Q,
            (mq, max(n * 20, 200))).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for (mid,) in rows:
        if mid in pool_ids and mid not in out:
            out.append(mid)
        if len(out) >= n:
            break
    return out


def hit_rate(n_hits: int, n_total: int) -> float:
    """纯函数、可单测。"""
    return round(n_hits / float(n_total), 4) if n_total else 0.0


def wilson_ci(hits: int, n: int, z: float = 1.96) -> "tuple[float, float]":
    """Wilson 区间（小样本比正态近似稳；n 小、p 接近 0 时尤其重要）。纯函数、可单测。"""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / float(n)
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (round(max(0.0, (c - h) / d), 4), round(min(1.0, (c + h) / d), 4))


#: 效应量地板（首版经验值，**必须回看**）。
#: 理由（§13.4：阈值是经验值，第一次真实判定前无法知道灵敏度是否合适）：
#: 冷槽位占注入配额的 2/5，每开一个槽就挤掉一条真实上下文。若命中率低到
#: 「绝大多数槽位注入的都是无关内容」，那正是 §818 量到的**有害**形态
#: （证据充分时 −5.3pp）⇒ 只有命中率足够高才谈得上开启。
#: 取 0.05 的依据：这是"20 次里至少 1 次真的捞到相关冷记忆"的量级；
#: **首次真实判定后必须回看**（太松=漏报缓慢退化；太紧=变成新的噪声源）。
MIN_HIT_DEFAULT = 0.05


def judge(res: dict, min_hit: float = MIN_HIT_DEFAULT) -> dict:
    """闸门判定。**必须能失败**，且**带效应量地板**。纯函数、可单测。

    2026-10-05 实测代价：初版只有严格不等式 ⇒ 3/220(1.36%) vs 0/220(0%) 也判 PASS，
    而 1.36% 意味着冷槽位**98.6% 的时间注入无关内容** —— 正是 §818 量到有害的形态。
    **只比大小不比效应量的判据会把"统计上可分辨、实践上无用"的机制放行。**
    """
    d = res.get("arms", {})
    if not all(k in d for k in ("doc2query", "rand_strat", "uniform")):
        return {"verdict": "INCONCLUSIVE", "why": "三臂不全 —— 不得当作通过"}
    n = int(res.get("n_evaluated") or 0)
    if n <= 0:
        return {"verdict": "INCONCLUSIVE", "why": "无有效受测题（n_evaluated=0）"}
    hits = int(d["doc2query"]["hits"])
    b, a, u = (d["doc2query"]["hit@n"], d["rand_strat"]["hit@n"], d["uniform"]["hit@n"])
    lo, hi = wilson_ci(hits, n)
    audit = {"doc2query_hits": hits, "n": n, "wilson95": [lo, hi],
             "baseline_max": max(a, u), "min_hit": min_hit}
    if b <= a:
        return dict(audit, verdict="FAIL",
                    why="doc2query(%.4f) 未优于 §818 随机分层(%.4f) ⇒ **机制不成立，不铺开**"
                        % (b, a))
    if b <= u:
        return dict(audit, verdict="FAIL",
                    why="doc2query(%.4f) 未优于均匀零假设(%.4f) ⇒ 选源没带来信息" % (b, u))
    if lo <= max(a, u):
        return dict(audit, verdict="FAIL",
                    why="doc2query 的 95%% 下界 %.4f 未超过基线 %.4f ⇒ 差异在噪声内"
                        % (lo, max(a, u)))
    if b < min_hit:
        return dict(audit, verdict="FAIL",
                    why="方向对（%.4f > 基线 %.4f）但**效应量过小**（%.4f < 地板 %.4f）"
                        "⇒ 冷槽位仍会约 %.1f%% 的时间注入无关内容（§818 的**有害**形态）"
                        "⇒ **不足以据此开启冷槽位**"
                        % (b, max(a, u), b, min_hit, (1 - b) * 100))
    return dict(audit, verdict="PASS",
                why="doc2query(%.4f, 95%%CI [%.4f,%.4f]) > 基线(%.4f) 且达效应量地板 %.4f "
                    "⇒ 换候选源有效" % (b, lo, hi, max(a, u), min_hit))


def run(store: str, n_slots: int, sample: int, exclude_self: bool = False,
        min_hit: float = MIN_HIT_DEFAULT) -> dict:
    """跑三臂 A/B。

    ## 关于 `exclude_self`（**默认 False = 目标留在候选池里**）

    2026-10-05 实测代价：初版默认**剔除**目标自身，理由是"避免三臂被算成必中" ——
    **那是错的，而且让整个实验失去判别力**：
      · 只有 `doc2query` 能靠「查询 ↔ 该记忆生成的问题」匹配找到目标；
      · `rand_strat` / `uniform` 是**盲选**，命中概率 = N/池子 ≈ 2/17068，可忽略；
      · 把目标剔除 ⇒ **doc2query 臂永远打不中**，三臂齐刷刷 0.0
        （实测就是这样，读数形状像"机制无效"，真相是我的对照写反了）。
    ⇒ 目标**必须留在池里**。且这不构成循环论证：目标记忆索引的是**其余 4 个问题**，
    测试用的是**留出的那 1 个**，索引从未见过它。
    `--exclude-self` 仅作诊断开关（它会人为把 doc2query 臂压成 0）。
    """
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "store": store, "n_slots": n_slots}
    excl, esrc = load_exclusions()
    out["engine_exclude_categories"] = excl
    out["engine_exclude_source"] = esrc
    if not excl:
        out.update(verdict="INCONCLUSIVE", error="engine exclusions unavailable: %s" % esrc)
        return out
    if not os.path.exists(store):
        out.update(verdict="INCONCLUSIVE", error="store not found: %s" % store)
        return out
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % store.replace("\\", "/"), uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=55000")
    try:
        for t in (D2Q, HOLDOUT):
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (t,)).fetchone():
                con.close()
                out.update(verdict="INCONCLUSIVE",
                           error="%s 不存在 —— 先跑 doc2query_pilot.py --build" % t)
                return out
        ph = ",".join("?" for _ in excl)
        pool_rows = con.execute(
            "SELECT memory_id, COALESCE(category,'') FROM memories "
            "WHERE status='active' AND CAST(COALESCE(access_count,0) AS INTEGER)=0 "
            "AND category NOT IN (%s)" % ph, tuple(excl)).fetchall()
        pool_by_cat: dict = {}
        for mid, cat in pool_rows:
            pool_by_cat.setdefault(cat, []).append(mid)
        pool_ids = {m for m, _ in pool_rows}
        out["pool_size"] = len(pool_ids)
        out["pool_strata"] = len(pool_by_cat)

        pairs = con.execute(
            "SELECT memory_id, question FROM %s LIMIT ?" % HOLDOUT,
            (sample,)).fetchall()
        out["n_queries"] = len(pairs)
        hits = {"doc2query": 0, "rand_strat": 0, "uniform": 0}
        n_eval = 0
        detail = []
        for mid, q in pairs:
            if mid not in pool_ids:
                continue        # 目标已不在冷池（被读过或被归档）⇒ 不计入任何臂
            if exclude_self:
                pool_minus = {m for m in pool_ids if m != mid}
                pbc_minus = {c: [m for m in v if m != mid] for c, v in pool_by_cat.items()}
            else:
                pool_minus, pbc_minus = pool_ids, pool_by_cat
            b = pick_by_doc2query(con, pool_minus, q, n_slots)
            a = pick_rand_strat(pbc_minus, n_slots, salt=str(q))
            u = pick_uniform(sorted(pool_minus)[:20000], n_slots, seed=str(q))
            n_eval += 1
            hits["doc2query"] += 1 if mid in b else 0
            hits["rand_strat"] += 1 if mid in a else 0
            hits["uniform"] += 1 if mid in u else 0
            if len(detail) < 15:
                detail.append({"memory_id": mid, "query": (q or "")[:50],
                               "doc2query": mid in b, "rand_strat": mid in a,
                               "uniform": mid in u})
        out["n_evaluated"] = n_eval
        out["arms"] = {k: {"hits": v, "hit@n": hit_rate(v, n_eval)} for k, v in hits.items()}
        out["detail"] = detail
        out["exclude_self"] = exclude_self
    except Exception as e:  # noqa: BLE001
        con.close()
        out.update(verdict="INCONCLUSIVE",
                   error="query failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return out
    con.close()
    out["verdict"] = "OK"
    out["judgement"] = judge(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="")
    ap.add_argument("--n-slots", type=int, default=2, help="冷槽位数（§818 文档口径：上限 2）")
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--exclude-self", action="store_true",
                    help="诊断用：把目标自身剔出候选池（会人为把 doc2query 臂压成 0）")
    ap.add_argument("--min-hit", type=float, default=MIN_HIT_DEFAULT,
                    help="效应量地板（低于它即判 FAIL，见 MIN_HIT_DEFAULT 的理由）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    store, src = resolve_store(a.store)
    r = run(store, a.n_slots, a.sample, exclude_self=a.exclude_self, min_hit=a.min_hit)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("== 冷条候选源 A/B（doc2query vs §818 随机分层）==")
        print("库：%s（来源 %s）" % (store, src))
        print("[采样时刻] %s" % r.get("ts"))
        if r.get("verdict") != "OK":
            print("判定：%s %s" % (r.get("verdict"), r.get("error")))
            return 2
        print("候选池 %d 条 / %d 个 category 层；被测留出问题 %d 条（实际计入 %d）"
              % (r["pool_size"], r["pool_strata"], r["n_queries"], r["n_evaluated"]))
        print("冷槽位 N=%d；目标自身%s候选池（%s）"
              % (r["n_slots"], "不在" if r["exclude_self"] else "仍在",
                 "**诊断模式：doc2query 臂必为 0**" if r["exclude_self"]
                 else "正确设置：只有 doc2query 能靠匹配找到它"))
        print()
        print("%-14s %-10s %-12s %s" % ("臂", "命中数", "hit@N", "说明"))
        for k, desc in (("rand_strat", "§818 同款（随机分层抽样）"),
                        ("doc2query", "本源新机制（任务形状问题匹配）"),
                        ("uniform", "零假设（全池均匀随机）")):
            v = r["arms"][k]
            print("%-14s %-10d %-12s %s" % (k, v["hits"], v["hit@n"], desc))
        print()
        j = r["judgement"]
        print("闸门判定：%s —— %s" % (j["verdict"], j["why"]))
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        fp = os.path.join(ROOT, "output", "cold_candidate_ab_%s.json"
                          % time.strftime("%Y%m%d_%H%M%S"))
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        print()
        print("产物：%s" % fp)
    return 0 if r.get("verdict") == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())

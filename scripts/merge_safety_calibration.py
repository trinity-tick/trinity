#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_safety_calibration.py —— 在**生产语料**上量测既有合并安全判据（t4，只读）。

## 为什么要"条件于合并闸门"来量

`verify_merge_safety` 只在**相似度闸门已经打开**时才被调用
（`_ingest.py::merge_if_similar`：`best_score >= SIMILARITY_MERGE_THRESHOLD`，阈值 0.75）。
若不分条件、在随机对上量命中率，得到的数字既不能解释也不能预测线上行为。
故本脚本的**分母被钉死为**：可读语料里 Jaccard ≥ 阈值（即"真的会被拿去合并"）的候选对。

## 口径

| 项 | 定义 |
|---|---|
| 语料 | `content` 非空且非 `enc:v1:` 的行（密文无法判读；这是硬限制，已写进报告） |
| 候选对 | MinHash(64)×LSH(16band×4row) 出候选 → **精确 Jaccard ≥ 0.75** 复核 |
| 分母 | 通过闸门的候选对数（`gate_open_pairs`） |
| 命中率 | 该规则判红的对数 / gate_open_pairs |
| 误杀（false positive） | 判红，但独立判据显示"其实没丢信息" |
| 漏杀（false negative） | 规则没判红，但独立判据显示"确实是纯冗余/纯回灌" |

## 三条规则的独立判据（不引用被判规则自己的逻辑）

* **R1 content_collapse** —— *真阳*：来料 token 集合 ⊆ 既有 token 集合（纯截断/回声）。
  *误杀*：判红但来料含有**既有内容里没有的实词**（≥1 个 ≥2 字的新 token）⇒ 它其实是新信息。
  *漏杀*：既有 ≥ `MIN_EXISTING_CHARS`、来料是纯回声，但长度比 ≥ `COLLAPSE_RATIO` ⇒ 规则不判红。
* **R2 source_downgrade** —— 实现对**当前调用点**是**恒真式**（见 `_judge_r2_tautology`）：
  `projected = set(existing); projected.add(new)` 之后判 `issuperset` 在任何输入下都为真。
  故线上命中率恒为 0，**没有判别力**。脚本用随机输入穷举给出证明，并给出"它只能在调用方
  改成重建集合时才会响"的结构性说明。
* **R3 duplicate_no_new_evidence** —— *真阳*：归一化后完全相同（按构造必真）。
  *误杀*：0（任何命中的对都确实无新信息；脚本另报"原始文本差异是否携带信息"以证）。
  *漏杀*：Jaccard ≥ 0.95 但归一化后不完全相同（差异不携带新证据的近似重复）。

用法：
    python scripts/merge_safety_calibration.py --json <out.json>
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERGE_SAFETY_PY = os.path.join(REPO, "trinity", "agents", "aggregator", "_merge_safety.py")

#: 与 `trinity/agents/aggregator/_constants.py::SIMILARITY_MERGE_THRESHOLD` 同值
GATE_THRESHOLD = 0.75
#: R3 漏杀的口径阈值：高度相似但未归一化相等
R3_NEARMISS = 0.95

_TOKEN = re.compile(r"[0-9A-Za-z_]{2,}|[\u4e00-\u9fff]")


def load_merge_safety():
    """**按文件路径**加载判据模块。

    绕开 `import trinity.agents.aggregator._merge_safety`：那会连带执行
    `trinity/__init__.py` → second_brain 初始化链（实测打印 20+ 行横幅并触发旁路清理）。
    `_merge_safety.py` 自身只依赖标准库，可独立加载 ⇒ 本脚本保持"零副作用只读"。
    """
    spec = importlib.util.spec_from_file_location("_merge_safety_standalone", MERGE_SAFETY_PY)
    mod = importlib.util.module_from_spec(spec)
    # `@dataclass` 在 exec 期要 `sys.modules[cls.__module__].__dict__` ⇒ 必须先注册，
    # 否则报 "AttributeError: 'NoneType' object has no attribute '__dict__'"。
    sys.modules["_merge_safety_standalone"] = mod
    spec.loader.exec_module(mod)
    return mod


def connect_ro(path: str):
    import sqlite3
    if os.path.isdir(path):
        path = os.path.join(path, "trinity_store.db")
    return sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)


def tokens(text: str) -> set:
    """判据无关的独立分词：中文按字、英文/数字按 ≥2 长度词。"""
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    return set(_TOKEN.findall(t))


def new_content_tokens(new_t: set, old_t: set) -> set:
    """来料里**既有内容没有**的 token（长度 ≥2 的算实词；单字中文不计，噪声大）。"""
    return {w for w in (new_t - old_t) if len(w) >= 2}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = len(a | b)
    return len(a & b) / u if u else 0.0


# ────────────────────────────────────────────── R2 恒真式证明

def r2_tautology_proof(ms, trials: int = 5000, seed: int = 20261006) -> Dict[str, Any]:
    """对 R2 做**随机输入穷举**：能否构造出让 `source_downgrade` 判红的输入？

    结构性理由：`_merge_safety.py:116-125` 的实现是

        projected = set(existing_sources); projected.add(new_source)
        if not projected.issuperset(set(existing_sources)):   # ← 永远为 False

    `set.add` 只能增大集合，`set(existing)` 又保证 `projected ⊇ existing`
    ⇒ 判据恒真、**线上不可能响**。下面的随机搜索是它的反事实证据。
    """
    rnd = random.Random(seed)
    fired = 0
    examples = []
    for i in range(trials):
        n = rnd.randint(1, 6)
        existing = {"a%d" % rnd.randint(0, 40) for _ in range(n)}
        new = "a%d" % rnd.randint(0, 40)
        content = "".join(rnd.choice("甲乙丙丁戊己庚辛") for _ in range(rnd.randint(1, 30)))
        v = ms.verify_merge_safety(content, content + "x",
                                   existing_sources=existing, new_source=new)
        if (not v.safe) and v.code == "source_downgrade":
            fired += 1
            if len(examples) < 3:
                examples.append({"existing": sorted(existing), "new": new})
    return {
        "trials": trials,
        "source_downgrade_fired": fired,
        "verdict": ("TAUTOLOGY（在当前调用点的输入空间里恒真、命中率恒为 0）"
                    if fired == 0 else "NOT a tautology — 需重估"),
        "structural_reason": ("projected = set(existing); projected.add(new) 之后 "
                              "projected.issuperset(existing) 恒为 True（set.add 只增不减）"),
        "file_line": "trinity/agents/aggregator/_merge_safety.py:116-125",
        "only_way_to_fire": ("调用方把 source_agents 改成**重建**（如 projected = {new}）时才会响；"
                             "而当前调用点 `_ingest.py:248` 是 `best_dv.source_agents.add(source_agent)` "
                             "⇒ 判据保护的是一个**不会被违反**的不变量"),
        "examples_that_fired": examples,
    }


# ────────────────────────────────────────────── 候选对与判定

def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def minhash(tok: set, perms: int = 64) -> Tuple[int, ...]:
    if not tok:
        return tuple([0] * perms)
    hs = [_h64(w) for w in tok]
    mod = (1 << 61) - 1
    return tuple(min((0x9E3779B97F4A7C15 * (i + 1) % mod | 1) * h + _h64("p%d" % i) for h in hs) % mod
                 for i in range(perms))


def candidate_pairs(sigs: Dict[str, Tuple[int, ...]], bands: int = 16, rows: int = 4) -> set:
    buckets: Dict[Tuple[int, tuple], List[str]] = defaultdict(list)
    for mid, sig in sigs.items():
        for b in range(bands):
            buckets[(b, sig[b * rows:(b + 1) * rows])].append(mid)
    pairs = set()
    for _k, ids in buckets.items():
        if len(ids) < 2:
            continue
        ids = sorted(ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.add((ids[i], ids[j]))
    return pairs


def build_pairs(rows: List[Tuple[str, str]], ms, max_pairs: int, seed: int) -> List[Dict[str, Any]]:
    tok = {mid: tokens(c) for mid, c in rows}
    sigs = {mid: minhash(t) for mid, t in tok.items()}
    cand = candidate_pairs(sigs)
    out: List[Dict[str, Any]] = []
    for a, b in sorted(cand):
        sj = jaccard(tok[a], tok[b])
        if sj < GATE_THRESHOLD:
            continue
        # 闸门打开 ⇒ 判据真的会被调用（existing 取更长的那个，与调用点的 best_dv 同向）
        longer, shorter = (a, b) if len(tok[a]) >= len(tok[b]) else (b, a)
        det = {m: c for m, c in rows}
        verdict = ms.verify_merge_safety(det[shorter], det[longer])
        out.append({
            "a": longer, "b": shorter, "score": round(sj, 4),
            "existing_len": len(det[longer]), "incoming_len": len(det[shorter]),
            "existing_tokens": len(tok[longer]), "incoming_tokens": len(tok[shorter]),
            "new_tokens": len(new_content_tokens(tok[shorter], tok[longer])),
            "fired": (not verdict.safe), "code": verdict.code,
            "raw_equal_after_norm": ms._norm(det[longer]) == ms._norm(det[shorter]),
            "raw_identical": det[longer] == det[shorter],
        })
            # 候选对数可能很大（同一主题反复入库），按 score 排序后截断保证可复现
    out.sort(key=lambda r: (-r["score"], r["a"], r["b"]))
    if len(out) > max_pairs:
        rnd = random.Random(seed)
        out = sorted(rnd.sample(out, max_pairs), key=lambda r: (-r["score"], r["a"], r["b"]))
    return out


def classify(ms, pairs: List[Dict[str, Any]], contents: Dict[str, str]) -> Dict[str, Any]:
    """按规则统计命中率 + 误杀/漏杀（标签来自独立判据，不引用被判规则的逻辑）。"""
    n = len(pairs)
    fired = Counter(p["code"] for p in pairs if p["fired"])

    # ── R1 ──
    r1 = [p for p in pairs if p["existing_len"] >= ms.MIN_EXISTING_CHARS]
    r1_fire = [p for p in r1 if p["code"] == "content_collapse"]
    r1_tp = [p for p in r1_fire if p["new_tokens"] == 0]
    r1_fp = [p for p in r1_fire if p["new_tokens"] > 0]
    # 漏杀：既有 ≥ 阈值、来料是**纯回声**（无新实词）且更短，但 R1 不判红
    r1_fn = [p for p in r1 if p["code"] != "content_collapse" and p["new_tokens"] == 0
             and p["incoming_len"] < p["existing_len"]]
    ratios = sorted(p["incoming_len"] / max(1, p["existing_len"]) for p in r1)
    below = sum(1 for r in ratios if r < ms.COLLAPSE_RATIO)

    # ── R2 ──（恒真式，见 r2_tautology_proof）
    r2_fire = [p for p in pairs if p["code"] == "source_downgrade"]

    # ── R3 ──
    r3_fire = [p for p in pairs if p["code"] == "duplicate_no_new_evidence"]
    # 误杀：命中 R3 但两行的**原始文本**不同且归一化后仍不同 —— 逻辑上不可能出现，
    # 本项是"判据没被绕过"的自洽检查（若 >0 说明我对判据的理解或调用有误）。
    r3_fp = [p for p in r3_fire
             if (not p["raw_identical"]) and ms._norm(contents[p["a"]]) != ms._norm(contents[p["b"]])]
    # 仅空白/全角/大小写差异 ⇒ 归一化后相等，不算误杀（本来就没有新信息）
    r3_only_format = [p for p in r3_fire if not p["raw_identical"]
                      and ms._norm(contents[p["a"]]) == ms._norm(contents[p["b"]])]
    # 漏杀（严格口径）：**词表完全相同**（无任何新 token，只有顺序/重复差异）却没被判红
    r3_fn_token_identical = [p for p in pairs
                             if p["code"] != "duplicate_no_new_evidence"
                             and tokens(contents[p["a"]]) == tokens(contents[p["b"]])
                             and p["score"] >= R3_NEARMISS]
    near_miss = [p for p in pairs if p["score"] >= R3_NEARMISS
                 and p["code"] != "duplicate_no_new_evidence"]

    return {
        "gate_open_pairs": n,
        "caliber": {
            "denominator": "通过合并闸门的候选对（精确 Jaccard ≥ %.2f）" % GATE_THRESHOLD,
            "corpus": "content 非空且非 enc:v1: 的行（密文无法判读）",
            "r1_scope": "既有文本 ≥ %d 字符的对（R1 的适用域）" % ms.MIN_EXISTING_CHARS,
        },
        "fire_counts": {k: v for k, v in fired.items()},
        "R1_content_collapse": {
            "applicable_pairs": len(r1),
            "fired": len(r1_fire),
            "hit_rate_all_pairs": round(len(r1_fire) / n, 4) if n else 0.0,
            "hit_rate_in_scope": round(len(r1_fire) / len(r1), 4) if r1 else 0.0,
            "true_positive_pure_echo": len(r1_tp),
            "false_positive_new_information": len(r1_fp),
            "false_positive_rate_in_fired": round(len(r1_fp) / len(r1_fire), 4) if r1_fire else 0.0,
            "false_negative_pure_echo_not_fired": len(r1_fn),
            "length_ratio_min": round(ratios[0], 4) if ratios else None,
            "length_ratio_p05": round(ratios[max(0, int(0.05 * len(ratios)) - 1)], 4) if ratios else None,
            "length_ratio_p50": round(ratios[len(ratios) // 2], 4) if ratios else None,
            "pairs_below_collapse_ratio": below,
            "collapse_ratio": ms.COLLAPSE_RATIO,
            "structural_verdict": (
                "DEAD-AT-CALLSITE（结构性不可达）：闸门要求 Jaccard ≥ %.2f ⇒ 必然有 "
                "|∩| ≥ %.2f·|A∪B| ≥ %.2f·|A| ⇒ 来料 token 数 ≥ %.0f%% 的既有 token 数；"
                "而 R1 触发要求**字符数** < %.0f%%。闸门与判据在算术上互斥 —— 实测 "
                "%d/%d 的闸门内对的长度比低于 R1 阈值（最小长度比 %.2f，中位 %.2f）。"
                % (GATE_THRESHOLD, GATE_THRESHOLD, GATE_THRESHOLD, GATE_THRESHOLD * 100,
                   ms.COLLAPSE_RATIO * 100, below, len(r1),
                   ratios[0] if ratios else -1, ratios[len(ratios) // 2] if ratios else -1)),
        },
        "R2_source_downgrade": {
            "fired": len(r2_fire),
            "hit_rate": 0.0 if not r2_fire else round(len(r2_fire) / n, 4),
            "note": "恒真式 ⇒ 线上命中率恒 0（证明见 r2_tautology_proof）",
        },
        "R3_duplicate_no_new_evidence": {
            "fired": len(r3_fire),
            "hit_rate_all_pairs": round(len(r3_fire) / n, 4) if n else 0.0,
            "false_positive_content_bearing": len(r3_fp),
            "fired_but_only_format_diff": len(r3_only_format),
            "false_negative_token_identical": len(r3_fn_token_identical),
            "near_miss_pairs_jaccard_ge_95": len(near_miss),
            "r3_nearmiss_threshold": R3_NEARMISS,
            "fn_caliber_caveat": ("`near_miss_pairs_jaccard_ge_95` 只是**上界候选**：Jaccard 0.95 的对"
                                  "常常只差一个数字（播放量/时间戳），那算**新证据**、不该拦。"
                                  "严格的漏杀口径是 `false_negative_token_identical`（词表完全相同）。"),
        },
    }


def pick_samples(pairs: List[Dict[str, Any]], ms, rows_map: Dict[str, str],
                 k: int = 10) -> Dict[str, List[Dict[str, Any]]]:
    """每规则 10 正例 + 10 反例（正例 = 该规则判红的对；反例 = 闸门开着但不判红的对）。

    R1 的"正例"特别说明：**闸门内不存在 R1 正例**（见 `structural_verdict`）。
    故本函数另给 `R1_pos_constructed`：用**真实语料行**拼出 R1 的触发形状
    （既有 ≥120 字符、来料 <35% 且非子串），标注为 `constructed_shape`，用来证明
    "判据本身能响，只是响不到闸门内的对"。这不是伪造数据：两行都是库内真实文本。
    """
    def rec(p: Dict[str, Any], label: str) -> Dict[str, Any]:
        prev = 110
        return {
            "label": label,
            "score": p["score"],
            "code": p["code"] or "(safe)",
            "existing": {"memory_id": p["a"], "len": p["existing_len"],
                         "preview": rows_map[p["a"]][:prev]},
            "incoming": {"memory_id": p["b"], "len": p["incoming_len"],
                         "preview": rows_map[p["b"]][:prev]},
            "new_content_tokens": p["new_tokens"],
        }

    def rec_constructed(ex_id: str, in_id: str, code: str, detail: str) -> Dict[str, Any]:
        return {
            "label": "constructed_shape",
            "score": None,
            "code": code,
            "detail": detail,
            "existing": {"memory_id": ex_id, "len": len(rows_map[ex_id]),
                         "preview": rows_map[ex_id][:110]},
            "incoming": {"memory_id": in_id, "len": len(rows_map[in_id]),
                         "preview": rows_map[in_id][:110]},
        }

    out: Dict[str, List[Dict[str, Any]]] = {}
    r1_scope = [p for p in pairs if p["existing_len"] >= ms.MIN_EXISTING_CHARS]
    out["R1_pos_in_gate"] = [rec(p, "positive") for p in r1_scope
                             if p["code"] == "content_collapse"][:k]
    # 形状正例：真实行两两拼 R1 触发形状（长既有 + 短来料，且不是子串）
    long_rows = sorted(r1_scope, key=lambda p: -p["existing_len"])[:60]
    short_rows = sorted(r1_scope, key=lambda p: p["incoming_len"])[:60]
    constructed: List[Dict[str, Any]] = []
    for lp in long_rows:
        ex = rows_map[lp["a"]]
        for sp in short_rows:
            inc = rows_map[sp["b"]]
            if len(ms._norm(inc)) >= ms.COLLAPSE_RATIO * len(ms._norm(ex)) or not inc.strip():
                continue
            v = ms.verify_merge_safety(inc, ex)
            if v.code == "content_collapse":
                constructed.append(rec_constructed(lp["a"], sp["b"], v.code, v.detail))
                break
        if len(constructed) >= k:
            break
    out["R1_pos_constructed"] = constructed
    out["R1_neg"] = [rec(p, "negative") for p in r1_scope
                     if p["code"] != "content_collapse"
                     and tokens(rows_map[p["b"]]) - tokens(rows_map[p["a"]])][:k]
    out["R1_fn_pure_echo_not_caught"] = [rec(p, "negative") for p in r1_scope
                                         if p["code"] != "content_collapse"
                                         and p["new_tokens"] == 0
                                         and p["incoming_len"] < p["existing_len"]][:k]
    out["R2_pos"] = [rec(p, "positive") for p in pairs if p["code"] == "source_downgrade"][:k]
    out["R2_neg"] = [rec(p, "negative") for p in pairs if p["code"] != "source_downgrade"][:k]
    out["R3_pos"] = [rec(p, "positive") for p in pairs
                     if p["code"] == "duplicate_no_new_evidence"][:k]
    out["R3_neg"] = [rec(p, "negative") for p in pairs
                     if p["code"] != "duplicate_no_new_evidence" and p["score"] >= 0.9][:k]
    out["R3_fn_token_identical_not_caught"] = [
        rec(p, "negative") for p in pairs
        if p["code"] != "duplicate_no_new_evidence"
        and tokens(rows_map[p["a"]]) == tokens(rows_map[p["b"]])
        and p["score"] >= R3_NEARMISS][:k]
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="合并安全判据在生产语料上的标定（只读）")
    ap.add_argument("--db", default=os.environ.get("TRINITY_STORE", DEFAULT_DB))
    ap.add_argument("--max-pairs", type=int, default=40000)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--json", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ms = load_merge_safety()
    db_file = args.db
    if os.path.isdir(db_file):
        db_file = os.path.join(db_file, "trinity_store.db")
    con = connect_ro(args.db)
    rows = con.execute(
        "select memory_id, content from memories where content is not null "
        "and trim(content) <> '' and content not like 'enc:v1:%'").fetchall()
    rows = [(str(a), str(b)) for a, b in rows]
    total = con.execute("select count(*) from memories").fetchone()[0]
    con.close()
    t0 = time.time()
    pairs = build_pairs(rows, ms, args.max_pairs, args.seed)
    stats = classify(ms, pairs, {m: c for m, c in rows})
    proof = r2_tautology_proof(ms)
    samples = pick_samples(pairs, ms, {m: c for m, c in rows})
    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "readonly": True,
        "db": db_file,
        "db_rows_total": total,
        "readable_rows": len(rows),
        "readable_share": round(len(rows) / max(1, total), 4),
        "seed": args.seed,
        "elapsed_s": round(time.time() - t0, 2),
        "rules": stats,
        "r2_tautology_proof": proof,
        "samples": samples,
    }
    if not args.quiet:
        print(json.dumps({k: v for k, v in rep.items() if k != "samples"},
                         ensure_ascii=False, indent=1))
        for key, items in samples.items():
            print("\n== %s (%d) ==" % (key, len(items)))
            for it in items:
                print("  score=%s code=%s new_tokens=%s"
                      % (it.get("score"), it.get("code"), it.get("new_content_tokens")))
                print("    existing %s: %s" % (it["existing"]["memory_id"],
                                               it["existing"]["preview"][:90].replace("\n", " | ")))
                print("    incoming %s: %s" % (it["incoming"]["memory_id"],
                                               it["incoming"]["preview"][:90].replace("\n", " | ")))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
        print("-> %s" % args.json)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/merge_safety_calibration.py::<module>")
    raise SystemExit(main())

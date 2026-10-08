#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""corpus_quality_audit.py —— 语料卫生与利用率**只读**量测（t4 / 2026-10-06）。

## 为什么有这个脚本

本仓反复栽在"同一个指标三个数、谁都不说明分母"上（冷池 80.4%/62.2%/31.4% 就是
活例子）。所以本脚本的**第一位设计目标不是算数，而是把口径钉死**：
每个比率都必须带 `caliber`（分母是什么、取哪一列、限定哪个 status）与
`reproduce`（可复制的等价 SQL）字段，任何一个数字都能被第三方用 sqlite3 复算。

## 四个比率（外加利用率与重复放大）

| 指标 | 口径（分母） | 取哪一列 |
|---|---|---|
| 精确重复率 | 全库行数 / active 行数 | `sha256_hash`（明文指纹，**不是** `content`：93%+ 行是 `enc:v1:` 密文，密文逐行不同，用 content 量会把重复率读到 ~0.09%） |
| 近重复率 | 可读文本子集内**确定性抽样** N 行 | MinHash(5-gram)×64 + LSH，Jaccard ≥ 0.8 连通分量 |
| 无标签率 | 全库 / active | `tags IS NULL` **与** `'[]'` 分开计（历史报告只数了 `'[]'`，漏了 NULL，差 1.48×） |
| 自产率 | 全库 / active / 近 N 天写入 | 三条独立信号（内容生成头 / 生产者命名空间 / metadata 派生标记）分别报，不合成单值 |
| 冷池率 | **active**（不是全库） | 见 `caliber`：`access_count=0` 为权威口径；`last_accessed_at IS NULL` 已证伪（见 CALIBER-CAVEATS） |

## 硬约束（本脚本遵守）

* **只读**：SQLite 连接一律 `file:...?mode=ro`；不 import `trinity` 包（那会连带跑
  second_brain 初始化链，实测会打印横幅并触发旁路清理副作用）。
* 不解密、不改库、不写任何 `~/.trinity/` 下的文件；产物只写到 `--json` 指定的路径。

## 用法

    python scripts/corpus_quality_audit.py                     # 人类可读报告
    python scripts/corpus_quality_audit.py --json out.json     # 同时落 JSON（给报告引用）
    python scripts/corpus_quality_audit.py --near-sample 20000 --seed 20261006
    python scripts/corpus_quality_audit.py --db <path>

退出码：0 正常；2 库不可读。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import logging

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db")


def resolve_db(value: str) -> str:
    """把「store 目录」或「db 文件」都解析成 db 文件路径。

    为什么需要这一步：本机环境里 `TRINITY_STORE` 被设成**目录**
    （`C:\\Users\\Administrator\\.trinity\\store-restored`），而别的脚本把它当文件用。
    直接 `sqlite3.connect("file:<目录>?mode=ro")` 会抛 OperationalError: unable to open
    database file —— 一个纯粹由口径/约定不一致造成的失败。这里显式兼容两种形态，
    并且**不做静默兜底**：目录里没有 trinity_store.db 就原样返回，让上层报错。
    """
    v = os.path.expanduser(value or "")
    if v and os.path.isdir(v):
        cand = os.path.join(v, "trinity_store.db")
        return cand
    return v


#: 生成式头的标记（内容信号 S1）。顺序无关，用于自产率与自指深度。
GEN_MARKERS = (
    "[自动关联]",
    "[self-reflection]",
    "[会话自动摘要]",
    "[COMPRESSED SUMMARY",
    "[AUTO-COMPRESSED]",
    "[kb-section:",
    "[kb-table-row:",
    "[procedure]",
    "[experience]",
    "[Decision: Goal lifecycle",
    "[evolution]",
    "[milestone]",
    "[SESSION]",
)

#: 机器回灌型生产者（生产者信号 S2）。判定依据：这些 agent_id 的写入不来自外部
#: 输入，而是本系统自己的采集/派生回路（见 CORPUS-QUALITY.md §2 根因）。
GEN_PRODUCERS = (
    "kb-harvester", "perception", "doc-fusion", "brain-procedure", "brain-consumer",
    "brain-cycle", "evolution", "cognition", "skill-library", "observation-builder",
    "recurrence-consolidate", "compress-econ", "agent-alpha", "market", "curator",
)

#: 公开评测/压测命名空间前缀——它们写入的是**评测语料**，不是这台机器的记忆。
EVAL_PREFIXES = ("eval-", "eval_", "ablate-", "ablate_", "bench-", "bench_",
                 "locomo-", "lme-", "lmev2-", "stress-", "scale-", "sig_", "ab_",
                 "ckpt-test", "crash-test", "test-stim")

_WS = re.compile(r"\s+")


# ──────────────────────────────────────────────────────────── 基础工具

def connect_ro(path: str):
    """只读连接。绝不使用默认（可写）模式。"""
    import sqlite3
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def norm_text(s: Optional[str]) -> str:
    """NFKC + 折叠空白 + 小写：近重复的归一化口径。"""
    if not s:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKC", str(s))).strip().lower()


def shingles(text: str, k: int = 5, cap: int = 400) -> List[str]:
    """k-gram 字符 shingle；cap 限制长文本的 shingle 数（保证可复现的采样上界）。"""
    t = norm_text(text)
    if len(t) <= k:
        return [t] if t else []
    return [t[i:i + k] for i in range(min(len(t) - k + 1, cap))]


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def minhash(sh_list: Sequence[str], perms: int = 64) -> Tuple[int, ...]:
    """固定参数 MinHash：(a*h + b) mod (2**61-1) 的确定性置换。"""
    if not sh_list:
        return tuple([0] * perms)
    hs = [_h64(s) for s in sh_list]
    mod = (1 << 61) - 1
    out = []
    for i in range(perms):
        a = 0x9E3779B97F4A7C15 * (i + 1) % mod | 1
        b = _h64("perm%d" % i)
        out.append(min((a * h + b) % mod for h in hs))
    return tuple(out)


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = len(a | b)
    return len(a & b) / u if u else 0.0


# ──────────────────────────────────────────────────────────── 指标实现

def metric_exact_dup(con, scope: str, status_filter: str = "") -> Dict[str, Any]:
    """精确重复：1 - distinct(sha256_hash)/rows。分母 = scope 内的行数。"""
    where = ("where " + status_filter) if status_filter else ""
    and_status = (" and " + status_filter) if status_filter else ""
    q = con.execute
    total = q("select count(*) from memories %s" % where).fetchone()[0]
    d_sha = q("select count(distinct sha256_hash) from memories %s" % where).fetchone()[0]
    d_ch = q("select count(distinct content_hash) from memories where content_hash is not null%s"
             % and_status).fetchone()[0]
    d_raw = q("select count(distinct content) from memories %s" % where).fetchone()[0]
    groups = q("select count(*) from (select sha256_hash from memories %s "
               "group by sha256_hash having count(*) > 1)%s"
               % (where, "")).fetchone()[0]
    return {
        "scope": scope,
        "rows": total,
        "distinct_sha256": d_sha,
        "redundant_rows_sha256": total - d_sha,
        "exact_dup_rate_sha256": round(1.0 - d_sha / total, 6) if total else 0.0,
        "distinct_content_hash": d_ch,
        "distinct_content_raw": d_raw,
        "exact_dup_rate_raw_content": round(1.0 - d_raw / total, 6) if total else 0.0,
        "dup_groups_sha256": groups,
        "caliber": ("分母 = %s 的行数；重复键 = sha256_hash（明文指纹，adapter 于写入时"
                    "对**明文**计算 ⇒ 密文行也可比）。raw content 口径同时给出，用于证明"
                    "「用 content 量重复会读到 ~0」这个坑。" % scope),
        "reproduce": [
            "SELECT count(*) FROM memories %s;" % where,
            "SELECT count(DISTINCT sha256_hash) FROM memories %s;" % where,
            "SELECT count(DISTINCT content) FROM memories %s;" % where,
        ],
    }


def metric_dup_mechanism(con) -> Dict[str, Any]:
    """重复的**机制指纹**：归档是否给去重"洗白"。"""
    q = con.execute
    tot_groups = q("select count(*) from (select sha256_hash from memories "
                   "group by sha256_hash having count(*)>1)").fetchone()[0]
    mixed = q("select count(*) from (select sha256_hash from memories group by sha256_hash "
              "having count(*)>1 and sum(case when status='active' then 1 else 0 end)>0 "
              "and sum(case when status<>'active' then 1 else 0 end)>0)").fetchone()[0]
    all_dead = q("select count(*) from (select sha256_hash from memories group by sha256_hash "
                 "having count(*)>1 and sum(case when status='active' then 1 else 0 end)=0)").fetchone()[0]
    dead_rows = q("select coalesce(sum(n),0) from (select count(*) n from memories "
                  "group by sha256_hash having count(*)>1 "
                  "and sum(case when status='active' then 1 else 0 end)=0)").fetchone()[0]
    active_dup = q("select count(*) from (select agent_id, content_hash from memories "
                   "where status='active' and content_hash is not null group by 1,2 "
                   "having count(*)>1)").fetchone()[0]
    span7 = q("select count(*) from (select sha256_hash from memories group by sha256_hash "
              "having count(*)>1 and julianday(max(created_at))-julianday(min(created_at))>7)").fetchone()[0]
    worst = q("select sha256_hash, count(*) n, sum(case when status='active' then 1 else 0 end) act, "
              "min(created_at), max(created_at), group_concat(distinct agent_id), "
              "group_concat(distinct category) from memories group by sha256_hash "
              "having n>1 order by n desc limit 5").fetchall()
    return {
        "dup_groups": tot_groups,
        "groups_with_active_and_nonactive": mixed,
        "groups_with_zero_active": all_dead,
        "rows_in_fully_archived_dup_groups": dead_rows,
        "active_duplicate_groups": active_dup,
        "groups_spanning_more_than_7_days": span7,
        "worst_groups": [
            {"sha256": r[0][:16], "n": r[1], "active": r[2], "first": r[3],
             "last": r[4], "agents": r[5], "categories": r[6]} for r in worst
        ],
        "caliber": ("「重复组」= 按 sha256_hash 分组的 count>1；"
                    "active_duplicate_groups 期望为 0（唯一索引 idx_memories_content_hash 生效）；"
                    "groups_with_active_and_nonactive 是「归档后被重新写入」的直接指纹。"),
        "reproduce": [
            "SELECT count(*), sum(1) FROM (SELECT sha256_hash FROM memories "
            "GROUP BY sha256_hash HAVING count(*)>1);",
            "SELECT count(*) FROM (SELECT status, sha256_hash FROM memories GROUP BY sha256_hash "
            "HAVING count(*)>1 AND sum(status='active')>0 AND sum(status<>'active')>0);",
            "SELECT count(*) FROM (SELECT agent_id, content_hash FROM memories "
            "WHERE status='active' AND content_hash IS NOT NULL "
            "GROUP BY 1,2 HAVING count(*)>1);",
        ],
    }


def metric_near_dup(con, sample_n: int, seed: int, threshold: float = 0.8) -> Dict[str, Any]:
    """近重复：在**可读文本子集**内做确定性抽样 + MinHash/LSH。

    为什么只在可读子集上做：`content` 90%+ 是 `enc:v1:` 密文，密文逐行不同
    ⇒ 对密文做近重复**恒等于 0**，那是个假读数。故本指标的分母明确写死为
    「content 非密文且非空的行」。
    """
    rows = con.execute(
        "select memory_id, content from memories "
        "where content is not null and trim(content) <> '' and content not like 'enc:v1:%'"
    ).fetchall()
    total_readable = len(rows)
    rnd = random.Random(seed)
    pool = rows if sample_n <= 0 or total_readable <= sample_n else rnd.sample(rows, sample_n)

    sigs: Dict[str, Tuple[int, ...]] = {}
    shsets: Dict[str, set] = {}
    for mid, content in pool:
        sh = shingles(content)
        shsets[mid] = set(sh)
        sigs[mid] = minhash(sh)

    # LSH：16 段 × 4 行（64 个 hash）
    bands = 16
    rows_per_band = 4
    buckets: Dict[Tuple[int, Tuple[int, ...]], List[str]] = defaultdict(list)
    for mid, sig in sigs.items():
        for b in range(bands):
            key = (b, sig[b * rows_per_band:(b + 1) * rows_per_band])
            buckets[key].append(mid)

    parent: Dict[str, str] = {mid: mid for mid in sigs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    cand_pairs = 0
    verified_pairs = []
    for (_b, _k), ids in buckets.items():
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                cand_pairs += 1
                a, b = ids[i], ids[j]
                if find(a) == find(b):
                    continue
                sj = jaccard(shsets[a], shsets[b])
                if sj >= threshold:
                    union(a, b)
                    if len(verified_pairs) < 40:
                        verified_pairs.append({"a": a, "b": b, "jaccard": round(sj, 4)})

    clusters: Dict[str, List[str]] = defaultdict(list)
    for mid in sigs:
        clusters[find(mid)].append(mid)
    dup_clusters = {k: v for k, v in clusters.items() if len(v) > 1}
    clustered_rows = sum(len(v) for v in dup_clusters.values())
    n = len(sigs)

    # 与历史口径可比的"前 120 字符塌缩"（全库、无需明文）
    head_groups = con.execute(
        "select count(*), coalesce(sum(n),0) from (select substr(content,1,120) h, count(*) n "
        "from memories group by 1 having count(*)>1 and count(distinct content)>1)"
    ).fetchone()

    return {
        "caliber": ("分母 = **可读文本子集**（content 非空且非 enc:v1:）内的确定性抽样 %d 行"
                    "（seed=%d）；判近重复 = 5-gram shingle 的 MinHash(64)×LSH(16band×4row) "
                    "候选对 + Jaccard≥%.2f 复核，连通分量即重复簇。密文行**结构性排除**"
                    "（密文互不相同，纳入会得到恒 0 的假读数）。" % (len(pool), seed, threshold)),
        "readable_rows_total": total_readable,
        "readable_share_of_corpus": round(
            total_readable / max(1, con.execute("select count(*) from memories").fetchone()[0]), 6),
        "sampled_rows": n,
        "seed": seed,
        "lsh_candidate_pairs": cand_pairs,
        "near_dup_clusters": len(dup_clusters),
        "rows_in_near_dup_clusters": clustered_rows,
        "near_dup_rate": round(clustered_rows / n, 6) if n else 0.0,
        "near_dup_excess_rate": round((clustered_rows - len(dup_clusters)) / n, 6) if n else 0.0,
        "largest_clusters": sorted(
            [{"size": len(v), "members": v[:6]} for v in dup_clusters.values()],
            key=lambda x: -x["size"])[:5],
        "sample_verified_pairs": verified_pairs[:10],
        "prefix120_collapse_groups": head_groups[0],
        "prefix120_collapse_rows": head_groups[1],
        "prefix120_caliber": ("历史可比口径（PG 侧曾报 100 组/473 行）：前 120 字符相同但全文不同的组；"
                              "分母 = 全库行数。它偏保守（长文本前缀一致即算），与 MinHash 口径不可互换。"),
        "reproduce": [
            "SELECT count(*) FROM memories WHERE content NOT LIKE 'enc:v1:%' AND trim(content)<>'';",
            "SELECT count(*), sum(n) FROM (SELECT substr(content,1,120) h, count(*) n FROM memories "
            "GROUP BY 1 HAVING count(*)>1 AND count(DISTINCT content)>1);",
            "python scripts/corpus_quality_audit.py --near-sample %d --seed %d" % (len(pool), seed),
        ],
    }


def metric_tags(con) -> Dict[str, Any]:
    """无标签率：**NULL 与 '[]' 分开计**（历史报告只数 '[]'，漏 NULL，量级差 1.48×）。"""
    q = con.execute
    tot = q("select count(*) from memories").fetchone()[0]
    act = q("select count(*) from memories where status='active'").fetchone()[0]
    out = {}
    for name, where in (("all", ""), ("active", "where status='active'")):
        nulls = q("select count(*) from memories %s%s" % (where, " and " if where else "where ")
                  + "tags is null").fetchone()[0]
        empties = q("select count(*) from memories %s%s" % (where, " and " if where else "where ")
                    + "trim(tags) in ('','[]','{}')").fetchone()[0]
        tagless = q("select count(*) from memories %s%s" % (where, " and " if where else "where ")
                    + "(tags is null or trim(tags) in ('','[]','{}'))").fetchone()[0]
        denom = tot if name == "all" else act
        out[name] = {
            "denominator": denom,
            "tags_null": nulls,
            "tags_empty_array": empties,
            "tagless_total": tagless,
            "tagless_rate": round(tagless / denom, 6) if denom else 0.0,
        }
    out["caliber"] = ("分母 = 全库行数（all）/ active 行数（active）；无标签 = tags IS NULL "
                      "**或** trim(tags) ∈ {'', '[]', '{}'}。NULL 与 '[]' 必须分开报："
                      "历史上只数 '[]' 会低估 1.48×。")
    out["reproduce"] = [
        "SELECT count(*) FROM memories WHERE tags IS NULL;",
        "SELECT count(*) FROM memories WHERE trim(tags) IN ('','[]','{}');",
        "SELECT count(*) FROM memories WHERE (tags IS NULL OR trim(tags) IN ('','[]','{}'));",
    ]
    return out


def _producer_class(agent: str) -> str:
    a = (agent or "").lower()
    if a.startswith(EVAL_PREFIXES):
        return "eval_or_stress"
    if a in GEN_PRODUCERS or a.startswith("brain") or a.startswith("dsh-session"):
        return "self_generated"
    if a in ("default", ""):
        return "unattributed"
    return "external_or_agent"


def metric_self_generated(con, window_days: int) -> Dict[str, Any]:
    """自产率：三条独立信号 + 生产者分类，**分别报数**（不合成单值）。"""
    q = con.execute
    tot = q("select count(*) from memories").fetchone()[0]
    act = q("select count(*) from memories where status='active'").fetchone()[0]
    w_expr = "created_at >= datetime('now','-%d days')" % int(window_days)
    w_tot = q("select count(*) from memories where %s" % w_expr).fetchone()[0]

    def sig(where_extra: str, denom_scope: str) -> Dict[str, Any]:
        # ⚠️ 2026-10-06 实测踩坑：`where_extra` 内部的 `or` 链**必须**整体加括号。
        # 否则 `where status='active' and a or b or c` 会被 SQLite 解析成
        # `(status='active' and a) or b or c` ⇒ **status 限定被 or 链吃掉**，
        # 得到「全库的机器生产者」= 25,454 行，active 口径被读成 111.55%（>100%）。
        # 这个 bug 在本轮真发生过并被抓出来（口径自洽检查：比率不得 >100%）。
        if denom_scope == "active":
            base = "status='active'"
            denom = act
        elif denom_scope == "window":
            base = w_expr
            denom = w_tot
        else:
            base = "1=1"
            denom = tot
        n = q("select count(*) from memories where %s and (%s)"
              % (base, where_extra)).fetchone()[0]
        res = {"rows": n, "denominator": denom, "rate": round(n / denom, 6) if denom else 0.0}
        assert n <= denom, "口径自洽失败：命中数 %d > 分母 %d（%s）" % (n, denom, where_extra[:60])
        return res

    marker_or = " or ".join("content like '%%%s%%'" % m.replace("'", "''") for m in GEN_MARKERS)
    prod_or = " or ".join("agent_id = '%s'" % p for p in GEN_PRODUCERS)
    eval_or = " or ".join("agent_id like '%s%%'" % p for p in EVAL_PREFIXES)
    meta_or = ("metadata like '%\"fused_at\"%' or metadata like '%\"source_file\"%' "
               "or metadata like '%session_distill%'")

    out: Dict[str, Any] = {"window_days": int(window_days)}
    for scope in ("all", "active", "window"):
        out[scope] = {
            "denominator_rows": {"all": tot, "active": act, "window": w_tot}[scope],
            "window_days": int(window_days) if scope == "window" else None,
            "S1_content_marker": sig(marker_or, scope),
            "S2_producer_namespace": sig(prod_or, scope),
            "S3_metadata_derived": sig(meta_or, scope),
            "S1_or_S2": sig("(%s) or (%s)" % (marker_or, prod_or), scope),
            "S1_or_S2_or_S3": sig("(%s) or (%s) or (%s)" % (marker_or, prod_or, meta_or), scope),
            "eval_or_stress_namespace": sig(eval_or, scope),
        }
    by_class = q("select case when 1=1 then agent_id end a, count(*) from memories group by 1").fetchall()
    cls = Counter()
    for agent, n in by_class:
        cls[_producer_class(agent)] += n
    wcls = Counter()
    for agent, n in q("select agent_id, count(*) from memories where %s group by 1" % w_expr).fetchall():
        wcls[_producer_class(agent)] += n
    out["producer_class_all"] = dict(cls)
    out["producer_class_window"] = dict(wcls)
    out["producer_class_caliber"] = (
        "eval_or_stress = agent_id 前缀属于评测/压测命名空间；self_generated = 本系统自身"
        "采集/派生回路的生产者（kb-harvester/perception/doc-fusion/brain-*/dsh-session-*/evolution…）；"
        "unattributed = default 或空。三者互斥、并集为全部行。")
    out["caliber"] = ("自产**不合成单值**：三条信号各自给分母（all / active / 近 %d 天写入）"
                      "与命中率。历史报告报的 55.5%%–91.7%% 之所以是个区间，正是因为口径不同。"
                      % int(window_days))
    out["reproduce"] = [
        "SELECT count(*) FROM memories WHERE " + prod_or + ";",
        "SELECT count(*) FROM memories WHERE " + marker_or + ";",
        "SELECT count(*) FROM memories WHERE " + meta_or + ";",
        "SELECT count(*) FROM memories WHERE created_at >= datetime('now','-%d days');" % int(window_days),
    ]
    return out


def metric_cold_pool(con, window_days: int = 30) -> Dict[str, Any]:
    """冷池：四个口径并列 + 证伪证据（哪个能用、哪个不能用）。"""
    q = con.execute
    # 「被读」的可辩护定义：access_count 自增 **或** last_accessed_at 晚于 created_at。
    # 后半句正是为了把「写入时打的时间戳」与「事后真的被 touch 过」分开。
    LAN = "julianday(replace(substr(last_accessed_at,1,19),'T',' '))"
    CRE = "julianday(replace(substr(created_at,1,19),'T',' '))"
    READ = "coalesce(access_count,0)>0 or (%s is not null and %s > %s)" % (LAN, LAN, CRE)
    act = q("select count(*) from memories where status='active'").fetchone()[0]
    acc0 = q("select count(*) from memories where status='active' "
             "and coalesce(access_count,0)=0").fetchone()[0]
    lan = q("select count(*) from memories where status='active' "
            "and last_accessed_at is null").fetchone()[0]
    both = q("select count(*) from memories where status='active' "
             "and coalesce(access_count,0)=0 and last_accessed_at is null").fetchone()[0]
    acc0_stamped = q("select count(*) from memories where status='active' "
                     "and coalesce(access_count,0)=0 and last_accessed_at is not null").fetchone()[0]
    stamp_artifact = q(
        "select count(*) from memories where status='active' and coalesce(access_count,0)=0 "
        "and last_accessed_at is not null and abs("
        "julianday(replace(substr(last_accessed_at,1,19),'T',' ')) - "
        "julianday(replace(substr(created_at,1,19),'T',' '))) <= (60.0/86400.0)"
    ).fetchone()[0]
    read_rows = q("select count(*) from memories where status='active' and (%s)" % READ).fetchone()[0]
    cold_d = act - read_rows
    acc_pos_lan_null = q("select count(*) from memories where status='active' "
                         "and coalesce(access_count,0)>0 and last_accessed_at is null").fetchone()[0]
    fresh_cold = q("select count(*) from memories where status='active' "
                   "and coalesce(access_count,0)=0 and created_at >= datetime('now','-%d days')"
                   % int(window_days)).fetchone()[0]
    by_status = q("select status, count(*), sum(case when coalesce(access_count,0)=0 then 1 else 0 end) "
                  "from memories group by 1 order by 2 desc").fetchall()
    by_prod = q("select agent_id, count(*) n, sum(case when coalesce(access_count,0)=0 then 1 else 0 end) cold "
                "from memories where status='active' group by 1 having n>=300 order by n desc limit 12").fetchall()
    return {
        "active_rows": act,
        "caliber_A_access_count_0": {
            "rows": acc0, "rate": round(acc0 / act, 6) if act else 0.0,
            "verdict": "下界（可跨库对齐）",
            "why": ("access_count 只在部分读路径自增（DEFAULT 0），不受写入时刻戳污染。"
                    "两库该口径几乎同值（本轮 SQLite vs PG 差 ~2.8pp）⇒ 唯一可直接对齐的口径，"
                    "但它**低估**冷池（有读路径不 bump 它）。"),
        },
        "caliber_B_last_accessed_at_is_null": {
            "rows": lan, "rate": round(lan / act, 6) if act else 0.0,
            "verdict": "REFUTED（已证伪，禁止用作冷池率）",
            "why": ("实测 %d 行的 last_accessed_at 在**写入时**就被打上（与 created_at 相差 ≤60s）"
                    "⇒ 本口径把 %d 条从未被读的条目算成【读过】，**低估冷池 %d 行（真冷的 %.0f%%）**。"
                    % (stamp_artifact, acc0_stamped, acc0_stamped,
                       100.0 * acc0_stamped / max(1, acc0))),
        },
        "caliber_C_intersection": {
            "rows": both, "rate": round(both / act, 6) if act else 0.0,
            "verdict": "最严下界（两信号同时为冷）",
        },
        "caliber_D_access_count_0_and_not_touched_after_created": {
            "rows": cold_d, "rate": round(cold_d / act, 6) if act else 0.0,
            # ⚠️ 2026-10-06（t21，verifier F-口径D）：**原表述已更正** —— 原先写的是
            #   「本库可辩护口径（推荐在 SQLite 上用它）」，但实测它与 A **逐值相同**：
            #   `acc=0 且 last_accessed_at > created_at` 的行数 = 0
            #   ⇒ `acc=0 AND NOT(> created_at)` ≡ `acc=0` ⇒ **零分离力**，不是独立口径。
            #   保留原句与数字以便复核，判定改为"与 A 等价，不单独列举"。
            # 复核命令（只读）：见 t21 报告 §13.4 / CALIBER-CAVEATS；
            # 期望：rows == caliber_A.rows 且 separation.acc0_but_touched_later == 0。
            "verdict": "与 A 等价（零分离力）——不单独列举（原判「推荐在 SQLite 上用它」已于 t21 更正）",
            "read_definition": "被读 := access_count>0 OR last_accessed_at > created_at",
            "why": ("把「写入时打的时间戳」从「事后被 touch」里剥离后，剩下的才是真冷。"
                    "它仍然依赖 access_count 的覆盖度 ⇒ 是**下界**，不是与 PG last_retrieved_at 同义的数。"),
            "equivalent_to_A": cold_d == acc0,
            "original_verdict_before_t21": "本库可辩护口径（推荐在 SQLite 上用它）",
        },
        "separation": {
            "acc0_but_touched_later": q(
                "select count(*) from memories where status='active' and coalesce(access_count,0)=0 "
                "and last_accessed_at is not null and abs("
                "julianday(replace(substr(last_accessed_at,1,19),'T',' ')) - "
                "julianday(replace(substr(created_at,1,19),'T',' '))) > (60.0/86400.0)"
            ).fetchone()[0],
            "note": ("这个数 = 「acc=0 但被事后 touch」的行数。为 0 ⇒ D 与 A 逐值相同、零分离力；"
                     ">0 ⇒ D 才是一个独立口径（可把写入时打戳剥离掉）。"),
        },
        "consistency": {
            "acc0_but_last_accessed_not_null": acc0_stamped,
            "of_which_stamped_at_write_time": stamp_artifact,
            "acc_positive_but_last_accessed_null": acc_pos_lan_null,
            "cold_A_minus_cold_D": acc0 - cold_d,
        },
        "cold_new_within_window": {"rows": fresh_cold, "window_days": int(window_days),
                                   "share_of_cold": round(fresh_cold / max(1, acc0), 6)},
        "by_status": [{"status": s, "rows": n, "acc0": c,
                       "acc0_rate": round(c / n, 6) if n else 0.0} for s, n, c in by_status],
        "cold_by_producer": [{"producer": a or "(null)", "active": n, "cold": c,
                              "cold_rate": round(c / n, 6) if n else 0.0} for a, n, c in by_prod],
        "caliber": ("分母一律 = **active 行数**（不是全库：archived 天然 95% 冷，混进来会虚高）。"
                    "四个口径并列，SQLite 上推荐 D（可辩护），跨库/与快照对齐用 PG 的 "
                    "last_retrieved_at（见 cold_pool_pg 段）。"),
        "reproduce": [
            "SELECT count(*) FROM memories WHERE status='active';",
            "SELECT count(*) FROM memories WHERE status='active' AND coalesce(access_count,0)=0;",
            "SELECT count(*) FROM memories WHERE status='active' AND last_accessed_at IS NULL;",
            "SELECT count(*) FROM memories WHERE status='active' AND NOT (coalesce(access_count,0)>0 "
            "OR (julianday(replace(substr(last_accessed_at,1,19),'T',' ')) IS NOT NULL "
            "AND julianday(replace(substr(last_accessed_at,1,19),'T',' ')) > "
            "julianday(replace(substr(created_at,1,19),'T',' '))));",
        ],
    }


def metric_cold_pool_pg(days: int = 30) -> Dict[str, Any]:
    """PG 侧冷池（`last_retrieved_at` 只存在于 PG）——与 AGENTS.md 快照 / t9 同口径对账。

    只读：`set_session(readonly=True)`；连不上就 fail-soft 返回 unavailable（不猜数）。
    """
    creds: Dict[str, str] = {}
    try:
        p = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8-sig", errors="replace"):
                if ":" in line and line.strip() and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    creds[k.strip()] = v.strip().strip("'").strip('"')
    except Exception:
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/corpus_quality_audit.py::metric_cold_pool_pg")
    try:
        import psycopg2
        pg = psycopg2.connect(
            host="127.0.0.1", port=5432,
            dbname=creds.get("TRINITY_PG_DB", "trinity"),
            user=creds.get("TRINITY_PG_USER", "postgres"),
            password=creds.get("TRINITY_PG_PASSWORD", ""), connect_timeout=5)
        pg.set_session(readonly=True, autocommit=True)
        c = pg.cursor()

        def one(sql: str) -> int:
            c.execute(sql)
            return int(c.fetchone()[0])

        act = one("select count(*) from memories where status='active'")
        never = one("select count(*) from memories where status='active' and last_retrieved_at is null")
        cold30 = one("select count(*) from memories where status='active' and (last_retrieved_at is null "
                     "or last_retrieved_at < now() - interval '%d days')" % int(days))
        acc0 = one("select count(*) from memories where status='active' and coalesce(access_count,0)=0")
        lan = one("select count(*) from memories where status='active' and last_accessed_at is null")
        total = one("select count(*) from memories")
        nested = one("select count(*) from memories where status='active' and last_retrieved_at is null "
                     "and (last_retrieved_at is null or last_retrieved_at < now() - interval '%d days')"
                     % int(days))
        pg.close()
        return {
            "available": True,
            "total_rows": total,
            "active_rows": act,
            "never_retrieved": {"rows": never, "rate": round(never / act, 6) if act else 0.0},
            "not_retrieved_within_window": {"rows": cold30, "window_days": int(days),
                                           "rate": round(cold30 / act, 6) if act else 0.0},
            "coverage_ever": round(1.0 - never / act, 6) if act else 0.0,
            "coverage_window": round(1.0 - cold30 / act, 6) if act else 0.0,
            "access_count_0": {"rows": acc0, "rate": round(acc0 / act, 6) if act else 0.0},
            "last_accessed_at_is_null": {"rows": lan, "rate": round(lan / act, 6) if act else 0.0},
            "nesting_check_never_subset_of_window": {"never_rows_inside_window_cold": nested,
                                                    "ok": nested == never},
            "caliber": ("PG 才有 `last_retrieved_at`（SQLite 无此列）⇒ 「从未被检索」的唯一权威信号。"
                        "分母 = PG active。快照 AGENTS.md 的冷池就是这个口径"
                        "（`scripts/update_dsh_agents_md.py:204-206`）。"),
            "reproduce": [
                "SELECT count(*) FROM memories WHERE status='active';",
                "SELECT count(*) FROM memories WHERE status='active' AND last_retrieved_at IS NULL;",
                "SELECT count(*) FROM memories WHERE status='active' AND (last_retrieved_at IS NULL "
                "OR last_retrieved_at < now() - interval '%d days');" % int(days),
            ],
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160]),
                "caliber": "PG 不可达 ⇒ 本段不可用。**不猜数**：SQLite 侧无 last_retrieved_at 列，"
                           "任何 SQLite 数字都替代不了它。"}


def metric_delivery_face(days: int = 1) -> Dict[str, Any]:
    """**投递面**重复率（与「库内重复率」是两个不同的量，禁止混用）。

    算法逐行对齐 `trinity/bridges/delivery_ledger.py::delivery_stats()`（同一文件格式、
    同一窗口语义、同一 repeat_rate 定义），但**在本脚本内重新实现**：避免为了读一个
    JSONL 而 import `trinity` 包（那会连带跑 second_brain 初始化链、产生旁路副作用）。
    """
    p = os.environ.get("TRINITY_DELIVERY_LEDGER") or os.path.join(
        os.path.expanduser("~"), ".trinity", "state", "opening_surface_deliveries.jsonl")
    out: Dict[str, Any] = {"path": p, "window_days": int(days),
                           "caliber": ("分母 = 账本里窗口内**投递条目数**（∑ ids，含重复）；"
                                       "重复率 = (deliveries - distinct) / deliveries。"
                                       "这是**投递面**重复（同一条记忆被反复投进上下文），"
                                       "与库内 sha256 重复率**不是同一个量**。")}
    if not os.path.exists(p):
        out["available"] = False
        out["note"] = "账本不存在"
        return out
    cnt = Counter()
    records = 0
    cut = time.time() - int(days) * 86400
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts = float(d.get("ts") or 0)
            except Exception:
                continue
            if ts < cut:
                continue
            records += 1
            for mid in (d.get("ids") or []):
                cnt[str(mid)] += 1
    out.update({
        "available": True,
        "records": records,
        "deliveries": sum(cnt.values()),
        "distinct": len(cnt),
        "repeat_rate": (round((sum(cnt.values()) - len(cnt)) / sum(cnt.values()), 4)
                        if cnt else None),
        "max_repeat": max(cnt.values()) if cnt else 0,
        "top_repeated": [{"memory_id": k, "times": v} for k, v in cnt.most_common(5)],
    })
    return out


def metric_throughput(con, window_days: int = 30) -> Dict[str, Any]:
    q = con.execute
    today = q("select count(*) from memories where created_at >= date('now')").fetchone()[0]
    w = q("select count(*) from memories where created_at >= datetime('now','-%d days')" % int(window_days)).fetchone()[0]
    cold_w = q("select count(*) from memories where status='active' "
               "and created_at >= datetime('now','-%d days') "
               "and coalesce(access_count,0)=0" % int(window_days)).fetchone()[0]
    reads_24h = None
    try:
        reads_24h = q("select count(distinct je.value) from audit_log, "
                      "json_each(audit_log.details,'$.memory_ids') je "
                      "where audit_log.action in ('search','search_hybrid') "
                      "and audit_log.timestamp > datetime('now','-24 hours')").fetchone()[0]
    except Exception as e:
        reads_24h = "unavailable: %s" % type(e).__name__
    r7 = None
    try:
        r7 = q("select count(distinct je.value) from audit_log, "
               "json_each(audit_log.details,'$.memory_ids') je "
               "where audit_log.action in ('search','search_hybrid') "
               "and audit_log.timestamp > datetime('now','-168 hours')").fetchone()[0]
    except Exception as e:
        r7 = "unavailable: %s" % type(e).__name__
    return {
        "writes_today": today,
        "writes_window": w,
        "window_days": int(window_days),
        "writes_window_that_are_cold": cold_w,
        "cold_share_of_new_writes": round(cold_w / w, 6) if w else 0.0,
        "cross_check_cold_share_matches_cold_pool": cold_w,
        "distinct_read_ids_24h": reads_24h,
        "distinct_read_ids_7d": r7,
        "write_read_ratio_window": (round(w / r7, 4) if isinstance(r7, int) and r7 else None),
        "caliber": ("写入 = memories.created_at 在窗口内的新行；读取 = audit_log(action in "
                    "search/search_hybrid) 的 details.$.memory_ids 去重 id 数。"
                    "⚠️ 单次检索至多记 10 个 id ⇒ 读取是**下界**（截断），不是精确值。"),
        "reproduce": [
            "SELECT count(*) FROM memories WHERE created_at >= date('now');",
            "SELECT count(DISTINCT je.value) FROM audit_log, json_each(audit_log.details,'$.memory_ids') je "
            "WHERE audit_log.action IN ('search','search_hybrid') "
            "AND audit_log.timestamp > datetime('now','-24 hours');",
        ],
    }


# ──────────────────────────────────────────────────────────── 报告

def run(db: str, near_sample: int, seed: int, window_days: int, pg: bool = True,
        delivery_days: int = 1) -> Dict[str, Any]:
    t0 = time.time()
    con = connect_ro(db)
    out: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "script": "scripts/corpus_quality_audit.py",
        "readonly": True,
        "db": db,
        "db_size_bytes": os.path.getsize(db) if os.path.exists(db) else None,
        "note": ("库在量测期间**仍在被写入**（active 每次查询都会小幅变化）⇒ 各比率末位 ±0.01% "
                 "属正常抖动；跨段相除得到的比率必须用**同一段**的分母。"),
    }
    out["exact_dup_all"] = metric_exact_dup(con, "全库（所有 status）")
    out["exact_dup_active"] = metric_exact_dup(con, "active", "status='active'")
    out["dup_mechanism"] = metric_dup_mechanism(con)
    out["near_dup"] = metric_near_dup(con, near_sample, seed)
    out["tags"] = metric_tags(con)
    out["self_generated"] = metric_self_generated(con, window_days)
    out["cold_pool"] = metric_cold_pool(con, window_days)
    out["throughput"] = metric_throughput(con, window_days)
    con.close()
    out["cold_pool_pg"] = metric_cold_pool_pg(window_days) if pg else {
        "available": False, "error": "skipped (--no-pg)"}
    out["delivery_face_1d"] = metric_delivery_face(1)
    out["delivery_face_30d"] = metric_delivery_face(30)
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def fmt_pct(x: float) -> str:
    return "%.2f%%" % (100.0 * x)


def render(r: Dict[str, Any]) -> str:
    L: List[str] = []
    p = L.append
    p("=" * 78)
    p("Trinity 语料卫生与利用率 —— 只读量测（db=%s）" % r["db"])
    p("ts=%s  elapsed=%ss" % (r["ts"], r.get("elapsed_s")))
    p("=" * 78)

    e = r["exact_dup_all"]
    p("\n[1] 精确重复率")
    p("  全库口径：%d 行 / 去重 %d → 冗余 %d = %s"
      % (e["rows"], e["distinct_sha256"], e["redundant_rows_sha256"], fmt_pct(e["exact_dup_rate_sha256"])))
    p("  同一分母用 raw content 量：%s   ← 密文逐行不同，这个口径是坑"
      % fmt_pct(e["exact_dup_rate_raw_content"]))
    a = r["exact_dup_active"]
    p("  active 口径：%d 行 → 冗余 %d = %s"
      % (a["rows"], a["redundant_rows_sha256"], fmt_pct(a["exact_dup_rate_sha256"])))
    m = r["dup_mechanism"]
    p("  机制：重复组 %d；「≥1 active + ≥1 非 active」%d 组；全归档组 %d（%d 行）"
      % (m["dup_groups"], m["groups_with_active_and_nonactive"], m["groups_with_zero_active"],
         m["rows_in_fully_archived_dup_groups"]))
    p("  active 内重复组 = %d（期望 0：唯一索引生效）" % m["active_duplicate_groups"])
    for w in m["worst_groups"]:
        p("    最重组 n=%d（active %d）%s→%s agent=%s" % (w["n"], w["active"], w["first"], w["last"], w["agents"]))

    n = r["near_dup"]
    p("\n[2] 近重复率")
    p("  可读文本 %d 行（占全库 %s）；抽样 %d 行（seed=%d）"
      % (n["readable_rows_total"], fmt_pct(n["readable_share_of_corpus"]), n["sampled_rows"], n["seed"]))
    p("  近重复簇 %d 个、涉及 %d 行 → 命中率 %s（冗余率 %s）"
      % (n["near_dup_clusters"], n["rows_in_near_dup_clusters"],
         fmt_pct(n["near_dup_rate"]), fmt_pct(n["near_dup_excess_rate"])))
    p("  历史可比口径（前 120 字符塌缩，全库）：%d 组 / %d 行"
      % (n["prefix120_collapse_groups"], n["prefix120_collapse_rows"]))
    for c in n["largest_clusters"][:3]:
        p("    最大簇 size=%d 例：%s" % (c["size"], ", ".join(c["members"][:4])))

    t = r["tags"]
    p("\n[3] 无标签率")
    for scope in ("all", "active"):
        s = t[scope]
        p("  %-6s：分母 %d；NULL %d + '[]' %d = %d → %s（只数 '[]' 会读成 %s）"
          % (scope, s["denominator"], s["tags_null"], s["tags_empty_array"],
             s["tagless_total"], fmt_pct(s["tagless_rate"]),
             fmt_pct(s["tags_empty_array"] / s["denominator"] if s["denominator"] else 0.0)))

    s = r["self_generated"]
    p("\n[4] 自产率（三信号分别报，不合成单值）")
    for scope in ("all", "active", "window"):
        d = s[scope]
        p("  分母=%-6s(%d 行): S1 内容生成头 %s | S2 机器生产者 %s | S3 metadata 派生 %s | S1∪S2 %s"
          % (scope, d["denominator_rows"], fmt_pct(d["S1_content_marker"]["rate"]),
             fmt_pct(d["S2_producer_namespace"]["rate"]), fmt_pct(d["S3_metadata_derived"]["rate"]),
             fmt_pct(d["S1_or_S2"]["rate"])))
    p("  生产者分类（全库）：%s" % s["producer_class_all"])
    p("  生产者分类（近 %d 天写入）：%s" % (s["window_days"], s["producer_class_window"]))

    c = r["cold_pool"]
    p("\n[5] 冷池（分母 = active %d 行）" % c["active_rows"])
    p("  A access_count=0        ：%d = %s   ← %s"
      % (c["caliber_A_access_count_0"]["rows"], fmt_pct(c["caliber_A_access_count_0"]["rate"]),
         c["caliber_A_access_count_0"]["verdict"]))
    p("  B last_accessed_at NULL ：%d = %s   ← %s"
      % (c["caliber_B_last_accessed_at_is_null"]["rows"], fmt_pct(c["caliber_B_last_accessed_at_is_null"]["rate"]),
         c["caliber_B_last_accessed_at_is_null"]["verdict"]))
    p("  C 交集                  ：%d = %s" % (c["caliber_C_intersection"]["rows"],
                                              fmt_pct(c["caliber_C_intersection"]["rate"])))
    d = c["caliber_D_access_count_0_and_not_touched_after_created"]
    p("  D acc0 且未被 touch 过  ：%d = %s   ← %s" % (d["rows"], fmt_pct(d["rate"]), d["verdict"]))
    p("  证伪证据：acc0 但 last_accessed_at 非空 %d 行，其中 %d 行的时间戳= 写入时刻"
      % (c["consistency"]["acc0_but_last_accessed_not_null"],
         c["consistency"]["of_which_stamped_at_write_time"]))
    g = r["cold_pool_pg"]
    if g.get("available"):
        p("  [PG 对账] active %d：从未被检索 %d = %s（coverage_ever %s）；"
          "近 %d 天未检索 %d = %s（coverage %s）"
          % (g["active_rows"], g["never_retrieved"]["rows"], fmt_pct(g["never_retrieved"]["rate"]),
             fmt_pct(g["coverage_ever"]), g["not_retrieved_within_window"]["window_days"],
             g["not_retrieved_within_window"]["rows"],
             fmt_pct(g["not_retrieved_within_window"]["rate"]), fmt_pct(g["coverage_window"])))
        p("  [PG 对账] 嵌套自洽（never ⊆ 窗口冷）：%s；PG access_count=0 = %s"
          % (g["nesting_check_never_subset_of_window"]["ok"],
             fmt_pct(g["access_count_0"]["rate"])))
    else:
        p("  [PG 对账] 不可用：%s" % g.get("error"))
    p("  近 %d 天新建且冷：%d 行（占冷池 %s）"
      % (c["cold_new_within_window"]["window_days"], c["cold_new_within_window"]["rows"],
         fmt_pct(c["cold_new_within_window"]["share_of_cold"])))
    for row in c["cold_by_producer"][:6]:
        p("    %-32s active=%-6d cold=%-6d %s" % (row["producer"], row["active"], row["cold"],
                                                  fmt_pct(row["cold_rate"])))

    th = r["throughput"]
    p("\n[6] 利用率")
    p("  今日写入 %d；近 %d 天写入 %d（其中冷 %d = %s）"
      % (th["writes_today"], th["window_days"], th["writes_window"],
         th["writes_window_that_are_cold"], fmt_pct(th["cold_share_of_new_writes"])))
    p("  去重读取 id：24h %s；7d %s；写读比(窗口) %s"
      % (th["distinct_read_ids_24h"], th["distinct_read_ids_7d"], th["write_read_ratio_window"]))

    p("\n[7] 投递面重复率（★ 与库内重复率**不是同一个量**，禁止混用）")
    for key, label in (("delivery_face_1d", "24h"), ("delivery_face_30d", "30d")):
        df = r[key]
        if df.get("available"):
            p("  %-4s：投递 %d 条 / 去重 %d → 重复率 %s（最大重复 %d 次）"
              % (label, df["deliveries"], df["distinct"], df["repeat_rate"], df["max_repeat"]))
        else:
            p("  %-4s：不可用（%s）" % (label, df.get("note") or df.get("error")))
    p("  口径：分母 = 窗口内 ∑ ids（含重复）；重复率 = (deliveries-distinct)/deliveries。")
    p("")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Trinity 语料卫生与利用率只读量测")
    ap.add_argument("--db", default=os.environ.get("TRINITY_STORE", DEFAULT_DB),
                    help="SQLite store 文件，或 store 目录（自动补 trinity_store.db）")
    ap.add_argument("--near-sample", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--json", default="")
    ap.add_argument("--no-pg", action="store_true", help="跳过 PG 对账段")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    args.db = resolve_db(args.db)

    if not os.path.exists(args.db):
        print("DB missing: %s" % args.db, file=sys.stderr)
        return 2
    try:
        rep = run(args.db, args.near_sample, args.seed, args.window_days, pg=not args.no_pg)
    except Exception as e:  # noqa: BLE001
        print("audit failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    if not args.quiet:
        print(render(rep))
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
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/corpus_quality_audit.py::<module>")
    raise SystemExit(main())

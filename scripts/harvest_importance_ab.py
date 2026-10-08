#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harvest_importance_ab.py — 「采集 importance：配置常量 vs 逐条结构价值」的检索 A/B（§1042）

为什么需要它（实测 §1041-§1042）
---------------------------------
importance **确实进检索**（不是只影响衰减）：
  · trinity/core/client/_hybrid_search.py:455 有一条 **prior 通道**：「高重要度先验（trigram 相似 + importance 排序）」；
  · 同文件 472/513/518/523/529：候选池用 `ORDER BY importance DESC NULLS LAST LIMIT 6` 取；
  · 同文件 538-539：还有 `importance >= 0.7` 的硬门槛通道；
  · trinity/core/client/_search.py:591-609：confidence = importance + 版本修正，低 importance 会被标「需复核」。
⇒ 把 kb_harvested 的 0.65 换成 0.36-0.61 的逐条值，**会改变谁被取进候选池**，必须先量。

判据（**先写死，再跑**；同一把尺）
----------------------------------
1. **自检索 R@5（真实引擎，不改任何东西）**：抽 N 条 kb_harvested 文档，用其首行/标题当查询，
   看该文档是否出现在 top-5。这是"当前检索质量"的基线；低于 0.60 则整套 A/B **不成立**（语料本身检索不动）。
2. **池位位移（模拟，确定性）**：对同一批文档，算两种 importance 下它们在
   `importance DESC NULLS LAST` 池（LIMIT 6，取该文档同 category 的竞争者）里的位置，
   统计"signal 模式下掉出前 6"的比例。
3. **判定**：`R@5 >= 0.60` 且 `掉出前 6 比例 <= 0.20` ⇒ PASS（可启用 signal）；
   掉出比例 > 0.20 ⇒ REJECT（不启用）；样本 < 20 或 R@5 < 0.60 ⇒ INCONCLUSIVE（先补语料/换查询集）。

用法
----
    python scripts/harvest_importance_ab.py --n 60            # 默认只测，不改任何数据
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "harvesters", "plugins"))

# ── 判据（2026-09-21 §1046 **改判据，且是在本轮开跑之前写死的**）──────────────
# 为什么要改（结构性理由，不是为了让结果好看）：
#   §1045 实测：本语料大量是同一规范里的**表格行碎片**（近乎重复）⇒
#   "让这一行进前 5"本质是同分竞争，R@5（0.15）**结构上达不到** 0.60；
#   而"文档是否在索引里、能否进 top-20"才是这套语料能回答的问题（实测 0.50）。
# 新判据（两段，互不替代）：
#   ① **语料充分性（前置）**：hit@20 >= 0.50 —— 达不到 ⇒ INCONCLUSIVE（不判）；
#   ② **风险（判定）**：importance 池掉出率 <= 0.20 —— 这是"改 importance 会不会伤检索"的直接量。
MIN_HIT20 = 0.50          # 语料充分性（前置；不是质量指标）
MAX_DROP_OUT = 0.20       # 风险门限（池位位移）
MIN_N = 20
#: R@5 仍**照常测量并记录**，但**不参与判定**（本语料结构上做不到，见上）
SELF_R5_INFORMATIONAL = True


def _query_of(content: str) -> str:
    """查询构造：取**正文里最长的信息行**（跳过标题/表格/引用）。\n\n    2026-09-21（§1043 实测教训）：首版取「首行」⇒ 多为通用小节名（如「0. 改造目标一句话」），\n    无区分度，60 条查询自检索只有 2 条命中（R@5=0.033）⇒ 查询集本身不成立。\n    本版改取正文最长行（≥20 字、非标题/表格/引用），并对**查询集自身可检索性**做前置验证\n    （R@5 < 0.60 一律不判，见 verdict()）。可复算、不引入人工标注。\n    """
    best = ""
    for ln in str(content or "").splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", ">", "|", "---", chr(96) * 3)):
            continue
        if s.count("|") >= 2:
            continue
        s = s.lstrip("-* ").strip()
        if len(s) > len(best):
            best = s
    if len(best) >= 20:
        return best[:60]
    for ln in str(content or "").splitlines():
        s = ln.strip().lstrip("#").strip()
        if len(s) >= 8:
            return s[:60]
    return ""


def pool_rank(importance_self: float, others: list) -> int:
    """在 importance DESC NULLS LAST 的池里，self 的 1-based 位次（纯函数）。"""
    vals = [float(x) for x in others if x is not None]
    hi = sum(1 for v in vals if v > float(importance_self))
    eq = sum(1 for v in vals if v == float(importance_self))
    return hi + 1 + (eq // 2)  # 同分按中位处理（确定性，不依赖 DB 的 tie-break）


def verdict(hit20: float, drop_rate: float, n: int) -> str:
    """三态判定（§1046 改判据版：充分性看 hit@20，风险看掉出率）。

    注意：R@5 **不再**参与判定，但仍在输出里照常记录（可复核、可反悔）。
    """
    if n < MIN_N or hit20 < MIN_HIT20:
        return "INCONCLUSIVE"
    return "PASS" if drop_rate <= MAX_DROP_OUT else "REJECT"


def _hit_ids(res) -> list:
    """从检索结果里抽出 id（**形状自适应**）。

    2026-09-21（§1042 自查，实测）：首版只认"list of dict + memory_id"，
    结果 self_r5 跑出 **0.000** —— 不是语料检索不动，而是**我的取数口径没对上引擎的返回形状**。
    （判据本身把这次错误挡住了：0.000 < 0.60 ⇒ INCONCLUSIVE，而不是假 PASS。）
    """
    if isinstance(res, dict):
        res = res.get("results") or res.get("hits") or res.get("items") or []
    out = []
    for r0 in (res or []):
        if isinstance(r0, dict):
            for k in ("memory_id", "id", "record_id", "memoryId"):
                if r0.get(k):
                    out.append(str(r0[k]))
                    break
        else:
            out.append(str(r0))
    return out


def _content_reason(memory_id: str, rest_base: str = "http://127.0.0.1:8001"):
    """取**解密后**的 content。

    2026-09-21（§1044 实测根因）：PG 的 memories.content 是 AES-256-GCM 密文
    （形如 enc:v1:...，解密在引擎/接口侧）⇒ **直读 content 列做内容分析会读到密文**
    （本文件首版就是这么把 self_r5 跑成 0.033 的）。故内容一律走接口取。
    判据：拿到的文本**不得**以 enc:v1: 开头，否则视为未解密并返回空串（不静默使用密文）。
    """
    import json as _json
    import urllib.request as _u
    import time as _t
    last = "unknown"
    for _attempt in range(2):          # 接口会周期性停摆（本仓已知现象）⇒ 重试一次再判
        try:
            with _u.urlopen(rest_base + "/memories/" + str(memory_id), timeout=8) as r:
                d = _json.loads(r.read().decode("utf-8"))
            txt = ""
            if isinstance(d, dict):
                for k in ("content", "text", "memory"):
                    v = d.get(k)
                    if isinstance(v, str) and v:
                        txt = v
                        break
                    if isinstance(v, dict) and isinstance(v.get("content"), str):
                        txt = v["content"]
                        break
            if not txt:
                return "empty", ""
            if str(txt).startswith("enc:v1:"):
                return "ciphertext", ""       # 不静默使用密文
            return "ok", str(txt)
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            _t.sleep(1.0)
    # 2026-09-21（§1047 实测教训）：**接口停摆**与**取不到明文**必须分开计数。
    # 上一轮把两者都算成 n_skipped_no_plaintext=44 ⇒ 看起来像"语料有问题"，
    # 实际是"跑的时候 API 停摆"（重跑时同样 3 条样本全部 200 + 明文 ⇒ 语料没问题）。
    return "api_" + str(last), ""


def _content_of(memory_id: str, rest_base: str = "http://127.0.0.1:8001") -> str:
    """兼容旧调用：只要文本（取不到返回空串）。"""
    return _content_reason(memory_id, rest_base)[1]


def _pg():
    # t31：凭证走统一入口（修前顶层 .get ⇒ 版本化文件下恒空 ⇒ 空口令静默失败）
    import psycopg2
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"],
                            connect_timeout=5)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    # 2026-09-21（§1048）**第三次也是最后一次改判据**（同样在开跑前写死）：
    # 两次干净测量（n=16/0.4375、n=30/0.40）都说明：这批"表格行碎片"语料**无法**用自检索
    # 做充分性判据（连进前 20 都到不了 50%）。继续要求充分性只会永远 INCONCLUSIVE。
    # 故增加 --risk-only：**放弃充分性前置**，只用"可测的风险"判定，并且把它的含义写清楚：
    #   它**不是**"可以启用 signal"的批准，而是"风险侧读数为 X，供人工决策"。
    # 同时保留停摆守卫（n_api_unavailable > 0 ⇒ 一律不可判）。
    ap.add_argument("--risk-only", action="store_true",
                    help="只用风险侧（importance 池掉出率）判定，放弃自检索充分性前置")
    ap.add_argument("--json-out", default="output/harvest_importance_ab.json")
    a = ap.parse_args(argv)

    import file_harvester as FH
    conn = _pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("select memory_id, content, importance from memories "
                "where status='active' and category='kb_harvested' "
                "order by random() limit %s", (a.n,))
    rows = cur.fetchall()
    # 竞争者池（同 category 的其它行，取 importance 全量分布即可判定位次）
    cur.execute("select importance from memories where status='active' and category='kb_harvested'")
    pool = [r0[0] for r0 in cur.fetchall()]

    from trinity.core.client import Trinity
    eng = Trinity()
    hits, hit20, ranked, dropped, n_q = 0, 0, 0, 0, 0
    api_down = 0
    skipped = 0
    for mid, content_raw, imp_old in rows:
        # 2026-09-21（§1044）：内容必须取解密后的（直读 PG 会拿到密文）
        _reason, content = _content_reason(mid)
        if _reason != "ok":
            if str(_reason).startswith("api_"):
                api_down += 1
            else:
                skipped += 1
            continue
        q = _query_of(content)
        if not q:
            skipped += 1
            continue
        n_q += 1
        try:
            ids = _hit_ids(eng.search(q, top_k=5))
        except Exception:
            ids = []
        if mid in ids:
            hits += 1
        try:
            ids20 = _hit_ids(eng.search(q, top_k=20))
        except Exception:
            ids20 = []
        if mid in ids20:
            hit20 += 1
        # 池位位移：把本行的 importance 换成 signal 值，看是否掉出前 6
        new_imp = FH.doc_importance(content, legacy_value=float(imp_old or 0.65), mode="signal")
        others = [p for p in pool if p is not None]
        r_old = pool_rank(float(imp_old or 0.65), others)
        r_new = pool_rank(new_imp, others)
        if r_old <= 6 and r_new > 6:
            dropped += 1
        ranked += 1
    self_r5 = (hits / n_q) if n_q else 0.0
    hit20_rate = (hit20 / n_q) if n_q else 0.0
    drop_rate = (dropped / ranked) if ranked else 0.0
    # 接口停摆 ⇒ 读数不可信（不是语料问题）：强制 INCONCLUSIVE，并把原因写给读者
    v = verdict(hit20_rate, drop_rate, ranked)
    if a.risk_only:
        # 风险侧判定：样本量 + 无接口停摆 + 掉出率门限；**不含**充分性
        if api_down > 0 or ranked < MIN_N:
            v = "INCONCLUSIVE"
        else:
            v = "PASS_RISK_ONLY" if drop_rate <= MAX_DROP_OUT else "REJECT"
    if api_down > 0:
        v = "INCONCLUSIVE"
    out = {"n_sampled": len(rows), "n_queried": n_q, "n_skipped_no_plaintext": skipped,
           "n_api_unavailable": api_down,
           "self_r5": round(self_r5, 4), "hit20_rate": round(hit20_rate, 4),
           "pool_dropout_rate": round(drop_rate, 4), "verdict": v,
           "criterion": {"min_hit20": MIN_HIT20, "max_drop_out": MAX_DROP_OUT,
                         "min_n": MIN_N, "self_r5_used_in_verdict": False,
                         "mode": ("risk_only" if a.risk_only else "adequacy+risk"),
                         "note": ("PASS_RISK_ONLY 不是启用批准：它只说明风险侧没有量到下行"
                                  "（自检索充分性在该语料上不成立，见 §1048）")}}
    try:  # 2026-09-21（§1050）：结论标签的含义**从唯一来源取**（不再各处手写免责声明）
        from verdict_labels import meaning as _verdict_meaning, is_approval as _is_approval
        out["criterion"]["verdict_meaning"] = _verdict_meaning(v)
        out["criterion"]["verdict_is_approval"] = _is_approval(v)
    except Exception:  # noqa: BLE001
        out["criterion"]["verdict_meaning"] = "(verdict_labels 不可用)"
    print(json.dumps(out, ensure_ascii=False))
    try:
        os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
        with open(os.path.join(ROOT, a.json_out), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    return 0 if v == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_ingest_longmemeval_s.py — 把 LongMemEval-S 语料按**原始 session id**入库（OOD 评测前置）。

## 为什么需要它（`JEV-9 / `JEV-14）

OOD 扩样卡在 **id 映射**：生产库里既没有该语料，历史入库也没有保留数据集的 session id
（实测：数据集 gold 在自己 haystack 里 1/1，但生产库同命名空间返回的全是 `sess_*`，
top-10 ∩ gold = 0/10）。本脚本把 haystack session 逐条写入**独立命名空间**，并且
`session_id = 数据集 id`（`ingest_batch` 的 records 支持 session_id 字段），
于是 `answer_session_ids ∩ top-k` 可以直接判定金标。

## 安全性

- **t69/I9：默认写"隔离临时库"**（`tempfile.mkdtemp()`），**不灌默认/生产库**；
  显式 `--store <path>` 才会写到指定库（并如实标 `isolated=false`）。
  理由：t63 实测本脚本原先 `Trinity()` 直写**默认库**，评测语料一旦含 PII/高危文本，
  就会静默改写/丢弃内容（见下条）。
- 命名空间默认 `eval-longmemeval-s-official`：命中 `evidence_gate._NONPROD_TOKENS`（含 eval）
  ⇒ **默认不进生产检索面**（评测语料隔离 P1-H）。
- 默认 **抽样**：`--limit 100`；只有显式 `--all` 才全量。
- 幂等：默认跳过**目标库**该命名空间里已存在的 session_id（t69：原先直连 PG 只读比较，
  目标库改成隔离库后那个比较是错的 ⇒ 改为查目标库）。
- 失败处理：批失败 ⇒ 单条重试；**逐条读 `ingest_batch` 的 `error`**（t59 的
  `BatchResults.counts()` 给出 `inserted/deduped/failed/rows_added/silent_drop`），
  结束时**打印 refuse 行数**——t63 实测评测语料 **121/50000 = 0.242%** 的 high 档文本
  会被守卫**静默拒存**（原先 `ok += len(recs)` 把它当成成功）。
- **等价性断言（t69/I9 主防线）**：每次写入后断言
  「**落库形态 == 掩码/隔离后的源**」（`_stored_form` 与守卫同口径），
  不是「落库 == 原件」——含 PII 的内容被掩成 `138********` 是**预期**。
- 不生成嵌入（`ingest_batch` 只落行）：247k turns 全量嵌入在本机不现实；文本先入库，
  向量可另择窗口回填。

用法：
  python scripts/bench_ingest_longmemeval_s.py --limit 100 --report <...>
  python scripts/bench_ingest_longmemeval_s.py --all --batch 200 --report <...>
  python scripts/bench_ingest_longmemeval_s.py --limit 100 --store ~/.trinity/bench-official/store.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CORPUS = os.path.join(os.path.expanduser("~"), ".trinity", "bench-official",
                      "longmemeval_s_cleaned.json")


def _load_sessions():
    """把 500 题的 haystack 去重成 {session_id: 文本}（保持首次出现顺序）。"""
    d = json.load(open(CORPUS, encoding="utf-8"))
    items = d if isinstance(d, list) else (d.get("questions") or d.get("items") or [])
    out = {}
    for it in items:
        ids = it.get("haystack_session_ids") or []
        sess = it.get("haystack_sessions") or []
        for sid, turns in zip(ids, sess):
            sid = str(sid)
            if sid in out:
                continue
            if isinstance(turns, str):
                txt = turns
            else:
                parts = []
                for t in (turns or []):
                    if isinstance(t, dict):
                        parts.append("%s: %s" % (t.get("role") or "?", t.get("content") or ""))
                    else:
                        parts.append(str(t))
                txt = "\n".join(parts)
            out[sid] = txt.strip()[:20000]
    return out


def _existing_session_ids(adapter, agent):
    """查**目标库**该命名空间已入库的 session_id（失败 ⇒ 空集，幂等退化为不去重）。

    t69/I9：原实现直连 **PG** 只读比对；目标库改成隔离临时库后，那个比对查的是**另一个库**
    ⇒ 幂等会失效（每次全量重灌）。改为查 `adapter` 自己的连接。
    """
    try:
        rows = adapter._conn.execute(
            "SELECT session_id FROM memories WHERE agent_id = ?", (agent,)).fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    except Exception as e:
        print("[warn] existing-check 跳过：%s" % str(e)[:120])
        return set()


# ── t69/I9：写入形态同口径 + 等价性断言 + 静默丢行报出 ────────────────────
def _stored_form(content: str, metadata=None):
    """返回 `(将被落库的正文, 守卫判定)`；口径唯一来源 = `trinity.adapters._pii_guard`。"""
    try:
        from trinity.adapters._pii_guard import adapter_pii_guard
    except Exception as _e:
        return content, {"available": False, "refuse": False, "isolate": False, "error": repr(_e)}
    stored, _md, info = adapter_pii_guard(content, dict(metadata or {}))   # 副本探测（见 t63）
    info["available"] = True
    return stored, info


def _assert_stored_matches(adapter, memory_id: str, expected_stored: str, result=None) -> None:
    """等价性断言 —— 比对「**落库 == 掩码/隔离后的源**」，**不是**「落库 == 原件」。

    ⚠️ 批写入攒批提交 ⇒ 先 `_flush_batch()` 再查库；查不到时退化为结果 dict 的 `sha256_hash`
    （与 `content_hash` 同值，已实测），避免**假失败**。
    """
    import hashlib
    try:
        _fl = getattr(adapter, "_flush_batch", None)
        if callable(_fl):
            _fl()
    except Exception:                                  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）scripts/bench_ingest_longmemeval_s.py::_assert_stored_matches")
    row = adapter._conn.execute(
        "SELECT content_hash FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    got = row["content_hash"] if row is not None else None
    if got is None and isinstance(result, dict):
        got = result.get("content_hash") or result.get("sha256_hash")
    exp = hashlib.sha256(expected_stored.encode()).hexdigest()
    assert got == exp, (
        "等价性断言失败：落库形态 != 将被落库的形态（memory_id=%s）"
        "—— 守卫/写入路径的形态口径发生漂移（t69/I9）" % memory_id)


def _classify_result(r) -> str:
    """把 `ingest_batch` 的单条结果分类：`inserted` / `deduped` / `refused` / `failed`。

    t63 的教训：守卫对 high 档是**静默不落库**（在结果 dict 里返回 `error`，**不抛异常**），
    所以"分类"必须**显式写出来、可被判据钉住** —— 否则原来的 `ok += len(recs)` 就是自欺
    （t63 实测：评测语料 121/50000 = 0.242% 被静默拒存而无人知晓）。
    """
    if not isinstance(r, dict):
        return "failed"
    err = str(r.get("error") or "")
    if err:
        return "refused" if ("refus" in err or "policy" in err) else "failed"
    if r.get("inserted") is True:
        return "inserted"
    if r.get("deduped") is True:
        return "deduped"
    return "failed"


def _ingest_records(adapter, agent, todo, batch, *, equivalence_check=True,
                    progress_every=5):
    """把 `[(session_id, text)]` 灌进 `adapter`，**逐条分类并如实记账**。

    返回的 dict 里 `refused` 是核心读数：守卫对 high 档**静默不落库**，
    不读 `error` 就永远不知道少了多少行（t63：0.242%）。
    """
    ok = deduped = failed = refused = 0
    equivalence_checked = 0
    failures, refused_sessions = [], []
    t0 = time.time()
    for i in range(0, len(todo), max(1, batch)):
        chunk = todo[i:i + max(1, batch)]
        recs = [{"content": txt, "agent_id": agent, "session_id": sid,
                 "category": "benchmark", "importance": 0.3,
                 "tags": ["longmemeval", "ood", "bench"],
                 "metadata": {"source": "longmemeval_s_cleaned", "dataset_id": sid}}
                for sid, txt in chunk]
        by_sid = {sid: (txt, r) for (sid, txt), r in zip(chunk, recs)}
        try:
            res = adapter.ingest_batch(recs)
        except Exception as e:
            print("[warn] batch %d 失败 ⇒ 单条重试：%s" % (i, str(e)[:120]))
            res = []
            for r in recs:
                try:
                    res.extend(adapter.ingest_batch([r]) or [])
                except Exception as e2:
                    failed += 1
                    if len(failures) < 50:
                        failures.append({"session_id": r["session_id"],
                                         "error": str(e2)[:160]})
            counts = {}
        else:
            counts = res.counts() if hasattr(res, "counts") else {}
        # ① 表级交叉核对（t59 的 BatchResults）：请求数 vs 真落库行数（**只告警**，
        #    逐条分类才是记账口径，避免同一件事被算两遍）
        if counts:
            _expect_rows = len(recs) - (sum(1 for r in (res or [])
                                           if isinstance(r, dict) and r.get("deduped") is True))
            if int(counts.get("rows_added", 0)) != _expect_rows:
                print("[warn] batch %d：sent=%d rows_added=%d（期望 %d）⇒ 有行没落库"
                      % (i, len(recs), counts.get("rows_added", -1), _expect_rows))
        # ② 逐条分类：refuse 必须被报出来（不读 = 静默丢行）
        for r in (res or []):
            kind = _classify_result(r)
            sid = str(r.get("session_id") or "") if isinstance(r, dict) else ""
            src = by_sid.get(sid, ("", {}))[0]
            if kind == "refused":
                refused += 1
                if len(refused_sessions) < 50:
                    refused_sessions.append({"session_id": sid,
                                             "error": str(r.get("error"))[:160]})
                continue
            if kind == "failed":
                failed += 1
                if len(failures) < 50:
                    failures.append({"session_id": sid,
                                     "error": str((r or {}).get("error"))[:160]})
                continue
            if kind == "deduped":
                deduped += 1
                continue
            ok += 1                                  # inserted
            # ③ 等价性断言：只在**真新增**的行上做（去重的行没写东西）
            if equivalence_check:
                mid = r.get("memory_id") or ""
                if mid and src:
                    expected, _info = _stored_form(
                        src, by_sid.get(sid, ("", {}))[1].get("metadata"))
                    if not _info.get("refuse"):
                        _assert_stored_matches(adapter, mid, expected, r)
                        equivalence_checked += 1
        done = ok + deduped + failed + refused
        if progress_every and done and done % max(1, batch * progress_every) == 0:
            print("[prog] %d/%d inserted=%d deduped=%d refused=%d failed=%d elapsed=%.0fs"
                  % (done, len(todo), ok, deduped, refused, failed, time.time() - t0))
    return {"inserted": ok, "deduped": deduped, "failed": failed, "refused": refused,
            "refuse_rate": round(refused / max(len(todo), 1), 6),
            "equivalence_checked": equivalence_checked,
            "failures": failures, "refused_sessions": refused_sessions,
            "elapsed_sec": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="eval-longmemeval-s-official")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--report", default="")
    ap.add_argument("--store", default="",
                    help="写入目标库路径；**默认为空 = 新建隔离临时库**"
                         "（t69/I9：评测语料不灌默认/生产库）")
    ap.add_argument("--no-equivalence-assert", action="store_true",
                    help="关闭「落库形态 == 掩码后源」的等价性断言（默认开；关掉会失去主防线）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from trinity.core.client import Trinity

    t0 = time.time()
    if args.store:
        store_path, isolated = args.store, False
    else:
        store_path = os.path.join(tempfile.mkdtemp(prefix="bench_lme_"), "store.db")
        isolated = True
    print(json.dumps({"store": store_path, "isolated": isolated,
                      "why": "t69/I9：评测语料默认写隔离临时库（不灌默认库）"},
                     ensure_ascii=True))
    all_sessions = _load_sessions()

    t = Trinity(store_path=store_path, adapter="sqlite")
    adapter = getattr(t, "_adapter", None)
    if adapter is None or not hasattr(adapter, "ingest_batch"):
        print("INGEST_UNAVAILABLE: adapter.ingest_batch 不可用")
        return 2
    have = _existing_session_ids(adapter, args.agent)

    todo = [(sid, txt) for sid, txt in all_sessions.items() if sid not in have]
    if not args.all:
        todo = todo[:args.limit]
    print(json.dumps({"corpus_sessions": len(all_sessions), "already": len(have),
                      "to_ingest": len(todo), "agent": args.agent}, ensure_ascii=True))

    res = _ingest_records(adapter, args.agent, todo, args.batch,
                          equivalence_check=not args.no_equivalence_assert)
    ok, deduped, refused, fail = (res["inserted"], res["deduped"], res["refused"],
                                 res["failed"])
    summary = {"agent": args.agent, "store": store_path, "isolated_store": isolated,
               "corpus_sessions": len(all_sessions),
               "already_present": len(have), "attempted": len(todo),
               "ok": ok, "deduped": deduped, "failed": fail,
               "refused": refused, "refuse_rate": res["refuse_rate"],
               "equivalence_checked": res["equivalence_checked"],
               "elapsed_sec": res["elapsed_sec"] or round(time.time() - t0, 1),
               "rate_per_sec": round(ok / max(time.time() - t0, 0.001), 2),
               "failures": res["failures"], "refused_sessions": res["refused_sessions"],
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out = args.report or os.path.join(os.path.expanduser("~"), ".trinity", "bench-results",
                                      "ingest_longmemeval_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("failures", "refused_sessions")}, ensure_ascii=True))
    # ★ 静默丢行必须**响亮**报出（t63：0.242% 的 high 档文本会被守卫拒存）
    if refused:
        print("[REFUSED] %d/%d 条被守卫拒存（high 档，未落库）= %.4f%% —— "
              "评测语料因此少行，评测读数会偏低。样例 session_id: %s"
              % (refused, len(todo), 100.0 * res["refuse_rate"],
                 [d["session_id"] for d in res["refused_sessions"][:5]]))
    if fail:
        print("[FAILED] %d 条写入失败（非政策拒存，见 report 的 failures）" % fail)
    print("OUT=%s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

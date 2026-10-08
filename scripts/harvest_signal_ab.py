"""S1 离线重排（**只读**，不改任何状态）：D1 的「排序会不会变」证据。

做什么：取真实检索词 → 引擎 hybrid 检索拿候选（含融合分）→ 用两种 importance 做**重排代理**，
比较 top-5 重叠与排名变化。

**必须声明**：本脚本量的是「**排序**在两种 importance 下会不会变」，**不是**端到端检索效果；
重排用的是**代理公式** score × (0.5 + importance)（引擎内部真实权重不可见）。

纪律：①先跑**负例对照**（两种 importance 取同值 ⇒ 重叠必须 100%）；②内容走 API（§13.1）；
③失败按原因分开计数（§13.2）；④样本不足判 INCONCLUSIVE。
"""
from __future__ import annotations

import argparse, json, os, sys, urllib.request
sys.path.insert(0, r"C:/Users/Administrator/trinity")
sys.path.insert(0, r"C:/Users/Administrator/trinity/harvesters")
import psycopg2, yaml
from plugins.file_harvester import doc_importance

API = "http://127.0.0.1:8001"


def _conn():
    # t31：凭证走统一入口（修前顶层 .get ⇒ 版本化文件下恒空 ⇒ 空口令静默失败）
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"])


def _post(path, payload, timeout=20):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get(path, timeout=8):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _candidates(q, top_k):
    d = _post("/memory/search/hybrid", {"query": q, "top_k": top_k})
    out = []
    for it in (d.get("results") or d.get("memories") or []):
        mid = it.get("memory_id") or it.get("id")
        sc = it.get("score") or it.get("hybrid_score") or it.get("rrf_score") or 0.0
        if mid:
            out.append((mid, float(sc)))
    return out


def run(queries, top_k, same_value):
    cn = _conn(); cn.autocommit = True; cur = cn.cursor()
    fails = {"api_unavailable": 0, "shape_mismatch": 0, "empty": 0, "ciphertext": 0}
    overlaps, moved, nq = [], 0, 0
    for q in queries:
        try:
            cands = _candidates(q, top_k)
        except Exception:
            fails["api_unavailable"] += 1
            continue
        if not cands:
            fails["shape_mismatch"] += 1
            continue
        ids = [c[0] for c in cands]
        cur.execute("select memory_id, importance from memories where memory_id = any(%s)", (ids,))
        imp = {r[0]: float(r[1] or 0.5) for r in cur.fetchall()}
        row_old, row_new = [], []
        for mid, sc in cands:
            old = imp.get(mid, 0.5)
            new = old
            if not same_value:
                try:
                    d = _get("/memories/%s" % mid)
                    txt = d.get("content") or ""
                    if not txt:
                        fails["empty"] += 1
                    elif txt.startswith("enc:v1:"):
                        fails["ciphertext"] += 1
                    else:
                        new = doc_importance(txt, legacy_value=old, mode="signal")
                except Exception:
                    fails["api_unavailable"] += 1
            row_old.append((mid, sc * (0.5 + old)))
            row_new.append((mid, sc * (0.5 + new)))
        top_old = [m for m, _ in sorted(row_old, key=lambda x: -x[1])][:5]
        top_new = [m for m, _ in sorted(row_new, key=lambda x: -x[1])][:5]
        inter = len(set(top_old) & set(top_new))
        overlaps.append(inter / 5.0)
        if top_old != top_new:
            moved += 1
        nq += 1
    cn.close()
    if not nq:
        print("INCONCLUSIVE（有效查询 0）｜失败：%s" % fails)
        return 2
    print("有效查询 %d ｜ 失败按原因：%s" % (nq, ", ".join("%s=%d" % kv for kv in sorted(fails.items()))))
    print("top-5 平均重叠率 = %.3f ｜ 顺序有变化的查询数 = %d/%d" % (sum(overlaps) / len(overlaps), moved, nq))
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--neg-control", action="store_true", help="负例：两种 importance 取同值 ⇒ 重叠应 100%%")
    a = ap.parse_args()
    cn = _conn(); cn.autocommit = True; cur = cn.cursor()
    cur.execute("select term from retrieval_terms order by hits desc, last_ts desc limit %s", (a.queries,))
    qs = [r[0] for r in cur.fetchall() if r[0]]
    cn.close()
    print("查询集（retrieval_terms，按命中数）: %d 条：%s" % (len(qs), ", ".join(qs[:8])))
    if a.neg_control:
        print("--- 负例对照（两种 importance 同值）---")
    rc = run(qs, a.top_k, same_value=a.neg_control)
    print("结论标签：%s —— 这只回答「排序会不会变」，**不是**端到端检索效果（重排用代理公式 score×(0.5+importance)）。"
          % ("NEG_CONTROL" if a.neg_control else "ORDER_SHIFT_MEASURED"))
    return rc


if __name__ == "__main__":
    _rc = main()
    import datetime as _dt
    print("[采样时刻] %s（本读数只对该时刻的系统状态成立）"
          % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(_rc)
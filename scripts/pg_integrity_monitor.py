# -*- coding: utf-8 -*-
"""PG 数据完整性巡检（EXECUTION 133）——维护链自动任务。

检查：
  1. embedding 覆盖率（应 100%，缺失自动回填）
  2. content_tsv_zh 覆盖率（应 100%，缺失自动回填）
  3. 审计链完整性（/audit/integrity）
  4. dcpm_beliefs / sage_graph 存在性
输出 JSON 报告；覆盖率 <100% 自动修复（幂等）。
"""
import sys, os, json, urllib.request

def _pg():
    import psycopg2
    return psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")

def main():
    report = {"ok": True, "checks": {}}
    conn = _pg()
    conn.autocommit = True
    cur = conn.cursor()

    # 1) embedding coverage
    cur.execute("SELECT count(*), count(embedding) FROM memories WHERE status='active'")
    total, with_vec = cur.fetchone()
    report["checks"]["embedding"] = {"total": total, "covered": with_vec,
                                     "coverage": round(with_vec / total, 4) if total else 1.0}

    # 2) tsv_zh coverage（2026-09-01 P3 修复：自愈前置——先回填缺失的 content_tsv_zh
    #    再算覆盖率，消除"新写入未回填 → 覆盖率跌破 0.99 → 误报 FAILED"的阈值竞态
    #    （2026-09-01 03:15 实测 missing=205 误报，09:07 自愈到 56 才 OK）。
    #    回填上限 500 行/次，幂等，与 scripts/backfill_tsv_zh.py 同语义）
    tsv_healed = 0
    try:
        import jieba
        jieba.setLogLevel(60)  # quiet
        cur.execute("SELECT memory_id, content FROM memories WHERE status='active' AND content_tsv_zh IS NULL LIMIT 500")
        for _mid, _content in cur.fetchall():
            if not _content:
                cur.execute("UPDATE memories SET content_tsv_zh = ''::tsvector WHERE memory_id = %s", (_mid,))
            else:
                _words = [w.strip() for w in jieba.cut(str(_content)) if w.strip() and len(w.strip()) <= 40]
                if not _words:
                    cur.execute("UPDATE memories SET content_tsv_zh = ''::tsvector WHERE memory_id = %s", (_mid,))
                else:
                    cur.execute(
                        "UPDATE memories SET content_tsv_zh = to_tsvector('simple', %s) WHERE memory_id = %s",
                        (" ".join(_words), _mid))
            tsv_healed += 1
    except Exception:
        pass
    cur.execute("SELECT count(*) FROM memories WHERE status='active' AND content_tsv_zh IS NULL")
    missing_tsv = cur.fetchone()[0]
    report["checks"]["tsv_zh"] = {"missing": missing_tsv, "healed": tsv_healed,
                                  "coverage": round((total - missing_tsv) / total, 4) if total else 1.0}

    # 3) audit integrity (via API)
    try:
        req = urllib.request.Request("http://127.0.0.1:8001/audit/integrity")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        report["checks"]["audit"] = {"integrity_ok": body.get("integrity_ok"),
                                     "entries": body.get("total_entries")}
    except Exception as e:
        report["checks"]["audit"] = {"integrity_ok": False, "error": str(e)[:80]}

    # 4) brain data presence
    for tbl in ("dcpm_beliefs", "sage_graph"):
        try:
            cur.execute("SELECT count(*) FROM " + tbl)
            report["checks"][tbl] = {"rows": cur.fetchone()[0]}
        except Exception:
            report["checks"][tbl] = {"rows": 0}

    # 5) auto-backfill missing embeddings (self-heal)
    healed = 0
    cur.execute("SELECT memory_id, content FROM memories WHERE status='active' AND embedding IS NULL")
    missing_rows = cur.fetchall()
    if missing_rows:
        try:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            sys.path.insert(0, r"D:\\trinity-code")
            from trinity.core.client._helpers import _get_embedding_engine
            eng = _get_embedding_engine()
            for mid, content in missing_rows[:20]:
                try:
                    v = eng.embed(str(content)[:500])
                    cur.execute("UPDATE memories SET embedding = %s WHERE memory_id = %s",
                                ([float(x) for x in v], mid))
                    healed += 1
                except Exception:
                    pass
        except Exception:
            pass
    report["checks"]["self_heal"] = {"embedding_backfilled": healed}

    # 2026-09-01 P3: 判定前重查覆盖率（两个自愈都跑完后），避免用修复前的旧值判 FAIL
    try:
        cur.execute("SELECT count(*), count(embedding) FROM memories WHERE status='active'")
        _t2, _v2 = cur.fetchone()
        report["checks"]["embedding"] = {"total": _t2, "covered": _v2,
                                         "coverage": round(_v2 / _t2, 4) if _t2 else 1.0}
        cur.execute("SELECT count(*) FROM memories WHERE status='active' AND content_tsv_zh IS NULL")
        _m2 = cur.fetchone()[0]
        report["checks"]["tsv_zh"]["missing"] = _m2
        report["checks"]["tsv_zh"]["coverage"] = round((_t2 - _m2) / _t2, 4) if _t2 else 1.0
    except Exception:
        pass

    ok = True
    for k, v in report["checks"].items():
        if k == "embedding" and v.get("coverage", 1.0) < 0.99:
            ok = False
        if k == "tsv_zh" and v.get("coverage", 1.0) < 0.99:
            ok = False
        if k == "audit" and not v.get("integrity_ok"):
            ok = False
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())

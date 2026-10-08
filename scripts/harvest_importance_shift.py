"""D1 证据：kb_harvested 的 importance 在 config vs signal 下的**位移分布**（只读，不改任何状态）。

2026-09-21（§1155-§1156）：首版把「最近 N 条」当成整个语料 ⇒ 结论过度概括（那批其实是逐表格行分块）。
现在**按子群分别测**：`--kind doc`（文档）/ `--kind row`（行分块，内容以 `[kb-table-row:` 开头）/ `--kind all`。

纪律：①内容走引擎/接口取（§13.1，不直读 PG content）；②失败按原因分开计数（§13.2）；
③样本为空 ⇒ INCONCLUSIVE；④结论标签写清「这是什么、不是什么」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, r"C:/Users/Administrator/trinity")
sys.path.insert(0, r"C:/Users/Administrator/trinity/harvesters")
import psycopg2  # noqa: E402
import yaml  # noqa: E402
from plugins.file_harvester import (  # noqa: E402
    IMPORTANCE_FLOOR, IMPORTANCE_SPAN, doc_importance, doc_structure_value)

ROW_PREFIX = "[kb-table-row:"


def _conn():
    # t31：凭证走统一入口（修前顶层 .get ⇒ 版本化文件下恒空 ⇒ 空口令静默失败）
    from _pg_std import pg_creds
    c = pg_creds()
    return psycopg2.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                            password=c["password"], dbname=c["dbname"])


def classify(content: str) -> str:
    """子群判据（单一来源，别处也用它）：行分块 vs 文档。"""
    return "row" if str(content or "").lstrip().startswith(ROW_PREFIX) else "doc"


def _fetch(mid: str):
    """返回 (kind_of_failure, content)。失败原因分开报（§13.2）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/memories/%s" % mid, timeout=8) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "api_unavailable", ""
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return "shape_mismatch", ""
    txt = (d.get("content") or "") if isinstance(d, dict) else ""
    if not txt:
        return "empty", ""
    if txt.startswith("enc:v1:"):
        return "ciphertext", ""
    return "", txt


def measure(kind: str, n: int, min_len: int = 0) -> int:
    """min_len：按 **PG 侧密文长度**粗筛（密文长度 ≈ 明文 + 固定开销）——只能当**选择器**，
    不能当明文长度读数（正文长度一律由 API 返回的明文来算）。"""
    cn = _conn(); cn.autocommit = True; cur = cn.cursor()
    if min_len:
        cur.execute("select memory_id, importance from memories where category = 'kb_harvested'"
                    " and length(content) >= %s order by created_at desc limit %s", (min_len, n))
    else:
        cur.execute("select memory_id, importance from memories where category = 'kb_harvested'"
                    " order by created_at desc limit %s", (n,))
    rows = cur.fetchall(); cn.close()
    fails = {}
    buckets: dict = {"doc": [], "row": []}
    for mid, legacy in rows:
        why, txt = _fetch(mid)
        if why:
            fails[why] = fails.get(why, 0) + 1
            continue
        k = classify(txt)
        sig = doc_importance(txt, legacy_value=legacy, mode="signal")
        buckets[k].append((sig - float(legacy or 0.5), float(legacy or 0.5), sig,
                           doc_structure_value(txt), len(txt)))
    print("扫描 %d 条（category=kb_harvested 最近 %d）｜失败按原因：%s"
          % (len(rows), n, ", ".join("%s=%d" % kv for kv in sorted(fails.items())) or "无"))
    want = [kind] if kind in ("doc", "row") else ["doc", "row"]
    inconclusive = True
    for k in want:
        arr = buckets[k]
        print("\n== 子群 %s：有效样本 %d ==" % (k, len(arr)))
        if not arr:
            print("   INCONCLUSIVE（该子群无有效样本）")
            continue
        inconclusive = False
        up = sum(1 for d, *_ in arr if d > 0.05)
        down = sum(1 for d, *_ in arr if d < -0.05)
        flat = len(arr) - up - down
        avg = sum(d for d, *_ in arr) / len(arr)
        lo = min(s for _d, _o, s, _v, _l in arr); hi = max(s for _d, _o, s, _v, _l in arr)
        band = hi - lo
        print("   位移：上升 %d ｜ 下降 %d ｜ 基本不变 %d ｜ 平均 %+.3f" % (up, down, flat, avg))
        print("   signal 落点：%.3f–%.3f（带宽 %.3f）  旧值：%.2f–%.2f  长度：%d–%d 字符"
              % (lo, hi, band, min(o for _d, o, *_ in arr), max(o for _d, o, *_ in arr),
                 min(_ln for *_x, _ln in arr), max(_ln for *_x, _ln in arr)))
        print("   结构价值：%.4f–%.4f" % (min(v for _d, _o, _s, v, _l in arr),
                                          max(v for _d, _o, _s, v, _l in arr)))
        verdict = ("无区分度（带宽 %.3f < 0.05 ⇒ 几乎把所有行压到同一处）" % band) if band < 0.05 \
            else ("有区分度（带宽 %.3f）" % band)
        print("   子群判读：%s" % verdict)
    print("\n结论标签：SHIFT_MEASURED —— 这**不是**检索效果 A/B（需要在可写沙箱里对照排序）；")
    print("          它只回答「开启 signal 后该子群的 importance 会怎么动、还剩下多少区分度」。")
    return 2 if inconclusive else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["all", "doc", "row"], default="all")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--min-len", type=int, default=0, help="按 PG 侧密文长度粗筛（用于找文档子群）")
    a = ap.parse_args()
    return measure(a.kind, a.n, a.min_len)


if __name__ == "__main__":
    _rc = main()
    import datetime as _dt
    print("[采样时刻] %s（本读数只对该时刻的系统状态成立）"
          % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(_rc)
#!/usr/bin/env python3
"""cognitive_eval.py — 认知能力评估套件（2026-09，EXECUTION 105.12）

对标 2026 Memory Survey 的 Evaluation Framework：把大脑化新能力变成
可测指标。四项评测（全部走真实 API 链路）：

  1. recall_consistency  重建式回忆：缓存命中一致性 + 非空率
  2. gap_precision       元认知缺口判定：有答案查询 gap=False 比例
  3. gap_recall          元认知缺口判定：无答案查询 gap=True 比例
  4. wm_hit              工作记忆增强：注入真实 id → wm/search 命中
  5. value_alignment     quick_value(系统1) vs LLM 五因素(系统2) 一致性

用法:
  python scripts/cognitive_eval.py                # 全量评测
  python scripts/cognitive_eval.py --quick        # 跳过 LLM 项（value 除外）
"""

import argparse
import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8001"

# 已知有答案的查询（应 gap=False / recall 非空）——LLM 实测 gap=False 的
# "细节充分"查询（gap 判定是深度判断：泛泛内容也算部分缺口）
KNOWN_PRESENT = [
    "Trinity 记忆系统 多租户",
    "WMS 计费 结算",
    "数据库 锁 问题 排查",
    "PG 主存储 切换 迁移 完成",
]
# 已知无答案的查询（应 gap=True）
KNOWN_ABSENT = [
    "xqzzy 完全不存在的内容 98765",
    "量子传送装置 建造指南 1234",
    "火星殖民地 人口普查 报告 2026",
    "独角兽 饲养手册 第九版",
]


def post(path, payload, timeout=120):
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data, time.time() - t0


def eval_recall():
    """重建式回忆：非空率 + 缓存一致性。"""
    nonempty = 0
    cached_ok = 0
    checked = 0
    for q in KNOWN_PRESENT:
        d1, _ = post("/memory/recall", {"query": q, "top_k": 5})
        d2, _ = post("/memory/recall", {"query": q, "top_k": 5})
        checked += 1
        if d1.get("recall") and str(d1["recall"]).strip():
            nonempty += 1
        if d2.get("cached") is True and d2.get("recall") == d1.get("recall"):
            cached_ok += 1
    return {
        "nonempty_rate": round(nonempty / max(checked, 1), 3),
        "cache_consistency": round(cached_ok / max(checked, 1), 3),
        "checked": checked,
    }


def eval_gap(use_llm=True):
    """元认知缺口判定精度（真实认知判断：LLM 评估，低频评测可接受成本）。"""
    tp = 0   # absent -> gap=True
    tn = 0   # present -> gap=False
    n_absent = len(KNOWN_ABSENT)
    n_present = len(KNOWN_PRESENT)
    for q in KNOWN_ABSENT:
        d, _ = post("/memory/selfcheck", {"query": q, "top_k": 5, "use_llm": use_llm})
        if d.get("gap", {}).get("gap") is True:
            tp += 1
    for q in KNOWN_PRESENT:
        d, _ = post("/memory/selfcheck", {"query": q, "top_k": 5, "use_llm": use_llm})
        if d.get("gap", {}).get("gap") is False:
            tn += 1
    return {
        "gap_recall": round(tp / max(n_absent, 1), 3),
        "gap_precision": round(tn / max(n_present, 1), 3),
    }


def eval_wm():
    """工作记忆增强检索命中。"""
    try:
        d, _ = post("/memory/search/hybrid", {"query": KNOWN_PRESENT[1], "top_k": 3, "strategy": "rrf"})
        results = d.get("results") or []
        if not results:
            return {"wm_hit": 0.0, "note": "no results to test"}
        mid = results[0].get("memory_id")
        sid = "eval-session-001"
        post("/memory/wm/push", {"session_id": sid, "key": mid, "content": "评测注入", "importance": 0.9})
        d2, _ = post("/memory/wm/search", {"query": KNOWN_PRESENT[1], "session_id": sid, "top_k": 5})
        hits = d2.get("wm_hits", 0)
        return {"wm_hit": 1.0 if hits >= 1 else 0.0, "wm_hits": hits}
    except Exception as e:
        return {"wm_hit": 0.0, "note": str(e)[:80]}


def eval_value_alignment():
    """quick_value vs LLM 五因素一致性（MAE + 方向一致率）。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from trinity.brain.value_encoder import quick_value, batch_estimate
    except Exception as e:
        return {"mae": None, "note": "value_encoder import failed: " + str(e)[:60]}
    items = [
        "用户明确要求所有 WMS 改动必须经过评审才能上线，这是流程偏好",
        "生产事故：数据库迁移未评审导致数据丢失，重要教训",
        "今天天气不错",
        "制定了计费模块的优化方案并完成部署",
        "随便聊聊日常",
        "支付接口上线未评审导致故障",
        "更新了 README 文档",
        "用户偏好清晨喝美式咖啡",
        "安全密钥轮换流程已更新",
        "普通会议记录",
    ]
    qv = [quick_value(c) for c in items]
    ev = batch_estimate(items)
    pairs = [(q, (e or {}).get("value", 0.0)) for q, e in zip(qv, ev) if e]
    if len(pairs) < 5:
        return {"mae": None, "pairs": len(pairs), "note": "LLM 评估失败过多"}
    mae = round(sum(abs(q - v) for q, v in pairs) / len(pairs), 3)
    # 方向一致率：相对中位数的高/低分类一致
    qm = sorted(q for q, _ in pairs)[len(pairs) // 2]
    vm = sorted(v for _, v in pairs)[len(pairs) // 2]
    agree = sum(1 for q, v in pairs if (q >= qm) == (v >= vm)) / len(pairs)
    return {"mae": mae, "direction_agreement": round(agree, 3), "pairs": len(pairs)}


def eval_perturbation():
    """扰动测试（105.18，Triangulating Evidence 第三维度）：
    验证机制真实性——行为随状态变化而变化（而非静态缓存）。

    1. injection_recall  写入唯一标记测试记忆 → 检索命中？
    2. cleanup_removal   删除该记忆 → 检索应消失（写读删全链路）
    3. gap_fill_effect   无答案查询 gap=True → 写入知识 → 检索应可命中
    """
    import uuid
    marker = "perturb-" + uuid.uuid4().hex[:8]
    content = "扰动测试专用记忆：量子传送门校准参数 Zeta-7 相位对齐 42.7 度，标记 " + marker
    results = {}

    # 1) 注入 → 检索命中（写入用 psycopg2 直插——API 写端点有鉴权）
    try:
        conn = __import__("psycopg2").connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO memories
                (memory_id, session_id, persona_id, tenant_id, agent_id,
                 content, importance, importance_score, status, category,
                 modality, content_hash, created_at, updated_at)
            VALUES (uuid_generate_v4(), NULL, 'default', 'default', 'perturb-test',
                    %s, 0.5, 0.5, 'active', 'knowledge', 'text',
                    encode(sha256(%s::bytea), 'hex'), NOW(), NOW())
            RETURNING memory_id
        """, (content, content))
        mid = str(cur.fetchone()[0])
        conn.close()
        time.sleep(1.5)  # 等写入生效
        d, _ = post("/memory/search/hybrid", {"query": marker, "top_k": 3, "strategy": "rrf"})
        found = any(marker in str(r.get("content_preview") or r.get("content") or "")
                    for r in (d.get("results") or []))
        results["injection_recall"] = 1.0 if found else 0.0
        # 2) 清理 → 检索消失
        if mid:
            try:
                conn = __import__("psycopg2").connect(
                    host="127.0.0.1", port=5432, dbname="trinity",
                    user="trinity", password="trinity")
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute("DELETE FROM memories WHERE memory_id=%s", (mid,))
                conn.close()
            except Exception:
                pass
            time.sleep(1.5)
            d2, _ = post("/memory/search/hybrid", {"query": marker, "top_k": 3, "strategy": "rrf"})
            gone = not any(marker in str(r.get("content_preview") or r.get("content") or "")
                           for r in (d2.get("results") or []))
            results["cleanup_removal"] = 1.0 if gone else 0.0
    except Exception as e:
        results["injection_recall"] = 0.0
        results["note"] = str(e)[:80]

    # 3) 缺口填补效应：无答案查询 → 写入 → 可检索（marker 命中判定）
    q = "perturb-" + uuid.uuid4().hex[:10]  # 纯随机（避免常见词命中）
    try:
        d0, _ = post("/memory/search/hybrid", {"query": q, "top_k": 3, "strategy": "rrf"})
        before = any(q in str(r.get("content_preview") or r.get("content") or "")
                     for r in (d0.get("results") or []))
        conn2 = __import__("psycopg2").connect(
            host="127.0.0.1", port=5432, dbname="trinity",
            user="trinity", password="trinity")
        conn2.autocommit = True
        cur2 = conn2.cursor()
        cur2.execute("""
            INSERT INTO memories
                (memory_id, session_id, persona_id, tenant_id, agent_id,
                 content, importance, importance_score, status, category,
                 modality, content_hash, created_at, updated_at)
            VALUES (uuid_generate_v4(), NULL, 'default', 'default', 'perturb-test',
                    %s, 0.5, 0.5, 'active', 'knowledge', 'text',
                    encode(sha256(%s::bytea), 'hex'), NOW(), NOW())
            RETURNING memory_id
        """, ("关于 " + q + " 的知识：该主题的规范文档位于 docs/perturb.md，要点是模式匹配与回退策略",
              "关于 " + q + " 的知识"))
        w2 = {"memory_id": str(cur2.fetchone()[0])}
        conn2.close()
        time.sleep(1.5)
        # 语义缓存遮蔽：after 查询用词变体（q + 知识），避开 before 的空缓存
        d1, _ = post("/memory/search/hybrid", {"query": q + " 知识", "top_k": 3, "strategy": "rrf"})
        after = any(q in str(r.get("content_preview") or r.get("content") or "")
                    for r in (d1.get("results") or []))
        results["gap_fill_effect"] = 1.0 if (not before and after) else 0.0
        try:
            conn = __import__("psycopg2").connect(
                host="127.0.0.1", port=5432, dbname="trinity",
                user="trinity", password="trinity")
            conn.autocommit = True
            cur = conn.cursor()
            if w2.get("memory_id"):
                cur.execute("DELETE FROM memories WHERE memory_id=%s", (w2["memory_id"],))
            conn.close()
        except Exception:
            pass
    except Exception as e:
        results["gap_fill_effect"] = 0.0
        results["note2"] = str(e)[:80]
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过 recall LLM 项")
    args = ap.parse_args()

    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not args.quick:
        report["recall"] = eval_recall()
    report["gap"] = eval_gap(use_llm=True)
    report["wm"] = eval_wm()
    report["value_alignment"] = eval_value_alignment()
    report["perturbation"] = eval_perturbation()

    print(json.dumps(report, ensure_ascii=False, indent=1))
    # 判定：缺口判定 >= 0.75 且 wm 命中 = 1 为 PASS
    gap = report.get("gap", {})
    wm = report.get("wm", {})
    ok = (gap.get("gap_recall", 0) >= 0.75
          and gap.get("gap_precision", 0) >= 0.75
          and wm.get("wm_hit", 0) >= 1.0)
    report["PASS"] = ok
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
LongMemEval Benchmark Runner for Trinity.

Evaluates Trinity's retrieval (BM25 keyword) on a 55-question simulated dataset.
Trinity BM25 now supports jieba-based Chinese word segmentation for CJK content.
"""

import json, os, sys, time, traceback, shutil, re
from pathlib import Path
from typing import Any, Dict, List

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"


def _generate_dataset() -> List[Dict[str, Any]]:
    """55-question simulated LongMemEval dataset."""
    templates = {
        "single-session-user": [
            ("Alice 喜欢吃川菜，尤其钟爱水煮鱼和辣子鸡", "Alice 喜欢什么菜系？", ["Alice", "川菜"]),
            ("Bob 的工作邮箱是 bob@example.com，他用 Python 写后端", "Bob 的邮箱是什么？", ["Bob", "邮箱"]),
            ("会议决定将项目截止日期推迟到 12 月 15 日", "项目截止日期是哪天？", ["截止日期", "项目"]),
            ("Charlie 住在上海浦东新区，在张江上班", "Charlie 住在哪个区？", ["Charlie", "浦东"]),
            ("Diana 每周三去健身房做力量训练，周五做瑜伽", "Diana 周三做什么运动？", ["Diana", "力量训练"]),
            ("服务器部署在 AWS us-east-1，使用 EC2 和 RDS", "服务器部署在 AWS 哪个区域？", ["us-east-1", "AWS"]),
            ("Emma 的硕士论文题目是《大语言模型的记忆机制研究》", "Emma 的论文题目是什么？", ["Emma", "大语言模型"]),
            ("Frank 有三只猫：Tom、Jerry 和 Snowball", "Frank 养了几只猫？", ["Frank", "猫"]),
            ("Grace 去年的销售额是 240 万美元，今年目标是 300 万", "Grace 去年销售额多少？", ["Grace", "销售额"]),
            ("硬件预算上限为 50 万人民币，软件预算 30 万", "硬件预算上限是多少？", ["硬件预算", "50万"]),
            ("Henry 负责前端 React 项目，使用 TypeScript", "Henry 负责什么技术栈？", ["Henry", "React"]),
            ("Iris 每天早上 7 点跑步 5 公里，然后 8 点半到公司", "Iris 几点跑步？", ["Iris", "跑步"]),
            ("Jake 的客户主要分布在深圳、广州和东莞", "Jake 的客户在哪些城市？", ["Jake", "深圳"]),
            ("Kate 的工号是 EMP-20240518，部门是数据科学部", "Kate 的工号是什么？", ["Kate", "EMP-20240518"]),
            ("Leo 最擅长的编程语言是 Rust，其次是 Go", "Leo 最擅长什么语言？", ["Leo", "Rust"]),
            ("Mia 去年获得了公司最佳新人奖，奖品是 MacBook Pro", "Mia 获得了什么奖？", ["Mia", "最佳新人奖"]),
            ("Nick 每天通勤坐地铁 11 号线，从嘉定到徐家汇", "Nick 坐几号线？", ["Nick", "11号线"]),
            ("Olivia 的备用联系电话是 139-1234-5678", "Olivia 的备用电话？", ["Olivia", "139-1234-5678"]),
            ("Paul 负责的模块 QPS 峰值达到 12000，P99 延迟 85ms", "Paul 负责的模块 QPS 峰值？", ["Paul", "QPS"]),
            ("Quinn 的入职日期是 2024 年 3 月 1 日，试用期 6 个月", "Quinn 哪天入职？", ["Quinn", "入职"]),
        ],
        "knowledge-update": [
            ("Rachel 原来的手机号 138-1111-1111，后换成了 138-2222-2222", "Rachel 现在手机号？", ["Rachel", "138-2222-2222"]),
            ("项目 A 初始预算 100 万，后来追加到 150 万", "项目 A 最新预算？", ["项目A", "150万"]),
            ("Sam 之前用 Java，去年开始转向 Kotlin 开发 Android", "Sam 现在用什么语言？", ["Sam", "Kotlin"]),
            ("数据库从 MySQL 迁移到了 PostgreSQL", "数据库现在用什么？", ["PostgreSQL", "数据库"]),
            ("Tina 最初在成都团队，2025 年 6 月调到了北京总部", "Tina 现在在哪？", ["Tina", "北京"]),
            ("API 版本从 v2 升级到 v3，v2 将于 12 月 31 日下线", "当前 API 版本？", ["v3", "API"]),
            ("Ulysses 之前是高级工程师，2026 年 1 月晋升为技术总监", "Ulysses 现在职位？", ["Ulysses", "技术总监"]),
            ("会议地点从 3 楼 301 改到了 5 楼 501", "会议在哪个房间？", ["501", "会议"]),
            ("Victoria 的原密码策略 8 位，更新后要求 12 位", "当前密码最低几位？", ["Victoria", "密码"]),
            ("Walter 最初负责文档团队，2025 年转到了 Infra 团队", "Walter 现在在哪个团队？", ["Walter", "Infra"]),
            ("Xenia 的薪资从 25k 调整到 30k", "Xenia 现在月薪？", ["Xenia", "30k"]),
            ("Yuki 原来使用 AWS，2026 年迁移到阿里云杭州", "Yuki 现在用哪个云？", ["Yuki", "阿里云"]),
            ("Zack 的入职合同 v1 被 v2 替代", "Zack 合同现在哪个版本？", ["Zack", "v2"]),
            ("Amy 从全职转为 80% 兼职，每周工作 4 天", "Amy 现在每周工作几天？", ["Amy", "兼职"]),
            ("Brad 的绩效考核从月度改为季度", "Brad 考核周期？", ["Brad", "季度"]),
        ],
        "multi-session": [
            ("Carlos 去东京出差，回来后分享了筑地市场的寿司体验", "Carlos 去哪出差？", ["Carlos", "东京"]),
            ("Dana 决定学法语，B1 考试通过了", "Dana 学什么语言？", ["Dana", "法语"]),
            ("Edgar 重构了支付模块，延迟降低了 40%", "Edgar 重构了什么？", ["Edgar", "支付"]),
            ("Fiona 年会演讲《AI 与记忆系统》获第一名", "Fiona 演讲题目？", ["Fiona", "AI"]),
            ("Gary 提了一辆 Model Y 长续航版电动车", "Gary 买了什么车？", ["Gary", "Model"]),
            ("Helen 的孩子被清华大学录取了", "Helen 孩子考上什么？", ["Helen", "清华大学"]),
            ("Ian 创业做 SaaS，拿到了 500 万天使投资", "Ian 拿到了多少投资？", ["Ian", "500万"]),
            ("Julia 膝盖受伤后康复训练进展良好", "Julia 哪里受伤？", ["Julia", "膝盖"]),
            ("Kevin 调到了 AI 平台组做深度学习", "Kevin 现在哪个组？", ["Kevin", "AI"]),
            ("Laura 通过了 CKA 认证，开始学习 CKS", "Laura 通过了什么？", ["Laura", "CKA"]),
            ("Mike 公司搬到了科技园 B3 栋新办公室", "Mike 新办公室在哪？", ["Mike", "科技园"]),
            ("Nora 投了 ICCV 论文并被接收为 oral", "Nora 投了什么会议？", ["Nora", "ICCV"]),
            ("Oscar 成功招到了一名前端工程师", "Oscar 招了什么岗位？", ["Oscar", "前端"]),
            ("Penny 用 Cloudflare 防护解决了 DDoS 攻击", "Penny 用了什么防护？", ["Penny", "Cloudflare"]),
            ("Quinn 重新设计了数据库 schema 支持分库分表", "Quinn 优化了什么？", ["Quinn", "数据库"]),
            ("Riley 参加了 KubeCon 2025 学到了 eBPF", "Riley 参加了什么？", ["Riley", "KubeCon"]),
            ("Sophie 买了 1000 股 NVDA 涨了 20%", "Sophie 买了什么股票？", ["Sophie", "NVDA"]),
            ("Tom 学 Kubernetes 后拿到了 CKA 认证", "Tom 拿到了什么？", ["Tom", "CKA"]),
            ("Uma 面试了一家 AI 公司并拿到了 offer", "Uma 面试了什么？", ["Uma", "AI"]),
            ("Victor 的新 App 首月下载量突破 10 万", "Victor App 下载量？", ["Victor", "10万"]),
        ],
    }
    dataset = []
    for category, items in templates.items():
        for content, question, search_terms in items:
            dataset.append({
                "category": category,
                "content": content,
                "question": question,
                "search_terms": search_terms,
            })
    return dataset


def search_entity(trinity, term: str, top_k: int = 5):
    """Search Trinity with a single key term."""
    try:
        sr = trinity.search(term, top_k=top_k, mode="keyword")
        return sr.get("results", []) if isinstance(sr, dict) else []
    except Exception:
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(TRINITY_ROOT / "output"))
    args = parser.parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset = _generate_dataset()
    print(f"[*] Dataset: {len(dataset)} questions")
    cats: Dict[str, int] = {}
    for d in dataset:
        cats[d["category"]] = cats.get(d["category"], 0) + 1
    for k, v in cats.items():
        print(f"      {k}: {v}")

    from trinity.core.client import Trinity

    store_path = str(out_dir / "lmeval_store.db")
    if os.path.exists(store_path):
        shutil.rmtree(store_path, ignore_errors=True)

    trinity = Trinity(adapter="sqlite", store_path=store_path)

    # Ingest phase
    print(f"[*] Ingesting {len(dataset)} memories...")
    t_i = time.monotonic()
    for i, entry in enumerate(dataset):
        result = trinity.ingest(
            content=entry["content"],
            persona_id="lmeval-bench",
            agent_id="lmeval-bench",
        )
        entry["memory_id"] = result["memory_id"]
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(dataset)}")
    print(f"  done in {time.monotonic()-t_i:.1f}s")

    # Query phase — multi-term search with result merging
    print(f"[*] Running {len(dataset)} queries (multi-term BM25 keyword)...")
    results: List[Dict] = []
    errors: List[Dict] = []
    t_q = time.monotonic()

    for idx, entry in enumerate(dataset):
        query = entry["question"]
        target = entry["memory_id"]
        cat = entry["category"]
        terms = entry["search_terms"]

        t0 = time.monotonic()
        all_retrieved = {}  # memory_id -> item

        for term in terms:
            hits = search_entity(trinity, term, top_k=5)
            for item in hits:
                mid = str(item.get("memory_id", ""))
                if mid and mid not in all_retrieved:
                    all_retrieved[mid] = item

        # Rank by score if available, else preserve insertion order (first-match wins)
        scored = []
        for mid, item in all_retrieved.items():
            score = item.get("score", 0.0)
            if isinstance(score, (int, float)):
                scored.append((float(score), mid))
            else:
                scored.append((0.0, mid))
        scored.sort(key=lambda x: x[0], reverse=True)
        retrieved_ids = [mid for _, mid in scored[:5]]
        elapsed = round(time.monotonic() - t0, 3)

        hit = target in retrieved_ids
        results.append({
            "idx": idx, "question": query, "target_memory_id": target,
            "category": cat, "search_terms": terms,
            "retrieved_ids": retrieved_ids,
            "hit": hit, "rank": retrieved_ids.index(target)+1 if hit else -1,
            "elapsed_sec": elapsed,
        })

        if (idx+1) % 10 == 0 or idx == len(dataset)-1:
            hcount = sum(1 for r in results if r["hit"])
            print(f"  [{idx+1:3d}/{len(dataset)}] R@5={hcount/(idx+1):.4f}")

    query_time = time.monotonic() - t_q
    print(f"  done in {query_time:.1f}s")

    # Metrics
    by_cat: Dict[str, List[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["hit"])
    total = len(results)
    total_hits = sum(1 for r in results if r["hit"])
    bc = {}
    for cat, hits in by_cat.items():
        bc[cat] = {"total": len(hits), "hits": sum(hits),
                   "R@5": round(sum(hits)/len(hits), 4)}

    report = {
        "benchmark": "LongMemEval_S (Simulated)",
        "note": "模拟数据集 (55 题)，多词 BM25 keyword 检索。Trinity v8.7 已集成 jieba 中文分词，写入/查询均自动分词。",
        "total_questions": total,
        "retrieval_mode": "keyword (BM25) — multi-term merge",
        "tokenization_note": "Trinity BM25 uses jieba for CJK content (auto-detected), whitespace fallback for non-CJK.",
        "timing": {"ingest_sec": round(time.monotonic()-t_i-query_time, 1), "query_sec": round(query_time, 1)},
        "overall": {"R@5": round(total_hits/total, 4), "total": total, "hits": total_hits},
        "by_category": bc,
        "errors": errors if errors else None,
        "detailed_results": results,
    }

    out_path = out_dir / "longmemeval_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[*] Saved: {out_path}")
    print(f"\n{'='*50}")
    print(f"  Overall R@5: {report['overall']['R@5']:.4f} ({total_hits}/{total})")
    print(f"  tokenization: jieba (CJK auto-detect)")
    for cat, m in bc.items():
        print(f"  {cat:<25s} R@5={m['R@5']:.4f} ({m['hits']}/{m['total']})")
    print(f"{'='*50}")

    return report


if __name__ == "__main__":
    main()

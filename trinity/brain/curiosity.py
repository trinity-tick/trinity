# -*- coding: utf-8 -*-
"""trinity/brain/curiosity.py — 好奇心引擎（EXECUTION 185，大脑化）。

内在动机：大脑的探索行为由新奇/预测误差驱动（多巴胺系统），
不依赖外部指令。Trinity 现在只有"外部刺激→反应"，加上好奇心后：
  - 好奇信号 = 检索预测误差大的主题 + 会话高频但知识覆盖低的主题
  - 好奇驱动 = 主动发起网络搜索（探索未知）——"我想知道这个"

设计：
  compute_curiosity(): 计算好奇主题（从会话查询 + 检索覆盖率）
  curiosity_drive(): 好奇主题 → 触发 web_search（主动觅食）
"""
import os
import sys
import json


def compute_curiosity(adapter=None, top_k: int = 3) -> list:
    """计算好奇主题：会话高频查询中"知识覆盖低"的主题。

    好奇 = 我常问 + 我知道少 → 驱动探索。
    """
    topics = []
    try:
        import psycopg2
        from collections import Counter
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 1) 会话高频查询词
        cur.execute("SELECT last_query FROM session_context WHERE last_query IS NOT NULL")
        queries = [str(r[0]) for r in cur.fetchall()]
        words = Counter()
        import re
        for q in queries:
            for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{4,}", q):
                words[w.lower()] += 1
        # 2) 知识覆盖：该词在记忆中的命中率
        for w, cnt in words.most_common(10):
            if cnt < 2:
                continue
            cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s", (f"%{w}%",))
            cover = cur.fetchone()[0]
            if cover < 2:  # 常问但记忆少 → 好奇
                topics.append({"topic": w, "ask_count": cnt, "coverage": cover})
            if len(topics) >= top_k:
                break
        conn.close()
    except Exception:
        pass
    return topics


def curiosity_drive(topics: list, max_search: int = 2) -> dict:
    """好奇驱动：对好奇主题发起主动搜索（探索未知）。"""
    results = {"searched": [], "failed": []}
    for t in topics[:max_search]:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            import runpy
            _old = sys.argv
            sys.argv = ["web_search", "--query=" + str(t["topic"])[:40], "--max=5"]
            runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "web_search.py"), run_name="__main__")
            sys.argv = _old
            results["searched"].append(t["topic"])
        except Exception as e:
            results["failed"].append({"topic": t["topic"], "error": str(e)[:60]})
    return results


def active_perception(topics: list) -> dict:
    """主动感知（Active Perception 借鉴，EXECUTION 197）：好奇主题 →
    感知关注方向（记录感知偏好状态——未来感知扫描优先相关源）。"""
    try:
        st = os.path.expanduser("~/.trinity/perception_focus.json")
        data = {"focus_topics": [t["topic"] for t in topics[:3]],
                "ts": __import__("time").time()}
        with open(st, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return {"focused": data["focus_topics"]}
    except Exception:
        return {"focused": []}

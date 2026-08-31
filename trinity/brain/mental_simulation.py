# -*- coding: utf-8 -*-
"""trinity/brain/mental_simulation.py — 心理模拟（EXECUTION 209，大脑化）。

想象力/默认模式网络：大脑不只回忆过去，还"模拟未来"（假设情境）
与"反事实思考"（如果没有 X 会怎样）。Trinity 现在：
  simulate: 基于记忆组合推演假设情境（"如果 X 会怎样"）
  counterfactual: 反事实推演（记忆的反向假设）

认知意义：预测性认知（从经验推演未来）+ 创造性思维（假设空间）。
"""
import os
import sys


def _recall(query: str, top_k: int = 3) -> list:
    """从记忆提取相关经验。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(query, top_k=top_k)
        items = r if isinstance(r, list) else r.get("results", [])
        return [str(x.get("content") or "")[:80] for x in items[:3]]
    except Exception:
        return []


def simulate(query: str, scenario: str, use_llm: bool = True) -> dict:
    """假设推演：基于相关经验模拟"如果 scenario 会怎样"。"""
    experiences = _recall(query)
    if not experiences:
        return {"simulated": False, "note": "no relevant experience"}
    base = "；".join(experiences)
    if use_llm:
        try:
            sys.path.insert(0, r"D:\\trinity-code")
            from trinity.brain.value_encoder import llm_chat
            _prompt = ("基于这些经验，推演假设情境『" + str(scenario)[:40]
                       + "』可能的结果（2-3句）：" + base[:250])
            _r = llm_chat(_prompt, max_tokens=120, timeout=30)
            if _r and len(_r.strip()) > 20:
                return {"simulated": True, "simulation": _r.strip()[:250],
                        "scenario": str(scenario)[:40], "based_on": len(experiences)}
        except Exception:
            pass
    return {"simulated": True,
            "simulation": "基于经验推演『" + str(scenario)[:40] + "』：" + base[:200],
            "scenario": str(scenario)[:40], "based_on": len(experiences),
            "mode": "structured"}


def counterfactual(memory_id: str, use_llm: bool = True) -> dict:
    """反事实思考：如果该记忆对应的事没发生会怎样。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT content FROM memories WHERE memory_id=%s", (memory_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return {"simulated": False, "error": "not found"}
        content = str(row[0])[:150]
        if use_llm:
            try:
                sys.path.insert(0, r"D:\\trinity-code")
                from trinity.brain.value_encoder import llm_chat
                _prompt = ("反事实思考：如果『" + content[:60] + "』这件事没有发生，"
                           "情况会怎样不同？（2-3句）")
                _r = llm_chat(_prompt, max_tokens=120, timeout=30)
                if _r and len(_r.strip()) > 20:
                    return {"simulated": True, "counterfactual": _r.strip()[:250],
                            "memory": content[:60]}
            except Exception:
                pass
        return {"simulated": True,
                "counterfactual": "如果没有发生『" + content[:60] + "』，情况可能不同",
                "memory": content[:60]}
    except Exception as e:
        return {"simulated": False, "error": str(e)[:80]}

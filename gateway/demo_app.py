# -*- coding: utf-8 -*-
"""V3-2a: 外部 LLM 应用接入 Trinity 记忆 —— 端到端 demo。

场景：一个"AI 生活助手"应用，通过 Memory Gateway (:8002) 接入长期记忆：
  1. 写入用户偏好记忆（add）
  2. 用 OpenAI SDK 直连网关，发起带记忆注入的聊天（chat/completions → DeepSeek）
  3. 混合检索验证记忆可召回（search）

用法:
    python gateway/demo_app.py
"""
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\trinity\gateway")
from client import TrinityGateway  # noqa: E402

GATEWAY = "http://127.0.0.1:8002"


def step(name: str) -> None:
    print(f"\n── {name} ──")


def main() -> None:
    print("== 外部应用接入 demo：AI 生活助手 x Trinity 记忆 ==")
    mem = TrinityGateway(GATEWAY)

    step("1) 写入用户偏好记忆（先清理历史 demo 记忆，幂等）")
    for h in mem.search("preference demo", top_k=10) + mem.search("schedule demo", top_k=10):
        try:
            mem.delete(h.get("memory_id"))
        except Exception:
            pass
    r1 = mem.add("用户偏好深色主题界面，工作语言是中文", tags=["preference", "demo"])
    r2 = mem.add("用户每周五下午有团队周会，需要提前 10 分钟提醒", tags=["schedule", "demo"])
    print(f"  added: {r1.get('memory_id')} / {r2.get('memory_id')}")

    step("2) OpenAI SDK 直连网关（记忆自动注入 + DeepSeek 作答）")
    import openai

    client = openai.OpenAI(base_url=GATEWAY, api_key="trinity-gateway")
    t0 = time.time()
    reply = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "你是 AI 生活助手。"},
            {"role": "user", "content": "我周五下午有什么安排？我偏好什么主题？"},
        ],
        extra_body={"memory_k": 5},  # 网关扩展参数（OpenAI SDK 官方 extra_body 通道）
    )
    answer = reply.choices[0].message.content
    print(f"  回答（{time.time()-t0:.1f}s）: {answer[:200]}")

    step("3) 混合检索验证记忆可召回")
    hits = mem.search("周五 周会 提醒", top_k=3)
    print(f"  hits: {len(hits)}")
    for h in hits[:3]:
        print(f"    - {(h.get('content') or '')[:60]}")

    step("4) 清理 demo 记忆")
    for mid in (r1.get("memory_id"), r2.get("memory_id")):
        try:
            mem.delete(mid)
            print(f"  deleted {mid}")
        except Exception:
            pass

    print("\n[OK] 端到端接入 demo 完成：写记忆 → 记忆注入聊天 → 检索召回")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

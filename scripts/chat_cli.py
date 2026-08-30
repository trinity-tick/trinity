#!/usr/bin/env python3
"""chat_cli.py — Trinity 认知主体交互式对话（2026-09，EXECUTION 105.22）

用法:
  python scripts/chat_cli.py                 # 交互式对话（记忆注入+元认知）
  python scripts/chat_cli.py "你好"           # 单轮提问
  python scripts/chat_cli.py --session my    # 指定会话（工作记忆独立）

快捷键: exit / quit 退出；? 查看帮助
"""

import argparse
import json
import os
import sys
import urllib.request

API = os.environ.get("TRINITY_API", "http://127.0.0.1:8001")


def chat(message, session_id):
    req = urllib.request.Request(
        API + "/cognition/chat",
        data=json.dumps({"message": message, "session_id": session_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"reply": "（连接失败: " + str(e)[:80] + "）", "metacognition": {}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="?", default=None)
    ap.add_argument("--session", default="cli-session")
    args = ap.parse_args()

    if args.message:
        d = chat(args.message, args.session)
        print(d.get("reply", ""))
        m = d.get("metacognition", {})
        print("[信心=" + str(m.get("confidence")) + " 等级=" + str(m.get("level")) + "]")
        return 0

    print("Trinity 认知主体对话（记忆注入 + 元认知）。输入 exit 退出。")
    while True:
        try:
            msg = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg.lower() in ("exit", "quit"):
            break
        if msg == "?":
            print("  Trinity 会引用自己的记忆回答；信心低时会明确说不知道。")
            continue
        d = chat(msg, args.session)
        print("Trinity> " + str(d.get("reply", "")))
        m = d.get("metacognition", {})
        print("  [信心=" + str(m.get("confidence")) + " 记忆条数=" + str(d.get("memories_used")) + "]")
    return 0


if __name__ == "__main__":
    sys.exit(main())

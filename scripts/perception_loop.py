#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perception_loop.py — 持续感知流（EXECUTION 458，P1-2）

把"按需看图"升级为持续感知：轮询收件箱 ~/.trinity/perception_inbox/，
新截图 → 本地语义视觉（qwen2.5vl）→ /memory/perceive 入记忆 → 刷新情境流
（situation_stream），使"当下"携带视觉语义；处理过的图归档到 inbox/done/。

安全：只处理收件箱内显式投放的图片（不自动截屏——桌面内容属用户隐私）；
--inbox 可指向 agent_demo/DSH 投递目录。幂等（按文件名移档）。

用法: python scripts/perception_loop.py [--once] [--inbox PATH]
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRINITY_QUIET_IMPORT", "1")

API = "http://127.0.0.1:8001"
DEFAULT_INBOX = os.path.expanduser("~/.trinity/perception_inbox")
IMGS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def perceive(signal: str) -> bool:
    try:
        payload = {"channel": "vision", "signal": signal[:300], "importance": 0.5}
        req = urllib.request.Request(API + "/memory/perceive",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("encoded", False))
    except Exception:
        return False


def refresh_stream():
    try:
        from trinity.brain.situation_stream import refresh
        refresh(force=True)
    except Exception:
        pass


def process_one(path: str) -> str:
    """单张图：语义视觉 → 感知记忆。返回描述或 None。"""
    from PIL import Image
    from trinity.vision import describe_image_any, _semantic_reset
    _semantic_reset()  # 每图重新探测可用性（模型可能刚加载）
    img = Image.open(path)
    desc = describe_image_any(img)
    img.close()
    if not desc:
        return None
    ok = perceive(f"[vision-file {os.path.basename(path)}] " + desc)
    return desc if ok else ("perceived-but-not-encoded " + desc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只处理当前积压后退出")
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--interval", type=int, default=30, help="轮询秒数（守护模式）")
    args = ap.parse_args()

    inbox = args.inbox
    done_dir = os.path.join(inbox, "done")
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(done_dir, exist_ok=True)
    print(f"perception loop | inbox={inbox}")

    processed = 0
    while True:
        files = sorted(f for f in os.listdir(inbox)
                       if f.lower().endswith(IMGS) and os.path.isfile(os.path.join(inbox, f)))
        for name in files:
            src = os.path.join(inbox, name)
            try:
                desc = process_one(src)
                if desc:
                    processed += 1
                    print(f"[perceived] {name} -> {desc[:160]}")
                else:
                    print(f"[skip/desc-fail] {name}")
            except Exception as e:
                print(f"[err] {name}: {str(e)[:100]}")
            # 无论如何归档（防重复处理；失败可在 done 找回）
            dst = os.path.join(done_dir, time.strftime("%Y%m%d_%H%M%S") + "_" + name)
            try:
                shutil.move(src, dst)
            except Exception:
                pass
        if processed:
            refresh_stream()  # 情境流带上最新视觉语义
        if args.once:
            break
        time.sleep(args.interval)
    print("perception loop pass done, processed:", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

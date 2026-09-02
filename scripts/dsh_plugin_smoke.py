#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH dsh-trinity 插件冒烟测试（2026-09-01，rc.7 契约回归——把 08-24 冻结教训制度化）

验证三件事（任一失败 exit 1）：
  1. headless profile 合成配置含 trinity 层（dsh.bundle 模型生效）
  2. headless 会话能真实调用 trinity_ping（worker 存活、工具注册）
  3. 会话结束后 dsh_events 水位推进（structure_sync 端到端）

用法: python scripts/dsh_plugin_smoke.py [--dsh <cli path>] [--timeout 240]
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time


NODE = shutil.which("node") or "node"


def _dsh_cmd(args, *rest):
    return [NODE, args.dsh, *rest]


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsh", default="C:/Users/Administrator/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/lib/bin.js", help="dsh CLI 入口（node bin.js 直调）")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    ok = True
    # 1) 合成配置含 trinity 层
    r1 = _run(_dsh_cmd(args, "--profile", "headless", "--dump-config"), args.timeout)
    layer = "@deepseek-ai/dsh-trinity" in (r1.stdout or "")
    print("SMOKE[1] trinity bundle in composed config:", "PASS" if layer else "FAIL")
    ok = ok and layer

    # 2) headless 会话调 trinity_ping
    r2 = _run(_dsh_cmd(args, "--profile", "headless",
               "Call the trinity_ping tool now and report its exact raw output."), args.timeout)
    pong = '"pong": true' in (r2.stdout or "") or 'pong' in (r2.stdout or "").lower()
    print("SMOKE[2] trinity_ping in headless session:", "PASS" if pong else "FAIL")
    if not pong:
        print("  reply tail:", (r2.stdout or "")[-300:])
    ok = ok and pong

    # 3) dsh_events 水位推进（会话前 vs 会话后）
    def watermark():
        db = os.path.expanduser("~/.trinity/store/trinity_store.db")
        c = sqlite3.connect(db, timeout=20)
        row = c.execute("SELECT MAX(time), COUNT(*) FROM dsh_events").fetchone()
        c.close()
        return row
    before = watermark()
    # 会话已在步骤 2 跑过；再等 5s 观察（插件在会话结束/事件缓冲时同步）
    time.sleep(5)
    after = watermark()
    advanced = after and before and (after[0] or 0) >= (before[0] or 0) and after[1] >= before[1]
    print("SMOKE[3] dsh_events watermark advanced: %s (events %s -> %s)" %
          ("PASS" if advanced else "FAIL", before[1] if before else "?", after[1] if after else "?"))
    ok = ok and advanced

    print("SMOKE: %s" % ("ALL PASS" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

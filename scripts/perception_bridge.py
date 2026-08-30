#!/usr/bin/env python3
"""perception_bridge.py — 感知桥（2026-09，EXECUTION 105.8 认知循环集成）

把 DSH 结构事件流（工具错误/目标完成/关键 todo 变更）自动 feed 感知通道——
具身感知流持续自动流入，不再只靠手动 API。

流程：
  1. 扫描 SQLite dsh_events 最近 N 分钟高显著事件：
     - tool/result 且 payload.error 非空  → channel=error
     - goal/write 完成                    → channel=goal
     - todo/write 关键变更                → channel=session
  2. 调 /memory/perceive（本地 API）编码为感知记忆（习惯化/门控由感知引擎处理）；
  3. 水位文件 ~/.trinity/perception_bridge.watermark 幂等续跑。

用法:
  python scripts/perception_bridge.py --minutes 30
  python scripts/perception_bridge.py --dry-run
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

SQLITE_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
WATERMARK = os.path.expanduser("~/.trinity/perception_bridge.watermark")
API = "http://127.0.0.1:8001/memory/perceive"


def load_watermark() -> float:
    try:
        with open(WATERMARK, "r") as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def save_watermark(ts: float) -> None:
    try:
        with open(WATERMARK, "w") as f:
            f.write(str(ts))
    except Exception:
        pass


def scan(watermark: float, minutes: int) -> list:
    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    since = watermark if watermark > 0 else time.time() - minutes * 60
    cur.execute("""
        SELECT type, payload, time FROM dsh_events
        WHERE time >= ? ORDER BY time ASC LIMIT 500
    """, (since,))
    signals = []
    for etype, payload, ts in cur.fetchall():
        try:
            data = json.loads(payload)
        except Exception:
            continue
        signal = None
        channel = None
        if etype == "tool/result":
            err = data.get("error")
            if err:
                signal = "工具执行错误: " + str(err)[:200]
                channel = "error"
        elif etype == "goal/write":
            st = str(data.get("status") or data.get("state") or "")
            if "complete" in st.lower() or st.lower() in ("done", "completed"):
                signal = "目标完成: " + str(data.get("objective") or data.get("title") or "")[:150]
                channel = "goal"
        if signal and channel:
            signals.append({"channel": channel, "signal": signal, "ts": ts})
    conn.close()
    return signals


def feed(signals: list, dry_run: bool) -> int:
    ok = 0
    for s in signals:
        if dry_run:
            print("  [dry] " + s["channel"] + ": " + s["signal"][:60])
            ok += 1
            continue
        try:
            req = urllib.request.Request(
                API, data=json.dumps({"channel": s["channel"],
                                      "signal": s["signal"]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            print("  " + s["channel"] + ": encoded=" + str(d.get("encoded"))
                  + " salience=" + str(d.get("salience")))
            ok += 1
        except Exception as e:
            print("  feed fail: " + str(e)[:100])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wm = load_watermark()
    signals = scan(wm, args.minutes)
    print(f"signals: {len(signals)} (watermark={wm:.0f})")
    if not signals:
        return 0
    ok = feed(signals, args.dry_run)
    if not args.dry_run:
        save_watermark(max(s["ts"] for s in signals))
    print(f"DONE: fed={ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

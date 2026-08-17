# -*- coding: utf-8 -*-
"""启动 Trinity 双向记忆同步守护进程（BidirectionalSyncDaemon，60s 轮询）。"""
import sys
import os

sys.path.insert(0, r"C:\Users\Administrator\trinity")
os.chdir(r"C:\Users\Administrator\trinity")

from trinity.bridges.auto_syncer import BidirectionalSyncDaemon

if __name__ == "__main__":
    daemon = BidirectionalSyncDaemon(interval=60)
    daemon.run_forever()

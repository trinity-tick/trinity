#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trinity-sync-agent.py — Trinity 多机实时记忆同步代理（落地 round45 设计）

对齐网络最优方案：
  - Mem0 Edge 变体：本地 Trinity 是离线优先的"写缓存"，服务器是"记忆权威汇聚端"
  - SAMEP(arXiv 2507.10562)：content/similarity 幂等 + updated_at 新者胜 + 审计链
  - REST 主通道：POST /agents/memory/bulk_write（服务器零改造，纯用现成端点）

职责（单向 本地→服务器，一期；双向二期）：
  - 游标持久化（~/.trinity/sync-agent-cursor.json），按 updated_at 增量
  - 断线退避重试 + 游标不前进 → 断线续传
  - 服务器 endpooint 幂等（聚合池相似度 merge 天然幂等，重复推送安全）

用法：
  1) 复制 sync-agent.yaml.template 为 ~/.trinity/sync-agent.yaml 并按需配置
  2) python trinity-sync-agent.py            # 前台跑一轮
     python trinity-sync-agent.py --loop     # 连续轮询
     python trinity-sync-agent.py --one       # 只跑一轮（供自检/调试）

P0 概念验证（不跑全量基准）：
  python trinity-sync-agent.py --p0 --source <临时SQLite路径> --api http://127.0.0.1:8001
    — 用临时 SQLite 模拟"电脑 B"，推送到本机 :8001（模拟服务器），验证闭环。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ── 常量 ────────────────────────────────────────────────────────────
DEFAULT_HEADERS = {"X-Agent-ID": "trinity-sync-agent", "X-Agent-Role": "admin"}
BATCH_MAX = 100          # API /bulk_write 单批上限
MAX_PER_CYCLE = 500      # 每轮最多推送条数
BACKOFF_BASE = 2.0       # 断线退避基数，2^n 秒
BACKOFF_MAX = 60.0
DEFAULT_INTERVAL = 3.0   # 默认轮询间隔

# 本地引擎库字段 → 可用的目标字段（聚合池 MemoryWriteRequest）
# 源表 memories: memory_id, content, tags, category, importance, updated_at, status
# 目标要求:      agent_id(必填), content(必填), category, scope, importance, tags, metadata


def resolve_store_db(store_dir: Optional[str] = None) -> Path:
    """解析引擎库 sqlite 路径。
    优先环境 TRINITY_STORE/TRINITY_DB；否则回默认 ~/.trinity/store/trinity_store.db。
    """
    db = os.environ.get("TRINITY_DB")
    if db and Path(db).exists():
        return Path(db)
    base = Path(store_dir or os.environ.get("TRINITY_STORE") or
                (Path.home() / ".trinity" / "store"))
    cand = base if base.name.endswith(".db") else base / "trinity_store.db"
    return cand


def load_cursor(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_updated_at")
    except Exception:
        return None


def save_cursor(path: Path, ts: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_updated_at": ts, "last_run": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_delta(db_path: Path, cursor_ts: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """读取本地引擎库 active + updated_at > cursor 的增量记忆。"""
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    try:
        cur = con.cursor()
        q = ("SELECT memory_id, content, category, importance, tags, updated_at "
             "FROM memories WHERE status='active'")
        args: List[Any] = []
        if cursor_ts:
            q += " AND updated_at > ?"
            args.append(cursor_ts)
        q += " ORDER BY updated_at ASC LIMIT ?"
        args.append(limit)
        cur.execute(q, args)
        rows = cur.fetchall()
        items = []
        for mid, content, category, importance, tags, updated_at in rows:
            if not content:
                continue
            try:
                tag_list = json.loads(tags) if tags else []
            except Exception:
                tag_list = []
            if not isinstance(tag_list, list):
                tag_list = []
            items.append({
                "memory_id": mid,
                "content": content,
                "category": category or "general",
                "importance": float(importance or 0.5),
                "tags": tag_list,
                "updated_at": updated_at,
            })
        return items
    finally:
        con.close()


def build_entries(items: List[Dict[str, Any]], machine: str) -> List[Dict[str, Any]]:
    """把引擎库条目映射为聚合池 MemoryWriteRequest。
    agent_id 用 机器名:memory_id 前缀，服务器按 agent_id 隔离不冲突。
    """
    entries = []
    for it in items:
        entries.append({
            "agent_id": f"{machine}:{it['memory_id']}",
            "content": it["content"],
            "category": it["category"],
            "scope": "cross_agent",
            "importance": it["importance"],
            "tags": it["tags"] or [],
            "metadata": {
                "sync_source": machine,
                "sync_memory_id": it["memory_id"],
                "sync_updated_at": it["updated_at"],
            },
        })
    return entries


class SyncAgent:
    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.api = cfg["server"]["url"].rstrip("/")
        self.headers = {**DEFAULT_HEADERS}
        key = cfg.get("server", {}).get("api_key")
        if key:
            self.headers["Authorization"] = f"Bearer {key}"
        self.machine = cfg.get("server", {}).get("machine", "pc-unknown")
        self.interval = float(cfg.get("sync", {}).get("interval_seconds", DEFAULT_INTERVAL))
        self.batch = int(cfg.get("sync", {}).get("batch_size", BATCH_MAX))
        self.max_per_cycle = int(cfg.get("sync", {}).get("max_per_cycle", MAX_PER_CYCLE))
        self.cursor_file = Path(cfg.get("cursor", {}).get("file") or
                                (Path.home() / ".trinity" / "sync-agent-cursor.json"))
        db = cfg.get("source", {}).get("db") or None
        self.db_path = Path(db) if db else resolve_store_db()

    def push(self, entries: List[Dict[str, Any]]) -> bool:
        """推送一批，成功返回 True（游标前进），失败 False（退避重试）。"""
        for i in range(0, len(entries), self.batch):
            chunk = entries[i:i + self.batch]
            r = requests.post(
                f"{self.api}/agents/memory/bulk_write",
                json={"entries": chunk},
                headers={**self.headers, "Content-Type": "application/json"},
                timeout=120,
            )
            if r.status_code != 200:
                self.logger.warning("push failed %s: %s", r.status_code, r.text[:200])
                return False
            j = r.json()
            self.logger.info("pushed %d (written=%d failed=%d)",
                             len(chunk), j.get("written"), j.get("failed"))
        return True

    def sync_once(self) -> Dict[str, Any]:
        cursor = load_cursor(self.cursor_file)
        items = fetch_delta(self.db_path, cursor, self.max_per_cycle)
        if not items:
            return {"status": "noop", "pushed": 0, "source": str(self.db_path)}
        entries = build_entries(items, self.machine)
        if self.push(entries):
            new_cursor = items[-1]["updated_at"]
            save_cursor(self.cursor_file, new_cursor)
            return {"status": "ok", "pushed": len(items),
                    "last": new_cursor, "source": str(self.db_path)}
        return {"status": "retry-later", "pushed": 0, "source": str(self.db_path)}

    def run_loop(self) -> None:
        self.logger.info("sync-agent started: %s → %s (interval=%.1fs)",
                         self.db_path, self.api, self.interval)
        fail = 0
        while True:
            try:
                res = self.sync_once()
                if res["status"] == "ok":
                    fail = 0
                    self.logger.info("cycle ok, pushed=%d", res["pushed"])
                elif res["status"] == "retry-later":
                    fail += 1
                    wait = min(BACKOFF_BASE ** min(fail, 5), BACKOFF_MAX)
                    self.logger.warning("push failed, backoff %.1fs (fail#%d)", wait, fail)
                    time.sleep(wait)
                    continue
            except Exception as exc:
                fail += 1
                self.logger.error("cycle error: %s", exc)
                time.sleep(min(BACKOFF_BASE ** min(fail, 5), BACKOFF_MAX))
                continue
            time.sleep(self.interval)


def build_logger() -> logging.Logger:
    logger = logging.getLogger("sync-agent")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logf = Path.home() / ".trinity" / "logs" / "sync-agent.log"
    try:
        logf.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logf, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    logger.propagate = False
    return logger


def load_config(path: Optional[str]) -> Dict[str, Any]:
    default_path = Path.home() / ".trinity" / "sync-agent.yaml"
    p = Path(path or default_path)
    if not p.exists():
        # 无配置文件时给内置默认（指向本机/内网，内网环境适用）
        return {
            "server": {"url": "http://127.0.0.1:8001", "api_key": "",
                       "machine": os.environ.get("COMPUTERNAME", "pc-local")},
            "sync": {"interval_seconds": DEFAULT_INTERVAL,
                     "batch_size": BATCH_MAX, "max_per_cycle": MAX_PER_CYCLE},
            "cursor": {"file": str(Path.home() / ".trinity" / "sync-agent-cursor.json")},
            "source": {"db": ""},
        }
    # 简单 yaml 子集解析（键: 值）。先剥 UTF-8 BOM（Windows 编辑器/脚本常带 BOM）。
    raw = p.read_text(encoding="utf-8").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cfg: Dict[str, Any] = {"server": {}, "sync": {}, "cursor": {}, "source": {}}
    cur = None
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.endswith(":") and not line.endswith(": "):
            section = line.rstrip(":").strip()
            if section in cfg:
                cur = cfg[section]
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip(); v = v.strip()
            if cur is not None and len(v) > 0:
                if v.lower() in ("true", "false"):
                    cur[k] = v.lower() == "true"
                elif v.replace(".", "", 1).isdigit():
                    cur[k] = float(v) if "." in v else int(v)
                else:
                    cur[k] = v
    return cfg


def p0_verify(cfg: Dict[str, Any], logger: logging.Logger) -> None:
    """P0 概念验证：临时 SQLite 模拟电脑 B → 本机 :8001（模拟服务器）。
    不跑全量基准。验证: 增量读取 → bulk_write → 服务器内存 + 游标推进 + 幂等。
    """
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "trinity_sync_p0.db"
    # 建临时库
    con = sqlite3.connect(str(tmp))
    con.execute("CREATE TABLE IF NOT EXISTS memories ("
                "memory_id TEXT PRIMARY KEY, content TEXT, category TEXT,"
                "importance REAL, tags TEXT, status TEXT, updated_at TEXT)")
    now = datetime.now(timezone.utc)
    seed = [
        ("m1", "[P0] 电脑B 测试记忆 alpha 首次写入", "episodic", 0.7, '["p0"]', now.isoformat()),
        ("m2", "[P0] 电脑B 测试记忆 beta 首次写入", "decision", 0.8, '["p0"]', now.isoformat()),
    ]
    import uuid
    for sid, content, cat, imp, tags, upd in seed:
        con.execute("INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?)",
                    (f"mem_{uuid.uuid4().hex[:8]}_{sid}", content, cat, imp, tags, "active", upd))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    con.close()
    logger.info("P0 临时库已建，active=%d, 路径=%s", n, tmp)

    cfg["source"]["db"] = str(tmp)
    agent = SyncAgent(cfg, logger)
    agent.cursor_file = Path(tempfile.gettempdir()) / "trinity_sync_p0_cursor.json"
    res = agent.sync_once()
    logger.info("P0 推送结果: %s", json.dumps(res, ensure_ascii=False))

    # 幂等验证：重复跑一轮，应 noop 或已游标推进
    res2 = agent.sync_once()
    logger.info("P0 二次(幂等)结果: %s", json.dumps(res2, ensure_ascii=False))
    # 搜索服务器聚合池确认可检索
    hdr = {**DEFAULT_HEADERS, "Content-Type": "application/json"}
    r = requests.get(f"{agent.api}/agents/memory/search",
                     params={"q": "P0 电脑B 测试记忆", "top_k": 3, "mode": "hybrid"},
                     headers=hdr, timeout=30)
    logger.info("P0 服务器检索 status=%s total=%s",
                r.status_code, (r.json().get("total") if r.ok else "-"))


def main() -> None:
    logger = build_logger()
    ap = argparse.ArgumentParser(description="Trinity multi-node sync agent")
    ap.add_argument("--loop", action="store_true", help="连续轮询")
    ap.add_argument("--one", action="store_true", help="只跑一轮")
    ap.add_argument("--p0", action="store_true", help="P0 概念验证")
    ap.add_argument("--config", default="", help="sync-agent.yaml 路径")
    ap.add_argument("--api", default="", help="覆盖服务器 URL")
    ap.add_argument("--source", default="", help="覆盖源 SQLite 路径")
    ap.add_argument("--machine", default="", help="覆盖机器名")
    args = ap.parse_args()

    cfg = load_config(args.config or None)
    if args.api:
        cfg["server"]["url"] = args.api
    if args.machine:
        cfg["server"]["machine"] = args.machine
    if args.source:
        cfg["source"]["db"] = args.source

    if args.p0:
        p0_verify(cfg, logger)
        return

    agent = SyncAgent(cfg, logger)
    if args.one:
        print(json.dumps(agent.sync_once(), ensure_ascii=False))
        return
    agent.run_loop()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

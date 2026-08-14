#!/usr/bin/env python3
"""
OPT8: A2A 跨进程记忆共享演示
==============================
两个独立进程（各自 SQLite 存储 + 各自 A2A 注册）通过 localhost HTTP 交换
A2A memory.store 传输包：

  alpha（HTTP 服务，:18080）
    - 本地写 3 条记忆
    - POST /a2a/packet   → A2AMemorySync.receive_packet 合并（幂等+冲突解决）
    - GET  /a2a/search?q → 返回本地记忆（跨进程可查）
  beta（客户端进程）
    - 从同一 registry JSON 加载（持久化校验：能看到 alpha 的注册）
    - 向 alpha POST 自己的记忆包（记忆共享给 alpha）
    - 经 alpha 的 /a2a/search 查回自己共享的记忆（跨进程读取成功）

用法:
    python scripts/a2a_cross_process.py --role alpha --port 18080 --workdir <tmp>
    python scripts/a2a_cross_process.py --role beta  --peer http://127.0.0.1:18080 --workdir <tmp>
    python scripts/a2a_cross_process.py --run-all     # 自动编排 alpha 子进程 + beta 客户端
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

ALPHA_AGENT = "alpha-process"
BETA_AGENT = "beta-process"


def build_env(workdir: str, role: str):
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.a2a_memory import AdapterMemoryStore, A2AMemorySync, create_memory_entry
    from trinity.a2a_registry import AgentRegistry

    db = os.path.join(workdir, f"{role}_store.db")
    adapter = SQLiteAdapter(db_path=db)
    adapter.connect()
    store = AdapterMemoryStore(adapter)
    registry = AgentRegistry(db_path=os.path.join(workdir, "a2a_registry.json"))
    sync = A2AMemorySync(
        local_agent_id=ALPHA_AGENT if role == "alpha" else BETA_AGENT,
        registry=registry,
        local_store=store.put,
        local_search=store.search,
    )
    return store, registry, sync, adapter


# ── alpha: HTTP server ─────────────────────────────────────────────────────

class _AlphaHandler(BaseHTTPRequestHandler):
    store = None
    sync = None

    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/a2a/health"):
            return self._json(200, {"status": "ok", "agent": ALPHA_AGENT})
        if self.path.startswith("/a2a/search"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            try:
                results = self.store.search(q, top_k=10) if q else []
                return self._json(200, {"query": q, "count": len(results), "results": results})
            except Exception as e:
                return self._json(500, {"query": q, "error": str(e)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/a2a/packet"):
            length = int(self.headers.get("Content-Length", 0))
            packet = self.rfile.read(length).decode("utf-8")
            try:
                result = self.sync.receive_packet(packet, store=self.store)
                return self._json(200, {
                    "success": result.success,
                    "action": result.action,
                    "peer": result.peer,
                    "conflicts": result.conflicts,
                    "error": result.error or "",
                })
            except Exception as e:
                return self._json(500, {"success": False, "error": str(e)})
        return self._json(404, {"error": "not found"})


def run_alpha(port: int, workdir: str) -> int:
    from trinity.a2a_memory import create_memory_entry
    store, registry, sync, adapter = build_env(workdir, "alpha")
    _AlphaHandler.store = store
    _AlphaHandler.sync = sync

    # alpha 本地写 3 条记忆
    mems = [
        create_memory_entry("alpha deployment note: staging env on 10.0.0.5", source_agent=ALPHA_AGENT),
        create_memory_entry("alpha scheduled job: nightly backup at 02:00", source_agent=ALPHA_AGENT),
        create_memory_entry("alpha preference: use dark theme in dashboards", source_agent=ALPHA_AGENT),
    ]
    for m in mems:
        store.put(m)
    print(f"[alpha] stored {len(mems)} local memories; registry agents: "
          f"{list(registry._agents.keys())}")

    srv = ThreadingHTTPServer(("127.0.0.1", port), _AlphaHandler)
    print(f"[alpha] listening on 127.0.0.1:{port} (ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            adapter.disconnect()
        except Exception:
            pass
    return 0


# ── beta: client ───────────────────────────────────────────────────────────

def run_beta(peer: str, workdir: str) -> int:
    from trinity.a2a_memory import create_memory_entry
    store, registry, sync, adapter = build_env(workdir, "beta")
    results = {"passed": 0, "failed": 0, "details": []}

    def check(name: str, ok: bool, detail: str = ""):
        results["passed" if ok else "failed"] += 1
        results["details"].append(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1) registry 持久化：beta 能看到 alpha 的注册
    alpha_reg = registry._agents.get(ALPHA_AGENT)
    check("registry persistence (beta sees alpha)", alpha_reg is not None,
          f"alpha_reg={bool(alpha_reg)}")

    # 2) beta 本地写一条，打成 memory.store 传输包发给 alpha
    entry = create_memory_entry(
        "beta fact: quarterly review moved to 2026-09-30",
        source_agent=BETA_AGENT, tags=["shared"],
    )
    store.put(entry)
    import dataclasses
    packet = sync.registry.prepare_transfer(
        ALPHA_AGENT, {"action": "memory.store", "entry": dataclasses.asdict(entry)})
    check("packet prepared", bool(packet))
    req = urllib.request.Request(
        f"{peer}/a2a/packet",
        data=packet.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        ack = json.loads(resp.read().decode("utf-8"))
    check("alpha received packet", ack.get("success") is True,
          f"ack={ack}")

    # 3) 跨进程读取：beta 经 alpha 的 /a2a/search 查回自己共享的记忆
    time.sleep(0.3)
    with urllib.request.urlopen(f"{peer}/a2a/search?q=quarterly%20review", timeout=10) as resp:
        sres = json.loads(resp.read().decode("utf-8"))
    hit = any("quarterly review" in str(r.get("content", "")) for r in sres.get("results", []))
    check("cross-process query (beta's memory found on alpha)", hit,
          f"count={sres.get('count')} err={sres.get('error', '')} "
          f"results={[str(r.get('content',''))[:40] for r in sres.get('results', [])]}")

    # 3b) 顺带验证 alpha 本地 3 条也可跨进程查到
    with urllib.request.urlopen(f"{peer}/a2a/search?q=nightly%20backup", timeout=10) as resp:
        s3 = json.loads(resp.read().decode("utf-8"))
    check("cross-process query (alpha's own memory visible)", s3.get("count", 0) >= 1,
          f"count={s3.get('count')}")

    # 4) 幂等：重复发同一包不产生重复
    with urllib.request.urlopen(urllib.request.Request(
            f"{peer}/a2a/packet", data=packet.encode(), headers={"Content-Type": "application/json"},
            method="POST"), timeout=10) as resp:
        ack2 = json.loads(resp.read().decode("utf-8"))
    with urllib.request.urlopen(f"{peer}/a2a/search?q=quarterly%20review", timeout=10) as resp:
        sres2 = json.loads(resp.read().decode("utf-8"))
    check("idempotent resend (no duplicates)", sres2.get("count", 0) == sres.get("count", 0),
          f"count={sres2.get('count')}")

    try:
        adapter.disconnect()
    except Exception:
        pass

    print(f"\n=== beta result: {results['passed']}/{results['passed'] + results['failed']} PASS ===")
    return 0 if results["failed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["alpha", "beta"])
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--peer", default="http://127.0.0.1:18080")
    parser.add_argument("--workdir", default=tempfile.mkdtemp(prefix="a2a_xproc_"))
    parser.add_argument("--run-all", action="store_true")
    args = parser.parse_args()

    if args.run_all:
        workdir = args.workdir
        os.makedirs(workdir, exist_ok=True)
        alpha = subprocess.Popen(
            [sys.executable, __file__, "--role", "alpha", "--port", str(args.port),
             "--workdir", workdir],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            # 等 alpha 就绪
            ready = False
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"{args.peer}/a2a/health", timeout=2) as resp:
                        if json.loads(resp.read().decode()).get("status") == "ok":
                            ready = True
                            break
                except Exception:
                    time.sleep(0.2)
            print(f"[runner] alpha ready: {ready}")
            if not ready:
                out = alpha.stdout.read() if alpha.stdout else ""
                print(f"[runner] alpha output:\n{out}")
                return 1
            code = run_beta(args.peer, workdir)
            return code
        finally:
            alpha.terminate()
            try:
                alpha.wait(timeout=5)
            except subprocess.TimeoutExpired:
                alpha.kill()
            print("[runner] --- alpha subprocess output (tail) ---")
            if alpha.stdout:
                try:
                    tail = alpha.stdout.read()[-2500:]
                    print(tail)
                except Exception as e:
                    print(f"(read err {e})")
    elif args.role == "alpha":
        return run_alpha(args.port, args.workdir)
    elif args.role == "beta":
        return run_beta(args.peer, args.workdir)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())

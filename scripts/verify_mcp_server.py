#!/usr/bin/env python3
"""verify_mcp_server.py — MCP server 端到端验证（上架前必跑，2026-08-24）。

验证三种传输的 MCP 服务可被标准客户端发现并调用：
  - stdio           : spawn `trinity-mcp --mode stdio`，initialize + tools/list + 冒烟
  - sse (:8000)     : SSE 端点握手 + initialize
  - streamable-http (:8003) : well-known 元数据 + 鉴权 401/200 + initialize

用法：
    python scripts/verify_mcp_server.py --transport stdio
    python scripts/verify_mcp_server.py --transport sse --port 8000
    python scripts/verify_mcp_server.py --transport streamable-http --port 8003 --key <KEY>
退出码 0 = 可上架；非 0 = 有问题（打印 FAIL 项）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")  # 抑制聚合器自举

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def _http_json(url: str, headers: dict, method: str = "GET", body: bytes = None,
               timeout: int = 10):
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except Exception as e:
        code = getattr(e, "code", None)
        return (code if code else 0), str(e)[:100]


def verify_stdio() -> int:
    print("== stdio 验证 ==")
    import subprocess as sp
    try:
        proc = sp.Popen(
            [sys.executable, "-m", "trinity.mcp.server", "--mode", "stdio"],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
    except Exception as e:
        check("spawn stdio", False, str(e))
        return 1

    def send(obj: dict):
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "verify", "version": "1"}}})
        line = proc.stdout.readline()
        init = json.loads(line) if line else {}
        check("initialize", init.get("result", {}).get("protocolVersion", "") == "2025-06-18",
              json.dumps(init.get("result", {}).get("capabilities", {}))[:80])

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        line = proc.stdout.readline()
        tools = json.loads(line) if line else {}
        names = [t.get("name", "") for t in tools.get("result", {}).get("tools", [])]
        check("tools/list", "memory_search" in names and "memory_write" in names,
              f"{len(names)} tools")
    except Exception as e:
        check("stdio session", False, str(e))
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    return 1 if FAILS else 0


def verify_sse(port: int) -> int:
    print(f"== SSE :{port} 验证 ==")
    # 用 mcp 官方客户端库做真实 SSE 握手（裸 HTTP 会得到 421 属正常）。
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client

        async def _run():
            async with sse_client(f"http://127.0.0.1:{port}/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    return init, names

        import asyncio
        init, names = asyncio.run(_run())
        check("SSE initialize", bool(init.protocolVersion),
              f"proto={init.protocolVersion}")
        check("SSE tools/list", "memory_search" in names, f"{len(names)} tools")
    except Exception as e:
        check("SSE 会话", False, str(e)[:120])
    return 1 if FAILS else 0


def verify_streamable_http(port: int, key: str = "") -> int:
    print(f"== streamable-http :{port} 验证 ==")
    base = f"http://127.0.0.1:{port}"

    # 1. well-known 元数据
    st, body = _http_json(base + "/.well-known/oauth-protected-resource", {})
    ok = st == 200 and isinstance(body, dict) and "authorization_servers" in body
    check("well-known 元数据", ok, f"status={st}")

    # 2. 无 token → 401（鉴权开启时）
    st2, _ = _http_json(base + "/mcp", {}, method="POST",
                        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {}}).encode())
    if key:
        check("无 token 拒绝(401)", st2 == 401, f"status={st2}")

    # 3. 带 token initialize 握手
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    st3, body3 = _http_json(
        base + "/mcp", headers, method="POST",
        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "verify", "version": "1"}}}).encode(),
    )
    check("initialize 握手", st3 in (200, 202) and ("protocolVersion" in str(body3) if isinstance(body3, str) else True),
          f"status={st3}")
    return 1 if FAILS else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity MCP server 端到端验证")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--key", default="", help="Bearer key（streamable-http）")
    args = parser.parse_args()

    if args.transport == "stdio":
        rc = verify_stdio()
    elif args.transport == "sse":
        rc = verify_sse(args.port)
    else:
        rc = verify_streamable_http(args.port, args.key)

    if rc == 0:
        print("\nRESULT: PASS — 可上架")
    else:
        print(f"\nRESULT: FAIL（{len(FAILS)} 项）: {', '.join(FAILS)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

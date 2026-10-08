#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_http_probe.py — 直连常驻 MCP(:8003) 的 streamable-http 探针（只读）。

## 为什么需要它（§785.6 遗留④：MCP 没有行为探针）

P1-3 的结论里，MCP 侧**三项均未生效**，但那条结论**完全建立在结构性判据**上
（进程启动时刻 vs 修复提交时刻）—— 因为当时"**没有**为 MCP 写行为探针：
它是 streamable-http，需要完整会话握手"。本模块把那句话补齐：
**用最小握手直连 8003，调它自己的 `trinity_diagnostics`**，把 MCP 的结论
从"推断"升级为"**实测**"，并且（配合 R1 的代码指纹）升级为**决定性**。

## 协议（实测跑通的形态，不是照文档猜的）

    POST /mcp  initialize            → 200 + **SSE**（`event: message` + `data: {...}`）
                                       响应头带 `mcp-session-id`
    POST /mcp  notifications/initialized（带 session-id）→ 202
    POST /mcp  tools/call             → 200 + SSE，`result.structuredContent` 即返回值

鉴权：`Authorization: Bearer <TRINITY_MCP_API_KEY>`（实测缺它 ⇒ 401）。
token 来源优先级：环境变量 → `~/.dsh/.credentials.yaml`（**只读，不打印值**）。

## 边界（不冒充）

- **只读**：只调 `trinity_diagnostics`（诊断是只读工具）；不改数据、不重启服务。
- **不新建常驻连接**：一次握手、一次调用、进程退出即断（不占用会话）。
- token 缺失/服务不可达 ⇒ **INCONCLUSIVE**，绝不报 ACTIVE。
- 本模块**不**替 `trinity_search` 等写路径做探针 —— 那会 touch 记忆、污染 U1 读数。

用法：
    python scripts/mcp_http_probe.py             # 人类可读
    python scripts/mcp_http_probe.py --json
    python scripts/mcp_http_probe.py --tool trinity_diagnostics
退出码：0 = 取到身份块且判定 ACTIVE；1 = 未生效/不确定（**这是状态，不是失败**）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_URL = os.environ.get("TRINITY_MCP_URL", "http://127.0.0.1:8003/mcp")
CREDS = os.path.expanduser("~/.dsh/.credentials.yaml")
_KEY_RE = re.compile(r"^\s*(TRINITY_MCP_API_KEY|TRINITY_API_KEY)\s*:\s*(.+)$")


# ── token（纯函数部分可单测）───────────────────────────────────────────────
def parse_token(text: str) -> "tuple[str | None, str]":
    """从 credentials 文本里取 token（**只返回来源标签，不返回/不打印值**）。

    注释行跳过；先认 `TRINITY_MCP_API_KEY`（MCP 专用），没有再认 `TRINITY_API_KEY`。
    """
    found = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _KEY_RE.match(s)
        if m:
            found[m.group(1)] = m.group(2).strip().strip("\"'")
    for k in ("TRINITY_MCP_API_KEY", "TRINITY_API_KEY"):
        if found.get(k):
            return found[k], k
    return None, "not-found"


def load_token() -> "tuple[str | None, str]":
    for k in ("TRINITY_MCP_API_KEY", "TRINITY_API_KEY"):
        v = os.environ.get(k)
        if v:
            return v, "env:" + k
    try:
        # t31：行式读 + strip（键缩进在版本化文件的 `refs` 下；同时让"读站点"的容差形态可核）
        with open(CREDS, encoding="utf-8-sig") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("TRINITY_MCP_API_KEY") or s.startswith("TRINITY_API_KEY"):
                    return parse_token(s)
        return None, "not-found"
    except Exception as e:  # noqa: BLE001
        return None, "creds-unreadable(%s)" % type(e).__name__


# ── 响应解析（streamable-http 可能回 SSE 或裸 JSON）────────────────────────
def parse_rpc(raw: str):
    """把响应体解析成 JSON-RPC 对象；SSE 取最后一条 `data:` 行。解析不出返回 None。"""
    if not raw:
        return None
    s = raw.lstrip()
    if s.startswith("{"):
        try:
            return json.loads(s)
        except Exception:  # noqa: BLE001
            return None
    out = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                out = json.loads(line[5:].strip())
            except Exception:  # noqa: BLE001
                continue
    return out


def extract_tool_result(rpc) -> "tuple[dict | None, str]":
    """从 `tools/call` 的响应里取出工具返回值（structuredContent 优先，其次 content[].text）。"""
    if not isinstance(rpc, dict):
        return None, "响应不是 JSON-RPC 对象"
    if rpc.get("error"):
        return None, "JSON-RPC error: %r" % (rpc["error"],)
    res = rpc.get("result")
    if not isinstance(res, dict):
        return None, "result 不是对象：%r" % (res,)
    sc = res.get("structuredContent")
    if isinstance(sc, dict) and sc:
        return sc, "structuredContent"
    for part in res.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                d = json.loads(part.get("text") or "")
            except Exception:  # noqa: BLE001
                continue
            if isinstance(d, dict):
                return d, "content[].text(JSON)"
    if res.get("isError"):
        return None, "工具报错：%r" % (res.get("content"),)
    return None, "取不到工具返回值（keys=%s）" % sorted(res.keys())


# ── 客户端 ─────────────────────────────────────────────────────────────────
class McpHttpClient:
    """最小 streamable-http 客户端（initialize → initialized → tools/call）。"""

    def __init__(self, url: str = DEFAULT_URL, token: "str | None" = None, timeout: float = 45.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self.last_error = None

    def _post(self, body: dict, extra_headers=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        h.update(extra_headers or {})
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self.last_error = "HTTP %s" % e.code
            try:
                body_txt = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                body_txt = ""
            return e.code, body_txt
        except Exception as e:  # noqa: BLE001
            self.last_error = repr(e)[:160]
            return None, ""

    def initialize(self):
        return self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "dsh-trinity-probe", "version": "1.0"}}})

    def initialized(self):
        return self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: "dict | None" = None, rid: int = 2):
        return self._post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})


def fetch_diagnostics(url: str = DEFAULT_URL) -> dict:
    """握手并取 `trinity_diagnostics` 的返回值（成败都带 `ok` 与原因，绝不抛出）。"""
    tok, src = load_token()
    c = McpHttpClient(url, tok)
    st, raw = c.initialize()
    if st != 200:
        return {"ok": False, "stage": "initialize", "status": st,
                "error": c.last_error or (raw or "")[:160],
                "token_source": src, "auth": ("token" if tok else "none")}
    c.initialized()
    st2, raw2 = c.call_tool("trinity_diagnostics")
    if st2 != 200:
        return {"ok": False, "stage": "tools/call", "status": st2,
                "error": c.last_error or (raw2 or "")[:160],
                "session": bool(c.session_id), "token_source": src}
    payload, how = extract_tool_result(parse_rpc(raw2))
    if not isinstance(payload, dict):
        return {"ok": False, "stage": "parse", "error": how,
                "session": bool(c.session_id), "token_source": src}
    return {"ok": True, "stage": "done", "via": how, "session": bool(c.session_id),
            "token_source": src, "diagnostics": payload}


def mcp_code_provenance(url: str = DEFAULT_URL) -> dict:
    """取 MCP 侧身份块并给出**决定性**判定（服务不可达/无该字段 ⇒ UNKNOWN，不报 ACTIVE）。"""
    from scripts.activation_live_probe import classify_provenance
    r = fetch_diagnostics(url)
    if not r.get("ok"):
        return {"state": "UNKNOWN", "capability_present": None,
                "detail": "MCP 探针未取到诊断（stage=%s, %s）⇒ 不报 ACTIVE"
                          % (r.get("stage"), r.get("error")),
                "probe": {k: r.get(k) for k in ("stage", "status", "error", "token_source")}}
    diag = r["diagnostics"]
    v = classify_provenance(diag.get("code_provenance"), ROOT)
    v["mcp"] = {"via": r.get("via"), "token_source": r.get("token_source"),
                "diagnostics_keys": sorted(diag.keys())[:12]}
    return v


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    v = mcp_code_provenance(a.url)
    if a.json:
        print(json.dumps(v, ensure_ascii=False))
    else:
        print("常驻 MCP(:8003) streamable-http 探针（只读：initialize → tools/call）")
        print("  判定：[%s] %s" % (v.get("state"), v.get("detail")))
        m = v.get("mcp") or {}
        print("  握手：via=%s token=%s 诊断面键=%s"
              % (m.get("via"), m.get("token_source"), m.get("diagnostics_keys")))
        if v.get("stale"):
            print("  陈旧文件（%d）：%s" % (len(v["stale"]), ", ".join(v["stale"][:10])))
    return 0 if v.get("state") == "ACTIVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())

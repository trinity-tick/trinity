# -*- coding: utf-8 -*-
"""trinity/brain/global_workspace.py — 全局工作空间（EXECUTION 331）。

借鉴 Theater of Mind（2026：Global Workspace Theory——GWT 意识
理论）——全局工作空间：信息进入全局空间 → 广播整合 →
意识内容（大脑的意识核心——各模块信息在全局空间整合）。

与工作记忆（WM 容量）互补：WM=保持；本模块=全局广播。
Trinity 现在：
  broadcast(content, source): 全局广播（信息进入→广播）
  workspace_content(): 当前工作空间内容（意识内容）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/global_workspace.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"workspace": [], "broadcasts": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def broadcast(content: str, source: str = "perception") -> dict:
    """全局广播：信息进入全局工作空间（容量 ~7±2）。"""
    st = _load()
    ws = st.get("workspace", [])
    ws.append({"content": str(content)[:60], "source": str(source)[:15],
               "ts": __import__("time").time()})
    # 容量限制（7±2）
    ws = ws[-7:]
    st["workspace"] = ws
    st["broadcasts"] += 1
    _save(st)
    return {"broadcasted": True, "workspace_size": len(ws),
            "source": str(source)[:15], "content": str(content)[:40],
            "note": f"全局广播（{str(source)[:10]}→工作空间）——当前 {len(ws)}/7"}


def workspace_content() -> dict:
    """工作空间内容：当前意识内容（广播整合）。"""
    st = _load()
    ws = st.get("workspace", [])
    sources = {}
    for w in ws:
        sources[w["source"]] = sources.get(w["source"], 0) + 1
    return {"content": [w["content"] for w in ws],
            "sources": sources,
            "integration": len(ws) >= 2,
            "note": f"全局工作空间：{len(ws)} 项内容整合（{'多源整合' if len(sources) >= 2 else '单源'}）"}

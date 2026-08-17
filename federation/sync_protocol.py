# -*- coding: utf-8 -*-
"""B4 联邦记忆 — 多实例快照同步协议 v0。

子命令:
    export --api URL --agent ID --out snapshot.json
    import --api URL --file snapshot.json [--since TS] [--agent ID]
    diff   --file-a A.json --file-b B.json

用法示例见 federation/README.md
"""
import argparse
import json
import sys
import time
import requests

DEFAULT_HEADERS = {"X-Agent-ID": "federation", "X-Agent-Role": "admin"}


def export(api: str, agent: str, out: str) -> dict:
    r = requests.get(f"{api}/agents/memory/export", params={"format": "json"},
                     headers=DEFAULT_HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"exported_at": time.time(), "agent": agent, "data": data},
                  f, ensure_ascii=False, indent=1)
    return {"status": "ok", "file": out, "size": len(json.dumps(data, ensure_ascii=False))}


def _to_entries(data: dict, since: str = "") -> list:
    """把 export 响应归一为 bulk_write 的 entries。"""
    entries = []
    mems = data.get("memories") or data.get("results") or data.get("entries") or []
    if isinstance(data, list):
        mems = data
    for m in mems:
        content = m.get("content")
        if not content:
            continue
        if since and m.get("created_at", "") < since:
            continue
        entries.append({
            "content": content,
            "tags": m.get("tags") or [],
            "category": m.get("category") or "general",
            "importance": m.get("importance") or 0.5,
            "agent_id": m.get("agent_id") or "federation-import",
        })
    return entries


def import_(api: str, path: str, since: str = "", agent: str = "") -> dict:
    snap = json.load(open(path, encoding="utf-8"))
    data = snap.get("data", snap)
    entries = _to_entries(data, since)
    if agent:
        for e in entries:
            e["agent_id"] = agent
    if not entries:
        return {"status": "noop", "written": 0}
    r = requests.post(f"{api}/agents/memory/bulk_write", json={"entries": entries[:100]},
                      headers=DEFAULT_HEADERS, timeout=120)
    r.raise_for_status()
    return {"status": "ok", "written": len(entries), "response": r.json()}


def diff(a_path: str, b_path: str) -> dict:
    def ids(p):
        snap = json.load(open(p, encoding="utf-8"))
        data = snap.get("data", snap)
        return {m.get("memory_id") or m.get("content", "")[:40]
                for m in (data.get("memories") or data.get("results") or []) if m.get("content")}
    ia, ib = ids(a_path), ids(b_path)
    return {"only_a": len(ia - ib), "only_b": len(ib - ia), "common": len(ia & ib)}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("export"); p1.add_argument("--api", required=True); p1.add_argument("--agent", required=True); p1.add_argument("--out", required=True)
    p2 = sub.add_parser("import"); p2.add_argument("--api", required=True); p2.add_argument("--file", required=True); p2.add_argument("--since", default=""); p2.add_argument("--agent", default="")
    p3 = sub.add_parser("diff"); p3.add_argument("--file-a", required=True); p3.add_argument("--file-b", required=True)
    args = ap.parse_args()

    if args.cmd == "export":
        print(json.dumps(export(args.api, args.agent, args.out), ensure_ascii=False))
    elif args.cmd == "import":
        print(json.dumps(import_(args.api, args.file, args.since, args.agent), ensure_ascii=False))
    elif args.cmd == "diff":
        print(json.dumps(diff(args.file_a, args.file_b), ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
